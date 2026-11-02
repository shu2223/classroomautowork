import json
import stat

import pytest
from test_frontend import local_server, public  # noqa: F401

from classroomautowork import api_key
from classroomautowork.errors import ConfigurationError

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
