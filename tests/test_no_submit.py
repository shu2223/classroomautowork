"""Nothing in this program can submit a Google Form or turn in Classroom work, and neither can the AI.

Submitting is always the student's own click in Google Forms, after checking every answer the
prefill link brought in. These checks read the source itself, so a later change that adds a way
to submit fails here; the request guard of the school-login browser has its own table test
(test_browser_guard.py) and a real-Chromium harness check (staging.sh e2e-school). The one
request to formResponse that may pass is a page turn (Google's own 次へ, continue=1) while the
program walks a form's pages to see its later pictures; the final 送信 never does.
"""

import ast
import re
from pathlib import Path

from classroomautowork import auth, browser_guard, claude_agent, form_fill, openai_compat
from classroomautowork import school_browser as sb

SRC = Path(sb.__file__).resolve().parent
MODULES = sorted(SRC.glob("*.py"))


def strings(path):
    """Every string constant of a module except docstrings, with the name it is assigned to."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            for child in ast.walk(node.value) if node.value else []:
                owner[id(child)] = names[0] if names else None
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                yield node.value, owner.get(id(node)), node.lineno


def calls(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            name = (
                function.attr
                if isinstance(function, ast.Attribute)
                else getattr(function, "id", "")
            )
            yield name, node


def test_the_submit_endpoint_is_named_only_to_canonicalise_and_to_block_it():
    found = {
        (path.name, name)
        for path in MODULES
        for value, name, _ in strings(path)
        if "formresponse" in value.lower()
    }
    # form_read: a pasted …/formResponse link is read as its GET viewform page.
    # browser_guard and school_browser: the rule and the second net that block it in Chromium,
    # and the address of a page turn (walk mode) with the second net of a walking page.
    assert found == {
        ("form_read.py", "FORM_PATH"),
        ("browser_guard.py", "SUBMIT_PATH"),
        ("browser_guard.py", "TURN_PATH"),
        ("school_browser.py", "BLOCKED_URL_PATTERNS"),
        ("school_browser.py", "WALK_BLOCKED_URL_PATTERNS"),
    }
    assert browser_guard.submits("https://docs.google.com/forms/d/e/x/formResponse")
    # The rule decodes the path: no spelling of it gets through (SEC-1).
    for spelling in ("form%52esponse", "%66orm%52esponse", "form%2552esponse", "FORMRESPONSE"):
        assert browser_guard.submits("https://docs.google.com/forms/d/e/x/" + spelling)
    web = (SRC / "web" / "app.js").read_text(encoding="utf-8")
    assert "formResponse" not in web and "formresponse" not in web.lower()


def test_no_module_writes_to_google_forms_or_docs_pages():
    """No request to Google Forms or Classroom other than a GET. The only writes are the model
    turn (an OpenAI-compatible endpoint's POST {base}/chat/completions here; the Claude Code
    turn is its own process) and, when the student authorized it, their own Doc through the
    Docs API client (document_fill: documents().batchUpdate). (webapp.py is the server: its
    POST and PUT routes are requests it answers, not ones it sends.)"""
    senders = set()
    for path in MODULES:
        for name, node in calls(path):
            if name not in {"Request", "urlopen", "request", "putrequest", "post", "put", "patch"}:
                continue
            keywords = {k.arg: k.value for k in node.keywords}
            method = keywords.get("method")
            if "data" in keywords or (isinstance(method, ast.Constant) and method.value != "GET"):
                senders.add((path.name, ast.unparse(method) if method is not None else None))
            elif method is not None:
                assert isinstance(method, ast.Constant) and method.value == "GET", (
                    path.name,
                    node.lineno,
                )
    assert senders == {("openai_compat.py", "'POST'")}
    # The Google API client's one write: the student's own Doc, through documents().
    writes = [
        (path.name, ast.unparse(node.func))
        for path in MODULES
        for name, node in calls(path)
        # A Google API client call is a chain of resource calls: x.courses().courseWork()...
        if name in {"batchUpdate", "insert", "update", "delete", "patch", "create", "turnIn"}
        and "()." in ast.unparse(node.func)
        and not ast.unparse(node.func).startswith(("hashlib.", "self._lock"))
    ]
    assert writes == [("document_fill.py", "self.api.documents().batchUpdate")]
    turn = (SRC / "openai_compat.py").read_text(encoding="utf-8")
    assert 'base_url + "/chat/completions"' in turn
    # The anonymous form reader is a GET of the canonical viewform page and nothing else.
    source = (SRC / "form_read.py").read_text(encoding="utf-8")
    assert 'method="GET"' in source and "data=" not in source
    # A pasted submit link is read as its GET viewform page; a prefill link is a viewform GET too.
    assert form_fill.form_url("https://docs.google.com/forms/d/e/abcdefgh/formResponse").endswith(
        "/viewform"
    )


def test_classroom_access_is_read_only_and_nothing_turns_work_in():
    classroom = [scope for scope in auth.SCOPES if "classroom" in scope]
    assert classroom and all(scope.endswith(".readonly") for scope in classroom)
    every_scope = {*auth.SCOPES, *auth.DOCUMENT_SCOPES}
    assert not any("forms" in scope for scope in every_scope)
    assert not any("drive" in scope and not scope.endswith("readonly") for scope in every_scope)
    for path in MODULES:
        for name, node in calls(path):
            assert name not in {"turnIn", "reclaim", "modifyAttachments", "return_"}, (
                path.name,
                node.lineno,
            )
            # Classroom's own write calls on courseWork / studentSubmissions.
            if name in {"patch", "create"}:
                chain = ast.unparse(node.func)
                assert "studentSubmissions" not in chain and "courseWork" not in chain, (
                    path.name,
                    chain,
                )
        text = path.read_text(encoding="utf-8")
        assert "turnIn" not in text and "modifyAttachments" not in text, path.name


def test_the_ai_turns_have_no_tools():
    argv = claude_agent.command_line(
        ["claude"], model="m", effort="high", schema={"type": "object"}
    )
    assert (
        argv[argv.index("--tools") + 1] == ""
        and argv[argv.index("--disallowedTools") + 1] == "mcp__*"
    )
    assert "--bare" in argv and not {"--dangerously-skip-permissions", "--permission-mode"} & set(
        argv
    )
    helper = claude_agent.command_line(["claude"], model="m", effort=None, schema={}, helper=True)
    assert helper[helper.index("--tools") + 1] == ""
    mode = {
        "images": True,
        "stream": True,
        "stream_options": True,
        "effort": None,
        "format": "json_schema",
    }
    body = openai_compat.request_body(
        [{"type": "text", "text": "x"}], {"type": "object"}, "m", mode
    )
    assert not {"tools", "functions", "tool_choice", "function_call"} & set(body)
    source = (SRC / "openai_compat.py").read_text(encoding="utf-8")
    assert '"tools"' not in source and "'tools'" not in source


def test_the_school_browser_reads_without_typing_or_clicking_and_never_reads_cookies():
    import inspect

    reading = inspect.getsource(sb.SchoolBrowser.read_page) + inspect.getsource(
        sb.SchoolBrowser._images
    )
    assert "Input." not in reading and "dispatch" not in reading and "submit" not in reading.lower()
    source = (SRC / "school_browser.py").read_text(encoding="utf-8")
    for forbidden in ("getCookies", "getAllCookies", "Storage.get", "Cookies", "Login Data"):
        assert forbidden not in source
    # Every request of every target goes through the guard, and a submit is refused in any mode.
    for mode in (browser_guard.READ, browser_guard.SIGN_IN, browser_guard.WALK):
        for method in ("GET", "POST"):
            assert not browser_guard.allowed(
                mode,
                method,
                "https://docs.google.com/forms/d/e/x/formResponse",
                "Document",
                top_level=True,
            )
    assert not browser_guard.allowed(
        browser_guard.SIGN_IN, "POST", "https://classroom.google.com/u/0/c/x/a/y/submit", "XHR"
    )
    # The one request the program itself sends from that browser is the account list at 完成,
    # to Google's sign-in host; no other script it runs in a page makes a request.
    scripts = {
        name: value
        for name, value in vars(sb).items()
        if name.endswith(("_SCRIPT", "_DONE")) and isinstance(value, str)
    }
    assert {name for name, value in scripts.items() if "fetch(" in value or "POST" in value} == {
        "ACCOUNTS_SCRIPT"
    }
    assert sb.ACCOUNTS_URL.startswith("https://accounts.google.com/ListAccounts?")
    assert not browser_guard.submits(sb.ACCOUNTS_URL)


def test_the_page_says_the_student_submits():
    web = (SRC / "web" / "app.js").read_text(encoding="utf-8")
    assert "确认无误后由你自己点「送信」。作业助手不会、也无法提交。" in web
    page = (SRC / "web" / "index.html").read_text(encoding="utf-8")
    assert "AI 和程序都不能提交任何内容" in page
    # The page only ever talks to this server (relative paths under BASE); Google links open as
    # ordinary pages in a new tab for the student.
    assert re.findall(r"fetch\(([^,)]+)", web) == [
        "BASE + path",
        'BASE + "/api/school-login/cancel"',
    ]


def test_the_guard_never_rewrites_a_request():
    """SEC-7: a paused request is released unchanged (only its requestId) or failed; the guard
    never answers for the server (Fetch.fulfillRequest) or changes the method, URL, headers or
    body of what it lets through."""
    tree = ast.parse((SRC / "school_browser.py").read_text(encoding="utf-8"))
    released, failed = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        assert not first.value.startswith(
            ("Fetch.fulfillRequest", "Fetch.continueWithAuth", "Fetch.continueResponse")
        ), node.lineno
        if first.value == "Fetch.continueRequest":
            params = node.args[1]
            assert isinstance(params, ast.Dict), node.lineno
            assert [ast.literal_eval(key) for key in params.keys] == ["requestId"], node.lineno
            released.append(node.lineno)
        if first.value == "Fetch.failRequest":
            failed.append(node.lineno)
    assert len(released) == 1 and len(failed) == 1
    text = (SRC / "school_browser.py").read_text(encoding="utf-8")
    assert "fulfillRequest" not in text
    # A body is only read (to judge a page turn), never written.
    import inspect

    reader = inspect.getsource(sb._post_body)
    assert text.count("postData") == reader.count("postData") > 0


def test_the_walk_clicks_only_googles_own_next_and_types_nothing():
    import inspect

    walking = "".join(
        inspect.getsource(function)
        for function in (
            sb.SchoolBrowser.walk_pages,
            sb.SchoolBrowser._turn_pages,
            sb.SchoolBrowser._delete_draft,
            sb.SchoolBrowser._settle,
            sb.SchoolBrowser._click,
            sb.SchoolBrowser._wait_turn,
        )
    )
    for forbidden in ("insertText", "dispatchKeyEvent", "submit(", "Runtime.callFunctionOn"):
        assert forbidden not in walking
    assert walking.count("Input.dispatchMouseEvent") == 1
    # The page script only looks: it clicks, submits and dispatches nothing itself, and finds
    # the one 次へ, never on a page that shows 送信.
    for script in (sb.PAGE_SCRIPT, sb.CLEAR_SCRIPT):
        for forbidden in (".click(", "submit(", "dispatchEvent", "fetch(", "XMLHttpRequest"):
            assert forbidden not in script
    # The clear only deletes the draft the page turns left: after them, once (the guard checks
    # it is this form's deletedraft), and only if a page turned at all.
    assert "if browser.turns_sent:" in walking
    assert "allow_draft_deletion" in inspect.getsource(sb.SchoolBrowser._delete_draft)
    assert "送信" in sb.END_LABELS and not set(sb.NEXT_LABELS) & set(sb.END_LABELS)
    assert "next.length === 1 && !end" in sb.PAGE_SCRIPT
    # The guard's budget: as many turns as pages to go, none leaving the target page.
    assert "turns=target_page - 1, last_from=target_page - 2" in walking
