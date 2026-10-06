"""Import question data read in an authorized browser; never copy cookies or tokens."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .errors import PermissionDenied, WorkflowError
from .form_read import MAX_PAGE_BYTES, form_url, parse_form
from .local import atomic_json, require_private_path, utc_now

MAX_AGE = timedelta(hours=24)


def _path(data_dir: Path, url: str) -> Path:
    key = hashlib.sha256(form_url(url).encode()).hexdigest()
    return require_private_path(data_dir / "browser-form-questions" / (key + ".json"))


def _digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def import_form_capture(
    data_dir: Path, account: str, url: str, question_script: str, allowed_urls
) -> dict:
    canonical = form_url(url)
    if canonical not in {form_url(value) for value in allowed_urls}:
        raise WorkflowError("导入表单必须是这项作业原始来源中的链接。")
    if not isinstance(question_script, str) or len(question_script.encode()) > MAX_PAGE_BYTES:
        raise WorkflowError("表单题目数据超过读取上限。")
    # Parse the original question payload, then discard the supplied script immediately.
    # Only normalized questions, options, native IDs and context survive on disk.
    form = parse_form(question_script, canonical)
    record = {
        "form": form,
        "account": account.lower(),
        "captured_at": utc_now(),
        "source": "user_authorized_browser_import",
    }
    record["sha256"] = _digest(record)
    atomic_json(_path(data_dir, canonical), record)
    return form


def authenticated_form_cache(data_dir: Path, account: str, url: str, error: Exception) -> dict:
    # A deleted/forbidden form must not silently reuse an old browser capture.
    if not isinstance(error, PermissionDenied) or not any(
        marker in str(error)
        for marker in ("HTTP 401", "学校账户登录", "需要登录", "表单要求浏览器登录")
    ):
        raise error
    path = _path(data_dir, url)
    if not path.is_file():
        raise PermissionDenied(
            f"原表单需要学校账户登录读取：{form_url(url)}。请使用‘补充并继续’中的登录题目读取入口；没有题目时不会猜答案。"
        ) from error
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        digest = record.pop("sha256")
        captured = datetime.fromisoformat(record["captured_at"])
        if captured.tzinfo is None:
            raise ValueError
        age = datetime.now(UTC) - captured
        if (
            _digest(record) != digest
            or record["account"] != account.lower()
            or form_url(record["form"]["url"]) != form_url(url)
            or not timedelta(0) <= age <= MAX_AGE
            or record["source"] != "user_authorized_browser_import"
        ):
            raise ValueError
        return record["form"]
    except (ValueError, KeyError, TypeError):
        raise PermissionDenied("登录浏览器读取的题目已过期或校验失败，请重新读取原表单。") from None
