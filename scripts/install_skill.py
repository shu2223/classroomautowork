"""Install the checked-in skill and store local runtime paths outside Git."""

import json
import os
import shutil
import sys
from pathlib import Path

from classroomautowork.local import atomic_json, state_root


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
    runtime = {"python": sys.executable, "repository": str(repo), "skill": str(destination)}
    atomic_json(state_root() / "runtime.json", runtime)
    print(
        json.dumps(
            {"skill": str(destination), "runtime": str(state_root() / "runtime.json")},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
