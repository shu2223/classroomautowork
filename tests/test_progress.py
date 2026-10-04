"""Offline progress fixtures; these never represent live OAuth or model success."""

import json
import shutil
import sqlite3
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from filelock import FileLock
from test_frontend import local_server, request, unit_settings  # noqa: F401

from classroomautowork.live_progress import CacheObserver, MonitorHandler
from classroomautowork.progress import ProgressUpdate
from classroomautowork.ui_jobs import FrontendJobs


def test_live_cache_observation_never_resets_a_running_pipeline(tmp_path, monkeypatch):
    path = tmp_path / "tasks.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE tasks(key TEXT,kind TEXT,status TEXT,updated_at TEXT,result TEXT)")
    folder = tmp_path / "processed" / ("a" * 24)
    srt = folder / "segment" / "transcript.srt"
    srt.parent.mkdir(parents=True)
    srt.write_text("offline segment", encoding="utf-8")
    rows = [
        ("a" * 64, "media", "running", "2026-01-01T00:00:01+00:00", None),
        (
            "b" * 64,
            "transcript-segment",
            "done",
            "2026-01-01T00:02:00+00:00",
            json.dumps({"artifacts": [{"path": str(srt)}]}),
        ),
        ("c" * 64, "transcript-segment", "running", "2026-01-01T00:02:01+00:00", None),
        (
            "d" * 64,
            "drive-download",
            "done",
            "2026-01-01T00:00:01+00:00",
            json.dumps({"path": str(tmp_path / "attachments" / "offline.mp4")}),
        ),
    ]
    db.executemany("INSERT INTO tasks VALUES(?,?,?,?,?)", rows)
    db.commit()
    monkeypatch.setattr("classroomautowork.live_progress.state_root", lambda: tmp_path)
    (tmp_path / "buzz.json").write_text('{"segment_seconds":600}')
    observer = CacheObserver(tmp_path)
    monkeypatch.setattr(observer, "_duration", lambda _: 1200)
    job = {
        "status": "running",
        "kind": "review",
        "message": "Checking attachment unit-id",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    with FileLock(str(tmp_path / "pipeline.lock")):
        value = observer.enrich(job)
        assert value["progress"]["stage"] == "transcribe"
        assert value["progress"]["current"] == 1 and value["progress"]["total"] == 2
        assert value["progress"]["activity_at"] == "2026-01-01T00:02:01+00:00"
        assert list(db.execute("SELECT * FROM tasks")) == rows
        assert "progress" not in job
    db.close()


def test_status_requests_do_not_forge_activity_or_persist_heartbeat(tmp_path, monkeypatch):
    jobs = FrontendJobs(unit_settings(tmp_path))
    identifier = "a" * 32
    jobs._jobs[identifier] = {
        "id": identifier,
        "events": [],
        "items": [],
        "updated_at": "original",
        "status": "running",
    }
    jobs._active = identifier
    monkeypatch.setattr(jobs, "_save", lambda _: pytest.fail("Reading status cannot save progress"))
    value = jobs.job(identifier)
    assert value["runtime"]["worker_alive"] is False
    assert value["updated_at"] == "original" and value["events"] == []
    assert "runtime" not in jobs._jobs[identifier]


def test_structured_progress_preserves_start_time_but_tracks_real_new_activity(tmp_path):
    jobs = FrontendJobs(unit_settings(tmp_path))
    identifier = "a" * 32
    jobs._jobs[identifier] = {"id": identifier, "events": [], "items": [], "status": "running"}
    jobs._log(identifier, ProgressUpdate("下载中", "download", current=0, total=10, file_id="unit"))
    started = jobs.job(identifier)["progress"]["started_at"]
    jobs._log(identifier, ProgressUpdate("下载中", "download", current=5, total=10, file_id="unit"))
    assert jobs.job(identifier)["progress"]["started_at"] == started
    assert jobs.job(identifier)["progress"]["current"] == 5
    assert len(jobs.job(identifier)["events"]) == 1
    jobs._log(identifier, "开始下一阶段")
    assert "progress" not in jobs.job(identifier)


def test_monitor_can_only_read_status_and_cannot_pause_or_start(local_server):  # noqa: F811
    before = local_server.jobs.bootstrap()
    server = ThreadingHTTPServer(("127.0.0.1", 0), MonitorHandler)
    server.origin = f"http://127.0.0.1:{server.server_port}"
    server.token = "offline-monitor"
    server.source_origin = local_server.origin
    server.source_token = local_server.token
    server.observer = CacheObserver(local_server.jobs.settings.data_dir)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, raw = request(server, "/api/bootstrap")
        assert status == 200 and json.loads(raw)["monitor_only"] is True
        assert request(server, "/api/bootstrap", token=False)[0] == 401
        for route in ("/api/review", "/api/shutdown", "/api/documents/authorize"):
            assert request(server, route, method="POST", body={}, origin=server.origin)[0] == 403
        assert request(server, "/api/codex/models")[0] == 403
        assert local_server.jobs.bootstrap()["jobs"] == before["jobs"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_browser_progress_states_are_truthful():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the production frontend's pure state logic")
    subprocess.run(
        [node, str(Path(__file__).with_name("progress_view.js"))], check=True, capture_output=True
    )
