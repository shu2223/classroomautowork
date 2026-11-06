"""The /api/school-login/ routes over a stand-in for the school-login browser: the same guard
as every route, 409 while busy, frames as base64, and nothing the user types ever comes back."""

import base64

import pytest
from test_frontend import local_server, public, request  # noqa: F401 - the fixture is used

from classroomautowork import school_browser
from classroomautowork.errors import Busy, WorkflowError
from classroomautowork.school_browser import CDPError

TYPED = "typed-unit-secret-never-echoed"


class FakeManager:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.ended = False

    def status(self):
        self.calls.append(("status",))
        return {"state": "signed_out", "email": "", "verified_at": None, "session": None}

    def start(self, **options):
        self.calls.append(("start", options))
        if self.fail:
            raise self.fail
        return {"state": "signed_out", "session": {"active": True}}

    def frame(self, after=0, timeout=20.0):
        self.calls.append(("frame", after, timeout))
        if self.ended:
            raise WorkflowError("登录已取消。")
        return {
            "seq": after + 1,
            "jpeg": b"\xff\xd8unit",
            "image_width": 780,
            "image_height": 1040,
            "width": 390,
            "height": 520,
            "changed": True,
            "host": "accounts.google.com",
            "password": True,
            "editable": True,
            "hint": "",
        }

    def input(self, action):
        self.calls.append(("input", action.get("type")))  # the text itself is not recorded here
        if self.fail:
            raise self.fail
        return {"ok": True}

    def back(self):
        self.calls.append(("back",))
        return {"ok": True}

    def finish(self):
        self.calls.append(("finish",))
        return {"state": "signed_in", "email": "student@school.example"}

    def cancel(self):
        self.calls.append(("cancel",))
        return {"state": "signed_out"}

    def logout(self):
        self.calls.append(("logout",))
        return {"state": "signed_out"}


@pytest.fixture
def manager(monkeypatch):
    fake = FakeManager()
    monkeypatch.setattr(school_browser, "manager", lambda _settings: fake)
    return fake


def post(server, path, body):
    return request(server, path, method="POST", body=body, origin=server.origin)


def test_reads_take_the_token_and_writes_the_origin_too(local_server, manager):  # noqa: F811
    assert request(local_server, "/api/school-login/status", token=False)[0] == 401
    assert request(local_server, "/api/school-login/status")[0] == 200
    start = {"width": 358, "height": 480, "touch": True, "scale": 2}
    assert request(local_server, "/api/school-login/start", method="POST", body=start)[0] == 403
    assert post(local_server, "/api/school-login/start", start)[0] == 200
    assert (
        "start",
        {"width": 358, "height": 480, "touch": True, "scale": 2, "takeover": False},
    ) in manager.calls
    # 在这里重新打开: a window open elsewhere is closed and opened here instead.
    assert post(local_server, "/api/school-login/start", {**start, "takeover": True})[0] == 200
    assert manager.calls[-1][1]["takeover"] is True
    assert post(local_server, "/api/school-login/start", {**start, "takeover": "yes"})[0] == 400
    for bad in (
        {"width": 358},
        {**start, "url": "https://evil.example"},
        {**start, "touch": "yes"},
        {**start, "scale": 3},
    ):
        assert post(local_server, "/api/school-login/start", bad)[0] == 400


def test_public_access_needs_cloudflare_access_and_the_public_origin(local_server, manager):  # noqa: F811
    assert public(local_server, "/classroom/api/school-login/status", access=False)[0] == 403
    assert public(local_server, "/classroom/api/school-login/status")[0] == 200
    path = "/classroom/api/school-login/cancel"
    assert public(local_server, path, method="POST", body={})[0] == 403
    assert (
        public(local_server, path, method="POST", body={}, origin="https://evil.example")[0] == 403
    )
    assert (
        public(local_server, path, method="POST", body={}, origin="https://yishuapp.com")[0] == 200
    )


def test_a_second_session_or_a_read_is_a_409(local_server, manager):  # noqa: F811
    manager.fail = Busy("学校账号登录窗口已经打开（可能在另一台设备上）。请先完成或取消它。")
    status, _, data = post(local_server, "/api/school-login/start", {"width": 400, "height": 500})
    assert status == 409 and "已经打开" in data.decode()


def test_frames_long_poll_as_base64_and_say_when_the_session_ended(local_server, manager):  # noqa: F811
    status, _, data = request(local_server, "/api/school-login/frame?after=4&wait=25")
    frame = __import__("json").loads(data)
    assert status == 200 and base64.b64decode(frame["image"]) == b"\xff\xd8unit"
    assert frame["seq"] == 5 and frame["password"] is True and "jpeg" not in frame
    assert ("frame", 4, 25) in manager.calls
    assert request(local_server, "/api/school-login/frame?after=0&wait=99")[0] == 200
    assert manager.calls[-1] == ("frame", 0, 25)  # capped
    for query in ("after=-1", "after=x", "after=1&after=2", "after=1&url=x", "wait=100"):
        assert request(local_server, "/api/school-login/frame?" + query)[0] == 400
    manager.ended = True
    ended = __import__("json").loads(request(local_server, "/api/school-login/frame?after=5")[2])
    assert ended == {"ended": True, "message": "登录已取消。"}


@pytest.mark.parametrize(
    ("failure", "status", "words"),
    [
        (None, 200, '"ok": true'),
        (CDPError(f"Input.insertText: invalid {TYPED}"), 400, "登录页没有响应"),
        (WorkflowError("要发送的文字为空或太长（最多 2000 个字符）。"), 400, "最多 2000"),
        (Busy("学校账号登录窗口已经关闭，请重新点“登录”。"), 409, "已经关闭"),
        (RuntimeError(f"unexpected {TYPED}"), 400, "本机操作失败"),
    ],
)
def test_typed_text_is_never_answered_back(local_server, manager, capfd, failure, status, words):  # noqa: F811
    manager.fail = failure
    answer = post(local_server, "/api/school-login/input", {"type": "text", "text": TYPED})
    assert answer[0] == status and words in answer[2].decode()
    assert TYPED.encode() not in answer[2]
    assert TYPED not in "".join(capfd.readouterr())
    assert ("input", "text") in manager.calls


def test_the_other_actions_take_no_parameters(local_server, manager):  # noqa: F811
    for name in ("back", "finish", "cancel", "logout"):
        assert post(local_server, f"/api/school-login/{name}", {})[0] == 200
        assert (name,) in manager.calls
        assert post(local_server, f"/api/school-login/{name}", {"x": 1})[0] == 400
    assert post(local_server, "/api/school-login/submit", {})[0] == 404


def test_bootstrap_says_who_is_signed_in(local_server):  # noqa: F811
    data = __import__("json").loads(request(local_server, "/api/bootstrap")[2])
    assert data["school_login"]["state"] == "signed_out"
    school_browser._write_record(
        local_server.jobs.settings.data_dir,
        {
            "email": "other@school.example",
            "verified_at": "2026-11-06T00:00:00+00:00",
            "state": "signed_in",
        },
    )
    data = __import__("json").loads(request(local_server, "/api/bootstrap")[2])
    # The unit settings name another school email: the login is shown as not matching them.
    assert (
        data["school_login"]["state"] == "mismatch"
        and data["school_login"]["email"] == "other@school.example"
    )
