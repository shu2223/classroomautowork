"""Bounded, private answer checkpoints; partial output is never a validated draft."""

import time

from .errors import WorkflowError
from .local import utc_now

TURN_SECONDS = 15 * 60
OUTPUT_SECONDS = 5 * 60
OUTPUT_CHARACTERS = 64_000


class GenerationStream:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.output_started = None
        self.output_started_at = None
        self.item_id = None
        self.phase = None
        self.text = ""
        self.characters = 0

    def start_item(self, item):
        if item.get("type") != "agentMessage":
            return
        self.item_id, self.phase = item.get("id"), item.get("phase")
        self.text = ""
        self.output_started, self.output_started_at = None, None

    def append(self, params):
        delta = params.get("delta", "")
        if not isinstance(delta, str):
            return
        if params.get("itemId") and params["itemId"] != self.item_id:
            self.item_id, self.phase, self.text = params["itemId"], None, ""
        if self.phase != "commentary" and delta and self.output_started is None:
            self.output_started, self.output_started_at = self.clock(), utc_now()
        available = max(0, OUTPUT_CHARACTERS - len(self.text))
        self.text += delta[:available]
        self.characters += len(delta)
        if self.characters > OUTPUT_CHARACTERS:
            raise WorkflowError(
                "Codex 输出超过 64,000 字符，已停止异常长输出；资料、会话和未核验片段已保留。"
                "请用较短输出重试，片段不能当作完成的答案。"
            )

    def remaining(self, approval_wait=0):
        elapsed = self.clock() - self.started - approval_wait
        remaining = TURN_SECONDS - max(0, elapsed)
        if self.output_started is not None:
            remaining = min(remaining, OUTPUT_SECONDS - (self.clock() - self.output_started))
        if remaining <= 0:
            raise WorkflowError(self.timeout_message())
        return remaining

    @staticmethod
    def timeout_message():
        return (
            "Codex 回合超过 15 分钟，或答案连续输出超过 5 分钟，已停止本次异常等待；"
            "资料、会话和未核验输出片段已保留。请重试，不能把片段标为已完成。"
        )

    def metadata(self):
        return {
            "characters": self.characters,
            "checkpoint_characters": len(self.text),
            "item_id": self.item_id,
            "phase": self.phase,
            "output_started_at": self.output_started_at,
            "partial_file": "codex-output.partial.txt" if self.text else None,
            "validated": False,
        }
