"""Callable orchestration: live read, incremental processing, retrieval, review handoff.

The only remote adapter is GoogleReader's explicit read-only surface. Course text never
becomes a command, executable path, configuration, scope, approval or account selection.
"""

import json
from dataclasses import asdict
from pathlib import Path

from .auth import credentials_for
from .buzz import BuzzConfig, transcribe_media
from .config import Settings
from .errors import PermissionDenied, WorkflowError
from .extract import extract_document, processor_identity
from .google_read import GoogleReader, drive_attachments
from .local import atomic_json, sha256_file, utc_now
from .pending import PENDING_STATES, discover_pending
from .policy import CoursePolicy
from .search import retrieve
from .store import Store, artifact, fingerprint

MEDIA = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".mp3", ".wav", ".m4a", ".ogg", ".flac"}


def stable_source(value):
    """Google rotates thumbnail URLs without changing teaching content."""
    if isinstance(value, dict):
        return {key: stable_source(item) for key, item in value.items() if key != "thumbnailUrl"}
    if isinstance(value, list):
        return [stable_source(item) for item in value]
    return value


def require_connection(settings: Settings):
    path = settings.data_dir / "connection-receipt.json"
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        attachment = Path(receipt["attachment"]["path"])
        valid = (
            receipt["status"] == "verified"
            and receipt["identity"]["email"].lower() == settings.school_email
            and attachment.is_relative_to(settings.data_dir)
            and attachment.is_file()
            and sha256_file(attachment) == receipt["attachment"]["sha256"]
        )
    except (OSError, KeyError, ValueError, TypeError):
        valid = False
    if not valid:
        raise PermissionDenied(
            "Real connection checkpoint is incomplete; run auth and verify first."
        )


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


def sync_course(
    reader, settings: Settings, store: Store, course_id: str, *, defer_media=False, progress=None
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
        try:
            records.extend((kind, stable_source(item)) for item in fetch(course_id))
        except WorkflowError as exc:
            warnings.append({"source": kind, "error": str(exc)})
    attachments = {}
    for kind, item in records:
        source_id = kind + ":" + item["id"]
        revision = fingerprint(item)

        def snapshot(key, source=item):
            path = settings.data_dir / "snapshots" / key[:24] / "source.json"
            atomic_json(path, source)
            return {"path": str(path), "artifacts": [artifact(path)]}

        store.memo("classroom-snapshot", [course_id, source_id, revision], snapshot)
        store.replace_chunks(course_id, source_id, revision, text_chunks(item, kind))
        readable.add(source_id)
        for file in drive_attachments(item):
            attachments[file["id"]] = file
    for file_id, reference in attachments.items():
        try:
            if progress:
                progress(f"Checking attachment {file_id}")
            metadata = reader.file_metadata(file_id, reference.get("resourceKey"))
            if not metadata.get("capabilities", {}).get("canDownload"):
                raise PermissionDenied(
                    "Download permission is unavailable; no cached attachment used."
                )
            if int(metadata.get("size", "0")) > policy.max_attachment_bytes:
                raise WorkflowError(
                    "Attachment exceeds this course's download budget; increase max_attachment_bytes to process it."
                )
            suffix = Path(metadata["name"]).suffix.lower()
            if defer_media and (
                suffix in MEDIA or metadata["mimeType"].startswith(("video/", "audio/"))
            ):
                raise WorkflowError("Media processing explicitly deferred for this run.")
            if not metadata.get("version"):
                raise WorkflowError(
                    "Drive revision is unavailable; cannot establish an incremental cache key."
                )
            revision = fingerprint(
                {k: metadata.get(k) for k in ("id", "version", "mimeType", "size", "md5Checksum")}
            )

            def download(key, meta=metadata):
                result = reader.download(meta, settings.data_dir / "attachments" / key[:24])
                return {**result, "artifacts": [artifact(Path(result["path"]))]}

            raw = store.memo("drive-download", revision, download)
            after = reader.file_metadata(file_id, metadata.get("resourceKey"))
            if metadata["version"] != after.get("version") or not after.get("capabilities", {}).get(
                "canDownload"
            ):
                raise WorkflowError(
                    "Attachment changed or permission was removed during processing; rerun."
                )
            path = Path(raw["path"])
            media = path.suffix.lower() in MEDIA or metadata["mimeType"].startswith(
                ("video/", "audio/")
            )
            config = None
            if media:
                config = BuzzConfig.load()
                # Course language is trusted local config, not fetched course text.
                from dataclasses import replace

                config = replace(config, language=policy.language).validated()
                processor = {**asdict(config), "backend": "buzz-whispercpp-v1"}
            else:
                processor = processor_identity(policy)
            content_hash = raw["artifacts"][0]["sha256"]

            def process(key, file=path, is_media=media, buzz_config=config):
                destination = settings.data_dir / "processed" / key[:24]
                return (
                    transcribe_media(file, destination, store, buzz_config)
                    if is_media
                    else extract_document(file, destination, policy)
                )

            result = store.memo(
                "media" if media else "document", [content_hash, processor], process
            )
            source_id = "file:" + file_id
            chunks = [
                {
                    **x,
                    "source_kind": "transcript" if media else "document",
                    "title": metadata["name"],
                    "url": metadata.get("webViewLink"),
                }
                for x in result["chunks"]
            ]
            store.replace_chunks(course_id, source_id, fingerprint([revision, processor]), chunks)
            readable.add(source_id)
            warnings.extend({"source": source_id, "error": x} for x in result.get("warnings", []))
        except WorkflowError as exc:
            warnings.append({"source": "file:" + file_id, "error": str(exc)})
    store.prune(course_id, readable)
    return {
        "course": course,
        "policy": policy.draft_gate(),
        "warnings": warnings,
        "assignment_sources": {item["id"]: item for kind, item in records if kind == "coursework"},
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
    requirements = [x for x in chunks if x["source_id"] == "coursework:" + assignment_id]
    query = (assignment.get("title", "") + " " + assignment.get("description", ""))[:6000]
    related = retrieve([x for x in chunks if x not in requirements], query, 24)
    policy_sources = retrieve(
        chunks, "AI 生成AI 人工知能 ChatGPT 使用 禁止 レポート 規則 シラバス", 12
    )
    extra = [source for query in extra_queries for source in retrieve(chunks, query, 16)]
    evidence = list(
        {x["id"]: x for x in [*requirements, *related, *policy_sources, *extra]}.values()
    )
    identity = fingerprint(
        {
            "assignment": assignment,
            "evidence": evidence,
            "policy": course["policy"],
            "warnings": course["warnings"],
        }
    )
    path = settings.data_dir / "review-packages" / course_id / assignment_id / identity[:24]

    def package(_key):
        path.mkdir(parents=True, exist_ok=True)
        questions = [
            "请核对作业和课程中的 AI 使用规则。",
            "个人经历、课堂参与和调查结果仅使用你提供的真实事实。",
        ]
        if not course["policy"]["can_draft"]:
            questions.insert(0, "课程 AI 规则未知或禁止代写，当前不能生成作业答案初稿。")
        if course["warnings"]:
            questions.append("部分资料未处理或需目视核对，详见 manifest.json 的 warnings。")
        manifest = {
            "schema": 1,
            "status": "prepared_for_codex",
            "created_at": utc_now(),
            "course_id": course_id,
            "assignment_id": assignment_id,
            "assignment": assignment,
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
            path / "requirements.json", {"original_assignment": assignment, "sources": requirements}
        )
        (path / "questions.md").write_text(
            "\n".join("- " + x for x in questions) + "\n", encoding="utf-8"
        )
        (path / "checklist.md").write_text(
            "Codex 尚未逐项检查作业要求。请先读取 requirements.json，再生成检查结果。\n",
            encoding="utf-8",
        )
        immutable = [
            path / name for name in ("manifest.json", "evidence.json", "requirements.json")
        ]
        return {
            "package": str(path),
            "can_draft": course["policy"]["can_draft"],
            "status": "prepared_for_codex",
            "artifacts": [artifact(x) for x in immutable],
        }

    return store.memo("review-preparation", identity, package)


def prepare(
    settings: Settings,
    *,
    due_before: str | None = None,
    include_no_due=False,
    target: tuple[str, str] | None = None,
    defer_media=False,
    progress=None,
    extra_queries=(),
) -> dict:
    """Public core entry point. CLI/UI adapters can call this without argparse or MCP."""
    settings = settings.validated()
    require_connection(settings)
    credentials, _ = credentials_for(settings)
    reader = GoogleReader(credentials)
    with Store(settings.data_dir) as store:
        if target:
            course_id, assignment_id = target
            own = next(
                (
                    x
                    for x in reader.own_submissions(course_id)
                    if x["courseWorkId"] == assignment_id
                ),
                None,
            )
            if not own or own.get("state") not in PENDING_STATES:
                raise WorkflowError(
                    "Selected assignment is not confirmed pending; inspect its submission state manually."
                )
            selection = {
                "assignments": [{"course_id": course_id, "assignment_id": assignment_id}],
                "complete_discovery": True,
                "needs_confirmation": [],
                "inaccessible_courses": [],
            }
        else:
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
                if cid not in courses:
                    if progress:
                        progress(f"Syncing course {cid}")
                    courses[cid] = sync_course(
                        reader, settings, store, cid, defer_media=defer_media, progress=progress
                    )
                result = prepare_assignment(
                    settings, store, courses[cid], item["assignment_id"], extra_queries
                )
                packages.append(
                    {"course_id": cid, "assignment_id": item["assignment_id"], **result}
                )
            except WorkflowError as exc:
                failures.append({**item, "error": str(exc)})
        batch = {
            "status": "prepared_for_codex" if not failures else "partial",
            "finished_at": utc_now(),
            "due_before_inclusive": due_before,
            "timezone": settings.timezone,
            "packages": packages,
            "selection": selection,
            "failures": failures,
            "cache": store.stats,
            "note": "Codex must review course rules, draft allowed assignments, and finalize locally. Nothing was submitted.",
        }
        atomic_json(settings.data_dir / "latest-batch.json", batch)
        return batch
