"""Assignment-specific, user-supplied inputs stored outside the checkout."""

import hashlib
import json
from pathlib import Path

from .errors import WorkflowError
from .local import atomic_json, require_private_path, utc_now


def _path(data_dir: Path, course_id: str, assignment_id: str) -> Path:
    if not all(isinstance(value, str) and value.isdigit() for value in (course_id, assignment_id)):
        raise WorkflowError("补充内容必须属于一个真实的数字课程和作业 ID。")
    return require_private_path(data_dir / "supplements" / course_id / (assignment_id + ".json"))


def normalized(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - {"text", "personal_facts"}:
        raise WorkflowError("补充只接受说明文字和你提供的真实个人事实。")
    text, facts = value.get("text", ""), value.get("personal_facts", [])
    if not isinstance(text, str) or len(text) > 60000:
        raise WorkflowError("补充说明必须是文字，最多 60000 字符。")
    if (
        not isinstance(facts, list)
        or len(facts) > 200
        or any(not isinstance(fact, str) or len(fact) > 4000 for fact in facts)
    ):
        raise WorkflowError("个人事实最多 200 项，每项最多 4000 字符。")
    content = {
        "text": text.strip(),
        "personal_facts": list(dict.fromkeys(x.strip() for x in facts if x.strip())),
    }
    content["sha256"] = hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return content


def load_supplement(data_dir: Path, course_id: str, assignment_id: str) -> dict:
    path = _path(data_dir, course_id, assignment_id)
    if not path.exists():
        return normalized({})
    value = json.loads(path.read_text(encoding="utf-8"))
    content = normalized({key: value[key] for key in ("text", "personal_facts")})
    if value.get("sha256") != content["sha256"]:
        raise WorkflowError("本机补充内容校验失败，请重新保存。")
    return content


def save_supplement(data_dir: Path, course_id: str, assignment_id: str, value: dict) -> dict:
    content = normalized(value)
    path = _path(data_dir, course_id, assignment_id)
    if load_supplement(data_dir, course_id, assignment_id) != content:
        atomic_json(path, {**content, "updated_at": utc_now()})
    return content


def check_supplement(data_dir: Path, manifest: dict) -> None:
    current = load_supplement(data_dir, manifest["course_id"], manifest["assignment_id"])
    if current != manifest.get("supplement", normalized({})):
        raise WorkflowError("这项作业的补充已变化，请点击补充并继续，旧答案不能按新资料自动填写。")


def personal_facts(manifest: dict) -> list[str]:
    from .student import identity_facts

    return (
        manifest["policy"]["personal_facts"]
        + manifest.get("supplement", {}).get("personal_facts", [])
        + identity_facts(manifest.get("student_profile", {}))
    )
