"""Explicit read-only API surface. External material never selects an API method."""

import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from httplib2 import HttpLib2Error

from .errors import PermissionDenied, WorkflowError
from .local import atomic_json


def retry_read(operation, attempts: int = 4):
    for attempt in range(attempts):
        try:
            return operation()
        except HttpError as exc:
            status = exc.resp.status
            reasons = exc.error_details or []
            rate_limited = any(
                isinstance(x, dict)
                and x.get("reason") in {"rateLimitExceeded", "userRateLimitExceeded"}
                for x in reasons
            )
            if status not in {429, 500, 502, 503, 504} and not (status == 403 and rate_limited):
                if status in {401, 403, 404}:
                    raise PermissionDenied(
                        f"Google read request denied (HTTP {status}); check account, API enablement and school policy."
                    ) from exc
                raise WorkflowError(f"Google read request failed (HTTP {status}).") from exc
            if attempt == attempts - 1:
                raise WorkflowError(
                    f"Google read exhausted {attempts} attempts (HTTP {status})."
                ) from exc
        except (TimeoutError, ConnectionError, HttpLib2Error) as exc:
            if attempt == attempts - 1:
                raise WorkflowError("Google connection failed after bounded retries.") from exc
        time.sleep(min(2**attempt, 8))


class GoogleReader:
    def __init__(self, credentials):
        self._classroom = build(
            "classroom",
            "v1",
            http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=60)),
            cache_discovery=False,
        )
        self._drive = build(
            "drive",
            "v3",
            http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=60)),
            cache_discovery=False,
        )

    @staticmethod
    def _pages(factory, key: str, **parameters) -> list[dict]:
        items, token = [], None
        while True:
            request = factory(pageToken=token, pageSize=100, **parameters)
            page = retry_read(request.execute)
            items.extend(page.get(key, []))
            token = page.get("nextPageToken")
            if not token:
                return items

    def courses(self) -> list[dict]:
        return self._pages(
            self._classroom.courses().list, "courses", studentId="me", courseStates=["ACTIVE"]
        )

    def assignments(self, course_id: str) -> list[dict]:
        return self._pages(
            self._classroom.courses().courseWork().list,
            "courseWork",
            courseId=course_id,
            courseWorkStates=["PUBLISHED"],
        )

    def own_submissions(self, course_id: str) -> list[dict]:
        return self._pages(
            self._classroom.courses().courseWork().studentSubmissions().list,
            "studentSubmissions",
            courseId=course_id,
            courseWorkId="-",
            userId="me",
        )

    def course_materials(self, course_id: str) -> list[dict]:
        return self._pages(
            self._classroom.courses().courseWorkMaterials().list,
            "courseWorkMaterial",
            courseId=course_id,
        )

    def announcements(self, course_id: str) -> list[dict]:
        return self._pages(
            self._classroom.courses().announcements().list, "announcements", courseId=course_id
        )

    def course(self, course_id: str) -> dict:
        return retry_read(lambda: self._classroom.courses().get(id=course_id).execute())

    def assignment(self, course_id: str, assignment_id: str) -> dict:
        return retry_read(
            lambda: (
                self._classroom.courses()
                .courseWork()
                .get(courseId=course_id, id=assignment_id)
                .execute()
            )
        )

    def personal_document_metadata(self, file_id: str) -> dict:
        return retry_read(
            lambda: (
                self._drive.files()
                .get(
                    fileId=file_id,
                    supportsAllDrives=True,
                    fields="id,mimeType,ownedByMe,owners(emailAddress),capabilities(canEdit)",
                )
                .execute()
            )
        )

    def file_metadata(self, file_id: str, resource_key: str | None = None) -> dict:
        request = self._drive.files().get(
            fileId=file_id,
            supportsAllDrives=True,
            fields="id,name,mimeType,size,md5Checksum,modifiedTime,version,webViewLink,capabilities(canDownload),resourceKey,shortcutDetails",
        )
        if resource_key:
            request.headers["X-Goog-Drive-Resource-Keys"] = file_id + "/" + resource_key
        return retry_read(request.execute)

    def download(self, metadata: dict, destination: Path) -> dict:
        if not metadata.get("capabilities", {}).get("canDownload"):
            raise PermissionDenied(
                "Drive capabilities.canDownload is not true; attachment skipped."
            )
        mime = metadata["mimeType"]
        if mime.startswith("application/vnd.google-apps."):
            if mime not in {
                "application/vnd.google-apps.document",
                "application/vnd.google-apps.presentation",
                "application/vnd.google-apps.spreadsheet",
            }:
                raise PermissionDenied(
                    "This Google-native attachment cannot be exported by version 1."
                )
            request = self._drive.files().export_media(
                fileId=metadata["id"], mimeType="application/pdf"
            )
            suffix = ".pdf"
        else:
            request = self._drive.files().get_media(fileId=metadata["id"], supportsAllDrives=True)
            suffix = Path(metadata["name"]).suffix
            if not re.fullmatch(r"\.[a-zA-Z0-9]{1,12}", suffix):
                suffix = ".bin"
        if metadata.get("resourceKey"):
            request.headers["X-Goog-Drive-Resource-Keys"] = (
                metadata["id"] + "/" + metadata["resourceKey"]
            )
        # Names and IDs from the course do not become filesystem paths.
        identity = hashlib.sha256(metadata["id"].encode()).hexdigest()[:24]
        destination.mkdir(parents=True, exist_ok=True)
        output = destination / (identity + suffix)
        temporary = output.with_suffix(output.suffix + ".part")
        checkpoint = temporary.with_suffix(temporary.suffix + ".json")
        revision = {
            k: metadata.get(k) for k in ("id", "version", "mimeType", "size", "md5Checksum")
        }
        resume = False
        if (
            temporary.exists()
            and checkpoint.exists()
            and not mime.startswith("application/vnd.google-apps.")
        ):
            try:
                resume = json.loads(checkpoint.read_text(encoding="utf-8")) == revision
            except ValueError:
                pass
        if not resume:
            temporary.unlink(missing_ok=True)
        atomic_json(checkpoint, revision)
        try:
            with temporary.open("ab" if resume else "wb") as stream:
                downloader = MediaIoBaseDownload(stream, request, chunksize=1024 * 1024)
                # google-api-python-client 2.x supports Range reads using this offset.
                # The offset is only restored when the exact Drive revision matches.
                downloader._progress = temporary.stat().st_size if resume else 0
                done = bool(
                    resume
                    and metadata.get("size")
                    and downloader._progress == int(metadata["size"])
                )
                while not done:
                    _, done = retry_read(lambda: downloader.next_chunk(num_retries=0))
                    stream.flush()
            if temporary.stat().st_size == 0:
                raise WorkflowError("Attachment download returned an empty file.")
            if not mime.startswith("application/vnd.google-apps."):
                if "size" in metadata and temporary.stat().st_size != int(metadata["size"]):
                    temporary.unlink(missing_ok=True)
                    raise WorkflowError("Attachment download size does not match Drive metadata.")
                if metadata.get("md5Checksum"):
                    digest = hashlib.md5(usedforsecurity=False)
                    with temporary.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    if digest.hexdigest() != metadata["md5Checksum"]:
                        temporary.unlink(missing_ok=True)
                        raise WorkflowError("Attachment checksum does not match Drive metadata.")
            os.replace(temporary, output)
            checkpoint.unlink(missing_ok=True)
            return {
                "path": str(output),
                "bytes": output.stat().st_size,
                "exported_pdf": suffix == ".pdf"
                and mime.startswith("application/vnd.google-apps."),
            }
        except (WorkflowError, OSError):
            # Keep revision-checked partial bytes for the next run; never mark them done.
            raise


def drive_link(url: str) -> dict | None:
    """Parse a reference, never fetch a URL supplied by course text."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname not in {"drive.google.com", "docs.google.com"}:
        return None
    match = re.search(
        r"/(?:file|document|presentation|spreadsheets)/d/([A-Za-z0-9_-]+)", parsed.path
    )
    query = parse_qs(parsed.query)
    identity = match[1] if match else query.get("id", [""])[0]
    if re.fullmatch(r"[A-Za-z0-9_-]{10,200}", identity):
        return {"id": identity, "resourceKey": query.get("resourcekey", [None])[0]}
    return None


def attachment_metadata(reader, reference: dict) -> dict:
    """Resolve the authenticated ID or its actual Google link, including Drive shortcuts."""
    candidates = [(reference["id"], reference.get("resourceKey"))]
    alternate = drive_link(reference.get("alternateLink", ""))
    if alternate and alternate["id"] != reference["id"]:
        candidates.append((alternate["id"], alternate.get("resourceKey")))
    last_error = None
    for identity, resource_key in candidates:
        try:
            metadata = reader.file_metadata(identity, resource_key)
            seen = {identity}
            for _ in range(3):
                if metadata.get("mimeType") != "application/vnd.google-apps.shortcut":
                    return metadata
                target = metadata.get("shortcutDetails", {})
                identity = target.get("targetId")
                if not identity or identity in seen:
                    raise WorkflowError("Drive shortcut has a missing or circular target.")
                seen.add(identity)
                metadata = reader.file_metadata(identity, target.get("targetResourceKey"))
            raise WorkflowError("Drive shortcut resolution exceeded the bounded limit.")
        except PermissionDenied as exc:
            last_error = exc
    raise last_error or WorkflowError("No authenticated attachment reference could be read.")


def drive_attachments(item: dict):
    materials = [
        *item.get("materials", []),
        *item.get("assignmentSubmission", {}).get("attachments", []),
    ]
    for material in materials:
        wrapper = material.get("driveFile", {})
        file = wrapper.get("driveFile", wrapper)
        if file and file.get("id"):
            yield file
        link = material.get("link", {})
        if reference := drive_link(link.get("url", "")):
            yield {**reference, "title": link.get("title")}
    for url in re.findall(
        r"https://[^\s<>\"']+", item.get("description", "") + " " + item.get("text", "")
    ):
        if reference := drive_link(url.rstrip("。，、）)]")):
            yield reference
