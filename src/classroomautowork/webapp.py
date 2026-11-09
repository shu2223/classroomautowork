"""Loopback-only local frontend adapter. This is not an MCP or public web service."""

import argparse
import io
import json
import re
import secrets
import select
import socket
import urllib.request
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from filelock import FileLock, Timeout

from . import connection_test, school_browser
from .config import Settings
from .errors import Busy, RunCancelled, WorkflowError
from .local import atomic_json, prefer_ipv4, require_private_path, sha256_file
from .school_browser import CDPError
from .ui_jobs import FILES, FrontendJobs, read_json

ASSETS = Path(__file__).parent / "web"
ASSET_TYPES = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}
# yishuapp.com reaches this server through Cloudflare Tunnel, behind Cloudflare Access.
PUBLIC_HOST = "yishuapp.com"
PUBLIC_ORIGIN = "https://" + PUBLIC_HOST
PUBLIC_PREFIX = "/classroom"
ITEM_ROUTE = re.compile(
    r"/api/jobs/([a-f0-9]{32})/items/([0-9]+:[0-9]+)(?:/(supplement|download|answer-docx|image/[a-f0-9]{24}))?"
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
            filename = attachment if isinstance(attachment, str) else "classroom-review.zip"
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _public(self):
        return self.headers.get("Host") == PUBLIC_HOST

    def _path(self):
        """Request path without the public prefix, or None when this host may not ask for it."""
        path = urlparse(self.path).path
        if self._public():
            # Same rule as the mini PC status page: Cloudflare Access adds this header.
            if not self.headers.get("Cf-Access-Jwt-Assertion"):
                return None
            if not path.startswith(PUBLIC_PREFIX + "/"):
                return None
            return path.removeprefix(PUBLIC_PREFIX)
        if self.headers.get("Host") != urlparse(self.server.origin).netloc:
            return None
        return path

    def _guard(self, mutation=False):
        if self._path() is None:
            self._deny(403, "只允许访问本机助手地址。")
            return False
        expected = PUBLIC_ORIGIN if self._public() else self.server.origin
        if not self._public():
            token = self.headers.get("Authorization", "")
            if not secrets.compare_digest(
                token.encode("utf-8"), ("Bearer " + self.server.token).encode("utf-8")
            ):
                self._deny(401, "页面没有本机访问凭证，请重新打开作业助手。")
                return False
        origin = self.headers.get("Origin")
        if (mutation and origin != expected) or (origin and origin != expected):
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

    def _client_gone(self) -> bool:
        """Whether the page closed the connection (it gave up waiting, reloaded or was closed): a
        long request such as the connection test then stops instead of running on for nobody."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

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
        if self._public() and urlparse(self.path).path == PUBLIC_PREFIX:
            self.send_response(301)
            self.send_header("Location", PUBLIC_PREFIX + "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = self._path()
        if path is None:
            self._send(403, {"error": "地址无效。"})
            return
        if not path.startswith("/api/"):
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
            elif path == "/api/models":
                result = jobs.models()
            elif path in ("/api/settings/openai", "/api/settings/anthropic-compatible"):
                # Either path gives every provider's status and the endpoints' model suggestions.
                result = jobs.openai_settings()
            elif path == "/api/google-docs/status":
                result = jobs.google_doc_status()
            elif path == "/api/school-login/status":
                result = jobs.school_login_status()
            elif path == "/api/school-login/frame":
                # Long-poll for the next view of the sign-in page (works through the tunnel).
                after, wait = self._frame_query()
                result = jobs.school_login_frame(after, wait)
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
                            + "generation.json 记录真实模型、会话、输入来源和页图摘要；evidence.json 保存引用证据。\n"
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
                elif action == "answer-docx":
                    view = jobs.package_view(job_id, key)
                    if not view.get("answer_document"):
                        raise WorkflowError(
                            "答案 Word 文件尚未生成或已变化，请点击下载 Word 重新导出。"
                        )
                    package = jobs.package_path(job_id, key)
                    document = jobs.package_file(package, "answer.docx")
                    self._send(
                        200,
                        document.read_bytes(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        attachment="classroom-answer.docx",
                    )
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

    def _frame_query(self):
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        if set(query) - {"after", "wait"} or any(len(values) != 1 for values in query.values()):
            raise WorkflowError("画面请求的参数无效。")
        after, wait = query.get("after", ["0"])[0], query.get("wait", ["20"])[0]
        if not re.fullmatch(r"\d{1,9}", after) or not re.fullmatch(r"\d{1,2}", wait):
            raise WorkflowError("画面请求的参数无效。")
        return int(after), min(int(wait), 25)

    def do_POST(self):
        if not self._guard(mutation=True):
            return
        path = self._path()
        if path == "/api/school-login/input":
            self._school_input()
            return
        try:
            payload = self._body()
            jobs = self.server.jobs
            if path == "/api/refresh":
                if payload:
                    raise WorkflowError("刷新请求不接受额外参数。")
                result = jobs.refresh()
            elif path == "/api/review":
                if set(payload) - {"selected", "model", "effort", "ai_confirmed", "provider"}:
                    raise WorkflowError("生成请求包含不支持的参数。")
                # provider: whose model list the toolbar showed; a job never starts on another.
                result = jobs.start_selected(
                    payload.get("selected"),
                    model=payload.get("model"),
                    effort=payload.get("effort"),
                    ai_confirmed=payload.get("ai_confirmed", False),
                    shown_provider=payload.get("provider"),
                )
            elif path in ("/api/google-docs/authorize", "/api/google-docs/forget"):
                if payload:
                    raise WorkflowError("这个操作不接受参数。")
                result = (
                    jobs.google_doc_start()
                    if path.endswith("authorize")
                    else jobs.google_doc_forget()
                )
            elif path == "/api/google-docs/finish":
                # The pasted address holds a one-time code: no answer or error repeats it.
                result = jobs.google_doc_finish(payload)
            elif match := re.fullmatch(
                r"/api/jobs/([a-f0-9]{32})/items/([0-9]+:[0-9]+)/google-doc", path
            ):
                if payload:
                    raise WorkflowError("新建文档不接受远端文档 ID 或额外参数。")
                result = jobs.create_google_doc(match[1], match[2])
            elif path == "/api/documents/authorize":
                if payload:
                    raise WorkflowError("文档授权请求不接受额外参数。")
                result = jobs.authorize_documents()
            elif path == "/api/settings/test":
                # One tiny real request; a second one while it runs is a 409 (Busy). It ends when
                # the page stops waiting (RunCancelled): nobody is left to answer then.
                try:
                    result = jobs.test_connection(payload, cancelled=self._client_gone)
                except RunCancelled:
                    self.close_connection = True
                    return
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
                    if set(payload) - {
                        "text",
                        "personal_facts",
                        "model",
                        "effort",
                        "ai_confirmed",
                        "provider",
                    }:
                        raise WorkflowError("补充请求包含不支持的参数。")
                    result = jobs.save_item_supplement(
                        job_id,
                        key,
                        {k: payload[k] for k in ("text", "personal_facts") if k in payload},
                        continue_run=operation == "continue",
                        model=payload.get("model"),
                        effort=payload.get("effort"),
                        ai_confirmed=payload.get("ai_confirmed", False),
                        shown_provider=payload.get("provider"),
                    )
            elif match := re.fullmatch(
                r"/api/jobs/([a-f0-9]{32})/items/([0-9]+:[0-9]+)/export-answer", path
            ):
                if payload:
                    raise WorkflowError("导出请求不接受额外路径或远端写入指令。")
                result = jobs.export_answer(match[1], match[2])
            elif match := re.fullmatch(
                r"/api/jobs/([a-f0-9]{32})/items/([0-9]+:[0-9]+)/fill", path
            ):
                if payload:
                    raise WorkflowError("填入请求不接受远端文档 ID 或额外写入指令。")
                result = jobs.fill_existing(match[1], match[2])
            elif path == "/api/school-login/start":
                result = jobs.school_login_start(payload)
            elif match := re.fullmatch(
                r"/api/school-login/(back|finish|cancel|logout)", path or ""
            ):
                if payload:
                    raise WorkflowError("这个操作不接受参数。")
                result = jobs.school_login_action(match[1])
            elif match := re.fullmatch(r"/api/jobs/([a-f0-9]{32})/(pause|retry)", path):
                if payload:
                    raise WorkflowError("任务操作不接受额外参数。")
                result = jobs.pause(match[1]) if match[2] == "pause" else jobs.retry(match[1])
            else:
                self._send(404, {"error": "找不到本机接口。"})
                return
            self._send(200, result)
        except Exception as exc:
            self._send(
                409 if isinstance(exc, Busy) else 400,
                {"error": str(exc) if isinstance(exc, WorkflowError) else "本机操作失败，请重试。"},
            )

    def _school_input(self):
        """A tap, a key or text for the sign-in page. What the user typed is in this request only:
        it goes to Chromium and nowhere else, and no answer or error repeats any of it."""
        try:
            result = self.server.jobs.school_login_input(self._body())
        except CDPError:
            result, status = {"error": "登录页没有响应，请稍等后再试。"}, 400
        except Busy as exc:
            result, status = {"error": str(exc)}, 409
        except WorkflowError as exc:
            result, status = {"error": str(exc)}, 400
        except Exception:  # noqa: BLE001 - never echo anything of the request
            result, status = {"error": "本机操作失败，请重试。"}, 400
        else:
            status = 200
        self._send(status, result)

    def do_PUT(self):
        if not self._guard(mutation=True):
            return
        try:
            # Settings take the same guard as every write; none of them answers with a key.
            settings = {
                "/api/settings/api-key": self.server.jobs.set_api_key,
                "/api/settings/provider": self.server.jobs.set_provider,
                "/api/settings/openai": self.server.jobs.set_openai,
                "/api/settings/anthropic-compatible": self.server.jobs.set_compat,
            }.get(self._path())
            if settings:
                self._send(200, settings(self._body()))
                return
            match = re.fullmatch(r"/api/policies/([0-9]+)", self._path())
            if not match:
                self._send(404, {"error": "找不到课程规则。"})
                return
            result = self.server.jobs.save_policy(match[1], self._body())
            self._send(200, result)
        except Exception as exc:
            self._send(
                400, {"error": str(exc) if isinstance(exc, WorkflowError) else "设置保存失败。"}
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


def serve(settings: Settings | None = None, *, open_browser=True, port=0, token=None):
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
        server = LocalServer(FrontendJobs(settings), port, token=token)
        atomic_json(info_path, {"port": server.server_port, "token": server.token})
        if open_browser:
            webbrowser.open(server.browser_url)
        server.serve_forever(poll_interval=0.3)
    finally:
        if server:
            # A connection test that runs ends now, with its Claude Code process; the
            # school-login browser (and any sign-in session) stops too.
            connection_test.stop()
            school_browser.stop_all()
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
    prefer_ipv4()
    try:
        serve(Settings.load(args.config), open_browser=not args.no_browser, port=args.port)
    except WorkflowError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
