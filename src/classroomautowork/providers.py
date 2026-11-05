"""Which model service answers, and the settings of the OpenAI-compatible one.

model-provider.json names the active provider, "anthropic" (the Claude API, also what a missing
file means) or "openai". openai-compatible.json holds the endpoint: its base URL, the key, the
model names (the first is the default) and the model ids the server listed when it was saved,
offered back as suggestions. Both live in the private data directory and are written through
atomic_json, so only their owner can read them, like the Claude key. Nothing here returns the
key: the page sees the host, at most its last four characters and the model names.
"""

import ipaddress
import json
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .api_key import key_status
from .errors import ConfigurationError
from .local import atomic_json, require_private_path, utc_now
from .openai_compat import CREDENTIAL, EFFORTS, SECRET_LENGTH, list_models, sends_key_in_clear

PROVIDER_FILE = "model-provider.json"
OPENAI_FILE = "openai-compatible.json"
PROVIDERS = ("anthropic", "openai")
MODEL_NAME = re.compile(r"[A-Za-z0-9._:/@+-]{1,200}")
MAX_MODELS = 20
MAX_LISTED = 500
KEY = re.compile(r"[\x21-\x7e]{1,1000}")
# A short key (a placeholder for a local server, say) would be half shown by its last four.
SHOWN_TAIL = 16
# A host name: letters, digits, "_" and "-" in dot-separated labels (an IP literal is checked apart).
LABEL = r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
HOSTNAME = re.compile(rf"{LABEL}(?:\.{LABEL})*\.?")
# A last label of digits (or 0x…) makes resolvers and browsers read the whole host as IPv4.
NUMERIC_LABEL = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)", re.I)
# One writer at a time for the two settings files: a save that verifies for up to 20 s must not
# bring back settings that were deleted (or replaced) meanwhile.
WRITE = threading.Lock()


def provider_path(data_dir) -> Path:
    return require_private_path(Path(data_dir) / PROVIDER_FILE)


def openai_path(data_dir) -> Path:
    return require_private_path(Path(data_dir) / OPENAI_FILE)


def active_provider(data_dir) -> str:
    try:
        value = json.loads(provider_path(data_dir).read_text(encoding="utf-8")).get("provider")
    except (OSError, ValueError, AttributeError):
        return "anthropic"
    return value if value in PROVIDERS else "anthropic"


def normalize_base_url(value) -> str:
    """The address before /chat/completions: http(s), no user or password, query or fragment.

    A trailing slash and a pasted /chat/completions are dropped; nothing (such as /v1) is added.
    """
    if not isinstance(value, str):
        raise ConfigurationError("接口地址必须是文字。")
    text = value.strip()
    if not text:
        raise ConfigurationError("请填写接口地址，例如 https://api.example.com/v1。")
    if len(text) > 500 or not re.fullmatch(r"[\x21-\x7e]+", text):
        raise ConfigurationError("接口地址只能包含英文字母、数字和网址符号，不能有空格。")
    if "?" in text or "#" in text:
        raise ConfigurationError("接口地址不能带 ? 或 # 之后的参数，只填到 /v1 这样的路径为止。")
    try:
        parts = urlsplit(text)  # refuses a bracketed host that is no IPv6 address, such as [::zz]
    except ValueError:
        raise ConfigurationError("接口地址的 IPv6 地址无效。") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not text.lower().startswith(scheme + "://"):
        raise ConfigurationError("接口地址要以 http:// 或 https:// 开头。")
    if "@" in parts.netloc:
        raise ConfigurationError("接口地址不能包含用户名或密码；密钥请填在“密钥”里。")
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ConfigurationError("接口地址的端口无效。") from None
    if not host:
        raise ConfigurationError("接口地址缺少主机名。")
    check_host(host)
    path = parts.path.rstrip("/")
    if path.lower().endswith("/chat/completions"):
        path = path[: -len("/chat/completions")].rstrip("/")
    netloc = (f"[{host}]" if ":" in host else host) + (f":{port}" if port is not None else "")
    return f"{scheme}://{netloc}{path}"


def check_host(host) -> None:
    """Refuse a host that is not a plain name or IP address: the saved label must name the
    machine the key goes to, so "evil.example\\.api.example.com" and 2130706433 do not pass."""
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ConfigurationError("接口地址的 IPv6 地址无效。") from None
        return
    if NUMERIC_LABEL.fullmatch(host.rstrip(".").rsplit(".", 1)[-1]):
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise ConfigurationError(
                "接口地址的 IP 地址无效，请写成 127.0.0.1 这样的四段十进制数字。"
            ) from None
        return
    if not HOSTNAME.fullmatch(host):
        raise ConfigurationError("接口地址的主机名无效：只能用英文字母、数字、点、- 和 _。")


def host_of(base_url) -> str:
    return urlsplit(base_url).netloc


def origin_of(base_url) -> str:
    parts = urlsplit(base_url)
    return parts.scheme + "://" + parts.netloc


def parse_models(value) -> list[str]:
    """One or more model names, as text (commas or new lines) or a list; the first is the default."""
    if isinstance(value, str):
        names = re.split(r"[,，、\r\n]+", value)
    elif isinstance(value, list) and all(isinstance(x, str) for x in value):
        names = value
    else:
        raise ConfigurationError("模型名称必须是文字，或文字的列表。")
    models = list(dict.fromkeys(x.strip() for x in names if x.strip()))
    if not models:
        raise ConfigurationError("请至少填写一个模型名称。")
    if len(models) > MAX_MODELS:
        raise ConfigurationError(f"最多填写 {MAX_MODELS} 个模型名称。")
    for name in models:
        if CREDENTIAL.search(name):
            raise ConfigurationError(KEY_AS_MODEL)  # never echoed: it may be the key itself
        if not MODEL_NAME.fullmatch(name):
            raise ConfigurationError(
                f"模型名称“{name[:60]}”不对：只能用英文字母、数字和 . _ : / @ + -，"
                "不超过 200 个字符。"
            )
    return models


KEY_AS_MODEL = (
    "模型名称看起来像密钥，没有保存。请把密钥填在“密钥”里，模型只填名称，例如 deepseek-chat。"
)


def refuse_key_as_model(models, key) -> None:
    """A key pasted into 模型 would be shown and recorded as a model name everywhere."""
    for name in models:
        if name == key or (len(key) >= SECRET_LENGTH and key in name):
            raise ConfigurationError(KEY_AS_MODEL)


def unlisted(models, ids) -> list[str]:
    """Chosen names the server's list does not have (Gemini lists its ids as models/<name>)."""

    def bare(name):
        return name.removeprefix("models/")

    known = {bare(x) for x in ids}
    return [name for name in models if bare(name) not in known]


def _saved_bytes(data_dir) -> bytes | None:
    """The settings file as it is now (None when there is none), to parse and to compare."""
    try:
        return openai_path(data_dir).read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise ConfigurationError("保存的 OpenAI 兼容接口设置已损坏，请在设置里重新填写。") from None


def load_openai(data_dir) -> dict | None:
    return parse_openai(_saved_bytes(data_dir))


def parse_openai(raw: bytes | None) -> dict | None:
    if raw is None:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
        config = {
            "base_url": normalize_base_url(record["base_url"]),
            "api_key": record["api_key"],
            "models": parse_models(record["models"]),
        }
        if not isinstance(config["api_key"], str) or not KEY.fullmatch(config["api_key"]):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ConfigurationError):
        raise ConfigurationError("保存的 OpenAI 兼容接口设置已损坏，请在设置里重新填写。") from None
    listed = record.get("listed_models")
    count = record.get("listed_models_count")
    missing = record.get("unlisted_models")
    config.update(
        host=host_of(config["base_url"]),
        models_listed=record.get("models_listed") is True,
        listed_models=[x for x in listed if isinstance(x, str)][:MAX_LISTED]
        if isinstance(listed, list)
        else [],
        listed_models_count=count
        if isinstance(count, int) and not isinstance(count, bool)
        else None,
        unlisted_models=[x for x in missing if isinstance(x, str)]
        if isinstance(missing, list)
        else [],
        key_checked=record.get("key_checked")
        if record.get("key_checked") in (True, False)
        else None,
        updated_at=record.get("updated_at"),
    )
    return config


def openai_status(data_dir) -> dict:
    """What the page may know about the endpoint: never the key."""
    try:
        config, damaged = load_openai(data_dir), False
    except ConfigurationError:
        config, damaged = None, True
    if not config:
        return {
            "configured": False,
            "damaged": damaged,
            "base_url": None,
            "host": None,
            "last4": None,
            "models": [],
            "default_model": None,
            "models_listed": False,
            "listed_models_count": None,
            "unlisted_models": [],
            "key_checked": None,
            "plain_http": False,
            "updated_at": None,
        }
    key = config["api_key"]
    return {
        "configured": True,
        "damaged": False,
        "base_url": config["base_url"],
        "host": config["host"],
        "last4": key[-4:] if len(key) >= SHOWN_TAIL else None,
        "models": config["models"],
        "default_model": config["models"][0],
        "models_listed": config["models_listed"],
        "listed_models_count": config["listed_models_count"],
        "unlisted_models": config["unlisted_models"],
        "key_checked": config["key_checked"],
        "plain_http": sends_key_in_clear(config["base_url"]),
        "updated_at": config["updated_at"],
    }


def status(data_dir) -> dict:
    """The provider block of /api/bootstrap."""
    return {
        "active": active_provider(data_dir),
        "anthropic": key_status(data_dir),
        "openai": openai_status(data_dir),
    }


def settings_view(data_dir) -> dict:
    """The status plus the model ids the server listed, for the settings dialog's suggestions."""
    try:
        config = load_openai(data_dir)
    except ConfigurationError:
        config = None
    return {**status(data_dir), "suggestions": config["listed_models"] if config else []}


def save_provider(data_dir, values) -> dict:
    if not isinstance(values, dict) or set(values) != {"provider"}:
        raise ConfigurationError("模型接口设置只接受 provider。")
    provider = values["provider"]
    if provider not in PROVIDERS:
        raise ConfigurationError(
            "模型接口只能是 Claude API（anthropic）或 OpenAI 兼容接口（openai）。"
        )
    with WRITE:
        if provider == "openai" and not openai_status(data_dir)["configured"]:
            raise ConfigurationError(
                "还没有保存 OpenAI 兼容接口。请先填写接口地址、密钥和模型，点“保存并验证”，再切换。"
            )
        atomic_json(provider_path(data_dir), {"provider": provider, "updated_at": utc_now()})
    return settings_view(data_dir)


def save_openai(data_dir, values, *, verify=None) -> dict:
    """Save the endpoint after GET {base}/models has accepted it; {"clear": true} removes it."""
    if not isinstance(values, dict):
        raise ConfigurationError("OpenAI 兼容接口设置必须是对象。")
    if "clear" in values:
        # `is True`: 1 and 1.0 equal True in Python, and this request deletes.
        if len(values) != 1 or values["clear"] is not True:
            raise ConfigurationError("删除设置只接受 clear: true。")
        with WRITE:
            openai_path(data_dir).unlink(missing_ok=True)
            atomic_json(provider_path(data_dir), {"provider": "anthropic", "updated_at": utc_now()})
        return settings_view(data_dir)
    if set(values) - {"base_url", "api_key", "models"} or not {"base_url", "models"} <= set(values):
        raise ConfigurationError("OpenAI 兼容接口设置只接受 base_url、api_key 和 models。")
    base_url = normalize_base_url(values["base_url"])
    models = parse_models(values["models"])
    key = values.get("api_key")
    if key is not None and not isinstance(key, str):
        raise ConfigurationError("密钥必须是文字。")
    key = (key or "").strip()
    # What is saved now; the save goes ahead only if it is still the same after the check.
    before = _saved_bytes(data_dir)
    if key:
        if not KEY.fullmatch(key):
            raise ConfigurationError("密钥只能是不含空格的英文字母、数字和符号，最长 1000 个字符。")
    else:
        try:
            saved = parse_openai(before)
        except ConfigurationError:
            saved = None
        if not saved:
            raise ConfigurationError("请填写接口的密钥。")
        # The saved key goes only back to the scheme, host and port it was saved for.
        if origin_of(saved["base_url"]) != origin_of(base_url):
            raise ConfigurationError("换了接口地址，请重新填写密钥。")
        key = saved["api_key"]
    refuse_key_as_model(models, key)
    check = (verify or list_models)(base_url, key)
    listed = bool(check.get("listed"))
    ids = [x for x in check.get("ids", []) if isinstance(x, str)]
    suggestions = [x for x in ids if MODEL_NAME.fullmatch(x)][:MAX_LISTED]
    key_checked = check.get("key_checked") if listed else None
    record = {
        "base_url": base_url,
        "api_key": key,
        "models": models,
        "models_listed": listed,
        "listed_models": suggestions,
        "listed_models_count": len(ids) if listed else None,
        "unlisted_models": unlisted(models, ids) if listed else [],
        "key_checked": key_checked if key_checked in (True, False) else None,
        "updated_at": utc_now(),
    }
    with WRITE:
        if _saved_bytes(data_dir) != before:
            raise ConfigurationError(
                "验证期间这个接口的设置被删除或改动了（可能是在另一个页面），这次没有保存。"
                "请重新打开设置，确认后再保存。"
            )
        atomic_json(openai_path(data_dir), record)
    return {
        **settings_view(data_dir),
        "check": {
            "models_listed": listed,
            "listed_models_count": record["listed_models_count"],
            "unlisted_models": record["unlisted_models"],
            "key_checked": record["key_checked"],
            "status": check.get("status"),
        },
    }


def turn_config(data_dir) -> dict:
    """What an answer needs to call the endpoint now: address, key, host and model names."""
    config = load_openai(data_dir)
    if not config:
        # A retry keeps the job's provider, so switching to Claude would not help that retry.
        raise ConfigurationError(
            "这个任务用的 OpenAI 兼容接口的设置已经删除，这项作业没有作答。"
            "请在设置里重新填写这个接口后点“重试未完成”；或在列表里重新勾选这项作业，"
            "点“完成”用当前的模型接口作答。资料已保留。"
        )
    return {k: config[k] for k in ("base_url", "api_key", "host", "models")}


def openai_catalog(data_dir) -> dict:
    """The toolbar's model list for the OpenAI-compatible provider: the configured names."""
    config = load_openai(data_dir)
    if not config:
        raise ConfigurationError(
            "还没有设置 OpenAI 兼容接口，请在设置里填写接口地址、密钥和模型，或改用 Claude API。"
        )
    return {
        "provider": "openai",
        "models": [
            {
                "model": name,
                "displayName": name,
                "defaultReasoningEffort": None,
                "supportedReasoningEfforts": [{"reasoningEffort": x} for x in EFFORTS],
            }
            for name in config["models"]
        ],
        "default_model": config["models"][0],
        "default_effort": None,
        "source": "OpenAI 兼容接口 " + config["host"],
        "checked_at": config["updated_at"],
        "note": "实际完成 AI 回合后才确认所选模型可用。",
    }
