"""Offline recovery tests; never used as evidence of a real model/API connection."""

import json

import pytest
from test_frontend import unit_settings
from test_workflow import prepared_fixture

from classroomautowork import drafting, ui_jobs
from classroomautowork.errors import WorkflowError
from classroomautowork.local import atomic_json, sha256_file
from classroomautowork.ui_jobs import FrontendJobs


def failed_report(root):
    package, _, draft, review = prepared_fixture(root)
    source = review["requirement_checks"][0]["requirement_source_id"]
    review["requirement_checks"][0]["requirement_source_id"] = source[:8] + source[6:8] + source[8:]
    result = {
        "review": review,
        "draft": draft.read_text(encoding="utf-8"),
        "summary": "Offline completed result",
        "requirements_complete": True,
        "missing_sources": [],
        "used_evidence_ids": review["claim_checks"][0]["evidence_ids"],
        "document_answers": [],
        "form_answers": [],
    }
    atomic_json(package / "codex-result.json", result)
    generation = {
        "status": "failed",
        "turn_status": "completed",
        "model": "offline-model",
        "reasoning_effort": "high",
        "thread_id": "original-offline-thread",
        "turn_id": "original-offline-turn",
        "error": "Offline source identifier failure",
        "result_file": "codex-result.json",
        "result_sha256": sha256_file(package / "codex-result.json"),
        "source_files_sha256": {
            name: sha256_file(package / name)
            for name in ("manifest.json", "requirements.json", "evidence.json")
        },
    }
    atomic_json(package / "codex-generation.json", generation)
    return package, generation, result, source


def test_retry_recovers_completed_report_without_download_or_new_ai(tmp_path, monkeypatch):
    package, generation, result, source = failed_report(tmp_path)
    raw = (package / "codex-result.json").read_bytes()
    jobs = FrontendJobs(unit_settings(tmp_path))
    identifier = "a" * 32
    jobs._jobs[identifier] = {
        "id": identifier,
        "kind": "review",
        "status": "completed_with_issues",
        "events": [],
        "model": "offline-model",
        "effort": "high",
        "ai_confirmed": True,
        "items": [
            {"course_id": "1", "assignment_id": "2", "status": "failed", "package": str(package)}
        ],
    }
    monkeypatch.setattr(jobs, "start_selected", lambda *a, **k: pytest.fail("No new job/model"))
    monkeypatch.setattr(ui_jobs, "prepare", lambda *a, **k: pytest.fail("No synchronization"))
    restored = jobs.retry(identifier)
    item = restored["items"][0]
    assert restored["id"] == identifier and jobs._active is None
    assert item["status"] == "needs_user" and item["error"] is None
    assert item["generation"]["model"] == generation["model"]
    assert item["generation"]["turn_id"] == generation["turn_id"]
    assert item["answer_document"]["pending_requirements"] == ["Unit requirement"]
    assert (package / "answer.docx").is_file()
    assert (package / "codex-result.json").read_bytes() == raw
    review = json.loads((package / "review.json").read_text(encoding="utf-8"))
    assert review["requirement_checks"][0]["requirement_source_id"] == source
    assert result["review"]["requirement_checks"][0]["requirement_source_id"] != source
    repairs = item["generation"]["validation_normalizations"]
    assert (
        repairs[0]["original"] == result["review"]["requirement_checks"][0]["requirement_source_id"]
    )
    assert repairs[0]["corrected"] == source
    # A second retry opens the supplement editor and keeps the same file/model.
    timestamp = (package / "answer.docx").stat().st_mtime_ns
    assert jobs.retry(identifier)["next_action"] == "supplement"
    assert (package / "answer.docx").stat().st_mtime_ns == timestamp


@pytest.mark.parametrize("change", ["source", "result", "incomplete"])
def test_recovery_rejects_tampered_inputs_outputs_and_unfinished_turns(tmp_path, change):
    package, generation, _, _ = failed_report(tmp_path)
    if change == "source":
        (package / "requirements.json").write_text("{}", encoding="utf-8")
    elif change == "result":
        (package / "codex-result.json").write_text("{}", encoding="utf-8")
    else:
        generation["turn_status"] = "interrupted"
        atomic_json(package / "codex-generation.json", generation)
    assert drafting.recover_completed_result(package, model="offline-model", effort="high") is None
    assert not (package / "review-receipt.json").exists()
    assert not (package / "answer.docx").exists()


def test_unrecognized_identifier_is_still_rejected_and_ambiguity_is_not_guessed(tmp_path):
    package, generation, result, _ = failed_report(tmp_path)
    result["review"]["requirement_checks"][0]["requirement_source_id"] = "f" * 24
    with pytest.raises(WorkflowError, match="第 1 条要求的来源编号"):
        drafting.accept_model_result(package, generation, result, json.dumps(result))
    assert not (package / "review-receipt.json").exists()
    value = "a" * 12 + "bb" + "a" * 12
    allowed = {"a" * 10 + "bb" + "a" * 12, "a" * 12 + "bb" + "a" * 10}
    review = {"requirement_checks": [{"requirement_source_id": value}]}
    normalized, repairs = drafting._normalize_requirement_sources(review, allowed)
    assert normalized == review and not repairs


def test_model_schema_limits_requirement_sources_to_real_ids():
    ids = ["a" * 24, "b" * 24]
    schema = drafting.output_schema(ids)
    field = schema["properties"]["review"]["properties"]["requirement_checks"]["items"][
        "properties"
    ]["requirement_source_id"]
    assert field == {"type": "string", "enum": ids}
