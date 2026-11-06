"""Persistent local frontend jobs, independent of HTTP and independently callable."""

import json
import re
import threading
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from . import connection_test, school_browser
from .answer_document import create_answer_document, valid_answer_document
from .auth import documents_authorized
from .batch import course_batches
from .claude_agent import model_catalog
from .completion import (
    answers_incomplete,
    document_answers_incomplete,
    document_fill_outcome,
    form_answers_incomplete,
    form_content_gaps,
    form_fill_outcome,
    form_generation_incomplete,
    review_cautions,
)
from .config import Settings
from .document_fill import fill_review
from .drafting import generate_answer, recover_completed_result, unread_own_forms
from .errors import ConfigurationError, FormsUnread, MissingSources, RunCancelled, WorkflowError
from .form_capture import import_form_capture
from .form_fill import fill_response_forms, form_gap_reason, prepare_response_forms
from .form_read import form_references, form_url
from .job_progress import JobProgress
from .kb_client import KnowledgeClient
from .local import atomic_json, require_private_path, sha256_file, utc_now
from .pending import discover_pending
from .policy import CoursePolicy, confirmed_gate
from .progress import ProgressUpdate
from .providers import (
    KINDS,
    active_provider,
    claude_status,
    compat_catalog,
    openai_catalog,
    save_claude,
    save_compat,
    save_openai,
    save_provider,
    settings_view,
)
from .providers import status as provider_status
from .school_forms import school_account
from .student import StudentProfile
from .supplements import load_supplement, normalized, save_supplement
from .user_answers import load_user_answers, save_user_answers
from .workflow import prepare

ACTIVE = {"queued", "running", "stopping"}
FILES = {
    "manifest.json",
    "requirements.json",
    "evidence.json",
    "review.json",
    "review-receipt.json",
    "draft.md",
    "checklist.md",
    "questions.md",
    "summary.md",
    "generation.json",
    "model-result.json",
    "README.txt",
    "document-fill.json",
    "document-answers.json",
    "form-answers.json",
    "form-fill.json",
    "answer.docx",
    "answer.html",
    "answer-document.json",
}
POLICY_FIELDS = {
    "ai_use",
    "policy_evidence",
    "limitations",
    "disclosure",
    "personal_facts",
    "max_attachment_bytes",
    "max_pdf_pages",
    "unconfirmed_drafting",
}


def item_key(item: dict) -> str:
    return item["course_id"] + ":" + item["assignment_id"]


PROVIDER_LABELS = {
    "anthropic": "Claude API",
    "anthropic_compatible": "Anthropic 兼容接口",
    "openai": "OpenAI 兼容接口",
}


def job_provider(job: dict):
    """The provider a job answered with; AI jobs from before providers existed used Claude.

    A fill job answers nothing itself (it applies an answer to the form or document), so it has
    none: when its retry has to answer again, the new job takes the active provider, as the main
    button would."""
    if job.get("kind") == "fill":
        return job.get("provider")
    return job.get("provider") or ("anthropic" if job.get("ai_confirmed") else None)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def normalize_form_fill(receipt):
    """Migrate presentation coverage, keeping original answers and links intact."""
    changed = False
    targets = list(receipt.get("forms", []))
    if "user_input_fields" in receipt or "remaining_answer_fields" in receipt:
        targets.append(receipt)
    for target in targets:
        if "user_input_fields" not in target:
            continue
        previous = target["user_input_fields"]
        personal = [
            {**field, "gap_reason": "personal_fact"}
            for field in previous
            if form_gap_reason(field.get("title", ""), field.get("review_note", ""))
            == "personal_fact"
        ]
        missing = form_content_gaps(target)
        optional = [
            field
            for field in previous
            if not field.get("required")
            and form_gap_reason(field.get("title", ""), field.get("review_note", ""))
            != "personal_fact"
        ]
        updates = {"user_input_fields": personal, "remaining_answer_fields": missing}
        if optional:
            updates["optional_empty_fields"] = target.get("optional_empty_fields", []) + optional
        if any(target.get(key) != value for key, value in updates.items()):
            target.update(updates)
            changed = True
    if form_content_gaps(receipt) and receipt.get("form_mapping_complete") is not False:
        receipt["form_mapping_complete"] = False
        changed = True
    return changed


class FrontendJobs:
    def __init__(self, settings: Settings):
        self.settings = settings.validated()
        self.root = require_private_path(self.settings.data_dir / "ui")
        (self.root / "jobs").mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._active = None
        self._cancel = threading.Event()
        self._thread = None
        self._models_lock = threading.Lock()
        self._jobs = {}
        self._progress = {}
        self._on_start = None
        for path in sorted((self.root / "jobs").glob("*.json")):
            job = read_json(path)
            if not job or not re.fullmatch(r"[a-f0-9]{32}", job.get("id", "")):
                continue
            if job["status"] in ACTIVE:
                job["status"] = "interrupted"
                job["message"] = "上次运行已中断。点击重试，会复用已完成的资料和审核包。"
                for item in job.get("items", []):
                    if item.get("status") in {
                        "waiting",
                        "preparing",
                        "prepared",
                        "drafting",
                        "filling_document",
                        "filling_form",
                    }:
                        item["status"] = "interrupted"
                atomic_json(path, job)
            incomplete = False
            migrated = False
            for item in job.get("items", []):
                if item.get("form_fill"):
                    migrated = normalize_form_fill(item["form_fill"]) or migrated
                if item.get("status") in {"ready", "document_ready", "document_needs_user"} and (
                    document_answers_incomplete(item)
                ):
                    receipt = item.get("document_fill") or {}
                    if item.get("requires_regeneration"):
                        receipt = {**receipt, "status": "partial"}
                    item.update(document_fill_outcome(receipt))
                    incomplete = True
                if item.get("status") in {"ready", "form_opened", "needs_form"} and (
                    form_answers_incomplete(item)
                ):
                    generation = item.get("generation") or {}
                    if item.get("form_requires_regeneration"):
                        generation = {**generation, "form_mapping_complete": False}
                    outcome = form_fill_outcome(item.get("form_fill") or {}, generation)
                    item.update(outcome)
                    incomplete = True
            if incomplete:
                if job["status"] == "completed":
                    job["status"] = "completed_with_issues"
                job["message"] = (
                    "原文档或表单还有未完成的必填栏，请查看原因并继续处理；已有答案会保留。"
                )
                atomic_json(path, job)
            elif migrated:
                atomic_json(path, job)
            self._jobs[job["id"]] = job
        queued = read_json(self.root / "answer-fill-requests.json", {})
        self._answer_requests = (
            {
                key: job_id
                for key, job_id in queued.items()
                if isinstance(key, str)
                and re.fullmatch(r"[0-9]+:[0-9]+", key)
                and isinstance(job_id, str)
                and job_id in self._jobs
            }
            if isinstance(queued, dict)
            else {}
        )
        self._next_answer_fill()

    def _save(self, job):
        if job["id"] in self._progress:
            job["estimate"] = self._progress[job["id"]].snapshot()
        job["updated_at"] = utc_now()
        atomic_json(self.root / "jobs" / (job["id"] + ".json"), job)

    def bootstrap(self):
        with self._lock:
            pending = read_json(self.root / "pending.json")
            policies = {}
            if pending:
                for cid in {x["course_id"] for x in pending["assignments"]}:
                    policies[cid] = CoursePolicy.load(self.settings.data_dir, cid).draft_gate()
            jobs = sorted(self._jobs.values(), key=lambda x: x["created_at"], reverse=True)[:30]
            # Refresh jobs must not evict an assignment's result from the one-click
            # skip list. Keep one current record per assignment, outside log paging.
            result_jobs, seen = [], set()
            for job in sorted(self._jobs.values(), key=lambda x: x["created_at"], reverse=True):
                if job["kind"] not in {"review", "fill"}:
                    continue
                items = []
                for item in job["items"]:
                    if item_key(item) not in seen:
                        items.append(item)
                        seen.add(item_key(item))
                if items:
                    result_jobs.append(
                        {
                            **{k: job[k] for k in ("id", "kind", "created_at", "status")},
                            "items": items,
                        }
                    )
            return json.loads(
                json.dumps(
                    {
                        "documents_authorized": documents_authorized(self.settings),
                        "account": self.settings.school_email,
                        "timezone": self.settings.timezone,
                        "pending": pending,
                        "policies": policies,
                        "jobs": [self._snapshot(x) for x in jobs],
                        "result_jobs": result_jobs,
                        "active_job_id": self._active,
                        # Kept for pages that predate the provider block, which carries it too.
                        "api_key": claude_status(self.settings.data_dir),
                        "provider": provider_status(self.settings.data_dir),
                        # 设置 → 学校账号: who is signed in (the email only; no session data).
                        "school_login": school_browser.login_state(
                            self.settings.data_dir, self.settings.school_email
                        ),
                        "submission": "manual_only",
                    }
                )
            )

    def job(self, job_id: str):
        with self._lock:
            if job_id not in self._jobs:
                raise WorkflowError("找不到该本机任务记录。")
            return self._snapshot(self._jobs[job_id])

    def _snapshot(self, job):
        value = json.loads(json.dumps(job))
        if job["id"] in self._progress:
            value["estimate"] = self._progress[job["id"]].snapshot()
        value["runtime"] = {
            "observed_at": utc_now(),
            "worker_alive": bool(
                self._active == job["id"] and self._thread and self._thread.is_alive()
            ),
        }
        return value

    def _log(self, job_id, message):
        if self._cancel.is_set():
            raise RunCancelled("已暂停，完成的资料与审核包会保留。")
        with self._lock:
            job = self._jobs[job_id]
            job["message"] = str(message)[:500]
            if isinstance(message, ProgressUpdate):
                previous = job.get("progress", {})
                details = dict(message.details)
                if job_id in self._progress:
                    self._progress[job_id].update(details)
                details.pop("workload_files", None)
                same_step = all(
                    previous.get(key) == details.get(key)
                    for key in ("stage", "file_id", "item_id", "phase")
                )
                job["progress"] = {
                    **details,
                    "started_at": previous["started_at"]
                    if same_step and previous.get("started_at")
                    else utc_now(),
                    "activity_at": utc_now(),
                }
            else:
                job.pop("progress", None)
            if not job["events"] or job["events"][-1]["message"] != job["message"]:
                job["events"].append({"at": utc_now(), "message": job["message"]})
                job["events"] = job["events"][-150:]
            self._save(job)

    def _item(self, job_id, target, **values):
        with self._lock:
            job = self._jobs[job_id]
            entry = next(x for x in job["items"] if item_key(x) == item_key(target))
            if values.get("status") and values["status"] != entry.get("status"):
                entry["stage_started_at"] = utc_now()
            entry.update(values)
            if job_id in self._progress:
                self._progress[job_id].item(entry, values)
            self._save(job)

    def set_api_key(self, values):
        """The Claude API's key and its optional output limit: {key?, max_output_tokens?}."""
        return save_claude(self.settings.data_dir, values)

    def set_provider(self, values):
        return save_provider(self.settings.data_dir, values)

    def set_openai(self, values):
        return save_openai(self.settings.data_dir, values)

    def set_compat(self, values):
        return save_compat(self.settings.data_dir, values)

    def openai_settings(self):
        return settings_view(self.settings.data_dir)

    def test_connection(self, values, *, cancelled=lambda: False):
        """One tiny real request through the provider's answer path (connection_test.py)."""
        return connection_test.run(self.settings.data_dir, values, cancelled=cancelled)

    # -- 设置 → 学校账号: the school-login browser's sign-in session (school_browser.py)

    def _school(self):
        return school_browser.manager(self.settings)

    def school_login_status(self):
        return self._school().status()

    def school_login_start(self, values):
        """Open Google's sign-in in the remote view, sized like the page's view of it;
        `takeover` closes a sign-in window open elsewhere and opens it here."""
        keys = {"width", "height", "touch", "scale", "takeover"}
        if set(values) - keys or not {"width", "height"} <= set(values):
            raise WorkflowError("登录窗口只接受画面尺寸、是否触摸、缩放和是否在这里重新打开。")
        touch, scale = values.get("touch", False), values.get("scale", 1)
        takeover = values.get("takeover", False)
        if (
            type(touch) is not bool
            or type(takeover) is not bool
            or scale not in (1, 2)
            or type(scale) is not int
        ):
            raise WorkflowError("登录窗口的触摸、缩放或重新打开选项无效。")
        return self._school().start(
            width=values["width"],
            height=values["height"],
            touch=touch,
            scale=scale,
            takeover=takeover,
        )

    def school_login_frame(self, after, wait):
        """The newest view of the sign-in page; the JPEG goes to the page as base64."""
        import base64

        try:
            frame = self._school().frame(after=after, timeout=wait)
        except WorkflowError as exc:
            # The session is over (finished, cancelled, idle, or the page was gone too long).
            return {"ended": True, "message": str(exc)}
        return {
            **{key: value for key, value in frame.items() if key != "jpeg"},
            "image": base64.b64encode(frame.get("jpeg") or b"").decode("ascii"),
        }

    def school_login_input(self, values):
        # What the user types goes to the sign-in page only: never logged, stored or returned.
        return self._school().input(values)

    def school_login_action(self, name):
        browser = self._school()
        return {
            "back": browser.back,
            "finish": browser.finish,
            "cancel": browser.cancel,
            "logout": browser.logout,
        }[name]()

    def models(self):
        """The model list of the active provider, for the toolbar."""
        with self._models_lock:
            return self._catalog(active_provider(self.settings.data_dir))

    def _catalog(self, provider):
        # Claude's display names come with either list: past jobs and records keep naming their
        # Claude model "Claude Sonnet 5.5", not its id, while the endpoint is the active one.
        names = {x["model"]: x["displayName"] for x in model_catalog()["models"]}
        if provider == "openai":
            return {**openai_catalog(self.settings.data_dir), "claude_names": names}
        if provider == "anthropic_compatible":
            return {**compat_catalog(self.settings.data_dir), "claude_names": names}
        return {**model_catalog(), "provider": "anthropic", "claude_names": names}

    def _start_catalog(self, provider):
        """The list a new job's model is checked against. ui/models.json, which nothing in the
        app writes, can stand in for the Claude list (the tests use it) when it is a usable one;
        it never stands in for the OpenAI-compatible list, which always comes from the saved
        settings (as the Anthropic-compatible one does)."""
        if provider in KINDS:
            return self._catalog(provider)
        cached = read_json(self.root / "models.json")
        if (
            isinstance(cached, dict)
            and isinstance(cached.get("models"), list)
            and cached["models"]
            and cached.get("default_model")
            and cached.get("provider", "anthropic") == "anthropic"
        ):
            return cached
        return self._catalog("anthropic")

    def _start(
        self,
        kind,
        items,
        defer_media=False,
        model=None,
        effort=None,
        ai_confirmed=False,
        provider=None,
    ):
        with self._lock:
            if self._active:
                raise WorkflowError("已有任务在运行，请等它结束或暂停后再操作。")
            job = {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "status": "queued",
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "items": [{**x, "status": "waiting"} for x in items],
                "defer_media": defer_media,
                "model": model,
                "effort": effort,
                # Which service answers: fixed when the job starts, whatever the settings say later.
                "provider": provider,
                "ai_confirmed": ai_confirmed,
                "approval": None,
                "events": [],
                "message": "等待开始",
                "cache": None,
                "submission": "manual_only",
            }
            self._jobs[job["id"]] = job
            self._progress[job["id"]] = JobProgress(job, self.root / "progress-history.json")
            self._active = job["id"]
            self._cancel.clear()
            self._save(job)
            self._thread = threading.Thread(target=self._run, args=(job["id"],), daemon=True)
            self._thread.start()
            result = self.job(job["id"])
        # The power controller's preflight can acquire the jobs lock; notify
        # outside it so a countdown and a newly started worker cannot deadlock.
        if self._on_start:
            self._on_start(result)
        return result

    def refresh(self):
        return self._start("refresh", [])

    def authorize_documents(self):
        raise WorkflowError(
            "填写 Google 文档的写入授权将在后续版本提供；现在请打开答案或下载 Word 审阅。"
        )

    def fill_existing(self, job_id, key):
        package = self.package_path(job_id, key)
        source = self.job(job_id)
        item = next(x for x in source["items"] if item_key(x) == key)
        if not source.get("ai_confirmed"):
            raise WorkflowError("未确认使用 AI 的资料包不能自动填入。")
        if document_answers_incomplete(item) or form_generation_incomplete(item):
            return self.start_selected(
                [{"course_id": item["course_id"], "assignment_id": item["assignment_id"]}],
                defer_media=source.get("defer_media", False),
                model=source.get("model"),
                effort=source.get("effort"),
                ai_confirmed=True,
                provider=job_provider(source),
            )
        return self._start("fill", [{**item, "package": str(package)}], ai_confirmed=True)

    def export_answer(self, job_id, key):
        with self._lock:
            if self._active:
                raise WorkflowError("请等当前任务结束，再导出已有答案；不会重新下载或调用 AI。")
            view = self.package_view(job_id, key)
            if not view["text"]["draft.md"]:
                raise WorkflowError("没有已核验的实际答案可导出，请先完成或重新核验初稿。")
            result = create_answer_document(self.package_path(job_id, key))
            job = self._jobs[job_id]
            item = next(x for x in job["items"] if item_key(x) == key)
            item["answer_document"] = result
            if item["status"] in {"needs_user", "ready"}:
                item["status"] = "ready"
                if all(
                    x["status"] in {"ready", "document_ready", "form_opened"}
                    and not answers_incomplete(x)
                    for x in job["items"]
                ):
                    job["status"] = "completed"
                job["message"] = (
                    "答案文件已生成，打开答案或 Word 审阅；待确认项在审阅时核对，提交由你手动完成。"
                )
            self._save(job)
            return self._snapshot(job)

    def start_selected(
        self,
        selected: list[dict],
        *,
        defer_media=False,
        model=None,
        effort=None,
        ai_confirmed=False,
        provider=None,
        shown_provider=None,
    ):
        """provider None means the active one; a retry passes the provider its job used.

        shown_provider: the provider whose model list the page showed when the user chose (the
        toolbar, or the redo dialog). The model and effort were chosen from that list, so a job
        never starts on another provider, and so never bills another account: when the active
        provider was switched meanwhile (in another tab or on another device), it is refused."""
        if not isinstance(selected, list) or not 1 <= len(selected) <= 200:
            raise ConfigurationError("请勾选 1–200 项作业。")
        if not isinstance(defer_media, bool):
            raise ConfigurationError("录像选项无效。")
        if type(ai_confirmed) is not bool:
            raise ConfigurationError("AI 确认选项必须是勾选或未勾选。")
        pending = read_json(self.root / "pending.json")
        if not pending:
            raise WorkflowError("请先点击刷新，读取真实待完成作业。")
        available = {item_key(x): x for x in pending["assignments"]}
        items = []
        seen = set()
        for item in selected:
            if (
                not isinstance(item, dict)
                or set(item) != {"course_id", "assignment_id"}
                or any(not isinstance(v, str) or not v.isdigit() for v in item.values())
            ):
                raise ConfigurationError("作业选择无效。")
            key = item_key(item)
            if key not in available:
                raise WorkflowError("选中作业不在真实待办列表中，请刷新后重新勾选。")
            if key not in seen:
                items.append(available[key])
                seen.add(key)
        if not ai_confirmed:
            return self._start("review", items, defer_media, ai_confirmed=False)
        # The main button and the history retry must both reuse a valid mapping.
        # Empty mappings need a fresh inspection/generation, not an endless fill loop.
        latest = {}
        for previous in sorted(
            self._jobs.values(), key=lambda x: x.get("created_at", ""), reverse=True
        ):
            if previous["kind"] in {"review", "fill"} and previous.get("ai_confirmed"):
                for entry in previous["items"]:
                    latest.setdefault(item_key(entry), entry)
        reusable = [latest.get(item_key(item), {}) for item in items]
        if all(self._can_retry_fill(item) for item in reusable):
            return self._start("fill", reusable, ai_confirmed=True)
        # A provider passed in is the one a retried job used: the job keeps it, so the advice
        # when it cannot be used names that job, not the toolbar.
        pinned = provider is not None
        provider = provider or active_provider(self.settings.data_dir)
        if provider not in {"anthropic", "anthropic_compatible", "openai"}:
            raise ConfigurationError("模型接口无效。")
        if shown_provider is not None and not pinned and shown_provider != provider:
            if shown_provider not in PROVIDER_LABELS:
                raise ConfigurationError("模型接口无效。")
            shown = PROVIDER_LABELS[shown_provider]
            raise ConfigurationError(
                f"模型接口已在别处切换为 {PROVIDER_LABELS[provider]}，页面上列出的还是 {shown}"
                f"{' ' if shown[-1].isascii() else ''}的模型，没有开始作答。"
                "请重新选择模型后再点“完成”。"
            )
        kind = KINDS.get(provider)
        try:
            catalog = self._start_catalog(provider)
        except ConfigurationError:
            if not pinned or not kind:
                raise
            raise ConfigurationError(
                f"这个任务用的是 {kind.label}，它的设置已删除或损坏，不能按原样重试。"
                "请在设置里重新填写这个接口后再重试；或在列表里重新勾选这些作业，"
                "点“完成”用当前的模型接口作答。"
            ) from None
        model = model or catalog["default_model"]
        entry = next((x for x in catalog["models"] if x["model"] == model), None)
        if not entry:
            raise ConfigurationError(
                f"这个任务用的模型“{model}”已不在 {kind.label}的设置里。请在设置里加回这个模型"
                "后再重试；或在列表里重新勾选这些作业，用当前的模型作答。"
                if pinned and kind
                else f"所选模型不在 {kind.label}设置的模型里，请重新选择，或在设置里添加这个模型。"
                if kind
                else "所选模型不在可用模型列表中，请重新读取模型列表。"
            )
        allowed = {x["reasoningEffort"] for x in entry.get("supportedReasoningEfforts", [])}
        if effort is None:
            effort = (
                catalog["default_effort"]
                if model == catalog["default_model"]
                else entry.get("defaultReasoningEffort")
            )
        if effort is not None and effort not in allowed:
            raise ConfigurationError("所选模型不支持这个推理强度。")
        return self._start(
            "review", items, defer_media, model, effort, ai_confirmed, provider=provider
        )

    @staticmethod
    def _can_retry_fill(item):
        generation = item.get("generation") or {}
        return (
            item.get("status") in {"needs_document", "needs_form"}
            and not document_answers_incomplete(item)
            and not form_generation_incomplete(item)
            and item.get("package")
            and generation.get("status") == "completed"
            and generation.get("document_mapping_complete") is not False
            and (item["status"] == "needs_form" or bool(generation.get("document_answers_sha256")))
        )

    def retry(self, job_id):
        job = self.job(job_id)
        if job["status"] in ACTIVE:
            raise WorkflowError("任务仍在运行。")
        if self._recover_local_answers(job_id):
            return self.job(job_id)
        if job["kind"] == "refresh":
            return self.refresh()
        if job["kind"] == "document_auth":
            return self.authorize_documents()
        # Recovery may have completed some items. Never download or regenerate them
        # merely because another assignment in the original batch failed.
        job = self.job(job_id)
        remaining = [
            x
            for x in job["items"]
            if x["status"] not in {"ready", "document_ready", "form_opened", "document_needs_user"}
            or answers_incomplete(x)
            or (
                x["status"] == "ready"
                and not (x.get("package") and valid_answer_document(Path(x["package"])))
            )
        ]
        if not remaining:
            return job
        incomplete_mapping = any(
            document_answers_incomplete(x) or form_generation_incomplete(x) for x in remaining
        )
        if job["kind"] == "fill" and len(remaining) == 1 and not incomplete_mapping:
            return self.fill_existing(job_id, item_key(remaining[0]))
        if (job["kind"] == "fill" and not incomplete_mapping) or (
            job.get("ai_confirmed") and all(self._can_retry_fill(x) for x in remaining)
        ):
            # The fill adapters revalidate permissions, source context and answer
            # integrity. A fill failure does not require another generation pass.
            return self._start("fill", remaining, ai_confirmed=True)
        return self.start_selected(
            [{"course_id": x["course_id"], "assignment_id": x["assignment_id"]} for x in remaining],
            defer_media=job["defer_media"],
            model=job.get("model"),
            effort=job.get("effort"),
            ai_confirmed=job.get("ai_confirmed", False),
            provider=job_provider(job),
        )

    def _recover_local_answers(self, job_id):
        """Recover completed local reports before any synchronization or model call."""
        with self._lock:
            if self._active:
                return False
            job = self._jobs[job_id]
            recovered = False
            for item in job["items"]:
                if (
                    item.get("status") == "ready"
                    and item.get("package")
                    and not answers_incomplete(item)
                ):
                    package = self.package_path(job_id, item_key(item))
                    if valid_answer_document(package):
                        recovered = True
                        continue
                    # A newer export format only re-renders the verified draft; no AI call.
                    try:
                        document = create_answer_document(package)
                    except WorkflowError:
                        pass
                    else:
                        self._item(job_id, item, answer_document=document)
                        recovered = True
                        continue
                if item.get("status") != "failed" or not item.get("package"):
                    continue
                package = self.package_path(job_id, item_key(item))
                result = read_json(package / "model-result.json", {})
                if (
                    not job.get("ai_confirmed")
                    or result.get("document_answers")
                    or prepare_response_forms(package)
                ):
                    continue
                receipt = recover_completed_result(
                    package, model=job.get("model"), effort=job.get("effort")
                )
                if not receipt or not receipt.get("draft_sha256"):
                    continue
                document = create_answer_document(package)
                self._item(
                    job_id,
                    item,
                    generation=receipt["generation"],
                    status=document["status"],
                    answer_document=document,
                    error=None,
                    reused_review=True,
                )
                recovered = True
            if recovered:
                job["status"] = (
                    "completed"
                    if all(
                        x["status"] in {"ready", "document_ready", "form_opened"}
                        and not answers_incomplete(x)
                        for x in job["items"]
                    )
                    else "completed_with_issues"
                )
                self._log(
                    job_id,
                    "已核验并复用上次完整答案及审阅文件；未重新读取资料、未重新调用 AI。",
                )
            # A reusable item must not prevent the rest of a batch from retrying.
            return recovered and job["status"] == "completed"

    def supplement_item(self, job_id, key):
        item = next((x for x in self.job(job_id)["items"] if item_key(x) == key), None)
        if not item:
            raise WorkflowError("找不到这项作业。")
        return item

    def save_item_supplement(
        self,
        job_id,
        key,
        values,
        *,
        continue_run=False,
        model=None,
        effort=None,
        ai_confirmed=False,
        shown_provider=None,
    ):
        with self._lock:
            if self._active:
                raise WorkflowError(
                    "当前任务仍在处理；请等它完成或暂停后保存补充，避免改变本次输入。"
                )
            item = self.supplement_item(job_id, key)
            result = save_supplement(
                self.settings.data_dir, item["course_id"], item["assignment_id"], values
            )
            if not continue_run:
                return {"supplement": result, "status": "saved"}
            source = self.job(job_id)
            return self.start_selected(
                [{field: item[field] for field in ("course_id", "assignment_id")}],
                defer_media=source.get("defer_media", False),
                model=model,
                effort=effort,
                ai_confirmed=ai_confirmed,
                shown_provider=shown_provider,
            )

    def import_followup(self, job_id, key):
        raise WorkflowError("新版不再通过 Codex 会话接收补充；请在“调整答案或说明”里直接填写。")

    def import_questions(self, job_id, key, values):
        if set(values) != {"url", "question_script"}:
            raise WorkflowError("题目导入仅接受原表单链接和题目脚本数据。")
        if self.bootstrap()["active_job_id"]:
            raise WorkflowError("请等当前处理结束后导入题目。")
        package = self.package_path(job_id, key)
        manifest = read_json(package / "manifest.json")
        requirements = read_json(package / "requirements.json")
        allowed = set(form_references(manifest["assignment"]))
        allowed.update(form_references(requirements.get("student_submission") or {}))
        allowed.update(x["url"] for x in requirements.get("response_forms", []))
        for warning in manifest.get("warnings", []):
            if warning.get("url") and any(
                x.get("assignment_id") == manifest["assignment_id"]
                for x in warning.get("origins", [])
            ):
                try:
                    allowed.add(form_url(warning["url"]))
                except WorkflowError:
                    pass
        form = import_form_capture(
            self.settings.data_dir,
            school_account(self.settings),
            values["url"],
            values["question_script"],
            allowed,
        )
        return {
            "title": form["title"],
            "question_count": len(form["questions"]),
            "page_count": form["page_count"],
            "read_complete": form["read_complete"],
        }

    def save_form_inputs(self, job_id, key, values):
        if set(values) != {"answers", "apply"} or type(values["apply"]) is not bool:
            raise WorkflowError("补充答案只接受逐栏回答及是否立即填入。")
        package = self.package_path(job_id, key)
        manifest = read_json(package / "manifest.json")
        forms = prepare_response_forms(package)
        record = save_user_answers(
            self.settings.data_dir,
            manifest,
            forms,
            StudentProfile.load(self.settings.data_dir),
            values["answers"],
        )
        with self._lock:
            if values["apply"]:
                active = self._jobs.get(self._active, {})
                target = (
                    self._active
                    if any(item_key(x) == key for x in active.get("items", []))
                    else job_id
                )
                self._answer_requests[key] = target
                atomic_json(self.root / "answer-fill-requests.json", self._answer_requests)
                if not self._active:
                    started = self._next_answer_fill()
                    if started:
                        return started
            return {
                "status": "saved",
                "answer_count": len(record["answers"]),
                "processing_active": bool(self._active),
                "apply_queued": values["apply"],
            }

    def _next_answer_fill(self):
        """Drain explicit, durable UI fill requests after work finishes; never start AI."""
        with self._lock:
            if self._active:
                return None
            for key, job_id in list(self._answer_requests.items()):
                source = self._jobs.get(job_id, {})
                if source.get("status") not in {"completed", "completed_with_issues", "failed"}:
                    continue  # Pausing/interruption does not automatically resume work.
                del self._answer_requests[key]
                atomic_json(self.root / "answer-fill-requests.json", self._answer_requests)
                try:
                    item = next((x for x in source.get("items", []) if item_key(x) == key), None)
                    if item is None:
                        raise WorkflowError("找不到需要填入的作业记录。")
                    if not source.get("ai_confirmed"):
                        raise WorkflowError("未确认使用 AI 的资料包不能自动填入。")
                    package = self.package_path(job_id, key)
                    # Explicitly saved answers may complete an older partial mapping.
                    # Apply them before deciding whether any model work remains.
                    return self._start(
                        "fill", [{**item, "package": str(package)}], ai_confirmed=True
                    )
                except WorkflowError as exc:
                    item = next((x for x in source.get("items", []) if item_key(x) == key), None)
                    if item:
                        item.update(status="needs_form", error=str(exc))
                        self._save(source)
            return None

    def pause(self, job_id):
        with self._lock:
            if self._active != job_id:
                raise WorkflowError("这个任务当前没有运行。")
            self._cancel.set()
            job = self._jobs[job_id]
            job.update(
                status="stopping", message="正在暂停，当前资料处理结束后停止；已完成阶段会保留。"
            )
            if job_id in self._progress:
                self._progress[job_id].pause(True)
            self._save(job)
            return self.job(job_id)

    def _prepared_packages(self, job_id, job, assignment_event):
        """Yield after closing each course's Store lock, so drafting can start now."""
        cache = {"reused": 0, "processed": 0, "failed": 0}
        groups = course_batches(job["items"])
        for number, items in enumerate(groups, 1):
            self._log(
                job_id,
                ProgressUpdate(
                    f"正在处理第 {number}/{len(groups)} 门课程，共 {len(items)} 项作业",
                    "course_sync",
                    course_id=items[0]["course_id"],
                    course_index=number,
                    course_total=len(groups),
                ),
            )
            try:
                batch = prepare(
                    self.settings,
                    targets=[(x["course_id"], x["assignment_id"]) for x in items],
                    progress=lambda msg: self._log(job_id, msg),
                    on_assignment=assignment_event,
                )
            except RunCancelled:
                raise
            except WorkflowError as exc:
                for item in items:
                    self._item(job_id, item, status="failed", error=str(exc))
                if job_id in self._progress:
                    self._progress[job_id].prepared_courses.add(items[0]["course_id"])
                continue
            for key in cache:
                cache[key] += batch.get("cache", {}).get(key, 0)
            with self._lock:
                job["cache"] = dict(cache)
                self._save(job)
            self._log(
                job_id,
                ProgressUpdate(
                    "这门课程的资料已处理，接下来逐项作答",
                    "course_prepared",
                    course_id=items[0]["course_id"],
                ),
            )
            yield from batch["packages"]

    def _run(self, job_id):
        job = self._jobs[job_id]
        try:
            with self._lock:
                job["status"] = "running"
                job["started_at"] = utc_now()
                self._save(job)
            if job["kind"] == "fill":
                for item in job["items"]:
                    if prepare_response_forms(Path(item["package"])):
                        self._fill_response_item(job_id, item, Path(item["package"]))
                        if job_id in self._progress:
                            self._progress[job_id].item_done(item)
                        continue
                    self._item(job_id, item, status="filling_document")
                    try:
                        result = fill_review(
                            self.settings,
                            Path(item["package"]),
                            progress=lambda msg: self._log(job_id, msg),
                        )
                        self._item(
                            job_id,
                            item,
                            document_fill=result,
                            **document_fill_outcome(result),
                        )
                    except RunCancelled:
                        raise
                    except WorkflowError as exc:
                        self._item(job_id, item, status="needs_document", error=str(exc))
                    finally:
                        if job_id in self._progress:
                            self._progress[job_id].item_done(item)
                status = (
                    "completed"
                    if all(x["status"] in {"document_ready", "form_opened"} for x in job["items"])
                    else "completed_with_issues"
                )
            elif job["kind"] == "refresh":
                self._log(job_id, "正在从课程资料库读取待办")
                reader = KnowledgeClient.from_settings(self.settings)
                source = reader.status()
                pending = discover_pending(
                    reader,
                    timezone=self.settings.timezone,
                    include_no_due=True,
                    progress=lambda msg: self._log(job_id, msg),
                )
                if self._cancel.is_set():
                    raise RunCancelled("刷新已暂停。")
                pending["refreshed_at"] = utc_now()
                classroom = source.get("classroom") or {}
                pending["source"] = {
                    "index_built": source.get("built"),
                    "classroom_fetched": classroom.get("time"),
                    "classroom_ok": classroom.get("ok"),
                    "unmapped_courses": classroom.get("unmapped", []),
                }
                atomic_json(self.root / "pending.json", pending)
                self._log(
                    job_id, f"已读取 {len(pending['assignments'])} 项真实待办，包含无截止日期作业"
                )
                status = "completed" if pending["complete_discovery"] else "completed_with_issues"
            else:

                def assignment_event(item, result):
                    self._item(
                        job_id,
                        item,
                        status=("prepared" if "package" in result else result["status"]),
                        **{k: result[k] for k in ("package", "error") if k in result},
                    )

                provider = job_provider(job) or "anthropic"
                for result in self._prepared_packages(job_id, job, assignment_event):
                    unread = job.get("ai_confirmed") and unread_own_forms(
                        read_json(Path(result["package"]) / "manifest.json", {}) or {}
                    )
                    self._log(
                        job_id,
                        "课堂资料已准备，但这项作业的表单没有读到题目"
                        if unread
                        else "真实题目与课堂资料已准备"
                        + (
                            "；本次只整理资料"
                            if not job.get("ai_confirmed")
                            else "，正在交给 OpenAI 兼容接口的所选模型"
                            if provider == "openai"
                            else "，正在经 Anthropic 兼容接口交给所选 Claude 模型"
                            if provider == "anthropic_compatible"
                            else "，正在交给所选 Claude 模型"
                        ),
                    )
                    self._item(job_id, result, status="drafting")
                    try:
                        # Filling personal Google Docs needs write authorization, added later.
                        forms = []
                        if job_id in self._progress:
                            route = ["validate"] if job.get("ai_confirmed") else []
                            if forms:
                                route.append("fill")
                            if job.get("ai_confirmed") and prepare_response_forms(
                                Path(result["package"])
                            ):
                                route.append("form_fill")
                            self._progress[job_id].route(result, route)
                        receipt = generate_answer(
                            Path(result["package"]),
                            cancelled=self._cancel.is_set,
                            progress=lambda msg: self._log(job_id, msg),
                            model=job.get("model"),
                            effort=job.get("effort"),
                            provider=provider,
                            ai_confirmed=job.get("ai_confirmed", False),
                            forms=forms,
                            on_generation=lambda value, item=result: self._item(
                                job_id,
                                item,
                                generation=json.loads(json.dumps(value)),
                                **({"package": value["package"]} if value.get("package") else {}),
                            ),
                        )
                        self._item(
                            job_id,
                            result,
                            status=(
                                "ready"
                                if receipt["status"] == "ready_for_human_review"
                                else "failed"
                                if job.get("ai_confirmed")
                                else "materials_ready"
                            ),
                            reused_review=bool(receipt.get("reused")),
                            package=receipt["package"],
                        )
                        if receipt.get("draft_sha256"):
                            answer = create_answer_document(Path(receipt["package"]))
                            self._item(
                                job_id,
                                result,
                                answer_document=answer,
                                status="ready",
                                review_cautions=review_cautions(Path(receipt["package"]), receipt),
                            )
                        if job.get("ai_confirmed") and receipt.get("draft_sha256") and forms:
                            self._item(job_id, result, status="filling_document")
                            try:
                                filled = fill_review(
                                    self.settings,
                                    Path(receipt["package"]),
                                    progress=lambda msg: self._log(job_id, msg),
                                )
                                self._item(
                                    job_id,
                                    result,
                                    document_fill=filled,
                                    **document_fill_outcome(filled),
                                )
                            except RunCancelled:
                                raise
                            except WorkflowError as exc:
                                self._item(job_id, result, status="needs_document", error=str(exc))
                        if (
                            job.get("ai_confirmed")
                            and receipt.get("draft_sha256")
                            and prepare_response_forms(Path(receipt["package"]))
                        ):
                            self._fill_response_item(job_id, result, Path(receipt["package"]))
                    except RunCancelled:
                        raise
                    except FormsUnread as exc:
                        # The assignment's own form was not read: stopped before any model call.
                        self._item(job_id, result, status="needs_form", error=str(exc))
                    except MissingSources as exc:
                        # An empty answer naming what it lacks: the student supplies it.
                        self._item(job_id, result, status="needs_user", error=str(exc))
                    except WorkflowError as exc:
                        self._item(
                            job_id,
                            result,
                            status="failed",
                            error=str(exc),
                        )
                    finally:
                        if job_id in self._progress:
                            self._progress[job_id].item_done(result)
                status = (
                    "completed_with_issues"
                    if any(
                        x["status"]
                        in {
                            "failed",
                            "needs_document",
                            "needs_form",
                            "needs_user",
                            "document_needs_user",
                        }
                        for x in job["items"]
                    )
                    else "completed"
                )
                ready = sum(x["status"] == "document_ready" for x in job["items"])
                form_opened = sum(x["status"] == "form_opened" for x in job["items"])
                needs = sum(
                    x["status"]
                    in {"needs_user", "document_needs_user", "needs_document", "needs_form"}
                    for x in job["items"]
                )
                materials = sum(x["status"] == "materials_ready" for x in job["items"])
                local_ready = sum(x["status"] == "ready" for x in job["items"])
                failed = sum(x["status"] == "failed" for x in job["items"])
                counts = [
                    (local_ready, "项答案可审阅"),
                    (form_opened, "项预填表单已准备"),
                    (ready, "项已填入原文档"),
                    (needs, "项待补充"),
                    (materials, "项只整理了资料"),
                    (failed, "项失败"),
                ]
                summary = "，".join(f"{count} {label}" for count, label in counts if count)
                self._log(job_id, f"处理结束：{summary or '没有可处理的作业'}。提交由你操作。")
            with self._lock:
                job["status"] = status
        except RunCancelled as exc:
            with self._lock:
                job.update(status="paused", message=str(exc))
        except Exception as exc:
            with self._lock:
                job.update(
                    status="failed",
                    message=str(exc)
                    if isinstance(exc, WorkflowError)
                    else "本机处理失败。私人错误详情未输出，请重试。",
                )
        finally:
            with self._lock:
                if job["status"] in {"paused", "failed"}:
                    for item in job["items"]:
                        if item["status"] in {
                            "waiting",
                            "preparing",
                            "prepared",
                            "drafting",
                            "filling_document",
                        }:
                            item["status"] = job["status"]
                job["finished_at"] = utc_now()
                job["approval"] = None
                if job_id in self._progress:
                    self._progress[job_id].finish(job["status"] == "completed")
                try:
                    self._save(job)
                finally:
                    self._active = None
                    if job["status"] in {"completed", "completed_with_issues"}:
                        for item in job["items"]:
                            key = item_key(item)
                            if key in self._answer_requests:
                                self._answer_requests[key] = job_id
                        atomic_json(self.root / "answer-fill-requests.json", self._answer_requests)
                        self._next_answer_fill()

    def wait_idle(self, timeout=30):
        thread = self._thread
        if thread:
            thread.join(timeout)
        return self._active is None

    def _fill_response_item(self, job_id, item, package):
        self._item(job_id, item, status="filling_form")
        try:
            result = fill_response_forms(
                self.settings, package, progress=lambda msg: self._log(job_id, msg)
            )
            generation = read_json(package / "generation.json", {}) or {}
            self._item(
                job_id,
                item,
                form_fill=result,
                generation=generation,
                **form_fill_outcome(result, generation),
            )
        except RunCancelled:
            raise
        except WorkflowError as exc:
            self._item(job_id, item, status="needs_form", error=str(exc))

    def policy(self, course_id):
        pending = read_json(self.root / "pending.json", {"assignments": []})
        if course_id not in {x["course_id"] for x in pending["assignments"]}:
            raise WorkflowError("只能配置真实待办列表中的课程。")
        return asdict(CoursePolicy.load(self.settings.data_dir, course_id))

    def save_policy(self, course_id, values):
        with self._lock:
            if self._active:
                raise WorkflowError("请先等当前任务结束或暂停，再修改课程规则。")
            if not isinstance(values, dict) or set(values) - POLICY_FIELDS:
                raise ConfigurationError("规则表单包含不支持的字段。")
            if any(
                not isinstance(v, str) or len(v) > 30000
                for k, v in values.items()
                if k
                not in {
                    "personal_facts",
                    "max_attachment_bytes",
                    "max_pdf_pages",
                    "unconfirmed_drafting",
                }
            ):
                raise ConfigurationError("规则与来源必须是文字，长度不能超过 30000 字符。")
            for field in ("max_attachment_bytes", "max_pdf_pages"):
                if field in values and type(values[field]) is not int:
                    raise ConfigurationError("资料处理上限必须是整数。")
            if "unconfirmed_drafting" in values:
                if type(values["unconfirmed_drafting"]) is not bool:
                    raise ConfigurationError("未确认规则下的初稿选项必须由你明确选择。")
                values = {
                    **values,
                    "user_drafting_instruction": "用户在本机课程设置中明确选择：未找到教师 AI 规定时继续生成初稿，保留教师规则未确认的说明；遵守实际发现的更严格规定。"
                    if values["unconfirmed_drafting"]
                    else "",
                }
            facts = values.get("personal_facts", [])
            if (
                not isinstance(facts, list)
                or len(facts) > 200
                or any(not isinstance(x, str) or len(x) > 10000 for x in facts)
            ):
                raise ConfigurationError("个人事实必须由你提供，每行一项。")
            current = CoursePolicy(**self.policy(course_id))
            updated = replace(current, **values).validated()
            updated.save(self.settings.data_dir, course_id)
            return asdict(updated)

    def package_path(self, job_id, key):
        job = self.job(job_id)
        item = next((x for x in job["items"] if item_key(x) == key), None)
        if not item or not item.get("package"):
            raise WorkflowError("这项作业的资料包尚未准备好。")
        package = require_private_path(Path(item["package"]))
        expected = (
            self.settings.data_dir / "review-packages" / item["course_id"] / item["assignment_id"]
        )
        if package.parent != expected or not re.fullmatch(r"[a-f0-9]{24}", package.name):
            raise WorkflowError("审核包路径无效。")
        return package

    def package_view(self, job_id, key):
        package = self.package_path(job_id, key)
        manifest = read_json(self.package_file(package, "manifest.json"))
        if not manifest:
            raise WorkflowError("审核包缺少真实要求，请重新准备。")
        receipt = read_json(self.package_file(package, "review-receipt.json"))
        current_gate = CoursePolicy.load(self.settings.data_dir, manifest["course_id"]).draft_gate()
        if "ai_confirmation" in manifest:
            current_gate = confirmed_gate(current_gate, manifest["ai_confirmation"])
        supplement = load_supplement(
            self.settings.data_dir, manifest["course_id"], manifest["assignment_id"]
        )
        changed = current_gate != manifest["policy"] or supplement != manifest.get(
            "supplement", normalized({})
        )
        generation = read_json(self.package_file(package, "generation.json"))
        text = {}
        for name in ("draft.md", "checklist.md", "questions.md", "summary.md"):
            path = self.package_file(package, name)
            if name == "draft.md" and (
                changed
                or not generation
                or generation.get("status") != "completed"
                or not manifest["policy"]["can_draft"]
                or not receipt
                or not receipt.get("draft_sha256")
                or not path.is_file()
                or sha256_file(path) != receipt["draft_sha256"]
            ):
                text[name] = ""
                continue
            text[name] = path.read_text(encoding="utf-8") if path.is_file() else ""
        fields_error = None
        try:
            response_fields = prepare_response_forms(package)
        except WorkflowError as exc:
            response_fields, fields_error = [], str(exc)
        try:
            user_form_answers = load_user_answers(
                self.settings.data_dir,
                manifest,
                response_fields,
                StudentProfile.load(self.settings.data_dir),
            )
        except WorkflowError as exc:
            user_form_answers = {"answers": [], "sha256": None}
            fields_error = str(exc)
        return {
            "manifest": manifest,
            "text": text,
            "requirements": read_json(self.package_file(package, "requirements.json")),
            "evidence": read_json(self.package_file(package, "evidence.json")),
            "review": read_json(self.package_file(package, "review.json")),
            "receipt": receipt,
            "policy_changed": changed,
            "supplement": supplement,
            "response_fields": response_fields,
            "model_form_answers": read_json(package / "form-answers.json", {"answers": []})[
                "answers"
            ],
            "user_form_answers": user_form_answers,
            "response_fields_error": fields_error,
            "generation": generation,
            "document_fill": read_json(package / "document-fill.json"),
            "form_fill": read_json(package / "form-fill.json") if not changed else None,
            "answer_document": valid_answer_document(package) if text["draft.md"] else None,
        }

    @staticmethod
    def package_file(package, name):
        if name not in FILES:
            raise WorkflowError("审核文件类型无效。")
        path = (package / name).resolve()
        if path.parent != package:
            raise WorkflowError("审核文件不能链接到其他私人文件。")
        return path

    def source_image(self, job_id, key, source_id):
        package = self.package_path(job_id, key)
        evidence = read_json(self.package_file(package, "evidence.json"), {"sources": []})
        source = next((x for x in evidence["sources"] if x["id"] == source_id), None)
        if not source or not source.get("image_path"):
            raise WorkflowError("该来源没有页面图像。")
        path = require_private_path(Path(source["image_path"]))
        if (
            not path.is_relative_to(self.settings.data_dir / "processed")
            or path.suffix.lower() != ".png"
            or not path.is_file()
        ):
            raise WorkflowError("页面图像路径无效。")
        return path
