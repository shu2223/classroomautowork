"""Thin installed-skill bridge. Runtime paths are trusted private user configuration."""

import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    root = (
        Path(base) / "classroomautowork" if base else Path.home() / ".local/share/classroomautowork"
    )
    path = root / "runtime.json"
    if not path.is_file():
        print(
            "Classroom runtime is not installed. Run the repository's scripts/install_skill.py.",
            file=sys.stderr,
        )
        return 2
    config = json.loads(path.read_text(encoding="utf-8"))
    interpreter = Path(config["python"]).resolve()
    if not interpreter.is_file():
        print(
            "Configured Classroom Python is missing; reinstall the local runtime.", file=sys.stderr
        )
        return 2
    return subprocess.call(
        [str(interpreter), "-m", "classroomautowork.cli", *sys.argv[1:]], shell=False
    )


if __name__ == "__main__":
    raise SystemExit(main())
