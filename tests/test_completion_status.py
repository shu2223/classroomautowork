"""Content completeness gates use native answer coverage, not written field counts."""

import pytest
from test_frontend import unit_settings

from classroomautowork import ui_jobs
from classroomautowork.completion import (
    answers_incomplete,
    document_answers_incomplete,
    document_content_gaps,
    document_fill_outcome,
    form_answers_incomplete,
    form_content_gaps,
    form_fill_outcome,
    form_user_input_gaps,
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


def partial_form_receipt(*, legacy=False):
    form = {
        "url": "https://docs.google.com/forms/d/e/offline/viewform",
        "prefilled_fields": 7,
        "remaining_fields": [
            {"entry_id": "1", "title": "授業の要点は？", "required": True},
            {"entry_id": "2", "title": "任意の感想", "required": False},
        ],
    }
    if not legacy:
        form.update(
            remaining_answer_fields=[form["remaining_fields"][0]],
            user_input_fields=[],
            optional_empty_fields=[form["remaining_fields"][1]],
        )
    return {"status": "opened_native_prefill", "forms": [form]}


@pytest.mark.parametrize("legacy", [False, True])
def test_opened_prefill_with_unanswered_required_question_is_not_complete(legacy):
    receipt = partial_form_receipt(legacy=legacy)
    outcome = form_fill_outcome(receipt)
    assert outcome["status"] == "needs_form" and outcome["form_requires_regeneration"]
    assert "1 个必答题" in outcome["error"] and "授業の要点" in outcome["error"]
    assert len(form_content_gaps(receipt)) == 1
    assert form_answers_incomplete({"status": "form_opened", "form_fill": receipt})
    assert answers_incomplete({"status": "form_opened", "form_fill": receipt})
    assert not FrontendJobs._can_retry_fill(
        {
            "status": "needs_form",
            "form_fill": receipt,
            "package": "offline",
            "generation": {"status": "completed"},
        }
    )


def test_required_user_input_needs_action_but_optional_blanks_do_not_regenerate():
    receipt = partial_form_receipt()
    form = receipt["forms"][0]
    form["remaining_answer_fields"] = []
    form["user_input_fields"] = [{"title": "学年", "required": True}]
    outcome = form_fill_outcome(receipt)
    assert outcome["status"] == "needs_form" and not outcome["form_requires_regeneration"]
    assert "学年" in outcome["error"] and "请补充" in outcome["error"]
    assert form_answers_incomplete({"form_fill": receipt})
    assert FrontendJobs._can_retry_fill(
        {
            "status": "needs_form",
            "form_fill": receipt,
            "package": "offline",
            "generation": {"status": "completed"},
        }
    )
    form["user_input_fields"] = [{"title": "任意の所属", "required": False}]
    assert form_fill_outcome(receipt)["status"] == "form_opened"
    assert not form_answers_incomplete({"form_fill": receipt})


@pytest.mark.parametrize("aggregate", [False, True])
def test_legacy_source_gaps_require_answer_repair_instead_of_seventeen_personal_inputs(aggregate):
    gaps = [
        {
            "entry_id": str(index),
            "title": f"{index}. The Ice Hotel question",
            "required": True,
            "review_note": "缺少阅读原文和听力音频，不能确认答案。",
        }
        for index in range(1, 18)
    ]
    fields = {"remaining_answer_fields": [], "user_input_fields": gaps}
    receipt = {"form_mapping_complete": True, **(fields if aggregate else {"forms": [fields]})}
    outcome = form_fill_outcome(receipt)
    assert len(form_content_gaps(receipt)) == 17
    assert not form_user_input_gaps(receipt)
    assert outcome["status"] == "needs_form" and outcome["form_requires_regeneration"]
    assert "17 个必答题缺少作答资料" in outcome["error"]
    assert "必填信息需要补充" not in outcome["error"]
    assert not FrontendJobs._can_retry_fill(
        {"package": "offline", "form_fill": receipt, "generation": {"status": "completed"}}
    )


def test_current_merged_form_coverage_can_complete_an_old_model_mapping():
    receipt = {
        "form_mapping_complete": True,
        "forms": [{"remaining_answer_fields": [], "user_input_fields": []}],
    }
    generation = {"status": "completed", "form_mapping_complete": False}
    assert form_fill_outcome(receipt, generation)["status"] == "form_opened"
    assert not form_answers_incomplete({"form_fill": receipt, "generation": generation})
    assert form_fill_outcome({}, generation)["form_requires_regeneration"]


def test_required_native_controls_direct_user_to_original_form_without_regeneration():
    receipt = {
        "form_mapping_complete": True,
        "forms": [
            {
                "remaining_answer_fields": [],
                "unsupported_fields": [{"title": "添付", "required": True}],
            }
        ],
    }
    outcome = form_fill_outcome(receipt)
    assert outcome["status"] == "needs_form" and not outcome["form_requires_regeneration"]
    assert "原表单操作" in outcome["error"]
    assert form_answers_incomplete({"form_fill": receipt})


def test_partial_form_fill_preserves_receipt_and_retries_generation(tmp_path, monkeypatch):
    jobs = FrontendJobs(unit_settings(tmp_path))
    monkeypatch.setattr(ui_jobs, "prepare_response_forms", lambda _: [{}])
    monkeypatch.setattr(ui_jobs, "fill_response_forms", lambda *a, **kw: partial_form_receipt())
    item = {"course_id": "1", "assignment_id": "2", "package": str(tmp_path)}
    job = jobs._start("fill", [item], model="unit-model", effort="high", ai_confirmed=True)
    assert jobs.wait_idle()
    saved = jobs.job(job["id"])
    assert saved["status"] == "completed_with_issues"
    assert saved["items"][0]["status"] == "needs_form"
    assert saved["items"][0]["form_fill"]["forms"][0]["prefilled_fields"] == 7
    monkeypatch.setattr(jobs, "_recover_local_answers", lambda _: False)
    calls = []
    monkeypatch.setattr(
        jobs, "start_selected", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    jobs.retry(job["id"])
    assert calls[0][0][0] == [{"course_id": "1", "assignment_id": "2"}]
    assert calls[0][1]["model"] == "unit-model"


@pytest.mark.parametrize("legacy", [False, True])
def test_restart_repairs_stale_form_success_without_discarding_answers(tmp_path, legacy):
    jobs = FrontendJobs(unit_settings(tmp_path))
    identifier = "2" * 32
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
                    "status": "form_opened",
                    "form_fill": partial_form_receipt(legacy=legacy),
                    "answer_document": {"files": {"answer.docx": "verified"}},
                }
            ],
        },
    )
    restored = FrontendJobs(unit_settings(tmp_path)).job(identifier)
    assert restored["status"] == "completed_with_issues"
    item = restored["items"][0]
    assert item["status"] == "needs_form" and item["form_requires_regeneration"]
    assert item["answer_document"]["files"]["answer.docx"] == "verified"
