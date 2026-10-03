"""Real connection checkpoint. Its CLI calls live APIs and has no simulation mode."""

from pathlib import Path

from .auth import credentials_for
from .config import Settings
from .errors import WorkflowError
from .google_read import GoogleReader, drive_attachments
from .local import atomic_json, sha256_file, utc_now
from .pending import discover_pending


def verify_connection(settings: Settings, *, attachment_id: str | None = None) -> dict:
    settings = settings.validated()
    receipt_path = settings.data_dir / "connection-receipt.json"
    # Invalidate an old success before attempting a new verification.
    atomic_json(receipt_path, {"status": "in_progress", "started_at": utc_now()})
    try:
        credentials, identity = credentials_for(settings)
        reader = GoogleReader(credentials)
        course_id, assignment_id = settings.course_id, settings.assignment_id
        if not course_id:
            pending = discover_pending(reader, timezone=settings.timezone, include_no_due=True)
            atomic_json(settings.data_dir / "verification" / "pending.json", pending)
            if not pending["assignments"]:
                raise WorkflowError(
                    "No readable pending assignment found. Specify a real assignment for verification."
                )
            chosen = pending["assignments"][0]
            course_id, assignment_id = chosen["course_id"], chosen["assignment_id"]
        course = reader.course(course_id)
        assignment = reader.assignment(course_id, assignment_id)
        atomic_json(settings.data_dir / "verification" / "course.json", course)
        atomic_json(settings.data_dir / "verification" / "assignment.json", assignment)
        candidates = [
            {**x, "source_type": "coursework", "source_id": assignment_id}
            for x in drive_attachments(assignment)
        ]
        failures = []
        # A course reading can prove authorized attachment download even if the assignment has none.
        try:
            materials = reader.course_materials(course_id)
            atomic_json(
                settings.data_dir / "verification" / "course-materials.json", {"items": materials}
            )
            for material in materials:
                candidates.extend(
                    {**x, "source_type": "coursework_material", "source_id": material["id"]}
                    for x in drive_attachments(material)
                )
        except WorkflowError as exc:
            failures.append({"resource": "course_materials", "error": str(exc)})
        candidates = list({x["id"]: x for x in candidates}.values())
        if attachment_id:
            candidates = [x for x in candidates if x["id"] == attachment_id]
        for candidate in candidates:
            try:
                metadata = reader.file_metadata(candidate["id"])
                downloaded = reader.download(
                    metadata, settings.data_dir / "verification" / "attachments"
                )
                # Check again to avoid accepting content changed during the transfer.
                after = reader.file_metadata(candidate["id"])
                if not metadata.get("version") or metadata["version"] != after.get("version"):
                    raise WorkflowError("Attachment changed during verification. Retry.")
                receipt = {
                    "status": "verified",
                    "verified_at": utc_now(),
                    "identity": identity,
                    "course_id": course_id,
                    "assignment_id": assignment_id,
                    "assignment_title": assignment.get("title"),
                    "assignment_url": assignment.get("alternateLink"),
                    "assignment_snapshot_sha256": sha256_file(
                        settings.data_dir / "verification" / "assignment.json"
                    ),
                    "attachment": {
                        "id": metadata["id"],
                        "name": metadata["name"],
                        "version": metadata.get("version"),
                        "source_type": candidate["source_type"],
                        "source_id": candidate["source_id"],
                        "can_download": True,
                        **downloaded,
                        "sha256": sha256_file(Path(downloaded["path"])),
                    },
                    "proof": "Live OAuth identity + Classroom GET + Drive metadata and media/export requests",
                }
                atomic_json(receipt_path, receipt)
                return receipt
            except WorkflowError as exc:
                failures.append({"attachment_id": candidate["id"], "error": str(exc)})
        atomic_json(
            settings.data_dir / "verification" / "attachment-failures.json", {"failures": failures}
        )
        raise WorkflowError(
            "Assignment read succeeded, but no permitted course attachment was downloaded. Verification is incomplete."
        )
    except Exception as exc:
        atomic_json(
            receipt_path,
            {
                "status": "failed",
                "failed_at": utc_now(),
                "error": str(exc)
                if isinstance(exc, WorkflowError)
                else "Unexpected local error; no secrets logged.",
            },
        )
        raise
