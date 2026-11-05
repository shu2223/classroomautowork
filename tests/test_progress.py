"""Offline progress fixtures; these never represent live OAuth or model success."""

import shutil
import subprocess
from pathlib import Path

import pytest
from test_frontend import local_server, request, unit_settings  # noqa: F401

from classroomautowork.progress import ProgressUpdate
from classroomautowork.ui_jobs import FrontendJobs


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


@pytest.mark.parametrize("script", ["progress_view.js", "provider_view.js"])
def test_browser_progress_states_are_truthful(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the production frontend's pure state logic")
    subprocess.run([node, str(Path(__file__).with_name(script))], check=True, capture_output=True)
