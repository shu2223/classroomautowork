"""Cache actual read-only link content privately; never execute source instructions."""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from .errors import WorkflowError
from .local import atomic_json, require_private_path, utc_now


def source_urls(records) -> set[str]:
    urls = set()
    for _, record in records:
        urls.update(re.findall(r'https://[^\s<>"\\]+', json.dumps(record, ensure_ascii=False)))
        for material in record.get("materials", []):
            video_id = material.get("youtubeVideo", {}).get("id", "")
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                urls.add("https://www.youtube.com/watch?v=" + video_id)
    return urls


def save_external_text(
    data_dir: Path, course_id: str, url: str, title: str, text: str, kind: str, allowed_urls
) -> dict:
    if not isinstance(course_id, str) or not course_id.isdigit():
        raise WorkflowError("资料必须属于数字课程 ID。")
    if url not in allowed_urls or urlparse(url).scheme != "https":
        raise WorkflowError("资料链接不是当前课程的已知来源。")
    if (
        kind not in {"youtube_captions", "web_text", "user_material"}
        or not text.strip()
        or len(text) > 1_000_000
    ):
        raise WorkflowError("资料须包含实际读取的文字和真实获取方式。")
    value = {"url": url, "title": title, "text": text, "kind": kind}
    value["sha256"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    path = require_private_path(
        data_dir
        / "external-text"
        / course_id
        / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    )
    atomic_json(path, {**value, "read_at": utc_now()})
    return value


def external_chunks(data_dir: Path, course_id: str, allowed_urls):
    root = require_private_path(data_dir / "external-text" / course_id)
    for file in sorted(root.glob("*.json")):
        try:
            record = json.loads(require_private_path(file).read_text(encoding="utf-8"))
            value = {key: record[key] for key in ("url", "title", "text", "kind")}
            digest = hashlib.sha256(
                json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            if value["url"] not in allowed_urls or digest != record["sha256"]:
                continue
            locator = (
                "YouTube captions with timestamps (auto-generated; visual content not transcribed)"
                if value["kind"] == "youtube_captions"
                else "Read-only imported source text"
            )
            yield (
                "external:" + hashlib.sha256(value["url"].encode()).hexdigest()[:24],
                digest,
                [
                    {
                        "source_kind": value["kind"],
                        "title": value["title"],
                        "url": value["url"],
                        "text": value["text"],
                        "locator": locator,
                    }
                ],
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
