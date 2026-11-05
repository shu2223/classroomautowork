"""Which model service answers, and the settings of the endpoints it can answer through.

model-provider.json names the active provider: "anthropic" (the Claude API, also what a missing
file means), "anthropic_compatible" (an Anthropic-format endpoint, such as a Claude Code relay,
reached through the same Claude Code turn) or "openai" (an OpenAI-compatible endpoint). The two
endpoints are kept alike: openai-compatible.json and anthropic-compatible.json each hold the base
URL, the key, the model names (the first is the default), the optional output limit
(max_output_tokens) and the model ids the server listed when it was saved, offered back as
suggestions. The Claude API keeps its key in api_key.py's file, and its optional output limit in
claude-settings.json. connection-tests.json keeps the last connection test of each provider
(connection_test.py). All of them live in the private data directory and are written through
atomic_json, so only their owner can read them. Nothing here returns a key: the page sees the
host, at most its last four characters and the model names.
"""

import ipaddress
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .api_key import key_status, save_key
from .claude_agent import DEFAULT_EFFORT as CLAUDE_DEFAULT_EFFORT
from .claude_agent import EFFORTS as CLAUDE_EFFORTS
from .claude_agent import MODELS as CLAUDE_MODELS
from .errors import ConfigurationError
from .local import atomic_json, require_private_path, utc_now
from .openai_compat import CREDENTIAL, EFFORTS, SECRET_LENGTH, list_models, sends_key_in_clear

PROVIDER_FILE = "model-provider.json"
OPENAI_FILE = "openai-compatible.json"
COMPAT_FILE = "anthropic-compatible.json"
CLAUDE_SETTINGS_FILE = "claude-settings.json"
TESTS_FILE = "connection-tests.json"
PROVIDERS = ("anthropic", "anthropic_compatible", "openai")
MODEL_NAME = re.compile(r"[A-Za-z0-9._:/@+-]{1,200}")
MAX_MODELS = 20
MAX_LISTED = 500
# The optional output limit, in tokens: max_tokens on the OpenAI-compatible path.
OUTPUT_LIMIT_MIN = 1000
OUTPUT_LIMIT_MAX = 1_000_000
OUTPUT_LIMIT_WORDS = (
    f"输出上限要填 {OUTPUT_LIMIT_MIN} 到 {OUTPUT_LIMIT_MAX} 之间的整数（单位 tokens），"
    "或留空表示不设上限；没有保存。"
)
KEY = re.compile(r"[\x21-\x7e]{1,1000}")
# A short key (a placeholder for a local server, say) would be half shown by its last four.
SHOWN_TAIL = 16
# A host name: letters, digits, "_" and "-" in dot-separated labels (an IP literal is checked apart).
LABEL = r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
HOSTNAME = re.compile(rf"{LABEL}(?:\.{LABEL})*\.?")
# A last label of digits (or 0x…) makes resolvers and browsers read the whole host as IPv4.
NUMERIC_LABEL = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)", re.I)
# One writer at a time for the settings files: a save that verifies for up to 20 s must not
# bring back settings that were deleted (or replaced) meanwhile.
WRITE = threading.Lock()


@dataclass(frozen=True)
class Kind:
    """One of the two endpoints a key and an address are saved for."""

    name: str  # the provider name, as model-provider.json and the page say it
    file: str
    label: str  # how the page names it
    style: str  # "openai": {base}/chat/completions; "anthropic": {base}/v1/messages (Claude Code)


OPENAI = Kind("openai", OPENAI_FILE, "OpenAI 兼容接口", "openai")
COMPAT = Kind("anthropic_compatible", COMPAT_FILE, "Anthropic 兼容接口", "anthropic")
KINDS = {kind.name: kind for kind in (OPENAI, COMPAT)}


def provider_path(data_dir) -> Path:
    return require_private_path(Path(data_dir) / PROVIDER_FILE)


def endpoint_path(data_dir, kind=OPENAI) -> Path:
    return require_private_path(Path(data_dir) / kind.file)


def openai_path(data_dir) -> Path:
    return endpoint_path(data_dir, OPENAI)


def compat_path(data_dir) -> Path:
    return endpoint_path(data_dir, COMPAT)


def active_provider(data_dir) -> str:
    try:
        value = json.loads(provider_path(data_dir).read_text(encoding="utf-8")).get("provider")
    except (OSError, ValueError, AttributeError):
        return "anthropic"
    return value if value in PROVIDERS else "anthropic"


# A pasted request path the Anthropic-format address must not keep: Claude Code appends
# /v1/messages to ANTHROPIC_BASE_URL itself.
ANTHROPIC_SUFFIXES = ("/v1/messages", "/messages", "/v1")


def normalize_base_url(value, style="openai") -> str:
    """The address before /chat/completions: http(s), no user or password, query or fragment.

    A trailing slash and a pasted /chat/completions are dropped; nothing (such as /v1) is added.
    style "anthropic" reads it as ANTHROPIC_BASE_URL instead: the address before /v1/messages, so
    after the trailing slash a pasted /v1/messages, /messages or /v1 is dropped, again until none
    is left ("…/v1/v1/messages" too), so that reading the saved address again gives the same one:
    the address the save checked is the one the turn uses.
    """
    anthropic = style == "anthropic"
    if not isinstance(value, str):
        raise ConfigurationError("接口地址必须是文字。")
    text = value.strip()
    if not text:
        raise ConfigurationError(
            "请填写接口地址，例如 https://api.example.com。"
            if anthropic
            else "请填写接口地址，例如 https://api.example.com/v1。"
        )
    if len(text) > 500 or not re.fullmatch(r"[\x21-\x7e]+", text):
        raise ConfigurationError("接口地址只能包含英文字母、数字和网址符号，不能有空格。")
    if "?" in text or "#" in text:
        raise ConfigurationError(
            "接口地址不能带 ? 或 # 之后的参数，只填中转站给的 ANTHROPIC_BASE_URL。"
            if anthropic
            else "接口地址不能带 ? 或 # 之后的参数，只填到 /v1 这样的路径为止。"
        )
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
    if anthropic:
        cut = True
        while cut:
            cut = next((x for x in ANTHROPIC_SUFFIXES if path.lower().endswith(x)), None)
            if cut:
                path = path[: -len(cut)].rstrip("/")
    elif path.lower().endswith("/chat/completions"):
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


def parse_output_limit(value) -> int | None:
    """The output limit a settings request asks for: None for null, "" or only spaces; an int, or
    ASCII digits with spaces around them, from OUTPUT_LIMIT_MIN to OUTPUT_LIMIT_MAX. Anything else
    (true, 1.5, "16k", "16,000", a list) is refused with the range named."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if not re.fullmatch(r"[0-9]{1,12}", text):
            raise ConfigurationError(OUTPUT_LIMIT_WORDS)
        number = int(text)
    elif isinstance(value, bool):
        raise ConfigurationError(OUTPUT_LIMIT_WORDS)
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)  # 16000.0, as some JSON writers put an integer
    else:
        raise ConfigurationError(OUTPUT_LIMIT_WORDS)
    if not OUTPUT_LIMIT_MIN <= number <= OUTPUT_LIMIT_MAX:
        raise ConfigurationError(OUTPUT_LIMIT_WORDS)
    return number


def read_output_limit(value) -> int | None:
    """The limit as a saved file has it: a wrong value reads as none, and never as damage."""
    try:
        return parse_output_limit(value) if isinstance(value, int) else None
    except ConfigurationError:
        return None


def unlisted(models, ids) -> list[str]:
    """Chosen names the server's list does not have (Gemini lists its ids as models/<name>)."""

    def bare(name):
        return name.removeprefix("models/")

    known = {bare(x) for x in ids}
    return [name for name in models if bare(name) not in known]


def _damaged(kind) -> ConfigurationError:
    return ConfigurationError(f"保存的 {kind.label}设置已损坏，请在设置里重新填写。")


def _saved_bytes(data_dir, kind=OPENAI) -> bytes | None:
    """The settings file as it is now (None when there is none), to parse and to compare."""
    try:
        return endpoint_path(data_dir, kind).read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise _damaged(kind) from None


def load_endpoint(data_dir, kind=OPENAI) -> dict | None:
    return parse_endpoint(_saved_bytes(data_dir, kind), kind)


def load_openai(data_dir) -> dict | None:
    return load_endpoint(data_dir, OPENAI)


def load_compat(data_dir) -> dict | None:
    return load_endpoint(data_dir, COMPAT)


def parse_openai(raw: bytes | None) -> dict | None:
    return parse_endpoint(raw, OPENAI)


def parse_endpoint(raw: bytes | None, kind=OPENAI) -> dict | None:
    if raw is None:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
        config = {
            "base_url": normalize_base_url(record["base_url"], kind.style),
            "api_key": record["api_key"],
            "models": parse_models(record["models"]),
        }
        if not isinstance(config["api_key"], str) or not KEY.fullmatch(config["api_key"]):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ConfigurationError):
        raise _damaged(kind) from None
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
        # Whose model list the save saw: "anthropic", "openai" or None (none, or unknown).
        list_shape=record.get("list_shape")
        if record.get("list_shape") in ("anthropic", "openai")
        else None,
        # Files from before the limit existed have none; a wrong value is read as none.
        max_output_tokens=read_output_limit(record.get("max_output_tokens")),
        updated_at=record.get("updated_at"),
    )
    return config


def openai_status(data_dir) -> dict:
    return endpoint_status(data_dir, OPENAI)


def compat_status(data_dir) -> dict:
    return endpoint_status(data_dir, COMPAT)


def endpoint_status(data_dir, kind=OPENAI) -> dict:
    """What the page may know about the endpoint: never the key."""
    try:
        config, damaged = load_endpoint(data_dir, kind), False
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
            "max_output_tokens": None,
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
        "max_output_tokens": config["max_output_tokens"],
        "updated_at": config["updated_at"],
    }


def status(data_dir) -> dict:
    """The provider block of /api/bootstrap. tests: the last connection test of each provider."""
    return {
        "active": active_provider(data_dir),
        "anthropic": claude_status(data_dir),
        "anthropic_compatible": compat_status(data_dir),
        "openai": openai_status(data_dir),
        "tests": last_tests(data_dir),
    }


# ---------------------------------------------------------------- the Claude API's own settings


def claude_settings_path(data_dir) -> Path:
    return require_private_path(Path(data_dir) / CLAUDE_SETTINGS_FILE)


def claude_limit(data_dir) -> int | None:
    """The Claude API's optional output limit (claude-settings.json); a missing or damaged file,
    or a wrong value in it, means none."""
    try:
        record = json.loads(claude_settings_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return read_output_limit(record.get("max_output_tokens")) if isinstance(record, dict) else None


def claude_status(data_dir) -> dict:
    """The key's status (api_key.key_status, whose file stays as it was) and the output limit."""
    return {**key_status(data_dir), "max_output_tokens": claude_limit(data_dir)}


def save_claude(data_dir, values, *, verify=None) -> dict:
    """PUT /api/settings/api-key: {key?, max_output_tokens?}. A key is checked with Anthropic and
    saved as before ("" deletes it); the limit goes to claude-settings.json. Both are checked
    before anything is sent or saved, and a refused key leaves the limit as it was."""
    if not isinstance(values, dict) or not values or set(values) - {"key", "max_output_tokens"}:
        raise ConfigurationError("密钥设置只接受 key 和 max_output_tokens。")
    limit = parse_output_limit(values.get("max_output_tokens"))
    if "key" in values:
        save_key(data_dir, values["key"], verify=verify)
    changed = "key" in values
    if "max_output_tokens" in values and limit != claude_limit(data_dir):
        with WRITE:
            if limit is None:
                claude_settings_path(data_dir).unlink(missing_ok=True)
            else:
                atomic_json(
                    claude_settings_path(data_dir),
                    {"max_output_tokens": limit, "updated_at": utc_now()},
                )
        changed = True
    if changed:
        forget_test(data_dir, "anthropic")
    return claude_status(data_dir)


# ---------------------------------------------------------------- connection tests


def tests_path(data_dir) -> Path:
    return require_private_path(Path(data_dir) / TESTS_FILE)


def last_tests(data_dir) -> dict:
    """{provider: {at, ok, model, summary}}: what the settings dialog says under each group."""
    try:
        record = json.loads(tests_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict):
        return {}
    return {
        name: {k: entry.get(k) for k in ("at", "ok", "model", "summary")}
        for name, entry in record.items()
        if isinstance(name, str) and isinstance(entry, dict)
    }


TESTS = threading.Lock()


def record_test(data_dir, provider, entry) -> None:
    """Keep the last test of one provider: {at, ok, model, summary}, never a key."""
    with TESTS:
        tests = last_tests(data_dir)
        tests[provider] = {k: entry.get(k) for k in ("at", "ok", "model", "summary")}
        atomic_json(tests_path(data_dir), tests)


def forget_test(data_dir, provider) -> None:
    """New or deleted settings make the last test of that provider say nothing about them."""
    with TESTS:
        tests = last_tests(data_dir)
        if tests.pop(provider, None) is not None:
            atomic_json(tests_path(data_dir), tests)


def settings_view(data_dir) -> dict:
    """The status plus the model ids each endpoint listed, for the settings dialog's suggestions:
    suggestions (the OpenAI-compatible endpoint's, as before) and
    anthropic_compatible_suggestions."""

    def listed(kind):
        try:
            config = load_endpoint(data_dir, kind)
        except ConfigurationError:
            config = None
        return config["listed_models"] if config else []

    return {
        **status(data_dir),
        "suggestions": listed(OPENAI),
        "anthropic_compatible_suggestions": listed(COMPAT),
    }


def save_provider(data_dir, values) -> dict:
    if not isinstance(values, dict) or set(values) != {"provider"}:
        raise ConfigurationError("模型接口设置只接受 provider。")
    provider = values["provider"]
    if provider not in PROVIDERS:
        raise ConfigurationError(
            "模型接口只能是 Claude API（anthropic）、Anthropic 兼容接口（anthropic_compatible）"
            "或 OpenAI 兼容接口（openai）。"
        )
    with WRITE:
        kind = KINDS.get(provider)
        if kind and not endpoint_status(data_dir, kind)["configured"]:
            raise ConfigurationError(
                f"还没有保存 {kind.label}。请先填写接口地址、密钥和模型，点“保存并验证”，再切换。"
            )
        atomic_json(provider_path(data_dir), {"provider": provider, "updated_at": utc_now()})
    return settings_view(data_dir)


def check_anthropic_endpoint(base_url, key) -> dict:
    """The save check of an Anthropic-format endpoint: GET {base}/v1/models (see list_models)."""
    return list_models(base_url, key, style="anthropic")


def save_openai(data_dir, values, *, verify=None) -> dict:
    return save_endpoint(data_dir, values, OPENAI, verify=verify)


def save_compat(data_dir, values, *, verify=None) -> dict:
    return save_endpoint(data_dir, values, COMPAT, verify=verify)


def save_endpoint(data_dir, values, kind=OPENAI, *, verify=None) -> dict:
    """Save the endpoint after its model list has accepted the address and the key (GET
    {base}/models, or {base}/v1/models for the Anthropic format); {"clear": true} removes it, and
    answers go back to the Claude API if they came from it."""
    if not isinstance(values, dict):
        raise ConfigurationError(f"{kind.label}设置必须是对象。")
    if "clear" in values:
        # `is True`: 1 and 1.0 equal True in Python, and this request deletes.
        if len(values) != 1 or values["clear"] is not True:
            raise ConfigurationError("删除设置只接受 clear: true。")
        with WRITE:
            endpoint_path(data_dir, kind).unlink(missing_ok=True)
            # Deleting one endpoint never moves answers off the other one.
            if active_provider(data_dir) in (kind.name, "anthropic"):
                atomic_json(
                    provider_path(data_dir), {"provider": "anthropic", "updated_at": utc_now()}
                )
            forget_test(data_dir, kind.name)
        return settings_view(data_dir)
    if set(values) - {"base_url", "api_key", "models", "max_output_tokens"} or not {
        "base_url",
        "models",
    } <= set(values):
        raise ConfigurationError(
            f"{kind.label}设置只接受 base_url、api_key、models 和 max_output_tokens。"
        )
    base_url = normalize_base_url(values["base_url"], kind.style)
    models = parse_models(values["models"])
    limit = parse_output_limit(values.get("max_output_tokens"))
    key = values.get("api_key")
    if key is not None and not isinstance(key, str):
        raise ConfigurationError("密钥必须是文字。")
    key = (key or "").strip()
    # What is saved now; the save goes ahead only if it is still the same after the check.
    before = _saved_bytes(data_dir, kind)
    if key:
        if not KEY.fullmatch(key):
            raise ConfigurationError("密钥只能是不含空格的英文字母、数字和符号，最长 1000 个字符。")
    else:
        try:
            saved = parse_endpoint(before, kind)
        except ConfigurationError:
            saved = None
        if not saved:
            raise ConfigurationError("请填写接口的密钥。")
        # The saved key goes only back to the scheme, host and port it was saved for.
        if origin_of(saved["base_url"]) != origin_of(base_url):
            raise ConfigurationError("换了接口地址，请重新填写密钥。")
        key = saved["api_key"]
    refuse_key_as_model(models, key)
    check = (verify or (list_models if kind is OPENAI else check_anthropic_endpoint))(base_url, key)
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
        "max_output_tokens": limit,
        "updated_at": utc_now(),
    }
    if kind is COMPAT:
        # Kept so that a 404 of the turn can say whether the address looked like an Anthropic one.
        shape = check.get("shape") if listed else None
        record["list_shape"] = shape if shape in ("anthropic", "openai") else None
    with WRITE:
        if _saved_bytes(data_dir, kind) != before:
            raise ConfigurationError(
                "验证期间这个接口的设置被删除或改动了（可能是在另一个页面），这次没有保存。"
                "请重新打开设置，确认后再保存。"
            )
        atomic_json(endpoint_path(data_dir, kind), record)
        forget_test(data_dir, kind.name)
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


def turn_config(data_dir, kind=OPENAI) -> dict:
    """What an answer needs to call the endpoint now: address, key, host and model names."""
    config = load_endpoint(data_dir, kind)
    if not config:
        # A retry keeps the job's provider, so switching to Claude would not help that retry.
        raise ConfigurationError(
            f"这个任务用的 {kind.label}的设置已经删除，这项作业没有作答。"
            "请在设置里重新填写这个接口后点“重试未完成”；或在列表里重新勾选这项作业，"
            "点“完成”用当前的模型接口作答。资料已保留。"
        )
    turn = {k: config[k] for k in ("base_url", "api_key", "host", "models", "max_output_tokens")}
    if kind is COMPAT:
        # No Anthropic-format model list at save: a 404 is more likely the address than the model.
        turn["doubt_address"] = not config["models_listed"] or config["list_shape"] == "openai"
    return turn


def compat_turn_config(data_dir) -> dict:
    return turn_config(data_dir, COMPAT)


def _configured(data_dir, kind) -> dict:
    config = load_endpoint(data_dir, kind)
    if not config:
        raise ConfigurationError(
            f"还没有设置 {kind.label}，请在设置里填写接口地址、密钥和模型，或改用 Claude API。"
        )
    return config


def compat_catalog(data_dir) -> dict:
    """The toolbar's model list for the Anthropic-compatible provider: the configured names, with
    Claude's efforts (the turn is Claude Code's) and Claude's names for its official ids."""
    config = _configured(data_dir, COMPAT)
    claude = dict(CLAUDE_MODELS)
    return {
        "provider": "anthropic_compatible",
        "models": [
            {
                "model": name,
                "displayName": claude.get(name, name),
                "defaultReasoningEffort": CLAUDE_DEFAULT_EFFORT,
                "supportedReasoningEfforts": [{"reasoningEffort": x} for x in CLAUDE_EFFORTS],
            }
            for name in config["models"]
        ],
        "default_model": config["models"][0],
        "default_effort": CLAUDE_DEFAULT_EFFORT,
        "source": "Anthropic 兼容接口 " + config["host"],
        "checked_at": config["updated_at"],
        "note": "实际完成 AI 回合后才确认所选模型可用。",
    }


def openai_catalog(data_dir) -> dict:
    """The toolbar's model list for the OpenAI-compatible provider: the configured names."""
    config = _configured(data_dir, OPENAI)
    # A Claude model the endpoint serves under its official id is shown by its official name.
    claude = dict(CLAUDE_MODELS)
    return {
        "provider": "openai",
        "models": [
            {
                "model": name,
                "displayName": claude.get(name, name),
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
