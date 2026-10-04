"""Populate responder fields using Google's native prefill, never submit responses.

This module has no response POST, submit action, browser credentials or form-edit API.
The browser opens a validated viewform GET with exact native entry IDs and values.
Opening is recorded separately from browser verification and Google's draft saving.
"""

import hashlib
import json
import re
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

from filelock import FileLock, Timeout

from .errors import WorkflowError
from .form_read import form_references, form_url, read_form
from .local import atomic_json, require_private_path, sha256_file, utc_now
from .policy import CoursePolicy, confirmed_gate
from .progress import ProgressUpdate
from .student import StudentProfile

SUPPORTED = {"short_text", "paragraph", "single_choice", "dropdown", "checkbox", "scale"}


def context_digest(form):
    return hashlib.sha256(
        json.dumps(
            {
                "url": form_url(form["url"]),
                "questions": sorted(form["questions"], key=lambda q: q["item_id"]),
                "sections": form["sections"],
                "description": form["description"],
                "title": form["title"],
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def prepare_response_forms(package: Path) -> list[dict]:
    """Use only this assignment's already collected requirement sources."""
    requirements = json.loads((package / "requirements.json").read_text(encoding="utf-8"))
    result = []
    for ref in requirements.get("response_forms", []):
        url = form_url(ref["url"])
        sources = [
            json.loads(x["text"])
            for x in requirements["sources"]
            if x.get("source_kind") == "form" and x.get("url") == url
        ]
        header = next((x for x in sources if "sections" in x), None)
        questions = [x for x in sources if "item_id" in x and "fields" in x]
        if not header or not questions:
            raise WorkflowError("当前作业的表单题目未完整入包，不能猜测填写位置。")
        form = {**header, "url": url, "questions": questions}
        form["context_sha256"] = context_digest(form)
        result.append(form)
    return result


def validate_answers(forms: list[dict], answers: list[dict], profile=None) -> list[dict]:
    """Never allow model-provided destinations, arbitrary fields or invented options."""
    if not isinstance(answers, list):
        raise WorkflowError("表单答案必须是逐栏数组。")
    lookup = {
        (f["url"], field["entry_id"]): (f, q, field)
        for f in forms
        for q in f["questions"]
        for field in q["fields"]
    }
    seen, validated = set(), []
    for answer in answers:
        if not isinstance(answer, dict):
            raise WorkflowError("表单答案结构无效。")
        if any(not isinstance(answer.get(k), str) for k in ("form_url", "entry_id")):
            raise WorkflowError("表单目标和栏位必须是原生字符串标识。")
        key = (answer.get("form_url"), answer.get("entry_id"))
        if key in seen or key not in lookup:
            raise WorkflowError("表单答案指向非本次作业、未知栏位或重复栏位。")
        seen.add(key)
        form, question, field = lookup[key]
        if answer.get("context_sha256") != form["context_sha256"]:
            raise WorkflowError("表单原题已变化，不能沿用旧答案填写。")
        values = answer.get("values")
        if values == [] and not field["required"]:
            # An explicitly unanswered optional feedback field is not an invalid answer.
            # Preserve it blank without discarding all of the usable assignment answers.
            continue
        if (
            question["type"] not in SUPPORTED
            or not isinstance(values, list)
            or not values
            or any(
                not isinstance(x, str)
                or not x.strip()
                or len(x) > 20000
                or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", x)
                or "[E:" in x
                for x in values
            )
            or len(set(values)) != len(values)
            or (question["type"] != "checkbox" and len(values) != 1)
        ):
            raise WorkflowError("表单答案包含不支持的题型、空值或内部审核标记。")
        if field["choices"] and set(values) - set(field["choices"]):
            raise WorkflowError("表单答案包含原题不存在的选项，未打开填写。")
        # Identity cannot be changed by course content or model suggestions.
        identity = {"学籍番号": "student_id", "学号": "student_id", "氏名": "name", "姓名": "name"}
        label = question["title"].strip()
        if profile and label in identity:
            expected = getattr(profile, identity[label])
            if not expected or values != [expected]:
                raise WorkflowError("表单身份答案与用户私人配置不一致。")
        validated.append(answer)
    return validated


def prefill_url(form: dict, answers: list[dict], *, account="") -> str:
    """Only a viewform GET; repeated checkbox entry parameters select exact values."""
    values = validate_answers([form], answers)
    if not values:
        raise WorkflowError("没有实际可填的表单答案；不能把空链接称为已填写。")
    params = [("usp", "pp_url"), ("srd", "true")]
    if account:
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", account):
            raise WorkflowError("表单账户无效。")
        params.append(("authuser", account))
        # Native collected-email fields (including the verified-email checkbox)
        # are populated by this responder parameter; it does not submit a response.
        params.append(("emailAddress", account))
    for answer in values:
        if not re.fullmatch(r"\d{1,20}", answer["entry_id"]):
            raise WorkflowError("表单原生 entry ID 无效。")
        params.extend(("entry." + answer["entry_id"], x) for x in answer["values"])
    url = form_url(form["url"]) + "?" + urlencode(params)
    if len(url) > 60000:
        raise WorkflowError("答案超过原生预填链接大小限制，初稿已保留，未省略答案冒充完成。")
    return url


def current_response_urls(reader, course_id, assignment_id):
    submissions = [
        x for x in reader.own_submissions(course_id) if x.get("courseWorkId") == assignment_id
    ]
    if len(submissions) != 1 or submissions[0].get("state") not in {
        "NEW",
        "CREATED",
        "RECLAIMED_BY_STUDENT",
    }:
        raise WorkflowError("作业不再是本人待完成状态，未填入表单。")
    assignment = reader.assignment(course_id, assignment_id)
    return set(form_references(assignment)) | set(form_references(submissions[0]))


def fill_response_forms(settings, package: Path, *, progress=lambda _: None, opener=None) -> dict:
    """Revalidate actual model output, current rules and fresh original questions."""
    from .auth import credentials_for
    from .drafting import reusable_review
    from .google_read import GoogleReader

    package = require_private_path(package)
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    current = confirmed_gate(
        CoursePolicy.load(settings.data_dir, manifest["course_id"]).draft_gate(),
        manifest.get("ai_confirmation", False),
    )
    if current != manifest["policy"] or not current["can_draft"]:
        raise WorkflowError("课程规则已变化或不允许作答，未填入表单。")
    if manifest.get("student_profile") != StudentProfile.load(settings.data_dir).__dict__:
        raise WorkflowError("学生资料已变化，请重新生成答案。")
    receipt = reusable_review(package)
    if not receipt or not receipt.get("draft_sha256"):
        raise WorkflowError("表单填写需要真实模型生成并通过核验的初稿。")
    generation = receipt["generation"]
    answers_path = package / "form-answers.json"
    if not answers_path.is_file() or sha256_file(answers_path) != generation.get(
        "form_answers_sha256"
    ):
        raise WorkflowError("旧初稿没有核验过的逐栏表单答案，请使用所选模型重新生成。")
    forms = prepare_response_forms(package)
    read_credentials, _ = credentials_for(settings)
    allowed = current_response_urls(
        GoogleReader(read_credentials), manifest["course_id"], manifest["assignment_id"]
    )
    if {x["url"] for x in forms} - allowed:
        raise WorkflowError("表单不再属于这项本人待完成作业，未填写其他课程链接。")
    answers = validate_answers(
        forms,
        json.loads(answers_path.read_text(encoding="utf-8"))["answers"],
        StudentProfile.load(settings.data_dir),
    )
    plans = []
    for form in forms:
        progress(ProgressUpdate("正在核对原表单题目与自动填写的栏位", "form_fill"))
        fresh = read_form(form["url"])
        if context_digest(fresh) != form["context_sha256"]:
            raise WorkflowError("原表单题目或选项已修改，请重新读题生成；未覆盖草稿。")
        selected = [x for x in answers if x["form_url"] == form["url"]]
        url = prefill_url(form, selected, account=settings.school_email)
        filled = {x["entry_id"] for x in selected}
        plans.append(
            {
                "title": form["title"],
                "url": url,
                "original_url": form["url"],
                "prefilled_fields": len(selected),
                "needs_user": [x["review_note"] for x in selected if x.get("needs_user")],
                "remaining_fields": [
                    {"title": q["title"], "required": field["required"]}
                    for q in form["questions"]
                    for field in q["fields"]
                    if field["entry_id"] not in filled
                ],
                "browser_values_verified": False,
                "draft_saved_verified": False,
            }
        )
    if not plans:
        raise WorkflowError("这项作业没有原始表单。")
    record = {
        "status": "prefill_prepared",
        "forms": plans,
        "answers_sha256": sha256_file(answers_path),
        "submission": "manual_only",
        "created_at": utc_now(),
        "generation": {
            k: generation.get(k) for k in ("model", "reasoning_effort", "thread_id", "turn_id")
        },
    }
    path = package / "form-fill.json"
    try:
        with FileLock(str(package / "form-fill.lock"), timeout=0):
            return _open_plans(path, record, progress, opener)
    except Timeout as exc:
        raise WorkflowError("另一个本机填写正在处理此表单，请等待它结束。") from exc


def _open_plans(path, record, progress, opener):
    plans = record["forms"]
    # A completed run is not reopened automatically: preserve the user's subsequent edits.
    if path.is_file():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if previous.get("answers_sha256") == record["answers_sha256"]:
            old_forms = {x["original_url"]: x for x in previous.get("forms", [])}
            for plan in plans:
                old = old_forms.get(plan["original_url"], {})
                if old.get("url") != plan["url"]:
                    raise WorkflowError("预填记录的地址已经变化，不能复用未知链接。")
                plan["opened"] = old.get("opened") is True
            if previous.get("status") == "opened_native_prefill":
                return {**previous, "reused": True}
    atomic_json(path, record)
    open_url = opener or webbrowser.open
    for plan in plans:
        if plan.get("opened"):
            continue
        progress(ProgressUpdate("正在打开自动预填的原表单；提交按钮仅由你操作", "form_fill"))
        try:
            opened = open_url(plan["url"], new=2)
        except Exception:
            opened = False
        plan["opened"] = bool(opened)
    record["status"] = (
        "opened_native_prefill" if all(x["opened"] for x in plans) else "browser_open_failed"
    )
    atomic_json(path, record)
    if record["status"] == "browser_open_failed":
        raise WorkflowError("自动预填已准备，浏览器打开失败；请在结果中打开预填表单重试。")
    return record
