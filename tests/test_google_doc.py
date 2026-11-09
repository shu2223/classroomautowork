"""google_doc.py against a fake Google; nothing here reaches a real Google service."""

import json
import stat
from urllib.parse import parse_qs, urlsplit

import pytest
from test_frontend import unit_settings
from test_result_recovery import failed_report

from classroomautowork import drafting, google_doc
from classroomautowork.answer_document import create_answer_document
from classroomautowork.errors import ConfigurationError, PermissionDenied, WorkflowError
from classroomautowork.local import atomic_json
from classroomautowork.ui_jobs import FrontendJobs

SCHOOL = "student@school.example"
CLIENT = {
    "installed": {
        "client_id": "unit-client.apps.googleusercontent.com",
        "client_secret": "unit-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}
DOC_ID = "1" + "a" * 43


class Response:
    def __init__(self, status, data=None, text=""):
        self.status_code, self._data, self.text, self.encoding = status, data, text, None

    def json(self):
        return self._data


class FakeGoogle:
    """Token, userinfo and Drive endpoints; records every call."""

    def __init__(self):
        self.calls, self.scope = [], " ".join(google_doc.SCOPES)
        self.email, self.exported = SCHOOL, None
        self.files = {}

    def post(self, url, data=None, params=None, headers=None, timeout=None):
        self.calls.append(("post", url, data, params, headers))
        if url == google_doc.TOKEN_URL:
            if data["grant_type"] == "authorization_code":
                if data["code"] != "unit-code" or not data.get("code_verifier"):
                    return Response(400, {"error": "invalid_grant"})
                return Response(
                    200,
                    {
                        "access_token": "unit-access",
                        "refresh_token": "unit-refresh",
                        "scope": self.scope,
                    },
                )
            if data["refresh_token"] != "unit-refresh":
                return Response(400, {"error": "invalid_grant"})
            return Response(200, {"access_token": "unit-access", "scope": self.scope})
        if url == google_doc.UPLOAD_URL:
            assert headers["Authorization"] == "Bearer unit-access"
            file_id = DOC_ID if not self.files else "2" + "b" * 43
            self.files[file_id] = {"trashed": False, "body": data, "headers": headers}
            return Response(
                200,
                {"id": file_id, "name": "Unit", "mimeType": google_doc.GOOGLE_DOC},
            )
        raise AssertionError("unexpected POST " + url)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("get", url, None, params, headers))
        if url == google_doc.USERINFO_URL:
            return Response(200, {"email": self.email, "email_verified": True})
        file_id = url.removeprefix(google_doc.FILES_URL + "/").removesuffix("/export")
        if file_id not in self.files:
            return Response(404, {})
        if url.endswith("/export"):
            return Response(200, text=self.exported if self.exported is not None else self.text())
        return Response(200, {"id": file_id, "trashed": self.files[file_id]["trashed"]})

    def patch(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("patch", url, json, None, headers))
        assert json == {"trashed": True}
        self.files[url.removeprefix(google_doc.FILES_URL + "/")]["trashed"] = True
        return Response(200, {})

    def text(self):
        return "\ufeff" + "\r\n".join(google_doc._pieces(self.blocks))


@pytest.fixture
def google(tmp_path, monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(google_doc, "http", fake)
    atomic_json(tmp_path / google_doc.CLIENT_FILE, CLIENT)
    return fake


def authorize(root, fake, account=SCHOOL):
    started = google_doc.start(root, account)
    state = parse_qs(urlsplit(started["url"]).query)["state"][0]
    return google_doc.finish(
        root, f"http://localhost:8766/?state={state}&code=unit-code&scope=x", account
    )


def test_consent_address_asks_only_for_files_this_program_creates(tmp_path, google):
    started = google_doc.start(tmp_path, SCHOOL)
    query = parse_qs(urlsplit(started["url"]).query)
    assert started["url"].startswith(google_doc.AUTH_URL + "?")
    assert query["scope"] == [
        "openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/drive.file"
    ]
    assert query["redirect_uri"] == ["http://localhost:8766/"]
    assert query["code_challenge_method"] == ["S256"] and query["login_hint"] == [SCHOOL]
    assert query["access_type"] == ["offline"]
    pending = tmp_path / google_doc.PENDING_FILE
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    assert json.loads(pending.read_text())["verifier"] not in started["url"]


def test_finish_saves_the_school_grant_and_never_the_code(tmp_path, google):
    status = authorize(tmp_path, google)
    assert status["authorized"] and status["email"] == SCHOOL and not status["mismatch"]
    grant = tmp_path / google_doc.GRANT_FILE
    assert stat.S_IMODE(grant.stat().st_mode) == 0o600
    assert "unit-code" not in grant.read_text()
    assert not (tmp_path / google_doc.PENDING_FILE).exists()
    assert google_doc.forget(tmp_path, SCHOOL)["authorized"] is False and not grant.exists()


@pytest.mark.parametrize(
    "case", ["state", "host", "port", "denied", "scope", "account", "expired", "reused", "text"]
)
def test_finish_refuses_and_keeps_nothing(tmp_path, google, case):
    started = google_doc.start(tmp_path, SCHOOL)
    state = parse_qs(urlsplit(started["url"]).query)["state"][0]
    url = f"http://localhost:8766/?state={state}&code=unit-code"
    if case == "state":
        url = "http://localhost:8766/?state=other&code=unit-code"
    elif case == "host":
        url = f"https://example.com:8766/?state={state}&code=unit-code"
    elif case == "port":
        url = f"http://localhost:8765/?state={state}&code=unit-code"
    elif case == "denied":
        url = f"http://localhost:8766/?state={state}&error=access_denied"
    elif case == "scope":
        google.scope += " https://www.googleapis.com/auth/drive"
    elif case == "account":
        google.email = "someone@gmail.example"
    elif case == "expired":
        pending = json.loads((tmp_path / google_doc.PENDING_FILE).read_text())
        atomic_json(tmp_path / google_doc.PENDING_FILE, {**pending, "created": 0})
    elif case == "reused":
        url = url.replace("unit-code", "used-code")
    else:
        url = "unit-code"
    with pytest.raises(WorkflowError) as raised:
        google_doc.finish(tmp_path, url, SCHOOL)
    assert "unit-code" not in str(raised.value) and "used-code" not in str(raised.value)
    assert not (tmp_path / google_doc.GRANT_FILE).exists()


def test_no_client_configuration_is_reported(tmp_path, google):
    (tmp_path / google_doc.CLIENT_FILE).unlink()
    assert google_doc.status(tmp_path)["client"] is False
    with pytest.raises(ConfigurationError):
        google_doc.start(tmp_path)


def verified_report(root):
    package, generation, result, _ = failed_report(root)
    drafting.accept_model_result(package, generation, result, json.dumps(result))
    return package


def test_create_uploads_the_word_answer_as_a_doc_and_reads_it_back(tmp_path, google):
    package = verified_report(tmp_path)
    authorize(tmp_path, google)
    google.blocks = create_answer_document(package)["blocks"]
    record = google_doc.create(tmp_path, package, SCHOOL)
    assert record["file_id"] == DOC_ID and record["email"] == SCHOOL
    assert record["url"] == f"https://docs.google.com/document/d/{DOC_ID}/edit"
    upload = google.files[DOC_ID]
    metadata = json.loads(upload["body"].split(b"\r\n\r\n", 1)[1].split(b"\r\n--", 1)[0])
    assert metadata["mimeType"] == google_doc.GOOGLE_DOC and metadata["name"]
    assert (package / "answer.docx").read_bytes() in upload["body"]
    assert upload["headers"]["Content-Type"].startswith("multipart/related; boundary=")
    assert google_doc.current(package)["file_id"] == DOC_ID
    # Only Google's token, identity and Drive endpoints; never Classroom.
    assert all("classroom" not in call[1] for call in google.calls)
    assert {call[1].split("?")[0] for call in google.calls} <= {
        google_doc.TOKEN_URL,
        google_doc.USERINFO_URL,
        google_doc.UPLOAD_URL,
        f"{google_doc.FILES_URL}/{DOC_ID}/export",
    }
    # The same answer reuses its Doc while it exists; a Doc moved to the trash is made again.
    uploads = sum(call[1] == google_doc.UPLOAD_URL for call in google.calls)
    assert google_doc.create(tmp_path, package, SCHOOL)["reused"] is True
    assert sum(call[1] == google_doc.UPLOAD_URL for call in google.calls) == uploads
    google.files[DOC_ID]["trashed"] = True
    assert google_doc.create(tmp_path, package, SCHOOL)["file_id"] != DOC_ID


def test_a_doc_that_does_not_read_back_goes_to_the_trash(tmp_path, google):
    package = verified_report(tmp_path)
    authorize(tmp_path, google)
    google.exported = "something else entirely"
    with pytest.raises(WorkflowError, match="回收站"):
        google_doc.create(tmp_path, package, SCHOOL)
    assert ("patch", f"{google_doc.FILES_URL}/{DOC_ID}") in [c[:2] for c in google.calls]
    assert google.files[DOC_ID]["trashed"] and google_doc.current(package) is None


def test_create_needs_a_grant_for_the_school_account(tmp_path, google):
    package = verified_report(tmp_path)
    with pytest.raises(PermissionDenied, match="还没有授权"):
        google_doc.create(tmp_path, package, SCHOOL)
    authorize(tmp_path, google)
    with pytest.raises(PermissionDenied, match="不是学校账号"):
        google_doc.create(tmp_path, package, "other@school.example")
    assert not google.files


def test_reads_back_ignores_whitespace_but_not_order_or_text():
    blocks = [
        {"kind": "heading", "level": 1, "text": "題名"},
        {"kind": "paragraph", "text": "一行目\n二行目"},
        {"kind": "list", "items": [{"text": "項目A"}, {"text": "項目B"}]},
        {"kind": "table", "rows": [["店舗名", "ユニクロ"]]},
    ]
    assert google_doc.reads_back(
        "﻿題名\r\n\r\n一行目 二行目\n* 項目A\n* 項目B\n店舗名\tユニクロ\n", blocks
    )
    assert not google_doc.reads_back("題名\n項目A\n一行目二行目\n項目B\n店舗名ユニクロ", blocks)
    assert not google_doc.reads_back("題名\n一行目\n項目A\n項目B\n店舗名ユニクロ", blocks)


@pytest.mark.parametrize(
    ("change", "suggested"),
    [
        (None, True),
        ("shared", True),
        ("student_copy", False),
        ("turned_in", False),
        ("forms", False),
        ("question", False),
    ],
)
def test_own_document_is_suggested_only_where_there_is_nowhere_to_write(change, suggested):
    manifest = {
        "assignment": {"workType": "ASSIGNMENT", "materials": []},
        "student_submission": {"state": "CREATED"},
    }
    forms = []
    if change in {"shared", "student_copy"}:
        manifest["assignment"]["materials"] = [
            {
                "driveFile": {
                    "driveFile": {"id": "x"},
                    "shareMode": "EDIT" if change == "shared" else "STUDENT_COPY",
                }
            }
        ]
    elif change == "turned_in":
        manifest["student_submission"]["state"] = "TURNED_IN"
    elif change == "forms":
        forms = [{"url": "https://docs.google.com/forms/d/e/x/viewform"}]
    elif change == "question":
        manifest["assignment"]["workType"] = "SHORT_ANSWER_QUESTION"
    assert google_doc.needs_own_document(manifest, forms) is suggested


def test_frontend_creates_the_doc_and_shows_it_in_the_panel(tmp_path, google):
    package = verified_report(tmp_path)
    settings = unit_settings(tmp_path)
    google.email = settings.school_email
    authorize(tmp_path, google, settings.school_email)
    google.blocks = create_answer_document(package)["blocks"]
    jobs = FrontendJobs(settings)
    identifier = "b" * 32
    jobs._jobs[identifier] = {
        "id": identifier,
        "kind": "review",
        "status": "completed",
        "events": [],
        "ai_confirmed": True,
        "items": [
            {"course_id": "1", "assignment_id": "2", "status": "ready", "package": str(package)}
        ],
    }
    view = jobs.package_view(identifier, "1:2")
    assert view["google_doc"] is None and view["own_document_suggested"] is True
    result = jobs.create_google_doc(identifier, "1:2")
    assert result["document"]["file_id"] == DOC_ID and not result["reused"]
    assert result["job"]["items"][0]["google_doc"]["url"].endswith(f"/{DOC_ID}/edit")
    assert jobs.package_view(identifier, "1:2")["google_doc"]["file_id"] == DOC_ID
