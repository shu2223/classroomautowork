"""Offline cache regression cases; these do not count as OAuth connection evidence."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from classroomautowork.attachment_cache import download_revision, legacy_download
from classroomautowork.store import Store, artifact, fingerprint
from classroomautowork.workflow import sync_course


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


def test_retry_reuses_bytes_and_extraction_but_checks_live_permission(tmp_path):
    reader = BinaryReader()
    settings = SimpleNamespace(data_dir=tmp_path)
    events = []
    with Store(tmp_path) as store:
        sync_course(reader, settings, store, "1", progress=events.append)
        first_chunks = store.chunks("1")
        processed = store.stats["processed"]
        reader.version = "2"
        sync_course(reader, settings, store, "1", progress=events.append)
        assert reader.downloads == 1
        assert reader.metadata_reads == 4
        assert store.stats["processed"] == processed
        assert store.chunks("1") == first_chunks
        assert any("无需下载" in str(event) for event in events)

        # The content changed without a corresponding version change.
        reader.content = "食品安全 changed lecture".encode()
        sync_course(reader, settings, store, "1")
        assert reader.downloads == 2
        assert any("changed lecture" in chunk["text"] for chunk in store.chunks("1"))

        reader.denied = True
        result = sync_course(reader, settings, store, "1")
        assert reader.downloads == 2
        assert "no cached attachment" in result["warnings"][0]["error"]
        assert not any(x["source_id"].startswith("file:") for x in store.chunks("1"))


def test_old_completed_download_is_migrated_without_redownloading(tmp_path):
    reader = BinaryReader()
    metadata = reader.file_metadata("unit-file-01")
    old_revision = fingerprint(
        {k: metadata.get(k) for k in ("id", "version", "mimeType", "size", "md5Checksum")}
    )
    with Store(tmp_path) as store:
        old_result = store.memo(
            "drive-download",
            old_revision,
            lambda key: reader.save(metadata, tmp_path / "attachments" / key[:24]),
        )
    reader.version = "9000"
    with Store(tmp_path) as store:
        result = sync_course(reader, SimpleNamespace(data_dir=tmp_path), store, "1")
        assert not result["warnings"]
        assert reader.downloads == 0
        assert store.stats["reused"] == 1
        assert len(list((tmp_path / "attachments").glob("*/*.txt"))) == 1
        current_revision = download_revision(reader.file_metadata("unit-file-01"))
        cached = store.memo(
            "drive-download",
            current_revision,
            lambda _: pytest.fail("Cached content must not trigger a network download"),
        )
        assert cached == old_result


def test_corrupt_binary_cache_is_repaired_without_reextracting_unchanged_content(tmp_path):
    reader = BinaryReader()
    settings = SimpleNamespace(data_dir=tmp_path)
    with Store(tmp_path) as store:
        sync_course(reader, settings, store, "1")
        processed = store.stats["processed"]
        attachment = next((tmp_path / "attachments").glob("*/*.txt"))
        attachment.write_bytes(b"damaged")
        sync_course(reader, settings, store, "1")
        assert reader.downloads == 2
        assert attachment.read_bytes() == reader.content
        assert store.stats["processed"] == processed + 1


def test_metadata_version_churn_during_download_is_not_a_content_race(tmp_path):
    class ChangingVersionReader(BinaryReader):
        def file_metadata(self, *args):
            metadata = super().file_metadata(*args)
            return {**metadata, "version": str(self.metadata_reads)}

    reader = ChangingVersionReader()
    settings = SimpleNamespace(data_dir=tmp_path)
    with Store(tmp_path) as store:
        assert not sync_course(reader, settings, store, "1")["warnings"]
        assert not sync_course(reader, settings, store, "1")["warnings"]
        assert reader.downloads == 1


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
