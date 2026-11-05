"""The Anthropic-compatible endpoint (a Claude Code relay) and the Claude side of the output limit
and the connection test. Claude Code is a stand-in script here (CI has no CLI); the real CLI is
checked against a local fake Messages server by the staging harness, not by this suite. The model
list check runs against a threaded local server on 127.0.0.1."""

import json
import os
import re
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_frontend import (  # noqa: F401
    FrontendJobs,
    local_server,
    offline_rpc,
    public,
    seed_pending,
    unit_settings,
)
from test_workflow import prepared_fixture

from classroomautowork import api_key, claude_agent, connection_test, drafting, providers, ui_jobs
from classroomautowork.errors import ConfigurationError, RequestFailed, WorkflowError

KEY = "relay-key-" + "r" * 30 + "K9zQ"
CLAUDE_KEY = "sk-ant-api03-" + "c" * 40 + "WXYZ"
ORIGIN = "https://yishuapp.com"
HELPER = json.dumps({"apiKeyHelper": "printenv CLASSROOMAUTOWORK_RELAY_KEY"})

# ---------------------------------------------------------------- a stand-in for Claude Code

FAKE_CLI = r"""
import json, os, pathlib, sys, time
root = pathlib.Path(__file__).parent
argv = sys.argv[1:]
model = argv[argv.index('--model') + 1]
mode = json.loads((root / 'cli-mode.json').read_text()) if (root / 'cli-mode.json').exists() else {}
message = json.loads(sys.stdin.readline())
with (root / 'cli.jsonl').open('a') as f:
    f.write(json.dumps({'argv': argv, 'env_names': sorted(os.environ),
        'ANTHROPIC_BASE_URL': os.environ.get('ANTHROPIC_BASE_URL'),
        'CLAUDE_CODE_MAX_OUTPUT_TOKENS': os.environ.get('CLAUDE_CODE_MAX_OUTPUT_TOKENS'),
        'CLAUDE_CODE_MAX_RETRIES': os.environ.get('CLAUDE_CODE_MAX_RETRIES'),
        'CLAUDE_CODE_DISABLE_LEGACY_MODEL_REMAP': os.environ.get('CLAUDE_CODE_DISABLE_LEGACY_MODEL_REMAP'),
        'pid': os.getpid(),
        'content': [b.get('type') for b in message['message']['content']]}) + '\n')
def send(x): print(json.dumps(x), flush=True)
helper = '--settings' in argv and 'apiKeyHelper' in json.loads(argv[argv.index('--settings') + 1])
source = ('apiKeyHelper' if helper and os.environ.get('CLASSROOMAUTOWORK_RELAY_KEY')
          else 'ANTHROPIC_API_KEY' if os.environ.get('ANTHROPIC_API_KEY') else 'none')
kind = mode.get('mode', 'ok')
send({'type': 'system', 'subtype': 'init', 'model': mode.get('as', model) if kind == 'remap' else model,
      'session_id': 's-1', 'apiKeySource': source, 'tools': ['StructuredOutput'], 'mcp_servers': []})
limit = int(os.environ.get('CLAUDE_CODE_MAX_OUTPUT_TOKENS') or 128000)
def fail(text, status=None):
    send({'type': 'result', 'subtype': 'success', 'is_error': True, 'result': text, 'api_error_status': status})
if kind == 'retry401':
    send({'type': 'system', 'subtype': 'api_retry', 'attempt': 1, 'max_retries': 10, 'error_status': 401,
          'error': 'authentication_failed'})
    if os.environ.get('CLAUDE_CODE_MAX_RETRIES'):  # few retries: the result, with the relay's words
        fail('Failed to authenticate. API Error: 401 invalid x-api-key', 401)
    time.sleep(30)
elif kind == 'hang':
    time.sleep(30)
elif kind == 'retry503':
    for attempt in range(1, 30):
        send({'type': 'system', 'subtype': 'api_retry', 'attempt': attempt, 'max_retries': 10,
              'error_status': 503, 'error': 'server_error'})
        time.sleep(0.3)
elif kind == 'cap_then_hang' and limit > mode['cap']:
    time.sleep(mode.get('sleep', 0))
    fail(f"API Error: 400 max_tokens: {limit} > {mode['cap']}, which is the maximum allowed number of output tokens for {model}", 400)
elif kind == 'cap_then_hang':
    time.sleep(30)
elif kind == 'no_channel':
    fail(f'API Error: 503 当前分组 default 下对于模型 {model} 无可用渠道 (request id: 20261105000001)', 503)
elif kind == 'unauthorized':
    fail('Failed to authenticate. API Error: 401 该令牌额度已用尽 (request id: 20261105000002)', 401)
elif kind == 'malformed':
    fail('API Error: API returned an empty or malformed response (HTTP 200) — check for a proxy or gateway '
         'intercepting the request. Response: content-type html, body is an HTML page')
elif kind == 'cap' and limit > mode['cap']:
    fail(f"API Error: 400 max_tokens: {limit} > {mode['cap']}, which is the maximum allowed number of output tokens for {model}", 400)
elif kind == 'cap_always':
    fail(f"API Error: 400 max_tokens: {limit} > {limit // 2}, which is the maximum allowed number of output tokens for {model}", 400)
elif kind == 'cap_wrong':
    fail(f"API Error: 400 max_tokens: {limit} > {limit + 1000}, which is the maximum allowed number of output tokens for {model}", 400)
elif kind == 'cap_unreadable':
    fail('API Error: 400 max_tokens is too large for this endpoint', 400)
elif kind == 'truncated':
    fail(f"API Error: Claude's response exceeded the {limit if limit != 128000 else 32000} output token maximum. "
         'To configure this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable.')
elif kind == 'model404':
    fail(f"There's an issue with the selected model ({model}). It may not exist or you may not have access to it.", 404)
elif kind == 'echo_key':
    fail('upstream said: invalid key ' + os.environ.get('CLASSROOMAUTOWORK_RELAY_KEY', ''), 500)
else:
    if kind == 'images':
        send({'type': 'assistant', 'error': 'invalid_request', 'message': {'model': '<synthetic>', 'content': [{'type': 'text',
              'text': 'API Error: an image in the conversation could not be processed and was removed.'}]}})
    if kind == 'stream_slow':
        # --include-partial-messages: the reply's events while it streams, a gap apart.
        for index in range(mode['count']):
            time.sleep(mode['gap'])
            send({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'index': 0,
                  'delta': {'type': 'input_json_delta', 'partial_json': '{'}}})
    if kind == 'served':
        send({'type': 'assistant', 'error': 'invalid_request', 'message': {'model': '<synthetic>', 'content': [
              {'type': 'text', 'text': 'API Error: something Claude Code says itself'}]}})
        send({'type': 'assistant', 'message': {'model': mode['served'], 'content': [{'type': 'tool_use',
              'name': 'StructuredOutput'}]}})
    result = root / 'fixture-result.json'
    output = json.loads(result.read_text()) if result.exists() else {'ok': True, 'reply': '连接正常'}
    send({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'StructuredOutput'}]}})
    send({'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': output,
          'usage': {'input_tokens': 120, 'output_tokens': 18}, 'total_cost_usd': 0.0042, 'duration_ms': 800})
"""


def fake_cli(root: Path, monkeypatch, mode=None) -> Path:
    script = root / "fake_claude_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    if mode is not None:
        (root / "cli-mode.json").write_text(json.dumps(mode), encoding="utf-8")
    monkeypatch.setattr(claude_agent, "claude_command", lambda: [sys.executable, str(script)])
    return root / "cli.jsonl"


def runs(log: Path) -> list[dict]:
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


# ---------------------------------------------------------------- a stand-in for the model list


class ModelList:
    """GET /v1/models on 127.0.0.1: answers with `reply` (status, body) and records the headers."""

    def __init__(self, status=200, body=None):
        self.status, self.requests = status, []
        self.body = body if body is not None else {"data": [{"type": "model", "id": "claude-x"}]}
        lister = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                lister.requests.append({"path": self.path, "headers": dict(self.headers)})
                keyed = self.headers.get("x-api-key") or self.headers.get("authorization")
                status = lister.status if keyed else 401
                body = lister.body
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def lister():
    server = ModelList()
    yield server
    server.close()


def listed(ids=("claude-sonnet-5-5",), status=200):
    return lambda *_: {"listed": True, "ids": list(ids), "status": status, "key_checked": True}


def save_compat(data_dir, base="https://relay.example.com", models="claude-sonnet-5-5", **extra):
    return providers.save_compat(
        data_dir,
        {"base_url": base, "api_key": KEY, "models": models, **extra},
        verify=listed(),
    )


# ---------------------------------------------------------------- settings


@pytest.mark.parametrize(
    "value, expected",
    [
        ("https://relay.example.com", "https://relay.example.com"),
        ("  https://Relay.Example.com/  ", "https://relay.example.com"),
        ("https://relay.example.com/v1/messages", "https://relay.example.com"),
        ("https://relay.example.com/v1/messages/", "https://relay.example.com"),
        ("https://relay.example.com/api/v1", "https://relay.example.com/api"),
        ("https://relay.example.com/claude/messages", "https://relay.example.com/claude"),
        ("http://127.0.0.1:8080/v1/", "http://127.0.0.1:8080"),
    ],
)
def test_the_address_is_what_anthropic_base_url_takes(value, expected):
    assert providers.normalize_base_url(value, "anthropic") == expected
    # The OpenAI-compatible address is read as before: nothing but /chat/completions is cut.
    assert providers.normalize_base_url("https://relay.example.com/v1") == (
        "https://relay.example.com/v1"
    )


@pytest.mark.parametrize(
    "value, words",
    [
        ("", "例如 https://api.example.com。"),
        ("ftp://relay.example.com", "http:// 或 https://"),
        ("https://user:pw@relay.example.com", "用户名或密码"),
        ("https://relay.example.com/v1?beta=true", "ANTHROPIC_BASE_URL"),
        ("http://2130706433", "IP 地址无效"),
        ("https://evil.example\\.relay.example.com", "主机名无效"),
    ],
)
def test_bad_addresses_are_refused_with_the_same_checks(value, words):
    with pytest.raises(ConfigurationError, match=re.escape(words)):
        providers.normalize_base_url(value, "anthropic")


def test_compat_settings_are_private_verified_and_never_returned(tmp_path):
    calls = []

    def verify(base, key):
        calls.append((base, key))
        return {
            "listed": True,
            "ids": ["claude-sonnet-5-5", "x"],
            "status": 200,
            "key_checked": True,
        }

    view = providers.save_compat(
        tmp_path,
        {
            "base_url": "https://relay.example.com/v1/messages",
            "api_key": f"  {KEY} ",
            "models": "claude-opus-5-5, relay-custom",
            "max_output_tokens": " 32000 ",
        },
        verify=verify,
    )
    assert calls == [("https://relay.example.com", KEY)]
    path = tmp_path / providers.COMPAT_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    record = json.loads(path.read_text())
    assert set(record) == {
        "base_url", "api_key", "models", "max_output_tokens", "models_listed", "listed_models",
        "listed_models_count", "unlisted_models", "key_checked", "updated_at", "list_shape",
    }  # fmt: skip
    assert record["max_output_tokens"] == 32000 and record["unlisted_models"] == [
        "claude-opus-5-5",
        "relay-custom",
    ]
    status = view["anthropic_compatible"]
    assert status["configured"] and status["host"] == "relay.example.com"
    assert status["last4"] == "K9zQ" and status["max_output_tokens"] == 32000
    assert view["anthropic_compatible_suggestions"] == ["claude-sonnet-5-5", "x"]
    assert view["active"] == "anthropic"  # saving does not switch
    for data in (view, providers.status(tmp_path), providers.settings_view(tmp_path)):
        assert KEY not in json.dumps(data) and KEY[:-4] not in json.dumps(data)
    # The OpenAI-compatible endpoint is a separate file and stays unset.
    assert not providers.status(tmp_path)["openai"]["configured"]


def test_any_printable_key_and_reuse_only_for_the_same_origin(tmp_path):
    with pytest.raises(ConfigurationError, match="请填写接口的密钥"):
        providers.save_compat(
            tmp_path, {"base_url": "https://relay.example.com", "models": "m"}, verify=listed()
        )
    for bad in ("has space", "x" * 1001, "中文"):
        with pytest.raises(ConfigurationError, match="密钥只能"):
            providers.save_compat(
                tmp_path,
                {"base_url": "https://relay.example.com", "api_key": bad, "models": "m"},
                verify=lambda *_: pytest.fail("must not be checked"),
            )
    save_compat(tmp_path)
    seen = []
    providers.save_compat(
        tmp_path,
        {"base_url": "https://relay.example.com/v1", "models": "claude-haiku-5-5"},
        verify=lambda base, key: seen.append(key) or listed()(),
    )
    assert seen == [KEY]
    with pytest.raises(ConfigurationError, match="换了接口地址"):
        providers.save_compat(
            tmp_path, {"base_url": "https://other.example.com", "models": "m"}, verify=listed()
        )


@pytest.mark.parametrize("value", ["16k", "16,000", 999, 1_000_001, True, 1.5, [1000]])
def test_a_wrong_limit_is_refused_before_any_check(tmp_path, value):
    with pytest.raises(ConfigurationError, match="1000 到 1000000"):
        providers.save_compat(
            tmp_path,
            {"base_url": "https://r.example.com", "api_key": KEY, "models": "m",
             "max_output_tokens": value},
            verify=lambda *_: pytest.fail("must not be checked"),
        )  # fmt: skip
    assert not (tmp_path / providers.COMPAT_FILE).exists()


def test_the_model_list_check_sends_both_auth_headers_and_reads_both_shapes(lister):
    result = providers.check_anthropic_endpoint(lister.base, KEY)
    first, second = lister.requests
    assert first["path"] == "/v1/models"
    headers = {k.lower(): v for k, v in first["headers"].items()}
    assert headers["x-api-key"] == KEY and headers["authorization"] == "Bearer " + KEY
    assert headers["anthropic-version"] == "2023-06-01"
    # The same list without the key tells whether the endpoint checks keys at all.
    plain = {k.lower() for k in second["headers"]}
    assert "x-api-key" not in plain and "authorization" not in plain
    assert result == {
        "listed": True,
        "ids": ["claude-x"],
        "status": 200,
        "key_checked": True,
        "shape": "anthropic",
    }
    # The check names itself as an Anthropic-format client, not as an OpenAI one.
    assert headers["user-agent"] == "classroomautowork (Anthropic-compatible client)"
    lister.body = {"object": "list", "data": [{"id": "openai-shape"}]}
    result = providers.check_anthropic_endpoint(lister.base, KEY)
    assert result["ids"] == ["openai-shape"] and result["shape"] == "openai"


@pytest.mark.parametrize(
    "status, body, outcome",
    [
        (404, {"error": "no"}, "saved"),
        (405, {"error": "no"}, "saved"),
        (401, {"error": {"message": "invalid x-api-key"}}, "接口拒绝了这个密钥"),
        (403, {"error": {"message": "forbidden"}}, "接口拒绝了这个密钥"),
        (502, {"error": {"message": "bad gateway"}}, "验证时接口出错"),
        (200, b"<html>not json</html>", "可能不是 Anthropic 兼容接口的地址"),
    ],
)
def test_the_save_check_answers_like_the_openai_one(tmp_path, status, body, outcome):
    server = ModelList(status, body)
    try:
        values = {"base_url": server.base + "/v1", "api_key": KEY, "models": "m"}
        if outcome == "saved":
            view = providers.save_compat(tmp_path, values)
            assert view["anthropic_compatible"]["models_listed"] is False
            assert view["check"]["models_listed"] is False
        else:
            with pytest.raises(WorkflowError, match=outcome):
                providers.save_compat(tmp_path, values)
            assert not (tmp_path / providers.COMPAT_FILE).exists()
    finally:
        server.close()


def test_switching_and_deleting_keep_the_other_endpoint(tmp_path):
    with pytest.raises(ConfigurationError, match="还没有保存 Anthropic 兼容接口"):
        providers.save_provider(tmp_path, {"provider": "anthropic_compatible"})
    save_compat(tmp_path)
    assert providers.save_provider(tmp_path, {"provider": "anthropic_compatible"})["active"] == (
        "anthropic_compatible"
    )
    # Deleting the OpenAI-compatible endpoint (never saved) does not move answers off this one.
    providers.save_openai(tmp_path, {"clear": True})
    assert providers.active_provider(tmp_path) == "anthropic_compatible"
    providers.record_test(tmp_path, "anthropic_compatible", {"at": "t", "ok": True})
    view = providers.save_compat(tmp_path, {"clear": True})
    assert view["active"] == "anthropic" and not view["anthropic_compatible"]["configured"]
    assert "anthropic_compatible" not in providers.last_tests(tmp_path)
    with pytest.raises(ConfigurationError, match="还没有设置 Anthropic 兼容接口"):
        providers.compat_catalog(tmp_path)


def test_a_damaged_compat_file_reads_as_none_and_says_so(tmp_path):
    (tmp_path / providers.COMPAT_FILE).write_text("{not json", encoding="utf-8")
    status = providers.status(tmp_path)["anthropic_compatible"]
    assert not status["configured"] and status["damaged"]
    with pytest.raises(ConfigurationError, match="保存的 Anthropic 兼容接口设置已损坏"):
        providers.load_compat(tmp_path)


def test_compat_routes_take_the_write_guard_and_hide_the_key(local_server, monkeypatch):  # noqa: F811
    monkeypatch.setattr(providers, "check_anthropic_endpoint", listed())
    path = "/classroom/api/settings/anthropic-compatible"
    body = {"base_url": "https://relay.example.com", "api_key": KEY, "models": "claude-sonnet-5-5"}
    assert public(local_server, path, method="PUT", body=body)[0] == 403
    assert (
        public(local_server, path, method="PUT", body=body, origin="https://evil.example")[0] == 403
    )
    assert public(local_server, path, method="PUT", body={**body, "x": 1}, origin=ORIGIN)[0] == 400
    status, _, saved = public(local_server, path, method="PUT", body=body, origin=ORIGIN)
    assert (
        status == 200 and json.loads(saved)["anthropic_compatible"]["host"] == "relay.example.com"
    )
    switch = "/classroom/api/settings/provider"
    status, _, switched = public(
        local_server, switch, method="PUT", body={"provider": "anthropic_compatible"}, origin=ORIGIN
    )
    assert status == 200 and json.loads(switched)["active"] == "anthropic_compatible"
    status, _, view = public(local_server, path)
    assert status == 200 and json.loads(view)["anthropic_compatible_suggestions"] == [
        "claude-sonnet-5-5"
    ]
    _, _, boot = public(local_server, "/classroom/api/bootstrap")
    _, _, models = public(local_server, "/classroom/api/models")
    catalog = json.loads(models)
    assert catalog["provider"] == "anthropic_compatible" and catalog["default_effort"] == "high"
    assert catalog["models"][0]["displayName"] == "Claude Sonnet 5.5"
    for data in (saved, switched, view, boot, models):
        assert KEY.encode() not in data and KEY[:-4].encode() not in data
    status, _, cleared = public(
        local_server, path, method="PUT", body={"clear": True}, origin=ORIGIN
    )
    assert status == 200 and json.loads(cleared)["active"] == "anthropic"


# ---------------------------------------------------------------- the Claude API's own output limit


def test_the_claude_limit_is_kept_apart_from_the_key_file(tmp_path):
    checked = []
    status = providers.save_claude(tmp_path, {"key": CLAUDE_KEY}, verify=checked.append)
    assert checked == [CLAUDE_KEY] and status["max_output_tokens"] is None
    key_file = (tmp_path / api_key.KEY_FILE).read_text()
    providers.record_test(tmp_path, "anthropic", {"at": "t", "ok": True})
    status = providers.save_claude(tmp_path, {"max_output_tokens": " 20000 "})
    assert status["max_output_tokens"] == 20000 and status["last4"] == "WXYZ"
    assert "anthropic" not in providers.last_tests(
        tmp_path
    )  # new settings, the old test says nothing
    assert (tmp_path / api_key.KEY_FILE).read_text() == key_file  # the key file's format stays
    path = tmp_path / providers.CLAUDE_SETTINGS_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["max_output_tokens"] == 20000
    assert providers.claude_status(tmp_path) == {
        **api_key.key_status(tmp_path),
        "max_output_tokens": 20000,
    }
    for value in ("16k", 999, True, {"n": 1}):
        with pytest.raises(ConfigurationError, match="1000 到 1000000"):
            providers.save_claude(tmp_path, {"key": "sk-ant-x", "max_output_tokens": value})
    assert providers.claude_limit(tmp_path) == 20000
    # A refused key leaves the limit as it was.

    def refuse(_):
        raise ConfigurationError("Anthropic 拒绝了这个 API 密钥，未保存。")

    with pytest.raises(ConfigurationError, match="拒绝"):
        providers.save_claude(
            tmp_path, {"key": CLAUDE_KEY[:-4] + "AAAA", "max_output_tokens": 30000}, verify=refuse
        )
    assert providers.claude_limit(tmp_path) == 20000
    assert providers.save_claude(tmp_path, {"max_output_tokens": ""})["max_output_tokens"] is None
    assert not path.exists()
    path.write_text('{"max_output_tokens": "lots"}')
    assert providers.claude_limit(tmp_path) is None  # a wrong value reads as none
    for bad in ({}, {"key": CLAUDE_KEY, "x": 1}, []):
        with pytest.raises(ConfigurationError, match="只接受 key 和 max_output_tokens"):
            providers.save_claude(tmp_path, bad)


def test_the_claude_route_takes_the_limit_and_bootstrap_shows_it(local_server, monkeypatch):  # noqa: F811
    monkeypatch.setattr(api_key, "check_key", lambda _: None)
    path = "/classroom/api/settings/api-key"
    assert public(local_server, path, method="PUT", body={"max_output_tokens": 20000})[0] == 403
    status, _, body = public(
        local_server, path, method="PUT", body={"key": CLAUDE_KEY, "max_output_tokens": "20000"},
        origin=ORIGIN,
    )  # fmt: skip
    assert status == 200 and json.loads(body)["max_output_tokens"] == 20000
    status, _, body = public(
        local_server, path, method="PUT", body={"max_output_tokens": "16k"}, origin=ORIGIN
    )
    assert status == 400 and "1000 到 1000000" in json.loads(body)["error"]
    _, _, boot = public(local_server, "/classroom/api/bootstrap")
    boot = json.loads(boot)
    assert boot["provider"]["anthropic"]["max_output_tokens"] == 20000
    assert boot["provider"]["anthropic"] == boot["api_key"]
    assert CLAUDE_KEY not in json.dumps(boot)


# ---------------------------------------------------------------- the CLI's arguments and environment

SCHEMA = {"type": "object"}


def test_the_claude_api_turn_is_sent_exactly_as_before(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://kept.example")
    argv = claude_agent.command_line(["claude"], model="m", effort="high", schema=SCHEMA)
    assert argv == [
        "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
        "--verbose", "--model", "m", "--tools", "", "--disallowedTools", "mcp__*",
        "--no-session-persistence", "--bare", "--json-schema", '{"type": "object"}',
        "--effort", "high",
    ]  # fmt: skip
    assert claude_agent.turn_environment(None) is None
    assert claude_agent.turn_environment(CLAUDE_KEY) == {
        **os.environ,
        "ANTHROPIC_API_KEY": CLAUDE_KEY,
    }
    limited = claude_agent.turn_environment(CLAUDE_KEY, max_output_tokens=20000)
    assert limited == {
        **os.environ,
        "ANTHROPIC_API_KEY": CLAUDE_KEY,
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "20000",
    }


def test_the_endpoint_turn_gets_its_address_and_a_helper_never_the_key_in_argv(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.setenv(name, "inherited-" + name)
    argv = claude_agent.command_line(["claude"], model="m", effort=None, schema=SCHEMA, helper=True)
    # The helper names the key's variable, never the key; partial messages keep a slow but
    # streaming endpoint from looking silent to the idle limit.
    assert argv[-3:] == ["--settings", HELPER, "--include-partial-messages"]
    assert KEY not in json.dumps(argv)
    env = claude_agent.turn_environment(
        KEY, base_url="https://relay.example.com", auth="helper", max_output_tokens=16000
    )
    assert env["ANTHROPIC_BASE_URL"] == "https://relay.example.com"
    assert env["CLASSROOMAUTOWORK_RELAY_KEY"] == KEY
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16000"
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["CLAUDE_CODE_DISABLE_LEGACY_MODEL_REMAP"] == "1"
    assert "CLAUDE_CODE_MAX_RETRIES" not in env  # an answer keeps Claude Code's own retries
    own = ("CLASSROOMAUTOWORK_RELAY_KEY", "CLAUDE_CODE_DISABLE_LEGACY_MODEL_REMAP")
    assert {k: v for k, v in env.items() if not claude_agent._inherited_setting(k)
            and k not in own} == {
        k: v for k, v in os.environ.items() if not claude_agent._inherited_setting(k)
    }  # fmt: skip
    with pytest.raises(WorkflowError, match="设置不完整"):
        claude_agent.run_turn([], SCHEMA, model="m", effort=None, cwd=Path("."), auth="helper")


def relay_turn(tmp_path, **options):
    return claude_agent.run_turn(
        [{"type": "text", "text": "unit"}],
        SCHEMA,
        model="claude-sonnet-5-5",
        effort="low",
        cwd=tmp_path,
        api_key=KEY,
        base_url="https://relay.example.com",
        auth="helper",
        host="relay.example.com",
        **options,
    )


def test_an_endpoint_turn_runs_through_the_helper(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-inherited-should-not-pass")
    log = fake_cli(tmp_path, monkeypatch)
    outcome = relay_turn(tmp_path)
    (run,) = runs(log)
    assert outcome["output"] == {"ok": True, "reply": "连接正常"}
    assert outcome["api_key_source"] == "apiKeyHelper" and outcome["cost_usd"] == 0.0042
    assert outcome["output_limit"] is None and outcome["degradations"] == []
    assert run["argv"][-3:] == ["--settings", HELPER, "--include-partial-messages"]
    assert KEY not in json.dumps(run["argv"])
    assert run["ANTHROPIC_BASE_URL"] == "https://relay.example.com"
    assert "CLASSROOMAUTOWORK_RELAY_KEY" in run["env_names"]
    assert "ANTHROPIC_API_KEY" not in run["env_names"]
    assert run["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] is None


def test_a_refused_limit_is_sent_once_more_at_the_stated_maximum(tmp_path, monkeypatch):
    log = fake_cli(tmp_path, monkeypatch, {"mode": "cap", "cap": 32000})
    labels = []
    outcome = relay_turn(tmp_path, max_output_tokens=64000, on_activity=labels.append)
    assert [x["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] for x in runs(log)] == ["64000", "32000"]
    assert outcome["degradations"] == ["max_tokens_clamp"]
    assert outcome["output_limit"] == {
        "requested": 64000,
        "sent": 32000,
        "param": "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    }
    assert any("32000" in x for x in labels)
    # With no limit set, Claude Code's own default is what the endpoint refuses.
    (tmp_path / "cli.jsonl").unlink()
    outcome = relay_turn(tmp_path)
    assert [x["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] for x in runs(log)] == [None, "32000"]
    assert outcome["output_limit"]["requested"] is None and outcome["output_limit"]["sent"] == 32000


@pytest.mark.parametrize(
    "mode, words, count",
    [
        # Refused again at the maximum it named: the step runs once only.
        ("cap_always", "不接受 32000 tokens 的输出上限，最多 16000", 2),
        # A "maximum" that is not below what was sent cannot help.
        ("cap_wrong", "不接受 64000 tokens 的输出上限，最多 65000", 1),
        ("cap_unreadable", "回合未成功", 1),
    ],
)
def test_a_limit_refusal_without_a_usable_maximum_is_not_retried(
    tmp_path, monkeypatch, mode, words, count
):
    log = fake_cli(tmp_path, monkeypatch, {"mode": mode})
    with pytest.raises(RequestFailed if mode != "cap_unreadable" else WorkflowError, match=words):
        relay_turn(tmp_path, max_output_tokens=64000)
    assert len(runs(log)) == count


def test_a_refused_key_stops_at_the_first_retry(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, {"mode": "retry401"})
    started = time.monotonic()
    with pytest.raises(RequestFailed, match=r"Anthropic 兼容接口（relay\.example\.com）拒绝了密钥"):
        relay_turn(tmp_path)
    assert time.monotonic() - started < 15


def test_errors_name_the_endpoint_show_the_cli_words_and_never_the_key(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, {"mode": "model404"})
    with pytest.raises(RequestFailed) as caught:
        relay_turn(tmp_path)
    main, detail = str(caught.value).split("\n")
    assert "Anthropic 兼容接口（relay.example.com）找不到模型 claude-sonnet-5-5" in main
    assert detail.startswith("There's an issue with the selected model")
    fake_cli(tmp_path, monkeypatch, {"mode": "echo_key"})
    with pytest.raises(RequestFailed, match="出错（HTTP 500）") as caught:
        relay_turn(tmp_path)
    assert KEY not in str(caught.value) and "upstream said" in str(caught.value)


def test_an_answer_cut_at_the_limit_says_what_to_do_and_stays_repairable(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, {"mode": "truncated"})
    with pytest.raises(WorkflowError) as caught:
        relay_turn(tmp_path, max_output_tokens=16000)
    assert not isinstance(caught.value, RequestFailed)
    assert str(caught.value).startswith(
        "模型的输出达到了输出上限（16000 tokens），结果被截断（stop_reason: max_tokens）。"
        "可以在设置 → 模型接口里调高「输出上限」后重试（推理用的 token 也算在内）；资料已保留。\n"
        "API Error: Claude's response exceeded"
    )
    with pytest.raises(WorkflowError, match="Claude Code 默认的输出上限（32000 tokens）"):
        relay_turn(tmp_path)


def test_an_image_claude_code_had_to_drop_is_said(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, {"mode": "images"})
    assert relay_turn(tmp_path)["degradations"] == ["images"]


# ---------------------------------------------------------------- the catalog and jobs


def test_the_catalog_and_a_job_follow_the_endpoint(tmp_path, monkeypatch):
    jobs = FrontendJobs(unit_settings(tmp_path))
    seed_pending(jobs)
    monkeypatch.setattr(jobs, "_run", lambda _: None)
    save_compat(tmp_path, models="claude-opus-5-5, relay-custom")
    providers.save_provider(tmp_path, {"provider": "anthropic_compatible"})
    catalog = jobs.models()
    assert (
        catalog["provider"] == "anthropic_compatible"
        and catalog["default_model"] == "claude-opus-5-5"
    )
    assert [x["displayName"] for x in catalog["models"]] == ["Claude Opus 5.5", "relay-custom"]
    assert [
        x["reasoningEffort"] for x in catalog["models"][0]["supportedReasoningEfforts"]
    ] == list(claude_agent.EFFORTS)
    assert catalog["source"] == "Anthropic 兼容接口 relay.example.com"
    selection = [{"course_id": "1", "assignment_id": "2"}]
    with pytest.raises(WorkflowError, match="不在 Anthropic 兼容接口设置的模型里"):
        jobs.start_selected(selection, ai_confirmed=True, model="claude-haiku-5-5")
    with pytest.raises(WorkflowError, match="推理强度"):
        jobs.start_selected(selection, ai_confirmed=True, model="relay-custom", effort="minimal")
    job = jobs.start_selected(selection, ai_confirmed=True)
    assert (job["provider"], job["model"], job["effort"]) == (
        "anthropic_compatible",
        "claude-opus-5-5",
        "high",
    )
    jobs._jobs[job["id"]]["status"] = "failed"
    jobs._active = None
    providers.save_provider(tmp_path, {"provider": "anthropic"})
    assert jobs.retry(job["id"])["provider"] == "anthropic_compatible"  # the job keeps its provider
    jobs._active = None
    providers.save_compat(tmp_path, {"clear": True})
    with pytest.raises(WorkflowError, match="这个任务用的是 Anthropic 兼容接口，它的设置已删除"):
        jobs.retry(job["id"])


# ---------------------------------------------------------------- drafting through the endpoint


def test_an_answer_through_the_endpoint_is_recorded_without_the_key(tmp_path, monkeypatch):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)  # the skill and fixture-result.json
    log = fake_cli(tmp_path, monkeypatch)
    save_compat(tmp_path, models="claude-sonnet-5-5", max_output_tokens=24000)
    messages = []
    receipt = drafting.generate_review(
        package,
        ai_confirmed=True,
        provider="anthropic_compatible",
        progress=lambda x: messages.append(str(getattr(x, "message", x))),
    )
    generation = receipt["generation"]
    assert generation["provider"] == "anthropic_compatible"
    assert generation["host"] == "relay.example.com"
    assert (generation["requested_model"], generation["model"]) == ("claude-sonnet-5-5",) * 2
    assert (
        generation["reasoning_effort"] == "high" and generation["api_key_source"] == "apiKeyHelper"
    )
    assert generation["token_usage"] == {"input_tokens": 120, "output_tokens": 18}
    assert generation["cost_usd"] == 0.0042
    assert generation["output_limit"] == {
        "requested": 24000,
        "sent": 24000,
        "param": "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    }
    (run,) = runs(log)
    assert run["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "24000" and KEY not in json.dumps(run["argv"])
    assert any(
        "已通过 Claude Code 向 Anthropic 兼容接口（relay.example.com）发起请求" in x
        for x in messages
    )
    # The Claude prompt itself is unchanged: no line naming another model.
    prompt = json.dumps(run["content"])
    assert "本次作答的模型是" not in prompt
    for path in Path(receipt["package"]).iterdir():
        assert KEY not in path.read_text(errors="replace")
    # Reuse is kept apart from the Claude API's results: the same answer through the API is a new turn.
    identity = Path(receipt["package"]).name
    assert drafting.record_provider(generation) == "anthropic_compatible"
    assert drafting.same_provider(generation, "anthropic_compatible", "relay.example.com")
    assert not drafting.same_provider(generation, "anthropic_compatible", "other.example.com")
    assert not drafting.same_provider(generation, "anthropic", None)
    api_key.save_key(tmp_path, CLAUDE_KEY, verify=lambda _: None)
    claude = drafting.generate_review(
        package, ai_confirmed=True, model="claude-sonnet-5-5", effort="high"
    )
    assert Path(claude["package"]).name != identity
    assert claude["generation"]["provider"] == "anthropic"
    assert "output_limit" not in claude["generation"] and "degradations" not in claude["generation"]
    official = runs(log)[-1]
    assert "--settings" not in official["argv"] and official["ANTHROPIC_BASE_URL"] is None
    assert official["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] is None


def test_a_clamped_answer_is_recorded_and_the_claude_limit_reaches_the_cli(tmp_path, monkeypatch):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    log = fake_cli(tmp_path, monkeypatch, {"mode": "cap", "cap": 32000})
    save_compat(tmp_path)
    receipt = drafting.generate_review(package, ai_confirmed=True, provider="anthropic_compatible")
    generation = receipt["generation"]
    assert generation["degradations"] == ["max_tokens_clamp"]
    assert generation["output_limit"]["sent"] == 32000
    api_key.save_key(tmp_path, CLAUDE_KEY, verify=lambda _: None)
    providers.save_claude(tmp_path, {"max_output_tokens": 20000})
    receipt = drafting.generate_review(
        package, ai_confirmed=True, model="claude-haiku-5-5", effort="low"
    )
    assert runs(log)[-1]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "20000"
    assert receipt["generation"]["output_limit"] == {
        "requested": 20000,
        "sent": 20000,
        "param": "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    }


def test_a_job_on_the_endpoint_whose_settings_are_gone_stops_before_the_cli(tmp_path, monkeypatch):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    monkeypatch.setattr(claude_agent, "claude_command", lambda: pytest.fail("no turn"))
    with pytest.raises(ConfigurationError, match="Anthropic 兼容接口的设置已经删除"):
        drafting.generate_review(package, ai_confirmed=True, provider="anthropic_compatible")


# ---------------------------------------------------------------- the connection test


@pytest.mark.parametrize("provider", ["anthropic", "anthropic_compatible"])
def test_the_claude_providers_are_tested_through_the_route(local_server, monkeypatch, provider):  # noqa: F811
    data_dir = local_server.jobs.settings.data_dir
    log = fake_cli(data_dir, monkeypatch)
    if provider == "anthropic":
        api_key.save_key(data_dir, CLAUDE_KEY, verify=lambda _: None)
        providers.save_claude(data_dir, {"max_output_tokens": 20000})
        secret = CLAUDE_KEY
    else:
        save_compat(data_dir, max_output_tokens=16000)
        secret = KEY
    status, _, raw = public(
        local_server, "/classroom/api/settings/test", method="POST", body={"provider": provider},
        origin=ORIGIN,
    )  # fmt: skip
    result = json.loads(raw)
    assert status == 200 and result["ok"] is True, result
    assert result["provider"] == provider and result["requested_model"] == ("claude-sonnet-5-5")
    assert result["host"] == (
        "api.anthropic.com" if provider == "anthropic" else "relay.example.com"
    )
    assert result["structured_output_mode"] == "tool" and result["images"] == "sent"
    assert result["effort"] == {"requested": "low", "sent": "low"}
    assert result["usage"] == {"input_tokens": 120, "output_tokens": 18}
    assert result["cost_usd"] == 0.0042 and result["duration_ms"] == 800
    assert result["output_limit"]["sent"] == (20000 if provider == "anthropic" else 16000)
    assert secret.encode() not in raw
    (run,) = runs(log)
    assert run["argv"][run["argv"].index("--effort") + 1] == "low"
    assert run["content"] == ["text", "text", "image"]
    assert ("--settings" in run["argv"]) == (provider == "anthropic_compatible")
    kept = providers.last_tests(data_dir)[provider]
    assert kept["ok"] is True and kept["summary"].startswith("正常")
    tests = (data_dir / providers.TESTS_FILE).read_text()
    assert (
        secret not in tests
        and stat.S_IMODE((data_dir / providers.TESTS_FILE).stat().st_mode) == 0o600
    )


def test_a_failed_claude_test_says_why_and_a_test_has_its_own_time_limits(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, {"mode": "images"})
    save_compat(tmp_path)
    result = connection_test.run(tmp_path, {"provider": "anthropic_compatible"})
    assert result["ok"] and result["images"] == "refused" and result["degradations"] == ["images"]
    assert any("接口不接受图片" in x for x in result["notes"])
    fake_cli(tmp_path, monkeypatch, {"mode": "model404"})
    result = connection_test.run(
        tmp_path, {"provider": "anthropic_compatible", "model": "claude-sonnet-5-5"}
    )
    assert not result["ok"] and "找不到模型" in result["error"]
    assert providers.last_tests(tmp_path)["anthropic_compatible"]["ok"] is False
    with pytest.raises(ConfigurationError, match="不在 Anthropic 兼容接口设置的模型里"):
        connection_test.run(tmp_path, {"provider": "anthropic_compatible", "model": "other"})
    seen = {}

    def turn(*_args, **options):
        seen.update(options)
        return {"output": {}, "model": options["model"], "usage": None, "cost_usd": None,
                "duration_ms": 1, "degradations": [], "output_limit": None}  # fmt: skip

    monkeypatch.setattr(claude_agent, "run_turn", turn)
    api_key.save_key(tmp_path, CLAUDE_KEY, verify=lambda _: None)
    connection_test.run(tmp_path, {"provider": "anthropic"})
    assert (seen["turn_seconds"], seen["idle_seconds"], seen["effort"]) == (120, 60, "low")
    assert "max_output_tokens" not in seen and "auth" not in seen


def test_the_settings_dialog_has_three_providers_and_a_test_per_group():
    web = Path(ui_jobs.__file__).parent / "web"
    page = (web / "index.html").read_text(encoding="utf-8")
    script = (web / "app.js").read_text(encoding="utf-8")
    for provider in ("anthropic", "anthropic_compatible", "openai"):
        assert f'data-provider="{provider}"' in page
        assert (
            f'data-test-provider="{provider}"' in page and f'data-test-result="{provider}"' in page
        )
    for name in (
        "compatSettings", "compatForm", "compatStatus", "compatBaseUrl", "compatKey",
        "compatKeyReuse", "compatModels", "compatModelList", "compatModelsHint", "compatMaxTokens",
        "compatSave", "compatClear", "compatError", "claudeMaxTokens", "claudeError", "apiKeySave",
        "apiKeyReuse",
    ):  # fmt: skip
        assert f'id="{name}"' in page and f'$("{name}")' in script, name
    assert 'placeholder="https://api.example.com"' in page and "ANTHROPIC_BASE_URL" in page
    key_input = page.split('<input id="compatKey"', 1)[1].split(">", 1)[0]
    assert 'type="password"' in key_input and 'autocomplete="new-password"' in key_input
    assert '"/api/settings/anthropic-compatible"' in script
    assert "style=" not in page and "<style" not in page and "<script>" not in page
