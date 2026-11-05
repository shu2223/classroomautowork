"""The settings dialog's connection test (POST /api/settings/test), against the local fake endpoint
of test_openai_compat on 127.0.0.1; no real endpoint is used."""

import json
import stat
import struct
import threading
import zlib

import pytest
from test_frontend import local_server, public  # noqa: F401
from test_openai_compat import Endpoint, chunks, reply_json, reply_stream

from classroomautowork import connection_test, openai_compat, providers
from classroomautowork.errors import Busy, ConfigurationError

KEY = "sk-probe-" + "t" * 30 + "Q7wE"
ORIGIN = "https://yishuapp.com"
PATH = "/classroom/api/settings/test"
OK = json.dumps({"ok": True, "reply": "连接正常"}, ensure_ascii=False)


@pytest.fixture
def server():
    fake = Endpoint()
    yield fake
    fake.close()


def save(data_dir, fake, **extra):
    providers.save_openai(
        data_dir,
        {"base_url": fake.base, "api_key": KEY, "models": "probe-a, probe-b", **extra},
        verify=lambda *_: {"listed": True, "ids": ["probe-a"], "status": 200},
    )


def post(local, body, origin=ORIGIN):
    status, _, raw = public(local, PATH, method="POST", body=body, origin=origin)
    return status, json.loads(raw), raw


def test_the_tiny_page_image_is_a_real_png():
    data = connection_test.tiny_png()
    assert data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) < 400
    width, height = struct.unpack(">II", data[16:24])
    start = data.index(b"IDAT") + 4
    length = struct.unpack(">I", data[start - 8 : start - 4])[0]
    assert (width, height) == (16, 16)
    assert len(zlib.decompress(data[start : start + length])) == 16 * (1 + 16 * 3)


def test_a_test_runs_the_answer_path_and_is_kept_without_the_key(local_server, server):  # noqa: F811
    data_dir = local_server.jobs.settings.data_dir
    save(data_dir, server, max_output_tokens=16000)
    usage = {"prompt_tokens": 400, "completion_tokens": 12}
    server.replies.append(reply_stream(chunks(OK, model="probe-a-0601", usage=usage)))
    assert public(local_server, PATH, method="POST", body={"provider": "openai"})[0] == 403
    assert not server.requests
    status, result, raw = post(local_server, {"provider": "openai"})
    assert status == 200 and result["ok"] is True
    assert result["provider"] == "openai" and result["host"] == server.base.split("/")[2]
    assert result["requested_model"] == "probe-a" and result["reported_model"] == "probe-a-0601"
    assert result["structured_output_mode"] == "json_schema" and result["images"] == "sent"
    assert result["streamed"] is True and result["degradations"] == []
    assert result["usage"] == {
        "input_tokens": 400,
        "output_tokens": 12,
        "cache_read_input_tokens": 0,
    }
    assert isinstance(result["duration_ms"], int) and result["error"] is None
    assert result["message"].startswith("连接正常：probe-a 用时 ")
    assert "接口报告的模型是 probe-a-0601。" in result["message"] and result["notes"] == []
    assert result["output_limit"] == {"requested": 16000, "sent": 16000, "param": "max_tokens"}
    # Exactly an answer's request, only tiny: the text, a captioned PNG, the two-field schema,
    # no effort (默认) and the saved output limit.
    (request,) = server.requests
    body = request["body"]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer " + KEY
    assert "reasoning_effort" not in body and body["max_tokens"] == 16000 and body["stream"] is True
    assert body["response_format"]["json_schema"]["schema"] == connection_test.SCHEMA
    parts = body["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": connection_test.TEXT}
    assert parts[1] == {"type": "text", "text": connection_test.CAPTION}
    assert parts[2]["image_url"]["url"].startswith("data:image/png;base64,iVBORw0KGgo")
    assert parts[-1]["text"] == openai_compat.schema_instruction(connection_test.SCHEMA)
    # The last test is kept per provider, privately, and shown in the provider block.
    path = data_dir / providers.TESTS_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    kept = json.loads(path.read_text(encoding="utf-8"))
    assert set(kept["openai"]) == {"at", "ok", "model", "summary"}
    assert kept["openai"]["ok"] is True and kept["openai"]["model"] == "probe-a"
    assert kept["openai"]["summary"].startswith("正常 · ") and kept["openai"]["summary"].endswith(
        " 秒"
    )
    _, _, boot = public(local_server, "/classroom/api/bootstrap")
    assert json.loads(boot)["provider"]["tests"]["openai"] == kept["openai"]
    for data in (raw, boot, path.read_bytes()):
        assert KEY.encode() not in data and KEY[:-4].encode() not in data


def test_an_endpoint_that_refuses_images_is_told_apart(local_server, server):  # noqa: F811
    save(local_server.jobs.settings.data_dir, server)
    refused = {
        "error": {"message": "Invalid content type. image_url is only supported by certain models."}
    }
    server.replies += [reply_json(refused, 400), reply_stream(chunks(OK))]
    status, result, _ = post(local_server, {"provider": "openai", "model": "probe-b"})
    assert status == 200 and result["ok"] and result["images"] == "refused"
    assert result["degradations"] == ["images"] and result["requested_model"] == "probe-b"
    assert "接口不接受图片" in result["message"]
    assert result["notes"] == ["接口不接受图片：作答时页面图片不会发送，只按提取的文字作答。"]
    second = server.requests[1]["body"]["messages"][0]["content"]
    # Without the image the instruction is still sent; only the caption of the image goes.
    assert isinstance(second, str) and second.startswith(connection_test.TEXT)
    assert connection_test.CAPTION in second  # listed as the page that was not sent


def test_a_failed_test_says_why_and_is_kept_as_a_failure(local_server, server):  # noqa: F811
    data_dir = local_server.jobs.settings.data_dir
    save(data_dir, server)
    server.replies.append(
        reply_json({"error": {"message": f"Incorrect API key provided: {KEY}"}}, 401)
    )
    status, result, raw = post(local_server, {"provider": "openai"})
    assert status == 200 and result["ok"] is False
    main, detail = result["error"].split("\n", 1)
    assert (
        main.startswith("OpenAI 兼容接口拒绝了密钥（HTTP 401）") and "Incorrect API key" in detail
    )
    assert KEY.encode() not in raw and KEY[-8:].encode() not in raw
    kept = providers.last_tests(data_dir)["openai"]
    assert kept["ok"] is False and kept["summary"] == main


def test_what_cannot_be_tested_is_refused_before_anything_is_sent(local_server, server):  # noqa: F811
    data_dir = local_server.jobs.settings.data_dir
    status, result, _ = post(local_server, {"provider": "openai"})
    assert status == 400 and "还没有保存 OpenAI 兼容接口" in result["error"]
    save(data_dir, server)
    for body, words in [
        ({"provider": "openai", "model": "not-saved"}, "不在 OpenAI 兼容接口设置的模型里"),
        ({"provider": "openai", "model": "bad model"}, "模型名称无效"),
        ({"provider": "gemini"}, "模型接口无效"),
        ({"provider": "openai", "key": KEY}, "只接受 provider 和 model"),
        ({}, "只接受 provider 和 model"),
        # The Claude providers use the same route; with nothing saved they say what to save.
        ({"provider": "anthropic"}, "还没有保存 Claude API 密钥"),
        ({"provider": "anthropic_compatible"}, "还没有保存 Anthropic 兼容接口"),
    ]:
        status, result, _ = post(local_server, body)
        assert status == 400 and words in result["error"], body
    assert not server.requests and providers.last_tests(data_dir) == {}


def test_one_test_at_a_time_and_a_second_gets_409(local_server, server):  # noqa: F811
    save(local_server.jobs.settings.data_dir, server)
    assert connection_test.RUNNING.acquire(blocking=False)
    try:
        status, result, _ = post(local_server, {"provider": "openai"})
        assert status == 409 and "已有一次连接测试正在进行" in result["error"]
        with pytest.raises(Busy):
            connection_test.run(local_server.jobs.settings.data_dir, {"provider": "openai"})
    finally:
        connection_test.RUNNING.release()
    assert not server.requests


def test_a_test_has_tighter_time_limits_and_no_effort(tmp_path, monkeypatch, server):
    save(tmp_path, server)
    seen = {}

    def fake_turn(content, schema, **options):
        seen.update(options, content=content, schema=schema)
        return {
            "output": {"ok": True, "reply": "连接正常"},
            "model": options["model"],
            "usage": None,
            "cost_usd": None,
            "duration_ms": 2100,
            "provider": {"host": "h", "structured_output_mode": "json_schema", "degradations": []},
        }

    monkeypatch.setattr(openai_compat, "run_turn", fake_turn)
    result = connection_test.run(tmp_path, {"provider": "openai"})
    assert (seen["turn_seconds"], seen["idle_seconds"]) == (120, 60)
    assert seen["effort"] is None and seen["schema"] == connection_test.SCHEMA
    assert result["ok"] and providers.last_tests(tmp_path)["openai"]["summary"] == "正常 · 2.1 秒"
    assert openai_compat.idle_message(60).startswith("OpenAI 兼容接口连续 60 秒没有任何输出")
    assert "超过 2 分钟仍未完成" in openai_compat.turn_message(120)
    assert openai_compat.idle_message() == openai_compat.IDLE_MESSAGE


def test_settings_saved_during_a_test_are_not_described_by_it(tmp_path, monkeypatch, server):
    save(tmp_path, server)
    original = connection_test.TESTERS["openai"]

    def run(data_dir, model, cancelled):
        done = {"host": "h", "requested_model": "probe-a", "outcome": {"model": "probe-a"}}
        save(data_dir, server, max_output_tokens=20000)  # another page saves meanwhile
        return done

    monkeypatch.setitem(
        connection_test.TESTERS,
        "openai",
        connection_test.Tester(original.check, run, original.marker, original.describe),
    )
    assert connection_test.run(tmp_path, {"provider": "openai"})["ok"]
    assert "openai" not in providers.last_tests(tmp_path)


def test_saving_or_deleting_the_settings_forgets_the_last_test(tmp_path, server):
    save(tmp_path, server)
    providers.record_test(
        tmp_path, "openai", {"at": "t", "ok": True, "model": "probe-a", "summary": "正常 · 1.0 秒"}
    )
    providers.record_test(
        tmp_path, "anthropic", {"at": "t", "ok": True, "model": "m", "summary": "正常 · 1.0 秒"}
    )
    save(tmp_path, server, max_output_tokens=16000)
    assert set(providers.last_tests(tmp_path)) == {"anthropic"}
    providers.record_test(
        tmp_path, "openai", {"at": "t", "ok": False, "model": "probe-a", "summary": "x"}
    )
    providers.save_openai(tmp_path, {"clear": True})
    assert set(providers.last_tests(tmp_path)) == {"anthropic"}


def test_a_damaged_tests_file_reads_as_none(tmp_path):
    (tmp_path / providers.TESTS_FILE).write_text("[1, 2", encoding="utf-8")
    assert providers.last_tests(tmp_path) == {} and providers.status(tmp_path)["tests"] == {}


def test_tests_of_two_pages_at_once_leave_one_running(tmp_path, monkeypatch, server):
    save(tmp_path, server)
    started, release = threading.Event(), threading.Event()

    def slow_turn(*_args, **options):
        started.set()
        release.wait(5)
        return {"output": {}, "model": options["model"], "usage": None, "duration_ms": 5}

    monkeypatch.setattr(openai_compat, "run_turn", slow_turn)
    outcome = {}
    thread = threading.Thread(
        target=lambda: outcome.update(connection_test.run(tmp_path, {"provider": "openai"}))
    )
    thread.start()
    assert started.wait(5)
    with pytest.raises(Busy):
        connection_test.run(tmp_path, {"provider": "openai"})
    release.set()
    thread.join(5)
    assert outcome["ok"] and not connection_test.RUNNING.locked()
    with pytest.raises(ConfigurationError):
        connection_test.run(tmp_path, {"provider": "openai", "model": 5})


def test_the_page_has_the_limit_field_and_a_test_button_per_group():
    from pathlib import Path

    web = Path(connection_test.__file__).parent / "web"
    page = (web / "index.html").read_text(encoding="utf-8")
    script = (web / "app.js").read_text(encoding="utf-8")
    assert 'id="openaiMaxTokens"' in page and '$("openaiMaxTokens")' in script
    button = page.split('<button id="openaiTest"', 1)[1].split(">", 1)[0]
    assert 'data-test-provider="openai"' in button and 'type="button"' in button
    assert 'id="openaiTestResult"' in page and 'data-test-result="openai"' in page
    assert 'role="status"' in page.split('id="openaiTestResult"', 1)[1].split(">", 1)[0]
    assert "很短的请求" in page and "少量 token" in page
    # One generic wiring for every group's button, busy while it runs, and a wait longer than the
    # server's 120-second limit for the test.
    assert 'querySelectorAll("[data-test-provider]")' in script and '"测试中…"' in script
    assert 'api("/api/settings/test", "POST"' in script and "135000" in script
    assert "max_output_tokens:" in script
    assert "style=" not in page and "<style" not in page and "<script>" not in page
