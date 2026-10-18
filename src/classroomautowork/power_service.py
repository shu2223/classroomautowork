"""Observe completion without interrupting an existing Classroom worker."""

import argparse
import json
import queue
import re
import secrets
import threading
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

from filelock import FileLock, Timeout

from .config import Settings
from .errors import WorkflowError
from .local import atomic_json
from .power import ACTIVE, ASSIGNMENT_KINDS, ShutdownController


def is_new_work(path):
    return path in {"/api/review", "/api/refresh", "/api/documents/authorize"} or bool(
        re.fullmatch(
            r"/api/jobs/[a-f0-9]{32}(?:/items/[0-9]+:[0-9]+)?/(?:retry|fill|continue|answers)", path
        )
    )


def latest_completed_job(job, jobs):
    """Only power off for the latest batch, including fast intervening jobs."""
    if (
        not isinstance(job, dict)
        or not job.get("id")
        or job.get("status") != "completed"
        or not ShutdownController._all_items_complete(job)
    ):
        return False
    try:
        created = datetime.fromisoformat(job["created_at"])
        if created.tzinfo is None:
            return False
        found = False
        for other in jobs:
            started = datetime.fromisoformat(other["created_at"])
            if started.tzinfo is None or not other.get("id"):
                return False
            if other["id"] == job["id"]:
                if started != created or other.get("status") != "completed":
                    return False
                found = True
            elif started >= created:
                return False
        return found
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


class PowerWatch:
    def __init__(self, controller, lookup, *, interval=1):
        self.controller, self.lookup, self.interval = controller, lookup, interval
        self._stop = threading.Event()
        self._started = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise WorkflowError("自动关机开关必须为开启或关闭。")
        if not enabled:
            return self.controller.disable()
        self._drain_started(observe=False)
        job = self.lookup(None)
        identifier = (
            job["id"]
            if job and job.get("kind") in ASSIGNMENT_KINDS and job.get("status") in ACTIVE
            else None
        )
        self.controller.arm(identifier)
        if identifier:
            self.controller.observe(job)
        return self.controller.snapshot()

    def before_new_work(self):
        if self.controller.snapshot()["state"] in {"armed", "countdown"}:
            self.controller.disable("已开始新的操作，自动关机已取消；需要时请重新开启。")

    def on_start(self, job):
        # A follow-up fill may be started while its caller still holds the jobs
        # lock. Queueing never acquires the controller/preflight lock.
        self._started.put(job)

    def _drain_started(self, *, observe):
        while True:
            try:
                job = self._started.get_nowait()
            except queue.Empty:
                return
            if observe:
                self.controller.observe(job)

    def poll_once(self):
        state = self.controller.snapshot()
        self._drain_started(observe=state["enabled"])
        state = self.controller.snapshot()
        if state["enabled"]:
            try:
                self.controller.observe(self.lookup(state["job_id"]))
            except Exception:
                self.controller.disable("无法确认后台任务状态，已取消自动关机。")

    def _run(self):
        while not self._stop.wait(self.interval):
            self.poll_once()

    def close(self):
        self.controller.close()
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)


class WorkerSource:
    def __init__(self, source):
        port, token = source.get("port"), source.get("token")
        if type(port) is not int or not 0 < port < 65536 or not isinstance(token, str) or not token:
            raise WorkflowError("原助手的本机连接信息无效。")
        self.origin, self.token = f"http://127.0.0.1:{port}", token

    def request(self, path, *, method="GET", body=None):
        headers = {"Authorization": "Bearer " + self.token}
        if body is not None:
            headers.update(Origin=self.origin, **{"Content-Type": "application/json"})
        request = urllib.request.Request(
            self.origin + path, data=body, headers=headers, method=method
        )
        return urllib.request.urlopen(request, timeout=5)

    def json(self, path):
        with self.request(path) as response:
            return json.load(response)

    def lookup(self, identifier):
        if identifier:
            job = self.json("/api/jobs/" + identifier)
            if job.get("status") in ACTIVE:
                return job
        data = self.json("/api/bootstrap")
        active = data.get("active_job_id")
        if active:
            return next((job for job in data["jobs"] if job["id"] == active), None)
        return job if identifier else None

    def preflight(self, identifier):
        if not identifier:
            return False
        job = self.json("/api/jobs/" + identifier)
        data = self.json("/api/bootstrap")
        return bool(
            not data.get("active_job_id") and latest_completed_job(job, data.get("jobs", []))
        )


def create_companion(source, *, power=None, port=0):
    # Import at call time so the ordinary web server can reuse PowerWatch.
    from .webapp import LocalHandler

    class CompanionHandler(LocalHandler):
        def do_GET(self):
            if not urlparse(self.path).path.startswith("/api/"):
                return super().do_GET()
            self._proxy("GET")

        def do_POST(self):
            self._proxy("POST")

        def do_PUT(self):
            self._proxy("PUT")

        def _proxy(self, method):
            if not self._guard(mutation=method != "GET"):
                return
            path = urlparse(self.path).path
            try:
                payload = self._body() if method != "GET" else None
                if method == "GET" and path == "/api/shutdown":
                    return self._send(200, self.server.power.snapshot())
                if method == "POST" and path == "/api/shutdown" and payload:
                    if set(payload) != {"enabled"}:
                        raise WorkflowError("自动关机只接受开关设置。")
                    return self._send(200, self.server.power_watch.set_enabled(payload["enabled"]))
                if method == "POST" and path == "/api/shutdown/cancel":
                    if payload:
                        raise WorkflowError("取消关机不接受其他参数。")
                    return self._send(200, self.server.power.cancel())
                if method != "GET" and is_new_work(path):
                    self.server.power_watch.before_new_work()
                raw = None if payload is None else json.dumps(payload).encode("utf-8")
                try:
                    response = self.server.source.request(path, method=method, body=raw)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    status, data = response.status, response.read()
                    content_type = response.headers.get("Content-Type", "application/json")
                    content_type = content_type.split(";", 1)[0]
                    attachment = response.headers.get("Content-Disposition", "")
                if status == 200 and path in {"/api/bootstrap", "/api/health"}:
                    data = json.loads(data)
                    if path == "/api/bootstrap":
                        data["shutdown"] = self.server.power.snapshot()
                    else:
                        data.update(auto_shutdown=True, power_companion=True)
                if method == "POST" and path == "/api/shutdown" and status == 200:
                    self.server.power.close()
                    self._send(status, data, content_type)
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                filename = re.search(r'filename="([^"\r\n]+)"', attachment)
                self._send(
                    status, data, content_type, attachment=filename[1] if filename else False
                )
            except WorkflowError as error:
                self._send(400, {"error": str(error)})
            except (OSError, ValueError):
                if self.server.power.snapshot()["enabled"]:
                    self.server.power.disable("原助手连接中断，已取消自动关机。")
                self._send(502, {"error": "原作业助手暂时无法响应，请稍后重试。"})

    class CompanionServer(ThreadingHTTPServer):
        daemon_threads = True

        def server_close(self):
            self.power_watch.close()
            super().server_close()

        @property
        def browser_url(self):
            return self.origin + "/#" + self.token

    worker = WorkerSource(source)
    server = CompanionServer(("127.0.0.1", port), CompanionHandler)
    server.origin = f"http://127.0.0.1:{server.server_port}"
    server.token = secrets.token_urlsafe(32)
    server.source = worker
    server.power = power if power is not None else ShutdownController(preflight=worker.preflight)
    server.power_watch = PowerWatch(server.power, worker.lookup, interval=2)
    server.power_watch.start()
    return server


def serve_companion(settings, *, open_browser=True):
    from .ui_jobs import read_json

    root = settings.data_dir / "ui"
    source = read_json(root / "server.json", {})
    lock = FileLock(str(root / "power-companion.lock"))
    info_path = root / "power-companion.json"
    try:
        lock.acquire(timeout=0)
    except Timeout:
        info = read_json(info_path, {})
        peer = WorkerSource(info)
        if peer.json("/api/health").get("power_companion"):
            if open_browser:
                webbrowser.open(peer.origin + "/#" + peer.token)
            return
        raise WorkflowError("关机控制页面正在启动，请稍后重新打开。") from None
    server = None
    try:
        server = create_companion(source)
        server.source.json("/api/health")
        atomic_json(
            info_path,
            {
                "port": server.server_port,
                "token": server.token,
                "source_port": source["port"],
            },
        )
        if open_browser:
            webbrowser.open(server.browser_url)
        server.serve_forever(poll_interval=0.3)
    finally:
        if server:
            server.server_close()
        info_path.unlink(missing_ok=True)
        lock.release()


def main():
    parser = argparse.ArgumentParser(description="Auto-shutdown controls for an existing worker")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    serve_companion(Settings.load(), open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
