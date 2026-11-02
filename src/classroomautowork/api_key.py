"""The Claude API key: set from the web page, kept in the private data directory, never shown again.

Only the last four characters and timestamps are ever returned. Saving checks the key with
Anthropic's model list, which uses no tokens.
"""

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from .errors import ConfigurationError, WorkflowError
from .local import atomic_json, require_private_path, utc_now

KEY_FILE = "anthropic-api-key.json"
PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,300}")
MODELS_URL = "https://api.anthropic.com/v1/models"


def key_path(data_dir: Path) -> Path:
    return require_private_path(data_dir / KEY_FILE)


def load_key(data_dir: Path) -> str | None:
    path = key_path(data_dir)
    if not path.is_file():
        return None
    try:
        key = json.loads(path.read_text(encoding="utf-8"))["key"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigurationError("保存的 Claude API 密钥文件已损坏，请在设置里重新填写。") from None
    return key if isinstance(key, str) and PATTERN.fullmatch(key) else None


def key_status(data_dir: Path) -> dict:
    try:
        key = load_key(data_dir)
        record = json.loads(key_path(data_dir).read_text(encoding="utf-8")) if key else {}
    except ConfigurationError:
        key, record = None, {}
    return {
        "configured": bool(key),
        "last4": key[-4:] if key else None,
        "updated_at": record.get("updated_at"),
    }


def check_key(key: str) -> None:
    request = urllib.request.Request(
        MODELS_URL, headers={"x-api-key": key, "anthropic-version": "2023-06-01"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20):
            return
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise ConfigurationError("Anthropic 拒绝了这个 API 密钥，未保存。") from None
        raise WorkflowError(f"验证密钥时 Anthropic 返回错误（{exc.code}），未保存。") from None
    except OSError:
        raise WorkflowError("暂时连不上 Anthropic，无法验证密钥，未保存。") from None


def save_key(data_dir: Path, key, *, verify=None) -> dict:
    """An empty key removes the saved one."""
    if not isinstance(key, str):
        raise ConfigurationError("API 密钥必须是文字。")
    key = key.strip()
    path = key_path(data_dir)
    if not key:
        path.unlink(missing_ok=True)
        return key_status(data_dir)
    if not PATTERN.fullmatch(key):
        raise ConfigurationError("这不像 Claude API 密钥：应以 sk-ant- 开头，且不含空格。")
    (verify or check_key)(key)
    # atomic_json writes through mkstemp, so the file is readable by its owner only.
    atomic_json(path, {"key": key, "updated_at": utc_now()})
    return key_status(data_dir)
