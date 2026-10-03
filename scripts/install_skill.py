"""Install the checked-in skill and store local runtime paths outside Git."""

import json
import os
import shutil
import sys
from pathlib import Path

from classroomautowork.local import atomic_json, require_private_path, state_root


def main():
    repo = Path(__file__).resolve().parents[1]
    codex_dir = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    destination = codex_dir / "skills" / "classroom-assistant"
    source = repo / "skills" / "classroom-assistant"
    if destination.exists():
        existing = destination / "SKILL.md"
        if not existing.is_file() or "name: classroom-assistant" not in existing.read_text(
            encoding="utf-8"
        ):
            raise SystemExit(
                "A different skill occupies the destination; choose its location manually."
            )
    shutil.copytree(
        source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__")
    )
    root = state_root()
    runtime = {
        "python": sys.executable,
        "repository": str(repo),
        "skill": str(destination),
        "state_root": str(root),
        "settings_path": str(root / "settings.json"),
    }
    atomic_json(root / "runtime.json", runtime)
    # This machine-local pointer stays outside the source skill and Git.
    atomic_json(
        require_private_path(destination / "runtime-location.json"), {"state_root": str(root)}
    )
    print(
        json.dumps(
            {"skill": str(destination), "runtime": str(root / "runtime.json")},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
