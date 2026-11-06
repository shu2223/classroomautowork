"""Which requests the school-login browser may send. Pure functions, no I/O.

Every request of every target in that browser (main page, popups, iframes, workers, service
workers) is paused by the DevTools Fetch domain and released only when `allowed` says so; the
rest fail with BlockedByClient. The rules make submitting impossible from that browser, whoever
clicks:

* any URL whose path contains ``/formResponse`` is blocked, on any host and with any method
  (Google Forms' submit endpoint, which also submits on a GET with ``submit=``);
* read mode (the program reading a form): GET and HEAD only, and the page itself may only be
  the canonical ``viewform`` URL being read. A redirect to Google's sign-in means the login
  has expired; the reader stops there (`login_wall`);
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
from urllib.parse import unquote_to_bytes, urlsplit

READ = "read"
SIGN_IN = "sign_in"
MODES = {READ, SIGN_IN}
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


def allowed(
    mode: str,
    method: str,
    url: str,
    resource_type: str = "",
    *,
    top_level: bool = False,
    read_url: str = "",
) -> bool:
    """Whether the school-login browser may send this request.

    `resource_type` is the DevTools type (Document, Script, Image, XHR, Fetch, Ping, …);
    `top_level` marks the navigation of a page's own main frame (also a popup's), which in read
    mode must be the form being read (`read_url`, canonical viewform). Unknown modes, methods,
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
        return False
    host = host_of(url)
    if is_local_host(host):
        return False
    if mode == READ:
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
