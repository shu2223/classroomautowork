"""Content completeness gates use native answer coverage, not written field counts."""

import pytest
from test_frontend import unit_settings

from classroomautowork import ui_jobs
from classroomautowork.completion import (
    document_answers_incomplete,
    document_content_gaps,
    document_fill_outcome,
)
from classroomautowork.local import atomic_json
from classroomautowork.ui_jobs import FrontendJobs


def partial_receipt():
    return {
        "status": "partial",
        "documents": [
            {
                "url": "https://docs.google.com/document/d/offline/edit",
                "verified_fields": 10,
                "remaining_required_fields": [
                    {"id": "q2", "label": "最低何人で行うもの？その理由は？", "context": "設問②"},
                    {"id": "q4", "label": "その理由は？", "context": "設問④ ケーススタディ"},
                ],
            }
        ],
    }


def test_verified_writes_do_not_imply_all_questions_are_answered():
    receipt = partial_receipt()
    outcome = document_fill_outcome(receipt)
    assert outcome["status"] == "needs_document" and outcome["requires_regeneration"]
    assert "2 个" in outcome["error"] and "最低何人" in outcome["error"]
    assert document_answers_incomplete({"status": "document_ready", "document_fill": receipt})
    receipt["remaining_required_fields"] = document_content_gaps(receipt)
    assert len(document_content_gaps(receipt)) == 2  # Aggregate receipts are not counted twice.


def test_unmapped_questions_are_incomplete_even_with_no_known_empty_fields():
    receipt = {"documents": [{"unrecognized_questions": [{"label": "設問④", "context": "理由"}]}]}
    assert document_fill_outcome(receipt)["status"] == "needs_document"
    assert document_fill_outcome({"status": "partial"})["status"] == "needs_document"


def test_identity_and_unselected_alternatives_do_not_become_missing_homework():
    receipt = {
        "documents": [{"remaining_required_fields": [], "remaining_empty_fields": ["choice:2"]}],
        "identity_warnings": ["姓名需要核对"],
        "review_cautions": ["候选观点请审阅"],
    }
    outcome = document_fill_outcome(receipt)
    assert outcome["status"] == "document_needs_user"
    assert not outcome["requires_regeneration"]
    assert not document_answers_incomplete({"document_fill": receipt})


def test_partial_fill_job_remains_actionable_and_regenerates_missing_answers(tmp_path, monkeypatch):
    jobs = FrontendJobs(unit_settings(tmp_path))
    monkeypatch.setattr(ui_jobs, "prepare_response_forms", lambda _: [])
    monkeypatch.setattr(ui_jobs, "fill_review", lambda *a, **kw: partial_receipt())
    item = {
        "course_id": "1",
        "assignment_id": "2",
        "package": str(tmp_path),
        "generation": {"status": "completed", "document_answers_sha256": "partial-mapping"},
    }
    job = jobs._start("fill", [item], model="unit-model", effort="high", ai_confirmed=True)
    assert jobs.wait_idle()
    saved = jobs.job(job["id"])
    assert saved["status"] == "completed_with_issues"
    assert saved["items"][0]["status"] == "needs_document"
    assert saved["items"][0]["document_fill"]["documents"][0]["verified_fields"] == 10
    assert not jobs._can_retry_fill(saved["items"][0])
    monkeypatch.setattr(jobs, "_recover_local_answers", lambda _: False)
    calls = []
    monkeypatch.setattr(
        jobs, "start_selected", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    jobs.retry(job["id"])
    assert calls[0][0][0] == [{"course_id": "1", "assignment_id": "2"}]
    assert calls[0][1]["model"] == "unit-model" and calls[0][1]["effort"] == "high"


@pytest.mark.parametrize("status", ["document_ready", "document_needs_user", "ready"])
def test_restart_repairs_stale_success_status_for_concrete_unanswered_questions(tmp_path, status):
    jobs = FrontendJobs(unit_settings(tmp_path))
    identifier = "1" * 32
    atomic_json(
        jobs.root / "jobs" / (identifier + ".json"),
        {
            "id": identifier,
            "kind": "review",
            "status": "completed",
            "created_at": "offline",
            "items": [
                {
                    "course_id": "1",
                    "assignment_id": "2",
                    "status": status,
                    "document_fill": partial_receipt(),
                }
            ],
        },
    )
    restored = FrontendJobs(unit_settings(tmp_path)).job(identifier)
    assert restored["status"] == "completed_with_issues"
    assert restored["items"][0]["status"] == "needs_document"


def test_incomplete_generated_mapping_requires_generation_even_if_it_has_a_hash():
    item = {
        "status": "needs_document",
        "package": "offline",
        "generation": {
            "status": "completed",
            "document_answers_sha256": "partial-mapping",
            "document_mapping_complete": False,
        },
    }
    assert not FrontendJobs._can_retry_fill(item)
