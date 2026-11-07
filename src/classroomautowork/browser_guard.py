"""Which requests the school-login browser may send. Pure functions, no I/O.

Every request of every target in that browser (main page, popups, iframes, workers, service
workers) is paused by the DevTools Fetch domain and released only when `allowed` says so; the
rest fail with BlockedByClient. The rules make submitting impossible from that browser, whoever
clicks:

* any URL whose path contains ``/formResponse`` is blocked, on any host and with any method
  (Google Forms' submit endpoint, which also submits on a GET with ``submit=``). The one
  exception is a page turn in walk mode, below;
* read mode (the program reading a form): GET and HEAD only, and the page itself may only be
  the canonical ``viewform`` URL being read. A redirect to Google's sign-in means the login
  has expired; the reader stops there (`login_wall`);
* walk mode (the program turning the pages of a form to see its later pictures): read mode,
  plus Google's own 次へ. Google Forms turns a page by POSTing the page to ``formResponse`` with
  ``continue=1``; only the final 送信 posts without it, and only that records an answer. So a
  POST there passes only as `page_turn` says: the main frame's own navigation, to exactly this
  form's ``formResponse`` on docs.google.com (no query, no escapes), a form-encoded body with
  exactly one ``continue=1`` and no ``submit``, and a page history that starts at the first page,
  goes one page at a time and ends no later than `last_from` (never the last page). The browser
  also counts them: no more page turns than the walk asked for. Google keeps what a page turn
  carried as the account's draft of that form, so after the walk the form's own
  「フォームをクリア」 deletes it: once the walk is over, one XHR POST to this form's
  ``deletedraft`` (`draft_deletion`) passes too;
* sign-in mode (the user signing in through the remote view): GET and HEAD anywhere; any other
  method only to Google's sign-in hosts and, over https, to named hosts that are not Google's (a
  school's own sign-in page). Docs, Forms, Classroom, Drive, Gmail, Apps Script and the Google
  APIs get no POST, PUT, PATCH or DELETE at all, so nothing can be submitted or turned in there
  either; nor does an IP literal (it could be a Google front end under another name);
* in both modes nothing goes to this machine or its networks: localhost, loopback, private,
  link-local, shared (tailnet) and other non-public addresses, and names like *.local or
  *.ts.net, are refused for every method. The browser runs as root on the mini PC, so the remote
  view must not become a way to reach the local services the page token does not open.
"""

import ipaddress
import re
import socket
from urllib.parse import parse_qsl, unquote_to_bytes, urlsplit

READ = "read"
SIGN_IN = "sign_in"
WALK = "walk"
MODES = {READ, SIGN_IN, WALK}
SAFE_METHODS = {"GET", "HEAD"}
# Google's registrable domains (lower case); a host equal to one or below one is Google's.
GOOGLE_DOMAINS = (
    "google.com",
    "googleapis.com",
    "gstatic.com",
    "googleusercontent.com",
    "ggpht.com",
    "googlevideo.com",
    "youtube.com",
    "youtu.be",
    "ytimg.com",
    "gmail.com",
    "forms.gle",
    "goo.gl",
    "g.co",
    "g.page",
    "withgoogle.com",
    "googlesyndication.com",
    "googletagmanager.com",
    "google-analytics.com",
    "doubleclick.net",
    "recaptcha.net",
    "appspot.com",
    "blogger.com",
    "googledrive.com",
    "googlegroups.com",
)
# google.co.jp, google.com.hk, google.de, …
GOOGLE_COUNTRY = re.compile(r"(?:^|\.)google\.(?:[a-z]{2}|co\.[a-z]{2}|com\.[a-z]{2})$")
# Sign-in hosts that receive form posts while the user signs in.
SIGN_IN_HOSTS = re.compile(
    r"accounts\.google\.(?:com|[a-z]{2}|co\.[a-z]{2}|com\.[a-z]{2})|accounts\.youtube\.com"
)
# On www.google.com only the parts the sign-in itself posts to: a school's SAML response
# (/a/<domain>/acs) and reCAPTCHA. recaptcha.net is the same service under another name.
SIGN_IN_PATHS = {
    "www.google.com": re.compile(r"/a/[^/]+/acs|/recaptcha/.*"),
    "www.recaptcha.net": re.compile(r"/recaptcha/.*"),
}
SUBMIT_PATH = "/formResponse"
# A page turn's own address: this form's endpoint, written plainly (no escapes, no query).
TURN_PATH = re.compile(r"/forms/(?:u/\d{1,2}/)?d/e/([A-Za-z0-9_-]{8,200})/formResponse")
READ_PATH = re.compile(r"/forms/d/e/([A-Za-z0-9_-]{8,200})/viewform")
DRAFT_PATH = re.compile(r"/forms/(?:u/\d{1,2}/)?d/e/([A-Za-z0-9_-]{8,200})/deletedraft")
PAGE_HISTORY = re.compile(r"\d{1,3}(?:,\d{1,3}){0,99}")
MAX_TURN_BODY = 512 * 1024
# Names that resolve to this machine, its LAN or its tailnet rather than to a public site.
LOCAL_SUFFIXES = (
    "localhost",
    "local",
    "localdomain",
    "internal",
    "intranet",
    "lan",
    "home",
    "home.arpa",
    "corp",
    "private",
    "ts.net",
    "beta.tailscale.net",
)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def is_google_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return bool(GOOGLE_COUNTRY.search(host)) or any(
        host == domain or host.endswith("." + domain) for domain in GOOGLE_DOMAINS
    )


def is_sign_in_host(host: str) -> bool:
    return bool(SIGN_IN_HOSTS.fullmatch(host.lower().rstrip(".")))


def submits(url: str) -> bool:
    """Google Forms' submit endpoint, on any host, behind a /u/N/ prefix, in any letter case and
    however much of it is percent-encoded (form%52esponse, %2FformResponse, a double encoding,
    a backslash for the slash). Anything that cannot be decoded counts as a submit."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return True
    for _ in range(8):
        decoded = unquote_to_bytes(path).decode("latin-1")
        if decoded == path:
            break
        path = decoded
    else:
        return True  # still changing after eight rounds: not a URL anyone needs
    return SUBMIT_PATH.lower() in path.replace("\\", "/").lower()


def ip_literal(host: str):
    """The address a host names directly (also 2130706433, 0x7f.1 and other IPv4 spellings), or
    None for a name."""
    host = host.strip("[]")
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if re.fullmatch(r"[0-9a-fA-Fx.]+", host) and not host.endswith("."):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return None
    return None


def is_local_host(host: str) -> bool:
    """This machine, its networks or its tailnet: never a destination for the school browser."""
    host = host.lower().rstrip(".")
    if not host:
        return True
    address = ip_literal(host)
    if address is not None:
        mapped = getattr(address, "ipv4_mapped", None)
        return not (mapped or address).is_global
    if "." not in host:
        return True  # a single-label name resolves through the LAN's search domains
    return any(host == suffix or host.endswith("." + suffix) for suffix in LOCAL_SUFFIXES)


def login_wall(url: str) -> bool:
    """A page navigation to Google's sign-in while reading: the saved login no longer works."""
    host = host_of(url)
    if is_sign_in_host(host):
        return True
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    return is_google_host(host) and path.startswith(("/ServiceLogin", "/accounts/ServiceLogin"))


def turn_refusal(
    method: str,
    url: str,
    resource_type: str,
    *,
    top_level: bool,
    read_url: str,
    content_type,
    body,
    last_from: int,
) -> str:
    """Why this request is not a page turn of the form being walked ('' when it is one).

    A page turn is what Google's own 次へ sends: the page's main frame POSTing the page, form
    encoded, to this form's formResponse with continue=1, its page history 0,1,…,n with n (the
    page it leaves, counted from 0) no later than `last_from`."""
    if (method or "").upper() != "POST" or resource_type != "Document" or not top_level:
        return "not the page's own POST"
    if not isinstance(last_from, int) or last_from < 0:
        return "no page turn allowed"
    try:
        parts, own = urlsplit(url), urlsplit(read_url)
    except ValueError:
        return "unreadable address"
    target, form = TURN_PATH.fullmatch(parts.path), READ_PATH.fullmatch(own.path)
    if (
        parts.scheme != "https"
        or parts.netloc != "docs.google.com"
        or parts.query
        or parts.fragment
        or not target
        or own.netloc != "docs.google.com"
        or not form
        or target[1] != form[1]
    ):
        return "not this form's page turn address"
    if str(content_type or "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
        return "not form encoded"
    if not isinstance(body, str) or len(body) > MAX_TURN_BODY:
        return "no readable body"
    try:
        pairs = parse_qsl(body, keep_blank_values=True, strict_parsing=True, max_num_fields=5000)
    except ValueError:
        return "malformed body"
    if [value for key, value in pairs if key == "continue"] != ["1"]:
        return "no continue=1"
    if any(key.lower() == "submit" for key, _ in pairs):
        return "a submit field"
    history = [value for key, value in pairs if key == "pageHistory"]
    if len(history) != 1 or not PAGE_HISTORY.fullmatch(history[0]):
        return "no page history"
    pages = [int(x) for x in history[0].split(",")]
    if pages != list(range(len(pages))):
        return "pages not in order from the first"
    if pages[-1] > last_from:
        return "past the last page it may leave"
    return ""


def draft_deletion(method: str, url: str, resource_type: str, *, read_url: str, content_type, body):
    """Whether this is the form's own 「フォームをクリア」 deleting the draft of the form being
    walked: an XHR POST to its deletedraft on docs.google.com (query: ouid and token only),
    form encoded, its body the one draft_response_action_request field."""
    if (method or "").upper() != "POST" or resource_type not in {"XHR", "Fetch"}:
        return False
    try:
        parts, own = urlsplit(url), urlsplit(read_url)
        query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=bool(parts.query))
    except ValueError:
        return False
    target, form = DRAFT_PATH.fullmatch(parts.path), READ_PATH.fullmatch(own.path)
    if (
        parts.scheme != "https"
        or parts.netloc != "docs.google.com"
        or parts.fragment
        or not target
        or own.netloc != "docs.google.com"
        or not form
        or target[1] != form[1]
        or not {key for key, _ in query} <= {"ouid", "token"}
    ):
        return False
    if str(content_type or "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
        return False
    if not isinstance(body, str) or len(body) > 4096:
        return False
    try:
        pairs = parse_qsl(body, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    return [key for key, _ in pairs] == ["draft_response_action_request"]


def allowed(
    mode: str,
    method: str,
    url: str,
    resource_type: str = "",
    *,
    top_level: bool = False,
    read_url: str = "",
    content_type=None,
    body=None,
    last_from: int = -1,
) -> bool:
    """Whether the school-login browser may send this request.

    `resource_type` is the DevTools type (Document, Script, Image, XHR, Fetch, Ping, …);
    `top_level` marks the navigation of a page's own main frame (also a popup's), which in read
    and walk mode must be the form being read (`read_url`, canonical viewform). In walk mode a
    POST to the form's formResponse passes only as a page turn (`turn_refusal`, from its
    `content_type` and `body`, leaving no page later than `last_from`). Unknown modes, methods,
    schemes and unparsable URLs are refused.
    """
    method = (method or "").upper()
    if mode not in MODES or not method:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        # data:, blob: and about: never reach the network; anything else is not expected.
        return parts.scheme in {"data", "blob", "about"} and method in SAFE_METHODS
    if submits(url):
        return mode == WALK and not turn_refusal(
            method,
            url,
            resource_type,
            top_level=top_level,
            read_url=read_url,
            content_type=content_type,
            body=body,
            last_from=last_from,
        )
    host = host_of(url)
    if is_local_host(host):
        return False
    if mode in {READ, WALK}:
        if method not in SAFE_METHODS:
            return False
        if top_level:
            return bool(read_url) and _same_form(url, read_url)
        return True
    if method in SAFE_METHODS:
        return True
    if is_sign_in_host(host):
        return True
    path_rule = SIGN_IN_PATHS.get(host)
    if path_rule and path_rule.fullmatch(parts.path):
        return True
    # A school's own sign-in: a named, public, https host that is not Google's.
    return parts.scheme == "https" and ip_literal(host) is None and not is_google_host(host)


def _same_form(url: str, read_url: str) -> bool:
    from .errors import WorkflowError
    from .form_read import form_url

    try:
        return form_url(url.split("#", 1)[0].split("?", 1)[0]) == form_url(read_url)
    except WorkflowError:
        return False
