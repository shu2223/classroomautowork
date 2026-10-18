"""Inventory and worker/ETA integration, using only synthetic local events."""

import json

import pytest
from test_frontend import unit_settings

from classroomautowork.job_progress import JobProgress
from classroomautowork.progress import ProgressUpdate
from classroomautowork.ui_jobs import FrontendJobs


def fixture_job():
    return {
        "id": "e" * 32,
        "kind": "review",
        "status": "running",
        "model": "offline-model",
        "effort": "high",
        "ai_confirmed": True,
        "events": [],
        "items": [
            {"course_id": "1", "assignment_id": "2", "status": "preparing"},
            {"course_id": "3", "assignment_id": "4", "status": "waiting"},
        ],
    }


def test_unknown_future_courses_prevent_incomplete_overall_prediction(tmp_path):
    job = fixture_job()
    now = [0.0]
    tracker = JobProgress(job, tmp_path / "history.json", clock=lambda: now[0])
    tracker.update({"stage": "course_sync", "course_id": "1", "workload_files": []})
    tracker.update(
        {
            "stage": "download",
            "course_id": "1",
            "file_id": "file",
            "current": 0,
            "total": 1000,
            "unit": "bytes",
        }
    )
    now[0] = 10
    tracker.update(
        {
            "stage": "download",
            "course_id": "1",
            "file_id": "file",
            "current": 200,
            "total": 1000,
            "unit": "bytes",
        }
    )
    result = tracker.snapshot()
    assert result["stage"]["speed"] == pytest.approx(20)
    assert result["stage"]["remaining_seconds"] == pytest.approx(40)
    assert result["overall"]["remaining_seconds"] is None
    assert result["overall"]["scope_complete"] is False
    assert result["overall"]["unknown_tasks"] > 0


@pytest.mark.parametrize("cached", [True, False])
def test_partial_segment_caches_do_not_train_full_attachment_cost(tmp_path, cached):
    now = [0.0]
    tracker = JobProgress(fixture_job(), tmp_path / "history.json", clock=lambda: now[0])
    recorded = []
    tracker.estimator.observe_duration = lambda *args: recorded.append(args)
    context = {"course_id": "1", "file_id": "x", "filename": "lecture.flac"}
    tracker.update({**context, "stage": "transcribe", "phase": "start", "current": 1})
    now[0] = 20
    tracker.update(
        {**context, "stage": "transcribe", "phase": "complete", "current": 2, "cached": cached}
    )
    now[0] = 30
    tracker.update(
        {
            **context,
            "stage": "transcribe",
            "phase": "infer",
            "phase_current_seconds": 10,
            "cached": False,
        }
    )
    now[0] = 40
    tracker.update({**context, "stage": "attachment_done"})
    assert bool(recorded) is (not cached)
    if recorded:
        assert recorded[0][0] == "attachment_total_media"


def test_failed_attachments_leave_inventory_but_never_train_success(tmp_path):
    job = fixture_job()
    tracker = JobProgress(job, tmp_path / "history.json")
    tracker.estimator.observe_duration = lambda *args: pytest.fail(
        "A failed file cannot train history"
    )
    tracker.update(
        {
            "stage": "course_sync",
            "course_id": "1",
            "workload_files": [{"file_id": "x", "filename": "lecture.mp4"}],
        }
    )
    tracker.update({"stage": "attachment_failed", "course_id": "1", "file_id": "x", "failed": True})
    assert ("1", "x") in tracker.finished_files


def test_generated_answer_history_waits_for_entire_item_completion(tmp_path):
    job = fixture_job()
    item = job["items"][0]
    now = [0.0]
    tracker = JobProgress(job, tmp_path / "history.json", clock=lambda: now[0])
    recorded = []
    tracker.estimator.observe_duration = lambda *args: recorded.append(args)
    item["status"] = "drafting"
    tracker.item(item, {"status": "drafting"})
    tracker.item(item, {"generation": {"status": "starting"}})
    now[0] = 50
    item["status"] = "ready"
    tracker.item(item, {"status": "ready"})
    assert not recorded
    tracker.pause(True)
    now[0] = 150
    tracker.pause(False)
    now[0] = 160
    item["status"] = "document_ready"
    tracker.item_done(item)
    assert recorded == [("answer_total", 60, tracker.context)]


def test_status_reads_recompute_estimate_without_saving_or_forging_activity(tmp_path, monkeypatch):
    jobs = FrontendJobs(unit_settings(tmp_path))
    job = fixture_job()
    identifier = job["id"]
    jobs._jobs[identifier] = job
    now = [0.0]
    jobs._progress[identifier] = JobProgress(job, tmp_path / "history.json", clock=lambda: now[0])
    jobs._log(
        identifier, ProgressUpdate("synthetic", "download", current=0, total=100, unit="bytes")
    )
    now[0] = 10
    jobs._log(
        identifier, ProgressUpdate("synthetic", "download", current=20, total=100, unit="bytes")
    )
    before = json.dumps(job, sort_keys=True)
    monkeypatch.setattr(jobs, "_save", lambda _: pytest.fail("Reading cannot save"))
    first = jobs.job(identifier)
    now[0] = 20
    second = jobs.job(identifier)
    assert (
        second["estimate"]["stage"]["remaining_seconds"]
        > first["estimate"]["stage"]["remaining_seconds"]
    )
    assert first["progress"]["activity_at"] == second["progress"]["activity_at"]
    assert json.dumps(job, sort_keys=True) == before


def test_codex_thread_creation_does_not_split_assignment_timing(tmp_path):
    job = fixture_job()
    now = [0.0]
    tracker = JobProgress(job, tmp_path / "history.json", clock=lambda: now[0])
    item = job["items"][0]
    tracker.item(item, {"status": "drafting"})
    now[0] = 1
    tracker.route(item, ["validate"])
    now[0] = 4
    tracker.update({"stage": "ai", "item_id": "offline-codex-thread-uuid"})
    assert tracker.snapshot()["stage"]["elapsed_seconds"] == 3
    assert tracker.current["item_id"] == "1:2"


def test_cached_answer_and_google_authorization_do_not_train_processing_duration(tmp_path):
    now = [0.0]
    job = fixture_job()
    tracker = JobProgress(job, tmp_path / "cache-history.json", clock=lambda: now[0])
    item = job["items"][0]
    tracker.item(item, {"status": "drafting"})
    tracker.route(item, ["validate"])
    tracker.item(item, {"generation": {"status": "completed"}})
    now[0] = 30
    item["status"] = "ready"
    tracker.item_done(item)
    assert not (tmp_path / "cache-history.json").exists()
    auth = {**fixture_job(), "kind": "document_auth", "items": []}
    tracker = JobProgress(auth, tmp_path / "auth-history.json", clock=lambda: now[0])
    now[0] = 600
    assert tracker.snapshot()["state"] == "paused"
    tracker.finish(True)
    assert not (tmp_path / "auth-history.json").exists()
