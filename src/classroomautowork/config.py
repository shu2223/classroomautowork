import base64
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigurationError
from .local import atomic_json, require_private_path, state_root


def decode_classroom_id(value: str) -> str:
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True).decode()
    except (ValueError, UnicodeError):
        decoded = ""
    if decoded.isdigit():
        return decoded
    if value.isdigit():
        return value
    raise ConfigurationError("Cannot resolve Classroom ID. Supply numeric course/assignment IDs.")


def assignment_ids(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    match = re.fullmatch(r"(?:/u/\d+)?/c/([^/]+)/a/([^/]+)(?:/details)?/?", parsed.path)
    if parsed.scheme != "https" or parsed.hostname != "classroom.google.com" or not match:
        raise ConfigurationError(
            "Use the assignment details URL: https://classroom.google.com/c/…/a/…/details"
        )
    return decode_classroom_id(match[1]), decode_classroom_id(match[2])


@dataclass(frozen=True)
class Settings:
    school_email: str
    client_json: Path
    data_dir: Path
    course_id: str = ""
    assignment_id: str = ""
    timezone: str = "Asia/Tokyo"

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = require_private_path(path or state_root() / "settings.json")
        if not path.is_file():
            raise ConfigurationError(
                "Settings missing. Run classroomaw init with real OAuth client and assignment."
            )
        try:
            values = json.loads(path.read_text(encoding="utf-8-sig"))
            return cls(**values).validated()
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("Invalid local settings JSON.") from exc

    def validated(self) -> "Settings":
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.school_email):
            raise ConfigurationError("A school email is required to pin the authorized identity.")
        if (self.course_id or self.assignment_id) and not (
            self.course_id.isdigit() and self.assignment_id.isdigit()
        ):
            raise ConfigurationError(
                "Set numeric course and assignment IDs together, or omit both."
            )
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError("Unknown timezone in local settings.") from exc
        return Settings(
            school_email=self.school_email.lower(),
            client_json=require_private_path(Path(self.client_json)),
            data_dir=require_private_path(Path(self.data_dir)),
            course_id=self.course_id,
            assignment_id=self.assignment_id,
            timezone=self.timezone,
        )

    def save(self, path: Path | None = None) -> Path:
        settings = self.validated()
        path = require_private_path(path or state_root() / "settings.json")
        atomic_json(path, {k: str(v) for k, v in asdict(settings).items()})
        return path
