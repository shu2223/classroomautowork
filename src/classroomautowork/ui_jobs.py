"""Persistent local frontend jobs, independent of HTTP and independently callable."""

import json
import re
import threading
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from .auth import credentials_for, documents_authorized
from .browser_questions import recover_questions
from .codex_rpc import model_catalog
from .config import Settings
from .document_fill import fill_review, prepare_document_forms
from .drafting import codex_command, generate_review
from .errors import ConfigurationError, NeedsInput, RunCancelled, WorkflowError
from .followups import read_followup
from .form_capture import import_form_capture
from .form_fill import fill_response_forms, prepare_response_forms
from .form_read import form_references, form_url
from .google_read import GoogleReader
from .local import atomic_json, require_private_path, sha256_file, utc_now
from .pending import discover_pending
from .policy import CoursePolicy, confirmed_gate
from .progress import ProgressUpdate
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
    "codex-summary.md",
    "codex-generation.json",
    "codex-result.json",
    "README.txt",
    "document-fill.json",
    "document-answers.json",
    "form-answers.json",
    "form-fill.json",
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


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


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
        self._approval_answers = {}
        self._jobs = {}
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
            return json.loads(
                json.dumps(
                    {
                        "documents_authorized": documents_authorized(self.settings),
                        "account": self.settings.school_email,
                        "timezone": self.settings.timezone,
                        "pending": pending,
                        "policies": policies,
                        "jobs": [self._snapshot(x) for x in jobs],
                        "active_job_id": self._active,
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
                same_step = all(
                    previous.get(key) == details.get(key) for key in ("stage", "file_id", "item_id")
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
            self._save(job)

    def models(self):
        with self._models_lock:
            result = model_catalog(codex_command())
            atomic_json(self.root / "codex-models.json", result)
            return result

    def _start(self, kind, items, defer_media=False, model=None, effort=None, ai_confirmed=False):
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
                "ai_confirmed": ai_confirmed,
                "approval": None,
                "events": [],
                "message": "等待开始",
                "cache": None,
                "submission": "manual_only",
            }
            self._jobs[job["id"]] = job
            self._active = job["id"]
            self._cancel.clear()
            self._save(job)
            self._thread = threading.Thread(target=self._run, args=(job["id"],), daemon=True)
            self._thread.start()
            return self.job(job["id"])

    def refresh(self):
        return self._start("refresh", [])

    def authorize_documents(self):
        return self._start("document_auth", [])

    def fill_existing(self, job_id, key):
        package = self.package_path(job_id, key)
        source = self.job(job_id)
        item = next(x for x in source["items"] if item_key(x) == key)
        if not source.get("ai_confirmed"):
            raise WorkflowError("未确认使用 AI 的资料包不能自动填入。")
        return self._start("fill", [{**item, "package": str(package)}], ai_confirmed=True)

    def start_selected(
        self,
        selected: list[dict],
        *,
        defer_media=False,
        model=None,
        effort=None,
        ai_confirmed=False,
    ):
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
        catalog = read_json(self.root / "codex-models.json") or self.models()
        model = model or catalog["default_model"]
        entry = next((x for x in catalog["models"] if x["model"] == model), None)
        if not entry:
            raise ConfigurationError(
                "模型不在本机 Codex 返回的目录或当前配置中，请重新读取模型列表。"
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
        return self._start("review", items, defer_media, model, effort, ai_confirmed)

    def retry(self, job_id):
        job = self.job(job_id)
        if job["status"] in ACTIVE:
            raise WorkflowError("任务仍在运行。")
        if job["kind"] == "refresh":
            return self.refresh()
        if job["kind"] == "document_auth":
            return self.authorize_documents()
        if job["kind"] == "fill":
            return self.fill_existing(job_id, item_key(job["items"][0]))
        return self.start_selected(
            [
                {"course_id": x["course_id"], "assignment_id": x["assignment_id"]}
                for x in job["items"]
            ],
            defer_media=job["defer_media"],
            model=job.get("model"),
            effort=job.get("effort"),
            ai_confirmed=job.get("ai_confirmed", False),
        )

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
            )

    def import_followup(self, job_id, key):
        if self.bootstrap()["active_job_id"]:
            raise WorkflowError("请等当前处理结束后接收补充。")
        package = self.package_path(job_id, key)
        result = read_followup(package)
        item = self.supplement_item(job_id, key)
        supplement = load_supplement(
            self.settings.data_dir, item["course_id"], item["assignment_id"]
        )
        marker = f"[Codex 补充回合 {result['turn_id']}]"
        if marker not in supplement["text"]:
            supplement = save_supplement(
                self.settings.data_dir,
                item["course_id"],
                item["assignment_id"],
                {
                    "text": supplement["text"]
                    + "\n\n"
                    + marker
                    + "\n"
                    + "\n\n".join(
                        (
                            "用户补充："
                            if record["role"] == "user"
                            else "AI 候选内容（需要重新核验来源，不是个人事实）："
                        )
                        + record["text"]
                        for record in result["records"]
                    ),
                    "personal_facts": supplement["personal_facts"],
                },
            )
        return {
            "supplement": supplement,
            "followup": {k: v for k, v in result.items() if k != "records"},
        }

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
            self.settings.school_email,
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
                    return self.fill_existing(job_id, key)
                except WorkflowError as exc:
                    item = next((x for x in source.get("items", []) if item_key(x) == key), None)
                    if item:
                        item.update(status="needs_form", error=str(exc))
                        self._save(source)
            return None

    def _request_approval(self, job_id, method, params):
        identifier = uuid.uuid4().hex
        with self._lock:
            job = self._jobs[job_id]
            job["approval"] = {
                "id": identifier,
                "method": method,
                "command": str(params.get("command", ""))[:20000],
                "cwd": str(params.get("cwd", params.get("grantRoot", "")))[:2000],
                "reason": str(params.get("reason", "Codex 的现有权限规则要求确认本次操作。"))[
                    :2000
                ],
                "thread_id": params.get("threadId"),
            }
            job["message"] = "Codex 正等待你的本次操作审批；课程内容不能自动批准。"
            job["approval"]["requested_at"] = utc_now()
            self._save(job)
        try:
            for _ in range(4500):
                if self._cancel.wait(0.2):
                    raise RunCancelled("已暂停等待审批的 Codex 会话。")
                with self._lock:
                    if identifier in self._approval_answers:
                        return self._approval_answers.pop(identifier)
            return "cancel"
        finally:
            with self._lock:
                job["approval"] = None
                self._save(job)

    def approve(self, job_id, identifier, decision):
        with self._lock:
            job = self._jobs.get(job_id)
            if (
                self._active != job_id
                or not job
                or not job.get("approval")
                or job["approval"]["id"] != identifier
            ):
                raise WorkflowError("这个审批请求已经结束或不属于当前任务。")
            if decision not in {"accept", "decline", "cancel"}:
                raise ConfigurationError("只允许批准本次、拒绝或取消，不支持扩大长期权限。")
            if identifier in self._approval_answers:
                raise WorkflowError("这项审批已经回答。")
            self._approval_answers[identifier] = decision
            return {"status": "answered"}

    def pause(self, job_id):
        with self._lock:
            if self._active != job_id:
                raise WorkflowError("这个任务当前没有运行。")
            self._cancel.set()
            job = self._jobs[job_id]
            job.update(
                status="stopping", message="正在暂停，当前资料处理结束后停止；已完成阶段会保留。"
            )
            self._save(job)
            return self.job(job_id)

    def _run(self, job_id):
        job = self._jobs[job_id]
        try:
            with self._lock:
                job["status"] = "running"
                job["started_at"] = utc_now()
                self._save(job)
            if job["kind"] == "document_auth":
                self._log(
                    job_id,
                    "请在 Google 页面确认文档读写授权。权限允许编辑账户可编辑的文档；本程序只填写本人待完成作业副本。",
                )
                credentials_for(self.settings, authorize=True, documents=True)
                self._log(
                    job_id,
                    "学校账户的文档读写授权已实际完成，可以自动填入；课堂提交与留言仍由你手动操作。",
                )
                status = "completed"
            elif job["kind"] == "fill":
                for item in job["items"]:
                    if prepare_response_forms(Path(item["package"])):
                        self._fill_response_item(job_id, item, Path(item["package"]))
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
                            status="document_needs_user"
                            if any(d["remaining_empty_fields"] for d in result["documents"])
                            else "document_ready",
                            document_fill=result,
                            error=None,
                        )
                    except RunCancelled:
                        raise
                    except WorkflowError as exc:
                        self._item(job_id, item, status="needs_document", error=str(exc))
                status = (
                    "completed"
                    if all(x["status"] in {"document_ready", "form_opened"} for x in job["items"])
                    else "completed_with_issues"
                )
            elif job["kind"] == "refresh":
                self._log(job_id, "正在连接学校账户并读取真实待办")
                credentials, _ = credentials_for(self.settings)
                pending = discover_pending(
                    GoogleReader(credentials),
                    timezone=self.settings.timezone,
                    include_no_due=True,
                    progress=lambda msg: self._log(job_id, msg),
                )
                if self._cancel.is_set():
                    raise RunCancelled("刷新已暂停。")
                pending["refreshed_at"] = utc_now()
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

                batch = prepare(
                    self.settings,
                    targets=[(x["course_id"], x["assignment_id"]) for x in job["items"]],
                    defer_media=job["defer_media"],
                    progress=lambda msg: self._log(job_id, msg),
                    on_assignment=assignment_event,
                )
                with self._lock:
                    job["cache"] = batch["cache"]
                    self._save(job)
                for result in batch["packages"]:
                    self._log(
                        job_id,
                        "真实题目与课堂资料已准备"
                        + (
                            "，正在交给所选 Codex 模型"
                            if job.get("ai_confirmed")
                            else "；本次只整理资料"
                        ),
                    )
                    self._item(job_id, result, status="drafting")
                    try:
                        forms = []
                        if job.get("ai_confirmed"):
                            recovered = recover_questions(
                                self.settings,
                                Path(result["package"]),
                                model=job["model"],
                                effort=job.get("effort"),
                                cancelled=self._cancel.is_set,
                                progress=lambda msg: self._log(job_id, msg),
                                approval=lambda method, params: self._request_approval(
                                    job_id, method, params
                                ),
                                on_generation=lambda value, item=result: self._item(
                                    job_id, item, browser_read=value
                                ),
                            )
                            if recovered:
                                refreshed = prepare(
                                    self.settings,
                                    targets=[(result["course_id"], result["assignment_id"])],
                                    defer_media=job["defer_media"],
                                    progress=lambda msg: self._log(job_id, msg),
                                    on_assignment=assignment_event,
                                )
                                if not refreshed["packages"]:
                                    raise NeedsInput(
                                        "原题已读取，但重新准备资料尚未成功，请补充后继续。"
                                    )
                                result.update(refreshed["packages"][0])
                            self._log(
                                job_id,
                                ProgressUpdate(
                                    "正在核对原文档答案栏与表单逐栏映射，模型作答后将自动填写",
                                    "document_inspect",
                                ),
                            )
                            forms = prepare_document_forms(self.settings, Path(result["package"]))
                        receipt = generate_review(
                            Path(result["package"]),
                            cancelled=self._cancel.is_set,
                            progress=lambda msg: self._log(job_id, msg),
                            model=job.get("model"),
                            effort=job.get("effort"),
                            ai_confirmed=job.get("ai_confirmed", False),
                            forms=forms,
                            on_generation=lambda value, item=result: self._item(
                                job_id,
                                item,
                                generation=json.loads(json.dumps(value)),
                                **({"package": value["package"]} if value.get("package") else {}),
                            ),
                            approval=lambda method, params: self._request_approval(
                                job_id, method, params
                            ),
                        )
                        self._item(
                            job_id,
                            result,
                            status=(
                                "ready"
                                if receipt["status"] == "ready_for_human_review"
                                else "needs_user"
                                if job.get("ai_confirmed")
                                else "materials_ready"
                            ),
                            reused_review=bool(receipt.get("reused")),
                            package=receipt["package"],
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
                                    status="document_needs_user"
                                    if any(d["remaining_empty_fields"] for d in filled["documents"])
                                    else "document_ready",
                                    document_fill=filled,
                                    error=None,
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
                    except WorkflowError as exc:
                        self._item(
                            job_id,
                            result,
                            status="needs_user" if isinstance(exc, NeedsInput) else "failed",
                            error=str(exc),
                        )
                status = (
                    "completed_with_issues"
                    if any(
                        x["status"] in {"failed", "needs_document", "needs_form"}
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
                self._log(
                    job_id,
                    f"处理结束：{ready} 项原文档已填入，{form_opened} 项已打开自动预填原表单，{local_ready} 项本机答案，{materials} 项资料包，{needs} 项待补充，{failed} 项失败；请在原文档或原表单审阅并手动提交",
                )
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
                    else "本机处理失败。私人错误详情未输出，请重试或在 Codex 中检查。",
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
            self._item(
                job_id,
                item,
                status="needs_user"
                if any(f["remaining_fields"] for f in result["forms"])
                else "form_opened",
                form_fill=result,
                error=None,
                generation=read_json(package / "codex-generation.json"),
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
        generation = read_json(self.package_file(package, "codex-generation.json"))
        text = {}
        for name in ("draft.md", "checklist.md", "questions.md", "codex-summary.md"):
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
