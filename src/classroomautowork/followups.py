"""Read completed follow-ups from the recorded Codex chat, without sending messages."""

import json
import re
from pathlib import Path

from .codex_rpc import CodexClient
from .drafting import codex_command
from .errors import WorkflowError
from .local import atomic_json, require_private_path, utc_now


def read_followup(package: Path, command=None) -> dict:
    package = require_private_path(package)
    generation = json.loads((package / "codex-generation.json").read_text(encoding="utf-8"))
    if not generation.get("thread_id") or not generation.get("turn_id"):
        raise WorkflowError("这项作业还没有可接收补充的 Codex 会话。")
    with CodexClient(command or codex_command()) as client:
        thread = client.request(
            "thread/read", {"threadId": generation["thread_id"], "includeTurns": True}
        )["thread"]
    if (
        thread.get("id") != generation["thread_id"]
        or Path(thread.get("cwd", "")).resolve() != package
    ):
        raise WorkflowError("Codex 会话与这项作业的资料包不匹配。")
    turns = thread.get("turns", [])
    original = next(
        (index for index, turn in enumerate(turns) if turn.get("id") == generation["turn_id"]), None
    )
    if original is None:
        raise WorkflowError("无法确认原始生成回合，未导入其他会话内容。")
    completed = [turn for turn in turns[original + 1 :] if turn.get("status") == "completed"]
    if not completed:
        raise WorkflowError("尚无已完成的补充回合。请先在这项作业的 Codex 会话中补充并等它完成。")
    turn = completed[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", turn.get("id", "")):
        raise WorkflowError("补充回合 ID 无效，未写入任何其他路径。")
    records = []
    for item in turn.get("items", []):
        if item.get("type") == "userMessage":
            for block in item.get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    records.append({"role": "user", "text": block["text"]})
        elif item.get("type") == "agentMessage" and item.get("text"):
            records.append({"role": "assistant", "text": item["text"]})
    if not records or not any(x["role"] == "assistant" for x in records):
        raise WorkflowError("补充回合没有可读取的完整回答。")
    if sum(len(x["text"]) for x in records) > 55000:
        raise WorkflowError("补充回合过长，请在补充入口填写需要保留的内容。")
    result = {
        "thread_id": thread["id"],
        "turn_id": turn["id"],
        "model": thread.get("model"),
        "reasoning_effort": thread.get("reasoningEffort"),
        "records": records,
        "imported_at": utc_now(),
    }
    path = require_private_path(package / "followups" / (turn["id"] + ".json"))
    if not path.exists():
        atomic_json(path, result)
    else:
        result = json.loads(path.read_text(encoding="utf-8"))
    return result
