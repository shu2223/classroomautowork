"""Trusted, user-maintained course settings. Course content cannot supply these values."""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import ConfigurationError
from .local import atomic_json, require_private_path


@dataclass(frozen=True)
class CoursePolicy:
    ai_use: str = "unknown"  # unknown / forbidden / limited / allowed
    policy_evidence: str = ""
    limitations: str = ""
    disclosure: str = ""
    personal_facts: list[str] = field(default_factory=list)
    max_attachment_bytes: int = 536870912
    max_pdf_pages: int = 1000
    render_dpi: int = 110
    language: str = "ja"

    @classmethod
    def load(cls, data_dir: Path, course_id: str) -> "CoursePolicy":
        if not course_id.isdigit():
            raise ConfigurationError("Course ID must be numeric.")
        path = require_private_path(data_dir / "courses" / (course_id + ".json"))
        if not path.exists():
            atomic_json(path, asdict(cls()))
        try:
            policy = cls(**json.loads(path.read_text(encoding="utf-8-sig")))
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("Invalid private course configuration.") from exc
        return policy.validated()

    def validated(self) -> "CoursePolicy":
        policy = self
        if policy.ai_use not in {"unknown", "forbidden", "limited", "allowed"}:
            raise ConfigurationError("Unknown course AI policy value.")
        if policy.ai_use != "unknown" and not policy.policy_evidence.strip():
            raise ConfigurationError("A known AI policy requires the user's source evidence.")
        if policy.ai_use == "limited" and not policy.limitations.strip():
            raise ConfigurationError("Limited AI use requires explicit limits.")
        if not isinstance(policy.personal_facts, list) or not all(
            isinstance(x, str) for x in policy.personal_facts
        ):
            raise ConfigurationError("Personal facts must be supplied by the user as text.")
        if (
            not 1 <= policy.max_attachment_bytes <= 20_000_000_000
            or not 1 <= policy.max_pdf_pages <= 5000
        ):
            raise ConfigurationError("Invalid attachment/page processing budget.")
        if not 72 <= policy.render_dpi <= 200:
            raise ConfigurationError("PDF preview resolution must be 72–200 DPI.")
        return policy

    def save(self, data_dir: Path, course_id: str) -> Path:
        if not course_id.isdigit():
            raise ConfigurationError("Course ID must be numeric.")
        path = require_private_path(data_dir / "courses" / (course_id + ".json"))
        atomic_json(path, asdict(self.validated()))
        return path

    def draft_gate(self) -> dict:
        return {
            "ai_use": self.ai_use,
            "can_draft": self.ai_use in {"allowed", "limited"},
            "policy_evidence": self.policy_evidence,
            "limitations": self.limitations,
            "disclosure": self.disclosure,
            "personal_facts": self.personal_facts,
            "reason": "Check course and assignment rules before drafting. Unknown or forbidden policy blocks an answer draft.",
        }
