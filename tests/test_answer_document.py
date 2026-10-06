"""Local export tests only; fixture generation is not live AI or OAuth evidence."""

import io
import json

import pytest
from docx import Document
from test_frontend import local_server as bounded_loopback_server
from test_workflow import prepared_fixture

from classroomautowork.answer_document import (
    answer_blocks,
    create_answer_document,
    valid_answer_document,
)
from classroomautowork.errors import WorkflowError
from classroomautowork.local import atomic_json, sha256_file
from classroomautowork.review import finalize


def verified_package(tmp_path):
    package, review_path, draft, review = prepared_fixture(tmp_path)
    evidence_id = review["claim_checks"][0]["evidence_ids"][0]
    draft.write_text(
        f"# Offline report\n\n食品安全 [E:{evidence_id}]\n\n"
        "【本机审核说明】Private audit note.\n\n"
        "本人評価：未入力\n\n## 本机证据记录（不进入提交正文）\n\nPrivate evidence index.",
        encoding="utf-8",
    )
    review["requirement_checks"][0]["status"] = "needs_user"
    atomic_json(review_path, review)
    finalize(package, review_path, draft)
    atomic_json(
        package / "codex-generation.json", {"status": "completed", "model": "offline-fixture"}
    )
    return package


def test_verified_report_is_readable_and_reuses_exact_files_without_ai(tmp_path):
    package = verified_package(tmp_path)
    original_hash = sha256_file(package / "draft.md")
    result = create_answer_document(package)
    assert result["status"] == "needs_user"
    assert result["pending_requirements"]
    content = "\n".join(p.text for p in Document(package / "answer.docx").paragraphs)
    assert "食品安全" in content and "本人評価：未入力" in content
    assert "[E:" not in content and "Private evidence index" not in content
    assert "Private audit note" not in content
    assert Document(package / "answer.docx").paragraphs[0].style.name == "Title"
    assert not Document(package / "answer.docx").styles["Title"].element.xpath(".//w:pBdr")
    assert sha256_file(package / "draft.md") == original_hash
    timestamp = (package / "answer.docx").stat().st_mtime_ns
    assert create_answer_document(package) == result
    assert (package / "answer.docx").stat().st_mtime_ns == timestamp
    (package / "answer.docx").write_bytes(b"broken")
    assert valid_answer_document(package) is None
    assert create_answer_document(package)["files"]["answer.docx"] == sha256_file(
        package / "answer.docx"
    )


def test_changed_or_unfinished_ai_output_is_never_exported_as_verified(tmp_path):
    package = verified_package(tmp_path)
    create_answer_document(package)
    atomic_json(package / "codex-generation.json", {"status": "running"})
    assert valid_answer_document(package) is None
    with pytest.raises(WorkflowError, match="尚未完成核验"):
        create_answer_document(package)
    atomic_json(package / "codex-generation.json", {"status": "completed"})
    (package / "draft.md").write_text("unverified modification", encoding="utf-8")
    assert valid_answer_document(package) is None
    with pytest.raises(WorkflowError, match="内容已变化"):
        create_answer_document(package)


def test_report_tables_and_html_are_data_not_instructions(tmp_path):
    package = verified_package(tmp_path)
    text = "# Offline report\n\n|題目|回答|\n|---|---|\n|1|<script>alert(1)</script>|"
    assert answer_blocks(text)[1]["rows"][1][1] == "<script>alert(1)</script>"
    (package / "draft.md").write_text(text, encoding="utf-8")
    receipt_path = package / "review-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["draft_sha256"] = sha256_file(package / "draft.md")
    atomic_json(receipt_path, receipt)
    create_answer_document(package)
    assert "&lt;script&gt;" in (package / "answer.html").read_text(encoding="utf-8")
    assert "<script>" not in (package / "answer.html").read_text(encoding="utf-8")
    assert (
        Document(package / "answer.docx").tables[0].cell(1, 1).text == "<script>alert(1)</script>"
    )


@pytest.fixture
def export_server(tmp_path):
    yield from bounded_loopback_server.__wrapped__(tmp_path)


def test_export_endpoint_preserves_model_and_exposes_an_actual_word_file(export_server):
    from test_frontend import request

    local_server = export_server
    jobs = local_server.jobs
    package = verified_package(jobs.settings.data_dir)
    job_id = "c" * 32
    jobs._jobs[job_id] = {
        "id": job_id,
        "kind": "review",
        "created_at": "offline",
        "status": "completed",
        "events": [],
        "items": [
            {
                "course_id": "1",
                "assignment_id": "2",
                "status": "ready",
                "package": str(package),
                "generation": {"status": "completed", "model": "offline-fixture"},
            }
        ],
    }
    endpoint = f"/api/jobs/{job_id}/items/1:2"
    assert request(local_server, endpoint + "/answer-docx")[0] == 400
    assert (
        request(
            local_server,
            endpoint + "/export-answer",
            method="POST",
            body={},
            origin="https://evil.example",
        )[0]
        == 403
    )
    status, _, body = request(
        local_server,
        endpoint + "/export-answer",
        method="POST",
        body={},
        origin=local_server.origin,
    )
    assert status == 200
    item = json.loads(body)["items"][0]
    assert item["status"] == "needs_user" and item["generation"]["model"] == "offline-fixture"
    status, headers, body = request(local_server, endpoint + "/answer-docx")
    assert status == 200 and "wordprocessingml" in headers["Content-Type"]
    assert ".docx" in headers["Content-Disposition"]
    assert any("食品安全" in p.text for p in Document(io.BytesIO(body)).paragraphs)
