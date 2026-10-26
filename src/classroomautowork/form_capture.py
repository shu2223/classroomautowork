"""Import question data read in an authorized browser; never copy cookies or tokens."""

import copy
import hashlib
import json
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image

from .errors import PermissionDenied, WorkflowError
from .form_read import MAX_PAGE_BYTES, form_url, image_gap_warning, parse_form
from .local import atomic_json, require_private_path, sha256_file, utc_now

MAX_AGE = timedelta(hours=24)
MAX_IMAGE_BYTES = 15 * 1024 * 1024


def _path(data_dir: Path, url: str) -> Path:
    key = hashlib.sha256(form_url(url).encode()).hexdigest()
    return require_private_path(data_dir / "browser-form-questions" / (key + ".json"))


def _digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _images_path(data_dir: Path, url: str) -> Path:
    key = hashlib.sha256(form_url(url).encode()).hexdigest()
    return require_private_path(data_dir / "browser-form-images" / (key + ".json"))


def import_form_images(
    data_dir: Path, account: str, url: str, images: list[dict], allowed_urls
) -> dict:
    """Bind browser-downloaded pixels to the current form's actual native image IDs.

    The browser caller copies each downloaded asset into its private capture
    directory first. No cookies, image URL tokens or arbitrary URLs are retained.
    """
    canonical = form_url(url)
    if canonical not in {form_url(value) for value in allowed_urls}:
        raise WorkflowError("导入图片必须属于这项作业原始表单。")
    form = authenticated_form_cache(data_dir, account, canonical, PermissionDenied("HTTP 401"))
    known = {picture["item_id"]: picture for picture in form.get("media", [])}
    if not isinstance(images, list) or len(images) > len(known):
        raise WorkflowError("表单图片导入记录无效。")
    record_path = _images_path(data_dir, canonical)
    record = {"url": canonical, "account": account.lower(), "images": {}}
    if record_path.is_file():
        try:
            previous = json.loads(record_path.read_text(encoding="utf-8"))
            checksum = previous.pop("sha256")
            if (
                checksum == _digest(previous)
                and previous["account"] == account.lower()
                and previous["url"] == canonical
            ):
                record = previous
        except (ValueError, KeyError, TypeError):
            pass
    seen = set()
    for supplied in images:
        if not isinstance(supplied, dict) or set(supplied) != {"item_id", "path"}:
            raise WorkflowError("表单图片必须带有原生 item ID 和本机文件路径。")
        item_id = supplied["item_id"]
        if not isinstance(item_id, str) or item_id not in known or item_id in seen:
            raise WorkflowError("表单图片指向未知或重复的原生 item ID。")
        seen.add(item_id)
        if not isinstance(supplied["path"], str):
            raise WorkflowError("表单图片路径无效。")
        source = require_private_path(Path(supplied["path"]))
        if not source.is_file() or not 0 < source.stat().st_size <= MAX_IMAGE_BYTES:
            raise WorkflowError("表单图片不存在或超过 15 MB 读取上限。")
        key = _digest([canonical, item_id, known[item_id]["media_id"], sha256_file(source)])
        destination = require_private_path(data_dir / "processed" / "form-images" / (key + ".png"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(source) as picture:
                    if picture.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                        raise ValueError
                    picture.load()
                    # Strip metadata; only visible pixels survive in the evidence cache.
                    picture.convert("RGB").save(destination, format="PNG")
        except (OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise WorkflowError("表单图片不是可安全读取的 PNG/JPEG/WebP/GIF。") from None
        record["images"][item_id] = {
            "media_id": known[item_id]["media_id"],
            "image_path": str(destination),
            "sha256": sha256_file(destination),
            "captured_at": utc_now(),
        }
    record["sha256"] = _digest(record)
    atomic_json(record_path, record)
    return attach_form_images(data_dir, account, form)


def attach_form_images(data_dir: Path, account: str, form: dict) -> dict:
    """Retain actual image evidence and leave explicit gaps for unavailable pixels."""
    result = copy.deepcopy(form)
    pictures = result.get("media", [])
    if not pictures:
        return result
    records = {}
    path = _images_path(data_dir, form["url"])
    if path.is_file():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            checksum = record.pop("sha256")
            if (
                checksum == _digest(record)
                and record["account"] == account.lower()
                and record["url"] == form_url(form["url"])
            ):
                records = record["images"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    image_warnings = {image_gap_warning(picture) for picture in pictures}
    result["warnings"] = [x for x in result.get("warnings", []) if x not in image_warnings]
    for picture in pictures:
        picture.pop("image_path", None)
        picture.pop("sha256", None)
        try:
            cached = records[picture["item_id"]]
            image_path = Path(cached["image_path"]).resolve()
            if (
                cached["media_id"] != picture["media_id"]
                or not image_path.is_relative_to((data_dir / "processed" / "form-images").resolve())
                or not image_path.is_file()
                or sha256_file(image_path) != cached["sha256"]
            ):
                raise ValueError
            picture.update(image_path=str(image_path), sha256=cached["sha256"])
        except (OSError, ValueError, KeyError, TypeError):
            result["warnings"].append(image_gap_warning(picture))
    result["read_complete"] = not result["warnings"]
    return result


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
