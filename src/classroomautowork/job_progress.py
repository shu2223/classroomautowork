"""Join measured worker events to a conservative, whole-job work inventory."""

import time
from pathlib import Path

from .eta import ProgressEstimator

MEDIA_SUFFIXES = {
    ".mp4",
    ".m4v",
    ".mov",
    ".mkv",
    ".webm",
    ".mp3",
    ".m4a",
    ".wav",
    ".ogg",
    ".avi",
    ".flac",
}
DONE = {
    "ready",
    "document_ready",
    "document_needs_user",
    "form_opened",
    "needs_form",
    "needs_document",
    "needs_user",
    "materials_ready",
    "failed",
    "paused",
    "interrupted",
}


class JobProgress:
    def __init__(self, job, history_path, *, clock=time.monotonic):
        self.job = job
        self.clock = clock
        self._paused_at = None
        self._paused_total = 0.0
        self.estimator = ProgressEstimator(history_path, clock=clock)
        self.context = {"model": job.get("model"), "effort": job.get("effort")}
        initial_stage = {"fill": "fill", "document_auth": "authorization"}.get(
            job["kind"], "course_sync"
        )
        self.current = {"stage": initial_stage, **self.context}
        self.inventories = {}
        self.prepared_courses = set()
        self.finished_files = set()
        self.file_started = {}
        self.file_worked = set()
        self.file_cached = set()
        self.active_file = None
        self.active_item = None
        self.item_started = {}
        self.generated_items = set()
        self.finished_items = set()
        self.routes = {}
        self.estimator.update(self.current)
        if job["kind"] == "document_auth":
            self.pause(True)

    def active_time(self):
        raw = self.clock() if self._paused_at is None else self._paused_at
        return raw - self._paused_total

    def pause(self, paused):
        if paused and self._paused_at is None:
            self._paused_at = self.clock()
        elif not paused and self._paused_at is not None:
            self._paused_total += max(0, self.clock() - self._paused_at)
            self._paused_at = None
        self.estimator.pause(paused)

    @staticmethod
    def key(item):
        return item["course_id"] + ":" + item["assignment_id"]

    @staticmethod
    def file_stage(filename):
        return (
            "attachment_total_media"
            if Path(filename).suffix.lower() in MEDIA_SUFFIXES
            else "attachment_total_document"
        )

    def update(self, details):
        data = {**self.context, **details}
        if data.get("stage") == "ai" and self.active_item:
            # The worker also reports Codex thread IDs; the timed operation is
            # this assignment, including connection/model startup.
            data["item_id"] = self.active_item
        course = data.get("course_id")
        if "workload_files" in data:
            self.inventories[course] = list(data.pop("workload_files"))
        if data.get("stage") == "course_prepared":
            self.prepared_courses.add(course)
            self.active_file = None
        identity = (course, data.get("file_id")) if data.get("file_id") else None
        if identity:
            self.active_file = identity
            self.file_started.setdefault(identity, self.active_time())
            if data.get("cached"):
                self.file_cached.add(identity)
            if data["stage"] in {"extract", "transcribe"} and not data.get("cached"):
                # Only a genuinely executed processor can train full-attachment cost.
                value = (
                    data.get("phase_current_seconds", 0)
                    if data["stage"] == "transcribe"
                    else data.get("current", 0)
                )
                if value > 0:
                    self.file_worked.add(identity)
            if data["stage"] in {"attachment_done", "attachment_failed"}:
                self.finished_files.add(identity)
                if (
                    data["stage"] == "attachment_done"
                    and identity in self.file_worked
                    and identity not in self.file_cached
                ):
                    self.estimator.observe_duration(
                        self.file_stage(data.get("filename", "")),
                        self.active_time() - self.file_started[identity],
                    )
                self.active_file = None
        self.current = data
        self.estimator.update(data)

    def item(self, item, values):
        key = self.key(item)
        status = values.get("status")
        if status == "drafting":
            self.active_item = key
            self.active_file = None
            self.item_started.setdefault(key, self.active_time())
            self.update({"stage": "document_inspect", "item_id": key})
        elif status in {"filling_document", "filling_form"}:
            self.active_item = key
            self.update(
                {
                    "stage": "fill" if status == "filling_document" else "form_fill",
                    "item_id": key,
                }
            )
        elif status in {"failed", "needs_form", "needs_document"}:
            self.estimator.update({**self.current, "failed": True})
        generation = values.get("generation", {})
        if generation.get("status") == "starting":
            self.generated_items.add(key)
        elif generation.get("status") == "completed" and key not in self.generated_items:
            self.update({**self.current, "cached": True})

    def route(self, item, stages):
        self.routes[self.key(item)] = stages
        if self.job.get("ai_confirmed"):
            self.update({"stage": "ai", "item_id": self.key(item)})

    def item_done(self, item):
        key = self.key(item)
        entry = next(x for x in self.job["items"] if self.key(x) == key)
        self.finished_items.add(key)
        if entry["status"] in {"ready", "document_ready", "form_opened"}:
            if key in self.generated_items and key in self.item_started:
                self.estimator.observe_duration(
                    "answer_total", self.active_time() - self.item_started[key], self.context
                )
            self.estimator.update({**self.current, "completed": True})
        else:
            self.estimator.update({**self.current, "failed": True})
        self.active_item = None

    def snapshot(self):
        """Estimate the inventory without writing status, activity or history."""
        tasks = [self.current]
        courses = {x["course_id"] for x in self.job.get("items", [])}
        scope_complete = True
        if self.job["kind"] == "review":
            for course in sorted(courses - self.prepared_courses):
                inventory = self.inventories.get(course)
                if inventory is None:
                    tasks.append({"stage": "__unknown__"})
                    scope_complete = False
                    continue
                for file in inventory:
                    identity = (course, file["file_id"])
                    if identity in self.finished_files:
                        continue
                    if identity == self.active_file:
                        if self.current["stage"] not in {"transcribe", "extract"}:
                            # Download speed alone cannot predict subsequent inference.
                            tasks.append({"stage": "__unknown__"})
                        continue
                    tasks.append({"stage": self.file_stage(file.get("filename", ""))})
            for item in self.job.get("items", []):
                key = self.key(item)
                if key in self.finished_items or (
                    item["status"] in DONE and key != self.active_item
                ):
                    continue
                if key == self.active_item:
                    route = self.routes.get(key)
                    if route is None:
                        tasks.append({"stage": "__unknown__"})
                    else:
                        stage = self.current["stage"]
                        start = route.index(stage) + 1 if stage in route else 0
                        tasks.extend({"stage": x, **self.context} for x in route[start:])
                else:
                    tasks.append({"stage": "answer_total", **self.context})
        elif self.job["kind"] == "fill":
            for item in self.job["items"]:
                if item["status"] not in DONE and self.key(item) != self.active_item:
                    tasks.append({"stage": "fill", **self.context})
        self.estimator.set_pending(tasks, scope_complete=scope_complete)
        return self.estimator.snapshot()

    def finish(self, success):
        self.estimator.finish(success)
