"""Offline regression checks for scheduling and binary validation, not OAuth proof."""

import hashlib
from pathlib import Path

import pytest
from test_frontend import unit_settings

from classroomautowork import buzz, ui_jobs
from classroomautowork.batch import course_batches
from classroomautowork.errors import ConfigurationError, WorkflowError
from classroomautowork.local import atomic_json
from classroomautowork.ui_jobs import FrontendJobs


def test_course_batches_keep_priority_deduplicate_and_share_one_sync():
    items = [
        {"course_id": c, "assignment_id": a}
        for c, a in [("2", "5"), ("1", "4"), ("2", "6"), ("2", "5")]
    ]
    assert [[x["assignment_id"] for x in group] for group in course_batches(items)] == [
        ["5", "6"],
        ["4"],
    ]


@pytest.mark.parametrize("first_fails", [False, True])
def test_deliver_first_course_before_reading_next_and_isolate_course_failures(
    tmp_path, monkeypatch, first_fails
):
    jobs = FrontendJobs(unit_settings(tmp_path))
    items = [
        {"course_id": c, "assignment_id": a, "title": "Offline assignment"}
        for c, a in [("2", "5"), ("1", "4"), ("2", "6")]
    ]
    atomic_json(jobs.root / "pending.json", {"assignments": items})
    sequence = []

    def prepare(settings, *, targets, on_assignment, **_):
        sequence.append(("prepare", targets))
        if first_fails and targets[0][0] == "2":
            raise WorkflowError("Offline course permission failure")
        packages = []
        for c, a in targets:
            result = {"course_id": c, "assignment_id": a, "package": str(tmp_path / a)}
            on_assignment(result, {**result, "status": "prepared_for_codex"})
            packages.append(result)
        return {"packages": packages, "cache": {"reused": 2, "processed": 1, "failed": 0}}

    def generate(package, **_):
        sequence.append(("generate", Path(package).name))
        return {"package": str(package), "status": "materials_ready"}

    monkeypatch.setattr(ui_jobs, "prepare", prepare)
    monkeypatch.setattr(ui_jobs, "generate_answer", generate)
    job = jobs.start_selected([{k: x[k] for k in ("course_id", "assignment_id")} for x in items])
    assert jobs.wait_idle()
    saved = jobs.job(job["id"])
    if first_fails:
        assert sequence == [
            ("prepare", [("2", "5"), ("2", "6")]),
            ("prepare", [("1", "4")]),
            ("generate", "4"),
        ]
        assert saved["status"] == "completed_with_issues"
    else:
        assert sequence == [
            ("prepare", [("2", "5"), ("2", "6")]),
            ("generate", "5"),
            ("generate", "6"),
            ("prepare", [("1", "4")]),
            ("generate", "4"),
        ]
        assert saved["status"] == "completed"
        assert saved["cache"] == {"reused": 4, "processed": 2, "failed": 0}


def test_binary_hash_is_reused_only_inside_one_run_and_changes_are_detected(tmp_path, monkeypatch):
    path = tmp_path / "local-model.bin"
    path.write_bytes(b"offline model bytes")
    calls = []

    def digest(file):
        calls.append(file)
        return hashlib.sha256(file.read_bytes()).hexdigest()

    monkeypatch.setattr(buzz, "sha256_file", digest)
    with buzz.validation_session():
        first = buzz._verified_hash(path)
        with buzz.validation_session():
            assert buzz._verified_hash(path) == first
        assert len(calls) == 1
        path.write_bytes(b"changed local model bytes")
        assert buzz._verified_hash(path) != first
        assert len(calls) == 2
    with buzz.validation_session():
        buzz._verified_hash(path)
    assert len(calls) == 3


def test_binary_changed_while_hashing_is_never_cached(tmp_path, monkeypatch):
    path = tmp_path / "changing.bin"
    path.write_bytes(b"first")

    def changing(file):
        result = hashlib.sha256(file.read_bytes()).hexdigest()
        file.write_bytes(b"changed while reading")
        return result

    monkeypatch.setattr(buzz, "sha256_file", changing)
    with buzz.validation_session(), pytest.raises(ConfigurationError, match="changed while"):
        buzz._verified_hash(path)


def test_retry_fill_failure_does_not_download_or_generate_again(tmp_path, monkeypatch):
    jobs = FrontendJobs(unit_settings(tmp_path))
    job = {
        "id": "d" * 32,
        "kind": "review",
        "status": "completed_with_issues",
        "ai_confirmed": True,
        "items": [
            {
                "course_id": "1",
                "assignment_id": "2",
                "status": "needs_form",
                "package": str(tmp_path),
                "generation": {"status": "completed"},
            }
        ],
    }
    jobs._jobs[job["id"]] = job
    monkeypatch.setattr(jobs, "_recover_local_answers", lambda _: False)
    calls = []
    monkeypatch.setattr(jobs, "_start", lambda *args, **kw: calls.append((args, kw)) or job)
    monkeypatch.setattr(
        jobs, "start_selected", lambda *a, **kw: pytest.fail("Must not prepare or generate again")
    )
    jobs.retry(job["id"])
    assert calls[0][0][0] == "fill"
    assert calls[0][1]["ai_confirmed"] is True


def test_old_results_survive_more_than_thirty_refresh_jobs(tmp_path):
    jobs = FrontendJobs(unit_settings(tmp_path))
    jobs._jobs["old-result"] = {
        "id": "old-result",
        "kind": "review",
        "status": "completed",
        "created_at": "2026-01-01",
        "items": [{"course_id": "1", "assignment_id": "2", "status": "document_ready"}],
    }
    for index in range(35):
        jobs._jobs[str(index)] = {
            "id": str(index),
            "kind": "refresh",
            "created_at": f"2026-02-{index:02}",
            "items": [],
        }
    snapshot = jobs.bootstrap()
    assert len(snapshot["jobs"]) == 30
    assert all(job["kind"] == "refresh" for job in snapshot["jobs"])
    assert snapshot["result_jobs"][0]["items"][0]["assignment_id"] == "2"


def test_retry_of_multi_assignment_fill_keeps_every_unfinished_item(tmp_path, monkeypatch):
    jobs = FrontendJobs(unit_settings(tmp_path))
    job = {
        "id": "e" * 32,
        "kind": "fill",
        "status": "completed_with_issues",
        "ai_confirmed": True,
        "items": [
            {"course_id": "1", "assignment_id": str(i), "status": status, "package": str(tmp_path)}
            for i, status in [(2, "document_ready"), (3, "needs_form"), (4, "needs_document")]
        ],
    }
    jobs._jobs[job["id"]] = job
    monkeypatch.setattr(jobs, "_recover_local_answers", lambda _: False)
    calls = []
    monkeypatch.setattr(jobs, "_start", lambda *args, **kw: calls.append((args, kw)) or job)
    jobs.retry(job["id"])
    assert calls[0][0][0] == "fill"
    assert [item["assignment_id"] for item in calls[0][0][1]] == ["3", "4"]
