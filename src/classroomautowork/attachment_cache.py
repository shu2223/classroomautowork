"""Reuse permitted binary content across Drive metadata-only version changes."""

import hashlib
import re
from pathlib import Path

from .store import fingerprint


def content_signature(metadata):
    mime = metadata.get("mimeType", "")
    checksum = metadata.get("md5Checksum", "")
    size = str(metadata.get("size", ""))
    if (
        mime.startswith("application/vnd.google-apps.")
        or not isinstance(checksum, str)
        or not re.fullmatch(r"[a-fA-F0-9]{32}", checksum)
        or not size.isdigit()
        or int(size) <= 0
    ):
        return None
    return {
        "id": metadata["id"],
        "mimeType": mime,
        "size": int(size),
        "md5Checksum": checksum.lower(),
    }


def download_revision(metadata):
    signature = content_signature(metadata)
    return fingerprint(
        ["binary-content-v1", signature]
        if signature
        else {k: metadata.get(k) for k in ("id", "version", "mimeType", "size", "md5Checksum")}
    )


def legacy_download(store, metadata):
    """Only completed, intact bytes matching live Drive content qualify for migration."""
    signature = content_signature(metadata)
    if not signature:
        return None
    suffix = Path(metadata["name"]).suffix
    if not re.fullmatch(r"\.[a-zA-Z0-9]{1,12}", suffix):
        suffix = ".bin"
    filename = hashlib.sha256(metadata["id"].encode()).hexdigest()[:24] + suffix

    def matches(result):
        path = Path(result.get("path", "")).resolve()
        if (
            result.get("exported_pdf")
            or path.name != filename
            or not path.is_relative_to(store.root / "attachments")
            or not path.is_file()
            or path.stat().st_size != signature["size"]
            or not any(Path(x["path"]).resolve() == path for x in result.get("artifacts", []))
        ):
            return False
        digest = hashlib.md5(usedforsecurity=False)
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == signature["md5Checksum"]

    return store.find_cached("drive-download", matches)
