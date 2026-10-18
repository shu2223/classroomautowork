"""A complete local draft does not prove that all native answer slots were filled."""

import json

import pytest
from test_completion import candidate_result
from test_document_fill import native_document

from classroomautowork import drafting
from classroomautowork.document_fill import inspect_form
from classroomautowork.errors import WorkflowError


def test_native_mapping_omissions_are_recorded_even_when_draft_is_valid(tmp_path):
    package, generation, result = candidate_result(tmp_path)
    form = inspect_form(native_document())
    field = form["fields"][0]
    result["document_answers"] = [
        {
            "document_id": form["document_id"],
            "field_id": field["id"],
            "context_sha256": field["context_sha256"],
            "text": "〇",
        }
    ]
    receipt = drafting.accept_model_result(package, generation, result, json.dumps(result), [form])
    assert receipt["draft_sha256"]
    assert receipt["generation"]["document_mapping_complete"] is False
    assert len(receipt["generation"]["missing_document_fields"]) == len(form["fields"]) - 1


def test_duplicate_native_answers_fail_before_finalization(tmp_path):
    package, generation, result = candidate_result(tmp_path)
    form = inspect_form(native_document())
    field = form["fields"][0]
    answer = {
        "document_id": form["document_id"],
        "field_id": field["id"],
        "context_sha256": field["context_sha256"],
        "text": "〇",
    }
    result["document_answers"] = [answer, answer]
    with pytest.raises(WorkflowError, match="重复"):
        drafting.accept_model_result(package, generation, result, json.dumps(result), [form])


@pytest.mark.parametrize("repaired", [True, False])
def test_missing_mapping_gets_one_same_model_repair_and_retains_usable_candidates(
    monkeypatch, repaired
):
    calls = []

    def generate(package, **options):
        calls.append(options)
        return {
            "draft_sha256": "candidate-hash",
            "generation": {"document_mapping_complete": repaired and len(calls) == 2},
        }

    monkeypatch.setattr(drafting, "generate_review", generate)
    receipt = drafting.generate_answer("offline-package", ai_confirmed=True, model="chosen-model")
    assert len(calls) == 2
    assert all(x["model"] == "chosen-model" for x in calls)
    assert receipt["draft_sha256"] == "candidate-hash"
    assert receipt["generation"]["document_mapping_complete"] is repaired
