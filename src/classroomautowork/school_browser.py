"""The school-login browser: one headless Chromium that keeps the student's Google login.

Some Google Forms only open for the school account. The user signs in once, from the 作业助手
page, through a remote view of this browser (a screencast plus taps, text and a few keys). The
browser then reads those forms for the program: a GET of the canonical viewform page, nothing
clicked or typed. It cannot submit anything: every request of every target goes through
`browser_guard.allowed` (see there), at the browser level and again on each target, which is
held at start until its own interception is on.

Only Chromium uses the login. This module never reads cookies or credential files, never logs,
stores or returns what the user types, and stores only `school-login.json` (the verified email,
when it was verified and whether the login still works). It reads page HTML, the main
document's response body, image response bodies and the page URL.

One Chromium per profile runs at a time (an abstract-socket lock, so two processes cannot share
the profile), started on demand and stopped about three minutes after its last use and when the
service stops. As root the snap only starts with --no-sandbox; it runs new headless with the
normal Chrome user agent and no automation flags.
"""

import atexit
import base64
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from . import browser_guard as guard
from .errors import Busy, PermissionDenied, WorkflowError
from .local import atomic_json, require_private_path, utc_now

CHROMIUM_ENV = "CLASSROOMAUTOWORK_CHROMIUM"
PROFILE_ENV = "CLASSROOMAUTOWORK_SCHOOL_PROFILE"
# Test and staging hook only (never a setting): a JSON list of extra Chromium arguments, limited
# to pointing Google's hosts at a local fake.
TEST_ARGS_ENV = "CLASSROOMAUTOWORK_SCHOOL_BROWSER_TEST_ARGS"
TEST_ARG_PREFIXES = (
    "--host-resolver-rules=",
    "--ignore-certificate-errors",
    "--proxy-server=",
    "--proxy-bypass-list=",
)
DEFAULT_CHROMIUM = "/snap/bin/chromium"
SNAP = "/usr/bin/snap"
PROFILE_NAME = re.compile(r"(?:classroomautowork-school|caw-test-)[A-Za-z0-9._-]{0,80}")
STATE_FILE = "school-login.json"
IDLE_SECONDS = 180
SESSION_INPUT_SECONDS = 600
# Long enough for a phone to switch to the authenticator app (or the SMS) for a 2-step code and
# back: the page polls again when it is shown.
SESSION_POLL_SECONDS = 120
SESSION_MAX_SECONDS = 1800
READ_SECONDS = 60
SIGN_IN_URL = "https://accounts.google.com/ServiceLogin"
CONTINUE_URL = "https://docs.google.com/forms/"
ACCOUNTS_URL = (
    "https://accounts.google.com/ListAccounts?gpsia=1&source=ChromiumBrowser&json=standard"
)
# Google answers a GET of the account list with 400; Chromium itself POSTs it (a body of one
# space). So 完成 opens a small page of accounts.google.com in the hidden tab and posts the list
# request from there (same origin, in an isolated world). Sign-in mode lets a POST reach Google's
# sign-in host only; read mode refuses it.
ACCOUNTS_PAGE = "https://accounts.google.com/robots.txt"
ACCOUNTS_LIMIT = 65536
# Signs every Google account of this browser out (a GET, allowed in sign-in mode): after an
# account 完成 refused, nothing of it stays in the profile for a later read to use.
LOGOUT_URL = "https://accounts.google.com/Logout?hl=ja"
# How often a waiting frame long-poll looks at the page's focus again, so a field that lost
# focus on a page that does not repaint still updates the status line soon.
FOCUS_RECHECK_SECONDS = 1.5
ACCEPT_LANGUAGE = "ja-JP,ja,en-US,en"
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_IMAGES = 40
MAX_TEXT = 2000
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")
KEYS = {
    "Enter": ("Enter", 13, "\r"),
    "Backspace": ("Backspace", 8, ""),
    "Tab": ("Tab", 9, ""),
    "Escape": ("Escape", 27, ""),
    "ArrowLeft": ("ArrowLeft", 37, ""),
    "ArrowUp": ("ArrowUp", 38, ""),
    "ArrowRight": ("ArrowRight", 39, ""),
    "ArrowDown": ("ArrowDown", 40, ""),
}
# A second, independent net on every page: Chromium refuses these before any request leaves
# (WebSockets too, which Fetch does not see). Its wildcards match the literal spelling only (two
# letter cases); every other spelling (percent-encoded, mixed case) is the Fetch guard's
# (browser_guard.submits decodes the path). The harness empties it to prove Fetch alone holds.
BLOCKED_URL_PATTERNS = ["*formResponse*", "*formresponse*"]
# Targets that serve their own requests. Dedicated workers and worklets have no Fetch domain:
# their requests go through the page that owns them, checked there and at the browser level.
OWNED_BY_PAGE = {"worker", "worklet", "auction_worklet", "shared_storage_worklet"}
FLAGS = [
    "--headless=new",
    "--no-sandbox",
    "--remote-debugging-pipe",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--disable-breakpad",
    "--no-pings",
    "--safebrowsing-disable-auto-update",
    "--deny-permission-prompts",
    # Chromium's own background requests (not the pages'): translation, hints, media router,
    # autofill, the AI-mode eligibility check (AimEnabled: GET www.google.com/async/folae) and the
    # component updater (its on-demand checks also run with --disable-component-update; pointed
    # at a closed local port it sends nothing). None of these is needed to sign in or to read.
    "--disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,"
    "AutofillServerCommunication,CertificateTransparencyComponentUpdater,"
    "InterestFeedContentSuggestions,PrivacySandboxSettings4,AimEnabled",
    "--component-updater=url-source=http://127.0.0.1:9/update",
    "--password-store=basic",
    "--use-mock-keychain",
    "--mute-audio",
    "--hide-scrollbars",
    "--force-color-profile=srgb",
    "--window-size=1280,900",
    "--lang=ja-JP",
    "--accept-lang=" + ACCEPT_LANGUAGE,
]
# Runs in the page's isolated world: whether the focused field is a password field and whether
# Google refuses this browser or asks for a passkey. Only these flags leave the page.
FOCUS_SCRIPT = r"""(() => {
  let e = document.activeElement;
  try {
    for (let i = 0; i < 8 && e; i++) {
      if (e.shadowRoot && e.shadowRoot.activeElement) { e = e.shadowRoot.activeElement; continue; }
      if ((e.tagName === 'IFRAME' || e.tagName === 'FRAME') && e.contentDocument) {
        e = e.contentDocument.activeElement; continue;
      }
      break;
    }
  } catch (x) {}
  const password = !!e && e.tagName === 'INPUT' && String(e.type || '').toLowerCase() === 'password';
  const editable = !!e && (e.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName));
  // Where the focused field is in the viewport (CSS px, main frame only): the phone's view keeps
  // it in sight above the on-screen keyboard. A position, never the field's value.
  let top = -1, bottom = -1;
  if (editable && e.ownerDocument === document) {
    const r = e.getBoundingClientRect();
    top = Math.round(r.top); bottom = Math.round(r.bottom);
  }
  const text = String((document.body && document.body.innerText) || '').slice(0, 20000);
  let hint = '';
  if (/安全でない可能性|may not be secure|可能不安全/i.test(text)) hint = 'not_secure';
  else if (/パスキーを使用して|パスキーで本人確認|use your passkey|使用您的通行密钥/i.test(text)) hint = 'passkey';
  return {password, editable, hint, top, bottom};
})()"""
IMAGES_SCRIPT = r"""(() => {
  const ids = (node) => {
    const found = [];
    for (let n = node; n && n.getAttribute; n = n.parentElement) {
      const direct = n.getAttribute('data-item-id');
      if (direct && /^\d{1,20}$/.test(direct)) found.push(direct);
      const params = n.getAttribute('data-params');
      if (params) {
        const m = /^%\.@\.\[(\d{1,20}),/.exec(params);
        if (m) found.push(m[1]);
      }
    }
    return found;
  };
  return Array.from(document.images).slice(0, 200).map((img, index) => {
    const r = img.getBoundingClientRect();
    return {
      index, url: String(img.currentSrc || img.src || ''),
      natural_width: img.naturalWidth, natural_height: img.naturalHeight,
      width: Math.round(r.width), height: Math.round(r.height),
      attr_width: parseInt(img.getAttribute('width') || '0', 10) || 0,
      attr_height: parseInt(img.getAttribute('height') || '0', 10) || 0,
      complete: img.complete, item_ids: ids(img),
      in_list: !!img.closest('[role="list"]'),
    };
  });
})()"""
IMAGES_DONE = "Array.from(document.images).every((i) => i.complete)"
ACCOUNTS_SCRIPT = (
    "(async () => {\n"
    f"  const response = await fetch({json.dumps(ACCOUNTS_URL)}, {{\n"
    "    method: 'POST', body: ' ', credentials: 'include',\n"
    "    headers: {'Content-Type': 'application/x-www-form-urlencoded'},\n"
    "  });\n"
    f"  return [response.status, (await response.text()).slice(0, {ACCOUNTS_LIMIT})];\n"
    "})()"
)

_MANAGERS = {}
_MANAGERS_LOCK = threading.Lock()
_VERSIONS = {}


class CDPError(WorkflowError):
    """A DevTools command failed; the message names the method, never its parameters."""


class LoginExpired(PermissionDenied):
    pass


# --------------------------------------------------------------------------- state file


def state_path(data_dir: Path) -> Path:
    return require_private_path(Path(data_dir) / STATE_FILE)


def login_record(data_dir: Path) -> dict | None:
    """{email, verified_at, state} or None. Anything unexpected reads as no login."""
    path = state_path(data_dir)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("email"), str)
        or not EMAIL.fullmatch(record["email"])
        or record.get("state") not in {"signed_in", "expired"}
        or not isinstance(record.get("verified_at"), str)
    ):
        return None
    return record


def _write_record(data_dir: Path, record: dict) -> None:
    path = state_path(data_dir)
    atomic_json(path, record)
    os.chmod(path, 0o600)


def mark_expired(data_dir: Path) -> None:
    record = login_record(data_dir)
    if record and record["state"] != "expired":
        _write_record(data_dir, {**record, "state": "expired", "expired_at": utc_now()})


def verified_email(data_dir: Path) -> str:
    record = login_record(data_dir)
    return record["email"] if record else ""


def login_state(data_dir: Path, school_email: str = "") -> dict:
    """What 设置 → 学校账号 shows: signed_out, signed_in, expired or mismatch."""
    record = login_record(data_dir)
    if not record:
        return {"state": "signed_out", "email": "", "verified_at": None}
    state = record["state"]
    if school_email and record["email"] != school_email.lower():
        state = "mismatch"
    return {
        "state": state,
        "email": record["email"],
        "verified_at": record["verified_at"],
        **({"school_email": school_email.lower()} if state == "mismatch" else {}),
        **({"uncertain": True} if record.get("verified_by") == "page_text" else {}),
    }


# --------------------------------------------------------------------------- pure helpers


def parse_accounts(body: str) -> tuple[list[str], bool]:
    """Signed-in emails from Google's ListAccounts body, and whether the format was recognised.

    Known format: ["gaia.l.a.r", [["gaia.l.a", 1, name, email, photo, valid, …, signed_out at 14]]].
    When it changes, every email-looking string counts, marked as not certain.
    """
    text = (body or "").strip()
    if text.startswith(")]}'"):
        text = text[4:].lstrip()
    try:
        value = json.loads(text)
        if (
            isinstance(value, list)
            and len(value) >= 2
            and value[0] == "gaia.l.a.r"
            and isinstance(value[1], list)
        ):
            emails = []
            for account in value[1]:
                if not isinstance(account, list) or len(account) < 4:
                    raise ValueError
                email = account[3]
                if not isinstance(email, str) or not EMAIL.fullmatch(email):
                    raise ValueError
                valid = not (len(account) > 5 and account[5] == 0)
                signed_out = len(account) > 14 and account[14] == 1
                if valid and not signed_out:
                    emails.append(email.lower())
            return list(dict.fromkeys(emails)), True
    except (ValueError, TypeError):
        pass
    return list(dict.fromkeys(match.lower() for match in EMAIL.findall(text))), False


def google_picture(url: str) -> bool:
    """A picture Google serves for a form: from googleusercontent.com (public forms), or from
    docs.google.com/forms-images… (what a signed-in view of a form loads its pictures from)."""
    host = guard.host_of(url)
    if host == "googleusercontent.com" or host.endswith(".googleusercontent.com"):
        return True
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    return host == "docs.google.com" and path.startswith("/forms-images")


def jpeg_size(data: bytes) -> tuple[int, int]:
    """Width and height from a JPEG's frame header, or (0, 0)."""
    index = 2
    if data[:2] != b"\xff\xd8":
        return 0, 0
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xD8, 0x01} or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = int.from_bytes(data[index + 2 : index + 4], "big")
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            return width, height
        index += 2 + length
    return 0, 0


def frame_point(x, y, frame: dict) -> tuple[float, float]:
    """A tap at (x, y) in the frame image's pixels, as CSS pixels of the page viewport."""
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (x, y)):
        raise WorkflowError("点按位置无效。")
    image_w, image_h = frame.get("image_width") or 0, frame.get("image_height") or 0
    css_w, css_h = frame.get("width") or 0, frame.get("height") or 0
    if not (image_w and image_h and css_w and css_h):
        raise WorkflowError("登录画面还没有出现，请稍等再点。")
    if not (0 <= x <= image_w and 0 <= y <= image_h):
        raise WorkflowError("点按位置在画面以外。")
    return x * css_w / image_w, y * css_h / image_h


def clean_text(value) -> str:
    """Text the user sends to the sign-in page: printable, at most 2000 characters."""
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
        raise WorkflowError("要发送的文字为空或太长（最多 2000 个字符）。")
    if re.search(r"[\x00-\x1f\x7f]", value):
        raise WorkflowError("要发送的文字含有控制字符；换行请用“回车”。")
    return value


def sign_in_url(login_hint: str = "") -> str:
    params = [("continue", CONTINUE_URL), ("service", "wise"), ("hl", "ja")]
    if login_hint:
        params.append(("login_hint", login_hint))
    return SIGN_IN_URL + "?" + urlencode(params)


def user_agent(major: str) -> str:
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def ua_override(major: str, full_version: str) -> dict:
    return {
        "userAgent": user_agent(major),
        "acceptLanguage": ACCEPT_LANGUAGE,
        "platform": "Linux x86_64",
        "userAgentMetadata": {
            "brands": [
                {"brand": "Chromium", "version": major},
                {"brand": "Not=A?Brand", "version": "24"},
            ],
            "fullVersionList": [
                {"brand": "Chromium", "version": full_version},
                {"brand": "Not=A?Brand", "version": "24.0.0.0"},
            ],
            "fullVersion": full_version,
            "platform": "Linux",
            "platformVersion": "",
            "architecture": "x86",
            "bitness": "64",
            "model": "",
            "mobile": False,
        },
    }


def test_args() -> list[str]:
    raw = os.environ.get(TEST_ARGS_ENV)
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except ValueError:
        values = None
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value.startswith(TEST_ARG_PREFIXES) for value in values
    ):
        raise WorkflowError(f"{TEST_ARGS_ENV} 只接受指向本机替身的几个参数。")
    return values


def default_profile() -> Path:
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home / "snap" / "chromium" / "common" / "classroomautowork-school"


def profile_path(value=None) -> Path:
    path = Path(value or os.environ.get(PROFILE_ENV) or default_profile()).expanduser()
    if not path.is_absolute() or not PROFILE_NAME.fullmatch(path.name):
        raise WorkflowError(
            "学校账号浏览器的资料目录必须是绝对路径，名字以 classroomautowork-school 开头。"
        )
    return require_private_path(path)


def chromium_version(binary: str, *, fresh=False) -> tuple[str, str]:
    """(major, full) of the Chromium binary, from --version; `fresh` reads it again (a snap
    refresh changes it without changing the path)."""
    if fresh or binary not in _VERSIONS:
        try:
            output = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=60,
                env=_environment(),
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkflowError(
                f"这台主机上没有可用的 Chromium（{binary}），无法打开学校账号浏览器。"
            ) from exc
        match = re.search(r"\b(\d{2,4})\.(\d+)\.(\d+)\.(\d+)\b", output or "")
        if not match:
            raise WorkflowError(f"无法识别 Chromium 的版本（{binary}）。")
        _VERSIONS[binary] = (match[1], match[0])
    return _VERSIONS[binary]


def _environment(timezone: str = "Asia/Tokyo") -> dict:
    # Nothing of the service's own environment (keys, tokens) reaches Chromium.
    home = pwd.getpwuid(os.getuid()).pw_dir
    values = {
        "HOME": home,
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/snap/bin",
        "LANG": "C.UTF-8",
        "TZ": timezone or "Asia/Tokyo",
    }
    if os.environ.get("XDG_RUNTIME_DIR"):
        values["XDG_RUNTIME_DIR"] = os.environ["XDG_RUNTIME_DIR"]
    return values


# --------------------------------------------------------------------------- DevTools pipe


class _Pipe:
    """DevTools over --remote-debugging-pipe: NUL-separated JSON, one reader thread.

    Events are handed to `on_event(session, method, params)` on the reader thread, which must
    not wait for replies; `call` may be used from any other thread.
    """

    def __init__(self, read_fd, write_fd, on_event):
        self._read_fd, self._write_fd = read_fd, write_fd
        self._on_event = on_event
        self._write_lock = threading.Lock()
        self._cond = threading.Condition()
        self._next = 0
        self._replies = {}
        self._waiting = set()
        self.closed = False
        self._thread = threading.Thread(
            target=self._reader, name="school-browser-pipe", daemon=True
        )
        self._thread.start()

    def send(self, method, params=None, session=None, *, reply=False) -> int:
        with self._cond:
            if self.closed:
                raise CDPError(f"浏览器已关闭（{method}）。")
            self._next += 1
            number = self._next
            if reply:
                self._waiting.add(number)
        message = {"id": number, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        data = json.dumps(message).encode("utf-8") + b"\0"
        try:
            with self._write_lock:
                view = memoryview(data)
                while view:
                    written = os.write(self._write_fd, view)
                    view = view[written:]
        except OSError as exc:
            self._close()
            raise CDPError(f"浏览器已关闭（{method}）。") from exc
        return number

    def call(self, method, params=None, session=None, timeout=15.0) -> dict:
        number = self.send(method, params, session, reply=True)
        end = time.monotonic() + timeout
        with self._cond:
            while number not in self._replies:
                left = end - time.monotonic()
                if self.closed or left <= 0:
                    self._waiting.discard(number)
                    raise CDPError(
                        f"浏览器没有回应（{method}）。" if not self.closed else "浏览器已关闭。"
                    )
                self._cond.wait(min(left, 1.0))
            self._waiting.discard(number)
            message = self._replies.pop(number)
        if "error" in message:
            error = message["error"] or {}
            raise CDPError(f"{method}: {str(error.get('message', ''))[:200]}")
        return message.get("result") or {}

    def _reader(self):
        buffer = bytearray()
        try:
            while True:
                chunk = os.read(self._read_fd, 1 << 20)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    end = buffer.find(b"\0")
                    if end < 0:
                        break
                    raw = bytes(buffer[:end])
                    del buffer[: end + 1]
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        continue
                    if "id" in message:
                        with self._cond:
                            if message["id"] in self._waiting:
                                self._replies[message["id"]] = message
                                self._cond.notify_all()
                        continue
                    try:
                        self._on_event(
                            message.get("sessionId"),
                            message.get("method", ""),
                            message.get("params") or {},
                        )
                    except Exception:  # noqa: BLE001 - one bad event must not stop the guard
                        pass
        except OSError:
            pass
        self._close()

    def _close(self):
        with self._cond:
            self.closed = True
            self._cond.notify_all()

    def close(self):
        self._close()
        for fd in (self._read_fd, self._write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


# --------------------------------------------------------------------------- one Chromium


class _Page:
    """One page target this module opened (or a popup it follows)."""

    def __init__(self, target_id, session, opener=None):
        self.target_id = target_id
        self.session = session
        self.opener = opener
        self.url = "about:blank"
        self.cond = threading.Condition()
        self.lifecycle = set()  # (loaderId, event name) of the main frame
        self.documents = []  # (requestId, url, status) of main-frame documents
        self.responses = {}  # image url -> (requestId, mimeType, status)
        self.finished = set()  # requestIds whose body has fully arrived (Network.loadingFinished)
        self.world = None
        self.ready = threading.Event()
        self.failed = False
        self.closed = False

    def wait_load(self, loader_id, timeout, stop=lambda: False) -> bool:
        end = time.monotonic() + timeout
        with self.cond:
            while (loader_id, "load") not in self.lifecycle:
                left = end - time.monotonic()
                if left <= 0 or self.closed or stop():
                    return False
                self.cond.wait(min(left, 0.25))
        return True


class Chromium:
    """A running Chromium with the request guard on. Use through `SchoolBrowser`."""

    def __init__(self, binary, profile: Path, *, timezone="Asia/Tokyo", extra_args=(), scale=1):
        self.binary = binary
        self.profile = profile
        # Screencast frames come at the window's scale factor, not the emulated one: a phone's
        # remote view needs a browser started at 2 for sharp text.
        self.scale = 2 if scale == 2 else 1
        self.mode = None
        self.read_url = ""
        self.blocked = 0
        self.blocked_submits = 0
        self.login_wall = False
        self.emulation = None
        self.blocked_navigation = ""
        self.sign_in_page = None
        self.initial_targets = set()
        self._expecting = None
        self.pages = {}  # target id -> _Page (ours and followed popups)
        self.page_ids = set()  # every page target's id (= its main frame id)
        self.sessions = {}  # session id -> target info
        self.on_frame = None
        self.on_target = None
        self._lock = threading.RLock()
        self._tasks = []
        self._task_cond = threading.Condition()
        self.closed = False
        self.major, self.full_version = chromium_version(binary, fresh=True)
        self._lock_socket = self._acquire_profile()
        # Holding the lock, any Chromium still on this profile is an orphan (its manager died
        # without closing it, say a SIGKILL of the service): it goes before a new one starts.
        kill_orphans(self.profile)
        try:
            self._spawn(timezone, list(extra_args))
            self._worker = threading.Thread(target=self._work, name="school-browser", daemon=True)
            self._worker.start()
            self._setup()
        except BaseException:
            self.close()
            raise

    # -- process

    def _acquire_profile(self):
        key = hashlib.sha256(str(self.profile).encode()).hexdigest()[:40]
        lock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            lock.bind("\0classroomautowork-school-browser-" + key)
        except OSError:
            lock.close()
            raise Busy("另一个作业助手进程正在使用学校账号浏览器，请稍后再试。") from None
        return lock

    def _spawn(self, timezone, extra_args):
        to_child, self._write_fd = os.pipe()
        self._read_fd, from_child = os.pipe()
        # The child ends move above 9, so the dup2 to 3 and 4 below cannot overwrite each other.
        to_child, from_child = _high_fd(to_child), _high_fd(from_child)
        args = [
            self.binary,
            *FLAGS,
            "--user-agent=" + user_agent(self.major),
            f"--user-data-dir={self.profile}",
            *(["--force-device-scale-factor=2"] if self.scale == 2 else []),
            *extra_args,
            "about:blank",
        ]
        self._stderr_r, stderr_w = os.pipe()
        stderr_w = _high_fd(stderr_w)
        actions = [
            (os.POSIX_SPAWN_DUP2, to_child, 3),
            (os.POSIX_SPAWN_DUP2, from_child, 4),
            (os.POSIX_SPAWN_OPEN, 0, "/dev/null", os.O_RDONLY, 0),
            (os.POSIX_SPAWN_OPEN, 1, "/dev/null", os.O_WRONLY, 0),
            (os.POSIX_SPAWN_DUP2, stderr_w, 2),
        ]
        try:
            self.pid = os.posix_spawn(
                self.binary, args, _environment(timezone), file_actions=actions, setsid=True
            )
        except OSError as exc:
            for fd in (
                to_child,
                from_child,
                stderr_w,
                self._write_fd,
                self._read_fd,
                self._stderr_r,
            ):
                os.close(fd)
            raise WorkflowError(
                f"这台主机上没有可用的 Chromium（{self.binary}），无法打开学校账号浏览器。"
            ) from exc
        for fd in (to_child, from_child, stderr_w):
            os.close(fd)
        self._stderr = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.pipe = _Pipe(self._read_fd, self._write_fd, self._event)

    def _drain_stderr(self):
        # Kept in memory only (last lines, for a start failure); Chromium never sees typed text
        # on its command line or environment, and nothing here is written to disk.
        buffer = b""
        try:
            while chunk := os.read(self._stderr_r, 65536):
                buffer = (buffer + chunk)[-8192:]
        except OSError:
            pass
        finally:
            os.close(self._stderr_r)
        self._stderr = [
            line
            for line in buffer.decode("utf-8", "replace").splitlines()
            if line.strip() and "dbus" not in line.lower()
        ][-6:]

    def alive(self) -> bool:
        if self.closed or self.pipe.closed:
            return False
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            return False
        return pid == 0

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
        pipe = getattr(self, "pipe", None)
        if pipe and not pipe.closed:
            try:
                pipe.call("Browser.close", timeout=5)
            except WorkflowError:
                pass
        if hasattr(self, "pid"):
            for sig, wait in ((None, 6), (signal.SIGTERM, 4), (signal.SIGKILL, 4)):
                if sig is not None:
                    try:
                        os.killpg(self.pid, sig)
                    except (ProcessLookupError, PermissionError):
                        pass
                end = time.monotonic() + wait
                done = False
                while time.monotonic() < end:
                    try:
                        pid, _ = os.waitpid(self.pid, os.WNOHANG)
                    except ChildProcessError:
                        done = True
                        break
                    if pid:
                        done = True
                        break
                    time.sleep(0.05)
                if done:
                    break
            # Zygote and renderers share the process group started for this browser.
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if pipe:
            pipe.close()
        with self._task_cond:
            self._task_cond.notify_all()
        if self._lock_socket:
            self._lock_socket.close()
            self._lock_socket = None

    # -- guard

    def _setup(self):
        try:
            self.pipe.call("Browser.getVersion", timeout=60)
        except WorkflowError:
            time.sleep(0.2)
            detail = "；".join(self._stderr)[:400]
            raise WorkflowError(
                "学校账号浏览器没有启动。" + (f"\n{detail}" if detail else "")
            ) from None
        # Browser-wide interception first: from here on no request of any target leaves without
        # passing `allowed`, and until a mode is set every request is refused.
        self.pipe.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, timeout=15)
        self.pipe.call("Target.setDiscoverTargets", {"discover": True}, timeout=15)
        self.pipe.call(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
            timeout=15,
        )
        self.pipe.call("Browser.setDownloadBehavior", {"behavior": "deny"}, timeout=15)
        targets = self.pipe.call("Target.getTargets", {}, timeout=15).get("targetInfos") or []
        self.initial_targets = {info.get("targetId") for info in targets}
        # Let the worker prepare the targets that existed at launch before any page is opened.
        done = threading.Event()
        self._post(done.set)
        done.wait(15)

    def set_mode(self, mode, read_url=""):
        with self._lock:
            self.mode = mode
            self.read_url = read_url
            self.login_wall = False
            self.blocked_navigation = ""

    def _event(self, session, method, params):
        if method == "Fetch.requestPaused":
            self._paused(session, params)
        elif method == "Target.attachedToTarget":
            info = params.get("targetInfo") or {}
            with self._lock:
                self.sessions[params.get("sessionId")] = info
                if info.get("type") == "page":
                    self.page_ids.add(info.get("targetId"))
            self._post(lambda: self._prepare(params))
        elif method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            if info.get("type") == "page":
                with self._lock:
                    self.page_ids.add(info.get("targetId"))
        elif method in {"Target.detachedFromTarget", "Target.targetDestroyed"}:
            target = params.get("targetId") or (
                self.sessions.get(params.get("sessionId"), {}).get("targetId")
            )
            with self._lock:
                self.sessions.pop(params.get("sessionId"), None)
                page = self.pages.pop(target, None) if target else None
            if page and self.on_target:
                self._post(lambda: self.on_target("closed", page))
        elif method == "Page.screencastFrame":
            try:
                self.pipe.send(
                    "Page.screencastFrameAck", {"sessionId": params.get("sessionId")}, session
                )
            except WorkflowError:
                return
            if self.on_frame:
                self.on_frame(session, params)
        else:
            page = self._page_for(session)
            if not page:
                return
            if method == "Page.frameNavigated":
                frame = params.get("frame") or {}
                if not frame.get("parentId"):
                    page.url = frame.get("url", "")
                    page.world = None
            elif method == "Page.lifecycleEvent" and params.get("frameId") == page.target_id:
                with page.cond:
                    page.lifecycle.add((params.get("loaderId"), params.get("name")))
                    page.cond.notify_all()
            elif method == "Network.responseReceived":
                response = params.get("response") or {}
                entry = (params.get("requestId"), response.get("url", ""), response.get("status"))
                if params.get("type") == "Document" and params.get("frameId") == page.target_id:
                    page.documents.append(entry)
                elif params.get("type") == "Image":
                    page.responses[response.get("url", "")] = (
                        params.get("requestId"),
                        response.get("mimeType", ""),
                        response.get("status"),
                    )
            elif method == "Network.loadingFinished":
                with page.cond:
                    page.finished.add(params.get("requestId"))
                    page.cond.notify_all()

    def _page_for(self, session):
        with self._lock:
            for page in self.pages.values():
                if page.session == session:
                    return page
        return None

    def _paused(self, session, params):
        request = params.get("request") or {}
        url, method = request.get("url", ""), request.get("method", "")
        kind = params.get("resourceType", "")
        with self._lock:
            mode, read_url = self.mode, self.read_url
            top = kind == "Document" and params.get("frameId") in self.page_ids
        ok = mode is not None and guard.allowed(
            mode, method, url, kind, top_level=top, read_url=read_url
        )
        try:
            if ok:
                self.pipe.send("Fetch.continueRequest", {"requestId": params["requestId"]}, session)
                return
            with self._lock:
                self.blocked += 1
                if guard.submits(url):
                    self.blocked_submits += 1
                if mode == guard.READ and top:
                    self.blocked_navigation = url
                    if guard.login_wall(url):
                        self.login_wall = True
            self.pipe.send(
                "Fetch.failRequest",
                {"requestId": params["requestId"], "errorReason": "BlockedByClient"},
                session,
            )
        except (WorkflowError, KeyError):
            pass

    # -- targets

    def _post(self, task):
        with self._task_cond:
            self._tasks.append(task)
            self._task_cond.notify()

    def _work(self):
        while True:
            with self._task_cond:
                while not self._tasks and not self.closed:
                    self._task_cond.wait(1.0)
                if self.closed:
                    return
                task = self._tasks.pop(0)
            try:
                task()
            except Exception:  # noqa: BLE001 - the worker serves every target
                pass

    def _prepare(self, params):
        """A new target is held until its own interception is on; then it may run."""
        session = params.get("sessionId")
        info = params.get("targetInfo") or {}
        kind = info.get("type", "")
        try:
            try:
                self.pipe.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session)
            except CDPError:
                if kind not in OWNED_BY_PAGE:
                    raise
            try:
                self.pipe.call(
                    "Target.setAutoAttach",
                    {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                    session,
                )
            except CDPError:
                if kind not in OWNED_BY_PAGE:
                    raise
            if kind == "page":
                self._page_setup(session, info)
        except WorkflowError:
            # Fail closed: a target whose requests cannot be checked does not run.
            if kind == "page":
                try:
                    self.pipe.send("Target.closeTarget", {"targetId": info.get("targetId")})
                except WorkflowError:
                    pass
                with self._lock:
                    page = self.pages.get(info.get("targetId"))
                if page:
                    page.failed = True
                    page.ready.set()
            else:
                threading.Thread(target=self.close, daemon=True).start()
            return
        if params.get("waitingForDebugger"):
            self.pipe.send("Runtime.runIfWaitingForDebugger", {}, session)

    def _page_setup(self, session, info):
        target = info.get("targetId")
        with self._lock:
            page = self.pages.get(target)
            opener = info.get("openerId")
            if page is None and target not in self.initial_targets:
                expected = self._expecting
                if expected is not None and expected.target_id is None and not opener:
                    # The page open_page is creating right now.
                    expected.target_id = target
                    page = self.pages[target] = expected
                elif self.mode == guard.SIGN_IN and self.sign_in_page and len(self.pages) < 4:
                    # A popup or new tab of the sign-in page: the remote view follows it.
                    if opener not in self.pages:
                        opener = self.sign_in_page
                    page = self.pages[target] = _Page(target, session, opener=opener)
        if page is None:
            # Not ours: the start tab stays blank; anything else (a popup while reading, a
            # window nobody asked for) is closed. Its requests are refused either way.
            if target not in self.initial_targets:
                self.pipe.send("Target.closeTarget", {"targetId": target})
            return
        page.session = session
        self.pipe.call("Page.enable", {}, session)
        self.pipe.call("Page.setLifecycleEventsEnabled", {"enabled": True}, session)
        self.pipe.call(
            "Network.enable",
            {"maxTotalBufferSize": 64 * 1024 * 1024, "maxResourceBufferSize": MAX_BODY_BYTES},
            session,
        )
        if BLOCKED_URL_PATTERNS:
            try:
                self.pipe.call("Network.setBlockedURLs", {"urls": BLOCKED_URL_PATTERNS}, session)
            except CDPError:
                pass
        self.pipe.call(
            "Emulation.setUserAgentOverride", ua_override(self.major, self.full_version), session
        )
        emulation = self.emulation or {"width": 1280, "height": 900, "touch": False}
        self.pipe.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": emulation["width"],
                "height": emulation["height"],
                "deviceScaleFactor": emulation.get("scale") or (2 if emulation["touch"] else 1),
                "mobile": bool(emulation["touch"]),
            },
            session,
        )
        if emulation["touch"]:
            self.pipe.call(
                "Emulation.setTouchEmulationEnabled",
                {"enabled": True, "maxTouchPoints": 5},
                session,
            )
        self.pipe.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, session)
        page.ready.set()
        if page.opener and self.on_target:
            self.on_target("opened", page)

    def open_page(self, width=1280, height=900, touch=False, scale=None) -> _Page:
        placeholder = _Page(None, None)
        with self._lock:
            self.emulation = {"width": width, "height": height, "touch": touch, "scale": scale}
            self._expecting = placeholder
        try:
            result = self.pipe.call("Target.createTarget", {"url": "about:blank"}, timeout=15)
        finally:
            with self._lock:
                self._expecting = None
        target = result.get("targetId")
        with self._lock:
            if placeholder.target_id is None:
                # Created before its attach event reached the worker: register it now.
                placeholder.target_id = target
                self.pages[target] = placeholder
            self.page_ids.add(target)
        page = placeholder
        if page.target_id != target or not page.ready.wait(15) or page.failed or not page.session:
            self.pipe.send("Target.closeTarget", {"targetId": target})
            raise WorkflowError("学校账号浏览器无法安全地打开页面（请求检查没有生效），已停止。")
        return page

    def navigate(self, page: _Page, url: str, timeout=30.0) -> dict:
        with page.cond:
            page.lifecycle.clear()
            page.documents.clear()
        return self.pipe.call("Page.navigate", {"url": url}, page.session, timeout=timeout)

    def close_page(self, page: _Page):
        with self._lock:
            self.pages.pop(page.target_id, None)
        with page.cond:
            page.closed = True
            page.cond.notify_all()
        try:
            self.pipe.call("Target.closeTarget", {"targetId": page.target_id}, timeout=10)
        except WorkflowError:
            pass

    def evaluate(self, page: _Page, expression: str, timeout=10.0, await_promise=False):
        """Evaluate in an isolated world of the page's main frame (page scripts cannot see it);
        `await_promise` waits for an async expression's result."""
        if page.world is None:
            result = self.pipe.call(
                "Page.createIsolatedWorld",
                {"frameId": page.target_id, "worldName": "classroomautowork"},
                page.session,
                timeout=timeout,
            )
            page.world = result["executionContextId"]
        params = {"expression": expression, "contextId": page.world, "returnByValue": True}
        if await_promise:
            params["awaitPromise"] = True
        result = self.pipe.call("Runtime.evaluate", params, page.session, timeout=timeout)
        if result.get("exceptionDetails"):
            raise CDPError("页面检查失败。")
        return (result.get("result") or {}).get("value")

    def wait_finished(self, page: _Page, request_id, timeout=5.0) -> bool:
        """Whether the response's body has fully arrived (it can lag the page's load event by a
        moment; asking for it earlier gets nothing)."""
        end = time.monotonic() + timeout
        with page.cond:
            while request_id not in page.finished:
                left = end - time.monotonic()
                if left <= 0 or page.closed:
                    return False
                page.cond.wait(min(left, 0.25))
        return True

    def body(self, page: _Page, request_id) -> bytes:
        result = self.pipe.call(
            "Network.getResponseBody", {"requestId": request_id}, page.session, timeout=20
        )
        data = result.get("body", "")
        raw = base64.b64decode(data) if result.get("base64Encoded") else data.encode("utf-8")
        if len(raw) > MAX_BODY_BYTES:
            raise WorkflowError("页面超过安全读取大小上限。")
        return raw


def orphans(profile: Path, proc=Path("/proc")) -> list[int]:
    """Processes started with exactly this --user-data-dir (Chromium and its helpers)."""
    wanted = f"--user-data-dir={profile}".encode()
    found = []
    try:
        entries = list(proc.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if wanted in args:
            found.append(int(entry.name))
    return found


def kill_orphans(profile: Path) -> list[int]:
    pids = orphans(profile)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return pids


def _high_fd(fd):
    import fcntl

    new = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(fd)
    return new


# --------------------------------------------------------------------------- the manager


class _Session:
    def __init__(self, page, width, height, touch, scale=None):
        self.page = page
        self.width, self.height, self.touch = width, height, touch
        # Frames at twice the CSS size for a phone and for a high-density desktop screen.
        self.scale = 2 if touch or scale == 2 else 1
        self.seq = 0
        self.frame = None
        self.cond = threading.Condition()
        now = time.monotonic()
        self.started = self.last_input = self.last_poll = now
        self.started_at = utc_now()
        self.focus = {"password": False, "editable": False, "hint": "", "top": -1, "bottom": -1}
        self.focus_at = 0.0
        self.sent_focus = None
        self.ended = None


class SchoolBrowser:
    """The sign-in session and the form reader of one data directory. See `manager`."""

    def __init__(
        self,
        data_dir: Path,
        *,
        school_email="",
        timezone="Asia/Tokyo",
        profile=None,
        chromium=None,
        idle_seconds=IDLE_SECONDS,
    ):
        self.data_dir = Path(data_dir)
        self.school_email = (school_email or "").lower()
        self.timezone = timezone
        self.profile = profile_path(profile)
        self.chromium = chromium or os.environ.get(CHROMIUM_ENV) or DEFAULT_CHROMIUM
        self.idle_seconds = idle_seconds
        self._lock = threading.RLock()
        self._browser = None
        self._session = None
        self._reading = False
        self._opening = False
        self._last_use = time.monotonic()
        self._stopped = threading.Event()
        self._watchdog = threading.Thread(
            target=self._watch, name="school-browser-watch", daemon=True
        )
        self._watchdog.start()

    # -- lifecycle

    def _launch(self, scale=1) -> Chromium:
        return Chromium(
            self.chromium,
            self.profile,
            timezone=self.timezone,
            extra_args=test_args(),
            scale=scale,
        )

    def _ensure(self, scale=None) -> Chromium:
        """The running browser, started if needed; `scale` (1 or 2) restarts it at that scale."""
        if self._browser is not None and (
            not self._browser.alive() or (scale is not None and self._browser.scale != scale)
        ):
            self._browser.close()
            self._browser = None
        if self._browser is None:
            self._browser = self._launch(scale or 1)
            browser = self._browser
            browser.on_frame = self._frame
            browser.on_target = self._target
        self._last_use = time.monotonic()
        return self._browser

    def stop(self):
        """Stop the browser (and any sign-in session) now; the next use starts it again."""
        with self._lock:
            self._end_session("stopped")
            browser, self._browser = self._browser, None
        if browser:
            browser.close()

    def shutdown(self):
        self._stopped.set()
        self.stop()

    def _watch(self):
        while not self._stopped.wait(2.0):
            self._tick()

    def _tick(self, now=None):
        """End a sign-in session nobody uses any more; stop an idle browser."""
        stop = None
        with self._lock:
            now = time.monotonic() if now is None else now
            session = self._session
            if session:
                if now - session.last_input > SESSION_INPUT_SECONDS:
                    self._end_session("idle")
                elif now - session.last_poll > SESSION_POLL_SECONDS:
                    self._end_session("gone")
                elif now - session.started > SESSION_MAX_SECONDS:
                    self._end_session("too_long")
            if self._browser is not None and not self._opening and not self._browser.alive():
                stop, self._browser = self._browser, None
                self._end_session("stopped")
            elif (
                self._browser is not None
                and self._session is None
                and not self._reading
                and not self._opening
                and now - self._last_use > self.idle_seconds
            ):
                stop, self._browser = self._browser, None
        if stop:
            stop.close()

    # -- sign-in session

    def start(self, *, width=390, height=844, touch=False, scale=1, takeover=False) -> dict:
        """Open Google's sign-in in the remote view, sized for the client (its view of the page:
        the remote page is laid out at that CSS size; scale 2 for a phone or a dense screen).

        `takeover` closes a sign-in window that is open elsewhere (another device, a tab that
        was killed) and opens it here instead; the other page is told so."""
        for value in (width, height):
            if not isinstance(value, int) or isinstance(value, bool):
                raise WorkflowError("登录画面的尺寸无效。")
        width = max(320, min(width, 1600))
        # A short phone screen still gets its view at one to one (the sign-in page fits 300).
        height = max(300, min(height, 1400))
        with self._lock:
            if self._opening:
                raise Busy("学校账号登录窗口正在打开，请稍等几秒再试。")
            if self._reading:
                raise Busy("正在用学校账号读取表单，读完后再登录。")
            if self._session and takeover:
                self._end_session("replaced")
            if self._session:
                raise Busy(BUSY_ELSEWHERE)
            self._opening = True
        # Starting Chromium takes a few seconds; status() stays answerable meanwhile.
        try:
            dense = 2 if touch or scale == 2 else 1
            browser = self._ensure(dense)
            browser.set_mode(guard.SIGN_IN)
            try:
                page = browser.open_page(width, height, bool(touch), dense)
            except WorkflowError:
                self.stop()
                raise
            with self._lock:
                session = self._session = _Session(page, width, height, bool(touch), dense)
                browser.sign_in_page = page.target_id
                self._screencast(page)
                browser.pipe.send(
                    "Page.navigate", {"url": sign_in_url(self.school_email)}, page.session
                )
                self._last_use = time.monotonic()
        finally:
            with self._lock:
                self._opening = False
        return {"started_at": session.started_at, **self.status()}

    def _screencast(self, page):
        session = self._session
        scale = session.scale
        self._browser.pipe.call(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": 60,
                "maxWidth": session.width * scale,
                "maxHeight": session.height * scale,
                "everyNthFrame": 1,
            },
            page.session,
        )

    def _frame(self, cdp_session, params):
        session = self._session
        if not session or session.page.session != cdp_session:
            return
        try:
            data = base64.b64decode(params.get("data", ""))
        except ValueError:
            return
        width, height = jpeg_size(data)
        meta = params.get("metadata") or {}
        with session.cond:
            session.seq += 1
            session.frame = {
                "jpeg": data,
                "image_width": width,
                "image_height": height,
                "width": meta.get("deviceWidth") or session.width,
                "height": meta.get("deviceHeight") or session.height,
            }
            session.cond.notify_all()

    def _target(self, event, page):
        """Follow a popup of the sign-in page in the remote view, and come back when it closes."""
        with self._lock:
            session = self._session
            if not session or not self._browser:
                return
            if event == "opened":
                old = session.page
                session.page = page
            elif event == "closed" and page is session.page:
                opener = self._browser.pages.get(page.opener) if page.opener else None
                if not opener:
                    self._end_session("closed")
                    return
                old, session.page = None, opener
            else:
                return
            try:
                if old:
                    self._browser.pipe.send("Page.stopScreencast", {}, old.session)
                self._screencast(session.page)
            except WorkflowError:
                pass

    def _active(self) -> tuple["_Session", Chromium]:
        session, browser = self._session, self._browser
        if not session or not browser:
            raise WorkflowError("学校账号登录窗口已经关闭。" + AGAIN)
        return session, browser

    def frame(self, after=0, timeout=20.0) -> dict:
        """The newest frame once it is newer than `after` (long-poll, up to `timeout` seconds).

        {seq, jpeg, image_width, image_height, width, height, host, password, hint, changed};
        `width`/`height` are the page's CSS size, `password` whether the focused field is one.
        """
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise WorkflowError("画面序号无效。")
        timeout = max(0.0, min(float(timeout), 25.0))
        with self._lock:
            session, browser = self._active()
            session.last_poll = time.monotonic()
        end = time.monotonic() + timeout
        while True:
            left = max(0.0, end - time.monotonic())
            with session.cond:
                session.cond.wait_for(
                    lambda: session.seq > after or session.ended,
                    min(left, FOCUS_RECHECK_SECONDS),
                )
                frame, seq, ended = session.frame, session.seq, session.ended
            with self._lock:
                session.last_poll = time.monotonic()
            if ended:
                raise WorkflowError(_ENDED.get(ended, "学校账号登录窗口已经关闭。"))
            if seq > after or time.monotonic() >= end:
                break
            # Nothing repainted: answer early when the focus changed (a field lost focus on a
            # page that does not repaint), so the status line does not stay wrong.
            if (
                session.sent_focus is not None
                and self._focus(session, browser) != session.sent_focus
            ):
                break
        focus = self._focus(session, browser)
        session.sent_focus = focus
        page = session.page
        host = guard.host_of(page.url) if page.url.startswith("http") else ""
        if not frame:
            return {"seq": seq, "jpeg": b"", "changed": False, "host": host, **focus}
        return {
            "seq": seq,
            **frame,
            "changed": seq > after,
            "host": host,
            **focus,
        }

    def _focus(self, session, browser):
        now = time.monotonic()
        if now - session.focus_at > 0.3:
            session.focus_at = now
            try:
                value = browser.evaluate(session.page, FOCUS_SCRIPT, timeout=3)
                if isinstance(value, dict):
                    session.focus = {
                        "password": value.get("password") is True,
                        "editable": value.get("editable") is True,
                        "hint": value.get("hint")
                        if value.get("hint") in {"not_secure", "passkey"}
                        else "",
                        **{
                            key: _position(value.get(key), session.height)
                            for key in ("top", "bottom")
                        },
                    }
            except WorkflowError:
                pass
            path = urlsplit(session.page.url).path if session.page.url.startswith("http") else ""
            if "/signin/rejected" in path:
                session.focus["hint"] = "not_secure"
            elif "/challenge/pk" in path and not session.focus["hint"]:
                session.focus["hint"] = "passkey"
        return dict(session.focus)

    def input(self, action: dict) -> dict:
        """Send a tap, text, a key or a scroll to the sign-in page. Nothing of it is kept."""
        if not isinstance(action, dict) or action.get("type") not in {
            "tap",
            "text",
            "key",
            "scroll",
        }:
            raise WorkflowError("不支持的操作。")
        kind = action["type"]
        allowed_keys = {
            "tap": {"type", "x", "y"},
            "text": {"type", "text"},
            "key": {"type", "key"},
            "scroll": {"type", "x", "y", "dx", "dy"},
        }[kind]
        if set(action) - allowed_keys:
            raise WorkflowError("操作包含不支持的参数。")
        with self._lock:
            session, browser = self._active()
            session.last_input = session.last_poll = time.monotonic()
            frame = session.frame or {}
            target = session.page.session
        pipe = browser.pipe
        if kind == "text":
            pipe.call("Input.insertText", {"text": clean_text(action.get("text"))}, target)
        elif kind == "key":
            key = action.get("key")
            if key not in KEYS:
                raise WorkflowError("不支持的按键。")
            name, code, text = KEYS[key]
            down = {"type": "keyDown", "key": name, "code": name, "windowsVirtualKeyCode": code}
            if text:
                down["text"] = text
            pipe.call("Input.dispatchKeyEvent", down, target)
            pipe.call(
                "Input.dispatchKeyEvent",
                {"type": "keyUp", "key": name, "code": name, "windowsVirtualKeyCode": code},
                target,
            )
        elif kind == "tap":
            x, y = frame_point(action.get("x"), action.get("y"), frame)
            if session.touch:
                pipe.call(
                    "Input.dispatchTouchEvent",
                    {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]},
                    target,
                )
                pipe.call(
                    "Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []}, target
                )
            else:
                pipe.call(
                    "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, target
                )
                for event in ("mousePressed", "mouseReleased"):
                    pipe.call(
                        "Input.dispatchMouseEvent",
                        {"type": event, "x": x, "y": y, "button": "left", "clickCount": 1},
                        target,
                    )
        else:
            x, y = frame_point(action.get("x", 0), action.get("y", 0), frame)
            dx, dy = action.get("dx", 0), action.get("dy", 0)
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (dx, dy)):
                raise WorkflowError("滚动距离无效。")
            pipe.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": x,
                    "y": y,
                    "deltaX": max(-3000, min(float(dx), 3000)),
                    "deltaY": max(-3000, min(float(dy), 3000)),
                },
                target,
            )
        session.focus_at = 0.0
        return {"ok": True}

    def back(self) -> dict:
        with self._lock:
            session, browser = self._active()
            session.last_input = session.last_poll = time.monotonic()
            page = session.page
        history = browser.pipe.call("Page.getNavigationHistory", {}, page.session)
        index, entries = history.get("currentIndex", 0), history.get("entries") or []
        if index > 0 and index - 1 < len(entries):
            browser.pipe.call(
                "Page.navigateToHistoryEntry", {"entryId": entries[index - 1]["id"]}, page.session
            )
        elif page.opener:
            browser.close_page(page)
            self._target("closed", page)
        return {"ok": True}

    def cancel(self) -> dict:
        with self._lock:
            self._end_session("cancelled")
        return self.status()

    def _end_session(self, why):
        session = self._session
        if not session:
            return
        self._session = None
        with session.cond:
            session.ended = why
            session.cond.notify_all()
        browser = self._browser
        if browser and browser.alive():
            with browser._lock:
                pages = list(browser.pages.values())
                browser.pages.clear()
                browser.sign_in_page = None
            browser.set_mode(None)
            for page in pages:
                try:
                    browser.pipe.send("Target.closeTarget", {"targetId": page.target_id})
                except WorkflowError:
                    pass
        # After a finished sign-in the browser waits the usual idle time (the next form read is
        # likely soon); a cancelled or abandoned session stops it at the next watchdog tick.
        self._last_use = time.monotonic() if why == "finished" else float("-inf")

    def finish(self) -> dict:
        """Check who is signed in (Google's account list, no cookies read) and remember it.

        An account it cannot accept (another than the settings' school email, or several with
        none set) is signed out of this browser at once (Google's Logout, a GET) and the saved
        login is forgotten, so no later read can use it; the view goes back to the sign-in."""
        with self._lock:
            session, browser = self._active()
            session.last_input = session.last_poll = time.monotonic()
        refusal = None
        try:
            page = browser.open_page(1280, 900, False)
            try:
                emails, certain = parse_accounts(self._accounts(browser, page))
                refusal = self._refusal(emails)
                if refusal:
                    self._sign_out(browser, page)
            finally:
                browser.close_page(page)
                with browser._lock:
                    browser.emulation = {
                        "width": session.width,
                        "height": session.height,
                        "touch": session.touch,
                        "scale": session.scale,
                    }
        except WorkflowError as exc:
            raise WorkflowError(
                "没能确认登录的是哪个账号，请稍后再点“完成”。\n" + str(exc)[:200]
            ) from None
        if not emails:
            raise WorkflowError(
                "还没有登录完成：Google 没有列出已登录的账号。请在画面里完成登录后再点“完成”。"
            )
        if refusal:
            state_path(self.data_dir).unlink(missing_ok=True)
            try:
                browser.pipe.send(
                    "Page.navigate", {"url": sign_in_url(self.school_email)}, session.page.session
                )
            except WorkflowError:
                pass
            raise WorkflowError(refusal)
        email = self.school_email or emails[0]
        record = {
            "email": email,
            "verified_at": utc_now(),
            "state": "signed_in",
            "verified_by": "accounts_list" if certain else "page_text",
        }
        _write_record(self.data_dir, record)
        with self._lock:
            self._end_session("finished")
        return self.status()

    def _refusal(self, emails) -> str:
        """Why these signed-in accounts cannot be kept, or '' when one of them is the one."""
        if not emails:
            return ""
        if self.school_email:
            if self.school_email in emails:
                return ""
            return (
                f"登录的是 {emails[0]}，与设置里的学校邮箱 {self.school_email} 不一致；"
                "没有保存，已全部退出。请在画面里用学校邮箱重新登录，再点“完成”。"
            )
        if len(emails) > 1:
            return (
                "这个浏览器里登录了多个 Google 账号，无法确定用哪一个；没有保存，已全部退出。"
                "请在画面里只登录学校账号，再点“完成”。"
            )
        return ""

    def _sign_out(self, browser, page):
        """Google's Logout in the hidden tab: every account of this browser is signed out."""
        try:
            result = browser.navigate(page, LOGOUT_URL, timeout=20)
            if result.get("loaderId"):
                page.wait_load(result["loaderId"], 20)
        except WorkflowError:
            pass

    def _accounts(self, browser, page) -> str:
        """Google's list of the accounts signed in to this browser (its JSON text)."""
        result = browser.navigate(page, ACCOUNTS_PAGE, timeout=20)
        if result.get("errorText"):
            raise WorkflowError(f"页面没有打开（{result['errorText'][:80]}）。")
        if not page.wait_load(result.get("loaderId"), 20):
            raise WorkflowError("页面加载超时。")
        value = browser.evaluate(page, ACCOUNTS_SCRIPT, timeout=20, await_promise=True)
        if not (isinstance(value, list) and len(value) == 2 and isinstance(value[1], str)):
            raise WorkflowError("Google 的账号列表无法识别。")
        if value[0] != 200:
            raise WorkflowError(f"Google 的账号列表没有返回（HTTP {value[0]}）。")
        return value[1]

    def status(self) -> dict:
        with self._lock:
            session = self._session
            return {
                **login_state(self.data_dir, self.school_email),
                "session": (
                    {"active": True, "started_at": session.started_at, "touch": session.touch}
                    if session
                    else None
                ),
                "reading": self._reading,
                "browser_running": self._browser is not None,
                # Whether a browser profile is left (a sign-in that was never finished, say):
                # 退出登录 then still has something to delete.
                "profile": os.path.lexists(self.profile),
            }

    def logout(self) -> dict:
        """Stop the browser, delete its whole profile and the saved login."""
        with self._lock:
            if self._reading:
                raise Busy("正在用学校账号读取表单，读完后再退出登录。")
            if self._opening:
                raise Busy("学校账号登录窗口正在打开，请稍等几秒再退出登录。")
            self.stop()
            state_path(self.data_dir).unlink(missing_ok=True)
            error = delete_profile(self.profile, self.chromium)
        if error:
            raise WorkflowError(error)
        return self.status()

    # -- reading

    def read_page(self, url: str, capture_dir: Path) -> dict:
        """GET the canonical viewform page signed in; no click, no typing, no form interaction.

        Returns {"html": original document body, "images": [rendered images, each with the
        bytes saved under capture_dir when the response is available]}. A redirect to Google's
        sign-in marks the saved login expired and raises LoginExpired.
        """
        from .form_read import FormClosed, form_url

        canonical = form_url(url)
        with self._lock:
            if self._session or self._opening:
                raise Busy("学校账号登录窗口正在使用，表单暂时不能读取。完成或取消登录后再重试。")
            if self._reading:
                raise Busy("另一份表单正在用学校账号读取，请稍后重试。")
            self._reading = True
        try:
            # Scale 1: a browser left at 2 by a phone's sign-in would render the tall page
            # below at four times the pixels.
            browser = self._ensure(1)
            browser.set_mode(guard.READ, canonical)
        except BaseException:
            with self._lock:
                self._reading = False
            raise
        page = None
        try:
            # A tall viewport renders every picture of the first section without scrolling.
            page = browser.open_page(1280, 2400, False)
            try:
                result = browser.navigate(page, canonical, timeout=READ_SECONDS)
            except WorkflowError:
                result = {}
            if browser.login_wall:
                raise LoginExpired("login wall")
            if result.get("errorText") or not result.get("loaderId"):
                message = _not_opened(browser.blocked_navigation, result)
                if "/closedform" in urlsplit(browser.blocked_navigation).path:
                    raise FormClosed(message)
                raise WorkflowError(message)
            if not page.wait_load(result["loaderId"], READ_SECONDS, lambda: browser.login_wall):
                if browser.login_wall:
                    raise LoginExpired("login wall")
                raise WorkflowError("表单页面加载超时。")
            documents = [
                d
                for d in page.documents
                if d[2] == 200
                and guard.allowed(
                    guard.READ, "GET", d[1], "Document", top_level=True, read_url=canonical
                )
            ]
            if not documents:
                raise WorkflowError("表单页面没有返回题目（不是 200）。")
            html = browser.body(page, documents[-1][0]).decode("utf-8", "replace")
            end = time.monotonic() + 10
            while time.monotonic() < end:
                try:
                    if browser.evaluate(page, IMAGES_DONE, timeout=5):
                        break
                except WorkflowError:
                    break
                time.sleep(0.25)
            images = self._images(browser, page, capture_dir)
            if browser.login_wall:
                # The page sent itself to the sign-in after loading.
                raise LoginExpired("login wall")
            return {"html": html, "images": images, "blocked": browser.blocked}
        except LoginExpired:
            mark_expired(self.data_dir)
            raise LoginExpired(
                "学校账号的登录已过期，表单题目没有读到。请在 设置 → 学校账号 重新登录后重试。"
            ) from None
        finally:
            if page is not None:
                browser.close_page(page)
            browser.set_mode(None)
            with self._lock:
                self._reading = False
                self._last_use = time.monotonic()

    def _images(self, browser, page, capture_dir: Path) -> list[dict]:
        try:
            rendered = browser.evaluate(page, IMAGES_SCRIPT, timeout=10)
        except WorkflowError:
            return []
        if not isinstance(rendered, list):
            return []
        capture_dir = require_private_path(capture_dir)
        result = []
        for image in rendered[:200]:
            if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                continue
            entry = {
                key: image.get(key)
                for key in (
                    "index",
                    "natural_width",
                    "natural_height",
                    "width",
                    "height",
                    "attr_width",
                    "attr_height",
                )
            }
            entry["item_ids"] = [x for x in image.get("item_ids") or [] if isinstance(x, str)]
            entry["in_list"] = image.get("in_list") is True
            entry["google"] = google_picture(image["url"])
            response = page.responses.get(image["url"])
            saved = sum(1 for x in result if x.get("path"))
            if entry["google"] and response and response[2] == 200 and saved < MAX_IMAGES:
                data = b""
                for _attempt in range(3):
                    browser.wait_finished(page, response[0])
                    try:
                        data = browser.body(page, response[0])
                        break
                    except WorkflowError:
                        time.sleep(0.2)
                if data:
                    capture_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    path = capture_dir / f"{len(result):03d}.img"
                    path.write_bytes(data)
                    os.chmod(path, 0o600)
                    entry["path"] = str(path)
            result.append(entry)
        return result


def _not_opened(blocked: str, result: dict) -> str:
    path = urlsplit(blocked).path if blocked else ""
    if "/closedform" in path:
        return "表单已关闭，不再接受回答；没有读取题目。"
    if blocked:
        return f"表单页面跳转到了别处（{guard.host_of(blocked)}），只读读取已停止；没有读取题目。"
    detail = str(result.get("errorText") or "没有回应")[:80]
    return f"表单页面没有打开（{detail}）。"


AGAIN = "需要时请在 设置 → 学校账号 重新开始登录。"
BUSY_ELSEWHERE = (
    "学校账号登录窗口已经在别处打开（可能在另一台设备上）。可以在这里重新打开（那边会关闭）；"
    "那边没有人操作时，它约 2 分钟后也会自己关闭。"
)
_ENDED = {
    "cancelled": "登录已取消。",
    "finished": "登录已完成。",
    "idle": "登录窗口 10 分钟没有操作，已关闭。" + AGAIN,
    "gone": "页面有一段时间没有刷新登录画面，登录窗口已关闭。" + AGAIN,
    "too_long": "登录窗口已打开 30 分钟，已关闭。" + AGAIN,
    "stopped": "学校账号浏览器已停止。" + AGAIN,
    "closed": "登录页面已关闭。" + AGAIN,
    "replaced": "登录窗口已在别处重新打开，这里的已关闭。",
}


def _position(value, height) -> int:
    """A field position from the page: an int within the viewport, else -1 (none)."""
    if type(value) is not int or not -height <= value <= 2 * height:
        return -1
    return value


def delete_profile(profile: Path, chromium: str) -> str:
    """Delete the browser profile; '' when it is gone, else what went wrong.

    The service sees the profile read-only (ProtectHome); the snap's own mount namespace can
    write it, so a snap profile that cannot be removed here is removed by a shell in the snap.
    """
    if not PROFILE_NAME.fullmatch(profile.name) or not profile.is_absolute():
        return "学校账号浏览器的资料目录无效，没有删除。"
    if not os.path.lexists(profile):
        return ""
    try:
        shutil.rmtree(profile)
        return ""
    except OSError:
        pass
    snap_common = Path(pwd.getpwuid(os.getuid()).pw_dir) / "snap" / "chromium" / "common"
    if (
        # /snap/bin/chromium is a link to the snap launcher (itself a link to snapd's binary,
        # which acts by the name it is called with: always call it as /usr/bin/snap).
        os.path.realpath(chromium) == os.path.realpath(SNAP)
        and profile.parent == snap_common
        and re.fullmatch(r"[A-Za-z0-9/._-]+", str(profile))
    ):
        try:
            subprocess.run(
                [
                    SNAP,
                    "run",
                    "--shell",
                    "chromium",
                    "-c",
                    "rm -r -- " + shlex.quote(str(profile)),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                env=_environment(),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    if os.path.lexists(profile):
        return (
            f"浏览器资料没有删干净（{profile}）。登录信息已不再使用；请在小主机上手动删除这个目录。"
        )
    return ""


def manager(settings) -> SchoolBrowser:
    """The one SchoolBrowser of this process for these settings' data directory."""
    data_dir = Path(settings.data_dir).resolve()
    with _MANAGERS_LOCK:
        current = _MANAGERS.get(data_dir)
        if current is None:
            if not _MANAGERS:
                # A CLI run has no serve() to stop it; Chromium also exits when its pipe closes.
                atexit.register(stop_all)
            current = _MANAGERS[data_dir] = SchoolBrowser(
                data_dir,
                school_email=getattr(settings, "school_email", "") or "",
                timezone=getattr(settings, "timezone", "Asia/Tokyo") or "Asia/Tokyo",
            )
        else:
            current.school_email = (getattr(settings, "school_email", "") or "").lower()
        return current


def stop_all() -> None:
    """Called when the service stops: no Chromium outlives it."""
    with _MANAGERS_LOCK:
        managers = list(_MANAGERS.values())
        _MANAGERS.clear()
    for item in managers:
        item.shutdown()
