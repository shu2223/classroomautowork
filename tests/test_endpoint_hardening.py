"""Edge cases of the three model providers found in review: a toolbar left on another provider, a
connection test nobody waits for, one time limit across a resend, model ids Claude Code renames,
the model a relay really served, slow streams, a relay's own reason for a refused key, relay and
OpenRouter error shapes, more servers' wordings of the output limit, and what a failed answer or
test says. Claude Code is the stand-in script of test_anthropic_compatible; the OpenAI-compatible
endpoint is the threaded local fake of test_openai_compat. Nothing leaves 127.0.0.1."""

import json
import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest
from test_anthropic_compatible import (
    CLAUDE_KEY,
    KEY,
    fake_cli,
    relay_turn,
    runs,
    save_compat,
)
from test_frontend import (  # noqa: F401
    FrontendJobs,
    local_server,
    offline_rpc,
    public,
    seed_pending,
    unit_settings,
)
from test_openai_compat import Endpoint, chunks, reply_json, reply_stream
from test_workflow import prepared_fixture

from classroomautowork import (
    api_key,
    claude_agent,
    connection_test,
    drafting,
    openai_compat,
    providers,
)
from classroomautowork.errors import ConfigurationError, RequestFailed, RunCancelled, WorkflowError

ORIGIN = "https://yishuapp.com"
SCHEMA = {"type": "object"}
OPENAI_KEY = "sk-hard-" + "h" * 30 + "J4kL"


@pytest.fixture
def endpoint():
    server = Endpoint()
    yield server
    server.close()


def save_openai(data_dir, server, **extra):
    providers.save_openai(
        data_dir,
        {"base_url": server.base, "api_key": OPENAI_KEY, "models": "probe-a", **extra},
        verify=lambda *_: {"listed": True, "ids": ["probe-a"], "status": 200},
    )


def refusal(code, message, **error):
    return openai_compat._Refused(
        code, json.dumps({"error": {"message": message, **error}}).encode(), {}
    )


LIMITED = {
    "effort": None,
    "effort_lowered": False,
    "stream": True,
    "stream_options": True,
    "format": "json_schema",
    "images": True,
    "images_why": None,
    "limit": 16000,
    "limit_param": "max_tokens",
    "limit_clamped": False,
}


# ---------------------------------------------------------------- R1: the provider the page showed


def test_a_job_never_starts_on_a_provider_other_than_the_one_the_toolbar_listed(
    tmp_path, monkeypatch
):
    jobs = FrontendJobs(unit_settings(tmp_path))
    seed_pending(jobs)
    monkeypatch.setattr(jobs, "_run", lambda _: None)
    api_key.save_key(tmp_path, CLAUDE_KEY, verify=lambda _: None)
    save_compat(tmp_path, models="claude-opus-5-5")
    providers.save_provider(tmp_path, {"provider": "anthropic_compatible"})
    shown = jobs.models()["provider"]
    assert shown == "anthropic_compatible"
    providers.save_provider(tmp_path, {"provider": "anthropic"})  # another tab or device
    selected = [{"course_id": "1", "assignment_id": "2"}]
    with pytest.raises(ConfigurationError, match="已在别处切换为 Claude API") as caught:
        jobs.start_selected(
            selected,
            model="claude-opus-5-5",
            effort="high",
            ai_confirmed=True,
            shown_provider=shown,
        )
    assert "Anthropic 兼容接口的模型" in str(caught.value)
    assert not jobs._jobs  # nothing started, nothing billed
    with pytest.raises(ConfigurationError, match="模型接口无效"):
        jobs.start_selected(selected, ai_confirmed=True, shown_provider="gemini")
    job = jobs.start_selected(
        selected,
        model="claude-opus-5-5",
        effort="high",
        ai_confirmed=True,
        shown_provider="anthropic",
    )
    assert job["provider"] == "anthropic"


def test_the_start_and_redo_routes_carry_the_listed_provider(local_server):  # noqa: F811
    data_dir = local_server.jobs.settings.data_dir
    save_compat(data_dir, models="claude-opus-5-5")
    body = {
        "selected": [{"course_id": "1", "assignment_id": "2"}],
        "model": "claude-opus-5-5",
        "effort": "high",
        "ai_confirmed": True,
        "provider": "anthropic_compatible",
    }
    status, _, raw = public(
        local_server, "/classroom/api/review", method="POST", body=body, origin=ORIGIN
    )
    error = json.loads(raw)["error"]
    assert status == 400 and "已在别处切换为 Claude API" in error and "不支持的参数" not in error
    web = Path(connection_test.__file__).parent / "web"
    script = (web / "app.js").read_text(encoding="utf-8")
    assert "provider: state.models?.provider || null" in script  # 完成 sends the toolbar's
    assert "model, effort, provider}" in script  # 保存并重做 sends the dialog's
    # A switch made elsewhere reloads the toolbar's list.
    assert "state.models.provider !== state.provider.active) loadModels()" in script


# ---------------------------------------------------------------- R2: a test nobody waits for


def test_a_test_the_page_stopped_waiting_for_ends_and_is_not_kept(tmp_path, monkeypatch):
    log = fake_cli(tmp_path, monkeypatch, {"mode": "hang"})
    save_compat(tmp_path)
    gone = threading.Event()
    threading.Timer(0.6, gone.set).start()
    started = time.monotonic()
    with pytest.raises(RunCancelled):
        connection_test.run(tmp_path, {"provider": "anthropic_compatible"}, cancelled=gone.is_set)
    assert time.monotonic() - started < 6
    assert not connection_test.RUNNING.locked()
    assert "anthropic_compatible" not in providers.last_tests(tmp_path)
    (run,) = runs(log)
    with pytest.raises(ProcessLookupError):
        os.kill(run["pid"], 0)  # Claude Code went with the test


def test_a_closed_page_frees_the_test_at_once_over_http(local_server, monkeypatch):  # noqa: F811
    data_dir = local_server.jobs.settings.data_dir
    log = fake_cli(data_dir, monkeypatch, {"mode": "hang"})
    save_compat(data_dir)
    body = json.dumps({"provider": "anthropic_compatible"}).encode()
    sock = socket.create_connection(("127.0.0.1", local_server.server_port), timeout=5)
    sock.sendall(
        b"POST /classroom/api/settings/test HTTP/1.1\r\nHost: yishuapp.com\r\n"
        b"Cf-Access-Jwt-Assertion: unit\r\nOrigin: https://yishuapp.com\r\n"
        b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body) + body
    )
    for _ in range(100):
        if runs(log):
            break
        time.sleep(0.05)
    assert connection_test.RUNNING.locked()
    sock.close()  # the page gave up, reloaded or was closed
    for _ in range(100):
        if not connection_test.RUNNING.locked():
            break
        time.sleep(0.05)
    assert not connection_test.RUNNING.locked()
    fake_cli(data_dir, monkeypatch, {})  # the next test answers
    status, _, raw = public(
        local_server,
        "/classroom/api/settings/test",
        method="POST",
        body={"provider": "anthropic_compatible"},
        origin=ORIGIN,
    )
    assert status == 200 and json.loads(raw)["ok"] is True
    with pytest.raises(ProcessLookupError):
        os.kill(runs(log)[0]["pid"], 0)


def test_a_stopping_server_ends_a_running_test(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, {"mode": "hang"})
    save_compat(tmp_path)
    outcome = {}

    def test():
        try:
            connection_test.run(tmp_path, {"provider": "anthropic_compatible"})
        except RunCancelled as exc:
            outcome["cancelled"] = str(exc)

    thread = threading.Thread(target=test)
    thread.start()
    for _ in range(100):
        if connection_test.RUNNING.locked():
            break
        time.sleep(0.02)
    time.sleep(0.3)
    started = time.monotonic()
    connection_test.stop(timeout=10)
    thread.join(5)
    assert "cancelled" in outcome and time.monotonic() - started < 6
    assert not connection_test.STOPPING.is_set()  # a later server in this process can test


# ---------------------------------------------------------------- R3: one time limit for the turn


def test_a_resend_at_the_maximum_gets_what_is_left_of_the_time_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_agent, "RETRY_ROOM_SECONDS", 0.5)
    log = fake_cli(tmp_path, monkeypatch, {"mode": "cap_then_hang", "cap": 8000, "sleep": 1.0})
    started = time.monotonic()
    with pytest.raises(WorkflowError, match="的回合超过 3 秒"):
        relay_turn(tmp_path, max_output_tokens=16000, turn_seconds=3, idle_seconds=60)
    elapsed = time.monotonic() - started
    # One limit of 3 s in all; a fresh clock for the resend would have run to about 4 s.
    assert [x["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] for x in runs(log)] == ["16000", "8000"]
    assert elapsed < 3.8


def test_too_little_time_left_reports_the_maximum_instead_of_resending(tmp_path, monkeypatch):
    log = fake_cli(tmp_path, monkeypatch, {"mode": "cap_then_hang", "cap": 8000, "sleep": 0.5})
    with pytest.raises(RequestFailed, match="不接受 16000 tokens 的输出上限，最多 8000") as caught:
        relay_turn(tmp_path, max_output_tokens=16000, turn_seconds=claude_agent.RETRY_ROOM_SECONDS)
    assert len(runs(log)) == 1
    assert caught.value.details == {
        "output_limit": {
            "requested": 16000,
            "sent": 16000,
            "param": "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        },
        "degradations": [],
    }


# ---------------------------------------------------------------- R5, F1, F3, F4: the endpoint's CLI


def test_the_endpoint_turn_inherits_no_routing_credentials_or_headers(monkeypatch):
    inherited = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "ANTHROPIC_CUSTOM_HEADERS": "X-Service: secret",
        "ANTHROPIC_MODEL": "claude-other",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret",
        "AWS_BEARER_TOKEN_BEDROCK": "bedrock-secret",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "999999",
        "CLAUDE_CODE_MAX_RETRIES": "50",
        "CLAUDE_CODE_EXTRA_BODY": '{"x": 1}',
        "HTTPS_PROXY": "http://proxy.local:3128",
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    env = claude_agent.turn_environment(KEY, base_url="https://relay.example.com", auth="helper")
    for name in inherited:
        if name != "HTTPS_PROXY":
            assert name not in env, name
    assert env["HTTPS_PROXY"] == "http://proxy.local:3128"  # the network itself stays
    assert env["CLAUDE_CODE_DISABLE_LEGACY_MODEL_REMAP"] == "1"
    tested = claude_agent.turn_environment(
        KEY, base_url="https://relay.example.com", auth="helper", retries=1
    )
    assert tested["CLAUDE_CODE_MAX_RETRIES"] == "1"
    # The Claude API's turn is untouched: exactly the service's environment plus its key.
    assert claude_agent.turn_environment(CLAUDE_KEY) == {
        **os.environ,
        "ANTHROPIC_API_KEY": CLAUDE_KEY,
    }
    assert claude_agent.turn_environment(CLAUDE_KEY, retries=1) == {
        **os.environ,
        "ANTHROPIC_API_KEY": CLAUDE_KEY,
    }


def test_a_renamed_model_id_says_which_name_claude_code_used(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, {"mode": "remap", "as": "claude-opus-5-5"})
    with pytest.raises(WorkflowError) as caught:
        claude_agent.run_turn(
            [], SCHEMA, model="opus", effort="low", cwd=tmp_path, api_key=KEY,
            base_url="https://relay.example.com", auth="helper", host="relay.example.com",
        )  # fmt: skip
    message = str(caught.value)
    assert "Claude Code 把模型名 opus 换成了 claude-opus-5-5" in message
    assert "填写接口给的完整模型 ID" in message
    with pytest.raises(WorkflowError, match=r"实际使用的模型（claude-opus-5-5）与所选模型（opus）"):
        claude_agent.run_turn(
            [], SCHEMA, model="opus", effort="low", cwd=tmp_path, api_key=CLAUDE_KEY
        )


def test_a_slow_stream_is_not_taken_for_silence(tmp_path, monkeypatch):
    log = fake_cli(tmp_path, monkeypatch, {"mode": "stream_slow", "gap": 0.4, "count": 5})
    outcome = relay_turn(tmp_path, idle_seconds=1)  # 2 s of streaming, never 1 s silent
    assert outcome["output"] == {"ok": True, "reply": "连接正常"}
    assert "--include-partial-messages" in runs(log)[0]["argv"]


def test_the_test_retries_once_so_the_relay_says_why_and_an_answer_keeps_its_retries(
    tmp_path, monkeypatch
):
    log = fake_cli(tmp_path, monkeypatch, {"mode": "unauthorized"})
    save_compat(tmp_path)
    result = connection_test.run(tmp_path, {"provider": "anthropic_compatible"})
    assert runs(log)[0]["CLAUDE_CODE_MAX_RETRIES"] == "1"
    main, detail = result["error"].split("\n")
    assert main == (
        "Anthropic 兼容接口（relay.example.com）拒绝了密钥，或令牌已过期、额度已用尽（HTTP 401）。"
        "请检查密钥是否属于这个接口，以及令牌和余额。"
    )
    assert "该令牌额度已用尽" in detail  # the relay's own words
    fake_cli(tmp_path, monkeypatch, {"mode": "retry401"})
    with pytest.raises(RequestFailed) as caught:
        relay_turn(tmp_path)
    assert runs(log)[-1]["CLAUDE_CODE_MAX_RETRIES"] is None
    assert "令牌已过期、额度已用尽（HTTP 401），未作答" in str(caught.value)
    assert "点“测试连接”可以看到接口给出的原因" in str(caught.value)


# ---------------------------------------------------------------- F2: the model the relay served


def test_the_reported_model_is_the_one_the_relays_replies_named(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, {"mode": "served", "served": "claude-sonnet-4-5-20250929"})
    outcome = relay_turn(tmp_path)
    assert outcome["model"] == "claude-sonnet-4-5-20250929"  # not Claude Code's own <synthetic>
    save_compat(tmp_path)
    result = connection_test.run(tmp_path, {"provider": "anthropic_compatible"})
    assert result["reported_model"] == "claude-sonnet-4-5-20250929"
    assert "接口报告的模型是 claude-sonnet-4-5-20250929。" in result["message"]
    fake_cli(tmp_path, monkeypatch, {})  # no reply named one: the model Claude Code started with
    assert relay_turn(tmp_path)["model"] == "claude-sonnet-5-5"


def test_a_served_model_is_recorded_beside_the_requested_one(tmp_path, monkeypatch):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    fake_cli(tmp_path, monkeypatch, {"mode": "served", "served": "claude-sonnet-4-5-20250929"})
    save_compat(tmp_path)
    receipt = drafting.generate_review(package, ai_confirmed=True, provider="anthropic_compatible")
    generation = receipt["generation"]
    assert generation["requested_model"] == "claude-sonnet-5-5"
    assert generation["model"] == "claude-sonnet-4-5-20250929"


# ---------------------------------------------------------------- F5, F6, F15: relay error shapes


def test_no_channel_for_the_model_is_the_model_or_group_on_both_paths(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, {"mode": "no_channel"})
    with pytest.raises(RequestFailed) as caught:
        relay_turn(tmp_path)
    main = str(caught.value).split("\n")[0]
    assert "没有可用于模型 claude-sonnet-5-5 的渠道（HTTP 503）" in main and "稍后重试" not in main
    body = json.dumps(
        {
            "error": {
                "message": "当前分组 default 下对于模型 probe-a 无可用渠道",
                "type": "new_api_error",
            }
        }
    ).encode()
    error = openai_compat._http_error(503, body, {}, key=OPENAI_KEY, model="probe-a", host="h")
    assert isinstance(error, RequestFailed)
    assert "没有可用于模型“probe-a”的渠道（HTTP 503）" in str(error) and "请稍后重试" not in str(
        error
    )


def test_a_test_that_ends_among_retries_says_the_endpoint_kept_failing(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, {"mode": "retry503"})
    with pytest.raises(RequestFailed) as caught:
        relay_turn(tmp_path, turn_seconds=1.5)
    message = str(caught.value)
    assert message.startswith(
        "Anthropic 兼容接口（relay.example.com）连续返回 HTTP 503，Claude Code 重试"
    )
    assert "没有任何输出" not in message


def test_a_404_names_the_address_when_the_save_saw_no_anthropic_model_list(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, {"mode": "model404"})
    providers.save_compat(
        tmp_path,
        {"base_url": "https://relay.example.com/v1", "api_key": KEY, "models": "claude-sonnet-5-5"},
        verify=lambda *_: {"listed": True, "ids": ["gpt-x"], "status": 200, "shape": "openai"},
    )
    config = providers.turn_config(tmp_path, providers.COMPAT)
    assert config["doubt_address"] is True
    result = connection_test.run(tmp_path, {"provider": "anthropic_compatible"})
    assert result["error"].startswith(
        "Anthropic 兼容接口（relay.example.com）的这个地址可能不提供 Anthropic 格式的 /v1/messages"
    )
    assert "ANTHROPIC_BASE_URL" in result["error"]
    save_compat(tmp_path)  # an Anthropic-format list: the model first, the address too
    assert providers.turn_config(tmp_path, providers.COMPAT)["doubt_address"] is False
    with pytest.raises(RequestFailed) as caught:
        relay_turn(tmp_path)
    assert "找不到模型 claude-sonnet-5-5" in str(caught.value)
    assert "不提供 Anthropic 格式的 /v1/messages" in str(caught.value)


def test_a_web_page_from_the_relay_points_at_the_address(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, {"mode": "malformed"})
    with pytest.raises(RequestFailed) as caught:
        relay_turn(tmp_path)
    assert "返回的不是 Anthropic 格式的回复" in str(caught.value)
    assert "ANTHROPIC_BASE_URL" in str(caught.value)


# ---------------------------------------------------------------- R4: the saved address is the used one


@pytest.mark.parametrize(
    "value, expected",
    [
        ("https://x.example.com/v1/v1/messages", "https://x.example.com"),
        ("https://x.example.com/messages/v1", "https://x.example.com"),
        ("https://x.example.com/api/v1/messages/", "https://x.example.com/api"),
    ],
)
def test_reading_a_saved_address_again_gives_the_same_one(value, expected):
    once = providers.normalize_base_url(value, "anthropic")
    assert once == expected and providers.normalize_base_url(once, "anthropic") == once


# ---------------------------------------------------------------- F7–F10, F17, R7, R8: wordings


@pytest.mark.parametrize(
    "message, step",
    [
        # F7 AWS Bedrock through a relay's AWS channel
        (
            "ValidationException: The maximum tokens you requested exceeds the model limit of 8192. "
            "Try again with a maximum tokens value that is lower than 8192.",
            ("max_tokens_clamp", 8192),
        ),
        # F9 OpenRouter "requested about", SGLang "maximum context length of", Groq backticks
        (
            "This endpoint's maximum context length is 131072 tokens. However, you requested about "
            "140000 tokens (124000 of text input, 16000 in the output).",
            ("max_tokens_clamp", 7072),
        ),
        (
            "Requested token count exceeds the model's maximum context length of 32768 tokens. You "
            "requested a total of 40000 tokens: 24000 tokens from the input messages and 16000 "
            "tokens for the completion.",
            ("max_tokens_clamp", 8768),
        ),
        (
            "`max_tokens` must be less than or equal to `8192`, the maximum value for `max_tokens` "
            "is less than the `context_window` for this model",
            ("max_tokens_clamp", 8192),
        ),
        # F10 a unit is no number of tokens; R7 the maximum stated first, never one no answer fits
        ("max_tokens cannot exceed 64K for this model", ("max_tokens", None)),
        ("max_tokens 超过上限 32k，请调小后重试", ("max_tokens", None)),
        (
            "max_tokens must be <= 8192; this endpoint accepts at most 4 images per request",
            ("max_tokens_clamp", 8192),
        ),
        (
            "this endpoint accepts at most 4 images per request; max_tokens must be <= 8192",
            ("max_tokens_clamp", 8192),
        ),
        ("max_tokens must be <= 512", ("max_tokens", None)),
    ],
)
def test_more_servers_words_for_the_output_limit(message, step):
    assert openai_compat._degrade(refusal(400, message), dict(LIMITED)) == step


def test_openrouters_wrapped_provider_error_is_read_and_shown():
    raw = json.dumps(
        {
            "error": {
                "message": "max_tokens is too large: 16000. This model supports at most 8192 "
                "completion tokens, whereas you provided 16000."
            }
        }
    )
    body = json.dumps(
        {"error": {"message": "Provider returned error", "code": 400, "metadata": {"raw": raw}}}
    ).encode()
    refused = openai_compat._Refused(400, body, {})
    assert openai_compat._degrade(refused, dict(LIMITED)) == ("max_tokens_clamp", 8192)
    message, _ = openai_compat.error_fields(body)
    assert message.startswith("Provider returned error: max_tokens is too large")
    plain = json.dumps({"error": {"message": "Bad thing", "metadata": None}}).encode()
    assert openai_compat.error_fields(plain)[0] == "Bad thing"


@pytest.mark.parametrize(
    "message",
    [
        "the request exceeds the available context size, try increasing it",
        "This model's maximum prompt length is 131072 but the request contains 140000 tokens.",
        "Invalid request: Your request exceeded model token limit: 8192",
        "Prompt 超长",
        "length of prompt_tokens (40000) must be less than max_seq_len (32768).",
    ],
)
def test_more_servers_words_for_a_request_too_long(message):
    mode = {**LIMITED, "limit": None}
    assert openai_compat._degrade(refusal(400, message), mode) == ("images_size", None)
    error = openai_compat._http_error(
        400, refusal(400, message).body, {}, key=OPENAI_KEY, model="m", host="h"
    )
    assert "超出了这个模型能读的长度" in str(error)


def test_a_refused_effort_parameter_is_dropped_at_once():
    mode = {**LIMITED, "limit": None, "effort": "xhigh"}
    for message in (
        "Unrecognized request argument supplied: reasoning_effort",
        "Argument not supported on this model: reasoningEffort",
    ):
        assert openai_compat._degrade(refusal(400, message), dict(mode)) == (
            "reasoning_effort",
            None,
        )
    listed = "Unsupported value: 'xhigh'. Supported values are: 'low', 'medium', and 'high'."
    assert openai_compat._degrade(refusal(400, listed, param="reasoning_effort"), dict(mode)) == (
        "reasoning_effort_lowered",
        "high",
    )


# ---------------------------------------------------------------- F11, UX-3: a cut answer's words and record


def test_a_cut_answer_after_the_limit_was_refused_does_not_ask_for_the_limit_again(endpoint):
    config = {
        "base_url": endpoint.base,
        "api_key": OPENAI_KEY,
        "host": endpoint.base.split("/")[2],
        "models": ["unit-model"],
        "max_output_tokens": 16000,
    }
    endpoint.replies += [
        reply_json(
            {"error": {"message": "Unrecognized request argument supplied: max_tokens"}}, 400
        ),
        reply_stream(chunks('{"ok": tr', finish="length")),
    ]
    with pytest.raises(WorkflowError) as caught:
        openai_compat.run_turn(
            [{"type": "text", "text": "原题"}],
            SCHEMA,
            model="unit-model",
            effort=None,
            config=config,
        )
    message = str(caught.value)
    assert "这个接口不接受输出上限参数，设置里的 16000 tokens 没有发送" in message
    assert "填写「输出上限」" not in message
    assert caught.value.details["output_limit"] == {"requested": 16000, "sent": None, "param": None}
    assert caught.value.details["degradations"] == ["max_tokens"]


def test_a_failed_answer_records_the_limit_it_was_sent_with(tmp_path, monkeypatch):
    from test_output_limit import openai_package

    server = Endpoint()
    try:
        package, _ = openai_package(tmp_path, monkeypatch, server)
        providers.save_openai(
            tmp_path,
            {"base_url": server.base, "models": "unit-model", "max_output_tokens": 16000},
            verify=lambda *_: {"listed": False, "ids": [], "status": 404},
        )
        refused = "max_tokens: 16000 > 8000, which is the maximum allowed number of output tokens"
        server.replies += [
            reply_json({"error": {"message": refused}}, 400),
            reply_stream(chunks('{"draft": "', finish="length")),
        ]
        with pytest.raises(WorkflowError, match="这已经是接口说这个模型最多能输出的 token 数"):
            drafting.generate_review(
                package, ai_confirmed=True, model="unit-model", provider="openai"
            )
        record = next(Path(package).parent.glob("*/generation.json"))
        generation = json.loads(record.read_text(encoding="utf-8"))
        assert generation["status"] == "failed"
        assert generation["output_limit"] == {
            "requested": 16000,
            "sent": 8000,
            "param": "max_tokens",
        }
        assert generation["degradations"] == ["max_tokens_clamp"]
        assert generation["effort"] == {"requested": None, "sent": None}
    finally:
        server.close()


def test_a_failed_claude_api_turn_with_nothing_set_records_nothing_new(tmp_path, monkeypatch):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    fake_cli(tmp_path, monkeypatch, {"mode": "model404"})
    api_key.save_key(tmp_path, CLAUDE_KEY, verify=lambda _: None)
    with pytest.raises(RequestFailed):
        drafting.generate_review(
            package, ai_confirmed=True, model="claude-sonnet-5-5", effort="high"
        )
    record = next(
        x for x in Path(package).parent.glob("*/generation.json") if "relay" not in x.read_text()
    )
    generation = json.loads(record.read_text(encoding="utf-8"))
    assert generation["status"] == "failed"
    assert "output_limit" not in generation and "degradations" not in generation


# ---------------------------------------------------------------- F12–F14, R9, UX-2: what a test says


def test_a_test_through_a_relay_that_ignores_response_format_says_so(tmp_path, endpoint):
    save_openai(tmp_path, endpoint)
    fenced = (
        "好的，下面是结果：\n```json\n" + json.dumps({"ok": True, "reply": "连接正常"}) + "\n```"
    )
    endpoint.replies.append(reply_stream(chunks(fenced)))
    result = connection_test.run(tmp_path, {"provider": "openai"})
    assert result["ok"] and result["reply_json"] == "extracted"
    assert connection_test.IGNORED_SCHEMA in result["notes"]
    assert providers.last_tests(tmp_path)["openai"]["summary"].startswith(
        "正常 · 未按 JSON Schema 输出 · "
    )
    endpoint.replies.append(reply_stream(chunks(json.dumps({"ok": True}))))  # today's 缺少字段
    result = connection_test.run(tmp_path, {"provider": "openai"})
    assert not result["ok"]
    assert result["error"].startswith("模型返回的 JSON 不符合要求的格式：结果 缺少字段 reply。")
    assert "接口似乎没有按 JSON Schema（response_format）约束输出。" in result["error"]
    assert "资料已保留" not in result["error"]


def test_a_failed_test_names_its_host_and_model_and_reads_as_a_test(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, {"mode": "retry401"})
    save_compat(tmp_path, models="claude-sonnet-5-5, relay-custom")
    result = connection_test.run(tmp_path, {"provider": "anthropic_compatible"})
    assert not result["ok"]
    assert (
        result["host"] == "relay.example.com" and result["requested_model"] == "claude-sonnet-5-5"
    )
    first, detail = result["error"].split("\n")
    for words in ("未作答", "资料已保留", "设置 → 模型接口"):
        assert words not in first
    # A test retries once and waits for that result: the relay's words, not the bare category.
    assert detail == "Failed to authenticate. API Error: 401 invalid x-api-key"
    kept = providers.last_tests(tmp_path)["anthropic_compatible"]
    assert kept["model"] == "claude-sonnet-5-5" and kept["summary"] == first


@pytest.mark.parametrize(
    "error, expected",
    [
        (
            "OpenAI 兼容接口找不到这个地址或模型“x”（HTTP 404）。请在设置里检查接口地址（通常以 /v1 结尾）"
            "和模型名称；资料已保留。\nNot found",
            "OpenAI 兼容接口找不到这个地址或模型“x”（HTTP 404）。请检查接口地址（通常以 /v1 结尾）"
            "和模型名称。\nNot found",
        ),
        (
            "Claude 回合超过 2 分钟或 60 秒没有任何输出，已停止本次等待；资料已保留，请重试。",
            "Claude 回合超过 2 分钟或 60 秒没有任何输出，已停止本次等待，请重试。",
        ),
        (
            "Anthropic 兼容接口（h）的回合未成功，资料已保留，请重试。\nAPI Error",
            "Anthropic 兼容接口（h）的回合未成功，请重试。\nAPI Error",
        ),
        (
            "模型的输出达到了输出上限（16000 tokens），结果被截断（stop_reason: max_tokens）。"
            "可以在设置 → 模型接口里调高「输出上限」后重试（推理用的 token 也算在内）；资料已保留。",
            "模型的输出达到了输出上限（16000 tokens），结果被截断（stop_reason: max_tokens）。"
            "可以调高「输出上限」后重试（推理用的 token 也算在内）。",
        ),
        (
            "Anthropic 拒绝了 Claude API 密钥（HTTP 401），未作答。请在设置里检查或更换密钥；资料已保留。",
            "Anthropic 拒绝了 Claude API 密钥（HTTP 401）。请检查或更换密钥。",
        ),
    ],
)
def test_test_errors_drop_what_only_an_answer_needs_said(error, expected):
    assert connection_test.test_wording(error) == expected


def test_a_refused_image_on_the_claude_code_path_is_said_as_it_happens(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, {"mode": "images"})
    save_compat(tmp_path)
    result = connection_test.run(tmp_path, {"provider": "anthropic_compatible"})
    assert result["images"] == "refused"
    assert result["notes"][0].startswith(
        "接口不接受图片：作答时每次都会先被拒一次，Claude Code 去掉图片"
    )
    summary = providers.last_tests(tmp_path)["anthropic_compatible"]["summary"]
    assert summary.startswith("正常 · 不接受图片 · ")


# ---------------------------------------------------------------- UX-1, UX-5: the settings copy


def test_the_anthropic_compatible_group_says_what_matters_briefly():
    page = (Path(connection_test.__file__).parent / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    group = page.split('id="compatSettings"', 1)[1].split("</section>", 1)[0]
    foot = re.sub(r"<[^>]+>", "", group.split('class="group-foot">', 1)[1].split("</p>", 1)[0])
    assert len(foot) < 100 and "Claude Code" in foot and "学生资料" in foot and "少量 token" in foot
    key_help = group.split('id="compatKeyHelp"', 1)[1].split("</label>", 1)[0]
    assert "ANTHROPIC_AUTH_TOKEN" not in key_help and "x-api-key 和 Bearer" in key_help
    # One line each at 390 px (checked in the staging screenshots), so no orphaned 「接口」.
    subs = [x.split("<", 1)[0] for x in page.split('class="provider-sub">')[1:]]
    assert subs[1:] == ["经本机 Claude Code 连接中转站", "DeepSeek、通义千问、Ollama 等"]
