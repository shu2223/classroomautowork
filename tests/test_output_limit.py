"""The OpenAI-compatible output limit (输出上限) and the full effort levels, against the local fake
endpoint of test_openai_compat on 127.0.0.1; no real endpoint is used."""

import json
import stat
from pathlib import Path

import pytest
from test_api_key import listing
from test_frontend import (  # noqa: F401
    configure_openai,
    local_server,
    public,
    seed_pending,
    unit_settings,
)
from test_generation_stream import openai_package
from test_openai_compat import (
    KEY,
    MODE,
    SCHEMA,
    Endpoint,
    answer,
    chunks,
    refusal,
    reply_json,
    reply_stream,
    turn,
)

from classroomautowork import drafting, openai_compat, providers
from classroomautowork.errors import ConfigurationError, RequestFailed, WorkflowError
from classroomautowork.ui_jobs import FrontendJobs

GOOD = {"base_url": "https://api.example.com/v1", "api_key": "sk-limit-" + "k" * 30, "models": "m"}
LIMITED = {**MODE, "limit": 16000, "limit_param": "max_tokens", "limit_clamped": False}


@pytest.fixture
def endpoint():
    server = Endpoint()
    yield server
    server.close()


def limited(server, limit=16000):
    return {**server.config, "max_output_tokens": limit}


# ---------------------------------------------------------------- the setting


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        (1000, 1000),
        (1_000_000, 1_000_000),
        (16000, 16000),
        ("16000", 16000),
        (" 16000\n", 16000),
        (16000.0, 16000),
    ],
)
def test_the_limit_takes_an_integer_in_range_or_nothing(value, expected):
    assert providers.parse_output_limit(value) == expected


@pytest.mark.parametrize(
    "value",
    [True, False, 999, 1_000_001, 0, -5, 16000.5, "16k", "16,000", "1.6e4", "１６０００", "0x3e80",
     "16000 tokens", [16000], {"n": 16000}, float("nan"), float("inf"), "9" * 40],
)  # fmt: skip
def test_anything_else_is_refused_with_the_range_named(value):
    with pytest.raises(ConfigurationError, match="1000 到 1000000 之间的整数"):
        providers.parse_output_limit(value)


def test_the_limit_is_saved_verified_and_shown(tmp_path):
    verify, calls = listing(["m"])
    view = providers.save_openai(tmp_path, {**GOOD, "max_output_tokens": " 16000 "}, verify=verify)
    assert view["openai"]["max_output_tokens"] == 16000 and len(calls) == 1
    record = json.loads((tmp_path / providers.OPENAI_FILE).read_text(encoding="utf-8"))
    assert record["max_output_tokens"] == 16000
    assert stat.S_IMODE((tmp_path / providers.OPENAI_FILE).stat().st_mode) == 0o600
    assert providers.status(tmp_path)["openai"]["max_output_tokens"] == 16000
    assert providers.settings_view(tmp_path)["openai"]["max_output_tokens"] == 16000
    assert providers.turn_config(tmp_path)["max_output_tokens"] == 16000
    # A save that only changes the limit still goes through the check, keeping the saved key.
    view = providers.save_openai(
        tmp_path,
        {"base_url": GOOD["base_url"], "models": "m", "max_output_tokens": 32000},
        verify=verify,
    )
    assert view["openai"]["max_output_tokens"] == 32000 and len(calls) == 2
    assert calls[-1] == (GOOD["base_url"], GOOD["api_key"])
    # null, "" or no field at all: no limit.
    for cleared in ({**GOOD, "max_output_tokens": None}, {**GOOD, "max_output_tokens": ""}, GOOD):
        providers.save_openai(tmp_path, {**GOOD, "max_output_tokens": 20000}, verify=verify)
        assert providers.save_openai(tmp_path, cleared, verify=verify)["openai"][
            "max_output_tokens"
        ] is None  # fmt: skip


def test_a_refused_limit_saves_nothing_and_checks_nothing(tmp_path):
    verify, calls = listing(["m"])
    providers.save_openai(tmp_path, {**GOOD, "max_output_tokens": 16000}, verify=verify)
    before = (tmp_path / providers.OPENAI_FILE).read_bytes()
    for value in ("16k", "16,000", True, 1.5, 500, [1], {"a": 1}):
        with pytest.raises(ConfigurationError, match="输出上限"):
            providers.save_openai(tmp_path, {**GOOD, "max_output_tokens": value}, verify=verify)
    assert (tmp_path / providers.OPENAI_FILE).read_bytes() == before and len(calls) == 1


@pytest.mark.parametrize("saved", [None, "16000", True, 12, 5_000_000, [1], "x"])
def test_an_older_or_wrong_file_value_reads_as_no_limit_and_not_as_damage(tmp_path, saved):
    verify, _ = listing(["m"])
    providers.save_openai(tmp_path, GOOD, verify=verify)
    path = tmp_path / providers.OPENAI_FILE
    record = json.loads(path.read_text(encoding="utf-8"))
    if saved is None:
        record.pop("max_output_tokens")  # a file from before the limit existed
    else:
        record["max_output_tokens"] = saved
    path.write_text(json.dumps(record), encoding="utf-8")
    status = providers.openai_status(tmp_path)
    assert status["configured"] and not status["damaged"] and status["max_output_tokens"] is None
    assert providers.turn_config(tmp_path)["max_output_tokens"] is None


def test_the_route_takes_the_limit_and_the_bootstrap_shows_it(local_server, monkeypatch):  # noqa: F811
    verify, _ = listing(["m"])
    monkeypatch.setattr(providers, "list_models", verify)
    origin, path = "https://yishuapp.com", "/classroom/api/settings/openai"
    body = {**GOOD, "max_output_tokens": "16000"}
    status, _, saved = public(local_server, path, method="PUT", body=body, origin=origin)
    assert status == 200 and json.loads(saved)["openai"]["max_output_tokens"] == 16000
    status, _, refused = public(
        local_server, path, method="PUT", body={**GOOD, "max_output_tokens": "16k"}, origin=origin
    )
    assert status == 400 and "1000 到 1000000" in json.loads(refused)["error"]
    _, _, boot = public(local_server, "/classroom/api/bootstrap")
    assert json.loads(boot)["provider"]["openai"]["max_output_tokens"] == 16000
    assert GOOD["api_key"].encode() not in saved + refused + boot


# ---------------------------------------------------------------- the request


def test_the_limit_rides_as_max_tokens_and_without_it_the_body_is_unchanged(endpoint):
    endpoint.replies += [reply_stream(chunks(json.dumps(answer())))] * 2
    outcome, _ = turn(endpoint, effort="high")
    plain = endpoint.requests[0]["body"]
    assert plain == openai_compat.request_body(
        [{"type": "text", "text": "原题与资料"}],
        SCHEMA,
        "unit-model",
        {k: MODE[k] for k in MODE} | {"effort": "high"},
    )
    assert "max_tokens" not in plain and "max_completion_tokens" not in plain
    assert outcome["provider"]["output_limit"] == {"requested": None, "sent": None, "param": None}
    outcome = turn_with(endpoint, effort="high")
    body = endpoint.requests[1]["body"]
    assert (
        body["max_tokens"] == 16000
        and {k: v for k, v in body.items() if k != "max_tokens"} == plain
    )
    assert outcome["provider"]["output_limit"] == {
        "requested": 16000,
        "sent": 16000,
        "param": "max_tokens",
    }


def turn_with(endpoint, config=None, **options):
    labels = []
    options.setdefault("model", "unit-model")
    options.setdefault("effort", None)
    return openai_compat.run_turn(
        [{"type": "text", "text": "原题与资料"}],
        SCHEMA,
        config=config or limited(endpoint),
        on_activity=labels.append,
        **options,
    )


CLAMPS = [
    # OpenAI
    ("max_tokens is too large: 16000. This model supports at most 8192 completion tokens, "
     "whereas you provided 16000.", 8192),
    # DeepSeek
    ("Invalid max_tokens value, the valid range of max_tokens is [1, 8192]", 8192),
    # Anthropic through a relay
    ("max_tokens: 16000 > 8192, which is the maximum allowed number of output tokens for "
     "claude-opus-5-5", 8192),
    # Vertex / Gemini: X exclusive, so X - 1
    ("Unable to submit request because it has a maxOutputTokens value of 16000 but the supported "
     "range is from 1 (inclusive) to 8193 (exclusive). Update the value and try again.", 8192),
    ("max_tokens must be less than or equal to 8,192", 8192),
    ("max_tokens: Input should be less than or equal to 4096", 4096),
    ("max_tokens must be <= 4096", 4096),
    ("max_tokens cannot exceed 4096", 4096),
    ("The max_tokens should not exceed 12,000 for this model", 12000),
    ("max_tokens 参数错误：最大值为 8192", 8192),
    ("max_tokens 最多 8192", 8192),
    ("max_tokens 超出上限 8192", 8192),
    ("max_tokens 不能超过 8192", 8192),
    ("max_tokens 不超过 8192", 8192),
    ("max_tokens 的取值范围为 1 到 8192", 8192),
    ("max_tokens 的取值范围是 [1, 8192]", 8192),
    # context arithmetic: C - (T - K)
    ("This model's maximum context length is 128,000 tokens. However, you requested 140,000 tokens "
     "(124,000 in the messages, 16,000 in the completion). Please reduce the length of the "
     "messages or completion.", 4000),
    ("This model's maximum context length is 128000 tokens. However, you requested 130000 tokens "
     "(114000 of text input, 0 of image input, 16000 in the output).", 14000),
    # vLLM and Anthropic sums: C - P
    ("'max_tokens' or 'max_completion_tokens' is too large: 16000. This model's maximum context "
     "length is 32768 tokens and your request has 20000 input tokens (16000 > 32768 - 20000).", 12768),
    ("input length and `max_tokens` exceed context limit: 190000 + 16000 > 200000, decrease input "
     "length or `max_tokens` and try again", 10000),
]  # fmt: skip


@pytest.mark.parametrize("message, room", CLAMPS)
def test_a_stated_maximum_is_read_in_each_servers_words(message, room):
    assert openai_compat._degrade(refusal(400, message), dict(LIMITED)) == (
        "max_tokens_clamp",
        room,
    )


@pytest.mark.parametrize(
    "message",
    [
        "Unrecognized request argument supplied: max_tokens",
        'unknown field "max_tokens"',
        "max_tokens is invalid for this model",
        # a maximum that is not below the limit is no use
        "max_tokens is too large: 16000. This model supports at most 16000 completion tokens",
        "max_tokens must be <= 32000",
    ],
)
def test_a_limit_refusal_without_a_usable_maximum_drops_the_limit(message):
    assert openai_compat._degrade(refusal(400, message), dict(LIMITED)) == ("max_tokens", None)


def test_fastapi_extra_inputs_on_max_tokens_drop_the_limit():
    body = {"detail": [{"loc": ["body", "max_tokens"], "msg": "Extra inputs are not permitted"}]}
    refused = openai_compat._Refused(422, json.dumps(body).encode(), {})
    assert openai_compat._degrade(refused, dict(LIMITED)) == ("max_tokens", None)


def test_reasoning_models_get_the_limit_as_max_completion_tokens():
    message = (
        "Unsupported parameter: 'max_tokens' is not supported with this model. "
        "Use 'max_completion_tokens' instead."
    )
    assert openai_compat._degrade(refusal(400, message), dict(LIMITED)) == (
        "max_tokens_param",
        "max_completion_tokens",
    )
    # Once switched, the same words drop the limit rather than loop.
    switched = {**LIMITED, "limit_param": "max_completion_tokens"}
    assert openai_compat._degrade(refusal(400, message), switched) == ("max_tokens", None)


def test_limit_words_are_no_step_while_no_limit_is_sent():
    for message, _ in CLAMPS[:6]:
        assert openai_compat._degrade(refusal(400, message), dict(MODE)) is None


def test_a_context_sum_that_leaves_too_little_room_is_the_size_handling():
    message = (
        "This model's maximum context length is 8192 tokens. However, you requested 20000 tokens "
        "(7800 in the messages, 12200 in the completion)."
    )
    assert openai_compat._degrade(refusal(400, message), dict(LIMITED)) == ("images_size", None)
    assert openai_compat._degrade(refusal(400, message), {**LIMITED, "images": False}) is None


def test_a_length_refusal_that_does_not_name_the_limit_keeps_todays_handling():
    for message in (
        "prompt is too long: 210000 tokens > 200000 maximum",
        "This model's maximum context length is 8192 tokens, including reasoning tokens.",
    ):
        assert openai_compat._degrade(refusal(400, message), dict(LIMITED)) == (
            "images_size",
            None,
        )


def test_a_length_refusal_about_the_completion_without_a_sum_drops_the_limit():
    message = "The requested output tokens exceed the context window of this model."
    assert openai_compat._degrade(refusal(400, message), dict(LIMITED)) == ("max_tokens", None)


def test_a_clamp_is_resent_once_and_recorded(endpoint):
    message = CLAMPS[0][0]
    endpoint.replies += [
        reply_json({"error": {"message": message, "param": "max_tokens"}}, 400),
        reply_stream(chunks(json.dumps(answer()))),
    ]
    outcome = turn_with(endpoint)
    first, second = (x["body"] for x in endpoint.requests)
    assert first["max_tokens"] == 16000 and second["max_tokens"] == 8192
    assert {k: v for k, v in first.items() if k != "max_tokens"} == {
        k: v for k, v in second.items() if k != "max_tokens"
    }
    details = outcome["provider"]
    assert details["degradations"] == ["max_tokens_clamp"]
    assert details["output_limit"] == {"requested": 16000, "sent": 8192, "param": "max_tokens"}


def test_each_limit_step_runs_at_most_once(endpoint):
    switch = "Unsupported parameter: 'max_tokens' is not supported. Use 'max_completion_tokens'."
    clamp = "max_completion_tokens must be less than or equal to 8192"
    endpoint.replies += [
        reply_json({"error": {"message": switch}}, 400),
        reply_json({"error": {"message": clamp}}, 400),
        reply_json({"error": {"message": clamp}}, 400),  # the clamp is not tried twice: drop
        reply_stream(chunks(json.dumps(answer()))),
    ]
    outcome = turn_with(endpoint)
    bodies = [x["body"] for x in endpoint.requests]
    assert len(bodies) == 4
    assert bodies[0]["max_tokens"] == 16000 and "max_completion_tokens" not in bodies[0]
    assert bodies[1]["max_completion_tokens"] == 16000 and "max_tokens" not in bodies[1]
    assert bodies[2]["max_completion_tokens"] == 8192
    assert "max_tokens" not in bodies[3] and "max_completion_tokens" not in bodies[3]
    details = outcome["provider"]
    assert details["degradations"] == ["max_tokens_param", "max_tokens_clamp", "max_tokens"]
    assert details["output_limit"] == {"requested": 16000, "sent": None, "param": None}


def test_a_limit_refusal_that_persists_ends_as_an_error_not_a_loop(endpoint):
    endpoint.replies += [
        reply_json({"error": {"message": "max_tokens is invalid"}}, 400) for _ in range(5)
    ]
    with pytest.raises(RequestFailed, match="不接受这次请求"):
        turn_with(endpoint)
    assert len(endpoint.requests) == 2


# ---------------------------------------------------------------- truncation


def test_truncation_with_a_limit_says_to_raise_it(endpoint):
    endpoint.replies.append(reply_stream(chunks(json.dumps(answer())[:200], finish="length")))
    with pytest.raises(WorkflowError) as caught:
        turn_with(endpoint)
    assert type(caught.value) is WorkflowError  # repaired once, not a RequestFailed
    assert str(caught.value) == (
        "模型的输出达到了输出上限（16000 tokens），结果被截断（finish_reason: length）。"
        "可以在设置 → 模型接口里调高「输出上限」后重试（推理用的 token 也算在内）；资料已保留。"
    )


def test_truncation_after_a_clamp_says_it_is_already_the_most(endpoint):
    endpoint.replies += [
        reply_json({"error": {"message": CLAMPS[1][0]}}, 400),
        reply_stream(chunks(json.dumps(answer())[:200], finish="length")),
    ]
    with pytest.raises(
        WorkflowError, match=r"输出上限（8192 tokens）.*最多能输出.*输出上限更大的模型"
    ):
        turn_with(endpoint)


def test_truncation_without_a_limit_suggests_one(endpoint):
    endpoint.replies.append(reply_stream(chunks(json.dumps(answer())[:200], finish="length")))
    with pytest.raises(WorkflowError, match="接口的默认输出上限.*填写「输出上限」（例如 16000）"):
        turn_with(endpoint, config=endpoint.config)


def test_reasoning_that_used_up_the_output_is_said_so(endpoint):
    events = chunks("", reasoning=3, finish="length")
    endpoint.replies += [reply_stream(events), reply_stream(events)]
    with pytest.raises(WorkflowError, match="推理用完了输出上限（16000 tokens）"):
        turn_with(endpoint)
    with pytest.raises(WorkflowError, match="推理用完了接口的默认输出上限"):
        turn_with(endpoint, config=endpoint.config)


def test_a_truncated_answer_gets_its_one_repair(tmp_path, monkeypatch):
    server = Endpoint()
    try:
        package, result = openai_package(tmp_path, monkeypatch, server)
        providers.save_openai(
            tmp_path,
            {"base_url": server.base, "models": "unit-model", "max_output_tokens": 16000},
            verify=lambda *_: {"listed": False, "ids": [], "status": 404},
        )
        text = json.dumps(result, ensure_ascii=False)
        server.replies += [
            reply_stream(chunks(text[:300], finish="length")),
            reply_stream(chunks(text)),
        ]
        messages = []
        receipt = drafting.generate_answer(
            package,
            ai_confirmed=True,
            model="unit-model",
            provider="openai",
            progress=messages.append,
        )
        assert receipt["status"] == "ready_for_human_review" and len(server.requests) == 2
        assert all(x["body"]["max_tokens"] == 16000 for x in server.requests)
        assert any("自动修复" in str(x) for x in messages)
        generation = json.loads(
            (Path(receipt["package"]) / "generation.json").read_text(encoding="utf-8")
        )
        assert generation["output_limit"] == {
            "requested": 16000,
            "sent": 16000,
            "param": "max_tokens",
        }
        assert generation["effort"] == {"requested": None, "sent": None}
    finally:
        server.close()


# ---------------------------------------------------------------- the full effort levels


def test_the_endpoint_offers_all_five_efforts_and_claude_names(tmp_path):
    jobs = FrontendJobs(unit_settings(tmp_path))
    configure_openai(jobs, models="claude-opus-5-5, served-b")
    providers.save_provider(tmp_path, {"provider": "openai"})
    catalog = jobs.models()
    assert [x["displayName"] for x in catalog["models"]] == ["Claude Opus 5.5", "served-b"]
    for entry in catalog["models"]:
        assert [x["reasoningEffort"] for x in entry["supportedReasoningEfforts"]] == list(
            openai_compat.EFFORTS
        )
    assert openai_compat.EFFORTS == ("low", "medium", "high", "xhigh", "max")


@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_a_job_may_choose_the_efforts_above_high(tmp_path, monkeypatch, effort):
    jobs = FrontendJobs(unit_settings(tmp_path))
    seed_pending(jobs)
    monkeypatch.setattr(jobs, "_run", lambda _: None)
    configure_openai(jobs)
    providers.save_provider(tmp_path, {"provider": "openai"})
    job = jobs.start_selected(
        [{"course_id": "1", "assignment_id": "2"}],
        ai_confirmed=True,
        model="served-a",
        effort=effort,
    )
    assert (job["provider"], job["effort"]) == ("openai", effort)


LISTED = (
    "Unsupported value: 'reasoning_effort' does not support '{0}' with this model. "
    "Supported values are: 'low', 'medium', and 'high'."
)


@pytest.mark.parametrize(
    "effort, message, lowered",
    [
        ("xhigh", LISTED.format("xhigh"), "high"),
        ("max", LISTED.format("max"), "high"),
        ("max", "reasoning_effort: supported values are 'low', 'high', 'xhigh'", "xhigh"),
        ("xhigh", "Invalid value for reasoning_effort: 'xhigh'.", "high"),  # no list: high
        ("max", "Unsupported reasoning_effort 'max'.", "high"),
        # none below the chosen one: the lowest listed
        ("xhigh", "reasoning_effort: supported values are 'max'", "max"),
    ],
)
def test_a_refused_effort_above_high_steps_down_once(effort, message, lowered):
    mode = {**MODE, "effort": effort}
    assert openai_compat._degrade(refusal(400, message), mode) == (
        "reasoning_effort_lowered",
        lowered,
    )
    # A second refusal drops it, as does a refusal of low, medium or high.
    lowered_mode = {**mode, "effort": lowered, "effort_lowered": True}
    assert openai_compat._degrade(refusal(400, message), lowered_mode) == (
        "reasoning_effort",
        None,
    )


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_a_refusal_of_low_medium_or_high_drops_the_effort_as_before(effort):
    message = LISTED.format(effort).replace("'low', 'medium', and 'high'", "'minimal'")
    mode = {**MODE, "effort": effort}
    assert openai_compat.degradable(refusal(400, message), mode) == "reasoning_effort"


def test_pydantic_effort_lists_are_read():
    body = {
        "detail": [
            {
                "loc": ["body", "reasoning_effort"],
                "msg": "Input should be 'low', 'medium' or 'high'",
            }
        ]
    }
    refused = openai_compat._Refused(422, json.dumps(body).encode(), {})
    assert openai_compat._degrade(refused, {**MODE, "effort": "max"}) == (
        "reasoning_effort_lowered",
        "high",
    )


def test_xhigh_goes_to_high_then_away_and_is_recorded(endpoint):
    refused = {"error": {"message": LISTED.format("xhigh"), "param": "reasoning_effort"}}
    endpoint.replies += [reply_json(refused, 400), reply_stream(chunks(json.dumps(answer())))]
    outcome, _ = turn(endpoint, effort="xhigh")
    first, second = (x["body"] for x in endpoint.requests)
    assert first["reasoning_effort"] == "xhigh" and second["reasoning_effort"] == "high"
    details = outcome["provider"]
    assert details["degradations"] == ["reasoning_effort_lowered"]
    assert details["effort"] == {"requested": "xhigh", "sent": "high"}
    endpoint.requests.clear()
    still = {"error": {"message": LISTED.format("high"), "param": "reasoning_effort"}}
    endpoint.replies += [
        reply_json(refused, 400),
        reply_json(still, 400),
        reply_stream(chunks(json.dumps(answer()))),
    ]
    outcome, _ = turn(endpoint, effort="max")
    assert [x["body"].get("reasoning_effort") for x in endpoint.requests] == ["max", "high", None]
    details = outcome["provider"]
    assert details["degradations"] == ["reasoning_effort_lowered", "reasoning_effort"]
    assert details["effort"] == {"requested": "max", "sent": None}


def test_drafting_takes_the_new_efforts_and_records_them(tmp_path, monkeypatch):
    server = Endpoint()
    try:
        package, result = openai_package(tmp_path, monkeypatch, server)
        refused = {"error": {"message": LISTED.format("max"), "param": "reasoning_effort"}}
        server.replies += [
            reply_json(refused, 400),
            reply_stream(chunks(json.dumps(result, ensure_ascii=False))),
        ]
        receipt = drafting.generate_review(
            package, ai_confirmed=True, model="unit-model", effort="max", provider="openai"
        )
        generation = json.loads(
            (Path(receipt["package"]) / "generation.json").read_text(encoding="utf-8")
        )
        assert generation["reasoning_effort"] == "max"
        assert generation["effort"] == {"requested": "max", "sent": "high"}
        assert generation["degradations"] == ["reasoning_effort_lowered"]
        assert generation["output_limit"] == {"requested": None, "sent": None, "param": None}
        assert KEY not in json.dumps(generation)
    finally:
        server.close()
