"""Recover verified cached answers without mistaking template labels for user text."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_document_fill import PersonalReader, apply_inline_edits, native_document

from classroomautowork import auth, document_fill, drafting, google_read
from classroomautowork.document_fill import digest, inspect_form, template_matches_source
from classroomautowork.document_slots import utf16_length
from classroomautowork.errors import WorkflowError
from classroomautowork.local import atomic_json, sha256_file

LINES = [
    "■設問①　架空の練習問題",
    "この実験について考えた理由を書きなさい。",
    "選んだ理由：",
    "■設問②　別の練習問題",
    "もう一つの実験について考えた理由を書きなさい。",
    "選んだ理由：",
]
DRAFT = "設問①\n選んだ理由：第一の候補文。[E:source]\n\n設問②\n選んだ理由：第二の候補文。"


class LabeledDocs:
    def __init__(self):
        self.document = native_document()
        sample = self.document["tabs"][0]["documentTab"]["body"]["content"][0]
        content, offset = [], 1
        for text in LINES:
            paragraph = deepcopy(sample)
            end = offset + utf16_length(text + "\n")
            paragraph.update(startIndex=offset, endIndex=end)
            run = paragraph["paragraph"]["elements"][0]
            run.update(startIndex=offset, endIndex=end)
            run["textRun"]["content"] = text + "\n"
            content.append(paragraph)
            offset = end
        self.document["tabs"][0]["documentTab"]["body"]["content"] = content
        self.writes = 0

    def get(self, identifier):
        assert identifier == "offline-doc"
        return deepcopy(self.document)

    def batch(self, plan):
        self.writes += 1
        self.document = apply_inline_edits(self.document, plan)


def stale_answers():
    return [
        {
            "document_id": "offline-doc",
            "field_id": "t.0/after:" + digest(LINES[1])[:16],
            "context_sha256": digest(LINES[1]),
            "text": "選んだ理由：第一の候補文。",
        }
    ]


@pytest.mark.parametrize("cached_answers", [stale_answers(), []])
@pytest.mark.parametrize("source_integrity", [True, False])
def test_fill_review_recovers_all_labels_and_keeps_model_artifacts_immutable(
    tmp_path, monkeypatch, cached_answers, source_integrity
):
    package = tmp_path / "package"
    package.mkdir()
    atomic_json(
        package / "manifest.json",
        {
            "course_id": "1",
            "assignment_id": "2",
            "ai_confirmation": True,
            "policy": {"can_draft": True},
        },
    )
    atomic_json(
        package / "requirements.json",
        {
            "sources": [
                {
                    "source_id": "file:offline-doc",
                    "text": "\n".join(LINES),
                }
            ]
        },
    )
    (package / "draft.md").write_text(DRAFT, encoding="utf-8")
    atomic_json(package / "document-answers.json", {"answers": cached_answers})
    atomic_json(
        package / "codex-generation.json",
        {
            "document_answers_sha256": sha256_file(package / "document-answers.json"),
            "source_files_sha256": {
                "requirements.json": sha256_file(package / "requirements.json"),
            },
            "model": "original-model",
            "thread_id": "original-turn",
        },
    )
    if not source_integrity:
        # Keep the actual source text unchanged so its template comparison would
        # still pass. Only the authenticated package-byte binding detects this edit.
        requirements_path = package / "requirements.json"
        requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
        requirements["edited_after_review"] = True
        atomic_json(requirements_path, requirements)
    original_hashes = {p.name: sha256_file(p) for p in package.iterdir()}
    reader, docs = PersonalReader(), LabeledDocs()
    reader.file_metadata = lambda _: {"mimeType": "application/vnd.google-apps.document"}
    monkeypatch.setattr(auth, "credentials_for", lambda *a, **kw: (None, None))
    monkeypatch.setattr(
        drafting, "reusable_review", lambda _: {"draft_sha256": sha256_file(package / "draft.md")}
    )
    monkeypatch.setattr(google_read, "GoogleReader", lambda _: reader)
    monkeypatch.setattr(document_fill, "GoogleDocuments", lambda _: docs)
    settings = SimpleNamespace(data_dir=tmp_path)
    if not source_integrity:
        monkeypatch.setattr(
            "classroomautowork.document_answer_recovery.recover_labeled_answers",
            lambda *_: pytest.fail("Changed source bytes must never reach mapping recovery"),
        )
        with pytest.raises(WorkflowError):
            document_fill.fill_review(settings, package)
        assert docs.writes == 0
        assert not any(f["current"] for f in inspect_form(docs.document)["fields"])
        assert not list((tmp_path / "document-fills").rglob("mapping-recovery.json"))
        assert all(sha256_file(package / name) == value for name, value in original_hashes.items())
        return
    receipt = document_fill.fill_review(settings, package)
    assert receipt["status"] == "filled_verified"
    assert receipt["documents"][0]["verified_fields"] == 2
    assert receipt["documents"][0]["mapping_recovery"]["answer_count"] == 2
    assert [f["current"] for f in inspect_form(docs.document)["fields"]] == [
        "第一の候補文。",
        "第二の候補文。",
    ]
    assert docs.writes == 1
    replay = document_fill.fill_review(settings, package)
    assert replay["documents"][0]["inserted_fields"] == 0
    assert docs.writes == 1
    assert all(sha256_file(package / name) == value for name, value in original_hashes.items())
    records = list((tmp_path / "document-fills").rglob("mapping-recovery.json"))
    assert len(records) == 2
    assert json.loads(records[0].read_text(encoding="utf-8"))["original_answers"] == cached_answers


def test_recovery_rejects_changed_questions_even_when_section_and_labels_still_match(tmp_path):
    docs = LabeledDocs()
    changed_source = "\n".join(LINES).replace("この実験", "変更前の実験")
    with pytest.raises(WorkflowError, match="原题与已核验初稿"):
        document_fill.fill_personal_document(
            PersonalReader(),
            docs,
            course_id="1",
            assignment_id="2",
            document_id="offline-doc",
            answers=stale_answers(),
            record_dir=tmp_path,
            verified_draft=DRAFT,
            original_text=changed_source,
        )
    assert docs.writes == 0


def test_template_source_check_tolerates_export_wrapping_but_not_reordering():
    doc = LabeledDocs().document
    assert template_matches_source(doc, " \n".join(LINES).replace("実験", "実\n験"))
    assert not template_matches_source(doc, "\n".join(reversed(LINES)))
    assert not template_matches_source(doc, "")


@pytest.mark.parametrize("index", [1, 4])
def test_template_source_check_rejects_deleted_question_paragraphs(index):
    doc = LabeledDocs().document
    del doc["tabs"][0]["documentTab"]["body"]["content"][index]
    assert not template_matches_source(doc, "\n".join(LINES))
