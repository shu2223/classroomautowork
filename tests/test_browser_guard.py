"""The school-login browser's request rules, as a table. Pure functions, no browser."""

import pytest

from classroomautowork import browser_guard as guard

FORM = "https://docs.google.com/forms/d/e/1FAIpQLSdUnitLoginForm0000000000/viewform"
SUBMIT = "https://docs.google.com/forms/d/e/1FAIpQLSdUnitLoginForm0000000000/formResponse"
READ, SIGN_IN = guard.READ, guard.SIGN_IN

# (mode, method, url, resource type, top-level navigation, allowed)
TABLE = [
    # Google Forms' submit endpoint: never, on any host, with any method, in any mode.
    (READ, "POST", SUBMIT, "Document", True, False),
    (READ, "GET", SUBMIT + "?entry.1=a&submit=Submit", "Image", False, False),
    (SIGN_IN, "POST", SUBMIT, "Document", True, False),
    (SIGN_IN, "GET", SUBMIT + "?submit=Submit", "Document", True, False),
    (SIGN_IN, "POST", "https://docs.google.com/forms/u/0/d/e/x/formResponse", "XHR", False, False),
    (SIGN_IN, "POST", "https://example.ac.jp/forms/formResponse", "Document", True, False),
    (SIGN_IN, "GET", "https://example.ac.jp/a%2fformResponse", "Fetch", False, False),
    (SIGN_IN, "POST", "https://docs.google.com/forms/FORMRESPONSE", "Fetch", False, False),
    # ... however much of it is percent-encoded, double-encoded or written with a backslash.
    (
        READ,
        "GET",
        SUBMIT.replace("formResponse", "form%52esponse") + "?submit=1",
        "Image",
        False,
        False,
    ),
    (SIGN_IN, "GET", SUBMIT.replace("formResponse", "form%72esponse"), "Document", True, False),
    (SIGN_IN, "POST", SUBMIT.replace("formResponse", "%66orm%52esponse"), "XHR", False, False),
    (SIGN_IN, "GET", SUBMIT.replace("/formResponse", "%2FformResponse"), "Fetch", False, False),
    (SIGN_IN, "GET", SUBMIT.replace("formResponse", "form%2552esponse"), "Fetch", False, False),
    (SIGN_IN, "GET", SUBMIT.replace("/formResponse", "%5CformResponse"), "Fetch", False, False),
    (SIGN_IN, "GET", "https://example.ac.jp/x%" + "25" * 9 + "41", "Fetch", False, False),
    # This machine, its networks and its tailnet: never, in either mode, with any method.
    (SIGN_IN, "POST", "http://127.0.0.1:18080/api/tasks", "Document", True, False),
    (SIGN_IN, "GET", "http://127.0.0.1:8771/api/bootstrap", "Document", True, False),
    (READ, "GET", "http://127.0.0.1:8771/api/bootstrap", "Image", False, False),
    (SIGN_IN, "POST", "http://localhost:8773/api/records", "Fetch", False, False),
    (SIGN_IN, "GET", "http://app.localhost/", "Document", True, False),
    (SIGN_IN, "POST", "http://192.168.1.1/cgi-bin/x", "Fetch", False, False),
    (SIGN_IN, "GET", "http://10.0.0.5/admin", "Document", True, False),
    (SIGN_IN, "GET", "http://172.20.1.1/", "Image", False, False),
    (SIGN_IN, "POST", "http://100.100.100.100/", "Fetch", False, False),
    (SIGN_IN, "GET", "http://169.254.169.254/latest/meta-data/", "Fetch", False, False),
    (SIGN_IN, "GET", "http://0.0.0.0:8771/", "Document", True, False),
    (SIGN_IN, "GET", "http://[::1]:8771/", "Document", True, False),
    (SIGN_IN, "GET", "http://[fd7a:115c:a1e0::1]/", "Document", True, False),
    (SIGN_IN, "GET", "http://[fe80::1]/", "Document", True, False),
    (SIGN_IN, "GET", "http://[::ffff:127.0.0.1]/", "Document", True, False),
    (SIGN_IN, "GET", "http://2130706433/", "Document", True, False),
    (SIGN_IN, "GET", "http://0x7f.1/", "Document", True, False),
    (SIGN_IN, "GET", "http://minipc:8771/", "Document", True, False),
    (SIGN_IN, "GET", "http://minipc.local/", "Document", True, False),
    (SIGN_IN, "GET", "http://minipc.tail1234.ts.net/", "Document", True, False),
    (SIGN_IN, "GET", "http://router.home.arpa/", "Document", True, False),
    (READ, "GET", "https://printer.lan/x.png", "Image", False, False),
    # Read mode: GET/HEAD only, and the page itself only the form being read.
    (READ, "GET", FORM, "Document", True, True),
    (READ, "GET", FORM + "?usp=sf_link", "Document", True, True),
    (READ, "GET", FORM.replace("/forms/d/", "/forms/u/1/d/"), "Document", True, True),
    (READ, "HEAD", FORM, "Document", True, True),
    (READ, "GET", FORM.replace("Form0000", "Form1111"), "Document", True, False),
    (READ, "GET", "https://accounts.google.com/ServiceLogin?continue=x", "Document", True, False),
    (READ, "GET", "https://docs.google.com/forms/d/e/x/closedform", "Document", True, False),
    (READ, "GET", "https://www.gstatic.com/forms/app.js", "Script", False, True),
    (READ, "GET", "https://lh7-rt.googleusercontent.com/formsz/abc=w740", "Image", False, True),
    (READ, "GET", "https://www.youtube.com/embed/x", "Document", False, True),
    (READ, "POST", FORM, "Document", True, False),
    (READ, "POST", "https://docs.google.com/forms/d/e/x/naLogImpressions", "XHR", False, False),
    (READ, "POST", "https://play.google.com/log", "Ping", False, False),
    (READ, "PUT", "https://example.com/upload", "Fetch", False, False),
    (READ, "OPTIONS", "https://docs.google.com/x", "Preflight", False, False),
    # Sign-in mode: GET anywhere; other methods to sign-in hosts and to non-Google hosts only.
    (SIGN_IN, "GET", "https://accounts.google.com/v3/signin/identifier", "Document", True, True),
    (SIGN_IN, "GET", "https://docs.google.com/forms/", "Document", True, True),
    (SIGN_IN, "GET", FORM, "Document", True, True),
    (
        SIGN_IN,
        "POST",
        "https://accounts.google.com/v3/signin/_/AccountsSignInUi/data",
        "XHR",
        False,
        True,
    ),
    (SIGN_IN, "POST", "https://accounts.google.co.jp/accounts/SetSID", "Document", True, True),
    (SIGN_IN, "POST", "https://accounts.youtube.com/accounts/SetSID", "Document", True, True),
    (SIGN_IN, "POST", "https://www.google.com/a/school.ac.jp/acs", "Document", True, True),
    (SIGN_IN, "POST", "https://www.google.com/recaptcha/api2/reload?k=x", "XHR", False, True),
    (SIGN_IN, "POST", "https://www.recaptcha.net/recaptcha/api2/userverify", "XHR", False, True),
    (
        SIGN_IN,
        "POST",
        "https://sso.school.ac.jp/idp/profile/SAML2/POST/SSO",
        "Document",
        True,
        True,
    ),
    (SIGN_IN, "POST", "https://login.microsoftonline.com/common/login", "Document", True, True),
    (SIGN_IN, "POST", "https://docs.google.com/forms/d/e/x/viewform", "Document", True, False),
    (SIGN_IN, "POST", "https://forms.gle/abc", "Document", True, False),
    (SIGN_IN, "POST", "https://classroom.google.com/u/0/c/x/a/y/submissions", "XHR", False, False),
    (SIGN_IN, "PUT", "https://drive.google.com/upload", "XHR", False, False),
    (SIGN_IN, "POST", "https://www.googleapis.com/classroom/v1/turnIn", "Fetch", False, False),
    (
        SIGN_IN,
        "POST",
        "https://classroom.googleapis.com/v1/courses/1/courseWork/2:turnIn",
        "Fetch",
        False,
        False,
    ),
    (SIGN_IN, "POST", "https://script.google.com/macros/s/x/exec", "Document", True, False),
    (SIGN_IN, "POST", "https://script.googleusercontent.com/macros/echo", "Fetch", False, False),
    (SIGN_IN, "POST", "https://mail.google.com/mail/u/0/", "XHR", False, False),
    (SIGN_IN, "DELETE", "https://docs.google.com/document/d/x", "Fetch", False, False),
    (SIGN_IN, "POST", "https://www.google.com/search", "Document", True, False),
    (SIGN_IN, "POST", "https://www.google.co.jp/a/school.ac.jp/acs", "Document", True, False),
    (SIGN_IN, "POST", "https://play.google.com/log", "Ping", False, False),
    (SIGN_IN, "POST", "https://youtube.com/x", "XHR", False, False),
    (SIGN_IN, "POST", "https://accounts.google.com.evil.example/x", "XHR", False, True),
    # A school's sign-in only over https, and only by name: an IP literal could be Google's.
    (SIGN_IN, "POST", "http://sso.school.ac.jp/idp/SSO", "Document", True, False),
    (SIGN_IN, "POST", "https://142.250.196.110/u/0/batchexecute", "XHR", False, False),
    (SIGN_IN, "POST", "https://[2404:6800:4004:80a::200e]/x", "XHR", False, False),
    (SIGN_IN, "GET", "https://142.250.196.110/", "Document", True, True),
    (SIGN_IN, "POST", "https://evilaccounts.google.com/x", "XHR", False, False),
    # Odd input is refused.
    ("other", "GET", FORM, "Document", True, False),
    (None, "GET", FORM, "Document", True, False),
    (SIGN_IN, "", FORM, "Document", True, False),
    (SIGN_IN, "GET", "file:///etc/passwd", "Document", True, False),
    (SIGN_IN, "GET", "chrome://settings", "Document", True, False),
    (SIGN_IN, "GET", "https://[::1/x", "Document", True, False),
    (READ, "GET", "data:image/png;base64,AAAA", "Image", False, True),
    (READ, "POST", "data:text/plain,x", "Fetch", False, False),
]


@pytest.mark.parametrize("mode, method, url, kind, top, expected", TABLE)
def test_request_table(mode, method, url, kind, top, expected):
    assert guard.allowed(mode, method, url, kind, top_level=top, read_url=FORM) is expected


def test_read_mode_without_a_form_allows_no_page_at_all():
    assert not guard.allowed(READ, "GET", FORM, "Document", top_level=True, read_url="")


@pytest.mark.parametrize(
    "url, wall",
    [
        ("https://accounts.google.com/ServiceLogin?continue=x", True),
        ("https://accounts.google.com/v3/signin/identifier", True),
        ("https://accounts.google.co.jp/v3/signin/identifier", True),
        ("https://www.google.com/accounts/ServiceLogin", True),
        ("https://docs.google.com/forms/d/e/x/viewform", False),
        ("https://sso.school.ac.jp/ServiceLogin", False),
    ],
)
def test_login_wall(url, wall):
    assert guard.login_wall(url) is wall


@pytest.mark.parametrize(
    "host, local",
    [
        ("localhost", True),
        ("LOCALHOST.", True),
        ("127.0.0.53", True),
        ("100.64.0.1", True),
        ("192.168.0.10", True),
        ("::1", True),
        ("minipc", True),
        ("nas.local", True),
        ("docs.google.com", False),
        ("sso.school.ac.jp", False),
        ("8.8.8.8", False),
        ("deadbeef.cafe", False),
        ("2404:6800:4004:80a::200e", False),
    ],
)
def test_local_hosts(host, local):
    assert guard.is_local_host(host) is local


@pytest.mark.parametrize(
    "host, google",
    [
        ("docs.google.com", True),
        ("google.com", True),
        ("www.google.co.jp", True),
        ("www.google.com.hk", True),
        ("lh7-rt.googleusercontent.com", True),
        ("classroom.googleapis.com", True),
        ("forms.gle", True),
        ("youtu.be", True),
        ("www.recaptcha.net", True),
        ("sso.school.ac.jp", False),
        ("notgoogle.com", False),
        ("google.com.evil.example", False),
    ],
)
def test_google_hosts(host, google):
    assert guard.is_google_host(host) is google


# -- walk mode: Google's own 次へ, and nothing else to formResponse

WALK = guard.WALK
TURN = "https://docs.google.com/forms/u/0/d/e/1FAIpQLSdUnitLoginForm0000000000/formResponse"
FORM_ENCODED = "application/x-www-form-urlencoded"


def turn_body(history="0", extra=""):
    return (
        "entry.1=a&fvv=1&partialResponse=%5Bnull%5D&pageHistory="
        + history
        + extra
        + ("&fbzx=-1&submissionTimestamp=-1&continue=1")
    )


def walks(
    url=TURN,
    *,
    method="POST",
    kind="Document",
    top=True,
    ctype=FORM_ENCODED,
    body=None,
    last_from=0,
    mode=WALK,
    read_url=FORM,
):
    return guard.allowed(
        mode,
        method,
        url,
        kind,
        top_level=top,
        read_url=read_url,
        content_type=ctype,
        body=turn_body() if body is None else body,
        last_from=last_from,
    )


def test_a_page_turn_passes_in_walk_mode():
    assert walks()
    assert walks(TURN.replace("/u/0/", "/"))
    assert walks(body=turn_body("0,1"), last_from=1)
    assert walks(ctype=FORM_ENCODED + "; charset=UTF-8")


@pytest.mark.parametrize(
    "change",
    [
        # Not a page turn: the final 送信 (no continue), or anything that looks like one.
        {"body": turn_body().replace("&continue=1", "")},
        {"body": turn_body().replace("continue=1", "continue=0")},
        {"body": turn_body() + "&continue=1"},
        {"body": turn_body() + "&submit=Submit"},
        {"body": turn_body() + "&SUBMIT=1"},
        # Past the last page the walk may leave (never the form's last page), or out of order.
        {"body": turn_body("0,1")},
        {"body": turn_body("0,1,2"), "last_from": 1},
        {"body": turn_body("1")},
        {"body": turn_body("0,2"), "last_from": 5},
        {"body": turn_body("0,0"), "last_from": 5},
        {"body": turn_body("")},
        {"body": turn_body("0") + "&pageHistory=0"},
        {"last_from": -1},
        # Not the page's own form-encoded POST to this form's plain endpoint.
        {"method": "GET"},
        {"method": "PUT"},
        {"kind": "XHR"},
        {"kind": "Document", "top": False},
        {"ctype": "multipart/form-data; boundary=x"},
        {"ctype": None},
        {"body": ""},
        {"body": "%%%"},
        {"url": TURN + "?submit=Submit"},
        {"url": TURN + "#x"},
        {"url": TURN.replace("formResponse", "form%52esponse")},
        {"url": TURN.replace("formResponse", "formresponse")},
        {"url": TURN.replace("Form0000", "Form1111")},
        {"url": TURN.replace("https://", "http://")},
        {"url": TURN.replace("docs.google.com", "docs.google.com:443")},
        {
            "url": "https://example.ac.jp/forms/u/0/d/e/1FAIpQLSdUnitLoginForm0000000000/formResponse"
        },
        {"read_url": ""},
        # Read and sign-in mode never let it through, however it looks.
        {"mode": guard.READ},
        {"mode": guard.SIGN_IN},
    ],
)
def test_anything_else_to_form_response_is_refused_in_walk_mode(change):
    assert not walks(**change)


def test_walk_mode_is_read_mode_for_everything_else():
    for method, url, kind, top, expected in [
        ("GET", FORM + "?usp=pp_url&entry.1=a", "Document", True, True),
        ("GET", "https://lh7-rt.googleusercontent.com/formsz/abc=w740", "Image", False, True),
        ("GET", "https://docs.google.com/forms/d/e/x/closedform", "Document", True, False),
        ("POST", "https://docs.google.com/forms/d/e/x/naLogImpressions", "XHR", False, False),
        ("POST", FORM, "Document", True, False),
        ("GET", "http://127.0.0.1:8771/api/bootstrap", "Image", False, False),
        ("GET", TURN + "?entry.1=a&submit=Submit", "Image", False, False),
    ]:
        assert guard.allowed(WALK, method, url, kind, top_level=top, read_url=FORM) is expected


def test_the_turn_refusal_says_why():
    assert (
        guard.turn_refusal(
            "POST",
            TURN,
            "Document",
            top_level=True,
            read_url=FORM,
            content_type=FORM_ENCODED,
            body=turn_body().replace("&continue=1", ""),
            last_from=0,
        )
        == "no continue=1"
    )
    assert (
        guard.turn_refusal(
            "POST",
            TURN,
            "Document",
            top_level=True,
            read_url=FORM,
            content_type=FORM_ENCODED,
            body=turn_body("0,1"),
            last_from=0,
        )
        == "past the last page it may leave"
    )
    assert (
        guard.turn_refusal(
            "POST",
            TURN,
            "Document",
            top_level=True,
            read_url=FORM,
            content_type=FORM_ENCODED,
            body=None,
            last_from=0,
        )
        == "no readable body"
    )


# -- the draft deletion after a walk (the form's own 「フォームをクリア」)

DELETE = "https://docs.google.com/forms/u/0/d/e/1FAIpQLSdUnitLoginForm0000000000/deletedraft"
DELETE_BODY = "draft_response_action_request=%5Bnull%2C%5B%5D%5D"


def deletes(
    url=DELETE + "?ouid=1&token=t",
    *,
    method="POST",
    kind="XHR",
    ctype=FORM_ENCODED + ";charset=UTF-8",
    body=DELETE_BODY,
    read_url=FORM,
):
    return guard.draft_deletion(method, url, kind, read_url=read_url, content_type=ctype, body=body)


def test_the_forms_own_draft_deletion_is_recognised():
    assert deletes()
    assert deletes(DELETE)
    assert deletes(kind="Fetch")


@pytest.mark.parametrize(
    "change",
    [
        {"url": DELETE.replace("Form0000", "Form1111")},
        {"url": DELETE + "?ouid=1&token=t&submit=Submit"},
        {"url": DELETE.replace("deletedraft", "formResponse")},
        {"url": DELETE.replace("deletedraft", "delete%64raft")},
        {"url": DELETE.replace("https://", "http://")},
        {"url": DELETE.replace("docs.google.com", "example.ac.jp")},
        {"method": "GET"},
        {"kind": "Document"},
        {"ctype": "multipart/form-data; boundary=x"},
        {"body": DELETE_BODY + "&entry.1=a"},
        {"body": "entry.1=a"},
        {"body": None},
        {"read_url": ""},
    ],
)
def test_nothing_else_passes_as_the_draft_deletion(change):
    assert not deletes(**change)


def test_allowed_never_lets_the_deletion_through_by_itself():
    """Only the browser's walk lets it through, once, after the last page turn."""
    for mode in (READ, SIGN_IN, WALK):
        assert not guard.allowed(mode, "POST", DELETE + "?ouid=1&token=t", "XHR", read_url=FORM)
