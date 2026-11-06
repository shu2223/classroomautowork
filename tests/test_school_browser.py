"""The school-login browser's session logic, state file and helpers, with a stand-in for
Chromium (no browser runs here; the real one is exercised by the offline harness)."""

import io
import json
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from classroomautowork import browser_guard as guard
from classroomautowork import school_browser as sb
from classroomautowork.errors import Busy, WorkflowError

ACCOUNT = "student@school.example"
TYPED = "typed-test-value-not-a-password"


def jpeg(width, height):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


def accounts_body(*emails, valid=1):
    return json.dumps(
        [
            "gaia.l.a.r",
            [
                [
                    "gaia.l.a",
                    1,
                    "Name",
                    email,
                    "photo",
                    valid,
                    1,
                    0,
                    None,
                    1,
                    "1",
                    None,
                    None,
                    None,
                    0,
                ]
                for email in emails
            ],
        ]
    ).encode()


class FakePipe:
    def __init__(self, owner):
        self.owner = owner
        self.calls = []

    def call(self, method, params=None, session=None, timeout=15.0):
        self.calls.append((method, params or {}, session))
        if method == "Page.getNavigationHistory":
            return {"currentIndex": 0, "entries": [{"id": 1, "url": "about:blank"}]}
        return {}

    def send(self, method, params=None, session=None, **_):
        self.calls.append((method, params or {}, session))
        return 1


class FakeChromium:
    """What SchoolBrowser needs from Chromium, recorded."""

    launched = []

    def __init__(self, scale=1):
        self.scale = scale
        self.pages = {}
        self.blocked = 0
        self.login_wall = False
        self.blocked_navigation = ""
        self.sign_in_page = None
        self.mode = None
        self._lock = threading.RLock()
        self.pipe = FakePipe(self)
        self.closed = False
        self.on_frame = self.on_target = None
        self.body_value = accounts_body(ACCOUNT)
        self.documents = [("doc-1", "https://accounts.google.com/ListAccounts", 200)]
        self.focus = {"password": False, "editable": True, "hint": ""}
        self.modes = []
        FakeChromium.launched.append(self)

    def alive(self):
        return not self.closed

    def close(self):
        self.closed = True

    def set_mode(self, mode, read_url=""):
        self.mode, self.read_url = mode, read_url
        self.modes.append((mode, read_url))
        self.login_wall = False

    def open_page(self, width=1280, height=900, touch=False, scale=None):
        page = sb._Page(f"T{len(self.pages)}", f"S{len(self.pages)}")
        page.ready.set()
        self.pages[page.target_id] = page
        return page

    def navigate(self, page, url, timeout=30.0):
        page.url = url
        page.documents = list(self.documents)
        page.lifecycle.add(("L1", "load"))
        return {"loaderId": "L1"}

    def close_page(self, page):
        self.pages.pop(page.target_id, None)

    def evaluate(self, page, expression, timeout=10.0):
        if expression == sb.FOCUS_SCRIPT:
            return dict(self.focus)
        if expression == sb.IMAGES_DONE:
            return True
        if expression == sb.IMAGES_SCRIPT:
            return []
        return None

    def body(self, page, request_id):
        return self.body_value

    def wait_finished(self, page, request_id, timeout=5.0):
        return True


@pytest.fixture
def browser(tmp_path, monkeypatch):
    FakeChromium.launched = []
    profile = tmp_path / "caw-test-unit"
    manager = sb.SchoolBrowser(tmp_path / "data", profile=profile)
    monkeypatch.setattr(manager, "_launch", lambda scale=1: FakeChromium(scale))
    yield manager
    manager.shutdown()


def frame(manager, width=780, height=1688, css=(390, 844)):
    session = manager._session
    manager._frame(
        session.page.session,
        {
            "data": __import__("base64").b64encode(jpeg(width, height)).decode(),
            "metadata": {"deviceWidth": css[0], "deviceHeight": css[1]},
        },
    )


def test_phone_session_starts_at_scale_two_and_maps_taps_to_css_pixels(browser):
    status = browser.start(width=390, height=844, touch=True)
    chromium = FakeChromium.launched[-1]
    assert chromium.scale == 2 and chromium.mode == guard.SIGN_IN
    assert status["session"]["active"] and status["state"] == "signed_out"
    screencast = next(c for c in chromium.pipe.calls if c[0] == "Page.startScreencast")
    assert screencast[1]["maxWidth"] == 780 and screencast[1]["maxHeight"] == 1688
    navigate = next(c for c in chromium.pipe.calls if c[0] == "Page.navigate")
    assert navigate[1]["url"].startswith("https://accounts.google.com/ServiceLogin?")
    assert "continue=https%3A%2F%2Fdocs.google.com%2Fforms%2F" in navigate[1]["url"]
    frame(browser)
    value = browser.frame(after=0, timeout=1)
    assert value["seq"] == 1 and value["changed"] and value["jpeg"][:2] == b"\xff\xd8"
    assert (value["image_width"], value["image_height"], value["width"]) == (780, 1688, 390)
    browser.input({"type": "tap", "x": 390, "y": 844})
    touch = [c for c in chromium.pipe.calls if c[0] == "Input.dispatchTouchEvent"]
    assert touch[0][1]["touchPoints"] == [{"x": 195.0, "y": 422.0}]
    assert touch[1][1]["type"] == "touchEnd"


def test_desktop_session_clicks_with_the_mouse_and_restarts_a_phone_scaled_browser(browser):
    browser.start(width=390, height=844, touch=True)
    browser.cancel()
    browser.start(width=1280, height=900, touch=False)
    assert [c.scale for c in FakeChromium.launched] == [2, 1]
    assert FakeChromium.launched[0].closed
    frame(browser, 1280, 900, (1280, 900))
    browser.input({"type": "tap", "x": 100, "y": 50})
    calls = [
        c[1]["type"]
        for c in FakeChromium.launched[-1].pipe.calls
        if c[0] == "Input.dispatchMouseEvent"
    ]
    assert calls == ["mouseMoved", "mousePressed", "mouseReleased"]


def test_typed_text_goes_to_the_page_only(browser, tmp_path):
    browser.start(width=390, height=844, touch=True)
    frame(browser)
    assert browser.input({"type": "text", "text": TYPED}) == {"ok": True}
    chromium = FakeChromium.launched[-1]
    assert (
        "Input.insertText",
        {"text": TYPED},
        browser._session.page.session,
    ) in chromium.pipe.calls
    for key in ("Enter", "Backspace", "Tab", "Escape", "ArrowLeft", "ArrowDown"):
        browser.input({"type": "key", "key": key})
    enter = next(c for c in chromium.pipe.calls if c[0] == "Input.dispatchKeyEvent")
    assert enter[1]["key"] == "Enter" and enter[1]["text"] == "\r"
    status = json.dumps(browser.status())
    value = browser.frame(after=0, timeout=0)
    assert (
        TYPED not in status
        and TYPED.encode()
        not in json.dumps({k: v for k, v in value.items() if k != "jpeg"}).encode()
    )
    assert not any(
        TYPED.encode() in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )


@pytest.mark.parametrize(
    "action, message",
    [
        ({"type": "eval", "code": "x"}, "不支持的操作"),
        ({"type": "text", "text": "a", "extra": 1}, "不支持的参数"),
        ({"type": "text", "text": ""}, "为空或太长"),
        ({"type": "text", "text": "x" * 2001}, "为空或太长"),
        ({"type": "text", "text": "line\nbreak"}, "控制字符"),
        ({"type": "key", "key": "F12"}, "不支持的按键"),
        ({"type": "key", "key": "Enter", "ctrl": True}, "不支持的参数"),
        ({"type": "tap", "x": 9999, "y": 1}, "画面以外"),
        ({"type": "tap", "x": "1", "y": 1}, "点按位置无效"),
        ({"type": "scroll", "x": 1, "y": 1, "dy": "down"}, "滚动距离无效"),
        ("tap", "不支持的操作"),
    ],
)
def test_input_is_validated(browser, action, message):
    browser.start(width=390, height=844, touch=True)
    frame(browser)
    with pytest.raises(WorkflowError, match=message):
        browser.input(action)


def test_tap_before_any_frame_is_refused(browser):
    browser.start(width=390, height=844, touch=True)
    with pytest.raises(WorkflowError, match="还没有出现"):
        browser.input({"type": "tap", "x": 1, "y": 1})


def test_one_session_at_a_time_and_no_read_during_it(browser, tmp_path):
    browser.start(width=390, height=844, touch=True)
    with pytest.raises(Busy):
        browser.start(width=390, height=844, touch=True)
    with pytest.raises(Busy):
        browser.read_page("https://docs.google.com/forms/d/e/x0000000/viewform", tmp_path / "c")
    browser.cancel()
    with pytest.raises(WorkflowError, match="已经关闭"):
        browser.frame(after=0, timeout=0)
    browser._reading = True
    with pytest.raises(Busy):
        browser.start(width=390, height=844, touch=True)
    with pytest.raises(Busy):
        browser.logout()


def test_finish_records_only_the_verified_account(browser):
    browser.start(width=390, height=844, touch=True)
    status = browser.finish()
    path = browser.data_dir / sb.STATE_FILE
    record = json.loads(path.read_text())
    assert set(record) == {"email", "verified_at", "state", "verified_by"}
    assert record["email"] == ACCOUNT and record["state"] == "signed_in"
    assert record["verified_by"] == "accounts_list"
    assert path.stat().st_mode & 0o777 == 0o600
    assert status["state"] == "signed_in" and status["session"] is None
    assert sb.verified_email(browser.data_dir) == ACCOUNT


@pytest.mark.parametrize(
    "body, school_email, message",
    [
        (accounts_body(), "", "还没有登录完成"),
        (accounts_body(ACCOUNT, valid=0), "", "还没有登录完成"),
        (accounts_body("other@school.example"), ACCOUNT, "不一致"),
        (accounts_body(ACCOUNT, "other@gmail.example"), "", "多个 Google 账号"),
    ],
)
def test_finish_refuses_what_it_cannot_verify(browser, body, school_email, message):
    browser.school_email = school_email
    browser.start(width=390, height=844, touch=True)
    FakeChromium.launched[-1].body_value = body
    with pytest.raises(WorkflowError, match=message):
        browser.finish()
    assert not (browser.data_dir / sb.STATE_FILE).exists()
    assert browser.status()["session"]["active"]


def test_school_email_picks_its_account_among_several(browser):
    browser.school_email = ACCOUNT
    browser.start(width=1280, height=900, touch=False)
    FakeChromium.launched[-1].body_value = accounts_body("other@gmail.example", ACCOUNT)
    assert browser.finish()["email"] == ACCOUNT


def test_changed_account_list_format_is_used_but_marked_uncertain(browser):
    browser.start(width=1280, height=900, touch=False)
    FakeChromium.launched[-1].body_value = b'{"accounts": [{"email": "Student@School.example"}]}'
    status = browser.finish()
    assert status["email"] == ACCOUNT and status["uncertain"] is True


def test_session_ends_without_input_or_polling_and_idle_browser_stops(browser):
    browser.start(width=390, height=844, touch=True)
    chromium = FakeChromium.launched[-1]
    session = browser._session
    browser._tick(session.last_input + sb.SESSION_INPUT_SECONDS + 1)
    assert browser._session is None and session.ended == "idle"
    with pytest.raises(WorkflowError, match="已经关闭"):
        browser.frame(after=0, timeout=0)
    # An abandoned session stops the browser at the next tick, not after the idle time.
    browser._tick()
    assert browser._browser is None and chromium.closed
    browser.start(width=390, height=844, touch=True)
    session = browser._session
    browser._tick(session.last_poll + sb.SESSION_POLL_SECONDS + 1)
    assert session.ended == "gone"
    browser._tick()
    assert browser._browser is None and FakeChromium.launched[-1].closed


def test_a_finished_sign_in_keeps_the_browser_for_the_next_read(browser):
    browser.start(width=390, height=844, touch=True)
    browser.finish()
    browser._tick()
    assert browser._browser is not None and not FakeChromium.launched[-1].closed
    browser._tick(time.monotonic() + browser.idle_seconds + 1)
    assert browser._browser is None and FakeChromium.launched[-1].closed


def test_frame_long_poll_wakes_on_a_new_frame(browser):
    browser.start(width=390, height=844, touch=True)
    timer = threading.Timer(0.2, lambda: frame(browser))
    timer.start()
    started = time.monotonic()
    value = browser.frame(after=0, timeout=5)
    assert value["seq"] == 1 and time.monotonic() - started < 3
    unchanged = browser.frame(after=1, timeout=0.1)
    assert unchanged["changed"] is False and unchanged["seq"] == 1


def test_password_and_refusal_hints_reach_the_frame(browser):
    browser.start(width=390, height=844, touch=True)
    chromium = FakeChromium.launched[-1]
    frame(browser)
    chromium.focus = {"password": True, "editable": True, "hint": "not_secure"}
    browser._session.focus_at = 0
    value = browser.frame(after=0, timeout=0)
    assert value["password"] is True and value["hint"] == "not_secure"
    browser._session.page.url = "https://accounts.google.com/v3/signin/challenge/pk?TL=x"
    chromium.focus = {"password": False, "editable": False, "hint": "evil<script>"}
    browser._session.focus_at = 0
    value = browser.frame(after=0, timeout=0)
    assert value["hint"] == "passkey" and value["host"] == "accounts.google.com"


def test_read_redirected_to_sign_in_marks_the_login_expired(browser, tmp_path):
    sb._write_record(
        browser.data_dir,
        {"email": ACCOUNT, "verified_at": "2026-11-06T00:00:00+00:00", "state": "signed_in"},
    )

    def walled(page, url, timeout=30.0):
        browser._browser.login_wall = True
        return {"loaderId": "L1", "errorText": "net::ERR_BLOCKED_BY_CLIENT"}

    browser._ensure()
    browser._browser.navigate = walled
    with pytest.raises(sb.LoginExpired, match="设置 → 学校账号"):
        browser.read_page("https://docs.google.com/forms/d/e/x0000000/viewform", tmp_path / "c")
    assert sb.login_record(browser.data_dir)["state"] == "expired"
    assert browser.status()["state"] == "expired" and not browser._reading
    assert browser._browser.mode is None


def test_read_returns_the_original_document_body(browser, tmp_path):
    url = "https://docs.google.com/forms/d/e/x0000000/viewform"
    browser._ensure()
    chromium = browser._browser
    chromium.documents = [("doc-1", url + "?usp=x", 200)]
    chromium.body_value = b"<html>original</html>"
    page = browser.read_page(url, tmp_path / "c")
    assert page["html"] == "<html>original</html>" and page["images"] == []
    assert chromium.modes == [(guard.READ, url), (None, "")]
    chromium.documents = [("doc-2", "https://docs.google.com/forms/d/e/other000/viewform", 200)]
    with pytest.raises(WorkflowError, match="没有返回题目"):
        browser.read_page(url, tmp_path / "c")


def test_closed_or_redirected_form_says_so():
    assert "已关闭" in sb._not_opened("https://docs.google.com/forms/d/e/x/closedform", {})
    assert "example.org" in sb._not_opened("https://example.org/x", {})
    assert "没有回应" in sb._not_opened("", {})


def test_logout_deletes_profile_and_login(browser):
    browser.profile.mkdir(parents=True)
    (browser.profile / "Default").mkdir()
    (browser.profile / "Default" / "Preferences").write_text("{}")
    sb._write_record(
        browser.data_dir,
        {"email": ACCOUNT, "verified_at": "2026-11-06T00:00:00+00:00", "state": "signed_in"},
    )
    browser.start(width=390, height=844, touch=True)
    status = browser.logout()
    assert status["state"] == "signed_out" and status["session"] is None
    assert not browser.profile.exists() and not (browser.data_dir / sb.STATE_FILE).exists()
    assert FakeChromium.launched[-1].closed


def test_profile_deletion_refuses_unexpected_directories(tmp_path):
    important = tmp_path / "Documents"
    important.mkdir()
    assert "无效" in sb.delete_profile(important, "/snap/bin/chromium")
    assert important.exists()
    assert sb.delete_profile(tmp_path / "caw-test-missing", "/snap/bin/chromium") == ""


@pytest.mark.parametrize("name", ["Documents", "classroomautowork", "caw-test-x/../y", "."])
def test_profile_path_must_be_named_for_this_use(tmp_path, name):
    with pytest.raises(WorkflowError, match="资料目录"):
        sb.profile_path(tmp_path / name)


def test_profile_path_default_is_the_snap_common_directory(monkeypatch):
    monkeypatch.delenv(sb.PROFILE_ENV, raising=False)
    path = sb.profile_path()
    assert path.parts[-4:] == ("snap", "chromium", "common", "classroomautowork-school")


def test_test_hook_accepts_only_local_redirection_arguments(monkeypatch):
    monkeypatch.setenv(
        sb.TEST_ARGS_ENV,
        json.dumps(["--host-resolver-rules=MAP * 127.0.0.1", "--ignore-certificate-errors"]),
    )
    assert len(sb.test_args()) == 2
    for bad in (
        ["--remote-debugging-port=9222"],
        ["--user-data-dir=/tmp/x"],
        "x",
        ["--load-extension=/x"],
    ):
        monkeypatch.setenv(sb.TEST_ARGS_ENV, json.dumps(bad))
        with pytest.raises(WorkflowError):
            sb.test_args()
    monkeypatch.delenv(sb.TEST_ARGS_ENV)
    assert sb.test_args() == []


@pytest.mark.parametrize(
    "body, expected",
    [
        (accounts_body("A@School.example"), (["a@school.example"], True)),
        (")]}'\n" + accounts_body(ACCOUNT).decode(), ([ACCOUNT], True)),
        (accounts_body(ACCOUNT, valid=0), ([], True)),
        ('["gaia.l.a.r",[]]', ([], True)),
        ("<html>Signed in as student@school.example</html>", ([ACCOUNT], False)),
        ("", ([], False)),
    ],
)
def test_parse_accounts(body, expected):
    assert sb.parse_accounts(body if isinstance(body, str) else body.decode()) == expected


def test_signed_out_flag_in_account_list():
    entry = ["gaia.l.a", 1, "N", ACCOUNT, "p", 1, 1, 0, None, 1, "1", None, None, None, 1]
    assert sb.parse_accounts(json.dumps(["gaia.l.a.r", [entry]])) == ([], True)


def test_jpeg_size_and_frame_point():
    assert sb.jpeg_size(jpeg(780, 1688)) == (780, 1688)
    assert sb.jpeg_size(b"not a jpeg") == (0, 0)
    point = {"image_width": 780, "image_height": 1688, "width": 390, "height": 844}
    assert sb.frame_point(780, 0, point) == (390.0, 0.0)
    with pytest.raises(WorkflowError):
        sb.frame_point(-1, 0, point)
    with pytest.raises(WorkflowError):
        sb.frame_point(True, 0, point)


def test_user_agent_is_plain_chrome_with_matching_hints():
    override = sb.ua_override("156", "156.0.8078.47")
    assert (
        "HeadlessChrome" not in override["userAgent"]
        and "Chrome/156.0.0.0" in override["userAgent"]
    )
    assert override["acceptLanguage"].startswith("ja-JP")
    brands = override["userAgentMetadata"]["brands"]
    assert {"brand": "Chromium", "version": "156"} in brands
    assert all("Headless" not in b["brand"] for b in brands)
    assert "--disable-blink-features=AutomationControlled" in sb.FLAGS
    assert "--headless=new" in sb.FLAGS and "--no-sandbox" in sb.FLAGS
    assert "--remote-debugging-pipe" in sb.FLAGS
    assert not any(
        flag.startswith(("--remote-debugging-port", "--enable-automation")) for flag in sb.FLAGS
    )


def test_login_state_mismatch_and_bad_records(tmp_path):
    assert sb.login_state(tmp_path)["state"] == "signed_out"
    sb._write_record(tmp_path, {"email": ACCOUNT, "verified_at": "t", "state": "signed_in"})
    assert sb.login_state(tmp_path, ACCOUNT)["state"] == "signed_in"
    mismatch = sb.login_state(tmp_path, "other@school.example")
    assert mismatch["state"] == "mismatch" and mismatch["email"] == ACCOUNT
    sb.mark_expired(tmp_path)
    assert sb.login_state(tmp_path)["state"] == "expired"
    for bad in ('{"email": "x", "verified_at": "t", "state": "signed_in"}', "[]", "not json"):
        (tmp_path / sb.STATE_FILE).write_text(bad)
        assert sb.login_state(tmp_path)["state"] == "signed_out"


def test_module_never_reads_cookies_or_credential_files():
    source = Path(sb.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "Network.getCookies",
        "Network.getAllCookies",
        "Storage.getCookies",
        "Login Data",
        '"Cookies"',
        "Web Data",
    ):
        assert forbidden not in source


def test_manager_is_one_per_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(sb.PROFILE_ENV, str(tmp_path / "caw-test-manager"))
    settings = type(
        "S", (), {"data_dir": tmp_path / "d", "school_email": "", "timezone": "Asia/Tokyo"}
    )
    try:
        first = sb.manager(settings)
        assert sb.manager(settings) is first
    finally:
        sb.stop_all()
    assert sb._MANAGERS == {}


def test_read_only_snap_profile_is_removed_by_a_shell_in_the_snap(tmp_path, monkeypatch):
    """Under ProtectHome the service sees the profile read-only; the snap's own namespace can
    write it, so `snap run --shell chromium` removes it (always called as /usr/bin/snap)."""
    import shutil
    import subprocess
    from types import SimpleNamespace

    home = tmp_path / "home"
    profile = home / "snap" / "chromium" / "common" / "classroomautowork-school"
    (profile / "Default").mkdir(parents=True)
    launcher = tmp_path / "snapd"
    launcher.write_text("")
    chromium = tmp_path / "chromium"
    chromium.symlink_to(launcher)
    monkeypatch.setattr(sb, "SNAP", str(launcher))
    monkeypatch.setattr(sb.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(home)))
    remove = shutil.rmtree

    def read_only(_path):
        raise OSError(30, "Read-only file system")

    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        remove(profile)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(sb.shutil, "rmtree", read_only)
    monkeypatch.setattr(sb.subprocess, "run", run)
    assert sb.delete_profile(profile, str(chromium)) == ""
    assert calls == [[str(launcher), "run", "--shell", "chromium", "-c", f"rm -r -- {profile}"]]
    # Not the snap: nothing is run, and the leftover is reported.
    (profile / "Default").mkdir(parents=True)
    calls.clear()
    assert "没有删干净" in sb.delete_profile(profile, str(tmp_path / "plain-chromium"))
    assert calls == []


# --------------------------------------------------------------------------- reviewer findings


def test_a_window_open_elsewhere_can_be_reopened_here(browser):
    """F3: 在这里重新打开 closes the other window (its page is told why) and opens one here."""
    browser.start(width=390, height=844, touch=True)
    other = browser._session
    with pytest.raises(Busy, match="可以在这里重新打开") as caught:
        browser.start(width=390, height=844, touch=True)
    assert "约 2 分钟后" in str(caught.value)
    browser.start(width=390, height=844, touch=True, takeover=True)
    assert browser._session is not other and other.ended == "replaced"
    assert "别处重新打开" in sb._ENDED["replaced"]
    browser._reading = True
    with pytest.raises(Busy, match="读取表单"):
        browser.start(width=390, height=844, touch=True, takeover=True)
    assert browser._session is not None
    browser._reading = False


def test_no_logout_while_the_window_is_opening(browser):
    """SEC-4: a logout during start() would delete the profile under a starting browser."""
    browser._opening = True
    with pytest.raises(Busy, match="正在打开"):
        browser.logout()
    browser._opening = False


@pytest.mark.parametrize(
    "body, school_email",
    [
        (accounts_body("other@school.example"), ACCOUNT),
        (accounts_body(ACCOUNT, "other@gmail.example"), ""),
    ],
)
def test_a_refused_account_is_signed_out_and_the_saved_login_forgotten(browser, body, school_email):
    """F7: nothing of a refused account stays for a later read: Google's Logout runs in the
    hidden tab, the saved login is removed, and the view goes back to the sign-in."""
    sb._write_record(
        browser.data_dir,
        {"email": ACCOUNT, "verified_at": "2026-11-06T00:00:00+00:00", "state": "signed_in"},
    )
    browser.school_email = school_email
    browser.start(width=390, height=844, touch=True)
    chromium = FakeChromium.launched[-1]
    chromium.body_value = body
    visited = []
    navigate = chromium.navigate
    chromium.navigate = lambda page, url, timeout=30.0: visited.append(url) or navigate(page, url)
    with pytest.raises(WorkflowError, match="已全部退出"):
        browser.finish()
    assert visited == [sb.ACCOUNTS_URL, sb.LOGOUT_URL]
    assert not (browser.data_dir / sb.STATE_FILE).exists()
    assert browser.status()["state"] == "signed_out" and browser.status()["session"]["active"]
    again = [c for c in chromium.pipe.calls if c[0] == "Page.navigate"][-1]
    assert again[1]["url"] == sb.sign_in_url(school_email)
    assert again[2] == browser._session.page.session


def test_a_short_phone_view_is_not_stretched(browser):
    """F9: the remote height follows a short view down to 300 CSS px."""
    browser.start(width=358, height=330, touch=True)
    assert (browser._session.width, browser._session.height) == (358, 330)
    browser.cancel()
    browser.start(width=358, height=120, touch=True)
    assert browser._session.height == 300


def test_a_focus_change_without_a_new_picture_answers_the_poll(browser, monkeypatch):
    """F10c: a field that lost focus on a page that does not repaint updates soon."""
    monkeypatch.setattr(sb, "FOCUS_RECHECK_SECONDS", 0.05)
    browser.start(width=390, height=844, touch=True)
    chromium = FakeChromium.launched[-1]
    frame(browser)
    first = browser.frame(after=0, timeout=0)
    assert first["editable"] is True
    chromium.focus = {"password": False, "editable": False, "hint": ""}
    started = time.monotonic()
    value = browser.frame(after=first["seq"], timeout=5)
    assert value["editable"] is False and value["changed"] is False
    assert time.monotonic() - started < 2


def test_the_focused_fields_position_is_passed_on_checked(browser):
    """F1: where the focused field is (CSS px) so the phone keeps it above the keyboard; an
    odd value from the page is dropped."""
    browser.start(width=390, height=844, touch=True)
    chromium = FakeChromium.launched[-1]
    frame(browser)
    chromium.focus = {"password": True, "editable": True, "hint": "", "top": 300, "bottom": 348}
    browser._session.focus_at = 0
    value = browser.frame(after=0, timeout=0)
    assert (value["top"], value["bottom"]) == (300, 348)
    chromium.focus = {"password": True, "editable": True, "hint": "", "top": "x", "bottom": 99999}
    browser._session.focus_at = 0
    value = browser.frame(after=0, timeout=0)
    assert (value["top"], value["bottom"]) == (-1, -1)


def test_end_messages_name_the_place_not_a_button_label():
    """F10d: the button reads 登录 or 重新登录, so the messages say where to start again."""
    for why, text in sb._ENDED.items():
        assert "重新点“登录”" not in text, why
    assert "设置 → 学校账号" in sb._ENDED["idle"]


def test_orphaned_chromium_of_the_profile_is_found_exactly(tmp_path):
    """SEC-6: a Chromium left on this profile by a manager that died is killed before a new one
    starts; only an exact --user-data-dir match counts."""
    proc = tmp_path / "proc"
    profile = Path("/root/snap/chromium/common/caw-test-unit")
    rows = {
        "101": ["/snap/chromium/x/chrome", "--headless=new", f"--user-data-dir={profile}"],
        "102": ["/snap/chromium/x/chrome", "--type=renderer", f"--user-data-dir={profile}"],
        "103": ["/snap/chromium/x/chrome", f"--user-data-dir={profile}2"],
        "104": ["python3", "shot.py"],
        "self": ["ignored"],
    }
    for pid, argv in rows.items():
        (proc / pid).mkdir(parents=True)
        (proc / pid / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
    assert sorted(sb.orphans(profile, proc)) == [101, 102]


def test_the_chromium_version_is_read_again_for_a_new_browser(monkeypatch):
    """C13c: a snap refresh changes the version under the same path."""
    outputs = iter(["Chromium 156.0.7000.1 snap", "Chromium 157.0.7100.2 snap"])
    monkeypatch.setattr(
        sb.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": next(outputs)})()
    )
    monkeypatch.setattr(sb, "_VERSIONS", {})
    assert sb.chromium_version("/x/chromium") == ("156", "156.0.7000.1")
    assert sb.chromium_version("/x/chromium") == ("156", "156.0.7000.1")
    assert sb.chromium_version("/x/chromium", fresh=True) == ("157", "157.0.7100.2")


def test_an_image_body_is_read_once_it_has_fully_arrived():
    """A picture's bytes are asked for only after Network.loadingFinished (asking earlier,
    right after the load event, can get nothing: a flaky gap seen in the harness)."""
    page = sb._Page("T1", "S1")
    assert sb.Chromium.wait_finished(None, page, "r1", timeout=0.05) is False

    def arrive():
        with page.cond:
            page.finished.add("r1")
            page.cond.notify_all()

    threading.Timer(0.1, arrive).start()
    assert sb.Chromium.wait_finished(None, page, "r1", timeout=3) is True
