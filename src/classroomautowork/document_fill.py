"""Fill existing personal submission Docs. No Classroom mutation methods exist here.

The pure planner accepts native Docs or connector reads. The API adapter is separate;
only this module constructs insertText requests, with revision guards and readback.
"""

import hashlib
import json
import re
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path

import httplib2
from filelock import FileLock, Timeout
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

from .document_slots import (
    explicit_answer_slots,
    identity_slots,
    inline_slots,
    paragraph_options,
    protected_inline_runs,
    slot_template,
    utf16_length,
)
from .errors import PermissionDenied, WorkflowError
from .google_read import retry_read
from .local import atomic_json, require_private_path, sha256_file, utc_now
from .student import StudentProfile

HEADERS = re.compile(r"^(?:[〇○◯×/\s]+|判断の理由|理由|回答|答え|解答|answer|reason)$", re.I)
PERSONAL = re.compile(r"学籍|氏.?名|姓名|名前|student.?id|\bname\b", re.I)
SECTION = re.compile(r"^(?:[■□●]\s*)?(?:設問|課題|問)\s*[①-⑳0-9０-９一二三四五六七八九十]")
WRITTEN_PROMPT = re.compile(r"(?:書|挙げ|答|考え|記入|説明|述べ|定義).*?(?:ください|なさい|字程度)")
REASON_PROMPT = re.compile(r"^(?:その|判断の)?理由(?:は)?[?？:：。]?$")


def required_answer_fields(form, *, answers=None):
    """Return academic fields required by the currently selected alternatives.

    Individual checkbox alternatives are optional; their reason only becomes required
    when that alternative is selected. Identity uses the separate trusted profile flow.
    """
    selected = {
        field["id"]
        for field in form.get("fields", [])
        if field.get("kind") == "choice" and field.get("current")
    }
    for answer in answers or []:
        if isinstance(answer, dict) and str(answer.get("text", "")).strip() == "☑":
            selected.add(answer.get("field_id"))
    return [
        field
        for field in form.get("fields", [])
        if not field.get("identity_key")
        and field.get("kind") != "choice"
        and (field.get("kind") != "reason" or field["id"].rsplit("/", 1)[0] + "/choice" in selected)
    ]


def missing_answer_fields(form, answers):
    """Check complete, unique answer coverage before any document write is offered."""
    if not isinstance(answers, list) or not all(isinstance(answer, dict) for answer in answers):
        raise WorkflowError("答案栏映射无效，未修改文档。")
    fields = {field["id"]: field for field in form.get("fields", [])}
    supplied = set()
    for answer in answers:
        key = answer.get("field_id")
        if key in supplied or key not in fields:
            raise WorkflowError("答案栏映射无效或重复，未修改文档。")
        if answer.get("context_sha256") != fields[key]["context_sha256"]:
            raise WorkflowError("原题或表头已经变化，请重新读题；未修改文档。")
        if not isinstance(answer.get("text"), str) or not answer["text"].strip():
            raise WorkflowError("答案栏包含空答案，未修改文档。")
        supplied.add(key)
    return [
        {key: field[key] for key in ("id", "label", "context")}
        for field in required_answer_fields(form, answers=answers)
        if not field.get("current") and field["id"] not in supplied
    ]


@contextmanager
def document_lock(path):
    try:
        with FileLock(str(path), timeout=1):
            yield
    except Timeout as exc:
        raise WorkflowError("另一项处理正在填入这份文档，请稍后重试；没有重复写入。") from exc


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def document_tabs(document):
    """Preserve tab IDs and tree; accept the connector's flattened tab representation."""
    result = []

    def visit(tabs, parent=None):
        for tab in tabs:
            props = tab.get("tabProperties", tab)
            result.append(
                {
                    "id": props["tabId"],
                    "title": props.get("title", ""),
                    "parent": props.get("parentTabId", parent),
                    "index": props.get("index", 0),
                    "content": tab.get("documentTab", tab).get("body", {}).get("content", []),
                }
            )
            visit(tab.get("childTabs", []), props["tabId"])

    visit(document.get("tabs", []))
    if not result:
        raise WorkflowError("未读到 Google 文档的全部原生分页；不能猜测填写位置。")
    return result


def paragraph_text(element):
    return "".join(
        x.get("textRun", {}).get("content", "")
        for x in element.get("paragraph", {}).get("elements", [])
    )


def plain_container(content):
    """Reject images, controls, suggestions and nested structures in editable containers."""
    for p in content:
        if "paragraph" not in p or any(k.startswith("suggested") for k in p):
            return False
        for e in p["paragraph"].get("elements", []):
            if "textRun" not in e or any(k.startswith("suggested") for k in e):
                return False
            if re.search(r"[\ue000-\uf8ff]", e["textRun"].get("content", "")):
                return False
    return bool(content)


def inspect_form(document, *, include_identity=False):
    """Identify bounded containers; identity slots are only exposed to the trusted filler.

    Stable field IDs use tab/table/row/column, not shifting character offsets. Context
    hashes cover questions/headers, so regenerated answers cannot target changed questions.
    """
    fields, topology, instructions, unrecognized = [], [], [], []
    tabs = document_tabs(document)
    for tab in tabs:
        topology.append({k: tab[k] for k in ("id", "title", "parent", "index")})
        table_number = 0
        body = tab["content"]
        paragraphs = [p for p in body if "paragraph" in p]
        if (
            len(tabs) == 1
            and len(paragraphs) == 1
            and all("sectionBreak" in p or plain_container([p]) for p in body)
        ):
            # A newly-created Doc is one real blank paragraph, not an unknown template.
            # Retain the field after insertion for idempotent readback. Nonempty content
            # is still protected by plan_fill and cannot be overwritten.
            context = {"document_title": document.get("title", ""), "tab_title": tab["title"]}
            fields.append(
                {
                    "id": f"{tab['id']}/body-answer",
                    "tab_id": tab["id"],
                    "label": "回答（正文一段）",
                    "context": json.dumps(context, ensure_ascii=False),
                    "context_sha256": digest(context),
                    "index": paragraphs[0]["startIndex"],
                    "current": paragraph_text(paragraphs[0]).strip(),
                }
            )
            continue
        section, academic_section, question_context, option_context = "", "", "", ""
        prompt_counts = {}
        section_instructions = []
        section_number = -1
        answer_paragraphs = set()
        for i, element in enumerate(body):
            if i in answer_paragraphs:
                continue
            if "table" in element:
                rows = element["table"].get("tableRows", [])
                if not rows:
                    continue
                headers = [
                    "".join(paragraph_text(p) for p in c.get("content", [])).strip()
                    for c in rows[0].get("tableCells", [])
                ]
                for ri, row in enumerate(rows[1:], 1):
                    cells = row.get("tableCells", [])
                    texts = [
                        "".join(paragraph_text(p) for p in c.get("content", [])).strip()
                        for c in cells
                    ]
                    context = " | ".join(
                        text
                        for ci, text in enumerate(texts)
                        if ci < len(headers) and not HEADERS.fullmatch(headers[ci])
                    )
                    if not context or PERSONAL.search(context):
                        continue
                    for ci, cell in enumerate(cells):
                        content = cell.get("content", [])
                        if (
                            ci >= len(headers)
                            or not HEADERS.fullmatch(headers[ci])
                            or not plain_container(content)
                        ):
                            continue
                        fields.append(
                            {
                                "id": f"{tab['id']}/table:{table_number}/row:{ri}/cell:{ci}",
                                "tab_id": tab["id"],
                                "label": headers[ci],
                                "context": context,
                                "context_sha256": digest([headers, context]),
                                "index": content[0]["startIndex"],
                                "current": texts[ci],
                            }
                        )
                table_number += 1
            elif "paragraph" in element:
                label = paragraph_text(element).strip()
                instructions.append(label)
                # Native list numbering can belong to questions as well as options.
                # Keep ordinary list items protected, while recognizing explicit
                # question prompts whose following paragraph is the answer slot.
                if "bullet" in element["paragraph"] and not (
                    SECTION.match(label)
                    or WRITTEN_PROMPT.search(label)
                    or REASON_PROMPT.fullmatch(label)
                    or re.search(r"[?？]$", label)
                ):
                    if label:
                        section_instructions.append(label)
                    continue
                if re.match(r"^(?:課題|問)[①-⑳0-9０-９]", label):
                    section = label
                if SECTION.match(label):
                    academic_section, question_context, option_context = label, "", ""
                    section_instructions = []
                    section_number += 1
                slots = inline_slots(paragraph_text(element)) if plain_container([element]) else []
                labeled_slots = (
                    explicit_answer_slots(paragraph_text(element))
                    if plain_container([element])
                    else []
                )
                slots += labeled_slots
                if include_identity and plain_container([element]):
                    slots += identity_slots(paragraph_text(element))
                if slots:
                    text = paragraph_text(element)
                    context = (
                        [*section_instructions, slot_template(text, slots)]
                        if labeled_slots
                        else [section, slot_template(text, slots)]
                    )
                    for slot in slots:
                        start, end = slot["span"]
                        field_id = f"{tab['id']}/paragraph:{i}/{slot['kind']}"
                        if labeled_slots:
                            count_key = (academic_section, slot["label"])
                            occurrence = prompt_counts.get(count_key, 0)
                            prompt_counts[count_key] = occurrence + 1
                            field_id = (
                                f"{tab['id']}/labeled:"
                                + digest([academic_section, slot["label"], occurrence])[:16]
                            )
                        fields.append(
                            {
                                **slot,
                                "id": field_id,
                                "tab_id": tab["id"],
                                **(
                                    {
                                        "section": academic_section,
                                        "section_id": f"{tab['id']}/section:{section_number}",
                                    }
                                    if labeled_slots
                                    else {}
                                ),
                                "context": "\n".join(context),
                                "context_sha256": digest(context),
                                "index": element["startIndex"] + utf16_length(text[:start]),
                                "end_index": element["startIndex"] + utf16_length(text[:end]),
                                "paragraph_index": element["startIndex"],
                            }
                        )
                    continue
                if label:
                    section_instructions.append(label)
                written_prompt = bool(WRITTEN_PROMPT.search(label))
                legacy_prompt = (
                    label.startswith("■")
                    or element["paragraph"]
                    .get("paragraphStyle", {})
                    .get("namedStyleType", "")
                    .startswith("HEADING")
                    or re.search(r"(?:回答|答え|解答|まとめ|考察|answer)\s*[:：]$", label, re.I)
                    or written_prompt
                )
                options = paragraph_options(label)
                reason_prompt = bool(REASON_PROMPT.fullmatch(label))
                question_prompt = bool(re.search(r"[?？]$", label))
                new_prompt = bool(options or reason_prompt or question_prompt)
                if not label or not (legacy_prompt or new_prompt) or PERSONAL.search(label):
                    continue
                if question_prompt and not reason_prompt and not SECTION.match(label):
                    question_context, option_context = label, ""
                if options:
                    option_context = label
                # A scenario followed by spacing and an explicit list of alternatives
                # uses the answer below that list. Do not invent a second choice slot.
                next_text = ""
                for next_element in body[i + 1 :]:
                    if "paragraph" not in next_element:
                        break
                    next_text = paragraph_text(next_element).strip()
                    if next_text:
                        break
                if not options and paragraph_options(next_text):
                    continue
                context = "\n".join(
                    dict.fromkeys(
                        part
                        for part in (academic_section, question_context, option_context, label)
                        if part
                    )
                )
                if i + 1 >= len(body):
                    if new_prompt and not SECTION.match(label):
                        unrecognized.append({"label": label, "context": context})
                    continue
                following = body[i + 1]
                if "paragraph" not in following or not plain_container([following]):
                    if new_prompt and not SECTION.match(label):
                        unrecognized.append({"label": label, "context": context})
                    continue
                if "bullet" in following["paragraph"] or explicit_answer_slots(
                    paragraph_text(following)
                ):
                    # The explicit label owns its answer span; list options remain
                    # protected instructions even when they follow a written prompt.
                    continue
                # Only a label followed by an actual answer paragraph. A heading followed
                # by instructions (not a blank paragraph) is never treated as a fill slot.
                current = paragraph_text(following).strip()
                if (
                    inline_slots(paragraph_text(following))
                    or re.sub(r"[〔〕【】［］\[\]\s]", "", current)
                    in {"理由", "回答", "答え", "解答"}
                    or (
                        current
                        and (
                            SECTION.match(current)
                            or paragraph_options(current)
                            or WRITTEN_PROMPT.search(current)
                            or (SECTION.match(label) and re.search(r"[?？]$", current))
                        )
                    )
                ):
                    if new_prompt and not SECTION.match(label):
                        unrecognized.append({"label": label, "context": context})
                    continue
                if (
                    current
                    and not written_prompt
                    and not new_prompt
                    and not re.search(r"特徴|共通点|回答|答え|まとめ|考察|answer", label, re.I)
                ):
                    continue
                # Existing field IDs and hashes remain valid for cached keyword and
                # written-answer mappings. Newly recognized prompts include section
                # context so repeated "その理由は？" labels cannot collide.
                old_field = bool(legacy_prompt and not options and not reason_prompt)
                if old_field:
                    field_id, field_context, field_hash = (
                        f"{tab['id']}/after:{digest(label)[:16]}",
                        label,
                        digest(label),
                    )
                else:
                    count_key = (academic_section, label)
                    occurrence = prompt_counts.get(count_key, 0)
                    prompt_counts[count_key] = occurrence + 1
                    field_id = f"{tab['id']}/after:{digest([context, occurrence])[:16]}"
                    field_context, field_hash = context, digest(context)
                fields.append(
                    {
                        "id": field_id,
                        "tab_id": tab["id"],
                        "label": ("選択（" + "／".join(options) + "）") if options else label,
                        "context": field_context,
                        "context_sha256": field_hash,
                        "index": following["startIndex"],
                        "current": current,
                        **({"kind": "selection", "options": options} if options else {}),
                    }
                )
                answer_paragraphs.add(i + 1)
    return {
        "document_id": document["documentId"],
        "revision_id": document.get("revisionId"),
        "topology": topology,
        "fields": fields,
        "unrecognized_questions": unrecognized,
        "question_sha256": digest([[f["id"], f["context_sha256"]] for f in fields]),
    }


def plan_fill(document, answers, *, student_profile=None):
    include_identity = student_profile is not None
    profile = (student_profile or StudentProfile()).validated()
    form = inspect_form(document, include_identity=include_identity)
    if not form["revision_id"]:
        raise PermissionDenied("未读到可编辑的文档版本，不能填入。")
    lookup = {f["id"]: f for f in form["fields"]}
    seen, requests, expected, conflicts = set(), [], {}, []
    if not isinstance(answers, list) or not answers:
        raise WorkflowError("没有可映射到原文档答案栏的实际答案。初稿已保留。")
    answers = list(answers)
    identity_warnings = []
    for field in form["fields"]:
        if not field.get("identity_key"):
            continue
        if any(a.get("field_id") == field["id"] for a in answers):
            raise WorkflowError("身份栏只能采用私人学生资料自动填写，不能由模型指定。")
        value = getattr(profile, field["identity_key"])
        if not value:
            if not field["current"]:
                identity_warnings.append(f"{field['label']}尚无已保存的学生资料，身份栏仍为空。")
            continue
        if field["current"] and field["current"] != value:
            identity_warnings.append(f"{field['label']}已有内容与学生资料不同，已保留，请核对。")
            continue
        answers.append(
            {"field_id": field["id"], "context_sha256": field["context_sha256"], "text": value}
        )
    for answer in answers:
        key, text = answer.get("field_id"), answer.get("text")
        if key in seen or key not in lookup:
            raise WorkflowError("答案栏映射无效或重复，未修改文档。")
        seen.add(key)
        field = lookup[key]
        if answer.get("context_sha256") != field["context_sha256"]:
            raise WorkflowError("原题或表头已经变化，请重新读题；未修改文档。")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 20000
            or re.search(r"[\x00-\x1f\ue000-\uf8ff]", text.strip())
            or "[E:" in text
        ):
            raise WorkflowError("答案包含无效文字或内部证据标记；未修改文档。")
        text = text.strip()
        if field.get("choices") and text not in field["choices"]:
            raise WorkflowError("文档勾选栏只接受原栏允许的选中标记；未修改文档。")
        if field.get("kind") == "selection":
            allowed = {re.sub(r"\s+", "", option) for option in field["options"]} | {
                re.match(r"[①-⑳]|[（(]?[1-9][)）.．]", option)[0] for option in field["options"]
            }
            if re.sub(r"\s+", "", text) not in allowed:
                raise WorkflowError("文档选择栏只接受原题列出的选项；未修改文档。")
        expected[key] = text
        if field["current"] == text:
            continue
        if field["current"]:
            conflicts.append(key)
            continue
        edits = []
        if field.get("end_index", field["index"]) > field["index"]:
            edits.append(
                {
                    "deleteContentRange": {
                        "range": {
                            "startIndex": field["index"],
                            "endIndex": field["end_index"],
                            "tabId": field["tab_id"],
                        }
                    }
                }
            )
        edits.append(
            {
                "insertText": {
                    "location": {"index": field["index"], "tabId": field["tab_id"]},
                    "text": field.get("insert_prefix", "") + text,
                }
            }
        )
        requests.append((field["tab_id"], field["index"], edits))
    if conflicts:
        raise WorkflowError("保留你已填写的内容，未覆盖答案栏：" + ", ".join(conflicts[:8]))
    requests.sort(key=lambda r: (r[0], -r[1]))
    return {
        "document_id": form["document_id"],
        "form": form,
        "expected": expected,
        "requests": [edit for _, _, edits in requests for edit in edits],
        "write_control": {"requiredRevisionId": form["revision_id"]},
        "include_identity": include_identity,
        "identity_warnings": identity_warnings,
    }


def verify_fill(before, after, plan):
    original = inspect_form(before, include_identity=plan.get("include_identity", False))
    actual = inspect_form(after, include_identity=plan.get("include_identity", False))
    if (
        actual["document_id"] != plan["document_id"]
        or actual["topology"] != original["topology"]
        or actual["question_sha256"] != original["question_sha256"]
    ):
        raise WorkflowError("写入后原题或分页结构核对失败，需要查看原文档。")
    fields = {f["id"]: f for f in actual["fields"]}
    if any(fields.get(k, {}).get("current") != v for k, v in plan["expected"].items()):
        raise WorkflowError("写入结果尚未通过回读核对；重试前会重新读取，避免重复插入。")

    # Every out-of-scope paragraph and native element must be unchanged. Index shifts
    # are expected; editable paragraphs are checked separately against exact answers.
    def signature(document, form):
        editable = {
            (f["tab_id"], f["index"])
            for f in form["fields"]
            if f["id"] in plan["expected"] and "span" not in f
        }
        inline = {}
        for field in form["fields"]:
            if field["id"] in plan["expected"] and "span" in field:
                inline.setdefault((field["tab_id"], field["paragraph_index"]), []).append(
                    field["span"]
                )

        def clean(value, tab_id):
            if isinstance(value, list):
                return [clean(v, tab_id) for v in value]
            if not isinstance(value, dict):
                return value
            if "paragraph" in value and (tab_id, value.get("startIndex")) in editable:
                return {"answer_paragraph_style": value["paragraph"].get("paragraphStyle", {})}
            if "paragraph" in value and (tab_id, value.get("startIndex")) in inline:
                paragraph = value["paragraph"]
                return {
                    **{
                        k: clean(v, tab_id)
                        for k, v in value.items()
                        if k not in {"startIndex", "endIndex", "paragraph"}
                    },
                    "paragraph": {
                        **{k: clean(v, tab_id) for k, v in paragraph.items() if k != "elements"},
                        "elements": protected_inline_runs(
                            paragraph["elements"], inline[(tab_id, value["startIndex"])]
                        ),
                    },
                }
            return {
                k: clean(v, tab_id) for k, v in value.items() if k not in {"startIndex", "endIndex"}
            }

        return [clean(tab["content"], tab["id"]) for tab in document_tabs(document)]

    if signature(before, original) != signature(after, actual):
        raise WorkflowError("写入后非答案内容或原格式发生变化，请在文档中核对。")
    verified_by_kind = {}
    for key in plan["expected"]:
        kind = fields[key].get("kind", "paragraph")
        verified_by_kind[kind] = verified_by_kind.get(kind, 0) + 1
    remaining_required = [
        {key: field[key] for key in ("id", "label", "context")}
        for field in required_answer_fields(actual)
        if not field["current"]
    ]
    unknown_questions = actual.get("unrecognized_questions", [])
    return {
        "status": "partial" if remaining_required or unknown_questions else "filled_verified",
        "document_id": actual["document_id"],
        "url": f"https://docs.google.com/document/d/{actual['document_id']}/edit",
        "verified_fields": len(plan["expected"]),
        "verified_by_kind": verified_by_kind,
        "identity_warnings": plan.get("identity_warnings", []),
        "remaining_empty_fields": [f["id"] for f in actual["fields"] if not f["current"]],
        "remaining_required_fields": remaining_required,
        "unrecognized_questions": unknown_questions,
        "inserted_fields": sum("insertText" in request for request in plan["requests"]),
        "revision_id": actual["revision_id"],
        "answers_sha256": digest(plan["expected"]),
        "question_sha256": actual["question_sha256"],
        "verified_at": utc_now(),
        "submission": "manual_only",
    }


def legacy_answers(document, draft, *, original_text=None):
    """Migrate a real numbered ○/× draft into its exact existing worksheet layout.

    No generated answer, fake citation or generic Markdown dump is permitted here.
    Unrecognized layouts require structured model output instead.
    """
    fields = inspect_form(document)["fields"]
    if original_text is not None:

        def normalize(text):
            return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))

        source = normalize(original_text)
        if any(normalize(f["context"]) not in source for f in fields):
            raise WorkflowError("现有初稿的原题与当前文档不一致，请重新读取要求；未修改文档。")
    rows = re.findall(r"(?m)^\|\s*(\d+)\s*\|\s*([〇○◯×])\s*\|\s*([^\n]+)\|\s*$", draft)
    numbered = {
        int(n): (mark, re.sub(r"\s*\[E:[^\]]+\]", "", reason).strip()) for n, mark, reason in rows
    }
    answers = []
    for f in fields:
        match = re.match(r"\[(\d+)\]", f["context"])
        text = None
        if match and int(match[1]) in numbered:
            mark, reason = numbered[int(match[1])]
            if re.fullmatch(r"[〇○◯×/\s]+", f["label"]):
                text = mark
            elif f["label"] == "判断の理由":
                text = reason
        elif "/after:" in f["id"]:
            key = re.sub(r"[■「」\s]", "", f["label"])
            for title, content in re.findall(r"(?ms)^## ([^\n]+)\n(.*?)(?=^## |\Z)", draft):
                if re.sub(r"[「」\s]", "", title) == key:
                    text = re.sub(r"\s*\[E:[^\]]+\]", "", content).strip()
        if text:
            answers.append(
                {"field_id": f["id"], "context_sha256": f["context_sha256"], "text": text}
            )
    if len(rows) != len(numbered) or len(answers) != len(fields):
        raise WorkflowError("现有初稿不能完整映射到原表格；未向文档追加不明格式的内容。")
    return answers


class GoogleDocuments:
    def __init__(self, credentials):
        self.api = build(
            "docs",
            "v1",
            http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=60)),
            cache_discovery=False,
        )

    def get(self, document_id):
        return retry_read(
            lambda: (
                self.api.documents()
                .get(
                    documentId=document_id,
                    includeTabsContent=True,
                    suggestionsViewMode="SUGGESTIONS_INLINE",
                )
                .execute()
            )
        )

    def batch(self, plan):
        # Never retry a possibly successful mutation without reading its actual result.
        return (
            self.api.documents()
            .batchUpdate(
                documentId=plan["document_id"],
                body={"requests": plan["requests"], "writeControl": plan["write_control"]},
            )
            .execute(num_retries=0)
        )


def own_document_ids(reader, course_id, assignment_id):
    submissions = [
        s for s in reader.own_submissions(course_id) if s.get("courseWorkId") == assignment_id
    ]
    if len(submissions) != 1 or submissions[0].get("state") not in {
        "NEW",
        "CREATED",
        "RECLAIMED_BY_STUDENT",
    }:
        raise WorkflowError("作业不再是本人待完成状态，未修改文档。")
    return [
        a["driveFile"]["id"]
        for a in submissions[0].get("assignmentSubmission", {}).get("attachments", [])
        if a.get("driveFile", {}).get("id")
    ]


def template_matches_source(document, source_text):
    """Require every protected paragraph in order in the verified source export.

    PDF exports add line wraps, list numbers and numeric page footers;
    do not remap a saved draft onto changed questions with familiar answer labels.
    """

    def normalize(value):
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))

    def export_numbers(value):
        return re.fullmatch(r"[0-9.()\[\]、]*", value) is not None

    source = normalize(source_text)
    if not source:
        return False
    form = inspect_form(document, include_identity=True)
    editable, inline = set(), {}
    for field in form["fields"]:
        if "span" in field:
            inline.setdefault((field["tab_id"], field["paragraph_index"]), []).append(field)
        else:
            editable.add((field["tab_id"], field["index"]))

    def paragraphs(content):
        for element in content:
            if "paragraph" in element:
                yield element
            elif "table" in element:
                for row in element["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        yield from paragraphs(cell.get("content", []))

    cursor, checked = 0, 0
    for tab in document_tabs(document):
        for paragraph in paragraphs(tab["content"]):
            key = (tab["id"], paragraph["startIndex"])
            if key in editable:
                continue
            text = paragraph_text(paragraph)
            for field in sorted(inline.get(key, []), key=lambda f: f["span"][0], reverse=True):
                start, end = field["span"]
                text = text[:start] + text[end:]
            protected = normalize(text)
            if not protected:
                continue
            found = source.find(protected, cursor)
            if found < 0 or not export_numbers(source[cursor:found]):
                return False
            cursor = found + len(protected)
            checked += 1
    return bool(checked) and export_numbers(source[cursor:])


def fill_personal_document(
    reader,
    documents,
    *,
    course_id,
    assignment_id,
    document_id,
    answers,
    record_dir: Path,
    student_profile=None,
    verified_draft=None,
    original_text=None,
):
    """Fresh personal-submission allowlist, private lock/plan/receipt and verified write."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", document_id):
        raise WorkflowError("个人文档 ID 无效。")
    record_dir = require_private_path(record_dir)
    record_dir.mkdir(parents=True, exist_ok=True)
    with document_lock(record_dir / "fill.lock"):
        if document_id not in own_document_ids(reader, course_id, assignment_id):
            raise PermissionDenied(
                "只允许填入当前作业提交记录里的本人文档，不修改教师模板或课堂资料。"
            )
        metadata = reader.personal_document_metadata(document_id)
        if (
            metadata.get("id") != document_id
            or metadata.get("mimeType") != "application/vnd.google-apps.document"
            or metadata.get("ownedByMe") is not True
            or metadata.get("capabilities", {}).get("canEdit") is not True
        ):
            raise PermissionDenied("文档必须由当前学校账户拥有并可编辑，不修改共享的教师原件。")
        before = documents.get(document_id)
        recovery = None
        if verified_draft and original_text:
            from .document_answer_recovery import recover_labeled_answers

            recovered = recover_labeled_answers(inspect_form(before), verified_draft, answers)
            if recovered is not None and recovered != answers:
                if not template_matches_source(before, original_text):
                    raise WorkflowError("原题与已核验初稿的来源不一致，请重新读题；未修改文档。")
                recovery = {
                    "kind": "verified_draft_labels",
                    "draft_sha256": hashlib.sha256(verified_draft.encode("utf-8")).hexdigest(),
                    "source_sha256": hashlib.sha256(original_text.encode("utf-8")).hexdigest(),
                    "original_answers": answers,
                    "recovered_answers": recovered,
                    "recovered_at": utc_now(),
                }
                answers = recovered
        plan = plan_fill(before, answers, student_profile=student_profile)
        attempt_dir = record_dir / "attempts" / uuid.uuid4().hex
        attempt_dir.mkdir(parents=True)
        if not (record_dir / "original.json").exists():
            atomic_json(record_dir / "original.json", before)
        atomic_json(attempt_dir / "before.json", before)
        atomic_json(attempt_dir / "plan.json", plan)
        if recovery:
            atomic_json(attempt_dir / "mapping-recovery.json", recovery)
        atomic_json(record_dir / "status.json", {"status": "writing", "started_at": utc_now()})
        write_error = None
        if plan["requests"]:
            try:
                documents.batch(plan)
            except Exception as exc:
                write_error = exc
        try:
            after = documents.get(document_id)
            atomic_json(attempt_dir / "after.json", after)
            receipt = verify_fill(before, after, plan)
        except WorkflowError as exc:
            atomic_json(
                record_dir / "status.json",
                {
                    "status": "needs_review",
                    "error": str(exc),
                    "mutation_response": "failed_or_unknown" if write_error else "received",
                },
            )
            raise WorkflowError(
                "自动填入未通过核验，初稿已保留。"
                + ("Google 写入权限、文档版本或网络需要检查。" if write_error else str(exc))
            ) from exc
        if recovery:
            receipt["mapping_recovery"] = {
                "kind": recovery["kind"],
                "draft_sha256": recovery["draft_sha256"],
                "source_sha256": recovery["source_sha256"],
                "answer_count": len(answers),
            }
        atomic_json(record_dir / "receipt.json", receipt)
        atomic_json(attempt_dir / "receipt.json", receipt)
        atomic_json(record_dir / "status.json", receipt)
        return receipt


def prepare_document_forms(settings, package):
    from .auth import credentials_for
    from .google_read import GoogleReader

    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    read_credentials, _ = credentials_for(settings)
    reader = GoogleReader(read_credentials)
    docs = GoogleDocuments(read_credentials)
    forms = []
    for doc_id in own_document_ids(reader, manifest["course_id"], manifest["assignment_id"]):
        if reader.file_metadata(doc_id)["mimeType"] == "application/vnd.google-apps.document":
            forms.append(inspect_form(docs.get(doc_id)))
    return forms


def fill_review(settings, package, *, progress=lambda _: None):
    """Fill verified AI output into real personal copies, then expose the original links."""
    from .auth import credentials_for
    from .drafting import reusable_review
    from .google_read import GoogleReader

    package = require_private_path(Path(package))
    validated = reusable_review(package)
    if not validated or not validated.get("draft_sha256"):
        raise WorkflowError("只有通过证据核对的真实 Codex 初稿可以自动填入。")
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    read_credentials, _ = credentials_for(settings)
    credentials, _ = credentials_for(settings, documents=True)
    if manifest.get("ai_confirmation") is not True or not manifest["policy"]["can_draft"]:
        raise WorkflowError("本次作业未获用户的 AI 生成确认，不自动填入。")
    reader, docs = GoogleReader(read_credentials), GoogleDocuments(credentials)
    path = package / "document-answers.json"
    generation = json.loads((package / "codex-generation.json").read_text(encoding="utf-8"))
    structured = []
    if path.is_file() and generation.get("document_answers_sha256"):
        if sha256_file(path) != generation["document_answers_sha256"]:
            raise WorkflowError("答案栏文件已变化，未修改原文档。")
        structured = json.loads(path.read_text(encoding="utf-8")).get("answers", [])
    receipts = []
    draft_path, requirements_path = package / "draft.md", package / "requirements.json"
    draft_bytes = draft_path.read_bytes() if draft_path.is_file() else None
    if (
        draft_bytes is not None
        and hashlib.sha256(draft_bytes).hexdigest() != validated["draft_sha256"]
    ):
        raise WorkflowError("已核验初稿文件发生变化，未修改原文档。")
    verified_draft = draft_bytes.decode("utf-8") if draft_bytes is not None else None
    requirements_bytes = requirements_path.read_bytes() if requirements_path.is_file() else None
    requirements = json.loads(requirements_bytes) if requirements_bytes is not None else {}
    recovery_source_verified = requirements_bytes is not None and hashlib.sha256(
        requirements_bytes
    ).hexdigest() == generation.get("source_files_sha256", {}).get("requirements.json")
    for doc_id in own_document_ids(reader, manifest["course_id"], manifest["assignment_id"]):
        if reader.file_metadata(doc_id)["mimeType"] != "application/vnd.google-apps.document":
            continue
        answers = [a for a in structured if a.get("document_id") == doc_id]
        original_text = "\n".join(
            s["text"]
            for s in requirements.get("sources", [])
            if s.get("source_id") == "file:" + doc_id
        )
        # One-time migration of previous real ○/× worksheet drafts. It is deterministic,
        # preserves model provenance and never manufactures an extra model invocation.
        if not answers and not structured:
            requirements = json.loads((package / "requirements.json").read_text(encoding="utf-8"))
            original_text = "\n".join(
                s["text"] for s in requirements["sources"] if s.get("source_id") == "file:" + doc_id
            )
            try:
                answers = legacy_answers(
                    docs.get(doc_id),
                    (package / "draft.md").read_text(encoding="utf-8"),
                    original_text=original_text,
                )
            except WorkflowError:
                if not (verified_draft and original_text and recovery_source_verified):
                    raise
                # A fully labeled verified draft can recover an entirely empty old
                # mapping after the fresh document read inside the write lock.
        if not answers and not (verified_draft and original_text and recovery_source_verified):
            raise WorkflowError("这份个人文档没有已核验的答案栏映射，初稿已保留。")
        progress("正在填入个人 Google 文档；写入后会回读核对")
        receipt = fill_personal_document(
            reader,
            docs,
            course_id=manifest["course_id"],
            assignment_id=manifest["assignment_id"],
            document_id=doc_id,
            answers=answers,
            record_dir=settings.data_dir
            / "document-fills"
            / manifest["course_id"]
            / manifest["assignment_id"]
            / doc_id,
            student_profile=StudentProfile.load(settings.data_dir),
            verified_draft=verified_draft,
            original_text=original_text if recovery_source_verified else None,
        )
        receipts.append(receipt)
        progress(
            "已填入部分答案，仍有题目待补全"
            if receipt["status"] == "partial"
            else "已填入并核验个人文档，可以直接打开审阅；尚未提交"
        )
    if not receipts:
        raise WorkflowError("这项作业没有可填入的个人 Google 文档，请查看作业要求。")
    remaining_required = [
        {**field, "document_id": receipt["document_id"]}
        for receipt in receipts
        for field in receipt.get("remaining_required_fields", [])
    ]
    unknown_questions = [
        {**question, "document_id": receipt["document_id"]}
        for receipt in receipts
        for question in receipt.get("unrecognized_questions", [])
    ]
    result = {
        "status": "partial"
        if remaining_required
        or unknown_questions
        or any(receipt["status"] == "partial" for receipt in receipts)
        else "filled_verified",
        "documents": receipts,
        "remaining_required_fields": remaining_required,
        "unrecognized_questions": unknown_questions,
        "submission": "manual_only",
        "generation": {
            k: generation.get(k)
            for k in ("model", "reasoning_effort", "thread_id", "turn_id", "finished_at")
        },
        "draft_sha256": validated["draft_sha256"],
        "identity_warnings": [
            warning for receipt in receipts for warning in receipt.get("identity_warnings", [])
        ],
    }
    atomic_json(package / "document-fill.json", result)
    return result
