import json
import re
import stat
import threading

import pytest
from test_frontend import local_server, public  # noqa: F401

from classroomautowork import api_key, providers
from classroomautowork.errors import ConfigurationError, WorkflowError

KEY = "sk-ant-api03-" + "a" * 40 + "WXYZ"


def test_saved_key_is_private_and_only_its_tail_is_reported(tmp_path):
    checked = []
    status = api_key.save_key(tmp_path, f"  {KEY}\n", verify=checked.append)
    assert checked == [KEY]
    assert status["configured"] and status["last4"] == "WXYZ" and status["updated_at"]
    assert KEY not in json.dumps(api_key.key_status(tmp_path))
    path = tmp_path / api_key.KEY_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert api_key.load_key(tmp_path) == KEY
    assert not api_key.save_key(tmp_path, "", verify=checked.append)["configured"]
    assert not path.exists() and checked == [KEY]


@pytest.mark.parametrize("value", ["not-a-key", "sk-ant-short", "sk-ant-" + "a" * 30 + " b", 5])
def test_malformed_keys_are_rejected_before_any_check(tmp_path, value):
    with pytest.raises(ConfigurationError):
        api_key.save_key(tmp_path, value, verify=lambda _: pytest.fail("must not call Anthropic"))
    assert not (tmp_path / api_key.KEY_FILE).exists()


def test_a_key_anthropic_rejects_is_not_saved_and_the_old_one_stays(tmp_path):
    api_key.save_key(tmp_path, KEY, verify=lambda _: None)

    def reject(_):
        raise ConfigurationError("Anthropic 拒绝了这个 API 密钥，未保存。")

    with pytest.raises(ConfigurationError, match="拒绝"):
        api_key.save_key(tmp_path, "sk-ant-api03-" + "b" * 44, verify=reject)
    assert api_key.load_key(tmp_path) == KEY


def test_key_is_set_from_the_public_page_and_never_returned(local_server, monkeypatch):  # noqa: F811
    monkeypatch.setattr(api_key, "check_key", lambda _: None)
    path = "/classroom/api/settings/api-key"
    origin = "https://yishuapp.com"
    assert public(local_server, path, method="PUT", body={"key": KEY})[0] == 403
    status, _, body = public(local_server, path, method="PUT", body={"key": KEY}, origin=origin)
    assert status == 200 and json.loads(body)["last4"] == "WXYZ"
    _, _, bootstrap = public(local_server, "/classroom/api/bootstrap")
    assert json.loads(bootstrap)["api_key"]["configured"] and KEY.encode() not in bootstrap
    extra = public(local_server, path, method="PUT", body={"key": KEY, "x": 1}, origin=origin)
    assert extra[0] == 400


# ---------------------------------------------------------------- the OpenAI-compatible provider

OPENAI_KEY = "sk-endpoint-" + "k" * 30 + "TAIL"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("https://api.example.com/v1", "https://api.example.com/v1"),
        ("  https://API.Example.com/v1/  ", "https://api.example.com/v1"),
        ("https://api.deepseek.com", "https://api.deepseek.com"),
        ("https://api.deepseek.com/chat/completions", "https://api.deepseek.com"),
        (
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions/",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
        (
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "https://generativelanguage.googleapis.com/v1beta/openai",
        ),
        ("http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/v1"),
        ("http://[::1]:8000/v1", "http://[::1]:8000/v1"),
    ],
)
def test_base_urls_are_normalised_without_adding_anything(value, expected):
    assert providers.normalize_base_url(value) == expected


@pytest.mark.parametrize(
    "value, words",
    [
        ("", "请填写接口地址"),
        ("ftp://api.example.com/v1", "http:// 或 https://"),
        ("api.example.com/v1", "http:// 或 https://"),
        ("https://user:pass@api.example.com/v1", "用户名或密码"),
        ("https://api.example.com/v1?key=1", "?"),
        ("https://api.example.com/v1#top", "#"),
        ("https://api.example.com:99999/v1", "端口"),
        ("https://api.example.com/v 1", "空格"),
        ("https:///v1", "主机名"),
        (5, "文字"),
    ],
)
def test_bad_base_urls_are_refused_with_the_reason(value, words):
    with pytest.raises(ConfigurationError, match=re.escape(words)):
        providers.normalize_base_url(value)


def test_model_names_are_text_or_lists_first_is_the_default():
    assert providers.parse_models(" a-1, b:2 ，c/3\nd@4、a-1 ") == ["a-1", "b:2", "c/3", "d@4"]
    assert providers.parse_models(["gpt-4o", " models/gemini-2.5-flash "]) == [
        "gpt-4o",
        "models/gemini-2.5-flash",
    ]
    for value, words in [
        ("", "至少填写一个"),
        (", ,", "至少填写一个"),
        (",".join(f"m{i}" for i in range(21)), "最多填写 20 个"),
        ("gpt 4o", "模型名称“gpt 4o”不对"),
        ("x" * 201, "不超过 200"),
        ([1], "文字"),
    ]:
        with pytest.raises(ConfigurationError, match=re.escape(words)):
            providers.parse_models(value)


def listing(ids, listed=True, status=200):
    calls = []

    def verify(base_url, key):
        calls.append((base_url, key))
        return {"listed": listed, "ids": list(ids), "status": status}

    return verify, calls


def test_openai_settings_are_private_verified_and_never_returned(tmp_path):
    verify, calls = listing([f"listed-{i}" for i in range(600)] + ["chosen-a"])
    view = providers.save_openai(
        tmp_path,
        {
            "base_url": "https://api.example.com/v1/",
            "api_key": f" {OPENAI_KEY} ",
            "models": "chosen-a, other-b",
        },
        verify=verify,
    )
    assert calls == [("https://api.example.com/v1", OPENAI_KEY)]
    path = tmp_path / providers.OPENAI_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert view["active"] == "anthropic"  # saving does not switch
    status = view["openai"]
    assert status["configured"] and status["host"] == "api.example.com"
    assert status["base_url"] == "https://api.example.com/v1" and status["last4"] == "TAIL"
    assert status["models"] == ["chosen-a", "other-b"] and status["default_model"] == "chosen-a"
    assert status["models_listed"] and status["listed_models_count"] == 601
    assert status["unlisted_models"] == ["other-b"] and not status["plain_http"]
    assert view["check"] == {
        "models_listed": True,
        "listed_models_count": 601,
        "unlisted_models": ["other-b"],
        "key_checked": None,
        "status": 200,
    }
    assert len(view["suggestions"]) == providers.MAX_LISTED
    for shown in (view, providers.status(tmp_path), providers.settings_view(tmp_path)):
        text = json.dumps(shown)
        assert OPENAI_KEY not in text and OPENAI_KEY[:-4] not in text
    assert providers.load_openai(tmp_path)["api_key"] == OPENAI_KEY


def test_the_key_may_be_left_out_only_for_the_same_address(tmp_path):
    verify, calls = listing(["m"])
    first = {"base_url": "https://api.example.com/v1", "api_key": OPENAI_KEY, "models": ["m"]}
    with pytest.raises(ConfigurationError, match="请填写接口的密钥"):
        providers.save_openai(tmp_path, {**first, "api_key": ""}, verify=verify)
    providers.save_openai(tmp_path, first, verify=verify)
    # Same host, a new path and models: the saved key is used to verify and kept.
    view = providers.save_openai(
        tmp_path, {"base_url": "https://api.example.com/v2", "models": "m, n"}, verify=verify
    )
    assert calls[-1] == ("https://api.example.com/v2", OPENAI_KEY)
    assert view["openai"]["models"] == ["m", "n"] and view["openai"]["last4"] == "TAIL"
    for other in (
        "https://other.example.com/v1",
        "http://api.example.com/v1",
        "https://api.example.com:8443/v1",
    ):
        with pytest.raises(ConfigurationError, match="换了接口地址，请重新填写密钥"):
            providers.save_openai(
                tmp_path, {"base_url": other, "api_key": None, "models": "m"}, verify=verify
            )
    assert providers.load_openai(tmp_path)["base_url"] == "https://api.example.com/v2"


def test_a_refused_or_unreachable_endpoint_is_not_saved(tmp_path):
    verify, _ = listing(["m"])
    good = {"base_url": "https://api.example.com/v1", "api_key": OPENAI_KEY, "models": "m"}
    providers.save_openai(tmp_path, good, verify=verify)
    for error in (
        ConfigurationError("接口拒绝了这个密钥（HTTP 401），未保存。"),
        WorkflowError("连不上，未保存。"),
    ):

        def refuse(*_, error=error):
            raise error

        with pytest.raises(WorkflowError, match="未保存"):
            providers.save_openai(
                tmp_path, {**good, "api_key": "sk-new-" + "z" * 30, "models": "x"}, verify=refuse
            )
        assert providers.load_openai(tmp_path)["models"] == ["m"]


def test_an_endpoint_without_a_model_list_is_saved_and_says_so(tmp_path):
    verify, _ = listing([], listed=False, status=404)
    view = providers.save_openai(
        tmp_path,
        {"base_url": "http://192.168.0.9:8000/v1", "api_key": "ollama", "models": "local"},
        verify=verify,
    )
    status = view["openai"]
    assert status["configured"] and not status["models_listed"] and status["unlisted_models"] == []
    assert view["check"]["status"] == 404 and view["suggestions"] == []
    # A short key never shows its tail, and a private address is not flagged as clear text.
    assert status["last4"] is None and not status["plain_http"]
    assert "ollama" not in json.dumps(view)


def test_plain_http_to_the_internet_is_flagged(tmp_path):
    verify, _ = listing(["m"])
    view = providers.save_openai(
        tmp_path,
        {"base_url": "http://api.example.com/v1", "api_key": OPENAI_KEY, "models": "m"},
        verify=verify,
    )
    assert view["openai"]["plain_http"]


def test_switching_needs_a_saved_endpoint_and_clearing_goes_back_to_claude(tmp_path):
    with pytest.raises(ConfigurationError, match="还没有保存 OpenAI 兼容接口"):
        providers.save_provider(tmp_path, {"provider": "openai"})
    for bad in ({"provider": "gemini"}, {"provider": "openai", "x": 1}, {}):
        with pytest.raises(ConfigurationError):
            providers.save_provider(tmp_path, bad)
    assert providers.active_provider(tmp_path) == "anthropic"
    verify, _ = listing(["m"])
    providers.save_openai(
        tmp_path,
        {"base_url": "https://api.example.com/v1", "api_key": OPENAI_KEY, "models": "m"},
        verify=verify,
    )
    assert providers.save_provider(tmp_path, {"provider": "openai"})["active"] == "openai"
    assert stat.S_IMODE((tmp_path / providers.PROVIDER_FILE).stat().st_mode) == 0o600
    with pytest.raises(ConfigurationError, match="clear"):
        providers.save_openai(tmp_path, {"clear": True, "models": "m"})
    view = providers.save_openai(tmp_path, {"clear": True})
    assert view["active"] == "anthropic" and not view["openai"]["configured"]
    assert not (tmp_path / providers.OPENAI_FILE).exists()


def test_damaged_settings_read_as_missing_and_say_so(tmp_path):
    (tmp_path / providers.OPENAI_FILE).write_text("{not json", encoding="utf-8")
    (tmp_path / providers.PROVIDER_FILE).write_text('{"provider": "other"}', encoding="utf-8")
    status = providers.status(tmp_path)
    assert status["active"] == "anthropic"
    assert not status["openai"]["configured"] and status["openai"]["damaged"]
    with pytest.raises(ConfigurationError, match="已损坏"):
        providers.load_openai(tmp_path)
    with pytest.raises(ConfigurationError, match="还没有设置 OpenAI 兼容接口"):
        providers.openai_catalog(tmp_path / "elsewhere")


def test_openai_settings_routes_take_the_write_guard_and_hide_the_key(local_server, monkeypatch):  # noqa: F811
    verify, calls = listing(["model-a", "model-b"])
    monkeypatch.setattr(providers, "list_models", verify)
    origin = "https://yishuapp.com"
    path = "/classroom/api/settings/openai"
    body = {"base_url": "https://api.example.com/v1", "api_key": OPENAI_KEY, "models": "model-a"}
    assert public(local_server, path, method="PUT", body=body)[0] == 403
    assert (
        public(local_server, path, method="PUT", body=body, origin="https://evil.example")[0] == 403
    )
    assert not calls
    assert public(local_server, path, method="PUT", body={**body, "x": 1}, origin=origin)[0] == 400
    switch = "/classroom/api/settings/provider"
    status, _, refused = public(
        local_server, switch, method="PUT", body={"provider": "openai"}, origin=origin
    )
    assert status == 400 and "还没有保存" in json.loads(refused)["error"]
    status, _, saved = public(local_server, path, method="PUT", body=body, origin=origin)
    assert status == 200 and json.loads(saved)["openai"]["host"] == "api.example.com"
    status, _, switched = public(
        local_server, switch, method="PUT", body={"provider": "openai"}, origin=origin
    )
    assert status == 200 and json.loads(switched)["active"] == "openai"
    status, _, view = public(local_server, path)
    assert status == 200 and json.loads(view)["suggestions"] == ["model-a", "model-b"]
    status, _, boot = public(local_server, "/classroom/api/bootstrap")
    provider = json.loads(boot)["provider"]
    assert provider["active"] == "openai" and provider["openai"]["models"] == ["model-a"]
    assert "api_key" in json.loads(boot) and provider["anthropic"] == json.loads(boot)["api_key"]
    status, _, models = public(local_server, "/classroom/api/models")
    assert json.loads(models)["provider"] == "openai"
    for data in (saved, switched, view, boot, models):
        assert OPENAI_KEY.encode() not in data and OPENAI_KEY[:-4].encode() not in data
    status, _, cleared = public(
        local_server, path, method="PUT", body={"clear": True}, origin=origin
    )
    assert status == 200 and json.loads(cleared)["active"] == "anthropic"
    assert (
        public(local_server, path, method="PUT", body={"clear": True}, access=False, origin=origin)[
            0
        ]
        == 403
    )


# ---------------------------------------------------------------- after review (2026-11-05)


@pytest.mark.parametrize(
    "value, words",
    [
        ("https://evil.example\\.api.example.com/v1", "主机名无效"),
        ("https://-bad.example.com/v1", "主机名无效"),
        ("http://2130706433/v1", "IP 地址无效"),
        ("http://0x7f.0.0.1/v1", "IP 地址无效"),
        ("http://127.1/v1", "IP 地址无效"),
        ("http://127.000.000.001/v1", "IP 地址无效"),
        ("http://[::zz]/v1", "无效"),
    ],
)
def test_hosts_must_be_plain_names_or_addresses(value, words):
    with pytest.raises(ConfigurationError, match=words):
        providers.normalize_base_url(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:11434/v1",
        "http://ollama_server:11434/v1",
        "https://xn--fiqs8s.example/v1",
        "http://192.168.0.5:8000/v1",
    ],
)
def test_usual_hosts_still_pass(value):
    assert providers.normalize_base_url(value) == value


def test_a_key_in_the_model_field_is_refused_and_never_echoed(tmp_path):
    verify, calls = listing([], listed=False, status=404)
    base = {"base_url": "http://127.0.0.1:9/v1", "api_key": OPENAI_KEY}
    for models in (
        OPENAI_KEY,
        f"real-model, {OPENAI_KEY}",
        "sk-proj-" + "Zp4Lm9Xw2Rt8Vn5Kc3Hb",
        "x-" + OPENAI_KEY,
    ):
        with pytest.raises(ConfigurationError, match="模型名称看起来像密钥") as caught:
            providers.save_openai(tmp_path, {**base, "models": models}, verify=verify)
        assert OPENAI_KEY[:12] not in str(caught.value) and "Zp4L" not in str(caught.value)
    # A short placeholder key does not make names that contain it look like keys.
    view = providers.save_openai(
        tmp_path,
        {**base, "api_key": "ollama", "models": "ollama/llama3, qwen2.5:14b"},
        verify=verify,
    )
    assert view["openai"]["models"] == ["ollama/llama3", "qwen2.5:14b"]
    with pytest.raises(ConfigurationError, match="看起来像密钥"):
        providers.save_openai(
            tmp_path, {**base, "api_key": "ollama", "models": "ollama"}, verify=verify
        )
    assert len(calls) == 1  # refused before anything was sent


def test_a_clear_while_a_save_is_checking_wins(tmp_path):
    verify, _ = listing([], listed=False, status=404)
    good = {"base_url": "https://api.example.com/v1", "api_key": OPENAI_KEY, "models": "m"}
    providers.save_openai(tmp_path, good, verify=verify)
    checking, release = threading.Event(), threading.Event()

    def slow(*_):
        checking.set()
        release.wait(5)
        return {"listed": False, "ids": [], "status": 404}

    outcome = {}

    def save():
        try:
            providers.save_openai(
                tmp_path, {"base_url": good["base_url"], "models": "m"}, verify=slow
            )
        except ConfigurationError as exc:
            outcome["error"] = str(exc)

    thread = threading.Thread(target=save)
    thread.start()
    assert checking.wait(5)
    providers.save_openai(tmp_path, {"clear": True})
    release.set()
    thread.join(5)
    assert "验证期间" in outcome["error"]
    assert not (tmp_path / providers.OPENAI_FILE).exists()
    assert providers.active_provider(tmp_path) == "anthropic"
    # Two saves at once: the second one to finish its check does not overwrite the first.
    checking.clear(), release.clear()
    thread = threading.Thread(target=save)
    providers.save_openai(tmp_path, good, verify=verify)
    thread.start()
    assert checking.wait(5)
    providers.save_openai(tmp_path, {**good, "models": "n"}, verify=verify)
    release.set()
    thread.join(5)
    assert providers.load_openai(tmp_path)["models"] == ["n"]


@pytest.mark.parametrize("value", [1, 1.0, "true", [True], None])
def test_only_clear_true_deletes(tmp_path, value):
    verify, _ = listing(["m"])
    good = {"base_url": "https://api.example.com/v1", "api_key": OPENAI_KEY, "models": "m"}
    providers.save_openai(tmp_path, good, verify=verify)
    with pytest.raises(ConfigurationError, match="clear: true"):
        providers.save_openai(tmp_path, {"clear": value})
    assert providers.load_openai(tmp_path)["models"] == ["m"]


def test_a_list_that_does_not_check_keys_is_said_so(tmp_path):
    def public(*_):
        return {"listed": True, "ids": ["m"], "status": 200, "key_checked": False}

    view = providers.save_openai(
        tmp_path,
        {"base_url": "https://openrouter.ai/api/v1", "api_key": OPENAI_KEY, "models": "m"},
        verify=public,
    )
    assert view["check"]["key_checked"] is False and view["openai"]["key_checked"] is False
    assert providers.load_openai(tmp_path)["key_checked"] is False
