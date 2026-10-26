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

from .errors import PermissionDenied, WorkflowError
from .form_capture import authenticated_form_cache
from .form_read import form_references, form_url, read_form
from .local import atomic_json, require_private_path, sha256_file, utc_now
from .policy import CoursePolicy, confirmed_gate
from .progress import ProgressUpdate
from .student import StudentProfile
from .user_answers import load_user_answers, merge_answers

SUPPORTED = {"short_text", "paragraph", "single_choice", "dropdown", "checkbox", "scale"}
IDENTITY = {
    "学籍番号": "student_id",
    "学号": "student_id",
    "studentid": "student_id",
    "氏名": "name",
    "姓名": "name",
    "名前": "name",
    "name": "name",
    "学科": "department",
    "学部学科": "department",
    "所属学科": "department",
    "クラス": "class_name",
    "班级": "class_name",
    "学年": "year",
    "年级": "year",
}


def identity_key(title):
    label = re.sub(r"^\s*(?:[0-9０-９]+\s*[.)）:：、]|[①-⑳])\s*", "", title)
    return IDENTITY.get(re.sub(r"[\s:：*＊]", "", label).lower())


def form_gap_reason(title, review_note=""):
    """A review flag alone does not establish that a blank needs personal facts.

    Older model outputs use ``needs_user`` for both missing sources and personal
    facts. Keep those outputs intact, but interpret their explanation when deciding
    whether the next action is answer repair or asking for the student's own data.
    """
    if identity_key(title):
        return "personal_fact"
    note = review_note or ""
    personal = (
        r"(?:本人|用户|使用者|學生|学生|自分|あなた).{0,20}"
        r"(?:事実|事实|个人|個人|经历|経歴|経験|健康|体重|身長|身高|出席|学籍|学号|記録|记录)|"
        r"(?:実際|真实|实际).{0,12}(?:個人|个人|健康|体重|身長|身高|出席|記録|记录)|"
        r"(?:personal|your\s+own|student['’]s\s+own).{0,20}(?:fact|data|experience|record|health)|"
        r"(?:actual|real).{0,15}(?:attendance|weight|height|health|personal)"
    )
    if re.search(personal, title, re.I):
        return "personal_fact"
    source = (
        r"原文|本文|文章|阅读|聴解|听力|音声|音频|音訊|录音|録音|教材|课文|課文|图片|图像|画像|"
        r"トランスクリプト|スクリプト|逐字稿|转录|字幕|対話|对话|会话|會話|会話|"
        r"audio|recording|transcript|passage|source\s+material|listening|reading\s+text|"
        r"conversation|dialogue|image|picture"
    )
    unavailable = (
        r"缺少|缺失|未提供|未读取|未读到|未取得|未获取|未包含|未能|未下载|未转录|"
        r"无法读取|无法获取|不可读|不可用|不足|未入包|没有|不在|未収録|未読|"
        r"含まれ|確認でき|不明|ありません|ない|missing|unavailable|"
        r"not\s+(?:provided|available|included|read|include)|cannot\s+(?:read|access)"
    )
    if re.search(source, note, re.I) and re.search(unavailable, note, re.I):
        return "missing_source"
    if re.search(personal, note, re.I):
        return "personal_fact"
    if re.search(
        r"资料|資料|素材|(?:女子|男子|女人|男人|文中).{0,40}(?:评价|态度|意见|打算|想法|计划)",
        note,
    ) and re.search(unavailable, note, re.I):
        return "missing_source"
    return "missing_answer"


def form_answer_coverage(forms, answers, profile=None):
    """Account for every required native field, including deliberate personal blanks.

    Validation of supplied rows alone cannot detect omitted questions. Optional
    feedback remains optional; unsupported controls and unknown personal facts are
    exposed separately so an automatic repair never has to invent those values.
    """
    supplied = {(a["form_url"], a["entry_id"]): a for a in answers}
    coverage = {
        "missing_fields": [],
        "user_input_fields": [],
        "optional_empty_fields": [],
        "unsupported_fields": [],
    }
    for form in forms:
        for question in form["questions"]:
            for field in question["fields"]:
                answer = supplied.get((form["url"], field["entry_id"]))
                if answer and answer.get("values"):
                    continue
                detail = {
                    "form_url": form["url"],
                    "item_id": question["item_id"],
                    "entry_id": field["entry_id"],
                    "title": question["title"],
                    "type": question["type"],
                    "required": field["required"],
                }
                if question["type"] not in SUPPORTED:
                    coverage["unsupported_fields"].append(detail)
                    continue
                personal_key = identity_key(question["title"])
                unknown_identity = (
                    personal_key and profile is not None and not getattr(profile, personal_key, "")
                )
                reason = form_gap_reason(question["title"], (answer or {}).get("review_note", ""))
                if unknown_identity or (
                    answer and answer.get("needs_user") and reason == "personal_fact"
                ):
                    coverage["user_input_fields"].append(
                        {
                            **detail,
                            "gap_reason": "personal_fact",
                            "review_note": (answer or {}).get("review_note")
                            or "尚无用户提供的真实个人资料，请本人补充。",
                        }
                    )
                elif field["required"]:
                    coverage["missing_fields"].append(
                        {
                            **detail,
                            "gap_reason": reason if reason != "personal_fact" else "missing_answer",
                            "review_note": (answer or {}).get("review_note", ""),
                        }
                    )
                else:
                    coverage["optional_empty_fields"].append(detail)
    return coverage


def context_digest(form):
    return hashlib.sha256(
        json.dumps(
            {
                "url": form_url(form["url"]),
                "questions": sorted(form["questions"], key=lambda q: q["item_id"]),
                "sections": form["sections"],
                "description": form["description"],
                "title": form["title"],
                **(
                    {
                        "media": sorted(
                            [
                                {
                                    key: value
                                    for key, value in picture.items()
                                    if key not in {"image_path", "sha256"}
                                }
                                for picture in form["media"]
                            ],
                            key=lambda picture: picture["item_id"],
                        )
                    }
                    if form.get("media")
                    else {}
                ),
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
        if values == [] and (not field["required"] or answer.get("needs_user") is True):
            # Missing personal facts remain blank and appear in the software's question
            # editor. They must not invalidate other usable answers or invent a value.
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
    answer_record = json.loads(answers_path.read_text(encoding="utf-8"))
    supplied = answer_record["answers"] + answer_record.get("deferred_answers", [])
    profile = StudentProfile.load(settings.data_dir)
    answers = validate_answers(forms, supplied, profile)
    user_record = load_user_answers(
        settings.data_dir, manifest, forms, StudentProfile.load(settings.data_dir)
    )
    answers = merge_answers(answers, user_record["answers"])
    coverage = form_answer_coverage(forms, merge_answers(supplied, user_record["answers"]), profile)
    plans = []
    for form in forms:
        progress(ProgressUpdate("正在核对原表单题目与自动填写的栏位", "form_fill"))
        try:
            fresh = read_form(form["url"])
        except PermissionDenied as exc:
            fresh = authenticated_form_cache(
                settings.data_dir, settings.school_email, form["url"], exc
            )
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
                "needs_user": [x["review_note"] for x in selected if x.get("needs_user")]
                + [
                    x["review_note"]
                    for x in coverage["user_input_fields"]
                    if x["form_url"] == form["url"]
                ],
                "remaining_fields": [
                    {
                        "entry_id": field["entry_id"],
                        "item_id": q["item_id"],
                        "title": q["title"],
                        "type": q["type"],
                        "required": field["required"],
                    }
                    for q in form["questions"]
                    for field in q["fields"]
                    if field["entry_id"] not in filled
                ],
                "remaining_answer_fields": [
                    x for x in coverage["missing_fields"] if x["form_url"] == form["url"]
                ],
                **{
                    key: [x for x in coverage[key] if x["form_url"] == form["url"]]
                    for key in ("user_input_fields", "optional_empty_fields", "unsupported_fields")
                },
                "browser_values_verified": False,
                "question_verification": "fresh_anonymous_read_or_browser_capture_within_24h",
                "draft_saved_verified": False,
            }
        )
    if not plans:
        raise WorkflowError("这项作业没有原始表单。")
    record = {
        "status": "prefill_prepared",
        "forms": plans,
        "form_mapping_complete": not coverage["missing_fields"],
        "answers_sha256": hashlib.sha256(
            (sha256_file(answers_path) + user_record["sha256"]).encode()
        ).hexdigest()
        if user_record["answers"]
        else sha256_file(answers_path),
        "user_answers_sha256": user_record["sha256"],
        "user_answer_count": len(user_record["answers"]),
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
                # Refresh coverage for older receipts too. Reusing the same GET
                # must not preserve a stale claim that all native fields exist.
                previous.update(
                    form_mapping_complete=record.get("form_mapping_complete", True), forms=plans
                )
                atomic_json(path, previous)
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
