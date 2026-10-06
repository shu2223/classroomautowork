"""Loopback-only local frontend adapter. This is not an MCP or public web service."""

import argparse
import io
import json
import re
import secrets
import threading
import urllib.request
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from filelock import FileLock, Timeout

from .config import Settings
from .errors import WorkflowError
from .local import atomic_json, require_private_path, sha256_file
from .ui_jobs import FILES, FrontendJobs, read_json

ASSETS = Path(__file__).parent / "web"
ASSET_TYPES = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}
ITEM_ROUTE = re.compile(
    r"/api/jobs/([a-f0-9]{32})/items/([0-9]+:[0-9]+)(?:/(supplement|download|image/[a-f0-9]{24}))?"
)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, jobs: FrontendJobs, port=0, *, token=None):
        self.jobs = jobs
        self.token = token or secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), LocalHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    @property
    def browser_url(self):
        return self.origin + "/#" + self.token


class LocalHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # No request, token, account or course content in access logs.

    def _send(self, status, data, content_type="application/json", attachment=False):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header(
            "Content-Type",
            content_type
            + ("; charset=utf-8" if content_type.startswith(("text/", "application/json")) else ""),
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        if attachment:
            self.send_header("Content-Disposition", 'attachment; filename="classroom-review.zip"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _guard(self, mutation=False):
        if self.headers.get("Host") != urlparse(self.server.origin).netloc:
            self._deny(403, "只允许访问本机助手地址。")
            return False
        token = self.headers.get("Authorization", "")
        if not secrets.compare_digest(
            token.encode("utf-8"), ("Bearer " + self.server.token).encode("utf-8")
        ):
            self._deny(401, "页面没有本机访问凭证，请重新打开作业助手快捷方式。")
            return False
        origin = self.headers.get("Origin")
        if (mutation and origin != self.server.origin) or (origin and origin != self.server.origin):
            self._deny(403, "拒绝外部网页请求。")
            return False
        return True

    def _deny(self, status, message):
        # Drain a bounded body so Windows delivers the rejection instead of resetting
        # a socket with unread request bytes. Never parse rejected request content.
        if self.command in {"POST", "PUT"}:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if 0 < length <= 1_000_000:
                    self.connection.settimeout(1)
                    self.rfile.read(length)
            except (ValueError, OSError):
                pass
        self._send(status, {"error": message})

    def _body(self):
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise WorkflowError("请求必须是 JSON。")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, UnicodeError):
            raise WorkflowError("请求内容或长度无效。") from None

    def do_GET(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            if self.headers.get("Host") != urlparse(self.server.origin).netloc:
                self._send(403, {"error": "地址无效。"})
                return
            name = "index.html" if path == "/" else path.removeprefix("/")
            if name not in ASSET_TYPES:
                self._send(404, {"error": "找不到页面。"})
                return
            self._send(200, (ASSETS / name).read_bytes(), ASSET_TYPES[name])
            return
        if not self._guard():
            return
        try:
            jobs = self.server.jobs
            if path == "/api/health":
                result = {"app": "classroomautowork", "status": "running"}
            elif path == "/api/bootstrap":
                result = jobs.bootstrap()
            elif path == "/api/codex/models":
                result = jobs.models()
            elif match := re.fullmatch(r"/api/jobs/([a-f0-9]{32})", path):
                result = jobs.job(match[1])
            elif match := re.fullmatch(r"/api/policies/([0-9]+)", path):
                result = jobs.policy(match[1])
            elif match := ITEM_ROUTE.fullmatch(path):
                job_id, key, action = match.groups()
                if action == "supplement":
                    from .supplements import load_supplement

                    item = jobs.supplement_item(job_id, key)
                    result = load_supplement(
                        jobs.settings.data_dir, item["course_id"], item["assignment_id"]
                    )
                elif action == "download":
                    package = jobs.package_path(job_id, key)
                    view = jobs.package_view(job_id, key)
                    archive = io.BytesIO()
                    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
                        total = 0
                        generation = view.get("generation") or {}
                        instruction = (
                            "Classroom 本机资料与结果包\n\n"
                            + (
                                "包含实际 AI 初稿：程序自动填入个人文档或打开原生预填表单，请在原作业页面审阅，再由你手动提交。检查表和证据包仅供辅助。\n"
                                if view["text"]["draft.md"]
                                and generation.get("status") == "completed"
                                else "没有已核验的 AI 答案初稿。这份包用于查看已获取资料、题目、来源和待确认项，不能当作作业已经完成。\n"
                            )
                            + f"实际模型：{generation.get('model') or '没有已核验的模型记录'}\n会话：{generation.get('thread_id') or '没有实际会话记录'}\n"
                            + "codex-generation.json 记录真实模型、回合、输入来源和页图摘要；evidence.json 保存引用证据。\n"
                            + "sources/ 中为本次已授权下载、实际使用或当前作业必须的原文件/PDF 导出；pages/ 保留相应页图。\n"
                            + "未知教师规定保持未确认，用户要求继续生成不会被写成教师已经允许。\n"
                            + "document-fill.json / form-fill.json 记录实际填入或浏览器打开状态；程序不提交、不留言。\n"
                        )
                        out.writestr("README.txt", instruction)
                        for name in sorted(FILES):
                            if name == "README.txt":
                                continue
                            if name == "draft.md" and not view["text"]["draft.md"]:
                                continue
                            file = jobs.package_file(package, name)
                            if file.is_file():
                                total += file.stat().st_size
                                if total > 50_000_000:
                                    raise WorkflowError("审核包文字过大，请在本机目录查看。")
                                out.writestr(name, file.read_bytes())
                        used_ids = set(generation.get("used_evidence_ids", []))
                        chosen = {
                            x["source_id"]
                            for x in view["manifest"].get("source_index", [])
                            if x.get("required") or used_ids.intersection(x.get("evidence_ids", []))
                        }
                        portable = []
                        for source in view["manifest"].get("downloads", []):
                            if "file:" + source["id"] not in chosen:
                                continue
                            file = require_private_path(Path(source["path"]))
                            if (
                                not file.is_relative_to(jobs.settings.data_dir / "attachments")
                                or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", source["id"])
                                or sha256_file(file) != source["sha256"]
                            ):
                                raise WorkflowError("原文件不在本次授权资料缓存中或内容已变化。")
                            total += file.stat().st_size
                            if total > 100_000_000:
                                raise WorkflowError(
                                    "审核资料超过 100MB，请在本机目录查看，或缩小本次资料范围。"
                                )
                            name = "sources/" + source["id"] + file.suffix
                            out.writestr(name, file.read_bytes())
                            portable.append(
                                {
                                    "title": source["title"],
                                    "url": source.get("url"),
                                    "file": name,
                                    "sha256": source["sha256"],
                                }
                            )
                        for source in view["evidence"]["sources"]:
                            if source["source_id"] not in chosen or not source.get("image_path"):
                                continue
                            file = jobs.source_image(job_id, key, source["id"])
                            total += file.stat().st_size
                            if total > 100_000_000:
                                raise WorkflowError("审核页图过大，请在本机预览。")
                            out.writestr("pages/" + source["id"] + ".png", file.read_bytes())
                        out.writestr(
                            "portable-source-index.json",
                            json.dumps(portable, ensure_ascii=False, indent=2),
                        )
                    self._send(200, archive.getvalue(), "application/zip", attachment=True)
                    return
                if action and action.startswith("image/"):
                    image = jobs.source_image(job_id, key, action.split("/")[1])
                    self._send(200, image.read_bytes(), "image/png")
                    return
                result = jobs.package_view(job_id, key)
            else:
                self._send(404, {"error": "找不到本机接口。"})
                return
            self._send(200, result)
        except Exception as exc:
            self._send(
                400,
                {"error": str(exc) if isinstance(exc, WorkflowError) else "本机读取失败，请重试。"},
            )

    def do_POST(self):
        if not self._guard(mutation=True):
            return
        try:
            payload = self._body()
            path = urlparse(self.path).path
            jobs = self.server.jobs
            if path == "/api/refresh":
                if payload:
                    raise WorkflowError("刷新请求不接受额外参数。")
                result = jobs.refresh()
            elif path == "/api/review":
                if set(payload) - {"selected", "defer_media", "model", "effort", "ai_confirmed"}:
                    raise WorkflowError("生成请求包含不支持的参数。")
                result = jobs.start_selected(
                    payload.get("selected"),
                    defer_media=payload.get("defer_media", False),
                    model=payload.get("model"),
                    effort=payload.get("effort"),
                    ai_confirmed=payload.get("ai_confirmed", False),
                )
            elif path == "/api/documents/authorize":
                if payload:
                    raise WorkflowError("文档授权请求不接受额外参数。")
                result = jobs.authorize_documents()
            elif match := re.fullmatch(
                r"/api/jobs/([a-f0-9]{32})/items/([0-9]+:[0-9]+)/(supplement|continue|followup|questions|answers)",
                path,
            ):
                job_id, key, operation = match.groups()
                if operation == "followup":
                    if payload:
                        raise WorkflowError("接收补充不接受其他会话 ID 或操作指令。")
                    result = jobs.import_followup(job_id, key)
                elif operation == "answers":
                    result = jobs.save_form_inputs(job_id, key, payload)
                elif operation == "questions":
                    result = jobs.import_questions(job_id, key, payload)
                else:
                    if set(payload) - {"text", "personal_facts", "model", "effort", "ai_confirmed"}:
                        raise WorkflowError("补充请求包含不支持的参数。")
                    result = jobs.save_item_supplement(
                        job_id,
                        key,
                        {k: payload[k] for k in ("text", "personal_facts") if k in payload},
                        continue_run=operation == "continue",
                        model=payload.get("model"),
                        effort=payload.get("effort"),
                        ai_confirmed=payload.get("ai_confirmed", False),
                    )
            elif match := re.fullmatch(
                r"/api/jobs/([a-f0-9]{32})/items/([0-9]+:[0-9]+)/fill", path
            ):
                if payload:
                    raise WorkflowError("填入请求不接受远端文档 ID 或额外写入指令。")
                result = jobs.fill_existing(match[1], match[2])
            elif match := re.fullmatch(r"/api/jobs/([a-f0-9]{32})/approval", path):
                if set(payload) != {"id", "decision"}:
                    raise WorkflowError("审批只接受当前请求 ID 与本次决定。")
                result = jobs.approve(match[1], payload["id"], payload["decision"])
            elif match := re.fullmatch(r"/api/jobs/([a-f0-9]{32})/(pause|retry)", path):
                if payload:
                    raise WorkflowError("任务操作不接受额外参数。")
                result = jobs.pause(match[1]) if match[2] == "pause" else jobs.retry(match[1])
            elif path == "/api/shutdown":
                if payload:
                    raise WorkflowError("退出请求不接受额外参数。")
                active = jobs.bootstrap()["active_job_id"]
                if active:
                    raise WorkflowError("请先暂停当前任务并等它停止，再退出助手。")
                self._send(200, {"status": "stopping"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            else:
                self._send(404, {"error": "找不到本机接口。"})
                return
            self._send(200, result)
        except Exception as exc:
            self._send(
                400,
                {"error": str(exc) if isinstance(exc, WorkflowError) else "本机操作失败，请重试。"},
            )

    def do_PUT(self):
        if not self._guard(mutation=True):
            return
        try:
            path = urlparse(self.path).path
            match = re.fullmatch(r"/api/policies/([0-9]+)", path)
            if not match:
                self._send(404, {"error": "找不到课程规则。"})
                return
            result = self.server.jobs.save_policy(match[1], self._body())
            self._send(200, result)
        except Exception as exc:
            self._send(
                400, {"error": str(exc) if isinstance(exc, WorkflowError) else "规则保存失败。"}
            )


def existing_server(info_path: Path):
    info = read_json(info_path)
    if (
        not isinstance(info, dict)
        or not isinstance(info.get("port"), int)
        or not 0 < info["port"] < 65536
        or not isinstance(info.get("token"), str)
    ):
        return None
    origin = f"http://127.0.0.1:{info['port']}"
    try:
        request = urllib.request.Request(
            origin + "/api/health", headers={"Authorization": "Bearer " + info["token"]}
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            if json.load(response).get("app") == "classroomautowork":
                return origin + "/#" + info["token"]
    except (OSError, ValueError):
        pass
    return None


def serve(settings: Settings | None = None, *, open_browser=True, port=0):
    settings = (settings or Settings.load()).validated()
    ui_root = settings.data_dir / "ui"
    ui_root.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(ui_root / "server.lock"))
    info_path = ui_root / "server.json"
    try:
        lock.acquire(timeout=0)
    except Timeout:
        url = existing_server(info_path)
        if url:
            if open_browser:
                webbrowser.open(url)
            return
        raise WorkflowError("本机助手正在启动或运行，请稍后重新打开。") from None
    server = None
    try:
        server = LocalServer(FrontendJobs(settings), port)
        atomic_json(info_path, {"port": server.server_port, "token": server.token})
        if open_browser:
            webbrowser.open(server.browser_url)
        server.serve_forever(poll_interval=0.3)
    finally:
        if server:
            server.jobs.wait_idle(timeout=30)
            server.server_close()
        info_path.unlink(missing_ok=True)
        lock.release()


def main():
    parser = argparse.ArgumentParser(description="Local Classroom frontend")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    try:
        serve(Settings.load(args.config), open_browser=not args.no_browser, port=args.port)
    except WorkflowError as exc:
        # pythonw has no console; report a concrete startup failure in a native dialog.
        if __import__("os").name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, str(exc), "Classroom 作业助手", 0x10)
        else:
            raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
