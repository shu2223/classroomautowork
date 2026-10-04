"""Range transport fixtures; no real connection evidence is produced."""

import hashlib
from types import SimpleNamespace

from httplib2 import Response

from classroomautowork.google_read import GoogleReader
from classroomautowork.local import atomic_json


def test_partial_download_resumes_only_matching_drive_revision(tmp_path):
    content = b"unit-data-for-range"
    offsets = []

    def request(uri, method, headers):
        start = int(headers["range"].split("=")[1].split("-")[0])
        offsets.append(start)
        return Response(
            {"status": "206", "content-range": f"bytes {start}-{len(content) - 1}/{len(content)}"}
        ), content[start:]

    http = SimpleNamespace(request=request)
    media = SimpleNamespace(
        uri="https://www.googleapis.com/drive/v3/files/unit?alt=media", headers={}, http=http
    )
    files = SimpleNamespace(get_media=lambda **_: media)
    reader = GoogleReader.__new__(GoogleReader)
    reader._drive = SimpleNamespace(files=lambda: files)
    meta = {
        "id": "unit",
        "version": "1",
        "name": "unit.txt",
        "mimeType": "text/plain",
        "size": str(len(content)),
        "md5Checksum": hashlib.md5(content, usedforsecurity=False).hexdigest(),
        "capabilities": {"canDownload": True},
    }
    destination = tmp_path / "attachments"
    destination.mkdir()
    identity = hashlib.sha256(b"unit").hexdigest()[:24]
    part = destination / (identity + ".txt.part")
    part.write_bytes(content[:5])
    checkpoint = part.with_suffix(part.suffix + ".json")
    atomic_json(
        checkpoint, {k: meta.get(k) for k in ("id", "version", "mimeType", "size", "md5Checksum")}
    )
    events = []
    result = reader.download(meta, destination, progress=events.append)
    from pathlib import Path

    assert Path(result["path"]).read_bytes() == content
    assert offsets == [5] and not part.exists() and not checkpoint.exists()
    measured = [x.details for x in events if x.details["stage"] == "download"]
    assert measured[0]["current"] == 5 and measured[-1]["current"] == len(content)
    assert all(x["total"] == len(content) for x in measured)
    assert events[-1].details["stage"] == "download_verify"
    part.write_bytes(b"old revision bytes")
    atomic_json(checkpoint, {"id": "unit", "version": "obsolete"})
    result = reader.download(meta, destination)
    assert offsets[-1] == 0
    assert Path(result["path"]).read_bytes() == content
