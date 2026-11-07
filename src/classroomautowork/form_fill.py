"""Populate responder fields using Google's native prefill, never submit responses.

This module has no response POST, submit action, browser credentials or form-edit API.
The browser opens a validated viewform GET with exact native entry IDs and values.
Opening is recorded separately from browser verification and Google's draft saving.
Submitting is always the student's own click in Google Forms, after checking every answer.

Every type Google can prefill is filled: text, choices (with the "Other" option), linear
scales and ratings, both grids (one entry per row), dates and times. A file upload cannot be
prefilled; it is listed for the student to upload in the form, as is a question of a type the
reader does not know ("unknown"), for the student to answer there.
"""

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from filelock import FileLock, Timeout

from .errors import PermissionDenied, WorkflowError
from .form_read import OTHER_CHOICE, form_references, form_url, read_form
from .local import atomic_json, require_private_path, sha256_file, utc_now
from .policy import CoursePolicy, confirmed_gate
from .progress import ProgressUpdate
from .school_forms import restricted_form, school_account
from .student import StudentProfile
from .user_answers import load_user_answers, merge_answers

SUPPORTED = {
    "short_text",
    "paragraph",
    "single_choice",
    "dropdown",
    "checkbox",
    "scale",
    "rating",
    "grid",
    "checkbox_grid",
    "date",
    "time",
}
# Keys form_read derives from what a field already holds (its choices, its constraints): left out
# of the digest, so answers prepared before the reader added them still fit the same form.
DERIVED_KEYS = {"other", "row", "format"}
# Types whose field takes several values: checkboxes, and a checkbox grid's row.
MULTIPLE = {"checkbox", "checkbox_grid"}
# Google's prefill names a chosen "Other" option so, with its text in a parameter of its own.
OTHER_VALUE = "__other_option__"
OTHER_SUFFIX = ".other_option_response"
# A date or time value in the field's format (form_read.DATE_FORMATS / TIME_FORMATS), checked as
# a real calendar date or clock time; a date without a year is checked against a leap year.
VALUE_FORMATS = {
    "YYYY-MM-DD": (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
    "MM-DD": (r"\d{2}-\d{2}", "%Y-%m-%d"),
    "YYYY-MM-DD HH:MM": (r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", "%Y-%m-%d %H:%M"),
    "MM-DD HH:MM": (r"\d{2}-\d{2} \d{2}:\d{2}", "%Y-%m-%d %H:%M"),
    "HH:MM": (r"\d{2}:\d{2}", "%H:%M"),
    "HH:MM:SS": (r"\d{2}:[0-5]\d:[0-5]\d", None),
}


def other_text(value: str):
    """The respondent's words of an "Other" answer ("[その他の自由記入]…"), else None."""
    if not value.startswith(OTHER_CHOICE):
        return None
    return value[len(OTHER_CHOICE) :].lstrip(" 　:：").strip()


def value_matches(value: str, fmt: str) -> bool:
    pattern, parse = VALUE_FORMATS.get(fmt, (None, None))
    if not pattern or not re.fullmatch(pattern, value):
        return False
    if parse is None:  # a duration: hours of any two digits, minutes and seconds below 60
        return True
    try:
        datetime.strptime(("2000-" if fmt.startswith("MM") else "") + value, parse)
    except ValueError:
        return False
    return True


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
                detail = {
                    "form_url": form["url"],
                    "item_id": question["item_id"],
                    "entry_id": field["entry_id"],
                    "title": question["title"],
                    "type": question["type"],
                    "required": field["required"],
                }
                if question["type"] not in SUPPORTED:
                    # Never prefilled, whatever the model wrote: the student does it in the form.
                    coverage["unsupported_fields"].append(detail)
                    continue
                if answer and answer.get("values"):
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


def _digest_question(question):
    return {
        **question,
        "fields": [
            {key: value for key, value in field.items() if key not in DERIVED_KEYS}
            for field in question["fields"]
        ],
    }


def context_digest(form):
    """What an answer was written for. Questions of an unknown type are not part of it (the
    reader used to drop them, and nothing is ever filled into them)."""
    return hashlib.sha256(
        json.dumps(
            {
                "url": form_url(form["url"]),
                "questions": sorted(
                    (_digest_question(q) for q in form["questions"] if q["type"] != "unknown"),
                    key=lambda q: q["item_id"],
                ),
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


def check_values(question: dict, field: dict, values: list[str]) -> None:
    """The values must be the field's own choices, its "Other" with words, or its date or time
    format; anything else is refused before a link is made."""
    title = question.get("title") or field["entry_id"]
    fmt = field.get("format")
    if question["type"] in {"date", "time"}:
        if not fmt or not value_matches(values[0], fmt):
            raise WorkflowError(
                f"表单答案的日期或时间格式不对，应为 {fmt or '原题的格式'}：{title}。未打开填写。"
            )
        return
    if not field["choices"]:
        return
    others = []
    for value in values:
        if value in field["choices"] and value != OTHER_CHOICE:
            continue
        words = other_text(value) if field.get("other") else None
        if words:
            others.append(words)
            continue
        if field.get("other") and value.startswith(OTHER_CHOICE):
            raise WorkflowError(
                f"表单答案选了“其他”却没有写内容：{title}。应写成“{OTHER_CHOICE}”加内容。"
            )
        raise WorkflowError("表单答案包含原题不存在的选项，未打开填写。")
    if len(others) > 1:
        raise WorkflowError(f"表单答案的“其他”只能有一项：{title}。")


def prefill_parameters(question: dict, field: dict, values: list[str]) -> list[tuple]:
    """viewform parameters for one field: entry.N per value (a grid row is its own entry); an
    "Other" answer is entry.N=__other_option__ with its words in entry.N.other_option_response;
    dates and times go as their checked format (YYYY-MM-DD, MM-DD, … HH:MM, HH:MM:SS)."""
    name = "entry." + field["entry_id"]
    params, words = [], None
    for value in values:
        text = other_text(value) if field.get("other") else None
        if text:
            params.append((name, OTHER_VALUE))
            words = text
        else:
            params.append((name, value))
    if words is not None:
        params.append((name + OTHER_SUFFIX, words))
    return params


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
        if question["type"] not in SUPPORTED:
            # A file upload or an unknown type is never prefilled; a value the model wrote for
            # it anyway is dropped (the field stays listed for the student) rather than
            # costing the whole answer set another turn.
            continue
        if (
            not isinstance(values, list)
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
            or (question["type"] not in MULTIPLE and len(values) != 1)
        ):
            raise WorkflowError("表单答案包含不支持的题型、空值或内部审核标记。")
        check_values(question, field, values)
        # Identity cannot be changed by course content or model suggestions.
        identity = {"学籍番号": "student_id", "学号": "student_id", "氏名": "name", "姓名": "name"}
        label = question["title"].strip()
        if profile and label in identity:
            expected = getattr(profile, identity[label])
            if not expected or values != [expected]:
                raise WorkflowError("表单身份答案与用户私人配置不一致。")
        validated.append(answer)
    return validated


# Google answers a longer request URL with 400 Bad Request (measured 2026-11-06: 8,197
# characters were served, 8,497 refused). A prefill link stays below this; free-text answers that
# do not fit are left out of the link (the longest first) and listed for the student to paste.
MAX_PREFILL_URL = 8000
FREE_TEXT = {"short_text", "paragraph"}


def prefill_url(form: dict, answers: list[dict], *, account="") -> str:
    """Only a viewform GET; repeated checkbox entry parameters select exact values."""
    values = validate_answers([form], answers)
    if not values:
        raise WorkflowError("没有实际可填的表单答案；不能把空链接称为已填写。")
    url = _prefill_link(form, values, account)
    if len(url) > MAX_PREFILL_URL:
        raise WorkflowError(
            "答案超过 Google 预填链接的长度上限（约 8000 字符），初稿已保留，未省略答案冒充完成。"
        )
    return url


def fit_prefill(form: dict, answers: list[dict], *, account="") -> tuple[str, list[dict]]:
    """The prefill link, and the free-text answers left out of it so that Google accepts it.

    The longest text answer goes first, then the next, until the link fits; a choice is never
    left out (a link that cannot fit its choices is refused). What is left out keeps its answer:
    the student pastes it into the form."""
    values = validate_answers([form], answers)
    if not values:
        raise WorkflowError("没有实际可填的表单答案；不能把空链接称为已填写。")
    types = {
        field["entry_id"]: question["type"]
        for question in form["questions"]
        for field in question["fields"]
    }
    kept, left_out = list(values), []
    url = _prefill_link(form, kept, account)
    while len(url) > MAX_PREFILL_URL:
        texts = [x for x in kept if types.get(x["entry_id"]) in FREE_TEXT]
        if not texts:
            raise WorkflowError(
                "选择题的答案就超过了 Google 预填链接的长度上限，没有生成链接；初稿已保留。"
            )
        longest = max(texts, key=lambda x: len(urlencode([("v", "\n".join(x["values"]))])))
        kept.remove(longest)
        left_out.append(longest)
        url = _prefill_link(form, kept, account)
    return url, left_out


def _prefill_link(form: dict, values: list[dict], account: str) -> str:
    params = [("usp", "pp_url"), ("srd", "true")]
    if account:
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", account):
            raise WorkflowError("表单账户无效。")
        params.append(("authuser", account))
        # Native collected-email fields (including the verified-email checkbox)
        # are populated by this responder parameter; it does not submit a response.
        params.append(("emailAddress", account))
    fields = {
        field["entry_id"]: (question, field)
        for question in form["questions"]
        for field in question["fields"]
    }
    for answer in values:
        if not re.fullmatch(r"\d{1,20}", answer["entry_id"]):
            raise WorkflowError("表单原生 entry ID 无效。")
        question, field = fields[answer["entry_id"]]
        params.extend(prefill_parameters(question, field, answer["values"]))
    return form_url(form["url"]) + "?" + urlencode(params)


# What a page turn answers a required question with (school_forms walks a form's pages to see
# its later pictures). These go to Google only as the page being turned, never as a response:
# the student's prefill link is made from the real answers later.
WALK_TEXT = "（翻页用）"
WALK_DATES = {
    "YYYY-MM-DD": "2000-01-01",
    "MM-DD": "01-01",
    "YYYY-MM-DD HH:MM": "2000-01-01 00:00",
    "MM-DD HH:MM": "01-01 00:00",
    "HH:MM": "00:00",
    "HH:MM:SS": "00:00:00",
}


def walk_link(form: dict, page: int, *, account="", profile=None) -> str | None:
    """A prefill link that lets Google's 次へ through every page before `page`: each required
    field there gets the student's own identity (学籍番号, 氏名, … from the profile), its first
    choice, a fixed date or time, or WALK_TEXT. None when one of them cannot be prefilled (a file
    upload, a question of an unknown type, a choice question without choices): 次へ would not
    pass it."""
    params = []
    for question in form["questions"]:
        if question["page"] >= page:
            continue
        for field in question["fields"]:
            if not field.get("required"):
                continue
            kind = question["type"]
            if kind not in SUPPORTED:
                return None
            if kind in {"date", "time"}:
                value = WALK_DATES.get(field.get("format") or "")
                if value is None:
                    return None
            elif kind in FREE_TEXT:
                key = identity_key(question["title"])
                value = (getattr(profile, key, "") if profile and key else "") or WALK_TEXT
            else:
                choices = [c for c in field.get("choices") or [] if c != OTHER_CHOICE]
                if not choices:
                    return None
                value = choices[0]
            params.extend(prefill_parameters(question, field, [value]))
    query = [("usp", "pp_url")]
    if account:
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", account):
            raise WorkflowError("表单账户无效。")
        query += [("authuser", account), ("emailAddress", account)]
    url = form_url(form["url"]) + "?" + urlencode(query + params)
    return url if len(url) <= MAX_PREFILL_URL else None


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
    from .drafting import reusable_review
    from .kb_client import KnowledgeClient

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
    allowed = current_response_urls(
        KnowledgeClient.from_settings(settings), manifest["course_id"], manifest["assignment_id"]
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
            # Login-only: re-read with the school-login browser (read mode) or a recent capture.
            fresh = restricted_form(settings, form["url"], exc, progress)
        if context_digest(fresh) != form["context_sha256"]:
            raise WorkflowError("原表单题目或选项已修改，请重新读题生成；未覆盖草稿。")
        selected = [x for x in answers if x["form_url"] == form["url"]]
        url, left_out = fit_prefill(form, selected, account=school_account(settings))
        filled = {x["entry_id"] for x in selected} - {x["entry_id"] for x in left_out}
        questions = {
            field["entry_id"]: question
            for question in form["questions"]
            for field in question["fields"]
        }
        plans.append(
            {
                "title": form["title"],
                "url": url,
                "original_url": form["url"],
                "prefilled_fields": len(filled),
                # Answers too long for the link: the page shows each with a copy button.
                "paste_fields": [
                    {
                        "entry_id": x["entry_id"],
                        "item_id": questions[x["entry_id"]]["item_id"],
                        "title": questions[x["entry_id"]]["title"],
                        "type": questions[x["entry_id"]]["type"],
                        "text": "\n".join(x["values"]),
                    }
                    for x in left_out
                ],
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
                # A link longer than Google accepts (made before the limit) is replaced.
                if old.get("url") != plan["url"] and len(old.get("url") or "") <= MAX_PREFILL_URL:
                    raise WorkflowError("预填记录的地址已经变化，不能复用未知链接。")
                plan["opened"] = old.get("opened") is True
            if previous.get("status") in {"opened_native_prefill", "prefill_ready"}:
                # Refresh coverage for older receipts too. Reusing the same GET
                # must not preserve a stale claim that all native fields exist.
                previous.update(
                    form_mapping_complete=record.get("form_mapping_complete", True), forms=plans
                )
                atomic_json(path, previous)
                return {**previous, "reused": True}
    if opener is None:
        # The assistant runs on a server: the user opens each prefilled link from the result.
        record["status"] = "prefill_ready"
        atomic_json(path, record)
        return record
    atomic_json(path, record)
    open_url = opener
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
