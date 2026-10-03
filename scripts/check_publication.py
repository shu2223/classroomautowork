"""Inspect the Git index without printing sensitive values or reading OAuth tokens."""

import json
import re
import subprocess
from pathlib import Path

from classroomautowork.config import Settings
from classroomautowork.errors import WorkflowError

ROOT_FILES = {"LICENSE", "README.md", ".gitignore", ".gitattributes", "pyproject.toml", "uv.lock"}
SOURCE_DIRS = (
    "src/",
    "tests/",
    "scripts/",
    "skills/classroom-assistant/",
    "docs/",
    ".github/workflows/",
)
SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".ps1", ".cmd"}
SECRET = re.compile(
    r"GOCSPX-[A-Za-z0-9_-]{10,}|AIza[0-9A-Za-z_-]{30,}|ya29\.[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY|[0-9]{5,}-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com"
)


def git(*args):
    return subprocess.check_output(["git", *args], cwd=Path(__file__).resolve().parents[1])


def main():
    sensitive = []
    try:
        settings = Settings.load()
        sensitive.append(settings.school_email)
        installed = json.loads(settings.client_json.read_text(encoding="utf-8-sig"))["installed"]
        sensitive.extend(installed[k] for k in ("client_id", "client_secret") if installed.get(k))
        path = settings.data_dir / "connection-receipt.json"
        if path.exists():
            receipt = json.loads(path.read_text(encoding="utf-8"))
            sensitive.extend(
                str(receipt[k]) for k in ("course_id", "assignment_id") if receipt.get(k)
            )
            sensitive.append(receipt.get("attachment", {}).get("id", ""))
    except (WorkflowError, OSError, ValueError, KeyError):
        pass  # CI has no private configuration. Pattern and source-tree checks still run.
    violations = []
    names = git("ls-files", "-z").decode("utf-8").split("\0")
    for name in filter(None, names):
        allowed = name in ROOT_FILES or (
            name.startswith(SOURCE_DIRS) and Path(name).suffix in SUFFIXES
        )
        if not allowed:
            violations.append(name + ": unexpected publication file")
            continue
        content = git("show", ":" + name).decode("utf-8-sig")
        if SECRET.search(content) or any(
            value and len(value) >= 8 and value in content for value in sensitive
        ):
            violations.append(name + ": credential/private identifier detected")
    if violations:
        print("Publication rejected:\n" + "\n".join(violations))
        return 1
    print(
        f"Publication index checked: {len(list(filter(None, names)))} source files; no detected credential or known private identifier."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
