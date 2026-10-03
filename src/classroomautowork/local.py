"""Private local paths and atomic writes. No course text is executable configuration."""

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .errors import ConfigurationError


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def state_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    return (
        Path(base) / "classroomautowork" if base else Path.home() / ".local/share/classroomautowork"
    )


def require_private_path(path: Path) -> Path:
    """Reject paths within Git checkouts, even when .gitignore would hide them."""
    resolved = path.expanduser().resolve()
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists():
            raise ConfigurationError(
                "Private settings, credentials and materials must be outside Git."
            )
    return resolved


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
