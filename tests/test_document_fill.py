"""Offline native-Doc boundary tests, never represented as OAuth/write verification."""

from copy import deepcopy
from datetime import UTC

import pytest

from classroomautowork.document_fill import (
    fill_personal_document,
    inspect_form,
    plan_fill,
    verify_fill,
)
from classroomautowork.errors import PermissionDenied, WorkflowError


def native_document():
    counter = 1

    def paragraph(text):
        nonlocal counter
        index = counter
        counter += len(text) + 1
        return {
            "startIndex": index,
            "endIndex": counter,
            "paragraph": {
                "elements": [
                    {
                        "startIndex": index,
                        "endIndex": counter,
                        "textRun": {
                            "content": text + "\n",
                            "textStyle": {"fontSize": {"magnitude": 10, "unit": "PT"}},
                        },
                    }
                ],
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            },
        }

    intro = paragraph("氏名：")
    rows = []
    for values in [
        ("〇×", "ちがいについての状況", "判断の理由"),
        ("", "[1]例示された状況", ""),
        ("", "[2]別の状況", ""),
    ]:
        rows.append(
            {
                "tableCells": [
                    {
                        "content": [paragraph(v)],
                        "tableCellStyle": {"backgroundColor": {"color": {"rgbColor": {"red": 1}}}},
                    }
                    for v in values
                ]
            }
        )
    body = [
        intro,
        {"table": {"tableRows": rows, "columns": 3, "rows": 3}},
        paragraph("■まとめ"),
        paragraph(""),
    ]
    return {
        "documentId": "offline-doc",
        "revisionId": "offline-revision",
        "tabs": [
            {
                "tabProperties": {"tabId": "t.0", "title": "課題", "index": 0},
                "documentTab": {"body": {"content": body}},
            }
        ],
    }


def answers(document):
    return [
        {
            "field_id": f["id"],
            "context_sha256": f["context_sha256"],
            "text": "〇" if f["label"] == "〇×" else "授業資料から考えた理由。",
        }
        for f in inspect_form(document)["fields"]
    ]


def apply_insertions(document, plan):
    updated = deepcopy(document)

    def paragraphs(value):
        if isinstance(value, dict):
            if "paragraph" in value:
                yield value
            for item in value.values():
                yield from paragraphs(item)
        elif isinstance(value, list):
            for item in value:
                yield from paragraphs(item)

    by_index = {p["startIndex"]: p for p in paragraphs(updated)}
    for req in plan["requests"]:
        text = req["insertText"]
        by_index[text["location"]["index"]]["paragraph"]["elements"][0]["textRun"]["content"] = (
            text["text"] + "\n"
        )
    updated["revisionId"] = "offline-after"
    return updated


def test_targeted_reverse_insert_preserves_questions_styles_and_second_tab():
    before = native_document()
    before["tabs"].append(
        {
            "tabProperties": {"tabId": "t.1", "title": "変更しない", "index": 1},
            "documentTab": {"body": {"content": []}},
        }
    )
    plan = plan_fill(before, answers(before))
    indices = [r["insertText"]["location"]["index"] for r in plan["requests"]]
    assert indices == sorted(indices, reverse=True)
    assert plan["write_control"] == {"requiredRevisionId": "offline-revision"}
    after = apply_insertions(before, plan)
    receipt = verify_fill(before, after, plan)
    assert receipt["status"] == "filled_verified" and receipt["verified_fields"] == 5
    assert after["tabs"][1] == before["tabs"][1]
    assert plan_fill(after, answers(before))["requests"] == []
    after["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0]["textRun"][
        "content"
    ] = "modified name\n"
    with pytest.raises(WorkflowError, match="非答案"):
        verify_fill(before, after, plan)


def test_existing_user_answer_is_never_overwritten_and_changed_question_is_rejected():
    before = native_document()
    expected = answers(before)
    changed = deepcopy(before)
    cell = changed["tabs"][0]["documentTab"]["body"]["content"][1]["table"]["tableRows"][1][
        "tableCells"
    ][0]
    cell["content"][0]["paragraph"]["elements"][0]["textRun"]["content"] = "本人の回答\n"
    with pytest.raises(WorkflowError, match="保留你已填写"):
        plan_fill(changed, expected)
    changed = deepcopy(before)
    cells = changed["tabs"][0]["documentTab"]["body"]["content"][1]["table"]["tableRows"][1][
        "tableCells"
    ]
    cells[1]["content"][0]["paragraph"]["elements"][0]["textRun"]["content"] = "[1]題目が変わった\n"
    with pytest.raises(WorkflowError, match="原题"):
        plan_fill(changed, expected)


def test_unknown_mapping_private_use_controls_and_internal_citations_fail_closed():
    before = native_document()
    expected = answers(before)
    with pytest.raises(WorkflowError, match="映射"):
        plan_fill(before, [{**expected[0], "field_id": "teacher-template/anywhere"}])
    with pytest.raises(WorkflowError, match="证据"):
        plan_fill(before, [{**expected[0], "text": "回答 [E:internal]"}])
    before["tabs"][0]["documentTab"]["body"]["content"][1]["table"]["tableRows"][1]["tableCells"][
        0
    ]["content"][0]["paragraph"]["elements"].append({"textRun": {"content": "\ue907"}})
    with pytest.raises(WorkflowError, match="映射"):
        plan_fill(before, expected)


class PersonalReader:
    state = "CREATED"
    owned = True

    def personal_document_metadata(self, document_id):
        return {
            "id": document_id,
            "mimeType": "application/vnd.google-apps.document",
            "ownedByMe": self.owned,
            "capabilities": {"canEdit": True},
        }

    def own_submissions(self, course_id):
        assert course_id == "1"
        return [
            {
                "courseWorkId": "2",
                "state": self.state,
                "assignmentSubmission": {"attachments": [{"driveFile": {"id": "offline-doc"}}]},
            }
        ]


class DocsFixture:
    def __init__(self, *, timeout_after_commit=False):
        self.document = native_document()
        self.writes = 0
        self.timeout_after_commit = timeout_after_commit

    def get(self, document_id):
        assert document_id == "offline-doc"
        return deepcopy(self.document)

    def batch(self, plan):
        self.writes += 1
        self.document = apply_insertions(self.document, plan)
        if self.timeout_after_commit:
            raise TimeoutError("offline unknown response")


def test_personal_allowlist_submission_state_unknown_outcome_and_idempotent_retry(tmp_path):
    reader, docs = PersonalReader(), DocsFixture(timeout_after_commit=True)
    expected = answers(docs.document)
    kwargs = {
        "course_id": "1",
        "assignment_id": "2",
        "document_id": "offline-doc",
        "answers": expected,
        "record_dir": tmp_path / "records",
    }
    with pytest.raises(PermissionDenied, match="本人文档"):
        fill_personal_document(reader, docs, **{**kwargs, "document_id": "teacher-template"})
    assert docs.writes == 0
    reader.owned = False
    with pytest.raises(PermissionDenied, match="共享的教师原件"):
        fill_personal_document(reader, docs, **kwargs)
    reader.owned = True
    assert docs.writes == 0
    result = fill_personal_document(reader, docs, **kwargs)
    assert result["status"] == "filled_verified" and docs.writes == 1
    assert (
        fill_personal_document(reader, docs, **kwargs)["inserted_fields"] == 0 and docs.writes == 1
    )
    reader.state = "TURNED_IN"
    with pytest.raises(WorkflowError, match="待完成状态"):
        fill_personal_document(reader, docs, **kwargs)
    assert docs.writes == 1


def test_docs_oauth_is_separate_and_never_broadens_classroom_reader(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timedelta

    from google.oauth2.credentials import Credentials

    from classroomautowork import auth
    from classroomautowork.config import Settings

    client = tmp_path / "client.json"
    client.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "offline-id",
                    "client_secret": "offline-fixture",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        )
    )
    settings = Settings("offline@example.invalid", client, tmp_path).validated()

    def offline_credentials(scopes):
        return Credentials(
            "offline-token",
            refresh_token="offline-refresh",
            client_id="offline-id",
            client_secret="offline-fixture",
            scopes=scopes,
            expiry=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
        ).to_json()

    stored = {
        auth.SERVICE: offline_credentials(auth.SCOPES),
        auth.DOCUMENT_SERVICE: offline_credentials(auth.DOCUMENT_SCOPES),
    }
    original_reader = stored[auth.SERVICE]
    monkeypatch.setattr(auth, "secure_keyring", lambda: None)
    monkeypatch.setattr(
        auth, "Request", lambda: pytest.fail("Offline OAuth fixture must never connect to Google")
    )
    monkeypatch.setattr(auth.keyring, "get_password", lambda service, _: stored.get(service))
    monkeypatch.setattr(
        auth.keyring, "set_password", lambda service, _, value: stored.update({service: value})
    )
    monkeypatch.setattr(auth, "verify_identity", lambda *_: {"offline_fixture": True})
    assert set(auth.credentials_for(settings, documents=True)[0].scopes) == set(
        auth.DOCUMENT_SCOPES
    )
    assert stored[auth.SERVICE] == original_reader
    assert set(auth.credentials_for(settings)[0].scopes) == set(auth.SCOPES)
    stored[auth.SERVICE] = stored[auth.DOCUMENT_SERVICE]
    with pytest.raises(PermissionDenied):
        auth.credentials_for(settings)
