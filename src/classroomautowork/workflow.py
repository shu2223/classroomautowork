"""Callable orchestration: read the knowledge base, prepare review packages, hand off.

Course data comes from the knowledge API, plus the bounded Forms GET reader for form questions the
knowledge base does not hold. Course text never becomes a command, executable path, configuration,
scope, approval or account selection.
"""

import json
import re
from dataclasses import asdict
from pathlib import Path

from .config import Settings
from .errors import ConfigurationError, PermissionDenied, RunCancelled, WorkflowError
from .external_sources import external_chunks, source_urls
from .form_capture import attach_form_images, authenticated_form_cache
from .form_read import PARSER_VERSION, form_chunks, form_references, read_form
from .google_read import drive_attachments
from .kb_client import KnowledgeClient
from .local import atomic_json, utc_now
from .pages import LONG_EDGE, RENDER_VERSION, SLIDE_TYPES, page_images, renderable
from .pending import PENDING_STATES, discover_pending
from .policy import CoursePolicy
from .progress import report
from .search import retrieve
from .store import Store, artifact, fingerprint
from .student import StudentProfile
from .supplements import load_supplement


def stable_source(value):
    """Google rotates thumbnail URLs without changing teaching content."""
    if isinstance(value, dict):
        return {key: stable_source(item) for key, item in value.items() if key != "thumbnailUrl"}
    if isinstance(value, list):
        return [stable_source(item) for item in value]
    return value


def text_chunks(item: dict, kind: str) -> list[dict]:
    fields = [
        "title",
        "description",
        "text",
        "workType",
        "dueDate",
        "dueTime",
        "maxPoints",
        "multipleChoiceQuestion",
        "assigneeMode",
        "submissionModificationMode",
    ]
    text = "\n".join(
        f"{name}: {json.dumps(item[name], ensure_ascii=False) if not isinstance(item[name], str) else item[name]}"
        for name in fields
        if name in item
    )
    # Classroom form/link/YouTube resources are references, never executable actions.
    if item.get("materials"):
        text += "\nmaterials: " + json.dumps(item["materials"], ensure_ascii=False)
    if item.get("assignmentSubmission"):
        text += "\nstudent_document: " + json.dumps(
            item["assignmentSubmission"], ensure_ascii=False
        )
    return [
        {
            "source_kind": kind,
            "title": item.get("title", kind),
            "url": item.get("alternateLink"),
            "locator": f"Classroom text characters {start + 1}-{min(start + 1600, len(text))}",
            "text": text[start : start + 1600],
        }
        for start in range(0, len(text), 1600)
    ]


def document_chunks(reader, doc_id, *, source_kind, title, url):
    # A long page yields several chunks with the same locator; evidence IDs need each one distinct.
    return [
        {
            "source_kind": source_kind,
            "title": title,
            "url": url,
            "locator": f"{chunk['locator'] or '正文'} · kb:{doc_id}#{chunk['ord']}",
            "text": chunk["text"],
        }
        for chunk in reader.document_chunks(doc_id)
    ]


def attach_page_images(reader, settings, store, document, chunks, *, every_page):
    """Pictures, charts and scans reach the model as page images; extracted text misses them.

    Returns the chunks with `image_path` set on each rendered page, plus a warning or None.
    A page with no extracted text at all gets a chunk of its own so its image has a home.
    """
    name = document.get("source") or ""
    if not renderable(name) or not chunks:
        return chunks, None

    def render(key):
        rendered = page_images(
            reader.document_source(document["id"]),
            name,
            settings.data_dir / "processed" / key[:24],
            settings.data_dir / "office",
            every_page=every_page,
        )
        return {
            "pages": {str(number): str(path) for number, path in rendered.items()},
            "artifacts": [artifact(path) for path in rendered.values()],
        }

    identity = [
        document["id"],
        document.get("source_bytes"),
        document.get("source_mtime"),
        fingerprint([c["text"] for c in chunks]),
        every_page,
        RENDER_VERSION,
        LONG_EDGE,
    ]
    try:
        rendered = store.memo("kb-pages", identity, render)["pages"]
    except WorkflowError as exc:
        return chunks, str(exc)
    unit = "张" if Path(name).suffix.lower() in SLIDE_TYPES else "页"
    first = {}
    for chunk in chunks:
        match = re.match(r"第(\d+)[页张]", chunk["locator"])
        if match:
            first.setdefault(int(match[1]), chunk)
    template = chunks[0]
    for number, path in sorted(rendered.items(), key=lambda item: int(item[0])):
        if int(number) in first:
            first[int(number)]["image_path"] = path
        else:
            chunks.append(
                {
                    **{k: template[k] for k in ("source_kind", "title", "url")},
                    "locator": f"第{number}{unit} · 页面图片",
                    "text": "（这一页没有可提取的文字，内容见页面图片。）",
                    "image_path": path,
                }
            )
    return chunks, None


def sync_course(
    reader, settings: Settings, store: Store, course_id: str, *, progress=None, submissions=()
) -> dict:
    policy = CoursePolicy.load(settings.data_dir, course_id)
    course = reader.course(course_id)
    warnings, records, readable = [], [], set()
    # Partial failures are visible and stale unavailable sources are removed from retrieval.
    for kind, fetch in (
        ("coursework", reader.assignments),
        ("material", reader.course_materials),
        ("announcement", reader.announcements),
    ):
        report(
            progress,
            "正在读取同课程的"
            + {"coursework": "历史作业", "material": "课程资料", "announcement": "公告"}[kind],
            "course_sync",
        )
        records.extend((kind, stable_source(item)) for item in fetch(course_id))
    selected = {item["courseWorkId"] for item in submissions}
    attachments, origins, resolutions = {}, {}, {}
    linked_forms, form_origins, response_forms = {}, {}, {}
    for kind, item in records:
        source_id = kind + ":" + item.get("id", "")
        revision = fingerprint(item)
        store.replace_chunks(course_id, source_id, revision, text_chunks(item, kind))
        readable.add(source_id)
        for url in form_references(item):
            form_id = "form:" + fingerprint(url)[:24]
            linked_forms[form_id] = url
            form_origins.setdefault(form_id, []).append(
                {
                    "source_id": source_id,
                    "url": item.get("alternateLink"),
                    "assignment_id": item.get("id") if kind == "coursework" else None,
                    "role": kind,
                }
            )
        for file in drive_attachments(item):
            attachments[file["id"]] = file
            origins.setdefault(file["id"], []).append(
                {
                    "source_id": source_id,
                    "url": item.get("alternateLink"),
                    "assignment_id": item.get("id") if kind == "coursework" else None,
                    "role": kind,
                }
            )
    report(
        progress,
        f"已找到 {len(attachments)} 份课程附件，开始从资料库读取",
        "course_sync",
        course_id=course_id,
        workload_files=[
            {"file_id": key, "filename": str(value.get("title") or "课程附件")[:200]}
            for key, value in attachments.items()
        ],
    )
    for source_id, url in list(linked_forms.items())[:100]:
        try:
            report(progress, "正在只读读取 Google Forms 全部页面的题目与选项", "form_read")
            try:
                form = read_form(url)
            except PermissionDenied as exc:
                form = authenticated_form_cache(settings.data_dir, settings.school_email, url, exc)
            form = attach_form_images(settings.data_dir, settings.school_email, form)
            revision = fingerprint(form)

            def save_form(key, value=form):
                path = settings.data_dir / "forms" / key[:24] / "questions.json"
                atomic_json(path, value)
                return {
                    "path": str(path),
                    "chunks": form_chunks(value),
                    "artifacts": [artifact(path)]
                    + [
                        artifact(Path(picture["image_path"]))
                        for picture in value.get("media", [])
                        if picture.get("image_path")
                    ],
                }

            result = store.memo("form-questions", [revision, PARSER_VERSION], save_form)
            store.replace_chunks(course_id, source_id, revision, result["chunks"])
            readable.add(source_id)
            response_forms[source_id] = {
                "source_id": source_id,
                "title": form["title"],
                "url": url,
                "page_count": form["page_count"],
                "question_count": len(form["questions"]),
                "read_complete": form["read_complete"],
            }
            report(
                progress,
                f"已读取表单 {form['page_count']} 页、{len(form['questions'])} 个题目或身份栏，题目已加入要求来源",
                "form_read",
            )
            warnings.extend(
                {
                    "source": source_id,
                    "url": url,
                    "origins": form_origins[source_id],
                    "error": warning,
                }
                for warning in form["warnings"]
            )
        except RunCancelled:
            raise
        except WorkflowError as exc:
            warnings.append(
                {
                    "source": source_id,
                    "url": url,
                    "origins": form_origins[source_id],
                    "error": str(exc),
                }
            )
    for source_id, url in list(linked_forms.items())[100:]:
        warnings.append(
            {
                "source": source_id,
                "url": url,
                "error": "课程表单数量超过本次 100 份只读预算，未读取。",
            }
        )
    linked_docs = set()
    course_documents = {d["id"]: d for d in reader.documents(course_id)}
    for number, (file_id, reference) in enumerate(attachments.items(), 1):
        name = str(reference.get("title") or "课程附件")[:200]
        report(
            progress,
            f"{name} · 正在从资料库读取",
            "attachment",
            course_id=course_id,
            file_id=file_id,
            filename=name,
            attachment_index=number,
            attachment_total=len(attachments),
        )
        resolutions[file_id] = file_id
        found = reader.file_document(course_id, file_id)
        if found:
            linked_docs.add(found["doc"])
        if not found or found["status"] != "ready":
            warnings.append(
                {
                    "source": "file:" + file_id,
                    "title": reference.get("title"),
                    "url": reference.get("alternateLink"),
                    "origins": origins.get(file_id, []),
                    "error": (
                        f"资料库还没处理完这份附件（状态：{found['status']}）。"
                        if found
                        else "资料库里没有这份附件：可能是老师禁止下载、无权访问或还没抓取。"
                    ),
                }
            )
            continue
        document = course_documents.get(found["doc"], {"id": found["doc"], "kind": "document"})
        chunks = document_chunks(
            reader,
            found["doc"],
            source_kind="transcript" if document["kind"] == "video" else "document",
            title=name,
            url=reference.get("alternateLink"),
        )
        # Every page of the assignment's own files; elsewhere only picture-heavy pages.
        chunks, problem = attach_page_images(
            reader,
            settings,
            store,
            document,
            chunks,
            every_page=any(
                origin.get("assignment_id") in selected for origin in origins.get(file_id, [])
            ),
        )
        if problem:
            warnings.append({"source": "file:" + file_id, "title": name, "error": problem})
        source_id = "file:" + file_id
        store.replace_chunks(course_id, source_id, fingerprint(chunks), chunks)
        readable.add(source_id)
    # Lecture videos and notes in the knowledge base usually belong to no Classroom attachment.
    for document in course_documents.values():
        if document["kind"] == "post" or document["id"] in linked_docs:
            continue
        if document["status"] != "ready":
            warnings.append(
                {
                    "source": "kb:" + document["id"],
                    "title": document["title"],
                    "error": f"资料库还没处理完这份内容（状态：{document['status']}）。",
                }
            )
            continue
        chunks = document_chunks(
            reader,
            document["id"],
            source_kind="transcript" if document["kind"] == "video" else "document",
            title=document["title"],
            url=document.get("url"),
        )
        chunks, problem = attach_page_images(
            reader, settings, store, document, chunks, every_page=False
        )
        if problem:
            warnings.append(
                {"source": "kb:" + document["id"], "title": document["title"], "error": problem}
            )
        source_id = "kb:" + document["id"]
        store.replace_chunks(course_id, source_id, fingerprint(chunks), chunks)
        readable.add(source_id)
    allowed_link_urls = source_urls(records) | source_urls(
        [("cached", value) for value in store.chunks(course_id) if value["source_id"] in readable]
    )
    for source_id, revision, chunks in external_chunks(
        settings.data_dir, course_id, allowed_link_urls
    ):
        store.replace_chunks(course_id, source_id, revision, chunks)
        readable.add(source_id)
    store.prune(course_id, readable)
    return {
        "course": course,
        "policy": policy.draft_gate(),
        "warnings": warnings,
        "assignment_sources": {item["id"]: item for kind, item in records if kind == "coursework"},
        "submissions": {item["courseWorkId"]: stable_source(item) for item in submissions},
        "attachment_origins": origins,
        "attachment_resolutions": resolutions,
        "downloads": {},
        "response_forms": response_forms,
        "form_origins": form_origins,
        "resource_index": [
            {
                "source_id": kind + ":" + item.get("id", ""),
                "title": item.get("title", kind),
                "url": item.get("alternateLink"),
                "kind": kind,
            }
            for kind, item in records
        ],
        "synced_at": utc_now(),
    }


def prepare_assignment(
    settings: Settings, store: Store, course: dict, assignment_id: str, extra_queries=()
) -> dict:
    course_id = course["course"]["id"]
    assignment = course["assignment_sources"].get(assignment_id)
    if not assignment:
        raise WorkflowError("Selected assignment could not be read during this run.")
    chunks = store.chunks(course_id)
    submission = course.get("submissions", {}).get(assignment_id)
    requirement_ids = {"coursework:" + assignment_id}
    response_forms = [
        value
        for source_id, value in course.get("response_forms", {}).items()
        if any(
            origin.get("assignment_id") == assignment_id
            for origin in course.get("form_origins", {}).get(source_id, [])
        )
    ]
    requirement_ids.update(value["source_id"] for value in response_forms)
    student_profile = asdict(StudentProfile.load(settings.data_dir))
    supplement = load_supplement(settings.data_dir, course_id, assignment_id)
    required_files = set()
    for original, origins in course.get("attachment_origins", {}).items():
        if any(x.get("assignment_id") == assignment_id for x in origins):
            resolved = course["attachment_resolutions"].get(original)
            if resolved:
                required_files.add("file:" + resolved)
    own_files = set()
    if submission:
        requirement_ids.add("submission:" + submission.get("id", submission["courseWorkId"]))
        # The student's own worksheet is a primary source of questions, not optional background.
        for file in drive_attachments(submission):
            resolved = course.get("attachment_resolutions", {}).get(file["id"])
            if resolved and any(x["source_id"] == "file:" + resolved for x in chunks):
                requirement_ids.add("file:" + resolved)
                own_files.add(resolved)
    if not own_files:
        for material in assignment.get("materials", []):
            if material.get("driveFile", {}).get("shareMode") == "STUDENT_COPY":
                for file in drive_attachments({"materials": [material]}):
                    resolved = course.get("attachment_resolutions", {}).get(file["id"])
                    if resolved:
                        requirement_ids.add("file:" + resolved)
    requirements = [x for x in chunks if x["source_id"] in requirement_ids]
    query = (assignment.get("title", "") + " " + assignment.get("description", ""))[:6000]
    related = retrieve([x for x in chunks if x not in requirements], query, 24)
    policy_sources = retrieve(
        chunks, "AI 生成AI 人工知能 ChatGPT 使用 禁止 レポート 規則 シラバス", 12
    )
    extra = [source for query in extra_queries for source in retrieve(chunks, query, 16)]
    mandatory = [x for x in chunks if x["source_id"] in required_files]
    # Keep the entire current readable course corpus searchable in the local package.
    # Rankings guide reading; a lexical top-k cutoff must not silently hide a syllabus or worksheet.
    evidence = list(
        {
            x["id"]: x
            for x in [*requirements, *mandatory, *related, *policy_sources, *extra, *chunks]
        }.values()
    )
    source_index = []
    for source_id in dict.fromkeys(x["source_id"] for x in evidence):
        group = [x for x in evidence if x["source_id"] == source_id]
        parents = [
            x
            for original, values in course.get("attachment_origins", {}).items()
            if "file:" + course.get("attachment_resolutions", {}).get(original, "") == source_id
            for x in values
        ]
        parents.extend(course.get("form_origins", {}).get(source_id, []))
        source_index.append(
            {
                "source_id": source_id,
                "title": group[0]["title"],
                "url": group[0]["url"],
                "locators": [x["locator"] for x in group],
                "evidence_ids": [x["id"] for x in group],
                "required": source_id in required_files or source_id in requirement_ids,
                "parents": parents,
            }
        )
    identity = fingerprint(
        {
            "assignment": assignment,
            "evidence": evidence,
            "policy": course["policy"],
            "warnings": course["warnings"],
            "schema": 2,
            "source_index": source_index,
            "downloads": course.get("downloads", {}),
            "response_forms": response_forms,
            "student_profile": student_profile,
            "supplement": supplement,
        }
    )
    path = settings.data_dir / "review-packages" / course_id / assignment_id / identity[:24]

    def package(_key):
        path.mkdir(parents=True, exist_ok=True)
        questions = [
            "在主界面勾选是否使用 AI；课程 AI 规定只保留为说明。",
            "个人经历、课堂参与和调查结果仅使用你提供的真实事实。",
        ]
        if not course["policy"]["can_draft"]:
            questions.insert(0, "需要 AI 作答时勾选主界面的 AI 使用选项。")
        if course["warnings"]:
            questions.append("部分资料未处理或需目视核对，详见 manifest.json 的 warnings。")
        manifest = {
            "schema": 2,
            "status": "prepared",
            "created_at": utc_now(),
            "course_id": course_id,
            "assignment_id": assignment_id,
            "assignment": assignment,
            "student_submission": submission,
            "student_profile": student_profile,
            "supplement": supplement,
            "response_forms": response_forms,
            "source_index": source_index,
            "downloads": list(course.get("downloads", {}).values()),
            "recommended_evidence_ids": list(
                dict.fromkeys(
                    x["id"] for x in [*requirements, *mandatory, *extra, *related, *policy_sources]
                )
            ),
            "course_name": course["course"].get("name"),
            "policy": course["policy"],
            "warnings": course["warnings"],
            "requirement_source_ids": [x["id"] for x in requirements],
            "evidence_ids": [x["id"] for x in evidence],
            "fingerprint": identity,
            "stop_after": "local_review_package",
            "external_content_is_untrusted": True,
        }
        atomic_json(path / "manifest.json", manifest)
        atomic_json(path / "evidence.json", {"sources": evidence, "synced_at": course["synced_at"]})
        atomic_json(
            path / "requirements.json",
            {
                "original_assignment": assignment,
                "student_submission": submission,
                "response_forms": response_forms,
                "sources": requirements,
            },
        )
        (path / "questions.md").write_text(
            "\n".join("- " + x for x in questions) + "\n", encoding="utf-8"
        )
        (path / "checklist.md").write_text(
            "尚未逐项检查作业要求。\n",
            encoding="utf-8",
        )
        immutable = [
            path / name for name in ("manifest.json", "evidence.json", "requirements.json")
        ]
        return {
            "package": str(path),
            "can_draft": course["policy"]["can_draft"],
            "status": "prepared",
            "artifacts": [artifact(x) for x in immutable],
        }

    return store.memo("review-preparation", identity, package)


def prepare(
    settings: Settings,
    *,
    due_before: str | None = None,
    include_no_due=False,
    target: tuple[str, str] | None = None,
    targets: list[tuple[str, str]] | None = None,
    progress=None,
    extra_queries=(),
    on_assignment=None,
) -> dict:
    """Public core entry point. CLI/UI adapters can call this without argparse or MCP."""
    settings = settings.validated()
    if targets is not None and (target is not None or due_before is not None):
        raise ConfigurationError("Select specific assignments or a date cutoff, not both.")
    reader = KnowledgeClient.from_settings(settings)
    with Store(settings.data_dir) as store:
        if target or targets is not None:
            requested = [target] if target else targets
            if (
                not requested
                or len(requested) > 200
                or any(
                    len(item) != 2 or not all(isinstance(x, str) and x.isdigit() for x in item)
                    for item in requested
                )
            ):
                raise ConfigurationError("Select 1–200 actual numeric course/assignment pairs.")
            requested = list(dict.fromkeys(tuple(item) for item in requested))
            states, own_submissions = {}, {}
            for course_id, assignment_id in requested:
                if course_id not in states:
                    if progress:
                        progress(f"正在重新核对选中作业的提交状态：{course_id}")
                    own_submissions[course_id] = reader.own_submissions(course_id)
                    states[course_id] = {
                        x["courseWorkId"]: x.get("state") for x in own_submissions[course_id]
                    }
                if states[course_id].get(assignment_id) not in PENDING_STATES:
                    raise WorkflowError(
                        "Selected assignment is no longer confirmed pending; refresh the list before retrying."
                    )
            selection = {
                "assignments": [{"course_id": cid, "assignment_id": aid} for cid, aid in requested],
                "complete_discovery": True,
                "needs_confirmation": [],
                "inaccessible_courses": [],
            }
        else:
            own_submissions = {}
            if not due_before:
                raise WorkflowError("Batch processing requires an explicit local cutoff date.")
            selection = discover_pending(
                reader,
                due_before=due_before,
                timezone=settings.timezone,
                include_no_due=include_no_due,
            )
        atomic_json(settings.data_dir / "latest-selection.json", selection)
        packages, failures, courses = [], [], {}
        for item in selection["assignments"]:
            cid = item["course_id"]
            try:
                if progress:
                    progress(f"正在准备选中作业：{cid}/{item['assignment_id']}")
                if on_assignment:
                    on_assignment(item, {"status": "preparing"})
                if cid not in courses:
                    if progress:
                        report(progress, "正在同步同课程资料、历史作业与公告", "course_sync")
                    if cid not in own_submissions:
                        own_submissions[cid] = reader.own_submissions(cid)
                    selected_ids = {
                        x["assignment_id"]
                        for x in selection["assignments"]
                        if x["course_id"] == cid
                    }
                    courses[cid] = sync_course(
                        reader,
                        settings,
                        store,
                        cid,
                        progress=progress,
                        submissions=[
                            x for x in own_submissions[cid] if x["courseWorkId"] in selected_ids
                        ],
                    )
                result = prepare_assignment(
                    settings, store, courses[cid], item["assignment_id"], extra_queries
                )
                packages.append(
                    {"course_id": cid, "assignment_id": item["assignment_id"], **result}
                )
                if on_assignment:
                    on_assignment(item, result)
            except RunCancelled:
                raise
            except WorkflowError as exc:
                failures.append({**item, "error": str(exc)})
                if on_assignment:
                    on_assignment(item, {"status": "failed", "error": str(exc)})
        batch = {
            "status": "prepared" if not failures else "partial",
            "finished_at": utc_now(),
            "due_before_inclusive": due_before,
            "timezone": settings.timezone,
            "packages": packages,
            "selection": selection,
            "failures": failures,
            "cache": store.stats,
            "note": "The model drafts each package and local code validates it. Nothing was submitted.",
        }
        atomic_json(settings.data_dir / "latest-batch.json", batch)
        return batch
