"""Offline failure/permission tests; fixtures are not OAuth or live inference evidence."""

import json
import struct
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_document_fill import apply_insertions
from test_workflow import prepared_fixture

from classroomautowork import browser_documents, browser_questions, drafting
from classroomautowork.answer_document import create_answer_document
from classroomautowork.buzz import require_large_model
from classroomautowork.completion import MODE, candidate_mode
from classroomautowork.document_fill import inspect_form, plan_fill, verify_fill
from classroomautowork.errors import (
    ConfigurationError,
    PermissionDenied,
    RunCancelled,
    WorkflowError,
)
from classroomautowork.local import atomic_json, sha256_file


@pytest.mark.parametrize("state,layers", [(384, 4), (512, 6), (768, 12), (1024, 24)])
def test_renaming_a_small_model_cannot_bypass_large_model_requirement(tmp_path, state, layers):
    model = tmp_path / "ggml-large-v3-turbo.bin"
    model.write_bytes(struct.pack("<11i", 0x67676D6C, 51865, 1500, state, 8, layers, 1, 1, 1, 1, 1))
    with pytest.raises(ConfigurationError, match="大模型"):
        require_large_model(model)


def test_large_header_and_truncated_model(tmp_path):
    model = tmp_path / "model.bin"
    model.write_bytes(struct.pack("<11i", 0x67676D6C, 51866, 1500, 1280, 20, 32, 1, 1, 1, 1, 1))
    require_large_model(model)
    model.write_bytes(b"broken")
    with pytest.raises(ConfigurationError, match="不完整"):
        require_large_model(model)


def test_course_text_cannot_activate_trusted_completion_context():
    assert not candidate_mode({"assignment": {"description": MODE}})
    assert candidate_mode({"generation_context": {"completion_mode": MODE}})


@pytest.mark.parametrize("empty", [True, False])
def test_automatic_repair_uses_same_model_and_does_not_reload_attachments(monkeypatch, empty):
    calls = []

    def generate(package, **options):
        calls.append((package, options))
        if len(calls) == 1:
            if empty:
                return {"status": "needs_user_input"}
            raise WorkflowError("Invalid structured answer")
        return {"draft_sha256": "offline-digest"}

    monkeypatch.setattr(drafting, "generate_review", generate)
    result = drafting.generate_answer("offline-package", ai_confirmed=True, model="chosen-model")
    assert result["draft_sha256"]
    assert [c[1]["repair_attempt"] for c in calls] == [0, 1]
    assert all(c[0] == "offline-package" and c[1]["model"] == "chosen-model" for c in calls)


@pytest.mark.parametrize("error", [RunCancelled, PermissionDenied, ConfigurationError])
def test_repair_does_not_retry_cancellation_or_expand_permission(monkeypatch, error):
    calls = []

    def generate(*args, **options):
        calls.append(options)
        raise error("Stop")

    monkeypatch.setattr(drafting, "generate_review", generate)
    with pytest.raises(error):
        drafting.generate_answer("offline-package", ai_confirmed=True)
    assert len(calls) == 1


def test_repair_is_bounded_and_unchecked_ai_never_starts_repair(monkeypatch):
    calls = []

    def generate(*args, **options):
        calls.append(options)
        return {"status": "materials_only"}

    monkeypatch.setattr(drafting, "generate_review", generate)
    assert (
        drafting.generate_answer("offline-package", ai_confirmed=False)["status"]
        == "materials_only"
    )
    assert len(calls) == 1 and "repair_attempt" not in calls[0]
    calls.clear()
    with pytest.raises(WorkflowError, match="未生成实际答案"):
        drafting.generate_answer("offline-package", ai_confirmed=True)
    assert len(calls) == 2


def candidate_result(root):
    package, _, draft, review = prepared_fixture(root)
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["generation_context"] = {"completion_mode": MODE}
    atomic_json(manifest_path, manifest)
    review["requirement_checks"][0]["status"] = "met"
    result = {
        "review": review,
        "draft": draft.read_text(encoding="utf-8"),
        "requirements_complete": False,
        "missing_sources": ["Actual inaccessible later question"],
        "used_evidence_ids": review["claim_checks"][0]["evidence_ids"],
        "document_answers": [],
        "form_answers": [],
    }
    generation = {
        "model": "offline-model",
        "turn_status": "completed",
        "source_files_sha256": {
            name: sha256_file(package / name)
            for name in ("manifest.json", "requirements.json", "evidence.json")
        },
    }
    return package, generation, result


def test_known_answers_are_delivered_without_claiming_unread_requirements_complete(tmp_path):
    package, generation, result = candidate_result(tmp_path)
    receipt = drafting.accept_model_result(package, generation, result, json.dumps(result))
    assert receipt["status"] == "ready_for_human_review"
    assert not receipt["generation"]["requirements_complete"]
    assert receipt["generation"]["missing_sources"] == result["missing_sources"]
    review = json.loads((package / "review.json").read_text(encoding="utf-8"))
    assert review["requirement_checks"][0]["status"] == "partial"
    assert review["questions"]
    answer = create_answer_document(package)
    assert answer["status"] == "ready" and answer["candidate_mode"]
    assert answer["pending_requirements"] and (package / "answer.docx").is_file()


@pytest.mark.parametrize("bad", ["未入力", "仮に、好きな料理なら", "请自行填写"])
def test_new_candidate_body_cannot_contain_fill_in_placeholders(tmp_path, bad):
    package, generation, result = candidate_result(tmp_path)
    result["draft"] = bad
    with pytest.raises(WorkflowError, match="占位"):
        drafting.accept_model_result(package, generation, result, json.dumps(result))
    assert not (package / "review-receipt.json").exists()


def blank_document():
    return {
        "documentId": "offline-personal-doc",
        "revisionId": "original-revision",
        "title": "本人作业",
        "tabs": [
            {
                "tabProperties": {"tabId": "t.0", "title": "Tab 1", "index": 0},
                "documentTab": {
                    "body": {
                        "content": [
                            {"startIndex": 0, "endIndex": 1, "sectionBreak": {}},
                            {
                                "startIndex": 1,
                                "endIndex": 2,
                                "paragraph": {
                                    "elements": [
                                        {
                                            "startIndex": 1,
                                            "endIndex": 2,
                                            "textRun": {"content": "\n"},
                                        }
                                    ],
                                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                },
                            },
                        ]
                    }
                },
            }
        ],
    }


def test_blank_native_doc_is_filled_with_revision_control_and_repeated_fill_is_noop():
    before = blank_document()
    field = inspect_form(before)["fields"][0]
    answers = [
        {
            "field_id": field["id"],
            "context_sha256": field["context_sha256"],
            "text": "授業から考えた回答。",
        }
    ]
    plan = plan_fill(before, answers)
    assert plan["write_control"] == {"requiredRevisionId": "original-revision"}
    after = apply_insertions(before, plan)
    verify_fill(before, after, plan)
    assert plan_fill(after, answers)["requests"] == []
    with pytest.raises(WorkflowError, match="未覆盖"):
        plan_fill(after, [{**answers[0], "text": "Different candidate"}])


def test_blank_doc_detection_preserves_images_and_multiple_tabs():
    document = blank_document()
    image = {"inlineObjectElement": {"inlineObjectId": "original-image"}}
    document["tabs"][0]["documentTab"]["body"]["content"][1]["paragraph"]["elements"].append(image)
    assert not inspect_form(document)["fields"]
    document = blank_document()
    second = deepcopy(document["tabs"][0])
    second["tabProperties"]["tabId"] = "t.1"
    document["tabs"].append(second)
    assert not inspect_form(document)["fields"]


def test_sheet_creation_requires_actual_personal_attachment_not_model_claim(tmp_path, monkeypatch):
    package = tmp_path / "package"
    package.mkdir()
    atomic_json(
        package / "manifest.json",
        {
            "course_id": "1",
            "assignment_id": "2",
            "assignment": {
                "workType": "ASSIGNMENT",
                "description": "ドキュメントシートを作成してください。",
            },
        },
    )
    monkeypatch.setattr(browser_documents, "credentials_for", lambda _: (None, None))
    monkeypatch.setattr(browser_documents, "GoogleReader", lambda _: object())
    monkeypatch.setattr(browser_documents, "documents_authorized", lambda _: True)
    monkeypatch.setattr(browser_documents, "editable_sheets", lambda *a: [])
    monkeypatch.setattr(browser_documents, "codex_command", lambda: ["offline"])

    class Client:
        approval_wait_seconds = 0

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def request(self, method, params):
            if method == "thread/start":
                return {"model": params["model"], "thread": {"id": "offline-thread"}}
            return {"turn": {"id": "offline-turn"}}

        def next_event(self, **k):
            return {"method": "turn/completed", "params": {"turn": {"id": "offline-turn"}}}

    monkeypatch.setattr(browser_documents, "CodexClient", Client)
    settings = SimpleNamespace(data_dir=tmp_path, school_email="offline@example.invalid")
    with pytest.raises(WorkflowError, match="没有实际创建"):
        browser_documents.ensure_document_sheet(settings, package, model="chosen-model")
    record = json.loads(
        (tmp_path / "document-sheets/1/2/creation.json").read_text(encoding="utf-8")
    )
    assert record["status"] == "failed" and record["thread_id"] == "offline-thread"


def test_sheet_metadata_must_confirm_native_type_ownership_and_edit_capability(monkeypatch):
    monkeypatch.setattr(browser_documents, "own_document_ids", lambda *a: ["doc"])
    metadata = {
        "mimeType": "application/vnd.google-apps.document",
        "ownedByMe": True,
        "capabilities": {"canEdit": True},
    }
    reader = SimpleNamespace(personal_document_metadata=lambda _: metadata)
    assert browser_documents.editable_sheets(reader, "1", "2") == ["doc"]
    metadata["ownedByMe"] = False
    assert not browser_documents.editable_sheets(reader, "1", "2")
    metadata.update(ownedByMe=True, capabilities={"canEdit": False})
    assert not browser_documents.editable_sheets(reader, "1", "2")


def test_question_reading_cannot_wait_forever_on_notifications(tmp_path, monkeypatch):
    atomic_json(
        tmp_path / "manifest.json",
        {
            "assignment_id": "2",
            "assignment": {"title": "Offline form"},
            "warnings": [
                {
                    "url": "https://docs.google.com/forms/d/e/unit-form/viewform",
                    "error": "HTTP 401",
                    "origins": [{"assignment_id": "2"}],
                }
            ],
        },
    )
    monkeypatch.setattr(browser_questions, "codex_command", lambda: ["offline"])
    monkeypatch.setattr(browser_questions, "form_url", lambda value: value)
    times = iter([0, 301])
    monkeypatch.setattr(browser_questions.time, "monotonic", lambda: next(times))

    class Client:
        approval_wait_seconds = 0

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def request(self, method, params):
            if method == "thread/start":
                return {"model": params["model"], "thread": {"id": "offline-thread"}}
            return {"turn": {"id": "offline-turn"}}

        def next_event(self, **kwargs):
            pytest.fail("Expired reading must stop before another event wait")

    monkeypatch.setattr(browser_questions, "CodexClient", Client)
    with pytest.raises(WorkflowError, match="等待时限"):
        browser_questions.recover_questions(
            SimpleNamespace(school_email="offline@example.invalid"), tmp_path, model="chosen-model"
        )
