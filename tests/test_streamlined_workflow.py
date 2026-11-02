"""Offline regression checks for scheduling and binary validation, not OAuth proof."""

from pathlib import Path

import pytest
from test_frontend import unit_settings

from classroomautowork import ui_jobs
from classroomautowork.batch import course_batches
from classroomautowork.errors import WorkflowError
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
            on_assignment(result, {**result, "status": "prepared"})
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


@pytest.mark.parametrize("mapped", [True, False])
def test_main_button_retries_only_fill_when_document_mapping_exists(tmp_path, monkeypatch, mapped):
    jobs = FrontendJobs(unit_settings(tmp_path))
    target = {"course_id": "1", "assignment_id": "2"}
    atomic_json(jobs.root / "pending.json", {"assignments": [target]})
    atomic_json(
        jobs.root / "models.json",
        {
            "default_model": "offline-model",
            "default_effort": None,
            "models": [{"model": "offline-model"}],
        },
    )
    generation = {"status": "completed"}
    if mapped:
        generation["document_answers_sha256"] = "verified-mapping-hash"
    item = {
        **target,
        "status": "needs_document",
        "package": str(tmp_path),
        "generation": generation,
    }
    jobs._jobs["last"] = {
        "kind": "review",
        "ai_confirmed": True,
        "items": [item],
        "created_at": "2026-01-01",
    }
    calls = []
    monkeypatch.setattr(jobs, "_start", lambda *args, **kw: calls.append((args, kw)))
    jobs.start_selected([target], ai_confirmed=True)
    assert calls[0][0][0] == ("fill" if mapped else "review")
    if mapped:
        assert calls[0][0][1][0]["package"] == str(tmp_path)


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
