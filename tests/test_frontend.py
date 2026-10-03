"""Offline UI boundary fixtures. Never used as OAuth or real-course verification."""

import io
import json
import subprocess
import sys
import threading
import zipfile
from http.client import HTTPConnection

import pytest
from test_workflow import ReaderFixture, prepared_fixture

from classroomautowork import drafting, ui_jobs, workflow
from classroomautowork.config import Settings
from classroomautowork.errors import RunCancelled, WorkflowError
from classroomautowork.local import atomic_json
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


def test_unknown_rule_generates_real_checks_without_invoking_codex(tmp_path, monkeypatch):
    package, _, _, _ = prepared_fixture(tmp_path, "unknown")
    monkeypatch.setattr(
        drafting, "codex_command", lambda: pytest.fail("Unknown policy must not launch Codex")
    )
    receipt = drafting.generate_review(package)
    assert receipt["status"] == "needs_user_input" and receipt["draft_sha256"] is None
    review = json.loads((package / "review.json").read_text(encoding="utf-8"))
    requirements = json.loads((package / "requirements.json").read_text(encoding="utf-8"))
    assert [x["requirement"] for x in review["requirement_checks"]] == [
        x["text"] for x in requirements["sources"]
    ]
    assert all(x["status"] == "needs_user" for x in review["requirement_checks"])
    assert review["questions"]
    assert drafting.generate_review(package)["reused"]


def test_codex_process_contract_keeps_configuration_and_validates_output(tmp_path, monkeypatch):
    package, review, draft, _ = prepared_fixture(tmp_path)
    runtime = tmp_path / "runtime"
    skill = runtime / "skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Unit skill", encoding="utf-8")
    atomic_json(runtime / "runtime.json", {"skill": str(skill)})
    monkeypatch.setattr(drafting, "state_root", lambda: runtime)
    script = tmp_path / "offline_codex_fixture.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "prompt = sys.stdin.read()\n"
        "p = pathlib.Path(sys.argv[sys.argv.index('--cd')+1])\n"
        "(p/'review.json').write_bytes((p/'model-review.json').read_bytes())\n"
        "(p/'unit-command.json').write_text(json.dumps(sys.argv))\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'command_execution'}}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(drafting, "codex_command", lambda: [sys.executable, str(script)])
    result = drafting.generate_review(package)
    assert result["status"] == "ready_for_human_review"
    command = json.loads((package / "unit-command.json").read_text())
    assert command[-1] == "-" and "--json" in command
    assert not set(command) & {
        "-m",
        "--model",
        "--ignore-user-config",
        "--ignore-rules",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
    }
    # An invalid changed draft is not reusable, even when a previous receipt exists.
    draft.write_text("changed [E:invented]", encoding="utf-8")
    assert drafting.reusable_review(package) is None


def test_cancel_terminates_the_owned_codex_process(tmp_path, monkeypatch):
    package, _, _, _ = prepared_fixture(tmp_path)
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("Unit", encoding="utf-8")
    atomic_json(tmp_path / "runtime.json", {"skill": str(skill)})
    monkeypatch.setattr(drafting, "state_root", lambda: tmp_path)
    script = tmp_path / "idle_fixture.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(drafting, "codex_command", lambda: [sys.executable, str(script)])
    processes, real_popen = [], subprocess.Popen

    def observe(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(drafting.subprocess, "Popen", observe)
    cancel = threading.Event()
    timer = threading.Timer(0.7, cancel.set)
    timer.start()
    try:
        with pytest.raises(RunCancelled):
            drafting.generate_review(package, cancelled=cancel.is_set)
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
    with pytest.raises(WorkflowError, match="不在真实待办"):
        jobs.start_selected([{"course_id": "1", "assignment_id": "999"}])
    job = jobs.start_selected([{"course_id": "1", "assignment_id": "2"}])
    assert jobs.wait_idle()
    assert jobs.job(job["id"])["items"][0]["status"] == "needs_user"
    restarted = FrontendJobs(unit_settings(tmp_path))
    repeated = restarted.retry(job["id"])
    assert restarted.wait_idle()
    assert restarted.job(repeated["id"])["items"][0]["reused_review"]
    assert calls == [[("1", "2")], [("1", "2")]]
    # The fixture contained an old draft; an unvalidated draft must not appear in the UI.
    assert restarted.package_view(repeated["id"], "1:2")["text"]["draft.md"] == ""


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
