"""User-supplied identity stays in private configuration, never in course content."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import ConfigurationError
from .local import atomic_json, require_private_path


@dataclass(frozen=True)
class StudentProfile:
    student_id: str = ""
    name: str = ""
    department: str = ""
    class_name: str = ""

    def validated(self):
        if any(
            not isinstance(value, str) or len(value) > 120 or any(ord(char) < 32 for char in value)
            for value in asdict(self).values()
        ):
            raise ConfigurationError("学生资料须为不超过 120 字的单行文字。")
        return self

    @classmethod
    def load(cls, root: Path):
        path = require_private_path(root / "student-profile.json")
        if not path.exists():
            return cls()
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8-sig"))).validated()
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("私人学生资料配置无效。") from exc

    def save(self, root: Path):
        path = require_private_path(root / "student-profile.json")
        atomic_json(path, asdict(self.validated()))
        return path


def identity_facts(profile: dict) -> list[str]:
    labels = {
        "student_id": "学籍番号",
        "name": "氏名",
        "department": "学科",
        "class_name": "クラス",
    }
    return [f"{label}：{profile[key]}" for key, label in labels.items() if profile.get(key)]
