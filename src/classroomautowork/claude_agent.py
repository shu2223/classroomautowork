"""One tool-free Claude Code turn per answer. Course text is data, so the model gets no tools.

Billing is the Claude API: `--bare` never reads the machine's Claude login, so ANTHROPIC_API_KEY
must be set in the service environment, and a turn without it stops before any request is made.
Prompts go through stdin, never a shell.

The same turn also serves an Anthropic-compatible endpoint (a Claude Code relay): the CLI is then
pointed at it with ANTHROPIC_BASE_URL, and the key reaches it through an apiKeyHelper given with
--settings that prints a private environment variable, so Claude Code sends it both as X-Api-Key
and as Authorization: Bearer (checked with the real CLI against a local fake server) and the key
is in no argument list. An optional output limit goes as CLAUDE_CODE_MAX_OUTPUT_TOKENS, which the
CLI sends as the request's max_tokens; with none of these options the arguments and the
environment are exactly what they always were.
"""

import base64
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .errors import RequestFailed, RunCancelled, WorkflowError
from .openai_compat import NO_CHANNEL, detail_line, with_detail

MODELS = (
    ("claude-opus-5-5", "Claude Opus 5.5"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
    ("claude-haiku-5-5", "Claude Haiku 5.5"),
    ("claude-fable-5-1", "Claude Fable 5.1"),
)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_EFFORT = "high"
TURN_SECONDS = 15 * 60
IDLE_SECONDS = 5 * 60
ACTIVITY = {
    "thinking": "Claude 正在分析题目与资料",
    "text": "Claude 正在整理回答",
    "tool_use": "Claude 正在输出结构化答案，完整返回后再核验",
}
KEPT = "资料已保留。"
# The Anthropic-compatible endpoint: Claude Code reads its address from ANTHROPIC_BASE_URL and its
# key from the apiKeyHelper's output; the helper only prints this variable of the CLI's own
# environment, so neither --settings nor any argument holds the key.
RELAY_KEY_ENV = "CLASSROOMAUTOWORK_RELAY_KEY"
KEY_HELPER = {"apiKeyHelper": "printenv " + RELAY_KEY_ENV}
LIMIT_ENV = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
RETRIES_ENV = "CLAUDE_CODE_MAX_RETRIES"
# Claude Code 2.1.318 renames some model ids before it sends anything (claude-opus-4-1 and
# claude-opus-4-20250514 become claude-opus-5-5, say): an endpoint's ids go as they are.
REMAP_ENV = "CLAUDE_CODE_DISABLE_LEGACY_MODEL_REMAP"
# What the endpoint's turn must not inherit from the service: other credentials (ANTHROPIC_*,
# OAuth tokens, a Bedrock bearer token, client certificates), switches and addresses that would
# send the turn somewhere else (Bedrock, Vertex, Foundry, another base URL), custom headers and
# body fields that would go to the endpoint, and the settings the turn sets itself.
ENDPOINT_ENV_PREFIXES = (
    "ANTHROPIC_",
    "CLAUDE_CODE_USE_",
    "CLAUDE_CODE_SKIP_",
    "CLAUDE_CODE_OAUTH_",
    "CLAUDE_CODE_API_",
    "CLAUDE_CODE_CLIENT_",
    "AWS_BEARER_TOKEN_",
)
ENDPOINT_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_CUSTOM_OAUTH_URL",
    "CLAUDE_CODE_EXTRA_BODY",
    LIMIT_ENV,
    RETRIES_ENV,
    REMAP_ENV,
)
# Room the turn needs to be sent once more at the endpoint's maximum: with less left of the turn's
# time limit, the refusal is reported instead (with the maximum to set).
RETRY_ROOM_SECONDS = 30
_N = r"(\d{1,3}(?:,\d{3})+|\d+)"
# The Messages API's refusal of a max_tokens above the model's maximum, as Claude Code passes it on.
TOO_LARGE = re.compile(
    rf"max_tokens:\s*{_N}\s*>\s*{_N},?\s*which is the maximum allowed number of output tokens", re.I
)
# Claude Code's words when the answer stopped at max_tokens (stop_reason max_tokens).
TRUNCATED = re.compile(rf"exceeded the {_N} output token maximum", re.I)
MODEL_MISSING = re.compile(r"issue with the selected model|model_not_found|not_found_error", re.I)
# Claude Code's words for an HTTP 200 that is no Messages API reply (a web page, a JSON error).
MALFORMED = re.compile(r"empty or malformed response", re.I)
# Claude Code drops an image the API refused and carries on; the answer then rests on text alone.
IMAGE_REMOVED = re.compile(r"image\b.{0,80}\bcould not be processed|\[Image removed", re.I)
# The model Claude Code names its own messages (an error it reports as the assistant's).
SYNTHETIC = "<synthetic>"


def _inherited_setting(name) -> bool:
    return name in ENDPOINT_ENV or name.startswith(ENDPOINT_ENV_PREFIXES)


def claude_command() -> list[str]:
    path = os.environ.get("CLASSROOMAUTOWORK_CLAUDE") or shutil.which("claude")
    if not path:
        raise WorkflowError("未找到本机 Claude Code，资料已保留。")
    return [path]


def model_catalog() -> dict:
    return {
        "models": [
            {
                "model": model,
                "displayName": name,
                "defaultReasoningEffort": DEFAULT_EFFORT,
                "supportedReasoningEfforts": [{"reasoningEffort": x} for x in EFFORTS],
            }
            for model, name in MODELS
        ],
        "default_model": DEFAULT_MODEL,
        "default_effort": DEFAULT_EFFORT,
        "source": "classroomautowork Claude model list",
        "checked_at": datetime.now(UTC).isoformat(),
        "note": "实际完成 AI 回合后才确认所选模型可用。",
    }


def image_block(path: Path) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        },
    }


def command_line(command, *, model, effort, schema, helper=False) -> list[str]:
    """The CLI's arguments. helper adds the --settings that names the key helper (never the key),
    and --include-partial-messages: without it Claude Code writes nothing while a reply streams,
    so a slow endpoint that is still sending would look silent to the idle limit."""
    arguments = [
        *command,
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--tools",
        "",
        "--disallowedTools",
        "mcp__*",
        "--no-session-persistence",
        "--bare",
        "--json-schema",
        json.dumps(schema, ensure_ascii=False),
    ]
    if effort:
        arguments += ["--effort", effort]
    if helper:
        arguments += ["--settings", json.dumps(KEY_HELPER), "--include-partial-messages"]
    return arguments


def turn_environment(api_key, *, base_url=None, auth=None, max_output_tokens=None, retries=None):
    """The CLI's environment, or None for the service's own (no key given, no option set).

    The Claude API: the service environment plus ANTHROPIC_API_KEY, as always. An endpoint
    (auth "helper"): without the inherited settings ENDPOINT_ENV and ENDPOINT_ENV_PREFIXES name,
    plus its address in ANTHROPIC_BASE_URL, its key in RELAY_KEY_ENV for the helper, model ids
    sent as they are (REMAP_ENV) and, when given, how often Claude Code retries a failed request
    (RETRIES_ENV; a connection test retries once, so the endpoint's own reason comes back at
    once). A limit adds LIMIT_ENV."""
    if auth == "helper":
        env = {k: v for k, v in os.environ.items() if not _inherited_setting(k)}
        env["ANTHROPIC_BASE_URL"] = base_url
        env[RELAY_KEY_ENV] = api_key
        env[REMAP_ENV] = "1"
        if retries is not None:
            env[RETRIES_ENV] = str(int(retries))
    else:
        env = {**os.environ, "ANTHROPIC_API_KEY": api_key} if api_key else None
    if max_output_tokens:
        env = {**(os.environ if env is None else env), LIMIT_ENV: str(int(max_output_tokens))}
    return env


def _span(seconds) -> str:
    seconds = int(seconds)
    return f"{seconds // 60} 分钟" if seconds >= 120 and seconds % 60 == 0 else f"{seconds} 秒"


def _number(text) -> int:
    return int(text.replace(",", ""))


class _Where:
    """How errors name the service: the Claude API, or the endpoint by its host. doubt_address:
    the endpoint's saved check did not see an Anthropic-format model list (it listed none, or
    listed in OpenAI's shape), so a 404 is more likely a wrong address than a wrong model."""

    def __init__(self, host=None, doubt_address=False):
        self.host = host
        self.relay = host is not None
        self.name = f"Anthropic 兼容接口（{host}）" if host else "Claude API"
        # The name before Chinese words: a Latin name takes a space ("Claude API 出错").
        self.lead = self.name + ("" if self.relay else " ")
        self.place = "设置 → 模型接口 → Anthropic 兼容" if host else "设置 → 模型接口 → Claude API"
        self.doubt_address = bool(doubt_address) and self.relay


class _TooLarge(Exception):
    """The endpoint refused the output limit Claude Code sent, naming its maximum."""

    def __init__(self, requested, maximum, detail):
        super().__init__(detail)
        self.requested, self.maximum, self.detail = requested, maximum, detail


def truncated_message(limit, *, clamped=False, sent=False, where=None) -> str:
    """stop_reason max_tokens: the answer hit the output limit (the one set, the endpoint's
    maximum after a clamp, or Claude Code's default for the model when none was set)."""
    lead = (where or _Where()).lead
    if clamped:
        return (
            f"模型的输出达到了输出上限（{limit} tokens），结果被截断（stop_reason: max_tokens）。"
            f"这已经是 {lead}说这个模型最多能输出的 token 数；请换用输出上限更大的模型，"
            "或减少这次的资料后重试；" + KEPT
        )
    if sent:
        return (
            f"模型的输出达到了输出上限（{limit} tokens），结果被截断（stop_reason: max_tokens）。"
            "可以在设置 → 模型接口里调高「输出上限」后重试（推理用的 token 也算在内）；" + KEPT
        )
    return (
        f"模型的输出达到了 Claude Code 默认的输出上限（{limit} tokens），结果被截断"
        "（stop_reason: max_tokens）。可以在设置 → 模型接口里填写更大的「输出上限」后重试，"
        "或换用输出上限更大的模型、减少资料；" + KEPT
    )


def _failure(event, reported, where, *, key, model, limit, clamped):
    """The error for a result event that is not a success: what happened and what to do, with the
    CLI's own words (never a key) on the detail line."""
    text = str(event.get("result") or event.get("subtype") or "error")
    detail = detail_line(text, key)
    status = event.get("api_error_status")
    found = TOO_LARGE.search(text)
    if found:
        return _TooLarge(_number(found[1]), _number(found[2]), detail)
    found = TRUNCATED.search(text)
    if found:
        return WorkflowError(
            with_detail(
                truncated_message(
                    _number(found[1]), clamped=clamped, sent=bool(limit), where=where
                ),
                detail,
            )
        )
    rate = reported["rate_limit"] or {}
    if rate.get("status") == "rejected" or status == 429:
        if not where.relay and rate.get("status") == "rejected":
            return WorkflowError("Claude 用量已达上限，请等额度恢复后重试；资料已保留。")
        return RequestFailed(
            with_detail(f"{where.lead}限流或用量已达上限，请稍后重试；" + KEPT, detail)
        )
    if status in (401, 403):
        return _key_refused(status, where, detail)
    if where.relay and NO_CHANNEL.search(text):
        return _no_channel(status, where, model, detail)
    if status == 404 or MODEL_MISSING.search(text):
        return _not_found(status, where, model, detail)
    if isinstance(status, int) and status >= 500:
        return RequestFailed(
            with_detail(f"{where.lead}出错（HTTP {status}），请稍后重试；" + KEPT, detail)
        )
    if where.relay and MALFORMED.search(text):
        # An HTTP 200 that is a web page or an error, not a Messages API reply: the address.
        return RequestFailed(
            with_detail(
                f"{where.lead}返回的不是 Anthropic 格式的回复（可能是网页或一条错误信息）。"
                f"请在{where.place}里检查地址是不是中转站给 Claude Code 用的 ANTHROPIC_BASE_URL"
                "（不带 /v1/messages）；" + KEPT,
                detail,
            )
        )
    if where.relay:
        return WorkflowError(with_detail(f"{where.lead}的回合未成功，资料已保留，请重试。", detail))
    # On failure `result` holds the CLI's error text, such as an invalid key.
    return WorkflowError(f"Claude 回合未成功（{detail[:200]}），资料已保留，请重试。")


def _not_found(status, where, model, detail):
    """A 404: Claude Code words every one as "an issue with the selected model", whether the model
    or the address is missing, so an endpoint's message names both (the address first when the
    saved check did not see an Anthropic-format model list)."""
    code = f"HTTP {status or 404}"
    if where.doubt_address:
        message = (
            f"{where.lead}的这个地址可能不提供 Anthropic 格式的 /v1/messages，或找不到模型 {model}"
            f"（{code}）。保存时它没有给出 Anthropic 格式的模型列表，可能是 OpenAI 格式接口的地址；"
            f"请在{where.place}里改填中转站给 Claude Code 用的 ANTHROPIC_BASE_URL，"
            "或检查模型名称后重试；" + KEPT
        )
    elif where.relay:
        message = (
            f"{where.lead}找不到模型 {model}，或这个密钥不能用它，或这个地址不提供 Anthropic 格式的"
            f" /v1/messages（{code}）。请在{where.place}里检查模型名称和接口地址后重试；" + KEPT
        )
    else:
        message = (
            f"{where.lead}找不到模型 {model}，或这个密钥不能用它（{code}）。"
            f"请在{where.place}里检查模型名称后重试；" + KEPT
        )
    return RequestFailed(with_detail(message, detail))


def _no_channel(status, where, model, detail):
    """new-api / one-api: "当前分组 default 下对于模型 X 无可用渠道" is the key's group or the model,
    not a passing outage, so the advice is the model and the relay, not "try again later"."""
    code = f"（HTTP {status}）" if isinstance(status, int) else ""
    return RequestFailed(
        with_detail(
            f"{where.lead}没有可用于模型 {model} 的渠道{code}：这个模型不在密钥（令牌）的分组里，"
            f"或中转站暂时没有它的渠道。请在{where.place}里换用中转站提供的模型，或联系中转站；"
            + KEPT,
            detail,
        )
    )


def _key_refused(status, where, detail="", *, early=False):
    """A refused key. A relay also answers 401/403 for an expired token or one whose quota is used
    up (new-api, one-api). early: Claude Code's first retry, which carries only its category
    ("authentication_failed"); the connection test, which retries once, shows the relay's words."""
    if where.relay:
        message = (
            f"{where.lead}拒绝了密钥，或令牌已过期、额度已用尽（HTTP {status}），未作答。"
            f"请在{where.place}里检查密钥是否属于这个接口，以及令牌和余额"
            + ("（点“测试连接”可以看到接口给出的原因）" if early else "")
            + "；"
            + KEPT
        )
    else:
        message = (
            f"Anthropic 拒绝了 Claude API 密钥（HTTP {status}），未作答。请在设置里检查或更换密钥；"
            + KEPT
        )
    return RequestFailed(with_detail(message, detail))


def _model_mismatch(model, used, where, key):
    used = detail_line(used, key, 200) or "未知"
    if where.relay:
        return WorkflowError(
            f"Claude Code 把模型名 {model} 换成了 {used}（它把这个名字当作别名），与所选模型不一致，"
            f"已停止生成，没有发出请求。请在{where.place}里填写接口给的完整模型 ID；" + KEPT
        )
    return WorkflowError(
        f"Claude 实际使用的模型（{used}）与所选模型（{model}）不一致，已停止生成。"
    )


def _time_limit(where, turn_seconds, idle_seconds, retry):
    """The turn's time limit passed. After Claude Code's retries the endpoint did answer, with
    errors, so the message says so rather than "no output"."""
    if retry:
        status, attempt = retry
        code = f" HTTP {status}" if isinstance(status, int) else "错误"
        tried = f"重试 {attempt} 次" if isinstance(attempt, int) else "多次重试"
        return RequestFailed(
            f"{where.lead}连续返回{code}，Claude Code {tried}仍未成功，已停止等待。"
            f"请稍后重试；一直这样时，请在{where.place}里检查模型名称和接口地址；" + KEPT
        )
    if (turn_seconds, idle_seconds) == (TURN_SECONDS, IDLE_SECONDS) and not where.relay:
        return WorkflowError(
            "Claude 回合超过 15 分钟或 5 分钟没有任何输出，已停止本次等待；资料已保留，请重试。"
        )
    who = f"{where.lead}的回合" if where.relay else "Claude 回合"
    return WorkflowError(
        f"{who}超过 {_span(turn_seconds)}或 {_span(idle_seconds)}没有任何输出，"
        "已停止本次等待；资料已保留，请重试。"
    )


def run_turn(
    content: list[dict],
    schema: dict,
    *,
    model: str,
    effort: str | None,
    cwd: Path,
    cancelled=lambda: False,
    on_activity=lambda _label: None,
    api_key: str | None = None,
    command=None,
    clock=time.monotonic,
    base_url: str | None = None,
    auth: str | None = None,
    host: str | None = None,
    max_output_tokens: int | None = None,
    turn_seconds=None,
    idle_seconds=None,
    retries: int | None = None,
    doubt_address: bool = False,
) -> dict:
    """Return the validated structured output plus what the CLI reported about the turn.

    base_url and auth "helper" (with api_key and host) send the turn to an Anthropic-compatible
    endpoint; max_output_tokens sets the output limit. An endpoint that refuses the limit Claude
    Code sent (its own default when none was set) and names its maximum gets the same turn once
    more with that maximum, within the same time limit: output_limit {requested, sent, param} and
    degradations ["max_tokens_clamp"] say so. An image Claude Code had to drop is "images" in
    degradations. retries (an endpoint only) sets how often Claude Code retries a failed request;
    doubt_address words a 404 as more likely the address (see _Where).

    "model" is the model the server's replies named (a relay may serve another one than asked),
    or the one Claude Code started with when no reply named one. A WorkflowError raised after
    the turn began carries `details` {output_limit, degradations} for the generation record."""
    if auth not in (None, "helper") or (auth == "helper" and not (base_url and api_key)):
        raise WorkflowError("模型接口的设置不完整，未调用 Claude Code。")
    where = _Where(host or base_url if auth == "helper" else None, doubt_address)
    turn_seconds = TURN_SECONDS if turn_seconds is None else turn_seconds
    idle_seconds = IDLE_SECONDS if idle_seconds is None else idle_seconds
    seen = {"images_removed": False}
    options = dict(
        content=content,
        schema=schema,
        model=model,
        effort=effort,
        cwd=cwd,
        cancelled=cancelled,
        on_activity=on_activity,
        api_key=api_key,
        command=command,
        clock=clock,
        base_url=base_url,
        auth=auth,
        where=where,
        turn_seconds=turn_seconds,
        idle_seconds=idle_seconds,
        retries=retries if auth == "helper" else None,
        seen=seen,
    )
    degradations, sent = [], max_output_tokens
    # One time limit for the whole turn, a resend at the endpoint's maximum included.
    deadline = clock() + turn_seconds

    def record():
        relaxed = degradations + (["images"] if seen["images_removed"] else [])
        limit = (
            {"requested": max_output_tokens, "sent": sent, "param": LIMIT_ENV}
            if max_output_tokens or "max_tokens_clamp" in relaxed
            else None
        )
        return {"output_limit": limit, "degradations": relaxed}

    try:
        try:
            outcome = _turn(**options, limit=sent, clamped=False, deadline=deadline)
        except _TooLarge as refused:
            if not 1 <= refused.maximum < refused.requested:
                raise _limit_refused(refused, where) from None
            if deadline - clock() < RETRY_ROOM_SECONDS:
                # Too little of the time limit is left for the whole turn once more.
                raise _limit_refused(refused, where) from None
            sent = refused.maximum
            degradations.append("max_tokens_clamp")
            on_activity(f"{where.lead}的输出上限是 {sent} tokens，正在按 {sent} 重新发起请求")
            try:
                outcome = _turn(**options, limit=sent, clamped=True, deadline=deadline)
            except _TooLarge as again:
                raise _limit_refused(again, where) from None
    except WorkflowError as exc:
        if not isinstance(exc, RunCancelled):
            exc.details = record()
        raise
    outcome.update(record())
    return outcome


def _limit_refused(refused, where):
    return RequestFailed(
        with_detail(
            f"{where.lead}不接受 {refused.requested} tokens 的输出上限，最多 {refused.maximum}。"
            f"请在{where.place}里把「输出上限」改为 {refused.maximum} 或更小后重试；" + KEPT,
            refused.detail,
        )
    )


def _served_model(event, key):
    """The model a reply of the server named: Claude Code's init event only repeats --model, and
    its own error messages carry "<synthetic>"."""
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    served = message.get("model")
    if event.get("error") or not isinstance(served, str) or not served or served == SYNTHETIC:
        return None
    return detail_line(served, key, 200) or None


def _turn(
    *,
    content,
    schema,
    model,
    effort,
    cwd,
    cancelled,
    on_activity,
    api_key,
    command,
    clock,
    base_url,
    auth,
    where,
    turn_seconds,
    idle_seconds,
    retries,
    seen,
    limit,
    clamped,
    deadline,
) -> dict:
    arguments = command_line(
        command or claude_command(),
        model=model,
        effort=effort,
        schema=schema,
        helper=auth == "helper",
    )
    message = {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }
    try:
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            env=turn_environment(
                api_key, base_url=base_url, auth=auth, max_output_tokens=limit, retries=retries
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError:
        raise WorkflowError("无法启动本机 Claude Code，资料已保留。") from None
    events = queue.Queue()

    def read():
        try:
            for line in process.stdout:
                try:
                    events.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            events.put(None)

    threading.Thread(target=read, daemon=True).start()
    last = clock()
    reported = {
        "model": None,
        "served": None,
        "session_id": None,
        "rate_limit": None,
        "api_key_source": None,
    }
    retry = None  # (status, attempt) of Claude Code's latest retry
    try:
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.close()
        while True:
            if cancelled():
                raise RunCancelled("已暂停 Claude，资料已保留。")
            now = clock()
            if now > deadline or now - last > idle_seconds:
                raise _time_limit(where, turn_seconds, idle_seconds, retry)
            try:
                event = events.get(timeout=0.2)
            except queue.Empty:
                continue
            if event is None:
                raise WorkflowError("Claude Code 进程已结束，没有返回完整结果。")
            last = clock()
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                reported["model"], reported["session_id"] = (
                    event.get("model"),
                    event.get("session_id"),
                )
                reported["api_key_source"] = event.get("apiKeySource")
                if reported["api_key_source"] in (None, "none"):
                    if where.relay:
                        raise WorkflowError(
                            f"Claude Code 没有拿到 {where.lead}的密钥，未发出请求。"
                            f"请在{where.place}里重新保存这个接口；" + KEPT
                        )
                    raise WorkflowError(
                        "还没有设置 Claude API 密钥，未调用 Claude。请在作业助手的“设置”里填写；"
                        "资料已保留。"
                    )
                if reported["model"] != model:
                    raise _model_mismatch(model, reported["model"], where, api_key)
                if event.get("tools") not in ([], ["StructuredOutput"]) or event.get("mcp_servers"):
                    raise WorkflowError("Claude 会话带有额外工具，已停止生成。")
            elif kind == "system" and event.get("subtype") == "api_retry":
                # Claude Code retries a refused key ten times over about three minutes; a key
                # that was refused once will be refused again, so the turn stops here. With its
                # retries set low (a connection test) the result comes in seconds anyway, and it
                # carries the endpoint's own words, which this event does not.
                status = event.get("error_status")
                if status in (401, 403) and retries is None:
                    raise _key_refused(
                        status, where, detail_line(event.get("error"), api_key), early=True
                    )
                attempt, most = event.get("attempt"), event.get("max_retries")
                retry = (status, attempt)
                on_activity(
                    f"{where.lead}暂时没有正常响应"
                    + (f"（HTTP {status}）" if isinstance(status, int) else "")
                    + (f"，Claude Code 正在第 {attempt}/{most} 次重试" if attempt and most else "")
                )
            elif kind == "rate_limit_event":
                reported["rate_limit"] = event.get("rate_limit_info")
            elif kind == "stream_event":
                # --include-partial-messages (an endpoint's turn): the reply as it streams.
                part = event.get("event") if isinstance(event.get("event"), dict) else {}
                block = part.get("content_block")
                if part.get("type") == "content_block_start" and isinstance(block, dict):
                    if block.get("type") in ACTIVITY:
                        on_activity(ACTIVITY[block["type"]])
            elif kind == "assistant":
                blocks = event.get("message", {}).get("content", [])
                if event.get("error") and any(
                    IMAGE_REMOVED.search(str(b.get("text") or "")) for b in blocks
                ):
                    seen["images_removed"] = True
                reported["served"] = reported["served"] or _served_model(event, api_key)
                for block in blocks:
                    if block.get("type") in ACTIVITY:
                        on_activity(ACTIVITY[block["type"]])
            elif kind == "result":
                if event.get("is_error") or event.get("subtype") != "success":
                    raise _failure(
                        event,
                        reported,
                        where,
                        key=api_key,
                        model=model,
                        limit=limit,
                        clamped=clamped,
                    )
                if not isinstance(event.get("structured_output"), dict):
                    raise WorkflowError("Claude 没有返回符合格式的结构化结果。")
                return {
                    "output": event["structured_output"],
                    "model": reported["served"] or reported["model"],
                    "session_id": reported["session_id"] or event.get("session_id"),
                    "api_key_source": reported["api_key_source"],
                    "usage": event.get("usage"),
                    "cost_usd": event.get("total_cost_usd"),
                    "duration_ms": event.get("duration_ms"),
                }
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stdout.close()
