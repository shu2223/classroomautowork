"""Offline UI boundary fixtures. Never used as OAuth or real-course verification."""

import io
import json
import subprocess
import sys
import threading
import zipfile
from http.client import HTTPConnection
from pathlib import Path

import pytest
from test_workflow import ReaderFixture, prepared_fixture

from classroomautowork import claude_agent, drafting, ui_jobs, workflow
from classroomautowork.config import Settings
from classroomautowork.errors import RunCancelled, WorkflowError
from classroomautowork.local import atomic_json, sha256_file
from classroomautowork.store import Store
from classroomautowork.ui_jobs import FrontendJobs
from classroomautowork.webapp import LocalServer


class SelectedReader(ReaderFixture):
    submitted = False
    own_reads = 0
    sync_reads = 0

    def courses(self):
        raise AssertionError("Explicit selection must not discover unrelated courses")

    def own_submissions(self, _):
        self.own_reads += 1
        return [
            {"courseWorkId": "2", "state": "TURNED_IN" if self.submitted else "CREATED"},
            {"courseWorkId": "3", "state": "NEW"},
            {"courseWorkId": "4", "state": "NEW"},
        ]

    def assignments(self, cid):
        self.sync_reads += 1
        base = super().assignments(cid)[0]
        return [{**base, "id": str(i)} for i in (2, 3, 4)]


def unit_settings(root):
    return Settings("unit-test@example.invalid", root / "client.json", root).validated()


def test_multiple_explicit_selections_revalidate_states_and_sync_course_once(tmp_path, monkeypatch):
    reader = SelectedReader()
    monkeypatch.setattr(workflow.KnowledgeClient, "from_settings", lambda _: reader)
    events = []
    result = workflow.prepare(
        unit_settings(tmp_path),
        targets=[("1", "2"), ("1", "3"), ("1", "2")],
        on_assignment=lambda target, data: events.append((target["assignment_id"], data["status"])),
    )
    assert [x["assignment_id"] for x in result["packages"]] == ["2", "3"]
    assert reader.own_reads == reader.sync_reads == 1
    assert events == [
        ("2", "preparing"),
        ("2", "prepared"),
        ("3", "preparing"),
        ("3", "prepared"),
    ]
    reader.submitted = True
    with pytest.raises(WorkflowError, match="no longer confirmed pending"):
        workflow.prepare(unit_settings(tmp_path), targets=[("1", "2")])
    assert reader.sync_reads == 1


def test_pause_is_not_swallowed_as_an_attachment_warning(tmp_path):
    def cancel(message):
        raise RunCancelled("unit pause")

    with Store(tmp_path) as store, pytest.raises(RunCancelled):
        workflow.sync_course(ReaderFixture(), unit_settings(tmp_path), store, "1", progress=cancel)


@pytest.mark.parametrize("policy", ["unknown", "allowed", "forbidden"])
def test_unchecked_ai_never_invokes_codex_even_if_course_allows_it(tmp_path, monkeypatch, policy):
    package, _, _, _ = prepared_fixture(tmp_path, policy)
    monkeypatch.setattr(
        claude_agent, "claude_command", lambda: pytest.fail("Unchecked must not launch Codex")
    )
    receipt = drafting.generate_review(package, ai_confirmed=False)
    assert receipt["status"] == "needs_user_input" and receipt["draft_sha256"] is None
    review = json.loads((package / "review.json").read_text(encoding="utf-8"))
    requirements = json.loads((package / "requirements.json").read_text(encoding="utf-8"))
    assert [x["requirement"] for x in review["requirement_checks"]] == [
        x["text"] for x in requirements["sources"]
    ]
    generation = json.loads((package / "generation.json").read_text())
    assert generation["status"] == "not_invoked" and generation["thread_id"] is None
    assert all(x["status"] == "needs_user" for x in review["requirement_checks"])


def offline_rpc(tmp_path, monkeypatch, package, review):
    """Claude CLI fixture, never counted as an actual model or OAuth validation.

    Each turn is logged to requests.jsonl as {"method": "turn/start", "params": {"input", "argv"}}.
    """
    result = {
        "review": review,
        "draft": (package / "draft.md").read_text(encoding="utf-8"),
        "summary": "Offline unit result",
        "requirements_complete": True,
        "missing_sources": [],
        "used_evidence_ids": review["claim_checks"][0]["evidence_ids"],
    }
    atomic_json(tmp_path / "fixture-result.json", result)
    skill = tmp_path / "runtime" / "skill"
    (skill / "references").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("Offline unit skill", encoding="utf-8")
    (skill / "references/review-format.md").write_text("Offline format", encoding="utf-8")
    monkeypatch.setattr(drafting, "SKILL", skill)
    script = tmp_path / "offline_rpc.py"
    script.write_text(
        "import json,pathlib,sys\n"
        "root=pathlib.Path(__file__).parent\n"
        "result=json.loads((root/'fixture-result.json').read_text(encoding='utf-8'))\n"
        "argv=sys.argv[1:]; model=argv[argv.index('--model')+1]\n"
        "def send(x): print(json.dumps(x),flush=True)\n"
        "message=json.loads(sys.stdin.readline())\n"
        "with (root/'requests.jsonl').open('a') as f: f.write(json.dumps({'method':'turn/start','params':{'input':message['message']['content'],'argv':argv}})+'\\n')\n"
        "send({'type':'system','subtype':'init','model':model,'session_id':'unit-session','tools':['StructuredOutput'],'mcp_servers':[]})\n"
        "send({'type':'assistant','message':{'content':[{'type':'tool_use','name':'StructuredOutput'}]}})\n"
        "send({'type':'result','subtype':'success','is_error':False,'structured_output':result,'usage':{'output_tokens':1}})\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(claude_agent, "claude_command", lambda: [sys.executable, str(script)])


def test_actual_result_is_recorded_and_tampering_blocks_reuse(tmp_path, monkeypatch):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    receipt = drafting.generate_review(
        package, ai_confirmed=True, model="unit-model", effort="high"
    )
    variant = Path(receipt["package"])
    assert receipt["generation"]["thread_id"] == "unit-session"
    assert sha256_file(variant / "model-result.json") == receipt["generation"]["result_sha256"]
    assert drafting.reusable_review(variant) is not None
    (variant / "model-result.json").write_bytes(b"tampered actual result")
    assert drafting.reusable_review(variant) is None


@pytest.mark.parametrize("policy", ["allowed", "unknown"])
def test_checked_ai_uses_selected_model_preserves_permissions_and_reuses_only_valid_result(
    tmp_path, monkeypatch, policy
):
    package, _, _, review = prepared_fixture(tmp_path, policy)
    offline_rpc(tmp_path, monkeypatch, package, review)
    result = drafting.generate_review(package, ai_confirmed=True, model="unit-model", effort="high")
    assert result["status"] == "ready_for_human_review"
    variant = Path(result["package"])
    manifest = json.loads((variant / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["policy"]["ai_use"] == policy
    assert manifest["ai_confirmation"] and manifest["policy"]["can_draft"]
    requests = [json.loads(x) for x in (tmp_path / "requests.jsonl").read_text().splitlines()]
    argv = requests[0]["params"]["argv"]
    assert argv[argv.index("--model") + 1] == "unit-model"
    assert argv[argv.index("--effort") + 1] == "high"
    # Course text is data: the model gets no tools, no MCP servers and no saved session.
    assert argv[argv.index("--tools") + 1] == ""
    assert {"--no-session-persistence", "--safe-mode"} <= set(argv)
    assert not {"--dangerously-skip-permissions", "--permission-mode"} & set(argv)
    assert drafting.generate_review(package, ai_confirmed=True, model="unit-model", effort="high")[
        "reused"
    ]
    (variant / "draft.md").write_text("changed [E:invented]", encoding="utf-8")
    assert drafting.reusable_review(variant, model="unit-model", effort="high") is None


def test_user_choice_generates_despite_advisory_course_ai_prohibition(tmp_path, monkeypatch):
    package, _, _, review = prepared_fixture(tmp_path, "forbidden")
    offline_rpc(tmp_path, monkeypatch, package, review)
    result = drafting.generate_review(package, ai_confirmed=True, model="unit-model", effort="high")
    assert result["draft_sha256"]
    manifest = json.loads((Path(result["package"]) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["policy"]["ai_use"] == "forbidden"
    assert manifest["policy"]["course_ai_rules_advisory"] and manifest["policy"]["can_draft"]


def test_metadata_only_revision_reuses_real_output_but_changed_content_does_not(
    tmp_path, monkeypatch
):
    from classroomautowork.policy import confirmed_gate

    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    first = drafting.generate_review(package, ai_confirmed=True, model="unit-model", effort="high")

    class SameContent(ReaderFixture):
        version = "2"
        text = "食品安全 unit revision 1"

        def download(self, metadata, destination):
            destination.mkdir(parents=True)
            path = destination / "unit.txt"
            path.write_text(self.text, encoding="utf-8")
            return {"path": str(path)}

    reader = SameContent()
    settings = unit_settings(tmp_path)
    with Store(tmp_path) as store:
        course = workflow.sync_course(reader, settings, store, "1")
        prepared = workflow.prepare_assignment(settings, store, course, "2")
    monkeypatch.setattr(
        claude_agent,
        "claude_command",
        lambda: pytest.fail("Identical content must not invoke AI again"),
    )
    second = drafting.generate_review(
        Path(prepared["package"]), ai_confirmed=True, model="unit-model", effort="high"
    )
    assert second["reused"] and second["package"] == first["package"]
    assert second["generation"]["started_at"] == first["generation"]["started_at"]
    reader.version, reader.text = "3", "Changed unit content"
    with Store(tmp_path) as store:
        course = workflow.sync_course(reader, settings, store, "1")
        changed = workflow.prepare_assignment(settings, store, course, "2")
    path = Path(changed["package"])
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    manifest["policy"] = confirmed_gate(manifest["policy"], True)
    assert drafting.equivalent_review(path, manifest, model="unit-model", effort="high") is None


def test_cancel_terminates_the_owned_claude_process(tmp_path, monkeypatch):
    script = tmp_path / "idle_fixture.py"
    script.write_text("import sys,time\nsys.stdin.readline()\ntime.sleep(30)\n", encoding="utf-8")
    processes, real_popen = [], subprocess.Popen

    def observe(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(claude_agent.subprocess, "Popen", observe)
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    try:
        with pytest.raises(RunCancelled):
            claude_agent.run_turn(
                [{"type": "text", "text": "unit"}],
                {"type": "object"},
                model="unit-model",
                effort=None,
                cwd=tmp_path,
                cancelled=cancel.is_set,
                command=[sys.executable, str(script)],
            )
    finally:
        timer.cancel()
    assert processes[0].poll() is not None


def seed_pending(jobs):
    atomic_json(
        jobs.root / "pending.json",
        {
            "assignments": [
                {
                    "course_id": "1",
                    "assignment_id": "2",
                    "title": "Unit requirement",
                    "course_name": "Unit course",
                }
            ],
            "needs_confirmation": [],
            "inaccessible_courses": [],
            "complete_discovery": True,
            "refreshed_at": "2026-01-01T00:00:00+00:00",
        },
    )


def test_persistent_job_retries_reuses_review_and_rejects_unlisted_selection(tmp_path, monkeypatch):
    package, _, _, _ = prepared_fixture(tmp_path, "unknown")
    jobs = FrontendJobs(unit_settings(tmp_path))
    seed_pending(jobs)
    calls = []

    def offline_prepare(settings, *, targets, on_assignment, **kwargs):
        calls.append(targets)
        result = {
            "course_id": "1",
            "assignment_id": "2",
            "package": str(package),
            "status": "prepared",
        }
        on_assignment(result, result)
        return {"packages": [result], "cache": {"reused": 1, "processed": 0, "failed": 0}}

    monkeypatch.setattr(ui_jobs, "prepare", offline_prepare)
    monkeypatch.setattr(
        jobs, "models", lambda: pytest.fail("Materials-only jobs must not need Codex")
    )
    with pytest.raises(WorkflowError, match="不在真实待办"):
        jobs.start_selected([{"course_id": "1", "assignment_id": "999"}])
    job = jobs.start_selected([{"course_id": "1", "assignment_id": "2"}])
    assert jobs.wait_idle()
    assert jobs.job(job["id"])["items"][0]["status"] == "materials_ready"
    restarted = FrontendJobs(unit_settings(tmp_path))
    repeated = restarted.retry(job["id"])
    assert restarted.wait_idle()
    assert restarted.job(repeated["id"])["ai_confirmed"] is False
    assert restarted.job(repeated["id"])["items"][0]["status"] == "materials_ready"
    assert calls == [[("1", "2")], [("1", "2")]]
    # The fixture contained an old draft; an unvalidated draft must not appear in the UI.
    assert restarted.package_view(repeated["id"], "1:2")["text"]["draft.md"] == ""


def test_http_ai_confirmation_rejects_text_and_retry_preserves_checked_choice(
    local_server, monkeypatch
):
    jobs = local_server.jobs
    monkeypatch.setattr(jobs, "_run", lambda _: None)
    selection = [{"course_id": "1", "assignment_id": "2"}]
    status, _, _ = request(
        local_server,
        "/api/review",
        method="POST",
        origin=local_server.origin,
        body={"selected": selection, "ai_confirmed": "true"},
    )
    assert status == 400 and not jobs.bootstrap()["jobs"]
    atomic_json(
        jobs.root / "models.json",
        {
            "default_model": "unit-model",
            "default_effort": "high",
            "models": [
                {"model": "unit-model", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}
            ],
        },
    )
    job = jobs.start_selected(selection, ai_confirmed=True, model="unit-model", effort="high")
    jobs._jobs[job["id"]]["status"] = "failed"
    jobs._active = None
    repeated = jobs.retry(job["id"])
    assert (
        repeated["ai_confirmed"]
        and repeated["model"] == "unit-model"
        and repeated["effort"] == "high"
    )
    jobs._active = None


def test_restart_marks_live_records_interrupted_and_suppresses_private_errors(
    tmp_path, monkeypatch
):
    jobs = FrontendJobs(unit_settings(tmp_path))
    seed_pending(jobs)
    identifier = "a" * 32
    atomic_json(
        jobs.root / "jobs" / (identifier + ".json"),
        {
            "id": identifier,
            "status": "running",
            "kind": "review",
            "items": [],
            "created_at": "unit",
        },
    )
    restored = FrontendJobs(unit_settings(tmp_path))
    assert restored.job(identifier)["status"] == "interrupted"
    monkeypatch.setattr(
        ui_jobs.KnowledgeClient,
        "from_settings",
        lambda _: (_ for _ in ()).throw(RuntimeError("private-token-unit-value")),
    )
    job = restored.refresh()
    assert restored.wait_idle()
    assert "private-token-unit-value" not in json.dumps(restored.job(job["id"]))


def request(server, path, *, method="GET", body=None, token=True, origin=None, host=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + server.token
    if origin:
        headers["Origin"] = origin
    if host:
        headers["Host"] = host
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body)
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


@pytest.fixture
def local_server(tmp_path):
    jobs = FrontendJobs(unit_settings(tmp_path))
    seed_pending(jobs)
    server = LocalServer(jobs)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield server
    server.shutdown()
    server.server_close()
    worker.join(timeout=5)


def test_loopback_http_blocks_unauthed_cross_origin_and_rebinding_requests(local_server):
    server = local_server
    assert server.server_address[0] == "127.0.0.1"
    assert request(server, "/api/bootstrap", token=False)[0] == 401
    assert request(server, "/api/bootstrap", host="evil.example")[0] == 403
    assert (
        request(server, "/api/refresh", method="POST", body={}, origin="https://evil.example")[0]
        == 403
    )
    assert request(server, "/api/refresh", method="POST", body={})[0] == 403
    assert not server.jobs.bootstrap()["jobs"]
    status, headers, data = request(server, "/")
    assert status == 200 and b"unit-test@example.invalid" not in data
    assert (
        headers["Cache-Control"] == "no-store"
        and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    )
    assert request(server, "/../settings.json")[0] == 404
    assert request(server, "/api/bootstrap")[0] == 200


def test_policy_api_only_accepts_user_rule_fields_and_valid_evidence(local_server):
    server = local_server
    endpoint = "/api/policies/1"
    assert (
        request(server, endpoint, method="PUT", origin=server.origin, body={"ai_use": "allowed"})[0]
        == 400
    )
    assert (
        request(
            server,
            endpoint,
            method="PUT",
            origin=server.origin,
            body={"command": "run untrusted code"},
        )[0]
        == 400
    )
    assert (
        request(
            server,
            endpoint,
            method="PUT",
            origin=server.origin,
            body={"ai_use": "limited", "policy_evidence": "Unit syllabus"},
        )[0]
        == 400
    )
    status, _, data = request(
        server,
        endpoint,
        method="PUT",
        origin=server.origin,
        body={
            "ai_use": "allowed",
            "policy_evidence": "User supplied unit syllabus",
            "personal_facts": ["User supplied unit fact"],
        },
    )
    assert status == 200 and json.loads(data)["ai_use"] == "allowed"
    assert request(server, "/api/policies/999")[0] == 400
    assert (
        request(
            server,
            endpoint,
            method="PUT",
            origin=server.origin,
            body={"max_attachment_bytes": "run command"},
        )[0]
        == 400
    )
    assert (
        request(
            server, endpoint, method="PUT", origin=server.origin, body={"max_attachment_bytes": -1}
        )[0]
        == 400
    )
    assert (
        request(server, endpoint, method="PUT", origin=server.origin, body={"max_pdf_pages": 5001})[
            0
        ]
        == 400
    )
    assert (
        request(
            server,
            endpoint,
            method="PUT",
            origin=server.origin,
            body={"max_attachment_bytes": 1024 * 1024 * 1024, "max_pdf_pages": 500},
        )[0]
        == 200
    )


def test_review_preview_and_export_do_not_disclose_unvalidated_draft(local_server):
    jobs = local_server.jobs
    package, _, _, _ = prepared_fixture(jobs.settings.data_dir, "unknown")
    drafting.blocked_review(package)
    identifier = "b" * 32
    jobs._jobs[identifier] = {
        "id": identifier,
        "status": "completed",
        "kind": "review",
        "created_at": "unit",
        "items": [{"course_id": "1", "assignment_id": "2", "package": str(package)}],
    }
    endpoint = f"/api/jobs/{identifier}/items/1:2"
    status, _, data = request(local_server, endpoint)
    assert status == 200 and json.loads(data)["text"]["draft.md"] == ""
    status, headers, data = request(local_server, endpoint + "/download")
    assert status == 200 and headers["Content-Type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert "draft.md" not in archive.namelist()
        assert {"requirements.json", "evidence.json", "checklist.md", "questions.md"} <= set(
            archive.namelist()
        )
    assert request(local_server, endpoint + "/../../client.json")[0] == 404


def public(server, path, *, access=True, **options):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"Host": "yishuapp.com"}
    if access:
        headers["Cf-Access-Jwt-Assertion"] = "unit-access-assertion"
    if options.get("origin"):
        headers["Origin"] = options["origin"]
    body = options.get("body")
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body)
    connection.request(options.get("method", "GET"), path, body=body, headers=headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_public_path_needs_cloudflare_access_and_stays_under_its_prefix(local_server):
    assert public(local_server, "/classroom/", access=False)[0] == 403
    assert public(local_server, "/classroom/api/bootstrap", access=False)[0] == 403
    status, headers, page = public(local_server, "/classroom/")
    assert status == 200 and b'src="app.js"' in page
    assert public(local_server, "/classroom")[1]["Location"] == "/classroom/"
    assert public(local_server, "/classroom/api/bootstrap")[0] == 200
    # The public host never reaches the unprefixed loopback paths.
    assert public(local_server, "/api/bootstrap")[0] == 403
    assert public(local_server, "/")[0] == 403


def test_public_writes_need_the_public_origin(local_server):
    path = "/classroom/api/refresh"
    assert public(local_server, path, method="POST", body={})[0] == 403
    evil = public(local_server, path, method="POST", body={}, origin="https://evil.example")
    assert evil[0] == 403
    loopback = public(local_server, path, method="POST", body={}, origin=local_server.origin)
    assert loopback[0] == 403
    assert (
        public(local_server, path, method="POST", body={"x": 1}, origin="https://yishuapp.com")[0]
        == 400
    )  # Reached the handler, which rejects unexpected parameters.
