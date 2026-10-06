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

from classroomautowork import drafting, ui_jobs, workflow
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
    monkeypatch.setattr(workflow, "require_connection", lambda _: None)
    monkeypatch.setattr(workflow, "credentials_for", lambda _: (None, None))
    monkeypatch.setattr(workflow, "GoogleReader", lambda _: reader)
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
        ("2", "prepared_for_codex"),
        ("3", "preparing"),
        ("3", "prepared_for_codex"),
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
        drafting, "codex_command", lambda: pytest.fail("Unchecked must not launch Codex")
    )
    receipt = drafting.generate_review(package, ai_confirmed=False)
    assert receipt["status"] == "needs_user_input" and receipt["draft_sha256"] is None
    review = json.loads((package / "review.json").read_text(encoding="utf-8"))
    requirements = json.loads((package / "requirements.json").read_text(encoding="utf-8"))
    assert [x["requirement"] for x in review["requirement_checks"]] == [
        x["text"] for x in requirements["sources"]
    ]
    generation = json.loads((package / "codex-generation.json").read_text())
    assert generation["status"] == "not_invoked" and generation["thread_id"] is None
    assert all(x["status"] == "needs_user" for x in review["requirement_checks"])


def offline_rpc(tmp_path, monkeypatch, package, review):
    """Protocol fixture, never counted as an actual model or OAuth validation."""
    runtime = tmp_path / "runtime"
    skill = runtime / "skill"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("Offline unit skill", encoding="utf-8")
    (skill / "references/review-format.md").write_text("Offline format", encoding="utf-8")
    atomic_json(runtime / "runtime.json", {"skill": str(skill)})
    monkeypatch.setattr(drafting, "state_root", lambda: runtime)
    result = {
        "review": review,
        "draft": (package / "draft.md").read_text(encoding="utf-8"),
        "summary": "Offline unit result",
        "requirements_complete": True,
        "missing_sources": [],
        "used_evidence_ids": review["claim_checks"][0]["evidence_ids"],
    }
    atomic_json(tmp_path / "fixture-result.json", result)
    script = tmp_path / "offline_rpc.py"
    script.write_text(
        "import json,pathlib,sys\n"
        "root=pathlib.Path(__file__).parent\n"
        "result=json.loads((root/'fixture-result.json').read_text(encoding='utf-8'))\n"
        "def send(x): print(json.dumps(x),flush=True)\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line); m=r.get('method'); p=r.get('params',{})\n"
        " with (root/'requests.jsonl').open('a') as f: f.write(json.dumps(r)+'\\n')\n"
        " if 'id' not in r: continue\n"
        " if m=='initialize': answer={'userAgent':'offline-unit'}\n"
        " elif m=='thread/start': answer={'thread':{'id':'unit-thread'},'model':p['model'],'modelProvider':'offline','reasoningEffort':p.get('config',{}).get('model_reasoning_effort')}\n"
        " elif m=='turn/start': answer={'turn':{'id':'unit-turn'}}\n"
        " else: answer={}\n"
        " send({'id':r['id'],'result':answer})\n"
        " if m=='turn/start':\n"
        "  send({'method':'item/completed','params':{'threadId':'unit-thread','item':{'type':'agentMessage','text':json.dumps(result)}}})\n"
        "  send({'method':'turn/completed','params':{'threadId':'unit-thread','turn':{'id':'unit-turn','status':'completed'}}})\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(drafting, "codex_command", lambda: [sys.executable, str(script)])


def test_multiline_actual_protocol_result_is_reusable_on_windows(tmp_path, monkeypatch):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    script = tmp_path / "offline_rpc.py"
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            "'text':json.dumps(result)", "'text':json.dumps(result,indent=2)"
        ),
        encoding="utf-8",
    )
    receipt = drafting.generate_review(
        package, ai_confirmed=True, model="unit-model", effort="high"
    )
    variant = Path(receipt["package"])
    assert b"\n" in (variant / "codex-result.json").read_bytes()
    assert sha256_file(variant / "codex-result.json") == receipt["generation"]["result_sha256"]
    assert drafting.reusable_review(variant) is not None
    (variant / "codex-result.json").write_bytes(b"tampered actual result")
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
    start = next(x["params"] for x in requests if x.get("method") == "thread/start")
    assert start["model"] == "unit-model" and start["config"] == {"model_reasoning_effort": "high"}
    assert not set(start) & {
        "approvalPolicy",
        "approvalsReviewer",
        "sandbox",
        "modelProvider",
        "baseInstructions",
        "developerInstructions",
    }
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
        drafting, "codex_command", lambda: pytest.fail("Identical content must not invoke AI again")
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


def test_cancel_terminates_the_owned_codex_process(tmp_path, monkeypatch):
    from classroomautowork import codex_rpc

    script = tmp_path / "idle_fixture.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    processes, real_popen = [], subprocess.Popen

    def observe(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(codex_rpc.subprocess, "Popen", observe)
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    try:
        with pytest.raises(RunCancelled):
            codex_rpc.CodexClient([sys.executable, str(script)], cancelled=cancel.is_set)
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
            "status": "prepared_for_codex",
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
        jobs.root / "codex-models.json",
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


def test_approval_accepts_only_current_user_choice(local_server):
    jobs = local_server.jobs
    identifier, approval_id = "c" * 32, "d" * 32
    jobs._jobs[identifier] = {"approval": {"id": approval_id}}
    jobs._active = identifier
    with pytest.raises(WorkflowError):
        jobs.approve(identifier, "e" * 32, "accept")
    with pytest.raises(WorkflowError):
        jobs.approve(identifier, approval_id, "acceptForSession")
    assert jobs.approve(identifier, approval_id, "decline")["status"] == "answered"
    with pytest.raises(WorkflowError):
        jobs.approve(identifier, approval_id, "accept")
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
        ui_jobs,
        "credentials_for",
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
