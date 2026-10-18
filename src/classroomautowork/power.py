"""Opt-in, process-local shutdown after an observed successful assignment batch."""

import math
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime

from .completion import document_answers_incomplete
from .errors import WorkflowError

ACTIVE = {"queued", "running", "stopping"}
ASSIGNMENT_KINDS = {"review", "fill"}
READY_ITEMS = {"ready", "document_ready", "document_needs_user", "form_opened"}


def shutdown_windows():
    """Request a normal Windows shutdown; applications can still block unsaved work."""
    subprocess.run(
        ["shutdown.exe", "/s", "/t", "0"],
        check=True,
        capture_output=True,
        timeout=10,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class ShutdownController:
    """Only explicit arm + observed active-to-success transition can start a timer.

    No setting is persisted. ``snapshot`` is read-only, including when its displayed
    countdown reaches zero. The timer, not an HTTP read, performs the shutdown.
    ``observe`` must receive the bound job's final snapshot even after it leaves
    the worker's active slot. An idle arm ignores historical terminal jobs.
    """

    def __init__(
        self,
        *,
        executor=None,
        clock=time.monotonic,
        wall_clock=time.time,
        timer_factory=threading.Timer,
        platform=sys.platform,
        countdown_seconds=60,
        preflight=None,
    ):
        if not math.isfinite(countdown_seconds) or countdown_seconds < 1:
            raise ValueError("countdown_seconds must be at least one second")
        self._executor = executor or shutdown_windows
        self._clock = clock
        self._wall_clock = wall_clock
        self._timer_factory = timer_factory
        self._supported = platform == "win32"
        self._countdown_seconds = float(countdown_seconds)
        self._preflight = preflight
        self._lock = threading.RLock()
        self._timer = None
        self._generation = 0
        self._enabled = False
        self._state = "off"
        self._job_id = None
        self._saw_active = False
        self._deadline = None
        self._deadline_at = None
        self._closed = False
        self._message = "完成后自动关机已关闭。"

    def snapshot(self):
        with self._lock:
            return {
                "supported": self._supported,
                "enabled": self._enabled,
                "state": self._state,
                "job_id": self._job_id,
                "countdown_seconds": self._countdown_seconds,
                "remaining_seconds": (
                    max(0, math.ceil(self._deadline - self._clock()))
                    if self._deadline is not None
                    else None
                ),
                "deadline_at": self._deadline_at,
                "message": self._message,
            }

    def arm(self, job_id=None):
        with self._lock:
            if self._closed:
                raise WorkflowError("服务已关闭，无法启用自动关机。")
            if not self._supported:
                raise WorkflowError("完成后自动关机目前仅支持 Windows。")
            if job_id is not None and (not isinstance(job_id, str) or not job_id):
                raise WorkflowError("自动关机绑定的任务无效。")
            # Duplicate enable requests must not restart an existing countdown.
            if self._enabled:
                return self.snapshot()
            self._invalidate_timer()
            self._enabled = True
            self._job_id = job_id
            self._saw_active = False
            self._state = "armed" if job_id else "waiting"
            self._message = (
                "已开启；当前这批作业全部处理成功后，将倒计时 60 秒并关机。"
                if job_id
                else "已开启；等待下一批作业，全部处理成功后倒计时 60 秒并关机。"
            )
            return self.snapshot()

    def disable(self, reason="完成后自动关机已关闭。"):
        with self._lock:
            self._stop(reason, "off")
            return self.snapshot()

    def cancel(self):
        return self.disable("已取消自动关机。")

    def close(self):
        with self._lock:
            self._closed = True
            self._stop("服务已关闭，自动关机已取消。", "off")

    def observe(self, job):
        with self._lock:
            if not self._enabled or not isinstance(job, dict):
                return self.snapshot()
            identifier = job.get("id")
            if not isinstance(identifier, str) or not identifier:
                return self.snapshot()
            status = job.get("status")
            if self._job_id is None:
                if status in ACTIVE and job.get("kind") in ASSIGNMENT_KINDS:
                    self._job_id = identifier
                    self._state = "armed"
                else:
                    return self.snapshot()
            if identifier != self._job_id:
                if status in ACTIVE:
                    self._stop("任务批次发生变化，已取消自动关机；如有需要请重新开启。")
                return self.snapshot()
            if job.get("kind") not in ASSIGNMENT_KINDS:
                self._stop("当前任务不是作业处理批次，已取消自动关机。")
            elif status == "stopping":
                self._stop("任务正在暂停，已取消自动关机。")
            elif status in ACTIVE:
                if self._state == "countdown":
                    self._stop("任务重新开始，已取消自动关机；如有需要请重新开启。")
                else:
                    self._saw_active = True
                    self._message = "已开启；这批作业全部处理成功后，将倒计时 60 秒并关机。"
            elif status == "completed":
                if not self._saw_active:
                    self._stop("未观察到任务正在运行，已取消自动关机。")
                elif not self._all_items_complete(job):
                    self._stop("任务仍有未完成的作业或仅整理了资料，已取消自动关机。")
                elif self._state != "countdown":
                    self._start_countdown()
            elif status in {"paused", "failed", "interrupted", "completed_with_issues"}:
                self._stop("任务已暂停、失败或有未完成项，已取消自动关机。")
            else:
                self._stop("任务状态无法确认，已取消自动关机。")
            return self.snapshot()

    @staticmethod
    def _all_items_complete(job):
        items = job.get("items")
        try:
            return bool(
                isinstance(items, list)
                and items
                and all(
                    isinstance(item, dict)
                    and item.get("status") in READY_ITEMS
                    and not document_answers_incomplete(item)
                    for item in items
                )
            )
        except (AttributeError, TypeError, ValueError):
            # An invalid or partial receipt cannot establish successful completion.
            return False

    def _invalidate_timer(self):
        self._generation += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._deadline = None
        self._deadline_at = None

    def _stop(self, reason, state="cancelled"):
        self._invalidate_timer()
        self._enabled = False
        self._state = state
        self._job_id = None
        self._saw_active = False
        self._message = reason

    def _start_countdown(self):
        self._invalidate_timer()
        self._state = "countdown"
        self._deadline = self._clock() + self._countdown_seconds
        self._deadline_at = (
            datetime.fromtimestamp(self._wall_clock() + self._countdown_seconds, UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
        self._message = "这批作业已全部处理成功，即将关机；可关闭开关或点击取消。"
        self._schedule_timer(self._countdown_seconds, self._generation)

    def _schedule_timer(self, delay, generation):
        self._timer = self._timer_factory(delay, lambda: self._dispatch(generation))
        self._timer.daemon = True
        self._timer.start()

    def _dispatch(self, generation):
        with self._lock:
            if (
                generation != self._generation
                or not self._enabled
                or self._closed
                or self._state != "countdown"
            ):
                return
            remaining = self._deadline - self._clock()
            if remaining > 0:
                self._schedule_timer(remaining, generation)
                return
            # A monitor can recheck the actual worker immediately before dispatch.
            # Failure to verify is a reason to cancel, never a reason to power off.
            if self._preflight is not None:
                try:
                    allowed = self._preflight(self._job_id)
                except Exception:
                    allowed = False
                if not allowed:
                    self._stop("关机前无法确认这批作业已全部结束，已取消自动关机。")
                    return
            self._invalidate_timer()
            self._enabled = False
            self._state = "shutting_down"
            self._message = "已请求 Windows 正常关机。"
            try:
                self._executor()
            except Exception:
                self._state = "failed"
                self._message = "自动关机执行失败，请检查 Windows 权限或手动关机。"
