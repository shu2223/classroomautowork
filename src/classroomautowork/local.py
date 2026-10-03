"""Private local paths and atomic writes. No course text is executable configuration."""

import hashlib
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .errors import ConfigurationError


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def state_candidates(*, windows: bool | None = None, profile: Path | None = None) -> list[Path]:
    """Find physical package paths that also exist outside the Windows MSIX process."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    default = (
        Path(base) / "classroomautowork" if base else Path.home() / ".local/share/classroomautowork"
    )
    roots = []
    is_windows = windows if windows is not None else sys.platform == "win32"
    if is_windows:
        packages = (profile or Path.home()) / "AppData" / "Local" / "Packages"
        roots.extend(
            package / "LocalCache" / "Local" / "classroomautowork"
            for package in sorted(packages.glob("OpenAI.Codex_*"))
            if package.is_dir()
        )
    return list(dict.fromkeys([*roots, default]))


def state_root() -> Path:
    explicit = os.environ.get("CLASSROOMAUTOWORK_HOME")
    if explicit:
        return require_private_path(Path(explicit))
    candidates = state_candidates()
    for name in ("settings.json", "runtime.json"):
        existing = [root for root in candidates if (root / name).is_file()]
        if existing:
            if len(existing) > 1:
                try:
                    contents = [
                        json.loads((root / name).read_text(encoding="utf-8-sig"))
                        for root in existing
                    ]
                except (OSError, ValueError) as exc:
                    raise ConfigurationError(
                        "Cannot resolve private installation; set CLASSROOMAUTOWORK_HOME."
                    ) from exc
                if any(content != contents[0] for content in contents[1:]):
                    raise ConfigurationError(
                        "Multiple private installations found; set CLASSROOMAUTOWORK_HOME to the intended one."
                    )
            return require_private_path(existing[0])
    return require_private_path(candidates[-1])


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
