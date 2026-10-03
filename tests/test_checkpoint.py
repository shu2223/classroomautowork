"""Offline invariants. These tests provide no evidence of a live Google connection."""

import json

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from classroomautowork.auth import (
    SCOPES,
    credentials_for,
    normalize_scope_response,
    normalized_scopes,
)
from classroomautowork.config import Settings, assignment_ids
from classroomautowork.errors import ConfigurationError, PermissionDenied
from classroomautowork.google_read import GoogleReader, retry_read
from classroomautowork.local import require_private_path
from classroomautowork.pending import cutoff_end, discover_pending
from classroomautowork.verify import verify_connection


def test_classroom_detail_link_decodes_actual_shape():
    assert assignment_ids("https://classroom.google.com/c/MTIzNDU2/a/Nzg5MDEy/details") == (
        "123456",
        "789012",
    )
    assert assignment_ids(
        "https://classroom.google.com/u/1/c/MTIzNDU2/a/Nzg5MDEy/details?hl=ja"
    ) == ("123456", "789012")
    with pytest.raises(ConfigurationError):
        assignment_ids("https://classroom.google.com/u/1/a/not-turned-in/all?hl=ja")
    with pytest.raises(ConfigurationError):
        assignment_ids("https://evil.example/c/MTIzNDU2/a/Nzg5MDEy/details")


def test_private_paths_reject_git_and_symlink_target(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(ConfigurationError):
        require_private_path(repo / "ignored" / "credentials.json")
    assert require_private_path(tmp_path / "outside") == tmp_path / "outside"


def test_missing_real_client_cannot_verify(tmp_path):
    settings = Settings("unit-test@example.invalid", tmp_path / "absent.json", tmp_path / "private")
    with pytest.raises(ConfigurationError):
        verify_connection(settings)
    receipt = json.loads((settings.data_dir / "connection-receipt.json").read_text())
    assert receipt["status"] == "failed"
    assert "verified_at" not in receipt


def test_untrusted_oauth_endpoint_is_rejected_before_network(tmp_path):
    client = tmp_path / "bad-client.json"
    client.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "unit-only",
                    "client_secret": "unit-only",
                    "auth_uri": "https://evil.example/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        )
    )
    with pytest.raises(ConfigurationError, match="official OAuth endpoints"):
        credentials_for(Settings("unit-test@example.invalid", client, tmp_path / "private"))


def test_api_scopes_have_no_classroom_or_drive_write_authority():
    google_scopes = [s for s in SCOPES if "/auth/classroom." in s or "/auth/drive" in s]
    assert google_scopes
    assert all(s.endswith(".readonly") for s in google_scopes)


def test_scope_aliases_never_discard_unexpected_authority():
    from requests import Response as TokenResponse

    response = TokenResponse()
    response._content = json.dumps(
        {
            "scope": "email https://www.googleapis.com/auth/userinfo.email "
            "https://www.googleapis.com/auth/classroom.coursework.me.readonly "
            "https://www.googleapis.com/auth/drive",
            "access_token": "unit-only",
        }
    ).encode()
    normalized = normalize_scope_response(response).json()
    assert normalized["access_token"] == "unit-only"
    scopes = set(normalized["scope"].split())
    assert "https://www.googleapis.com/auth/drive" in scopes - set(SCOPES)
    assert "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly" in scopes
    assert "email" not in scopes
    assert normalized_scopes(["email", "https://www.googleapis.com/auth/userinfo.email"]) == {
        "https://www.googleapis.com/auth/userinfo.email"
    }


def test_denied_download_does_not_contact_media_api(tmp_path):
    reader = GoogleReader.__new__(GoogleReader)
    reader._drive = None  # Any media access would fail this test.
    with pytest.raises(PermissionDenied, match="canDownload"):
        reader.download({"capabilities": {"canDownload": False}}, tmp_path / "attachments")
    assert not (tmp_path / "attachments").exists()


def test_retry_transient_but_never_retry_permission_denial(monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr("classroomautowork.google_read.time.sleep", sleeps.append)

    def transient():
        calls.append(1)
        if len(calls) < 3:
            raise HttpError(Response({"status": "503"}), b'{"error":{"message":"unavailable"}}')
        return {"read": True}

    assert retry_read(transient) == {"read": True}
    assert len(calls) == 3 and sleeps == [1, 2]
    calls.clear()

    def denied():
        calls.append(1)
        raise HttpError(Response({"status": "403"}), b'{"error":{"message":"unit-secret-marker"}}')

    with pytest.raises(PermissionDenied) as failure:
        retry_read(denied)
    assert len(calls) == 1
    assert "unit-secret-marker" not in str(failure.value)


class UnitOnlyReader:
    """Selection fixtures, not an authenticated transport."""

    def courses(self):
        return [{"id": "1", "name": "unit fixture"}]

    def assignments(self, course):
        return [
            {
                "id": "10",
                "dueDate": {"year": 2026, "month": 10, "day": 10},
                "dueTime": {"hours": 14, "minutes": 59},
            },
            {
                "id": "11",
                "dueDate": {"year": 2026, "month": 10, "day": 10},
                "dueTime": {"hours": 15},
            },
            {"id": "12"},
            {"id": "13"},
            {"id": "14"},
        ]

    def own_submissions(self, course):
        return [
            {"courseWorkId": "10", "state": "CREATED"},
            {"courseWorkId": "11", "state": "NEW"},
            {"courseWorkId": "12", "state": "RECLAIMED_BY_STUDENT"},
            {"courseWorkId": "13", "state": "TURNED_IN"},
            {"courseWorkId": "14", "state": "RETURNED"},
        ]


def test_tokyo_inclusive_cutoff_and_ambiguous_status():
    result = discover_pending(UnitOnlyReader(), due_before="2026-10-10", timezone="Asia/Tokyo")
    assert [x["assignment_id"] for x in result["assignments"]] == ["10"]
    assert result["assignments"][0]["due_local"].startswith("2026-10-10T23:59")
    assert [x["assignment_id"] for x in result["needs_confirmation"]] == ["14"]
    included = discover_pending(UnitOnlyReader(), due_before="2026-10-10", include_no_due=True)
    assert [x["assignment_id"] for x in included["assignments"]] == ["10", "12"]
    with pytest.raises(ConfigurationError):
        cutoff_end("2026-02-30", "Asia/Tokyo")


def test_pagination_preserves_own_user_filter():
    seen = []

    class Request:
        def __init__(self, token):
            self.token = token

        def execute(self):
            return (
                {"items": [{"id": "b"}]}
                if self.token
                else {"items": [{"id": "a"}], "nextPageToken": "next"}
            )

    def factory(**parameters):
        seen.append(parameters)
        return Request(parameters["pageToken"])

    assert GoogleReader._pages(factory, "items", userId="me") == [{"id": "a"}, {"id": "b"}]
    assert all(x["userId"] == "me" for x in seen)
    assert [x["pageToken"] for x in seen] == [None, "next"]
