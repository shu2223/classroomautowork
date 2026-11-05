"""One OpenAI-compatible chat completion per answer: the alternative to the Claude path.

The turn is POST {base_url}/chat/completions with one user message (the prompt and the page
images), a strict JSON schema as response_format, streamed. A background thread reads the reply
into a queue, so a pause, the 15-minute turn limit and the 5-minute idle limit end the wait and
cut the connection, as on the Claude path. Servers differ in what they accept: a 400 or 422 that
names a feature drops that feature and repeats the same request, one step per feature and never
twice. The answer is validated here against the schema, so a lenient server cannot slip a
malformed result through.

The key travels only in the Authorization header to the configured address (redirects are never
followed, since they would carry the header elsewhere) and is never put into an error, a record or
a log: server messages, and whatever else the server says that is kept (its reply id, model name,
finish reason, a listed model id), are trimmed and scrubbed of anything shaped like a credential.

An error of the request itself (a limit, the quota, the size, the server, the network, the time
limits) is a RequestFailed, which drafting.generate_answer does not spend its one automatic repair
on; an answer that came back but does not fit stays a plain WorkflowError and is repaired once.
"""

import http.client
import ipaddress
import json
import queue
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .errors import ConfigurationError, RequestFailed, RunCancelled, WorkflowError

TURN_SECONDS = 15 * 60
IDLE_SECONDS = 5 * 60
CONNECT_SECONDS = 30
LIST_SECONDS = 20
EFFORTS = ("low", "medium", "high")
SCHEMA_NAME = "assignment_review"
API_KEY_SOURCE = "openai_compatible"
USER_AGENT = "classroomautowork (OpenAI-compatible client)"
MAX_REPLY_BYTES = 32_000_000
MAX_ERROR_BYTES = 65_536
MAX_LIST_BYTES = 8_000_000
ACTIVITY = {
    "reasoning": "模型正在分析题目与资料",
    "content": "模型正在输出结构化答案，完整返回后再核验",
}
KEPT = "资料已保留。"
HIDDEN = "[已隐藏]"
IDLE_MESSAGE = (
    "OpenAI 兼容接口连续 5 分钟没有任何输出，已停止等待并断开连接。"
    "请稍后重试，或换用更快的模型；" + KEPT
)
TURN_MESSAGE = (
    "OpenAI 兼容接口的回合超过 15 分钟仍未完成，已停止等待并断开连接。"
    "请稍后重试，或换用更快的模型；" + KEPT
)
# Each step drops one feature a server refused; json_schema falls back to json_object, then to a
# prompt-only request. The order is the order the request is relaxed in. (A refusal for size
# drops the page images as its own step, images_size; see degradable.)
STEPS = ("reasoning_effort", "stream_options", "json_schema", "json_object", "images", "stream")
STEP_WORDS = {
    # Only words about the effort or thinking itself: a length error can mention "reasoning
    # tokens" in passing. Servers name the field (reasoning_effort, xAI's reasoningEffort,
    # Gemini's thinking_config) or say the model cannot think, as Ollama does with
    # '"llama3.2" does not support thinking'.
    "reasoning_effort": re.compile(
        r"reasoning[\s_.-]*effort|thinking[\s_.-]*(?:config|budget)"
        r"|do(?:es)?(?:n['\u2019]?t|\s+not)\s+support\s+(?:thinking|reasoning)"
        r"|(?:thinking|reasoning)\s+(?:is\s+)?(?:not\s+(?:supported|enabled|available)|unsupported)"
    ),
    "stream_options": re.compile(r"stream[\s_.-]*options|include[\s_.-]*usage"),
    # Gemini names its own field: generation_config.response_schema / response_json_schema.
    "json_schema": re.compile(
        r"response[\s_.-]*format|json[\s_.-]*schema|response[\s_.-]*(?:json[\s_.-]*)?schema"
        r"|\bstrict\b|structured"
    ),
    "json_object": re.compile(r"response[\s_.-]*format|json[\s_.-]*object|json[\s_.-]*mode"),
    "images": re.compile(r"image|vision|multi[\s_.-]*modal"),
    "stream": re.compile(r"\bstream"),
}
# A request longer than the model can read, in the words of OpenAI, vLLM, DeepSeek, Mistral,
# DashScope, Gemini and Anthropic-style proxies.
TOO_LONG = re.compile(
    r"context[\s_.-]*(?:length|window|limit)|maximum[\s_.-]*context|too[\s_.-]*many[\s_.-]*tokens"
    r"|(?:prompt|input)[\s_.-]*(?:is[\s_.-]*)?too[\s_.-]*long|input[\s_.-]*(?:length|token)"
    r"|reduce[\s_.-]*the[\s_.-]*length|exceeds?\b[^.]{0,60}\btokens?\b"
)
# A request body larger than the server takes (a 413, or Gemini's 400 about its payload size).
TOO_LARGE = re.compile(
    r"payload[\s_.-]*(?:size|too)|(?:request|body|entity)[\s_.-]*(?:is[\s_.-]*)?too[\s_.-]*large"
    r"|(?:request|body)[\s_.-]*size|exceeds?\b[^.]{0,60}\bbytes?\b|too[\s_.-]*many[\s_.-]*images"
)

# ---------------------------------------------------------------------------- scrubbing

CREDENTIAL = re.compile(
    r"\b(?:sk|pk|rk|gsk|xai|nvapi)-[A-Za-z0-9._*-]{5,}[A-Za-z0-9*]"
    r"|\bhf_[A-Za-z0-9]{6,}"
    r"|\bAIza[0-9A-Za-z_-]{20,}"
    # A ****-masked token. It is matched only from where a token starts: tried from every
    # position of a long run without spaces, the leading [^...]* made redact() quadratic.
    r"|(?<![^\s\"'`,;])[^\s\"'`,;]*\*{4,}[^\s\"'`,;.]*"
)
SCHEME_SECRET = re.compile(r"(?i)\b(bearer|basic)(\s+)[^\s\"'`,;]+")
NAMED_SECRET = re.compile(
    r"(?i)\b(authorization|x-api-key|api[-_ ]?key|access[-_ ]?token)(\s*[:=]\s*)[^\s\"'`,;]+"
)
# Words long enough to carry a piece of the key: six characters, the size of the window below,
# so an echoed prefix such as "sk-REVI" goes too.
WORD = re.compile(r"[A-Za-z0-9_\-.~+/=]{6,}")
# Shorter than this a "key" is a placeholder for a local server that checks none ("ollama").
SECRET_LENGTH = 8


def redact(text, key=None) -> str:
    """Server words without the key, an Authorization value or anything shaped like a key."""
    text = str(text or "")
    if key and len(key) >= SECRET_LENGTH:
        text = text.replace(key, HIDDEN)
    elif key:
        # A placeholder is hidden only where it stands alone: hidden inside words it would garble
        # the server's message ("max_tokens" with the key "x").
        text = re.sub(rf"(?<![\w-]){re.escape(key)}(?![\w-])", HIDDEN, text)
    text = SCHEME_SECRET.sub(lambda m: m[1] + m[2] + HIDDEN, text)
    text = NAMED_SECRET.sub(lambda m: m[1] + m[2] + HIDDEN, text)
    text = CREDENTIAL.sub(HIDDEN, text)
    pieces = _random_pieces(key) if key and len(key) >= SECRET_LENGTH else set()
    if pieces:
        # A server may echo a masked or partial key: any word sharing six characters with it goes.

        def scrub(match):
            word = match[0]
            hit = any(word[i : i + 6] in pieces for i in range(len(word) - 5))
            return HIDDEN if hit else word

        text = WORD.sub(scrub, text)
    return text


CLASSES = (re.compile("[a-z]"), re.compile("[A-Z]"), re.compile("[0-9]"))


def _random_pieces(key) -> set:
    """The key's six-character windows that look random: two of lower case, upper case and
    digits. A window of one kind is a word or a number ("sk-no-key-required" is a placeholder
    llama.cpp suggests; a demo key may contain "staging"), and hiding every server word that
    shares one would garble a model name or "field required" without hiding anything secret."""
    windows = {key[i : i + 6] for i in range(len(key) - 5)}
    return {x for x in windows if sum(bool(c.search(x)) for c in CLASSES) >= 2}


def holds_key(text, key) -> bool:
    """A server-given name (a listed model id) that is the key, carries it, or looks like a key."""
    text = str(text or "")
    if key and (text == key or (len(key) >= SECRET_LENGTH and key in text)):
        return True
    return bool(CREDENTIAL.search(text))


def short(text, limit=300) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def detail_line(text, key=None, limit=300) -> str:
    """The server's own words for the muted line under an error: trimmed, never the key.

    Only the start is scrubbed and shown (a reply that is not JSON can be megabytes); the margin
    past `limit` is wider than any key, so a key cut at the edge is never in what is shown."""
    head = re.sub(r"\s+", " ", str(text or "")).strip()[: limit + 4000]
    return short(redact(head, key), limit)


def with_detail(message, detail) -> str:
    """An error's first line says what happened and what to do; the second is the server's words."""
    return message + ("\n" + detail if detail else "")


def error_fields(body) -> tuple[str, str]:
    """(message, text to look for feature names in) from an error body of any common shape."""
    text = (
        body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
    )
    try:
        return _error_fields(text)
    except (TypeError, AttributeError, ValueError, RecursionError):
        return text, text.lower()  # an error body of an odd shape is still read as its text


def _error_fields(text) -> tuple[str, str]:
    data = _json(text)
    if data is _NOT_JSON:
        data = None
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        error = data.get("error", data)
        if isinstance(error, str):
            return error, error.lower()
        if isinstance(error, dict):
            message = error.get("message") or error.get("msg") or error.get("error")
            detail = error.get("detail")
            if not message and isinstance(detail, list):
                # FastAPI validation errors: [{"loc": [...], "msg": "..."}]
                message = "; ".join(
                    ".".join(str(x) for x in item.get("loc", [])) + ": " + str(item.get("msg", ""))
                    for item in detail
                    if isinstance(item, dict)
                )
            elif not message and detail:
                message = detail
            if isinstance(message, (list, dict)):
                message = json.dumps(message, ensure_ascii=False)
            fields = [message] + [error.get(k) for k in ("param", "code", "type", "status")]
            return str(message or text), " ".join(str(x) for x in fields if x).lower()
    if re.search(r"(?i)<html|<!doctype", text):
        title = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
        plain = title[1] if title else re.sub(r"(?s)<[^>]+>", " ", text)
        return plain, plain.lower()
    return text, text.lower()


def _reason(exc) -> str:
    reason = getattr(exc, "reason", None) or exc
    return str(reason) or type(reason).__name__


def _local_host(host) -> bool:
    host = (host or "").strip("[]").lower()
    if host == "localhost" or host.endswith((".local", ".lan", ".internal", ".localhost")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def sends_key_in_clear(base_url) -> bool:
    """http to a machine outside this host and its private network: the key crosses in clear."""
    parts = urlsplit(base_url)
    return parts.scheme == "http" and not _local_host(parts.hostname)


# ---------------------------------------------------------------------------- HTTP


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A followed redirect would take the Authorization header to wherever it points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Line:
    """The socket of the one request in flight, so a stop can cut it at once.

    Closing a socket does not wake a thread blocked reading it; shutting it down does.
    """

    def __init__(self, read_timeout):
        self.read_timeout = read_timeout
        self.sock = None
        self.stopped = False
        self.lock = threading.Lock()

    def attach(self, sock):
        with self.lock:
            if self.stopped:
                _shutdown(sock)
                raise OSError("request stopped")
            sock.settimeout(self.read_timeout)
            self.sock = sock

    def stop(self):
        with self.lock:
            self.stopped = True
            sock, self.sock = self.sock, None
        if sock is not None:
            _shutdown(sock)


def _shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def opener(line=None):
    """urllib with proxies from the environment, no redirects, and (for a turn) a tracked socket."""
    handlers = [_NoRedirect()]
    if line is not None:

        class Plain(http.client.HTTPConnection):
            def connect(self):
                super().connect()
                line.attach(self.sock)

        class Secure(http.client.HTTPSConnection):
            def connect(self):
                super().connect()
                line.attach(self.sock)

        class PlainHandler(urllib.request.HTTPHandler):
            def http_open(self, req):
                return self.do_open(Plain, req)

        class SecureHandler(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(Secure, req, context=self._context)

        handlers += [PlainHandler(), SecureHandler()]
    return urllib.request.build_opener(*handlers)


def _headers(key, accept):
    return {
        "Authorization": "Bearer " + key,
        "Accept": accept,
        "User-Agent": USER_AGENT,
    }


def _redirect_target(headers, key=None) -> str:
    """The host a redirect points at: never its user:password part, and never the key."""
    parts = urlsplit((headers or {}).get("location") or "")
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        host, port = parts.hostname, None
    if not host:
        return "别的地址"
    label = (f"[{host}]" if ":" in host else host) + (f":{port}" if port else "")
    return short(redact(label, key), 120)


class _Late(Exception):
    """The deadline of a bounded request passed; its connection has been cut."""


def _bounded(seconds, work):
    """work(line) on a daemon thread, given `seconds` of wall-clock time in all.

    A socket timeout bounds each read, not the request: a server that sends a byte now and then
    would hold the caller for as long as it likes. Past the deadline the tracked connection is
    shut down and _Late is raised; whatever the thread found later is dropped.
    """
    line, box = _Line(seconds), {}

    def run():
        try:
            box["value"] = work(line)
        except BaseException as exc:  # handed to the waiting thread below
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(max(0.0, seconds))
    if thread.is_alive():
        line.stop()
        raise _Late
    if "error" in box:
        raise box["error"]
    return box["value"]


def list_models(base_url, key, *, timeout=LIST_SECONDS) -> dict:
    """GET {base}/models to check the address and the key; used when the settings are saved.

    Returns {"listed": bool, "ids": [...], "status": code, "key_checked": bool | None}. A server
    without a model list (404/405) is accepted with listed False; a refused key, a redirect, a
    server error, no connection or no complete answer within `timeout` seconds in all raise, and
    nothing is saved. When the list came back, the same GET without the key tells whether the
    list checks keys at all (OpenRouter's and a local server's do not): key_checked is False when
    it answers too, True when it is refused, None when that could not be told.
    """
    started = time.monotonic()
    try:
        result = _bounded(timeout, lambda line: _list_models(base_url, key, timeout, line))
    except _Late:
        raise WorkflowError(
            f"{_host_label(base_url)} 在 {timeout} 秒内没有返回完整的模型列表，无法验证，未保存。"
            "请检查接口地址和网络，或稍后再保存。"
        ) from None
    if result["listed"]:
        left = timeout - (time.monotonic() - started)
        result["key_checked"] = _key_checked(base_url, left) if left >= 1 else None
    else:
        result["key_checked"] = None
    return result


def _host_label(base_url) -> str:
    return urlsplit(base_url).netloc


def _key_checked(base_url, seconds):
    """Whether GET {base}/models refuses a request without the key (see list_models)."""

    def ask(line):
        request = urllib.request.Request(
            base_url + "/models", headers={"Accept": "application/json", "User-Agent": USER_AGENT}
        )
        try:
            with opener(line).open(request, timeout=seconds) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code

    try:
        status = _bounded(seconds, ask)
    except (_Late, OSError, http.client.HTTPException, ValueError):
        return None
    return False if status == 200 else True if status in (401, 403) else None


def _list_models(base_url, key, timeout, line) -> dict:
    host = _host_label(base_url)
    request = urllib.request.Request(
        base_url + "/models", headers=_headers(key, "application/json")
    )
    try:
        with opener(line).open(request, timeout=timeout) as response:
            body = response.read(MAX_LIST_BYTES + 1)
            status = response.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_ERROR_BYTES)
        except (OSError, http.client.HTTPException, ValueError):
            raw = b""
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        exc.close()
        detail = detail_line(error_fields(raw)[0], key)
        code = exc.code
        if code in (404, 405):
            return {"listed": False, "ids": [], "status": code}
        if code in (401, 403):
            raise ConfigurationError(
                with_detail(
                    f"接口拒绝了这个密钥（HTTP {code}），未保存。请检查密钥是否正确、是否属于这个接口。",
                    detail,
                )
            ) from None
        if 300 <= code < 400:
            raise ConfigurationError(
                f"接口地址跳转到了{_redirect_target(headers, key)}（HTTP {code}），未保存。"
                "为了不把密钥发到别处，不会跟随跳转；请改填跳转后的地址。"
            ) from None
        if code == 429:
            raise WorkflowError(
                with_detail("接口暂时限流（HTTP 429），无法验证，未保存。请稍后再保存。", detail)
            ) from None
        if code >= 500:
            raise WorkflowError(
                with_detail(f"验证时接口出错（HTTP {code}），未保存。请稍后再保存。", detail)
            ) from None
        raise WorkflowError(
            with_detail(f"验证时接口返回错误（HTTP {code}），未保存。请检查接口地址。", detail)
        ) from None
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise WorkflowError(
            with_detail(
                f"连不上 {host}，无法验证，未保存。请检查接口地址和网络。",
                detail_line(_reason(exc), key),
            )
        ) from None
    if len(body) > MAX_LIST_BYTES:
        raise WorkflowError("接口返回的模型列表过大，未保存。")
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        raise ConfigurationError(
            with_detail(
                "这个地址返回的不是模型列表，可能不是 OpenAI 兼容接口的地址，未保存。"
                "请检查地址，通常以 /v1 结尾。",
                detail_line(error_fields(body[:2000])[0], key, 120),
            )
        ) from None
    entries = None
    if isinstance(data, dict):
        entries = data.get("data") if isinstance(data.get("data"), list) else data.get("models")
    elif isinstance(data, list):
        entries = data
    if not isinstance(entries, list):
        return {"listed": False, "ids": [], "status": status}
    ids = []
    for entry in entries:
        name = entry
        if isinstance(entry, dict):
            name = entry.get("id") or entry.get("name") or entry.get("model")
        # A listed id goes back to the page as a suggestion: never one that is or holds the key.
        if isinstance(name, str) and name.strip() and not holds_key(name, key):
            ids.append(name.strip())
    return {"listed": True, "ids": list(dict.fromkeys(ids)), "status": status}


def _read(open_request, request, events):
    """Background reader: every outcome of one request goes into the queue, nothing is raised."""
    try:
        response = open_request(request, timeout=CONNECT_SECONDS)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_ERROR_BYTES)
        except (OSError, http.client.HTTPException, ValueError):
            body = b""
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        exc.close()
        events.put(("http_error", exc.code, body, headers))
        return
    except Exception as exc:  # any failure must reach the waiting turn, never end the thread
        events.put(("network_error", _reason(exc)))
        return
    try:
        events.put(("open", response.headers.get("Content-Type", "")))
        while True:
            chunk = response.read1(65536)
            if not chunk:
                break
            events.put(("data", chunk))
        events.put(("end",))
    except Exception as exc:
        events.put(("network_error", _reason(exc)))
    finally:
        response.close()


# ---------------------------------------------------------------------------- reply


class _Refused:
    def __init__(self, code, body, headers):
        self.code, self.body, self.headers = code, body, headers


class _InBand(Exception):
    """An {"error": ...} in a 200 reply or inside the stream; _exchange decides what it means."""

    def __init__(self, error):
        super().__init__("error in reply")
        self.error = error


class _Reply:
    """What the server said, gathered from stream chunks or from one JSON body.

    Everything kept from it that reaches the record or the page (the reply id, the model name, the
    finish reason) is scrubbed of the key, since a server may echo anything back.
    """

    def __init__(self, on_activity, key, model, host):
        self.on_activity = on_activity
        self.key, self.requested, self.host = key, model, host
        self.content = []
        # The answer of a server that returns structured output as a forced tool call.
        self.arguments = []
        self.refusal = []
        self.reasoning = False
        self.finish = None
        self.usage = None
        self.id = None
        self.model = None
        self.reported = None
        self.done = False
        self.chunks = 0

    def take(self, chunk):
        if not isinstance(chunk, dict):
            return
        if chunk.get("error"):
            raise _InBand(chunk["error"])
        self.chunks += 1
        if isinstance(chunk.get("id"), str) and chunk["id"] and not self.id:
            self.id = short(redact(chunk["id"], self.key), 200)
        if (
            isinstance(chunk.get("model"), str)
            and chunk["model"]
            and chunk["model"] != self.reported
        ):
            self.reported = chunk["model"]
            self.model = short(redact(chunk["model"], self.key), 200)
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        choices = chunk.get("choices")
        for choice in choices if isinstance(choices, list) else []:
            if not isinstance(choice, dict) or choice.get("index") not in (0, None):
                continue
            part = choice.get("delta")
            if not isinstance(part, dict):
                part = choice.get("message")
            if isinstance(part, dict):
                thought = part.get("reasoning_content") or part.get("reasoning")
                if isinstance(thought, str) and thought:
                    self.reasoning = True
                    self.on_activity(ACTIVITY["reasoning"])
                text = part.get("content")
                if isinstance(text, list):
                    text = "".join(
                        x["text"]
                        for x in text
                        if isinstance(x, dict) and isinstance(x.get("text"), str)
                    )
                if isinstance(text, str) and text:
                    self.content.append(text)
                    self.on_activity(ACTIVITY["content"])
                self.take_call(part.get("tool_calls"))
                refusal = part.get("refusal")
                if isinstance(refusal, str) and refusal:
                    self.refusal.append(refusal)
            if choice.get("finish_reason"):
                self.finish = str(choice["finish_reason"])

    def take_call(self, calls):
        """The first tool call's arguments: streamed in pieces (with an index) or whole."""
        if not isinstance(calls, list):
            return
        for position, call in enumerate(calls):
            if not isinstance(call, dict) or call.get("index", position) != 0:
                continue
            function = call.get("function")
            arguments = function.get("arguments") if isinstance(function, dict) else None
            if isinstance(arguments, str) and arguments:
                self.arguments.append(arguments)
                self.on_activity(ACTIVITY["content"])

    def text(self) -> str:
        return "".join(self.content).strip() or "".join(self.arguments).strip()

    def finish_label(self) -> str:
        return detail_line(self.finish, self.key, 40) if self.finish else ""


_NOT_JSON = object()


def _json(text):
    """The JSON value of text, or _NOT_JSON (a nesting too deep to parse counts as not JSON)."""
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return _NOT_JSON


LINE_END = re.compile(rb"\r\n|\r|\n")


class _EventStream:
    """Server-sent events: data lines joined until a blank line, comments skipped, [DONE] ends."""

    def __init__(self, reply):
        self.reply = reply
        self.buffer = b""
        self.data = []

    def feed(self, chunk):
        self.buffer += chunk
        while not self.reply.done:
            match = LINE_END.search(self.buffer)
            if not match or (match[0] == b"\r" and match.end() == len(self.buffer)):
                break  # no full line yet, or a CR whose LF may come in the next chunk
            line, self.buffer = self.buffer[: match.start()], self.buffer[match.end() :]
            self.line(line.decode("utf-8", "replace"))

    def close(self):
        if self.buffer and not self.reply.done:
            self.line(self.buffer.rstrip(b"\r").decode("utf-8", "replace"))
        self.buffer = b""
        self.dispatch()

    def line(self, text):
        if not text:
            self.dispatch()
        elif not text.startswith(":"):
            name, _, value = text.partition(":")
            if name == "data":
                self.data.append(value[1:] if value.startswith(" ") else value)

    def dispatch(self):
        if not self.data or self.reply.done:
            self.data = []
            return
        lines, self.data = self.data, []
        joined = "\n".join(lines)
        if joined.strip() == "[DONE]":
            self.reply.done = True
            return
        value = _json(joined)
        if value is not _NOT_JSON:
            self.reply.take(value)
            return
        # Some servers put several events in one without blank lines between them.
        for line in lines:
            if line.strip() == "[DONE]":
                self.reply.done = True
                return
            value = _json(line)
            if value is not _NOT_JSON:
                self.reply.take(value)


def normalise_usage(usage) -> dict | None:
    """Usage in the Claude record's terms: input excludes cached tokens, which are counted apart."""
    if not isinstance(usage, dict):
        return None

    def number(value):
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )

    prompt, completion = number(usage.get("prompt_tokens")), number(usage.get("completion_tokens"))
    if prompt is None and completion is None:
        return None
    details = usage.get("prompt_tokens_details")
    cached = number(details.get("cached_tokens")) if isinstance(details, dict) else None
    if cached is None:
        cached = number(usage.get("prompt_cache_hit_tokens"))  # DeepSeek's name for it
    cached = min(cached or 0, prompt or 0)
    return {
        "input_tokens": (prompt or 0) - cached,
        "output_tokens": completion or 0,
        "cache_read_input_tokens": cached,
    }


# ---------------------------------------------------------------------------- errors


def _http_error(code, body, headers, *, key, model, host) -> WorkflowError:
    """The error for an HTTP refusal. A refused key, a missing address or model and a redirect
    need the settings changed (ConfigurationError); everything else is a RequestFailed, which is
    not repaired automatically, since sending the same request again at once would not help."""
    message, words = error_fields(body)
    detail = detail_line(message, key)
    if code == 401:
        return ConfigurationError(
            with_detail(
                "OpenAI 兼容接口拒绝了密钥（HTTP 401）。请在设置里检查密钥后重试；" + KEPT, detail
            )
        )
    if code == 403:
        return ConfigurationError(
            with_detail(
                "OpenAI 兼容接口拒绝了这次请求（HTTP 403）：密钥无效，或没有使用这个模型的权限。"
                "请在设置里检查密钥和模型后重试；" + KEPT,
                detail,
            )
        )
    if code == 404:
        return ConfigurationError(
            with_detail(
                f"OpenAI 兼容接口找不到这个地址或模型“{model}”（HTTP 404）。"
                "请在设置里检查接口地址（通常以 /v1 结尾）和模型名称；" + KEPT,
                detail,
            )
        )
    if 300 <= code < 400:
        return ConfigurationError(
            f"OpenAI 兼容接口的地址跳转到了{_redirect_target(headers, key)}（HTTP {code}）。"
            "为了不把密钥发到别处，没有跟随跳转；请在设置里改填跳转后的地址。" + KEPT
        )
    if code == 402:
        return RequestFailed(
            with_detail(
                "OpenAI 兼容接口的账户余额不足（HTTP 402）。请充值或换用其他接口后重试；" + KEPT,
                detail,
            )
        )
    if code == 429:
        return RequestFailed(
            with_detail(
                "OpenAI 兼容接口限流或额度已用完（HTTP 429）。请稍后重试，或检查账户额度；" + KEPT,
                detail,
            )
        )
    if code in (400, 413, 422) and TOO_LONG.search(words):
        return RequestFailed(
            with_detail(
                f"题目、课程资料和页面图片超出了这个模型能读的长度（HTTP {code}）。"
                "请在设置里换用上下文更长的模型后重试；" + KEPT,
                detail,
            )
        )
    if code == 413 or (code in (400, 422) and TOO_LARGE.search(words)):
        return RequestFailed(
            with_detail(
                f"题目、资料和页面图片超出了接口允许的请求大小（HTTP {code}）。"
                "可以换用上限更大的接口或模型；" + KEPT,
                detail,
            )
        )
    if code >= 500:
        return RequestFailed(
            with_detail(f"OpenAI 兼容接口出错（HTTP {code}）。请稍后重试；" + KEPT, detail)
        )
    if code in (400, 422):
        return RequestFailed(
            with_detail(
                f"OpenAI 兼容接口不接受这次请求（HTTP {code}）。"
                "请检查模型名称，或换用其他模型；" + KEPT,
                detail,
            )
        )
    return RequestFailed(
        with_detail(
            f"OpenAI 兼容接口返回错误（HTTP {code}），没有得到结果。请稍后重试；" + KEPT, detail
        )
    )


def _status_of(error):
    """The HTTP status an in-band error names in its code, if it names one."""
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    if isinstance(code, int) and not isinstance(code, bool) and 300 <= code < 600:
        return code
    return None


def _error_in_reply(error, key, model, host) -> WorkflowError:
    """An {"error": ...} in a 200 reply or inside the stream that no step can answer."""
    body = json.dumps({"error": error}, ensure_ascii=False)
    message, _ = error_fields(body)
    code = _status_of(error)
    if code:
        return _http_error(code, body, {}, key=key, model=model, host=host)
    return RequestFailed(
        with_detail(
            "OpenAI 兼容接口在输出途中报错，没有得到完整结果。请重试；" + KEPT,
            detail_line(message, key),
        )
    )


# ---------------------------------------------------------------------------- schema


class SchemaMismatch(ValueError):
    def __init__(self, path, problem):
        super().__init__(f"{path or '结果'}{problem}")
        self.path, self.problem = path, problem


TYPE_NAMES = {
    "object": "对象",
    "array": "数组",
    "string": "文字",
    "integer": "整数",
    "number": "数字",
    "boolean": "true/false",
    "null": "null",
}
KEYWORDS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "description",
    "title",
    "default",
    "$schema",
}


def _is(value, kind) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        # JSON Schema counts 1.0 as an integer; conform() turns it into 1 for the program.
        return (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, float) and value.is_integer()
        )
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "null":
        return value is None
    raise ValueError(f"unsupported schema type {kind!r}")


def _kind(value) -> str:
    for kind in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _is(value, kind):
            return TYPE_NAMES[kind]
    return type(value).__name__


def _same(a, b) -> bool:
    return type(a) is type(b) and a == b


def validate(value, schema, path="") -> None:
    """Check a value against the JSON Schema subset output_schema() uses; SchemaMismatch names
    the first place that does not fit, as a path such as review.requirement_checks[0].status."""
    unknown = set(schema) - KEYWORDS
    if unknown:
        raise ValueError("unsupported schema keywords: " + ", ".join(sorted(unknown)))
    kinds = schema.get("type")
    if kinds is not None:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if not any(_is(value, kind) for kind in kinds):
            wanted = "或".join(TYPE_NAMES.get(kind, kind) for kind in kinds)
            raise SchemaMismatch(path, f" 应为{wanted}，实际是{_kind(value)}")
    if "enum" in schema and not any(_same(value, option) for option in schema["enum"]):
        shown = short(json.dumps(value, ensure_ascii=False), 40)
        raise SchemaMismatch(path, f" 的值 {shown} 不在允许的选项里")
    if "const" in schema and not _same(value, schema["const"]):
        raise SchemaMismatch(path, " 的值不对")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                raise SchemaMismatch(path, f" 缺少字段 {name}")
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            where = f"{path}.{name}" if path else str(name)
            if name in properties:
                validate(item, properties[name], where)
            elif extra is False:
                raise SchemaMismatch(path, f" 有 schema 以外的字段 {short(name, 40)}")
            elif isinstance(extra, dict):
                validate(item, extra, where)
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaMismatch(path, f" 至少要有 {schema['minItems']} 项")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaMismatch(path, f" 最多 {schema['maxItems']} 项")
        if "items" in schema:
            for index, item in enumerate(value):
                validate(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaMismatch(path, " 太短")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaMismatch(path, " 太长")
    if _is(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaMismatch(path, f" 不能小于 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaMismatch(path, f" 不能大于 {schema['maximum']}")


def conform(value, schema):
    """A validated value with each integer the model wrote as 1.0 turned into 1, so the program
    that indexes with it gets an int."""
    kinds = schema.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    if isinstance(value, float) and "integer" in kinds and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        properties, extra = schema.get("properties", {}), schema.get("additionalProperties")
        return {
            name: conform(
                item,
                properties[name]
                if name in properties
                else extra
                if isinstance(extra, dict)
                else {},
            )
            for name, item in value.items()
        }
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [conform(item, schema["items"]) for item in value]
    return value


THINK = re.compile(r"^\s*<think>.*?</think>", re.S)
FENCE = re.compile(r"```[A-Za-z]*[ \t]*\r?\n?(.*?)```", re.S)


def json_candidates(text) -> list[dict]:
    """JSON objects in a model's text: the whole text, fenced blocks, then objects inside prose.

    A nesting too deep for the parser counts as no JSON, never as a crash of the turn."""
    text = THINK.sub("", text, count=1).strip()
    found = []

    def add(value):
        if isinstance(value, dict) and value not in found:
            found.append(value)

    try:
        whole = _json(text)
        if whole is not _NOT_JSON:
            add(whole)
        if found:
            return found
        for block in FENCE.findall(text):
            value = _json(block.strip())
            if value is not _NOT_JSON:
                add(value)
        decoder = json.JSONDecoder()
        index, tries = text.find("{"), 0
        while index != -1 and tries < 200 and len(found) < 5:
            tries += 1
            try:
                value, end = decoder.raw_decode(text, index)
            except (ValueError, RecursionError):
                index = text.find("{", index + 1)
                continue
            add(value)
            index = text.find("{", end)
    except RecursionError:
        pass
    return found


def schema_instruction(schema) -> str:
    return (
        "输出要求：只输出一个 JSON 对象，不要输出任何其他文字、解释或 Markdown 代码块。"
        "这个 JSON 对象必须符合下面的 JSON Schema：所有 required 字段都要出现，"
        "不要添加 schema 以外的字段，带 enum 的字段只能取列出的值。\n"
        + json.dumps(schema, ensure_ascii=False)
    )


# ---------------------------------------------------------------------------- the turn


def _image_url(block) -> dict:
    source = block.get("source") or {}
    media = source.get("media_type") or "image/png"
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{media};base64,{source.get('data', '')}"},
    }


def request_body(content, schema, model, mode) -> dict:
    """The chat completion request for the current set of features (see STEPS)."""
    parts, omitted = [], []
    for index, block in enumerate(content):
        kind = block.get("type")
        if kind == "image":
            if mode["images"]:
                parts.append(_image_url(block))
            continue
        if kind != "text":
            continue
        caption = index + 1 < len(content) and content[index + 1].get("type") == "image"
        if caption and not mode["images"]:
            omitted.append(block["text"])  # the "页面图片 N：资料 · 第几页 [E:id]" line of a page
            continue
        parts.append({"type": "text", "text": block["text"]})
    dropped = sum(block.get("type") == "image" for block in content) if not mode["images"] else 0
    if dropped:
        listed = "；".join(omitted) if omitted else f"共 {dropped} 张"
        why = (
            "这次请求超出了接口的长度或大小上限"
            if mode.get("images_why") == "size"
            else "这个接口不接受图片"
        )
        parts.append(
            {
                "type": "text",
                "text": f"{why}，下面这些页面图片没有发送：{listed}。"
                "这些页面只能依据上面提取的文字作答；看不到的图表、截图或扫描内容写进 "
                "missing_sources 和 questions，不要猜测。",
            }
        )
    if mode["format"] != "json_schema":
        parts.append({"type": "text", "text": schema_instruction(schema)})
    images = any(part["type"] == "image_url" for part in parts)
    # Text alone goes as one string: some servers take content parts only together with images.
    message = parts if images else "\n\n".join(part["text"] for part in parts)
    body = {"model": model, "messages": [{"role": "user", "content": message}]}
    if mode["stream"]:
        body["stream"] = True
        if mode["stream_options"]:
            body["stream_options"] = {"include_usage": True}
    if mode["effort"]:
        body["reasoning_effort"] = mode["effort"]
    if mode["format"] == "json_schema":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": SCHEMA_NAME, "strict": True, "schema": schema},
        }
    elif mode["format"] == "json_object":
        body["response_format"] = {"type": "json_object"}
    return body


def _active(step, mode) -> bool:
    return {
        "reasoning_effort": bool(mode["effort"]),
        "stream_options": mode["stream"] and mode["stream_options"],
        "json_schema": mode["format"] == "json_schema",
        "json_object": mode["format"] == "json_object",
        "images": mode["images"],
        "images_size": mode["images"],
        "stream": mode["stream"],
    }[step]


def _relax(step, mode) -> None:
    if step == "reasoning_effort":
        mode["effort"] = None
    elif step == "stream_options":
        mode["stream_options"] = False
    elif step == "json_schema":
        mode["format"] = "json_object"
    elif step == "json_object":
        mode["format"] = "none"
    elif step in ("images", "images_size"):
        mode["images"] = False
        mode["images_why"] = "size" if step == "images_size" else "unsupported"
    elif step == "stream":
        mode["stream"] = mode["stream_options"] = False


def degradable(refused, mode) -> str | None:
    """The feature a 400/422 names, if it is still in the request; None otherwise.

    A refusal for size (a 413, or a 400/422 that says the request is too long or too large)
    drops the page images, which are most of the request, as one step of its own: images_size.
    Both image steps take the images out, so at most one of them is ever tried."""
    if refused.code not in (400, 413, 422):
        return None
    _, text = error_fields(refused.body)
    # Size first: a length error can name a feature in passing ("... including reasoning
    # tokens"), and dropping that feature would not make the request any shorter.
    if refused.code == 413 or TOO_LONG.search(text) or TOO_LARGE.search(text):
        return "images_size" if mode["images"] else None
    for step in STEPS:
        if _active(step, mode) and STEP_WORDS[step].search(text):
            return step
    return None


def _dump(error) -> bytes:
    try:
        return json.dumps({"error": error}, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return b'{"error": "unreadable"}'


def _exchange(url, key, body, *, started, clock, cancelled, on_activity, model, host):
    """One request; returns (_Reply, streamed) or _Refused for an HTTP error to look at."""
    if cancelled():
        raise RunCancelled("已暂停，" + KEPT)
    line = _Line(IDLE_SECONDS + 15)
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            **_headers(key, "text/event-stream, application/json"),
            "Content-Type": "application/json",
        },
    )
    events = queue.Queue()
    threading.Thread(target=_read, args=(opener(line).open, request, events), daemon=True).start()
    reply = _Reply(on_activity, key, model, host)
    try:
        return _receive(
            events, reply, started=started, clock=clock, cancelled=cancelled, key=key, host=host
        )
    except _InBand as inband:
        code = _status_of(inband.error)
        if code in (400, 413, 422):
            # Refused inside a 200 reply, as some proxies do: degradable() reads it as a refusal.
            return _Refused(code, _dump(inband.error), {})
        raise _error_in_reply(inband.error, key, model, host) from None
    except (TypeError, AttributeError, KeyError, IndexError, RecursionError):
        # A reply of an unexpected shape (a broken or hostile server), never a crash of the job.
        raise RequestFailed(
            "OpenAI 兼容接口的回复格式不对，没有得到可用的结果。请重试，或换一个模型；" + KEPT
        ) from None
    finally:
        line.stop()


def _web_page(raw, key) -> ConfigurationError:
    return ConfigurationError(
        with_detail(
            "这个地址返回的是网页，不是 OpenAI 兼容接口的回复。"
            "请在设置里检查接口地址（通常以 /v1 结尾）；" + KEPT,
            detail_line(error_fields(bytes(raw[:4000]))[0], key, 120),
        )
    )


def _receive(events, reply, *, started, clock, cancelled, key, host):
    stream, streamed, html, raw, received = None, None, False, bytearray(), 0
    last = clock()
    while True:
        if cancelled():
            raise RunCancelled("已暂停，" + KEPT)
        now = clock()
        if now - started > TURN_SECONDS:
            raise RequestFailed(TURN_MESSAGE)
        if now - last > IDLE_SECONDS:
            raise RequestFailed(IDLE_MESSAGE)
        try:
            event = events.get(timeout=0.2)
        except queue.Empty:
            continue
        last = clock()
        kind = event[0]
        if kind == "http_error":
            return _Refused(event[1], event[2], event[3])
        if kind == "network_error":
            raise RequestFailed(
                with_detail(
                    f"连不上 OpenAI 兼容接口（{host}），或连接中途断开。"
                    "请检查网络和设置里的接口地址后重试；" + KEPT,
                    detail_line(event[1], key),
                )
            )
        if kind == "open":
            content_type = event[1].lower()
            html = "html" in content_type
            streamed = (
                True
                if "event-stream" in content_type
                else False
                if "json" in content_type or html
                else None
            )
            continue
        if kind == "data":
            received += len(event[1])
            if received > MAX_REPLY_BYTES:
                raise RequestFailed("接口返回的内容过大，已停止接收；" + KEPT)
            if streamed is None:
                head = (bytes(raw) + event[1]).lstrip()[:1]
                if not head:
                    raw += event[1]
                    continue
                html = head == b"<"
                streamed = head not in (b"{", b"[", b"<")
                event = ("data", bytes(raw) + event[1])
                raw = bytearray()
            if streamed:
                stream = stream or _EventStream(reply)
                stream.feed(event[1])
                if reply.done:
                    return reply, True
            else:
                raw += event[1]
            continue
        if kind == "end":
            if streamed:
                if stream:
                    stream.close()
                return reply, True
            text = bytes(raw).decode("utf-8", "replace")
            if html or text.lstrip().startswith("<"):
                raise _web_page(raw, key)
            data = _json(text)
            if data is not _NOT_JSON:
                # Gemini wraps an error in a one-element list.
                reply.take(data[0] if isinstance(data, list) and data else data)
            elif text.lstrip().startswith(("data:", ":")):
                stream = _EventStream(reply)  # a stream sent without its content type
                stream.feed(bytes(raw))
                stream.close()
                return reply, True
            else:
                reply.content.append(text)
            return reply, False


def run_turn(
    content: list[dict],
    schema: dict,
    *,
    model: str,
    effort: str | None,
    config: dict,
    cancelled=lambda: False,
    on_activity=lambda _label: None,
    clock=time.monotonic,
) -> dict:
    """Return the validated output and what the server reported, in claude_agent.run_turn's shape,
    plus the provider details for the generation record."""
    base_url, key = config["base_url"], config["api_key"]
    host = urlsplit(base_url).netloc
    images = sum(block.get("type") == "image" for block in content)
    mode = {
        "effort": effort,
        "stream": True,
        "stream_options": True,
        "format": "json_schema",
        "images": bool(images),
        "images_why": None,
    }
    relaxed = []
    started = clock()
    while True:
        outcome = _exchange(
            base_url + "/chat/completions",
            key,
            request_body(content, schema, model, mode),
            started=started,
            clock=clock,
            cancelled=cancelled,
            on_activity=on_activity,
            model=model,
            host=host,
        )
        if isinstance(outcome, _Refused):
            step = degradable(outcome, mode)
            if step:
                _relax(step, mode)
                relaxed.append(step)
                continue
            raise _http_error(
                outcome.code, outcome.body, outcome.headers, key=key, model=model, host=host
            )
        reply, streamed = outcome
        break
    text = reply.text()
    if reply.finish == "length":
        raise WorkflowError(
            "模型的输出达到长度上限，结果被截断（finish_reason: length）。"
            "请换用输出上限更大的模型，或减少这次的资料后重试；" + KEPT
        )
    if reply.finish == "content_filter":
        raise WorkflowError(
            "接口的内容审核拦截了这次输出（finish_reason: content_filter）。"
            "可以换一个模型重试；" + KEPT
        )
    if reply.refusal:
        raise WorkflowError(
            with_detail(
                "模型拒绝回答这项作业。可以补充说明后重做，或换一个模型；" + KEPT,
                detail_line("".join(reply.refusal), key),
            )
        )
    if not text:
        if streamed and not reply.done and not reply.finish:
            raise RequestFailed("连接在模型输出完成前断开，没有得到完整结果。请重试；" + KEPT)
        raise WorkflowError(
            "模型没有返回任何内容"
            + (f"（finish_reason: {reply.finish_label()}）" if reply.finish else "")
            + "。请重试，或换一个模型；"
            + KEPT
        )
    candidates = json_candidates(text)
    output, mismatch = None, None
    for candidate in candidates:
        try:
            validate(candidate, schema)
        except SchemaMismatch as exc:
            mismatch = mismatch or exc
            continue
        output = conform(candidate, schema)
        break
    if output is None:
        if streamed and not reply.done and not reply.finish:
            # Neither [DONE] nor a finish reason: the stream was cut, whatever the text looks like.
            raise RequestFailed("连接在模型输出完成前断开，没有得到完整结果。请重试；" + KEPT)
        if not candidates:
            raise WorkflowError(
                with_detail(
                    "模型返回的内容不是可以解析的 JSON。请重试，或换用支持结构化输出的模型；"
                    + KEPT,
                    detail_line(text, key, 160),
                )
            )
        # The place and the value come from the model's reply, so they are scrubbed like its words.
        raise WorkflowError(
            f"模型返回的 JSON 不符合要求的格式：{detail_line(str(mismatch), key, 160)}。"
            "请重试，或换用支持结构化输出的模型；" + KEPT
        )
    return {
        "output": output,
        "model": reply.model or model,
        "session_id": reply.id,
        "api_key_source": API_KEY_SOURCE,
        "usage": normalise_usage(reply.usage),
        "cost_usd": None,
        "duration_ms": max(0, int(round((clock() - started) * 1000))),
        "provider": {
            "host": host,
            "structured_output_mode": mode["format"],
            "images_sent": images if mode["images"] else 0,
            "images_dropped": 0 if mode["images"] else images,
            "degradations": relaxed,
            "streamed": bool(streamed),
            "finish_reason": reply.finish_label() or None,
        },
    }
