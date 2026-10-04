"""Read-only companion for an already-running, older frontend. Never opens Store."""

import argparse
import json
import math
import re
import shutil
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import Settings
from .errors import WorkflowError
from .google_read import drive_attachments
from .local import atomic_json, require_private_path, state_root
from .ui_jobs import read_json
from .webapp import LocalHandler


class CacheObserver:
    def __init__(self, root):
        self.root = require_private_path(root)
        self._lock = threading.Lock()
        self._durations = {}
        self._names = {}

    def _duration(self, path):
        if path not in self._durations:
            value = None
            source = Path(path).resolve()
            engine = shutil.which("ffprobe")
            if source.is_relative_to(self.root / "attachments") and source.is_file() and engine:
                try:
                    result = subprocess.run(
                        [
                            engine,
                            "-v",
                            "error",
                            "-show_entries",
                            "format=duration",
                            "-of",
                            "default=noprint_wrappers=1:nokey=1",
                            str(source),
                        ],
                        capture_output=True,
                        timeout=5,
                        check=True,
                        creationflags=subprocess.CREATE_NO_WINDOW
                        if __import__("os").name == "nt"
                        else 0,
                    )
                    value = float(result.stdout)
                    if not 0 < value <= 86400:
                        value = None
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
            self._durations[path] = value
        return self._durations[path]

    def _name(self, file_id, rows):
        if file_id not in self._names:
            name = None
            for row in rows:
                if row["kind"] != "classroom-snapshot" or not row["result"]:
                    continue
                path = Path(json.loads(row["result"]).get("path", "")).resolve()
                if (
                    not path.is_relative_to(self.root / "snapshots")
                    or not path.is_file()
                    or path.stat().st_size > 1_000_000
                ):
                    continue
                source = read_json(path, {})
                match = next((x for x in drive_attachments(source) if x["id"] == file_id), None)
                if match and match.get("title"):
                    name = str(match["title"])[:200]
                    break
            self._names[file_id] = name
        return self._names[file_id]

    def enrich(self, job):
        if job.get("progress") or job.get("status") != "running" or job.get("kind") != "review":
            return job
        match = re.fullmatch(r"Checking attachment ([A-Za-z0-9_-]+)", job.get("message", ""))
        if not match:
            return job
        try:
            with self._lock:
                db = sqlite3.connect(
                    (self.root / "tasks.sqlite").as_uri() + "?mode=ro", uri=True, timeout=0.2
                )
                try:
                    db.row_factory = sqlite3.Row
                    rows = db.execute(
                        "SELECT key,kind,status,updated_at,result FROM tasks WHERE updated_at>=? ORDER BY updated_at DESC LIMIT 1500",
                        (job["created_at"],),
                    ).fetchall()
                finally:
                    db.close()
                running = [x for x in rows if x["status"] == "running"]
                media = next((x for x in running if x["kind"] == "media"), None)
                download = next((x for x in running if x["kind"] == "drive-download"), None)
                extract = next((x for x in running if x["kind"] == "document"), None)
                update = None
                if media:
                    folder = self.root / "processed" / media["key"][:24]
                    segments = [
                        x
                        for x in rows
                        if x["kind"] == "transcript-segment"
                        and x["updated_at"] >= media["updated_at"]
                    ]
                    complete = 0
                    for row in segments:
                        if row["status"] == "done" and row["result"]:
                            paths = [
                                Path(x["path"]).resolve()
                                for x in json.loads(row["result"]).get("artifacts", [])
                            ]
                            if paths and all(p.is_relative_to(folder) for p in paths):
                                complete += 1
                    total = None
                    raw = next(
                        (
                            x
                            for x in rows
                            if x["kind"] == "drive-download" and x["status"] == "done"
                        ),
                        None,
                    )
                    config = read_json(state_root() / "buzz.json", {})
                    length = config.get("segment_seconds")
                    if raw and isinstance(length, int) and 1 <= length <= 86400:
                        duration = self._duration(json.loads(raw["result"]).get("path", ""))
                        if duration:
                            total = math.ceil(duration / length)
                    update = {
                        "stage": "transcribe",
                        "current": complete,
                        "total": total,
                        "unit": "segments",
                        "started_at": media["updated_at"],
                        "activity_at": segments[0]["updated_at"]
                        if segments
                        else media["updated_at"],
                    }
                    message = (
                        f"已从本地缓存观察到带时间戳转录：完成 {complete}"
                        + (f"/{total}" if total else "")
                        + " 段；当前段完成后更新"
                    )
                elif download:
                    folder = self.root / "attachments" / download["key"][:24]
                    parts = list(folder.glob("*.part"))
                    current = parts[0].stat().st_size if len(parts) == 1 else 0
                    meta = (
                        read_json(parts[0].with_suffix(parts[0].suffix + ".json"), {})
                        if len(parts) == 1
                        else {}
                    )
                    total = int(meta["size"]) if str(meta.get("size", "")).isdigit() else None
                    update = {
                        "stage": "download",
                        "current": current,
                        "total": total,
                        "unit": "bytes",
                        "started_at": download["updated_at"],
                        "activity_at": download["updated_at"],
                    }
                    if parts:
                        from datetime import UTC, datetime

                        update["activity_at"] = datetime.fromtimestamp(
                            parts[0].stat().st_mtime, UTC
                        ).isoformat()
                    message = "正在下载附件，显示已落盘的实际字节数"
                elif extract:
                    update = {
                        "stage": "extract",
                        "started_at": extract["updated_at"],
                        "activity_at": extract["updated_at"],
                    }
                    message = "正在提取文档；旧任务未记录总页数，完成后继续"
                if update:
                    job = {
                        **job,
                        "progress": {
                            **update,
                            "filename": self._name(match[1], rows),
                            "source": "read_only_cache_observation",
                        },
                        "message": message,
                    }
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            pass  # Observation failure never changes or interrupts the original task.
        return job


class MonitorHandler(LocalHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return super().do_GET()
        if not self._guard():
            return
        if path == "/api/health":
            return self._send(200, {"app": "classroomautowork-progress", "status": "running"})
        if path != "/api/bootstrap" and not re.fullmatch(r"/api/jobs/[a-f0-9]{32}", path):
            return self._send(403, {"error": "只读进度窗口仅提供任务状态；请返回作业助手操作。"})
        request = urllib.request.Request(
            self.server.source_origin + path,
            headers={"Authorization": "Bearer " + self.server.source_token},
        )
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                value = json.load(response)
            if path == "/api/bootstrap":
                value.update(
                    monitor_only=True,
                    source_url=self.server.source_origin + "/#" + self.server.source_token,
                )
                value["jobs"] = [
                    self.server.observer.enrich(x) if x["id"] == value.get("active_job_id") else x
                    for x in value["jobs"]
                ]
            else:
                value = self.server.observer.enrich(value)
            self._send(200, value)
        except (OSError, ValueError, urllib.error.URLError):
            self._send(502, {"error": "原作业助手暂时无法响应；本窗口会重连，不会停止后台任务。"})

    def do_POST(self):
        self._deny(403, "只读进度窗口不会修改、暂停、重试或启动任务。")


def serve_monitor(settings, *, open_browser=True):
    import secrets

    root = settings.data_dir / "ui"
    source = read_json(root / "server.json")
    if not source or not isinstance(source.get("port"), int):
        raise WorkflowError("没有找到正在运行的原作业助手。")
    server = ThreadingHTTPServer(("127.0.0.1", 0), MonitorHandler)
    server.daemon_threads = True
    server.origin = f"http://127.0.0.1:{server.server_port}"
    server.token = secrets.token_urlsafe(32)
    server.source_origin = f"http://127.0.0.1:{source['port']}"
    server.source_token = source["token"]
    server.observer = CacheObserver(settings.data_dir)
    atomic_json(root / "progress-monitor.json", {"port": server.server_port, "token": server.token})
    if open_browser:
        webbrowser.open(server.origin + "/#" + server.token)
    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Observe a running Classroom job without restarting it"
    )
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    serve_monitor(Settings.load().validated(), open_browser=not args.no_browser)
