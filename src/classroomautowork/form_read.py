"""Read published Google Forms questions, including sections after the identity page.

Only GET of a fixed responder URL is exposed. No cookies, OAuth expansion, JavaScript
execution, response writes, submission endpoint, or arbitrary external URL fetching.
The published page's JSON layout is not a Google API contract: fail visibly if changed.
"""

import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import PermissionDenied, WorkflowError

MAX_PAGE_BYTES = 4 * 1024 * 1024
PARSER_VERSION = "published-form-v3-question-images"
FORM_PATH = re.compile(
    r"/forms/(?:u/\d+/)?d/(e/)?([A-Za-z0-9_-]{8,200})/(?:viewform|formResponse)/?"
)
TYPES = {
    0: "short_text",
    1: "paragraph",
    2: "single_choice",
    3: "dropdown",
    4: "checkbox",
    5: "scale",
    7: "grid",  # "checkbox_grid" when its rows allow several columns
    9: "date",
    10: "time",
    13: "file_upload",
    18: "rating",  # stars or hearts; read like a linear scale when the page data lists its values
}
# A question of a type this reader does not know is kept as "unknown" (title, entry IDs, whether
# it is required), so coverage lists it for the student to answer in the form instead of the
# question silently disappearing; the form then also says it was not completely read.
UNKNOWN = "unknown"
UNKNOWN_WARNING = "表单包含需目视核对的图像、视频或未知题型；不可声称已完整读取。"
# The label an "Other" option (its text is empty in the page data) is shown and answered by. An
# answer that picks it is this label followed by the respondent's own words (form_fill.py).
OTHER_CHOICE = "[その他の自由記入]"
# How a date or time field is answered, from the field's flags in the page data:
# date [include_time, include_year] at entry[7]; time [duration] at entry[6] (entry[7] in some
# documented layouts). These layouts are the commonly documented published-form format and were
# not checked against a live form; without flags a date has a year and no time, a time is a
# time of day.
DATE_FORMATS = {
    (False, True): "YYYY-MM-DD",
    (False, False): "MM-DD",
    (True, True): "YYYY-MM-DD HH:MM",
    (True, False): "MM-DD HH:MM",
}
TIME_FORMATS = {False: "HH:MM", True: "HH:MM:SS"}


def _flags(entry, *indexes):
    for index in indexes:
        if len(entry) > index and isinstance(entry[index], list):
            return [value == 1 or value is True for value in entry[index]]
    return []


def _field_details(kind, entry, row_number) -> dict:
    """What a grid row, a date, a time or a field with an "Other" option adds to a field.

    Fields of the other types get nothing, so their questions (and the answers checked
    against them) stay exactly as before."""
    if kind == 7:
        names = entry[3] if len(entry) > 3 else None
        label = names[0] if isinstance(names, list) and names else None
        return {"row": label if isinstance(label, str) and label else f"第 {row_number} 行"}
    if kind == 9:
        flags = _flags(entry, 7)
        time_flag = flags[0] if flags else False
        year = flags[1] if len(flags) > 1 else True
        return {"format": DATE_FORMATS[(time_flag, year)]}
    if kind == 10:
        flags = _flags(entry, 6, 7)
        return {"format": TIME_FORMATS[bool(flags and flags[0])]}
    return {}


def form_url(value: str) -> str:
    """Canonicalization strips prefilled answers and maps formResponse to GET viewform."""
    parsed = urlparse(value)
    match = FORM_PATH.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "docs.google.com"
        or parsed.netloc != "docs.google.com"
        or not match
    ):
        raise WorkflowError("只能只读获取标准 Google Forms 作答链接。")
    return f"https://docs.google.com/forms/d/{match[1] or ''}{match[2]}/viewform"


def form_references(item: dict) -> list[str]:
    values = []
    for material in item.get("materials", []):
        values.extend(
            [material.get("form", {}).get("formUrl", ""), material.get("link", {}).get("url", "")]
        )
    for attachment in item.get("assignmentSubmission", {}).get("attachments", []):
        values.append(attachment.get("link", {}).get("url", ""))
    for field in ("description", "text"):
        values.extend(
            re.findall(r"https://docs\.google\.com/forms/[^\s<>\"）)]+", item.get(field, ""))
        )
    result = []
    for value in values:
        try:
            url = form_url(value)
        except WorkflowError:
            continue
        if url not in result:
            result.append(url)
    return result


class FormClosed(PermissionDenied):
    """The form no longer accepts answers (Google sends its viewform to …/closedform)."""

    reason = "form_closed"


FORM_PAGE = re.compile(r"/forms/(?:u/\d+/)?d/(e/)?([A-Za-z0-9_-]{8,200})/([A-Za-z]{1,40})/?")


def other_form_page(url: str, form: str) -> str:
    """The page name when `url` is another page of the same form (closedform, …), else ''."""
    try:
        parsed, own = urlparse(url), FORM_PATH.fullmatch(urlparse(form).path)
    except ValueError:
        return ""
    match = FORM_PAGE.fullmatch(parsed.path)
    if parsed.hostname != "docs.google.com" or not match or not own:
        return ""
    return match[3] if (match[1], match[2]) == (own[1], own[2]) else ""


class _ResponderRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        try:
            target = form_url(newurl)
        except WorkflowError as exc:
            # The same form's own other pages are no login wall: say what they are, so a closed
            # form is not reported as needing the school login.
            page = other_form_page(newurl, request.full_url)
            if page == "closedform":
                raise FormClosed("表单已关闭，不再接受回答；没有读取题目。") from exc
            if page:
                raise PermissionDenied(
                    f"表单跳转到了它的 {page} 页面，不是题目页面；没有读取题目。"
                ) from exc
            # Google's sign-in, or anywhere else a login-only form may send an anonymous reader.
            raise PermissionDenied(
                "表单要求浏览器登录或重定向到非题目页面；未读取题目，也未扩大授权。"
            ) from exc
        if target != form_url(request.full_url):
            raise PermissionDenied("表单重定向到另一份表单，已停止只读获取。")
        return super().redirect_request(request, fp, code, msg, headers, target)


def parse_form(html: str, url: str) -> dict:
    canonical = form_url(url)
    match = re.search(r"\bFB_PUBLIC_LOAD_DATA_\s*=\s*(\[)", html)
    if not match:
        raise PermissionDenied("未读到表单题目数据：可能需要学校登录、已关闭或页面格式已变化。")
    try:
        payload, _ = json.JSONDecoder().raw_decode(html[match.start(1) :])
        body = payload[1]
        title, description, items = body[8], body[0], body[1]
        if not isinstance(title, str) or not isinstance(items, list) or len(items) > 1000:
            raise ValueError
        sections, questions, media, warnings, page = [], [], [], [], 1
        for item in items:
            item_id, label, help_text, kind, entries = item[:5]
            if not isinstance(item_id, int) or not isinstance(kind, int):
                raise ValueError
            if kind == 8:
                page += 1
            if kind in {6, 8}:
                sections.append(
                    {"page": page, "title": label or "", "description": help_text or ""}
                )
                continue
            if kind == 11:
                # Native image items carry an opaque media ID and dimensions,
                # not a public download URL. The authorized browser supplies
                # pixels separately; never guess a URL from this identifier.
                descriptor = item[6] if len(item) > 6 else None
                if not _media_descriptor(descriptor):
                    warnings.append(IMAGE_WARNING)
                    continue
                picture = _picture(item_id, page, label, help_text, "image", descriptor)
                media.append(picture)
                warnings.append(image_gap_warning(picture))
                continue
            # A picture added to a question itself (the question's image button) sits at index 9
            # of the question: [[media ID, None, [width, height, alignment]]]. It is a material of
            # that question (one per question), recorded like an image item under the question's
            # own ID, so the rendered picture inside that question pairs with it.
            attached = item[9] if len(item) > 9 else None
            if attached is not None:
                if (
                    isinstance(attached, list)
                    and len(attached) == 1
                    and _media_descriptor(attached[0])
                ):
                    picture = _picture(
                        item_id, page, label, help_text, "question_image", attached[0]
                    )
                    media.append(picture)
                    warnings.append(image_gap_warning(picture))
                else:
                    warnings.append(IMAGE_WARNING)
            if kind not in TYPES:
                warnings.append(UNKNOWN_WARNING)
                unknown = _unknown_question(item_id, page, label, help_text, entries)
                if unknown:
                    questions.append(unknown)
                continue
            if kind == 18:
                # The rating layout was not checked against a live form: read it like a scale
                # when it lists its values, else keep it for the student to answer in the form.
                try:
                    fields, _ = _fields(kind, entries) if isinstance(label, str) else ([], False)
                except (ValueError, TypeError, IndexError):
                    fields = []
                if not fields or not all(field["choices"] for field in fields):
                    warnings.append(UNKNOWN_WARNING)
                    unknown = _unknown_question(item_id, page, label, help_text, entries)
                    if unknown:
                        questions.append(unknown)
                    continue
                kind_name = TYPES[kind]
            else:
                if not isinstance(label, str) or not isinstance(entries, list) or not entries:
                    raise ValueError
                fields, several = _fields(kind, entries)
                kind_name = "checkbox_grid" if several else TYPES[kind]
            # File upload questions are read like any other; only the upload itself stays the
            # student's (form_fill lists them as 需要你在表单里自己上传), so it is no reading gap.
            questions.append(
                {
                    "item_id": str(item_id),
                    "page": page,
                    "title": label,
                    "description": help_text or "",
                    "type": kind_name,
                    "fields": fields,
                }
            )
        if not questions:
            raise ValueError
        return {
            "title": title,
            "description": description or "",
            "url": canonical,
            "page_count": page,
            "questions": questions,
            "sections": sections,
            "media": media,
            "warnings": list(dict.fromkeys(warnings)),
            "read_complete": not warnings,
            "parser": PARSER_VERSION,
        }
    except (ValueError, TypeError, IndexError, KeyError, RecursionError) as exc:
        raise WorkflowError(
            "Google Forms 题目结构无法可靠识别，已停止；不能以链接或模板冒充题目。"
        ) from exc


def branches(html: str) -> bool:
    """Whether the form may leave its page order: a choice that goes to another section
    (option[2] set) or a section that names where it goes next (item[5] set). True also when the
    page data cannot be read. Only a form that goes page by page is walked (school_forms)."""
    match = re.search(r"\bFB_PUBLIC_LOAD_DATA_\s*=\s*(\[)", html)
    if not match:
        return True
    try:
        payload, _ = json.JSONDecoder().raw_decode(html[match.start(1) :])
        items = payload[1][1]
        for item in items:
            if item[3] == 8:
                if len(item) > 5 and item[5] is not None:
                    return True
                continue
            for entry in item[4] if len(item) > 4 and isinstance(item[4], list) else []:
                options = entry[1] if isinstance(entry, list) and len(entry) > 1 else None
                for option in options if isinstance(options, list) else []:
                    if isinstance(option, list) and len(option) > 2 and option[2] is not None:
                        return True
    except (ValueError, TypeError, IndexError, KeyError, RecursionError):
        return True
    return False


def _fields(kind, entries):
    """The answer fields of one question (a grid: one per row), and whether it is a checkbox
    grid. Raises ValueError for page data of an unexpected shape."""
    fields = []
    several = False
    for row_number, entry in enumerate(entries, 1):
        if not isinstance(entry, list) or len(entry) < 3 or not isinstance(entry[0], int):
            raise ValueError
        choices = []
        for option in entry[1] or []:
            if not isinstance(option, list) or not isinstance(option[0], str):
                raise ValueError
            # "Other" is the option with no text and, where the page data says so, the is-other
            # flag (option[4]); a blank option without that flag stays blank.
            other = not option[0] and (len(option) < 5 or option[4] in (1, True))
            choices.append(OTHER_CHOICE if other else option[0])
        # Google can shuffle displayed choices per visit. Values, not positions,
        # identify choices; random order must not regenerate unchanged answers.
        if kind in {2, 3, 4}:
            choices = sorted(choices)
        field = {
            "entry_id": str(entry[0]),
            "choices": choices,
            "required": bool(entry[2]),
            "constraints": entry[3:8],
            **_field_details(kind, entry, row_number),
        }
        if kind in {2, 4} and OTHER_CHOICE in choices:
            field["other"] = True
        # A checkbox grid: each row may take several columns (entry[11] = [1]).
        several = several or (kind == 7 and bool(_flags(entry, 11)[:1] == [True]))
        fields.append(field)
    return fields, several


def _unknown_question(item_id, page, label, help_text, entries):
    """A question of an unknown type, when its page data still names its answer fields."""
    if not isinstance(label, str) or not isinstance(entries, list) or not entries:
        return None
    fields = []
    for entry in entries:
        if not isinstance(entry, list) or len(entry) < 3 or type(entry[0]) is not int:
            return None
        fields.append({"entry_id": str(entry[0]), "choices": [], "required": bool(entry[2])})
    return {
        "item_id": str(item_id),
        "page": page,
        "title": label,
        "description": help_text or "",
        "type": UNKNOWN,
        "fields": fields,
    }


IMAGE_WARNING = "表单包含无法识别的图像，需在原页面核对。"


def _media_descriptor(descriptor) -> bool:
    """[media ID, …, [width, height, …]]: an opaque ID and a positive size."""
    return (
        isinstance(descriptor, list)
        and len(descriptor) >= 3
        and isinstance(descriptor[0], str)
        and re.fullmatch(r"[A-Za-z0-9_-]{8,200}", descriptor[0]) is not None
        and isinstance(descriptor[2], list)
        and len(descriptor[2]) >= 2
        and all(type(value) is int and value > 0 for value in descriptor[2][:2])
    )


def _picture(item_id, page, label, help_text, kind, descriptor) -> dict:
    return {
        "item_id": str(item_id),
        "page": page,
        "title": label or "",
        "description": help_text or "",
        "type": kind,
        "media_id": descriptor[0],
        "width": descriptor[2][0],
        "height": descriptor[2][1],
    }


def image_gap_warning(picture: dict) -> str:
    return (
        f"Google Forms 第 {picture['page']} 页图片材料尚未读取："
        f"{picture['title'] or picture['item_id']}（item {picture['item_id']}）。"
    )


def read_form(url: str) -> dict:
    canonical = form_url(url)
    opener = build_opener(_ResponderRedirects())
    for attempt in range(4):
        try:
            request = Request(
                canonical,
                headers={"User-Agent": "ClassroomAutowork/0.1 (read-only questions)"},
                method="GET",
            )
            with opener.open(request, timeout=20) as response:
                if form_url(response.url) != canonical:
                    raise PermissionDenied("只读请求没有返回原表单。")
                raw = response.read(MAX_PAGE_BYTES + 1)
                if len(raw) > MAX_PAGE_BYTES:
                    raise WorkflowError("表单页面超过安全读取大小上限。")
            return parse_form(raw.decode("utf-8"), canonical)
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504}:
                raise PermissionDenied(
                    f"表单只读获取失败（HTTP {exc.code}）；请检查原表单的学校账户访问权限。"
                ) from exc
        except (URLError, TimeoutError, OSError, UnicodeError):
            pass
        if attempt < 3:
            time.sleep(min(2**attempt, 4))
    raise WorkflowError("Google Forms 只读请求重试后仍失败；题目尚未读取，请稍后重试。")


def form_chunks(form: dict) -> list[dict]:
    base = {"source_kind": "form", "title": form["title"], "url": form["url"]}
    chunks = [
        {
            **base,
            "locator": "Google Forms title and section requirements",
            "text": json.dumps(
                {
                    **{
                        key: form[key]
                        for key in ("title", "description", "sections", "read_complete", "warnings")
                    },
                    "media": [
                        {k: v for k, v in picture.items() if k not in {"image_path", "sha256"}}
                        for picture in form.get("media", [])
                    ],
                },
                ensure_ascii=False,
            ),
        }
    ]
    for question in form["questions"]:
        chunks.append(
            {
                **base,
                "locator": f"Google Forms page {question['page']} item {question['item_id']}",
                "text": json.dumps(question, ensure_ascii=False),
            }
        )
    for picture in form.get("media", []):
        chunks.append(
            {
                **base,
                "locator": f"Google Forms page {picture['page']} image {picture['item_id']}",
                "text": json.dumps(
                    {k: v for k, v in picture.items() if k != "image_path"}, ensure_ascii=False
                ),
                **({"image_path": picture["image_path"]} if picture.get("image_path") else {}),
            }
        )
    return chunks
