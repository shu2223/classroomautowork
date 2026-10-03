"""Resolve bootstrap paths without emitting credentials or course content."""

import json
import sys
from pathlib import Path

from classroomautowork.errors import ConfigurationError, WorkflowError
from classroomautowork.local import require_private_path, state_root


def resolve_runtime() -> dict:
    root = state_root()
    runtime = root / "runtime.json"
    if not runtime.is_file():
        raise ConfigurationError(
            "Local runtime missing; run scripts/install_skill.py with the project Python."
        )
    try:
        config = json.loads(runtime.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ConfigurationError("Invalid private runtime JSON; reinstall the Skill.") from exc
    if not Path(config.get("python", "")).is_file():
        raise ConfigurationError("Configured Python is missing; reinstall the local runtime.")
    settings = require_private_path(root / "settings.json")
    if not settings.is_file():
        raise ConfigurationError("Private settings missing; run classroomaw init.")
    return {**config, "state_root": str(root), "settings_path": str(settings)}


if __name__ == "__main__":
    try:
        print(json.dumps(resolve_runtime(), ensure_ascii=True))
    except WorkflowError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
