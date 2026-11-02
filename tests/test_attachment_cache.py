"""Offline cache regression cases; these do not count as OAuth connection evidence."""

import hashlib
import json
from pathlib import Path

import pytest

from classroomautowork.attachment_cache import download_revision, legacy_download
from classroomautowork.store import Store, artifact


class BinaryReader:
    def __init__(self):
        self.content = "食品安全 unchanged lecture".encode()
        self.version = "1"
        self.denied = False
        self.downloads = 0
        self.metadata_reads = 0

    def course(self, course_id):
        return {"id": course_id, "name": "Offline course"}

    def assignments(self, _):
        return [
            {
                "id": "2",
                "title": "Offline assignment",
                "materials": [{"driveFile": {"driveFile": {"id": "unit-file-01"}}}],
            }
        ]

    def course_materials(self, _):
        return []

    def announcements(self, _):
        return []

    def file_metadata(self, file_id, _resource_key=None):
        self.metadata_reads += 1
        return {
            "id": file_id,
            "name": "lecture.txt",
            "mimeType": "text/plain",
            "version": self.version,
            "size": str(len(self.content)),
            "md5Checksum": hashlib.md5(self.content, usedforsecurity=False).hexdigest(),
            "capabilities": {"canDownload": not self.denied},
        }

    def download(self, metadata, destination, *, progress=None):
        self.downloads += 1
        return self.save(metadata, destination)

    def save(self, metadata, destination):
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / (hashlib.sha256(metadata["id"].encode()).hexdigest()[:24] + ".txt")
        path.write_bytes(self.content)
        return {"path": str(path), "artifacts": [artifact(path)]}


@pytest.mark.parametrize(
    "invalid", ["changed_bytes", "bad_sha", "missing", "partial", "running", "wrong_id"]
)
def test_legacy_migration_rejects_invalid_or_incomplete_files(tmp_path, invalid):
    reader = BinaryReader()
    metadata = reader.file_metadata("unit-file-01")
    with Store(tmp_path) as store:
        saved_metadata = {**metadata, "id": "other-file-01"} if invalid == "wrong_id" else metadata
        result = store.memo(
            "drive-download",
            "legacy",
            lambda key: reader.save(saved_metadata, tmp_path / "attachments" / key[:24]),
        )
        path = Path(result["path"])
        if invalid == "changed_bytes":
            # Keep the size equal and update the stored SHA so live MD5 must catch the change.
            path.write_bytes(b"x" * len(reader.content))
            store.db.execute(
                "UPDATE tasks SET result=?",
                (json.dumps({"path": str(path), "artifacts": [artifact(path)]}),),
            )
        elif invalid == "bad_sha":
            result["artifacts"][0]["sha256"] = "0" * 64
            store.db.execute("UPDATE tasks SET result=?", (json.dumps(result),))
        elif invalid == "missing":
            path.unlink()
        elif invalid == "partial":
            path.rename(path.with_suffix(".txt.part"))
        elif invalid == "running":
            store.db.execute("UPDATE tasks SET status='running'")
        store.db.commit()
        assert legacy_download(store, metadata) is None


def test_native_or_unverifiable_content_retains_version_invalidation():
    reader = BinaryReader()
    metadata = reader.file_metadata("unit-file-01")
    changed_version = {**metadata, "version": "2"}
    assert download_revision(metadata) == download_revision(changed_version)
    for extra in (
        {"mimeType": "application/vnd.google-apps.document"},
        {"md5Checksum": None},
        {"md5Checksum": "not-a-checksum"},
        {"size": None},
    ):
        assert download_revision({**metadata, **extra}) != download_revision(
            {**changed_version, **extra}
        )
