"""The OpenAI-compatible turn against a local fake server on 127.0.0.1; no real endpoint is used."""

import json
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from classroomautowork import drafting, openai_compat
from classroomautowork.errors import ConfigurationError, RequestFailed, RunCancelled, WorkflowError

KEY = "sk-unit-" + "q" * 40 + "Z9yx"
SCHEMA = drafting.output_schema(["req-1"])


class Endpoint:
    """A threaded OpenAI-compatible stand-in for one test.

    Each request takes the next scripted reply; requests are recorded with their path, headers and
    JSON body. `hangups` is set when the client cut a reply short.
    """

    def __init__(self):
        self.replies = []
        self.requests = []
        self.hangups = threading.Event()
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                endpoint.serve(self, b"")

            def do_POST(self):
                endpoint.serve(self, self.rfile.read(int(self.headers.get("Content-Length", 0))))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    @property
    def config(self):
        return {"base_url": self.base, "api_key": KEY}

    def serve(self, handler, raw):
        self.requests.append(
            {
                "method": handler.command,
                "path": handler.path,
                "headers": dict(handler.headers),
                "body": json.loads(raw) if raw else None,
            }
        )
        reply = self.replies.pop(0) if self.replies else reply_json({"error": "unscripted"}, 500)
        try:
            reply(handler)
        except (BrokenPipeError, ConnectionResetError):
            self.hangups.set()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def endpoint():
    server = Endpoint()
    yield server
    server.close()


def head(handler, status=200, content_type="text/event-stream", length=None, extra=None):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    if length is not None:
        handler.send_header("Content-Length", str(length))
    for name, value in (extra or {}).items():
        handler.send_header(name, value)
    handler.end_headers()


def reply_json(data, status=200, content_type="application/json", extra=None):
    def reply(handler):
        raw = (data if isinstance(data, str) else json.dumps(data)).encode()
        head(handler, status, content_type, len(raw), extra)
        handler.wfile.write(raw)

    return reply


def reply_stream(events, *, done=True, newline="\n", piece=None, delay=0.0, comment=False):
    """SSE events (dicts or raw strings), written in `piece`-byte writes to split lines apart."""
    text = (": keep-alive" + newline + newline) if comment else ""
    for event in events:
        text += (event if isinstance(event, str) else "data: " + json.dumps(event)) + newline * 2
    if done:
        text += "data: [DONE]" + newline * 2
    raw = text.encode()

    def reply(handler):
        head(handler)
        step = piece or len(raw)
        for start in range(0, len(raw), step):
            handler.wfile.write(raw[start : start + step])
            handler.wfile.flush()
            if delay:
                time.sleep(delay)

    return reply


def reply_hold(ping=None, limit=8.0):
    """Headers, then silence (or keep-alive comments every `ping` s) until the client hangs up."""

    def reply(handler):
        head(handler)
        handler.wfile.flush()
        sock, end = handler.connection, time.monotonic() + limit
        while time.monotonic() < end:
            if ping:
                handler.wfile.write(b": ping\n\n")
                handler.wfile.flush()
            readable, _, _ = select.select([sock], [], [], ping or 0.05)
            if readable and not sock.recv(1, socket.MSG_PEEK):
                raise ConnectionResetError

    return reply


def answer(**changes):
    value = {
        "review": {
            "requirement_checks": [
                {
                    "requirement": "Unit requirement",
                    "requirement_source_id": "req-1",
                    "status": "met",
                    "draft_location": "全文",
                    "evidence_ids": [],
                }
            ],
            "claim_checks": [],
            "questions": [],
            "ai_policy_checked": True,
            "policy_notes": "",
            "suggested_disclosure": "",
        },
        "draft": "# 回答\n\n食品安全について述べる。",
        "summary": "unit",
        "requirements_complete": True,
        "missing_sources": [],
        "used_evidence_ids": [],
        "document_answers": [],
        "form_answers": [],
    }
    value.update(changes)
    return value


def chunks(text, *, model="served-model-2026", reasoning=0, usage=None, finish="stop", pieces=4):
    base = {"id": "chatcmpl-unit", "object": "chat.completion.chunk", "model": model}
    out = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
    ]
    out += [
        {**base, "choices": [{"index": 0, "delta": {"reasoning_content": "思考"}}]}
        for _ in range(reasoning)
    ]
    size = max(1, len(text) // pieces + 1)
    out += [
        {**base, "choices": [{"index": 0, "delta": {"content": text[i : i + size]}}]}
        for i in range(0, len(text), size)
    ]
    out.append({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    if usage:
        out.append({**base, "choices": [], "usage": usage})
    return out


def turn(endpoint, content=None, **options):
    labels = []
    options.setdefault("model", "unit-model")
    options.setdefault("effort", None)
    outcome = openai_compat.run_turn(
        content or [{"type": "text", "text": "原题与资料"}],
        SCHEMA,
        config=endpoint.config,
        on_activity=labels.append,
        **options,
    )
    return outcome, labels


def image(data="iVBORw0KGgo="):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def test_streamed_answer_is_validated_and_reported(endpoint):
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 200},
    }
    endpoint.replies.append(reply_stream(chunks(json.dumps(answer()), reasoning=2, usage=usage)))
    outcome, labels = turn(endpoint, effort="high")
    assert outcome["output"] == answer()
    assert outcome["model"] == "served-model-2026" and outcome["session_id"] == "chatcmpl-unit"
    assert outcome["api_key_source"] == "openai_compatible" and outcome["cost_usd"] is None
    assert outcome["usage"] == {
        "input_tokens": 800,
        "output_tokens": 50,
        "cache_read_input_tokens": 200,
    }
    assert outcome["provider"] == {
        "host": endpoint.base.split("/")[2],
        "structured_output_mode": "json_schema",
        "images_sent": 0,
        "images_dropped": 0,
        "degradations": [],
        "streamed": True,
        "finish_reason": "stop",
    }
    assert isinstance(outcome["duration_ms"], int)
    assert labels[0] == openai_compat.ACTIVITY["reasoning"]
    assert labels[-1] == openai_compat.ACTIVITY["content"]
    request = endpoint.requests[0]
    assert request["method"] == "POST" and request["path"] == "/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer " + KEY
    body = request["body"]
    assert body["model"] == "unit-model" and body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["reasoning_effort"] == "high"
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "assignment_review", "strict": True, "schema": SCHEMA},
    }
    assert body["messages"] == [{"role": "user", "content": "原题与资料"}]
    assert KEY not in json.dumps(outcome, ensure_ascii=False)


def test_effort_is_sent_only_when_chosen(endpoint):
    endpoint.replies.append(reply_stream(chunks(json.dumps(answer()))))
    outcome, _ = turn(endpoint)
    assert "reasoning_effort" not in endpoint.requests[0]["body"]
    assert outcome["usage"] is None and outcome["model"] == "served-model-2026"


def test_a_server_that_ignores_stream_and_answers_json(endpoint):
    endpoint.replies.append(
        reply_json(
            {
                "id": "resp-1",
                "choices": [
                    {"message": {"content": json.dumps(answer())}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "prompt_cache_hit_tokens": 4,
                },
            }
        )
    )
    outcome, labels = turn(endpoint, model="named-model")
    assert outcome["output"] == answer() and outcome["provider"]["streamed"] is False
    # No model in the reply: the requested one is reported.
    assert outcome["model"] == "named-model" and outcome["session_id"] == "resp-1"
    assert outcome["usage"] == {"input_tokens": 6, "output_tokens": 5, "cache_read_input_tokens": 4}
    assert labels == [openai_compat.ACTIVITY["content"]]


def test_event_stream_parsing_takes_crlf_comments_split_lines_and_no_done(endpoint):
    text = json.dumps(answer())
    events = chunks(text, pieces=9)
    # one event whose JSON spans two data lines (joined by a newline, so split between tokens)
    split = json.dumps(events[2])
    middle = split.index(", ") + 1
    events[2] = "data: " + split[:middle] + "\r\ndata: " + split[middle:]
    events.insert(1, "event: message\r\nid: 7\r\nretry: 1000")
    endpoint.replies.append(reply_stream(events, done=False, newline="\r\n", piece=7, comment=True))
    outcome, _ = turn(endpoint)
    assert outcome["output"] == answer()


def test_events_without_blank_lines_between_them_still_parse(endpoint):
    events = chunks(json.dumps(answer()), pieces=3)
    raw = "\n".join("data: " + json.dumps(event) for event in events) + "\n\ndata: [DONE]\n\n"

    def reply(handler):
        head(handler)
        handler.wfile.write(raw.encode())

    endpoint.replies.append(reply)
    assert turn(endpoint)[0]["output"] == answer()


def test_a_stream_cut_before_its_end_is_not_an_answer(endpoint):
    events = chunks(json.dumps(answer()), pieces=4)[:-1]  # no finish reason
    events[-1]["choices"][0]["delta"]["content"] = events[-1]["choices"][0]["delta"]["content"][:3]
    endpoint.replies.append(reply_stream(events, done=False))
    with pytest.raises(RequestFailed, match="断开"):
        turn(endpoint)


@pytest.mark.parametrize(
    "status, error, kind, words",
    [
        (
            401,
            {"error": {"message": f"Incorrect API key provided: {KEY}"}},
            ConfigurationError,
            "拒绝了密钥",
        ),
        (403, {"error": {"message": "Project does not have access"}}, ConfigurationError, "权限"),
        (
            404,
            {"error": {"message": "The model `unit-model` does not exist"}},
            ConfigurationError,
            "找不到",
        ),
        (402, {"error": {"message": "Insufficient Balance"}}, WorkflowError, "余额不足"),
        (
            429,
            {"error": {"message": "Rate limit reached", "type": "rate_limit"}},
            WorkflowError,
            "限流",
        ),
        (413, "request entity too large", WorkflowError, "请求大小"),
        (500, "<html><title>500 Internal Server Error</title></html>", WorkflowError, "接口出错"),
        (503, {"error": {"message": "overloaded"}}, WorkflowError, "接口出错"),
        (
            400,
            {"error": {"message": "Invalid value for temperature"}},
            WorkflowError,
            "不接受这次请求",
        ),
        (418, {"message": "teapot"}, WorkflowError, "返回错误"),
    ],
)
def test_http_errors_name_the_problem_and_keep_the_server_words(
    endpoint, status, error, kind, words
):
    endpoint.replies.append(reply_json(error, status))
    with pytest.raises(kind, match=words) as caught:
        turn(endpoint)
    message = str(caught.value)
    assert "资料已保留" in message and KEY not in message and KEY[:-4] not in message
    if status != 413:
        main, detail = message.split("\n")
        assert detail and detail not in main
    assert len(endpoint.requests) == 1


def test_a_refused_key_never_comes_back_even_masked(endpoint):
    masked = "sk-unit-****************************Z9yx"
    endpoint.replies.append(
        reply_json(
            {"error": {"message": f"Incorrect API key provided: {masked}. Bearer {KEY}"}}, 401
        )
    )
    with pytest.raises(ConfigurationError) as caught:
        turn(endpoint)
    message = str(caught.value)
    assert "sk-unit" not in message and "qqqq" not in message and KEY not in message
    assert "[已隐藏]" in message


def test_redirects_are_not_followed(endpoint):
    endpoint.replies.append(
        reply_json(
            {"error": "moved"},
            307,
            extra={"Location": "https://elsewhere.example/v1/chat/completions"},
        )
    )
    with pytest.raises(ConfigurationError, match="elsewhere.example"):
        turn(endpoint)
    assert len(endpoint.requests) == 1


def test_an_error_inside_the_stream(endpoint):
    events = chunks("{", pieces=1)[:2] + [
        {"error": {"message": "Rate limit exceeded", "code": 429}}
    ]
    endpoint.replies.append(reply_stream(events))
    with pytest.raises(WorkflowError, match="限流") as caught:
        turn(endpoint)
    assert "Rate limit exceeded" in str(caught.value)
    endpoint.replies.append(reply_stream([{"error": {"message": "upstream failed"}}]))
    with pytest.raises(WorkflowError, match="输出途中报错"):
        turn(endpoint)


@pytest.mark.parametrize("finish, words", [("length", "长度上限"), ("content_filter", "内容审核")])
def test_finish_reasons_that_mean_no_answer(endpoint, finish, words):
    endpoint.replies.append(reply_stream(chunks(json.dumps(answer()), finish=finish)))
    with pytest.raises(WorkflowError, match=words):
        turn(endpoint)


def test_a_refusal_is_reported_with_its_words(endpoint):
    events = [
        {"id": "r", "choices": [{"index": 0, "delta": {"refusal": "I can't help with that."}}]},
        {"id": "r", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    endpoint.replies.append(reply_stream(events))
    with pytest.raises(WorkflowError, match="拒绝回答") as caught:
        turn(endpoint)
    assert str(caught.value).endswith("I can't help with that.")


def test_text_that_is_not_json(endpoint):
    endpoint.replies.append(reply_stream(chunks("ごめんなさい、回答できません。")))
    with pytest.raises(WorkflowError, match="不是可以解析的 JSON"):
        turn(endpoint)


def test_json_that_does_not_fit_the_schema_names_the_place(endpoint):
    wrong = answer()
    wrong["review"]["requirement_checks"][0]["status"] = "done"
    endpoint.replies.append(reply_stream(chunks(json.dumps(wrong))))
    with pytest.raises(WorkflowError, match=r"review\.requirement_checks\[0\]\.status"):
        turn(endpoint)


def test_unsupported_reasoning_effort_is_dropped_once(endpoint):
    refused = reply_json(
        {"error": {"message": "Unrecognized request argument supplied: reasoning_effort"}}, 400
    )
    endpoint.replies += [refused, reply_stream(chunks(json.dumps(answer())))]
    outcome, _ = turn(endpoint, effort="medium")
    first, second = (x["body"] for x in endpoint.requests)
    assert first["reasoning_effort"] == "medium" and "reasoning_effort" not in second
    assert {k: v for k, v in first.items() if k != "reasoning_effort"} == second
    assert outcome["provider"]["degradations"] == ["reasoning_effort"]


def test_the_same_refusal_after_its_step_is_an_error_not_a_loop(endpoint):
    refused = {"error": {"message": "Unrecognized request argument supplied: reasoning_effort"}}
    endpoint.replies += [reply_json(refused, 400), reply_json(refused, 400)]
    with pytest.raises(WorkflowError, match="不接受这次请求"):
        turn(endpoint, effort="low")
    assert len(endpoint.requests) == 2


def test_unsupported_stream_options_are_dropped(endpoint):
    pydantic = {
        "detail": [{"loc": ["body", "stream_options"], "msg": "Extra inputs are not permitted"}]
    }
    endpoint.replies += [reply_json(pydantic, 422), reply_stream(chunks(json.dumps(answer())))]
    outcome, _ = turn(endpoint)
    assert "stream_options" not in endpoint.requests[1]["body"]
    assert endpoint.requests[1]["body"]["stream"] is True
    assert outcome["provider"]["degradations"] == ["stream_options"]


def test_json_schema_falls_back_to_json_object_then_to_the_prompt(endpoint):
    fenced = "好的，结果如下：\n```json\n" + json.dumps(answer(), ensure_ascii=False) + "\n```\n"
    endpoint.replies += [
        reply_json({"error": {"message": "This response_format type is unavailable now"}}, 400),
        reply_json(
            {"error": {"message": "json_object is not supported", "param": "response_format"}}, 400
        ),
        reply_stream(chunks(fenced)),
    ]
    outcome, _ = turn(endpoint)
    first, second, third = (x["body"] for x in endpoint.requests)
    assert first["response_format"]["type"] == "json_schema"
    assert second["response_format"] == {"type": "json_object"}
    assert "response_format" not in third
    for body in (second, third):
        prompt = body["messages"][0]["content"]
        assert prompt.startswith("原题与资料") and "JSON Schema" in prompt
        assert json.dumps(SCHEMA, ensure_ascii=False) in prompt
    assert outcome["output"] == answer()
    assert outcome["provider"]["structured_output_mode"] == "none"
    assert outcome["provider"]["degradations"] == ["json_schema", "json_object"]


def test_images_go_as_data_urls_and_are_dropped_with_a_note_when_refused(endpoint):
    content = [
        {"type": "text", "text": "原题与资料"},
        {"type": "text", "text": "页面图片 1：講義資料 · 第3页 [E:aaa]"},
        image("AAAA"),
        {"type": "text", "text": "页面图片 2：講義資料 · 第4页 [E:bbb]"},
        image("BBBB"),
    ]
    endpoint.replies += [
        reply_json(
            {
                "error": {
                    "message": "Invalid content type. image_url is only supported by certain models."
                }
            },
            400,
        ),
        reply_stream(chunks(json.dumps(answer()))),
    ]
    outcome, _ = turn(endpoint, content)
    first, second = (x["body"]["messages"][0]["content"] for x in endpoint.requests)
    assert first == [
        {"type": "text", "text": "原题与资料"},
        {"type": "text", "text": "页面图片 1：講義資料 · 第3页 [E:aaa]"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "text", "text": "页面图片 2：講義資料 · 第4页 [E:bbb]"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}},
    ]
    assert isinstance(second, str) and second.startswith("原题与资料")
    assert "页面图片 1：講義資料 · 第3页 [E:aaa]；页面图片 2：講義資料 · 第4页 [E:bbb]" in second
    assert "AAAA" not in second and "不接受图片" in second
    assert outcome["provider"]["images_sent"] == 0 and outcome["provider"]["images_dropped"] == 2


def test_a_server_that_refuses_stream_gets_one_plain_request(endpoint):
    endpoint.replies += [
        reply_json({"error": {"message": "'stream' is not supported for this model"}}, 400),
        reply_json(
            {"choices": [{"message": {"content": json.dumps(answer())}, "finish_reason": "stop"}]}
        ),
    ]
    outcome, _ = turn(endpoint)
    second = endpoint.requests[1]["body"]
    assert "stream" not in second and "stream_options" not in second
    assert outcome["provider"]["degradations"] == ["stream"]


def test_every_step_is_tried_at_most_once(endpoint):
    everything = {
        "error": {"message": "reasoning_effort stream_options response_format image stream"}
    }
    endpoint.replies += [reply_json(everything, 400) for _ in range(10)]
    with pytest.raises(WorkflowError, match="不接受这次请求"):
        turn(
            endpoint,
            [{"type": "text", "text": "x"}, {"type": "text", "text": "页面图片 1"}, image()],
            effort="high",
        )
    assert len(endpoint.requests) == 1 + len(openai_compat.STEPS)
    last = endpoint.requests[-1]["body"]
    assert set(last) == {"model", "messages"}


class Ticks:
    """A clock that moves `step` seconds every time it is read."""

    def __init__(self, step):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def test_five_silent_minutes_end_the_wait_and_the_connection(endpoint):
    endpoint.replies.append(reply_hold())
    started = time.monotonic()
    with pytest.raises(RequestFailed, match="连续 5 分钟没有任何输出") as caught:
        turn(endpoint, clock=Ticks(60))
    assert "15 分钟" not in str(caught.value)  # it says which limit it was
    assert time.monotonic() - started < 10
    assert endpoint.hangups.wait(5)


def test_fifteen_minutes_end_a_turn_that_keeps_talking(endpoint):
    endpoint.replies.append(reply_hold(ping=0.02))
    with pytest.raises(RequestFailed, match="超过 15 分钟") as caught:
        turn(endpoint, clock=Ticks(30))
    assert "5 分钟没有" not in str(caught.value)
    assert endpoint.hangups.wait(5)


def test_pause_mid_stream_cuts_the_connection(endpoint):
    events = chunks("x" * 400, pieces=200)
    endpoint.replies.append(reply_stream(events, piece=40, delay=0.03))
    seen = threading.Event()
    with pytest.raises(RunCancelled):
        openai_compat.run_turn(
            [{"type": "text", "text": "原题"}],
            SCHEMA,
            model="unit-model",
            effort=None,
            config=endpoint.config,
            cancelled=seen.is_set,
            on_activity=lambda _label: seen.set(),
        )
    assert endpoint.hangups.wait(5)


def test_no_connection_is_named_without_the_key():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = {"base_url": f"http://127.0.0.1:{port}/v1", "api_key": KEY}
    with pytest.raises(RequestFailed, match="连不上") as caught:
        openai_compat.run_turn(
            [{"type": "text", "text": "x"}], SCHEMA, model="m", effort=None, config=config
        )
    assert f"127.0.0.1:{port}" in str(caught.value) and KEY not in str(caught.value)


def test_json_candidates_tolerate_fences_prose_and_thinking():
    value = {"a": 1}
    assert openai_compat.json_candidates(json.dumps(value)) == [value]
    assert openai_compat.json_candidates("```json\n" + json.dumps(value) + "\n```") == [value]
    assert openai_compat.json_candidates("```\n" + json.dumps(value) + "```") == [value]
    assert openai_compat.json_candidates("结果：" + json.dumps(value) + " 以上。") == [value]
    assert openai_compat.json_candidates('<think>{"no": 0}</think>\n' + json.dumps(value)) == [
        value
    ]
    assert openai_compat.json_candidates("no json here") == []


def test_the_validator_covers_the_review_schema():
    openai_compat.validate(answer(), SCHEMA)
    openai_compat.validate(answer(), drafting.output_schema())
    cases = [
        ({**answer(), "extra": 1}, "schema 以外的字段 extra"),
        ({k: v for k, v in answer().items() if k != "draft"}, "缺少字段 draft"),
        (answer(draft=5), "draft 应为文字"),
        (answer(requirements_complete="yes"), "requirements_complete 应为true/false"),
        (answer(missing_sources=["ok", 3]), r"missing_sources\[1\] 应为文字"),
    ]
    claims = answer()
    claims["review"]["claim_checks"] = [
        {
            "claim": "c",
            "kind": "sourced",
            "evidence_ids": [],
            "personal_fact_indices": [True],
            "quotes": [],
        }
    ]
    cases.append((claims, r"personal_fact_indices\[0\] 应为整数"))
    for value, words in cases:
        with pytest.raises(openai_compat.SchemaMismatch, match=words):
            openai_compat.validate(value, SCHEMA)
    with pytest.raises(ValueError, match="unsupported"):
        openai_compat.validate("x", {"type": "string", "pattern": "x"})


def test_redaction_removes_keys_and_their_pieces():
    random = "sk-Zp4Lm9Xw2Rt8Vn5Kc3Hb6Jd1WXYZ"  # KEY repeats one letter, so its pieces are a word's
    text = openai_compat.redact(
        f"key {random}; Authorization: Bearer abc.def; api_key=secret123; sk-other-123456789 "
        f"AIza{'B' * 30} partial {random[4:20]} masked sk-****1234",
        random,
    )
    for secret in (random, "abc.def", "secret123", "sk-other", "AIza", random[4:20], "sk-****"):
        assert secret not in text
    assert (
        openai_compat.short("a  b\n c", 10) == "a b c"
        and len(openai_compat.short("x" * 400)) == 300
    )


def test_list_models_reads_the_list_and_refuses_what_is_not_one(endpoint):
    refused = reply_json({"error": {"message": "missing key"}}, 401)  # the same GET without the key
    endpoint.replies += [
        reply_json({"object": "list", "data": [{"id": "m-1"}, {"id": "m-2"}, {"id": "m-1"}]}),
        refused,
        reply_json({"error": "no"}, 404),
        reply_json({"error": "no"}, 405),
        reply_json({"models": [{"name": "local:7b"}]}),
        reply_json({"models": [{"name": "local:7b"}]}),  # a local server answers without a key too
        reply_json({"unexpected": True}),
    ]
    assert openai_compat.list_models(endpoint.base, KEY) == {
        "listed": True,
        "ids": ["m-1", "m-2"],
        "status": 200,
        "key_checked": True,
    }
    assert endpoint.requests[0]["path"] == "/v1/models"
    assert endpoint.requests[0]["headers"]["Authorization"] == "Bearer " + KEY
    assert endpoint.requests[1]["path"] == "/v1/models"
    assert "Authorization" not in endpoint.requests[1]["headers"]
    assert openai_compat.list_models(endpoint.base, KEY)["listed"] is False
    assert openai_compat.list_models(endpoint.base, KEY)["status"] == 405
    local = openai_compat.list_models(endpoint.base, KEY)
    assert local["ids"] == ["local:7b"] and local["key_checked"] is False
    assert openai_compat.list_models(endpoint.base, KEY) == {
        "listed": False,
        "ids": [],
        "status": 200,
        "key_checked": None,
    }
    for reply, kind, words in [
        (reply_json({"error": {"message": "bad key"}}, 401), ConfigurationError, "拒绝了这个密钥"),
        (reply_json({"error": "x"}, 403), ConfigurationError, "拒绝了这个密钥"),
        (
            reply_json("<html><body>Welcome</body></html>", 200, "text/html"),
            ConfigurationError,
            "不是模型列表",
        ),
        (
            reply_json("", 302, extra={"Location": "https://other.example/v1/models"}),
            ConfigurationError,
            "other.example",
        ),
        (reply_json({"error": "busy"}, 429), WorkflowError, "限流"),
        (reply_json({"error": "down"}, 502), WorkflowError, "接口出错"),
        (reply_json({"error": "?"}, 400), WorkflowError, "返回错误"),
    ]:
        endpoint.replies.append(reply)
        with pytest.raises(kind, match=words) as caught:
            openai_compat.list_models(endpoint.base, KEY)
        assert "未保存" in str(caught.value) and KEY not in str(caught.value)
    assert len(endpoint.requests) == 14  # the redirect was not followed


def test_sending_a_key_in_clear_is_flagged_only_off_this_network():
    assert openai_compat.sends_key_in_clear("http://api.example.com/v1")
    for local in (
        "http://127.0.0.1:11434/v1",
        "http://[::1]:8000",
        "http://192.168.0.5/v1",
        "http://localhost:1234/v1",
        "https://api.example.com/v1",
    ):
        assert not openai_compat.sends_key_in_clear(local)


def test_an_error_in_a_json_reply_even_wrapped_in_a_list(endpoint):
    endpoint.replies.append(reply_json([{"error": {"code": 429, "message": "Resource exhausted"}}]))
    with pytest.raises(WorkflowError, match="限流") as caught:
        turn(endpoint)
    assert "Resource exhausted" in str(caught.value)


def test_a_turn_paused_before_it_starts_sends_nothing(endpoint):
    with pytest.raises(RunCancelled):
        openai_compat.run_turn(
            [{"type": "text", "text": "x"}],
            SCHEMA,
            model="m",
            effort=None,
            config=endpoint.config,
            cancelled=lambda: True,
        )
    time.sleep(0.2)
    assert not endpoint.requests


# ---------------------------------------------------------------- after review (2026-11-05)

CONTEXT = {
    "error": {
        "message": "This model's maximum context length is 131072 tokens. However, you "
        "requested 152000 tokens. Please reduce the length of the messages or completion.",
        "code": "context_length_exceeded",
    }
}
PAYLOAD = [
    {
        "error": {
            "code": 400,
            "message": "Request payload size exceeds the limit: 20971520 bytes.",
            "status": "INVALID_ARGUMENT",
        }
    }
]
MODE = {
    "effort": None,
    "stream": True,
    "stream_options": True,
    "format": "json_schema",
    "images": True,
    "images_why": None,
}


def refusal(code, message):
    return openai_compat._Refused(code, json.dumps({"error": {"message": message}}).encode(), {})


@pytest.mark.parametrize(
    "message", ['"llama3.2" does not support thinking', "Thinking is not supported by this model."]
)
def test_a_thinking_refusal_drops_the_effort(endpoint, message):
    # Ollama's words for an effort sent to a model that cannot think, then Gemini's.
    endpoint.replies += [
        reply_json({"error": {"message": message, "type": "invalid_request_error"}}, 400),
        reply_stream(chunks(json.dumps(answer()))),
    ]
    outcome, _ = turn(endpoint, model="llama3.2", effort="low")
    first, second = (x["body"] for x in endpoint.requests)
    assert first["reasoning_effort"] == "low" and "reasoning_effort" not in second
    assert outcome["provider"]["degradations"] == ["reasoning_effort"]


def test_a_thinking_word_is_no_step_once_no_effort_is_sent(endpoint):
    endpoint.replies.append(reply_json({"error": {"message": "thinking failed upstream"}}, 400))
    with pytest.raises(RequestFailed, match="不接受这次请求"):
        turn(endpoint)
    assert len(endpoint.requests) == 1


@pytest.mark.parametrize(
    "message",
    [
        "Unsupported parameter: 'reasoning_effort' is not supported with this model.",
        "Model grok-3 does not support parameter reasoningEffort.",
        "thinking_config is not supported for models/gemini-2.0-flash",
        "This model doesn\u2019t support reasoning.",
        "Reasoning is not enabled for this deployment.",
    ],
)
def test_words_that_name_the_effort_drop_it(message):
    assert openai_compat.degradable(refusal(400, message), {**MODE, "effort": "low"}) == (
        "reasoning_effort"
    )


@pytest.mark.parametrize(
    "message",
    [
        "This model's maximum context length is 8192 tokens, including reasoning tokens.",
        "Your input exceeds the context window of this model (reasoning tokens included).",
    ],
)
def test_a_length_error_that_mentions_reasoning_keeps_the_effort(endpoint, message):
    refused = refusal(400, message)
    assert openai_compat.degradable(refused, {**MODE, "effort": "high"}) == "images_size"
    assert openai_compat.degradable(refused, {**MODE, "effort": "high", "images": False}) is None
    # Through a turn: the images go once, then the length error; the effort is never dropped.
    content = [{"type": "text", "text": "x"}, {"type": "text", "text": "页面图片 1"}, image()]
    endpoint.replies += [reply_json({"error": {"message": message}}, 400) for _ in range(3)]
    with pytest.raises(RequestFailed, match="能读的长度"):
        turn(endpoint, content, effort="high")
    assert len(endpoint.requests) == 2
    assert all(x["body"]["reasoning_effort"] == "high" for x in endpoint.requests)


def test_a_format_refusal_in_thinking_mode_relaxes_the_format(endpoint):
    message = "response_format json_schema is not supported in thinking mode"
    assert openai_compat.degradable(refusal(400, message), {**MODE, "effort": "low"}) == (
        "json_schema"
    )


@pytest.mark.parametrize(
    "status, body, words",
    [
        (400, CONTEXT, "超出了这个模型能读的长度"),
        (400, PAYLOAD, "超出了接口允许的请求大小"),
        (413, "<html><title>413 Request Entity Too Large</title></html>", "请求大小"),
        (
            400,
            {"error": {"message": "prompt is too long: 210000 tokens > 200000 maximum"}},
            "能读的长度",
        ),
    ],
)
def test_a_request_too_long_or_too_large_says_so(endpoint, status, body, words):
    endpoint.replies.append(reply_json(body, status))
    with pytest.raises(RequestFailed, match=words) as caught:
        turn(endpoint)
    assert "检查模型名称" not in str(caught.value) and "资料已保留" in str(caught.value)
    assert len(endpoint.requests) == 1  # no images to drop, so nothing to relax


def test_a_size_refusal_drops_the_page_images_once(endpoint):
    content = [
        {"type": "text", "text": "原题与资料"},
        {"type": "text", "text": "页面图片 1：講義資料 · 第3页 [E:aaa]"},
        image("AAAA"),
    ]
    endpoint.replies += [reply_json(PAYLOAD, 400), reply_stream(chunks(json.dumps(answer())))]
    outcome, _ = turn(endpoint, content)
    second = endpoint.requests[1]["body"]["messages"][0]["content"]
    assert isinstance(second, str) and "AAAA" not in second
    assert "超出了接口的长度或大小上限" in second and "第3页 [E:aaa]" in second
    assert "不接受图片" not in second
    provider = outcome["provider"]
    assert provider["degradations"] == ["images_size"] and provider["images_dropped"] == 1
    # Still too long without them: the length error, after exactly one more request.
    endpoint.requests.clear()
    endpoint.replies += [reply_json(CONTEXT, 400), reply_json(CONTEXT, 400)]
    with pytest.raises(RequestFailed, match="能读的长度"):
        turn(endpoint, content)
    assert len(endpoint.requests) == 2


def test_which_image_refusal_is_about_size():
    too_many = refusal(400, "Too many images in request. Max is 20.")
    assert openai_compat.degradable(too_many, dict(MODE)) == "images_size"
    vision = refusal(400, "Invalid content type. image_url is only supported by certain models.")
    assert openai_compat.degradable(vision, dict(MODE)) == "images"
    assert openai_compat.degradable(refusal(413, ""), dict(MODE)) == "images_size"
    assert openai_compat.degradable(refusal(413, ""), {**MODE, "images": False}) is None


def test_gemini_naming_its_schema_field_gets_json_object(endpoint):
    gemini = [
        {
            "error": {
                "code": 400,
                "message": 'Invalid JSON payload received. Unknown name "additionalProperties" at '
                "'generation_config.response_schema.properties[0].value': Cannot find field.",
                "status": "INVALID_ARGUMENT",
            }
        }
    ]
    endpoint.replies += [reply_json(gemini, 400), reply_stream(chunks(json.dumps(answer())))]
    outcome, _ = turn(endpoint)
    assert endpoint.requests[1]["body"]["response_format"] == {"type": "json_object"}
    assert outcome["provider"]["degradations"] == ["json_schema"]


def test_a_refusal_inside_a_200_reply_is_relaxed_like_an_http_one(endpoint):
    endpoint.replies += [
        reply_json(
            {"error": {"message": "response_format json_schema not supported", "code": 400}}
        ),
        reply_stream(chunks(json.dumps(answer()))),
    ]
    outcome, _ = turn(endpoint)
    assert endpoint.requests[1]["body"]["response_format"] == {"type": "json_object"}
    assert outcome["provider"]["degradations"] == ["json_schema"]
    endpoint.requests.clear()
    refused = {"error": {"message": "Unknown parameter: reasoning_effort", "code": "400"}}
    endpoint.replies += [reply_stream([refused]), reply_stream(chunks(json.dumps(answer())))]
    outcome, _ = turn(endpoint, effort="high")
    assert "reasoning_effort" not in endpoint.requests[1]["body"]
    assert outcome["provider"]["degradations"] == ["reasoning_effort"]


def test_an_answer_given_as_a_tool_call_is_taken(endpoint):
    text = json.dumps(answer())
    whole = {
        "id": "t",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "assignment_review", "arguments": text},
                        },
                        {"id": "c2", "type": "function", "function": {"arguments": "{}"}},
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    endpoint.replies.append(reply_json(whole))
    assert turn(endpoint)[0]["output"] == answer()
    base = {"id": "s", "model": "m"}
    start = {"index": 0, "id": "c1", "type": "function", "function": {"name": "f", "arguments": ""}}
    events = [{**base, "choices": [{"index": 0, "delta": {"tool_calls": [start]}}]}]
    events += [
        {
            **base,
            "choices": [
                {"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": x}}]}}
            ],
        }
        for x in (text[i : i + 50] for i in range(0, len(text), 50))
    ]
    events.append({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    endpoint.replies.append(reply_stream(events))
    outcome, labels = turn(endpoint)
    assert outcome["output"] == answer() and labels[-1] == openai_compat.ACTIVITY["content"]


def test_a_web_page_is_named_as_one(endpoint):
    page = "<!doctype html><html><head><title>Welcome to nginx!</title></head><body></body></html>"
    endpoint.replies.append(reply_json(page, 200, "text/html"))
    with pytest.raises(ConfigurationError, match="返回的是网页") as caught:
        turn(endpoint)
    assert str(caught.value).endswith("Welcome to nginx!")
    endpoint.replies.append(reply_json(page, 200, "application/octet-stream"))
    with pytest.raises(ConfigurationError, match="返回的是网页"):
        turn(endpoint)


@pytest.mark.parametrize(
    "reply",
    [
        lambda: reply_stream(
            [
                {"id": "x", "choices": 5},
                {"id": "x", "choices": [{"index": 0, "delta": {"content": [{"text": 5}]}}]},
                {"id": "x", "choices": [{"index": 0, "delta": {"tool_calls": [7, {"index": []}]}}]},
            ]
        ),
        lambda: reply_json("[" * 200_000 + "]" * 200_000),
        lambda: reply_stream(["data: " + "[" * 200_000 + "]" * 200_000]),
    ],
)
def test_a_reply_of_an_unexpected_shape_is_an_error_of_the_turn(endpoint, reply):
    endpoint.replies.append(reply())
    with pytest.raises(WorkflowError) as caught:
        turn(endpoint)
    assert "资料已保留" in str(caught.value)


def test_an_integer_written_as_a_float_is_taken_as_the_integer(endpoint):
    fact = {
        "claim": "c",
        "kind": "user_fact",
        "evidence_ids": [],
        "personal_fact_indices": [1.0],
        "quotes": [],
    }
    value = answer()
    value["review"]["claim_checks"] = [fact]
    endpoint.replies.append(reply_stream(chunks(json.dumps(value))))
    index = turn(endpoint)[0]["output"]["review"]["claim_checks"][0]["personal_fact_indices"][0]
    assert index == 1 and type(index) is int
    value["review"]["claim_checks"] = [{**fact, "personal_fact_indices": [1.5]}]
    with pytest.raises(openai_compat.SchemaMismatch, match="应为整数"):
        openai_compat.validate(value, SCHEMA)


@pytest.mark.parametrize(
    "status, kind",
    [
        (401, ConfigurationError),
        (403, ConfigurationError),
        (404, ConfigurationError),
        (307, ConfigurationError),
        (402, RequestFailed),
        (413, RequestFailed),
        (429, RequestFailed),
        (500, RequestFailed),
        (503, RequestFailed),
        (400, RequestFailed),
        (418, RequestFailed),
    ],
)
def test_errors_that_need_the_settings_or_are_not_repaired(status, kind):
    # ConfigurationError: the settings must change. RequestFailed: the same request at once would
    # fail again, so drafting.generate_answer spends no repair on it.
    assert type(openai_compat._http_error(status, b"{}", {}, key=KEY, model="m", host="h")) is kind


def test_the_save_check_has_one_deadline_for_the_whole_request():
    """A server that sends a byte now and then cannot hold the save past its 20 s (here 1 s)."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port, cut = listener.getsockname()[1], threading.Event()

    def serve():
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 400\r\n\r\n"
        )
        try:
            for _ in range(400):
                conn.sendall(b" ")
                time.sleep(0.1)
        except OSError:
            cut.set()
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    started = time.monotonic()
    try:
        with pytest.raises(WorkflowError, match="1 秒内没有返回完整的模型列表") as caught:
            openai_compat.list_models(f"http://127.0.0.1:{port}/v1", KEY, timeout=1)
        assert time.monotonic() - started < 3 and "未保存" in str(caught.value)
        assert cut.wait(5)  # the connection was cut, not left dripping
    finally:
        listener.close()


def test_a_short_placeholder_key_is_hidden_only_where_it_stands_alone():
    detail = openai_compat.detail_line
    assert detail("max_tokens exceeded for x", "x") == "max_tokens exceeded for [已隐藏]"
    assert detail('model "qwen2.5:14b" not found', "1") == 'model "qwen2.5:14b" not found'
    assert (
        detail("key ollama refused; ollama-cloud", "ollama") == "key [已隐藏] refused; ollama-cloud"
    )


def test_an_echoed_prefix_of_six_characters_is_hidden():
    other = "sk-Zp4Lm9Xw2Rt8Vn5Kc3Hb6Jd1WXYZ"
    for echo in ("sk-Zp4Lm9X", "sk-Zp4L", "sk-Zp4"):
        text = openai_compat.redact(f"the key starting with {echo} is revoked", other)
        assert echo not in text and text.startswith("the key starting with [已隐藏]")
    assert (
        openai_compat.redact("ordinary words stay readable", other)
        == "ordinary words stay readable"
    )


def test_what_the_server_says_on_success_is_scrubbed_too(endpoint):
    events = chunks(json.dumps(answer()), model="served-" + KEY)
    for event in events:
        event["id"] = "chatcmpl-" + KEY
    endpoint.replies.append(reply_stream(events))
    outcome, _ = turn(endpoint)
    shown = json.dumps(outcome)
    assert KEY not in shown and KEY[8:24] not in shown
    assert outcome["model"].startswith("served-") and outcome["session_id"].startswith("chatcmpl-")
    endpoint.replies.append(reply_stream(chunks("", finish=KEY)))
    with pytest.raises(WorkflowError, match="finish_reason") as caught:
        turn(endpoint)
    assert KEY[8:24] not in str(caught.value) and "[已隐藏]" in str(caught.value)
    wrong = answer()
    wrong["review"]["requirement_checks"][0]["status"] = KEY
    for value, words in [(wrong, "不在允许的选项里"), ({**answer(), KEY: 1}, "schema 以外的字段")]:
        endpoint.replies.append(reply_stream(chunks(json.dumps(value))))
        with pytest.raises(WorkflowError, match=words) as caught:
            turn(endpoint)
        assert KEY[8:24] not in str(caught.value)
    target = openai_compat._redirect_target(
        {"location": f"https://user:{KEY}@evil.example:8443/v1"}, KEY
    )
    assert target == "evil.example:8443"
    endpoint.replies += [
        reply_json({"data": [{"id": KEY}, {"id": "ok-model"}, {"id": "x-" + KEY}]}),
        reply_json({"error": "no"}, 401),
    ]
    assert openai_compat.list_models(endpoint.base, KEY)["ids"] == ["ok-model"]


def test_redaction_stays_linear_on_long_text_without_spaces():
    # A reply that is not JSON can be megabytes in one word; the masked-token pattern once took
    # quadratic time on it and held the turn after its own time limits had passed.
    started = time.monotonic()
    openai_compat.redact("[" * 400_000, KEY)
    openai_compat.detail_line("x" * 32_000_000, KEY, 160)
    assert time.monotonic() - started < 5


def test_a_key_made_of_words_does_not_hide_the_same_words_elsewhere():
    # llama.cpp suggests "sk-no-key-required"; the staging demo key contains "staging". Only the key
    # itself goes, not a model name or a server word that shares a word with it.
    assert openai_compat.detail_line("messages: field required", "sk-no-key-required") == (
        "messages: field required"
    )
    assert openai_compat.redact("bad key sk-no-key-required", "sk-no-key-required") == (
        "bad key [已隐藏]"
    )
    assert openai_compat.redact("demo-gpt-5-staging", "sk-demo-staging-key-0000") == (
        "demo-gpt-5-staging"
    )
