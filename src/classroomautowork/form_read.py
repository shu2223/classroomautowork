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
PARSER_VERSION = "published-form-v1"
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
    7: "grid",
    9: "date",
    10: "time",
    13: "file_upload",
}


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


class _ResponderRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        try:
            target = form_url(newurl)
        except WorkflowError as exc:
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
        sections, questions, warnings, page = [], [], [], 1
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
            if kind not in TYPES:
                warnings.append("表单包含需目视核对的图像、视频或未知题型；不可声称已完整读取。")
                continue
            if not isinstance(label, str) or not isinstance(entries, list) or not entries:
                raise ValueError
            fields = []
            for entry in entries:
                if not isinstance(entry, list) or len(entry) < 3 or not isinstance(entry[0], int):
                    raise ValueError
                choices = []
                for option in entry[1] or []:
                    if not isinstance(option, list) or not isinstance(option[0], str):
                        raise ValueError
                    choices.append(option[0] or "[その他の自由記入]")
                # Google can shuffle displayed choices per visit. Values, not positions,
                # identify choices; random order must not regenerate unchanged answers.
                if kind in {2, 3, 4}:
                    choices = sorted(choices)
                fields.append(
                    {
                        "entry_id": str(entry[0]),
                        "choices": choices,
                        "required": bool(entry[2]),
                        "constraints": entry[3:8],
                    }
                )
            if kind in {7, 13}:
                warnings.append("表单含网格或文件上传题，需用户核对原页面；不会自动作答或上传。")
            questions.append(
                {
                    "item_id": str(item_id),
                    "page": page,
                    "title": label,
                    "description": help_text or "",
                    "type": TYPES[kind],
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
            "warnings": list(dict.fromkeys(warnings)),
            "read_complete": not warnings,
            "parser": PARSER_VERSION,
        }
    except (ValueError, TypeError, IndexError, KeyError, RecursionError) as exc:
        raise WorkflowError(
            "Google Forms 题目结构无法可靠识别，已停止；不能以链接或模板冒充题目。"
        ) from exc


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
                    key: form[key]
                    for key in ("title", "description", "sections", "read_complete", "warnings")
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
    return chunks
