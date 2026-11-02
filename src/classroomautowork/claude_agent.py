"""One tool-free Claude Code turn per answer. Course text is data, so the model gets no tools.

Authentication is whatever the local `claude` binary uses: ANTHROPIC_API_KEY when the service
environment sets one, otherwise the machine's Claude login. Prompts go through stdin, never a shell.
"""

import base64
import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .errors import RunCancelled, WorkflowError

MODELS = (
    ("claude-opus-5-5", "Claude Opus 5.5"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
    ("claude-haiku-5-5", "Claude Haiku 5.5"),
    ("claude-fable-5-1", "Claude Fable 5.1"),
)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_EFFORT = "high"
TURN_SECONDS = 15 * 60
IDLE_SECONDS = 5 * 60
ACTIVITY = {
    "thinking": "Claude 正在分析题目与资料",
    "text": "Claude 正在整理回答",
    "tool_use": "Claude 正在输出结构化答案，完整返回后再核验",
}


def claude_command() -> list[str]:
    path = os.environ.get("CLASSROOMAUTOWORK_CLAUDE") or shutil.which("claude")
    if not path:
        raise WorkflowError("未找到本机 Claude Code，资料已保留。")
    return [path]


def model_catalog() -> dict:
    return {
        "models": [
            {
                "model": model,
                "displayName": name,
                "defaultReasoningEffort": DEFAULT_EFFORT,
                "supportedReasoningEfforts": [{"reasoningEffort": x} for x in EFFORTS],
            }
            for model, name in MODELS
        ],
        "default_model": DEFAULT_MODEL,
        "default_effort": DEFAULT_EFFORT,
        "source": "classroomautowork Claude model list",
        "checked_at": datetime.now(UTC).isoformat(),
        "note": "实际完成 AI 回合后才确认所选模型可用。",
    }


def image_block(path: Path) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        },
    }


def run_turn(
    content: list[dict],
    schema: dict,
    *,
    model: str,
    effort: str | None,
    cwd: Path,
    cancelled=lambda: False,
    on_activity=lambda _label: None,
    command=None,
    clock=time.monotonic,
) -> dict:
    """Return the validated structured output plus what the CLI reported about the turn."""
    arguments = [
        *(command or claude_command()),
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--tools",
        "",
        "--disallowedTools",
        "mcp__*",
        "--no-session-persistence",
        "--safe-mode",
        "--json-schema",
        json.dumps(schema, ensure_ascii=False),
    ]
    if effort:
        arguments += ["--effort", effort]
    message = {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }
    try:
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError:
        raise WorkflowError("无法启动本机 Claude Code，资料已保留。") from None
    events = queue.Queue()

    def read():
        try:
            for line in process.stdout:
                try:
                    events.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            events.put(None)

    threading.Thread(target=read, daemon=True).start()
    started = last = clock()
    reported = {"model": None, "session_id": None, "rate_limit": None}
    try:
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.close()
        while True:
            if cancelled():
                raise RunCancelled("已暂停 Claude，资料已保留。")
            now = clock()
            if now - started > TURN_SECONDS or now - last > IDLE_SECONDS:
                raise WorkflowError(
                    "Claude 回合超过 15 分钟或 5 分钟没有任何输出，已停止本次等待；资料已保留，请重试。"
                )
            try:
                event = events.get(timeout=0.2)
            except queue.Empty:
                continue
            if event is None:
                raise WorkflowError("Claude Code 进程已结束，没有返回完整结果。")
            last = clock()
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                reported["model"], reported["session_id"] = (
                    event.get("model"),
                    event.get("session_id"),
                )
                if reported["model"] != model:
                    raise WorkflowError("Claude 实际使用的模型与所选模型不一致，已停止生成。")
                if event.get("tools") not in ([], ["StructuredOutput"]) or event.get("mcp_servers"):
                    raise WorkflowError("Claude 会话带有额外工具，已停止生成。")
            elif kind == "rate_limit_event":
                reported["rate_limit"] = event.get("rate_limit_info")
            elif kind == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") in ACTIVITY:
                        on_activity(ACTIVITY[block["type"]])
            elif kind == "result":
                if event.get("is_error") or event.get("subtype") != "success":
                    limit = reported["rate_limit"] or {}
                    if limit.get("status") == "rejected":
                        raise WorkflowError("Claude 用量已达上限，请等额度恢复后重试；资料已保留。")
                    raise WorkflowError(
                        f"Claude 回合未成功（{event.get('subtype', 'error')}），资料已保留，请重试。"
                    )
                if not isinstance(event.get("structured_output"), dict):
                    raise WorkflowError("Claude 没有返回符合格式的结构化结果。")
                return {
                    "output": event["structured_output"],
                    "model": reported["model"],
                    "session_id": reported["session_id"] or event.get("session_id"),
                    "usage": event.get("usage"),
                    "cost_usd": event.get("total_cost_usd"),
                    "duration_ms": event.get("duration_ms"),
                }
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stdout.close()
