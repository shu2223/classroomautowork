"""Persistent local frontend jobs, independent of HTTP and independently callable."""

import json
import re
import threading
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from .auth import credentials_for
from .codex_rpc import model_catalog
from .config import Settings
from .drafting import codex_command, generate_review
from .errors import ConfigurationError, RunCancelled, WorkflowError
from .google_read import GoogleReader
from .local import atomic_json, require_private_path, sha256_file, utc_now
from .pending import discover_pending
from .policy import CoursePolicy, confirmed_gate
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
    "README.txt",
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
                    if item.get("status") in {"waiting", "preparing", "prepared", "drafting"}:
                        item["status"] = "interrupted"
                atomic_json(path, job)
            self._jobs[job["id"]] = job

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
                        "account": self.settings.school_email,
                        "timezone": self.settings.timezone,
                        "pending": pending,
                        "policies": policies,
                        "jobs": jobs,
                        "active_job_id": self._active,
                        "submission": "manual_only",
                    }
                )
            )

    def job(self, job_id: str):
        with self._lock:
            if job_id not in self._jobs:
                raise WorkflowError("找不到该本机任务记录。")
            return json.loads(json.dumps(self._jobs[job_id]))

    def _log(self, job_id, message):
        if self._cancel.is_set():
            raise RunCancelled("已暂停，完成的资料与审核包会保留。")
        with self._lock:
            job = self._jobs[job_id]
            job["message"] = str(message)[:500]
            if not job["events"] or job["events"][-1]["message"] != job["message"]:
                job["events"].append({"at": utc_now(), "message": job["message"]})
                job["events"] = job["events"][-150:]
            self._save(job)

    def _item(self, job_id, target, **values):
        with self._lock:
            job = self._jobs[job_id]
            entry = next(x for x in job["items"] if item_key(x) == item_key(target))
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
                self._save(job)
            if job["kind"] == "refresh":
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
                        receipt = generate_review(
                            Path(result["package"]),
                            cancelled=self._cancel.is_set,
                            progress=lambda msg: self._log(job_id, msg),
                            model=job.get("model"),
                            effort=job.get("effort"),
                            ai_confirmed=job.get("ai_confirmed", False),
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
                    except RunCancelled:
                        raise
                    except WorkflowError as exc:
                        self._item(job_id, result, status="failed", error=str(exc))
                status = (
                    "completed_with_issues"
                    if any(x["status"] == "failed" for x in job["items"])
                    else "completed"
                )
                ready = sum(x["status"] == "ready" for x in job["items"])
                needs = sum(x["status"] == "needs_user" for x in job["items"])
                materials = sum(x["status"] == "materials_ready" for x in job["items"])
                self._log(
                    job_id,
                    f"处理结束：{ready} 项实际初稿待审阅，{materials} 项资料包，{needs} 项待补充；没有提交任何作业",
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
                        if item["status"] in {"waiting", "preparing", "prepared", "drafting"}:
                            item["status"] = job["status"]
                job["finished_at"] = utc_now()
                job["approval"] = None
                try:
                    self._save(job)
                finally:
                    self._active = None

    def wait_idle(self, timeout=30):
        thread = self._thread
        if thread:
            thread.join(timeout)
        return self._active is None

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
        changed = current_gate != manifest["policy"]
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
        return {
            "manifest": manifest,
            "text": text,
            "requirements": read_json(self.package_file(package, "requirements.json")),
            "evidence": read_json(self.package_file(package, "evidence.json")),
            "review": read_json(self.package_file(package, "review.json")),
            "receipt": receipt,
            "policy_changed": changed,
            "generation": generation,
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
