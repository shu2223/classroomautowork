"""Supported Codex app-server transport; no token handling or permission overrides."""

import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import UTC, datetime

from .errors import RunCancelled, WorkflowError


class CodexClient:
    def __init__(self, command, *, cancelled=lambda: False, approval=None):
        self.cancelled, self.approval = cancelled, approval
        self.events, self.pending = queue.Queue(), deque()
        self.counter = 0
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        self.active_turn = None
        self.approval_wait_seconds = 0
        self.process = subprocess.Popen(
            [*command, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            **options,
        )
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.info = self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "classroomautowork",
                        "title": "Classroom 作业助手",
                        "version": "0.2.0",
                    }
                },
            )
            self.send({"method": "initialized", "params": {}})
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                if len(line) <= 8_000_000:
                    try:
                        self.events.put(json.loads(line))
                    except ValueError:
                        pass
        finally:
            self.events.put(None)

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=True) + "\n")
        self.process.stdin.flush()

    def _next(self, deadline=None, timeout_message=None):
        while True:
            if self.cancelled():
                raise RunCancelled("已暂停 Codex，资料和会话记录已保留。")
            if deadline and time.monotonic() > deadline:
                raise WorkflowError(
                    timeout_message or "Codex 接口响应超时，请重试；已保存的资料不会丢失。"
                )
            try:
                event = self.events.get(timeout=0.2)
            except queue.Empty:
                continue
            if event is None:
                raise WorkflowError("Codex 本机进程已结束，尚未确认 AI 生成成功。")
            if event.get("id") is not None and event.get("method"):
                method, params = event["method"], event.get("params", {})
                if method in {
                    "item/commandExecution/requestApproval",
                    "item/fileChange/requestApproval",
                }:
                    before = time.monotonic()
                    decision = self.approval(method, params) if self.approval else "cancel"
                    waited = time.monotonic() - before
                    self.approval_wait_seconds += waited
                    if deadline is not None:
                        deadline += waited
                    if decision not in {"accept", "decline", "cancel"}:
                        decision = "cancel"
                    self.send({"id": event["id"], "result": {"decision": decision}})
                else:
                    # Unsupported permission, connector or question requests cannot be auto-approved.
                    self.send(
                        {
                            "id": event["id"],
                            "error": {
                                "code": -32601,
                                "message": "Use the local review questions; this client cannot grant additional permissions.",
                            },
                        }
                    )
                    raise WorkflowError(
                        "Codex 请求了此界面尚不支持的交互；资料已保存，请在 Codex 会话中继续。"
                    )
                continue
            return event

    def request(self, method, params, timeout=30):
        self.counter += 1
        identity = self.counter
        self.send({"id": identity, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            event = self._next(deadline)
            if event.get("id") == identity:
                if "error" in event:
                    raise WorkflowError(
                        f"Codex 接口 {method} 未成功（错误码 {event['error'].get('code', 'unknown')}），请检查本机登录或版本。"
                    )
                result = event.get("result", {})
                if method == "turn/start":
                    self.active_turn = (params["threadId"], result["turn"]["id"])
                return result
            self.pending.append(event)

    def next_event(self, *, timeout=None, timeout_message=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        event = (
            self.pending.popleft()
            if self.pending
            else self._next(deadline, timeout_message=timeout_message)
        )
        if event.get("method") == "turn/completed":
            self.active_turn = None
        return event

    def close(self):
        if self.process.poll() is None:
            if self.active_turn:
                try:
                    thread, turn = self.active_turn
                    self.send(
                        {
                            "id": self.counter + 1,
                            "method": "turn/interrupt",
                            "params": {"threadId": thread, "turnId": turn},
                        }
                    )
                except (OSError, ValueError):
                    pass
            if os.name == "nt":
                # Terminate only this owned process tree, never other desktop sessions.
                subprocess.run(
                    ["taskkill.exe", "/PID", str(self.process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=5,
                    check=False,
                )
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def model_catalog(command) -> dict:
    with CodexClient(command) as client:
        models, cursor = [], None
        for _ in range(10):
            params = {"limit": 100, "includeHidden": True}
            if cursor:
                params["cursor"] = cursor
            page = client.request("model/list", params)
            models.extend(page.get("data", []))
            cursor = page.get("nextCursor")
            if not cursor:
                break
        config = client.request("config/read", {"includeLayers": False}).get("config", {})
        defaults = (
            config.get("models", {}).get("new_thread", {})
            if isinstance(config.get("models"), dict)
            else {}
        )
        selected = defaults.get("model") or config.get("model")
        effort = defaults.get("model_reasoning_effort") or config.get("model_reasoning_effort")
        visible = [x for x in models if not x.get("hidden") or x.get("model") == selected]
        if selected and not any(x.get("model") == selected for x in visible):
            # The effective local configuration can name a model absent from an older CLI catalog.
            # Label its origin accurately; a completed turn, not this catalog, verifies access.
            visible.insert(
                0,
                {
                    "id": selected,
                    "model": selected,
                    "displayName": selected + "（当前 Codex 配置）",
                    "defaultReasoningEffort": effort,
                    "supportedReasoningEfforts": [{"reasoningEffort": effort}] if effort else [],
                    "origin": "local_config",
                    "inputModalities": ["text", "image"],
                },
            )
        return {
            "models": visible,
            "default_model": selected,
            "default_effort": effort,
            "source": "Codex app-server model/list + config/read",
            "checked_at": datetime.now(UTC).isoformat(),
            "note": "目录与当前配置供选择；实际完成 AI 回合后才确认所选模型可用。",
        }
