"""Fill existing personal submission Docs. No Classroom mutation methods exist here.

The pure planner accepts native Docs or connector reads. The API adapter is separate;
only this module constructs insertText requests, with revision guards and readback.
"""

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

import httplib2
from filelock import FileLock, Timeout
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

from .errors import PermissionDenied, WorkflowError
from .google_read import retry_read
from .local import atomic_json, require_private_path, sha256_file, utc_now

HEADERS = re.compile(r"^(?:[〇○◯×/\s]+|判断の理由|理由|回答|答え|解答|answer|reason)$", re.I)
PERSONAL = re.compile(r"学籍|氏.?名|姓名|名前|student.?id|\bname\b", re.I)


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


def inspect_form(document):
    """Identify bounded answer containers; exclude identity fields and unknown layouts.

    Stable field IDs use tab/table/row/column, not shifting character offsets. Context
    hashes cover questions/headers, so regenerated answers cannot target changed questions.
    """
    fields, topology, instructions = [], [], []
    for tab in document_tabs(document):
        topology.append({k: tab[k] for k in ("id", "title", "parent", "index")})
        table_number = 0
        body = tab["content"]
        for i, element in enumerate(body):
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
                is_prompt = (
                    label.startswith("■")
                    or element["paragraph"]
                    .get("paragraphStyle", {})
                    .get("namedStyleType", "")
                    .startswith("HEADING")
                    or re.search(r"(?:回答|答え|解答|まとめ|考察|answer)\s*[:：]$", label, re.I)
                )
                if not label or not is_prompt or PERSONAL.search(label) or i + 1 >= len(body):
                    continue
                following = body[i + 1]
                if "paragraph" not in following or not plain_container([following]):
                    continue
                # Only a label followed by an actual answer paragraph. A heading followed
                # by instructions (not a blank paragraph) is never treated as a fill slot.
                current = paragraph_text(following).strip()
                if current and not re.search(
                    r"特徴|共通点|回答|答え|まとめ|考察|answer", label, re.I
                ):
                    continue
                fields.append(
                    {
                        "id": f"{tab['id']}/after:{digest(label)[:16]}",
                        "tab_id": tab["id"],
                        "label": label,
                        "context": label,
                        "context_sha256": digest(label),
                        "index": following["startIndex"],
                        "current": current,
                    }
                )
    return {
        "document_id": document["documentId"],
        "revision_id": document.get("revisionId"),
        "topology": topology,
        "fields": fields,
        "question_sha256": digest([[f["id"], f["context_sha256"]] for f in fields]),
    }


def plan_fill(document, answers):
    form = inspect_form(document)
    if not form["revision_id"]:
        raise PermissionDenied("未读到可编辑的文档版本，不能填入。")
    lookup = {f["id"]: f for f in form["fields"]}
    seen, requests, expected, conflicts = set(), [], {}, []
    if not isinstance(answers, list) or not answers:
        raise WorkflowError("没有可映射到原文档答案栏的实际答案。初稿已保留。")
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
        expected[key] = text
        if field["current"] == text:
            continue
        if field["current"]:
            conflicts.append(key)
            continue
        requests.append(
            {
                "insertText": {
                    "location": {"index": field["index"], "tabId": field["tab_id"]},
                    "text": text,
                }
            }
        )
    if conflicts:
        raise WorkflowError("保留你已填写的内容，未覆盖答案栏：" + ", ".join(conflicts[:8]))
    requests.sort(
        key=lambda r: (r["insertText"]["location"]["tabId"], -r["insertText"]["location"]["index"])
    )
    return {
        "document_id": form["document_id"],
        "form": form,
        "expected": expected,
        "requests": requests,
        "write_control": {"requiredRevisionId": form["revision_id"]},
    }


def verify_fill(before, after, plan):
    original, actual = inspect_form(before), inspect_form(after)
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
            (f["tab_id"], f["index"]) for f in form["fields"] if f["id"] in plan["expected"]
        }

        def clean(value, tab_id):
            if isinstance(value, list):
                return [clean(v, tab_id) for v in value]
            if not isinstance(value, dict):
                return value
            if "paragraph" in value and (tab_id, value.get("startIndex")) in editable:
                return {"answer_paragraph_style": value["paragraph"].get("paragraphStyle", {})}
            return {
                k: clean(v, tab_id) for k, v in value.items() if k not in {"startIndex", "endIndex"}
            }

        return [clean(tab["content"], tab["id"]) for tab in document_tabs(document)]

    if signature(before, original) != signature(after, actual):
        raise WorkflowError("写入后非答案内容或原格式发生变化，请在文档中核对。")
    return {
        "status": "filled_verified",
        "document_id": actual["document_id"],
        "url": f"https://docs.google.com/document/d/{actual['document_id']}/edit",
        "verified_fields": len(plan["expected"]),
        "remaining_empty_fields": [f["id"] for f in actual["fields"] if not f["current"]],
        "inserted_fields": len(plan["requests"]),
        "revision_id": actual["revision_id"],
        "answers_sha256": digest(plan["expected"]),
        "question_sha256": actual["question_sha256"],
        "verified_at": utc_now(),
        "submission": "manual_only",
    }


def legacy_answers(document, draft):
    """Migrate a real numbered ○/× draft into its exact existing worksheet layout.

    No generated answer, fake citation or generic Markdown dump is permitted here.
    Unrecognized layouts require structured model output instead.
    """
    fields = inspect_form(document)["fields"]
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


def fill_personal_document(
    reader, documents, *, course_id, assignment_id, document_id, answers, record_dir: Path
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
        plan = plan_fill(before, answers)
        attempt_dir = record_dir / "attempts" / uuid.uuid4().hex
        attempt_dir.mkdir(parents=True)
        if not (record_dir / "original.json").exists():
            atomic_json(record_dir / "original.json", before)
        atomic_json(attempt_dir / "before.json", before)
        atomic_json(attempt_dir / "plan.json", plan)
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
    for doc_id in own_document_ids(reader, manifest["course_id"], manifest["assignment_id"]):
        if reader.file_metadata(doc_id)["mimeType"] != "application/vnd.google-apps.document":
            continue
        answers = [a for a in structured if a.get("document_id") == doc_id]
        # One-time migration of previous real ○/× worksheet drafts. It is deterministic,
        # preserves model provenance and never manufactures an extra model invocation.
        if not answers and not structured:
            answers = legacy_answers(
                docs.get(doc_id), (package / "draft.md").read_text(encoding="utf-8")
            )
        if not answers:
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
        )
        receipts.append(receipt)
        progress("已填入并核验个人文档，可以直接打开审阅；尚未提交")
    if not receipts:
        raise WorkflowError("这项作业没有可填入的个人 Google 文档，请查看作业要求。")
    result = {
        "status": "filled_verified",
        "documents": receipts,
        "submission": "manual_only",
        "generation": {
            k: generation.get(k)
            for k in ("model", "reasoning_effort", "thread_id", "turn_id", "finished_at")
        },
        "draft_sha256": validated["draft_sha256"],
    }
    atomic_json(package / "document-fill.json", result)
    return result
