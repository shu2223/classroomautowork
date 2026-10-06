"""Native, user-entered answers overlay model candidates without rewriting AI provenance."""

import hashlib
import json
from pathlib import Path

from .errors import WorkflowError
from .local import atomic_json, require_private_path, utc_now


def _path(data_dir: Path, course_id: str, assignment_id: str) -> Path:
    if not all(isinstance(x, str) and x.isdigit() for x in (course_id, assignment_id)):
        raise WorkflowError("补充答案必须属于真实的课程和作业。")
    return require_private_path(
        data_dir / "user-form-answers" / course_id / (assignment_id + ".json")
    )


def _digest(answers) -> str:
    return hashlib.sha256(
        json.dumps(answers, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def load_user_answers(data_dir: Path, manifest: dict, forms: list, profile) -> dict:
    from .form_fill import validate_answers

    path = _path(data_dir, manifest["course_id"], manifest["assignment_id"])
    if not path.exists():
        return {"answers": [], "sha256": None}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        valid = record.get("source") == "user_local_form" and _digest(
            record["answers"]
        ) == record.get("sha256")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise WorkflowError("你提供的表单答案无法读取，请在软件中重新保存。") from exc
    if not valid:
        raise WorkflowError("你提供的表单答案校验失败，请在软件中重新保存。")
    validate_answers(forms, record["answers"], profile)
    return record


def save_user_answers(data_dir: Path, manifest: dict, forms: list, profile, answers: list) -> dict:
    from .form_fill import validate_answers

    if not isinstance(answers, list) or len(answers) > 1000:
        raise WorkflowError("补充答案应为原生表单逐栏数组。")
    for value in answers:
        if not isinstance(value, dict) or set(value) != {
            "form_url",
            "entry_id",
            "context_sha256",
            "values",
        }:
            raise WorkflowError("补充答案仅接受原表单地址、栏位、题目校验值和回答。")
    result = validate_answers(
        forms,
        [
            {**value, "needs_user": False, "review_note": "由用户在软件补充入口填写"}
            for value in answers
        ],
        profile,
    )
    record = {
        "source": "user_local_form",
        "answers": result,
        "sha256": _digest(result),
        "saved_at": utc_now(),
    }
    atomic_json(_path(data_dir, manifest["course_id"], manifest["assignment_id"]), record)
    return record


def merge_answers(model_answers: list, user_answers: list) -> list:
    values = {(x["form_url"], x["entry_id"]): x for x in model_answers}
    values.update({(x["form_url"], x["entry_id"]): x for x in user_answers})
    return list(values.values())
