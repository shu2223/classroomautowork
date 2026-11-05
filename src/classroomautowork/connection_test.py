"""The connection test of the settings dialog: one tiny real request through the answer's path.

A test sends what an answer sends, only very little of it: one short instruction, one tiny page
image made here, and a two-field schema, through exactly the turn an assignment uses (for the
OpenAI-compatible endpoint, openai_compat.run_turn with the saved address, key and output limit,
and no effort, as 默认; for the Claude API and the Anthropic-compatible endpoint, the Claude Code
turn of claude_agent.run_turn with the same environment an answer gets, at effort "low"). So its
degradations say what the endpoint takes: images, JSON Schema, streaming, the output limit. It is
bounded tighter than an answer (TURN_SECONDS, IDLE_SECONDS, the Anthropic-compatible endpoint's
Claude Code retrying a failed request once, so its own reason comes back at once), runs one at a
time (a second one meanwhile is refused with Busy, HTTP 409), ends when the page stops waiting
(`cancelled`: it closed, reloaded or gave up) or the server stops (stop()), and keeps the last
result of each provider in connection-tests.json as {at, ok, model, summary}, never a key. Its
error is the answer's, without what only an answer needs said ("未作答", "资料已保留", where in the
settings to go: the test is run from there).

A provider plugs in by adding a Tester to TESTERS:
  check(data_dir, model)     raises ConfigurationError when the provider is not set up or does not
                             list that model (HTTP 400; nothing is sent or kept);
  run(data_dir, model, cancelled)
                             returns {"host", "requested_model", "outcome"}, outcome being its
                             run_turn's result (its "provider" details, when it has them, fill in
                             the structured output mode, images, streaming and degradations);
  marker(data_dir)           something that changes when its settings change: a test of settings
                             saved over or deleted meanwhile is returned but not kept as the last;
  describe(data_dir, model)  (host, model the test asks for), known before it runs, so a failed
                             test names them too.
"""

import base64
import re
import struct
import tempfile
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import claude_agent, openai_compat
from .api_key import key_path, load_key
from .errors import Busy, ConfigurationError, RunCancelled, WorkflowError
from .local import utc_now
from .providers import (
    COMPAT,
    MODEL_NAME,
    _saved_bytes,
    claude_limit,
    claude_settings_path,
    compat_status,
    openai_status,
    record_test,
    turn_config,
)

PROVIDERS = ("anthropic", "anthropic_compatible", "openai")
TURN_SECONDS = 120
IDLE_SECONDS = 60
# The effort a Claude test asks for; the OpenAI-compatible test sends none (默认).
CLAUDE_EFFORT = "low"
# How often the Anthropic-compatible endpoint's Claude Code retries a failed request in a test:
# once, so a refused key, a used-up quota or a model the relay has no channel for comes back in
# seconds with the relay's own words (an answer keeps Claude Code's ten retries).
RELAY_RETRIES = 1
TEXT = "这是作业助手的连接测试。请按要求只返回 JSON：ok 为 true，reply 为“连接正常”。"
CAPTION = "页面图片 1：连接测试用的小图"
SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "reply": {"type": "string"}},
    "required": ["ok", "reply"],
    "additionalProperties": False,
}
RUNNING = threading.Lock()
# Set while the server stops: a test that runs ends at once (and its Claude Code process with it).
STOPPING = threading.Event()


def tiny_png(size=16) -> bytes:
    """A 16 × 16 PNG made here (a blue square on white), so the test needs no file."""

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b""
    for y in range(size):
        rows += b"\0" + b"".join(
            b"\x0b\x63\xce" if 4 <= x < 12 and 4 <= y < 12 else b"\xff\xff\xff" for x in range(size)
        )
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def content() -> list[dict]:
    """The test's message, in the blocks drafting.build_input makes: the instruction, then a
    captioned page image (so an endpoint that refuses images drops the caption, not the text)."""
    return [
        {"type": "text", "text": TEXT},
        {"type": "text", "text": CAPTION},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(tiny_png()).decode("ascii"),
            },
        },
    ]


@dataclass(frozen=True)
class Tester:
    check: Callable
    run: Callable
    marker: Callable
    describe: Callable


def _openai_check(data_dir, model):
    status = openai_status(data_dir)
    if not status["configured"]:
        raise ConfigurationError(
            "还没有保存 OpenAI 兼容接口，不能测试连接。请先填写接口地址、密钥和模型，点“保存并验证”。"
        )
    if model is not None and model not in status["models"]:
        raise ConfigurationError(
            "要测试的模型不在 OpenAI 兼容接口设置的模型里，请在设置里添加后再测试。"
        )


def _openai_marker(data_dir):
    try:
        return _saved_bytes(data_dir)
    except ConfigurationError:
        return None


def _openai_describe(data_dir, model):
    config = turn_config(data_dir)
    return config["host"], model or config["models"][0]


def _openai_run(data_dir, model, cancelled=lambda: False) -> dict:
    config = turn_config(data_dir)
    model = model or config["models"][0]
    outcome = openai_compat.run_turn(
        content(),
        SCHEMA,
        model=model,
        effort=None,
        config=config,
        cancelled=cancelled,
        turn_seconds=TURN_SECONDS,
        idle_seconds=IDLE_SECONDS,
    )
    return {"host": config["host"], "requested_model": model, "outcome": outcome}


# ---- the Claude Code turn: the Claude API and the Anthropic-compatible endpoint

CLAUDE_HOST = "api.anthropic.com"


def _claude_check(data_dir, model):
    try:
        key = load_key(data_dir)
    except ConfigurationError:
        key = None
    if not key:
        raise ConfigurationError(
            "还没有保存 Claude API 密钥，不能测试连接。请先在 Claude API 里填写密钥并保存。"
        )
    if model is not None and model not in dict(claude_agent.MODELS):
        raise ConfigurationError("要测试的模型不是 Claude API 的模型，请在工具栏里重新选择。")


def _claude_marker(data_dir):
    def read(path):
        try:
            return path.read_bytes()
        except OSError:
            return None

    return (read(key_path(data_dir)), read(claude_settings_path(data_dir)))


def _claude_details(outcome) -> dict:
    """What the Claude Code turn had to give up, in the shape the result reads endpoints' from:
    Claude Code asks for the answer as a StructuredOutput tool call, streamed."""
    return {
        "structured_output_mode": "tool",
        "streamed": True,
        "degradations": list(outcome.get("degradations") or []),
        "output_limit": outcome.get("output_limit"),
        "effort": {"requested": CLAUDE_EFFORT, "sent": CLAUDE_EFFORT},
    }


def _claude_turn(data_dir, model, **options) -> dict:
    # A private, empty working folder of its own; nothing of an assignment is in reach.
    with tempfile.TemporaryDirectory(prefix="connection-test-", dir=data_dir) as folder:
        outcome = claude_agent.run_turn(
            content(),
            SCHEMA,
            model=model,
            effort=CLAUDE_EFFORT,
            cwd=Path(folder),
            turn_seconds=TURN_SECONDS,
            idle_seconds=IDLE_SECONDS,
            **options,
        )
    return {**outcome, "provider": _claude_details(outcome)}


def _claude_describe(data_dir, model):
    return CLAUDE_HOST, model or claude_agent.DEFAULT_MODEL


def _claude_run(data_dir, model, cancelled=lambda: False) -> dict:
    model = model or claude_agent.DEFAULT_MODEL
    limit = claude_limit(data_dir)
    outcome = _claude_turn(
        data_dir,
        model,
        cancelled=cancelled,
        api_key=load_key(data_dir),
        **({"max_output_tokens": limit} if limit else {}),
    )
    return {"host": CLAUDE_HOST, "requested_model": model, "outcome": outcome}


def _compat_check(data_dir, model):
    status = compat_status(data_dir)
    if not status["configured"]:
        raise ConfigurationError(
            "还没有保存 Anthropic 兼容接口，不能测试连接。请先填写接口地址、密钥和模型，点“保存并验证”。"
        )
    if model is not None and model not in status["models"]:
        raise ConfigurationError(
            "要测试的模型不在 Anthropic 兼容接口设置的模型里，请在设置里添加后再测试。"
        )


def _compat_marker(data_dir):
    try:
        return _saved_bytes(data_dir, COMPAT)
    except ConfigurationError:
        return None


def _compat_describe(data_dir, model):
    config = turn_config(data_dir, COMPAT)
    return config["host"], model or config["models"][0]


def _compat_run(data_dir, model, cancelled=lambda: False) -> dict:
    config = turn_config(data_dir, COMPAT)
    model = model or config["models"][0]
    outcome = _claude_turn(
        data_dir,
        model,
        cancelled=cancelled,
        api_key=config["api_key"],
        base_url=config["base_url"],
        auth="helper",
        host=config["host"],
        max_output_tokens=config["max_output_tokens"],
        retries=RELAY_RETRIES,
        doubt_address=config.get("doubt_address", False),
    )
    return {"host": config["host"], "requested_model": model, "outcome": outcome}


TESTERS = {
    "anthropic": Tester(_claude_check, _claude_run, _claude_marker, _claude_describe),
    "anthropic_compatible": Tester(_compat_check, _compat_run, _compat_marker, _compat_describe),
    "openai": Tester(_openai_check, _openai_run, _openai_marker, _openai_describe),
}


def _images(details, sent_image) -> str:
    """sent; refused (the endpoint does not take images); dropped (the request was too large)."""
    relaxed = details.get("degradations") or []
    if "images" in relaxed:
        return "refused"
    if "images_size" in relaxed or not sent_image:
        return "dropped"
    return "sent"


def _seconds(ms) -> str:
    return f"{(ms or 0) / 1000:.1f} 秒"


# What a test found the endpoint does not take, as one or two words for the kept summary
# ("正常 · 不接受图片 · 2.1 秒") under the group: the step, in the order of `notes`.
TAGS = (
    ("images", "不接受图片"),
    ("images_size", "图片超出大小上限"),
    ("json_schema", "不支持 JSON Schema"),
    ("json_object", "不支持 JSON 输出"),
    ("stream", "不支持流式"),
    ("max_tokens_param", "上限改用 max_completion_tokens"),
    ("max_tokens_clamp", "输出上限已按接口最大值"),
    ("max_tokens", "不接受输出上限"),
)
SCHEMA_IGNORED = "接口似乎没有按 JSON Schema（response_format）约束输出。"
IGNORED_SCHEMA = (
    "接口似乎没有按 JSON Schema（response_format）约束输出，回复里的 JSON 要从文字中找出；"
    "作答时只能靠提示词和本机核验，结果可能缺字段。"
)


def notes(result) -> list[str]:
    """What the endpoint did not take, said the way an answer will be affected."""
    relaxed = result.get("degradations") or []
    found = []
    if result.get("images") == "refused":
        found.append(
            "接口不接受图片：作答时每次都会先被拒一次，Claude Code 去掉图片后只按文字作答。"
            if result.get("structured_output_mode") == "tool"
            else "接口不接受图片：作答时页面图片不会发送，只按提取的文字作答。"
        )
    elif result.get("images") == "dropped":
        found.append("请求超出了接口的大小上限，图片没有发送。")
    mode = result.get("structured_output_mode")
    if mode == "json_schema" and result.get("reply_json") == "extracted":
        found.append(IGNORED_SCHEMA)
    if mode == "json_object":
        found.append("接口不支持 JSON Schema，已改用 JSON 对象；结果仍在本机按格式核验。")
    elif mode == "none":
        found.append("接口不支持 JSON 格式输出，只靠提示词；结果仍在本机按格式核验。")
    if "stream" in relaxed:
        found.append("接口不支持流式输出，改为一次返回。")
    limit = result.get("output_limit") or {}
    if "max_tokens_param" in relaxed:
        found.append("输出上限要以 max_completion_tokens 发送，作答时也会这样发送。")
    if "max_tokens_clamp" in relaxed and limit.get("sent") and limit.get("requested"):
        found.append(
            f"输出上限 {limit.get('requested')} 超过了接口给出的最大值，已按 {limit['sent']} 发送；"
            f"可以在设置里把输出上限改为 {limit['sent']}。"
        )
    elif "max_tokens_clamp" in relaxed and limit.get("sent"):
        # Claude Code sends the model's own maximum when no limit is set; the endpoint took less.
        found.append(
            f"Claude Code 默认的输出上限超过了接口给出的最大值，已按 {limit['sent']} 发送；"
            f"可以在设置里把输出上限设为 {limit['sent']}，作答时就不用先被拒绝一次。"
        )
    if "max_tokens" in relaxed:
        found.append("接口不接受输出上限参数，作答时不会发送，用接口的默认值。")
    return found


def stop(timeout=10.0) -> None:
    """The server is stopping: a test that runs ends now, its Claude Code process with it."""
    STOPPING.set()
    try:
        if RUNNING.acquire(timeout=timeout):
            RUNNING.release()
    finally:
        STOPPING.clear()


def run(data_dir, values, *, cancelled=lambda: False) -> dict:
    """POST /api/settings/test: {provider, model?} → the result of one tiny real request.

    cancelled: whether the page stopped waiting; the test then ends at once (RunCancelled) and is
    not kept."""
    if (
        not isinstance(values, dict)
        or set(values) - {"provider", "model"}
        or "provider" not in values
    ):
        raise ConfigurationError("连接测试只接受 provider 和 model。")
    provider, model = values["provider"], values.get("model")
    if provider not in PROVIDERS:
        raise ConfigurationError("连接测试的模型接口无效。")
    if model is not None and (not isinstance(model, str) or not MODEL_NAME.fullmatch(model)):
        raise ConfigurationError("连接测试的模型名称无效。")
    tester = TESTERS.get(provider)
    if tester is None:
        raise ConfigurationError("这个模型接口暂时不能测试连接。")
    tester.check(data_dir, model)
    if not RUNNING.acquire(blocking=False):
        raise Busy("已有一次连接测试正在进行，请等它结束后再试。")
    try:
        return _run(data_dir, provider, model, tester, lambda: STOPPING.is_set() or cancelled())
    finally:
        RUNNING.release()


# What only an answer's error needs said: the test runs from the settings group it would send the
# user to, and has no assignment whose materials are kept or that was not answered.
TEST_WORDING = (
    (re.compile(r"请在设置 → 模型接口 → [^里，。；]{1,24}里"), "请"),
    (re.compile(r"可以在设置 → 模型接口里"), "可以"),
    (re.compile(r"请在设置 → 模型接口里"), "请"),
    (re.compile(r"请在设置里"), "请"),
    (re.compile(r"[；，]?资料已保留[。，；]?"), "；"),
    (re.compile(r"，未作答(?=[。，；])"), ""),
    (re.compile(r"；+(?=[。，]|$)"), ""),
    (re.compile(r"；(?=请重试)"), "，"),
)


def test_wording(error: str) -> str:
    """An answer's error as the test's: its first line without "未作答", "资料已保留" and where in
    the settings to go; the detail line (the server's words) as it is."""
    head, newline, detail = error.partition("\n")
    for pattern, words in TEST_WORDING:
        head = pattern.sub(words, head)
    head = head.strip("；，")
    if head and head[-1] not in "。！？":
        head += "。"
    return head + newline + detail


def _run(data_dir, provider, model, tester, cancelled) -> dict:
    marker = tester.marker(data_dir)
    started = time.monotonic()
    try:
        host, asked = tester.describe(data_dir, model)
    except ConfigurationError:
        host, asked = None, model
    result = {
        "ok": False,
        "provider": provider,
        "host": host,
        "requested_model": asked,
        "reported_model": None,
        "duration_ms": None,
        "structured_output_mode": None,
        "images": None,
        "streamed": None,
        "degradations": [],
        "usage": None,
        "cost_usd": None,
        "output_limit": None,
        "effort": None,
        "reply_json": None,
        "message": "",
        "notes": [],
        "error": None,
        "at": utc_now(),
    }
    try:
        done = tester.run(data_dir, model, cancelled)
        outcome = done["outcome"]
        details = outcome.get("provider") or {}
        result.update(
            ok=True,
            host=done.get("host") or details.get("host") or host,
            requested_model=done.get("requested_model") or asked,
            reported_model=outcome.get("model"),
            duration_ms=outcome.get("duration_ms"),
            structured_output_mode=details.get("structured_output_mode", "json_schema"),
            images=_images(details, True) if details else "sent",
            streamed=details.get("streamed", True),
            degradations=list(details.get("degradations") or []),
            usage=outcome.get("usage"),
            cost_usd=outcome.get("cost_usd"),
            output_limit=details.get("output_limit"),
            effort=details.get("effort"),
            reply_json=details.get("reply_json"),
        )
        if result["duration_ms"] is None:
            result["duration_ms"] = int((time.monotonic() - started) * 1000)
        result["notes"] = notes(result)
        requested, reported = result["requested_model"], result["reported_model"]
        result["message"] = " ".join(
            [
                f"连接正常：{requested} 用时 {_seconds(result['duration_ms'])}，"
                "返回了符合格式的 JSON。"
            ]
            + ([f"接口报告的模型是 {reported}。"] if reported and reported != requested else [])
            + result["notes"]
        )
        summary = " · ".join(["正常", *_tags(result), _seconds(result["duration_ms"])])
    except RunCancelled:
        # The page stopped waiting or the server stops: nobody reads this test, and it is not kept.
        raise RunCancelled("连接测试已停止：页面不再等待结果。") from None
    except WorkflowError as exc:
        error = test_wording(str(exc))
        details = getattr(exc, "details", None) or {}
        if details.get("structured_output_mode") == "json_schema" and str(exc).startswith(
            ("模型返回的 JSON 不符合要求的格式", "模型返回的内容不是可以解析的")
        ):
            # Today's "缺少字段 summary": a strict JSON Schema would not have let the reply miss a
            # field, so the endpoint took response_format and ignored it.
            head, newline, rest = error.partition("\n")
            error = head + SCHEMA_IGNORED + newline + rest
        result.update(
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error,
            message="连接测试没有通过。",
            degradations=list(details.get("degradations") or []),
            output_limit=details.get("output_limit"),
            effort=details.get("effort"),
        )
        summary = error.split("\n", 1)[0][:160]
    except Exception:  # never a crash of the server, and no private detail on the page
        result.update(
            duration_ms=int((time.monotonic() - started) * 1000),
            error="连接测试时本机出错，没有得到结果。请稍后再试。",
            message="连接测试没有通过。",
        )
        summary = result["error"]
    # A test of settings saved over or deleted meanwhile says nothing about the ones now saved.
    if tester.marker(data_dir) == marker:
        record_test(
            data_dir,
            provider,
            {
                "at": result["at"],
                "ok": result["ok"],
                "model": result["requested_model"],
                "summary": summary,
            },
        )
    return result


def _tags(result) -> list[str]:
    relaxed = set(result.get("degradations") or [])
    if result.get("images") == "refused":
        relaxed.add("images")
    tags = [words for step, words in TAGS if step in relaxed]
    if result.get("structured_output_mode") == "json_schema" and result.get("reply_json") == (
        "extracted"
    ):
        tags.append("未按 JSON Schema 输出")
    return tags
