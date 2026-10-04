"""Offline unit fixtures only. These tests are never counted as live OAuth evidence."""

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from defusedxml.common import EntitiesForbidden
from pypdf import PdfWriter

from classroomautowork.buzz import parse_srt
from classroomautowork.errors import ConfigurationError, WorkflowError
from classroomautowork.extract import extract_document
from classroomautowork.local import atomic_json
from classroomautowork.policy import CoursePolicy
from classroomautowork.review import finalize
from classroomautowork.search import retrieve
from classroomautowork.store import Store, artifact
from classroomautowork.workflow import prepare_assignment, sync_course


def test_restart_reuse_change_and_corruption(tmp_path):
    calls = []

    def operation(key):
        calls.append(key)
        path = tmp_path / key / "value.txt"
        path.parent.mkdir()
        path.write_text("unit test", encoding="utf-8")
        return {"value": len(calls), "artifacts": [artifact(path)]}

    with Store(tmp_path) as store:
        first = store.memo("test", "revision1", operation)
    with Store(tmp_path) as store:
        assert store.memo("test", "revision1", operation) == first
        assert len(calls) == 1
        assert store.memo("test", "revision2", operation)["value"] == 2
        Path(first["artifacts"][0]["path"]).unlink()

        def repair(key):
            path = tmp_path / key / "value.txt"
            path.write_text("repaired", encoding="utf-8")
            return {"artifacts": [artifact(path)]}

        store.memo("test", "revision1", repair)
        assert store.stats == {"reused": 1, "processed": 2, "failed": 0}


def test_failed_stage_retries_and_running_stage_recovers(tmp_path):
    with Store(tmp_path) as store:
        with pytest.raises(WorkflowError):
            store.memo("test", 1, lambda _: (_ for _ in ()).throw(RuntimeError("private")))
        assert store.tasks()[0]["error"] == "Local processing failed."
        assert store.memo("test", 1, lambda _: {"ok": True})["ok"]
        store.db.execute("UPDATE tasks SET status='running'")
        store.db.commit()
    with Store(tmp_path) as store:
        assert store.tasks()[0]["status"] == "interrupted"
        store.memo("test", 1, lambda _: {"ok": True})
        assert store.tasks()[0]["attempts"] == 3


def test_second_writer_is_rejected(tmp_path):
    with Store(tmp_path), pytest.raises(WorkflowError, match="Another local run"):
        with Store(tmp_path):
            pass


def test_scanned_pdf_keeps_image_and_does_not_invent_text(tmp_path):
    path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=400, height=300)
    writer.write(path)
    result = extract_document(path, tmp_path / "processed", CoursePolicy())
    assert not result["chunks"][0]["text"].strip()
    assert Path(result["chunks"][0]["image_path"]).is_file()
    assert "little extractable text" in result["warnings"][0]


def test_document_entities_are_not_executed(tmp_path):
    import zipfile

    path = tmp_path / "evil.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "word/document.xml", '<!DOCTYPE a [<!ENTITY x SYSTEM "file:///private">]><a>&x;</a>'
        )
    with pytest.raises(EntitiesForbidden):
        extract_document(path, tmp_path / "result", CoursePolicy())
    assert not (tmp_path / "result/extraction.json").exists()


def test_transcript_preserves_offset_and_rejects_reversed_time():
    text = "1\n00:00:01,000 --> 00:00:02,500\n実際の講義\n"
    chunks = parse_srt(text, 300)
    assert chunks[0]["start_seconds"] == 301
    assert chunks[0]["end_seconds"] == 302.5
    with pytest.raises(WorkflowError):
        parse_srt(text.replace("00:00:02,500", "00:00:00,500"))


def test_cjk_retrieval_and_course_isolation(tmp_path):
    with Store(tmp_path) as store:
        for course, text in (("1", "食品安全 食中毒について"), ("2", "食品安全 他の授業")):
            store.replace_chunks(
                course,
                "material:1",
                "1",
                [{"source_kind": "material", "title": "資料", "locator": "page 1", "text": text}],
            )
        matches = retrieve(store.chunks("1"), "食中毒")
        assert len(matches) == 1 and matches[0]["course_id"] == "1"
        assert not retrieve(store.chunks("1"), "量子力学")


def test_course_ai_gate_needs_evidence_and_limits(tmp_path):
    assert not CoursePolicy.load(tmp_path, "1").draft_gate()["can_draft"]
    atomic_json(tmp_path / "courses/1.json", {"ai_use": "allowed"})
    with pytest.raises(ConfigurationError, match="evidence"):
        CoursePolicy.load(tmp_path, "1")
    atomic_json(
        tmp_path / "courses/1.json",
        {"ai_use": "limited", "policy_evidence": "User supplied syllabus"},
    )
    with pytest.raises(ConfigurationError, match="limits"):
        CoursePolicy.load(tmp_path, "1")


class ReaderFixture:
    """An explicit offline unit fixture, never used by the production CLI."""

    denied = False
    version = "1"
    downloads = 0
    thumbnail = "unit-thumbnail-1"

    def course(self, cid):
        return {"id": cid, "name": "Unit course"}

    def assignments(self, _):
        return [
            {
                "id": "2",
                "title": "Test requirement",
                "description": "食品安全",
                "materials": [
                    {
                        "driveFile": {
                            "driveFile": {"id": "unit-file-01", "thumbnailUrl": self.thumbnail}
                        }
                    }
                ],
            }
        ]

    def course_materials(self, _):
        return []

    def announcements(self, _):
        return [{"id": "3", "text": "Unit announcement"}]

    def file_metadata(self, file, _key=None):
        return {
            "id": file,
            "name": "unit.txt",
            "mimeType": "text/plain",
            "version": self.version,
            "capabilities": {"canDownload": not self.denied},
        }

    def download(self, metadata, destination, *, progress=None):
        self.downloads += 1
        destination.mkdir(parents=True)
        path = destination / "unit.txt"
        path.write_text("食品安全 unit revision " + self.version, encoding="utf-8")
        return {"path": str(path)}


def test_live_metadata_gate_before_reuse_and_changed_revision(tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path)
    reader = ReaderFixture()
    with Store(tmp_path) as store:
        sync_course(reader, settings, store, "1")
        reader.thumbnail = "unit-thumbnail-2"
        sync_course(reader, settings, store, "1")
        assert reader.downloads == 1
        assert store.stats["processed"] == 4  # two snapshots, one download, one extraction
        reader.version = "2"
        sync_course(reader, settings, store, "1")
        assert reader.downloads == 2
        assert any("revision 2" in c["text"] for c in store.chunks("1"))
        reader.denied = True
        result = sync_course(reader, settings, store, "1")
        assert not any(c["source_id"] == "file:unit-file-01" for c in store.chunks("1"))
        assert "no cached attachment" in result["warnings"][0]["error"]


def test_own_document_is_required_alternate_id_retries_and_unranked_lecture_stays_searchable(
    tmp_path,
):
    from classroomautowork.errors import PermissionDenied

    class OwnDocumentReader(ReaderFixture):
        reads = []

        def assignments(self, cid):
            assignment = super().assignments(cid)[0]
            assignment["materials"][0]["driveFile"] = {
                "shareMode": "STUDENT_COPY",
                "driveFile": {
                    "id": "unit-denied-template",
                    "alternateLink": "https://docs.google.com/document/d/unit-reachable-template/edit",
                },
            }
            return [assignment]

        def course_materials(self, _):
            return [{"id": "9", "title": "Unmatched lecture", "description": "量子力学"}]

        def file_metadata(self, file, key=None):
            self.reads.append(file)
            if file == "unit-denied-template":
                raise PermissionDenied("Offline denied")
            return super().file_metadata(file, key)

    reader = OwnDocumentReader()
    submission = {
        "id": "own-unit",
        "courseWorkId": "2",
        "assignmentSubmission": {
            "attachments": [
                {
                    "driveFile": {
                        "id": "unit-own-document",
                        "alternateLink": "https://docs.google.com/document/d/unit-own-document/edit",
                    }
                }
            ]
        },
    }
    settings = SimpleNamespace(data_dir=tmp_path)
    with Store(tmp_path) as store:
        course = sync_course(reader, settings, store, "1", submissions=[submission])
        result = prepare_assignment(settings, store, course, "2")
    package = Path(result["package"])
    requirements = json.loads((package / "requirements.json").read_text(encoding="utf-8"))
    evidence = json.loads((package / "evidence.json").read_text(encoding="utf-8"))
    assert any(x["source_id"] == "file:unit-own-document" for x in requirements["sources"])
    assert any(x["source_id"] == "material:9" for x in evidence["sources"])
    assert "unit-denied-template" in reader.reads and "unit-reachable-template" in reader.reads
    assert not course["warnings"]


def prepared_fixture(root, ai_use="allowed"):
    atomic_json(
        root / "courses/1.json",
        asdict(CoursePolicy(ai_use=ai_use, policy_evidence="Unit syllabus")),
    )
    settings = SimpleNamespace(data_dir=root)
    reader = ReaderFixture()
    with Store(root) as store:
        course = sync_course(reader, settings, store, "1")
        result = prepare_assignment(settings, store, course, "2")
    package = Path(result["package"])
    sources = json.loads((package / "evidence.json").read_text(encoding="utf-8"))["sources"]
    requirement = next(c for c in sources if c["source_id"] == "coursework:2")
    evidence = next(c for c in sources if c["source_id"] == "file:unit-file-01")
    review = {
        "requirement_checks": [
            {
                "requirement": "Unit requirement",
                "requirement_source_id": requirement["id"],
                "status": "partial",
                "evidence_ids": [evidence["id"]],
            }
        ],
        "questions": [],
        "ai_policy_checked": True,
        "claim_checks": [
            {
                "claim": "食品安全",
                "kind": "sourced",
                "evidence_ids": [evidence["id"]],
                "quotes": [{"evidence_id": evidence["id"], "text": "食品安全"}],
            }
        ],
    }
    draft = package / "draft.md"
    draft.write_text(f"食品安全 [E:{evidence['id']}]", encoding="utf-8")
    path = package / "model-review.json"
    atomic_json(path, review)
    return package, path, draft, review


def test_review_validates_actual_quotes_and_unknown_citations(tmp_path):
    package, path, draft, review = prepared_fixture(tmp_path)
    assert finalize(package, path, draft)["status"] == "ready_for_human_review"
    review["claim_checks"][0]["quotes"][0]["text"] = "Invented quotation"
    atomic_json(path, review)
    with pytest.raises(WorkflowError, match="Quotation"):
        finalize(package, path, draft)
    draft.write_text("Test [E:invented]", encoding="utf-8")
    with pytest.raises(WorkflowError, match="citation"):
        finalize(package, path, draft)


def test_legacy_citations_match_only_exactly_equal_current_evidence(tmp_path):
    package, path, draft, _ = prepared_fixture(tmp_path)
    with Store(tmp_path) as store:
        original = [x for x in store.chunks("1") if x["source_id"] == "file:unit-file-01"]
        store.replace_chunks("1", "file:unit-file-01", "metadata-only-unit-revision", original)
    receipt = finalize(package, path, draft)
    assert original[0]["id"] in receipt["equivalent_current_evidence_ids"]
    with Store(tmp_path) as store:
        changed = {**original[0], "text": "Different readable source"}
        store.replace_chunks("1", "file:unit-file-01", "actual-unit-change", [changed])
    with pytest.raises(WorkflowError, match="Evidence changed"):
        finalize(package, path, draft)


def test_forbidden_ai_stops_answer_draft(tmp_path):
    package, path, draft, review = prepared_fixture(tmp_path, "forbidden")
    with pytest.raises(WorkflowError, match="blocks"):
        finalize(package, path, draft)
    review["questions"] = ["Course prohibits answer drafting; manual completion required."]
    atomic_json(path, review)
    assert finalize(package, path)["status"] == "needs_user_input"


def test_personal_experience_cannot_reference_missing_user_facts(tmp_path):
    package, path, draft, review = prepared_fixture(tmp_path)
    review["claim_checks"] = [
        {"claim": "I attended a lab", "kind": "user_fact", "personal_fact_indices": [0]}
    ]
    atomic_json(path, review)
    with pytest.raises(WorkflowError, match="Personal-experience"):
        finalize(package, path, draft)


def test_tampered_evidence_or_changed_policy_is_rejected(tmp_path):
    package, path, draft, _ = prepared_fixture(tmp_path)
    evidence_path = package / "evidence.json"
    values = json.loads(evidence_path.read_text(encoding="utf-8"))
    values["sources"][0]["text"] = "Forged course content"
    atomic_json(evidence_path, values)
    with pytest.raises(WorkflowError, match="Evidence changed"):
        finalize(package, path, draft)
    atomic_json(
        tmp_path / "courses/1.json",
        asdict(CoursePolicy(ai_use="forbidden", policy_evidence="New unit syllabus")),
    )
    with pytest.raises(WorkflowError, match="policy changed"):
        finalize(package, path, draft)
