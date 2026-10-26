"""Offline repair regressions; protocol fixtures never represent live inference."""

import json
from pathlib import Path

import pytest
from test_form_coverage import form_candidate
from test_form_fill import answer
from test_frontend import offline_rpc

from classroomautowork import drafting
from classroomautowork.local import atomic_json, sha256_file


def test_source_gap_repair_receives_specific_reason_and_retains_previous_native_answers(
    tmp_path, monkeypatch
):
    package, _, result, form = form_candidate(tmp_path)
    result["requirements_complete"] = True
    result["missing_sources"] = ["The listening recording is not transcribed"]
    known = answer(form, "200", ["UNIT"])
    missing = answer(form, needs_user=True, review_note="缺少本题的听力原文，请核对录音")
    missing["values"] = []
    result["form_answers"] = [known, missing]
    offline_rpc(tmp_path, monkeypatch, package, result["review"])
    atomic_json(tmp_path / "fixture-result.json", result)
    repaired = {**result, "form_answers": [answer(form)], "missing_sources": []}

    def progress(message):
        if str(message).startswith("首轮答案"):
            atomic_json(tmp_path / "fixture-result.json", repaired)

    receipt = drafting.generate_answer(
        package, ai_confirmed=True, model="unit-model", effort="high", progress=progress
    )
    output = Path(receipt["package"])
    generation = receipt["generation"]
    assert generation["form_mapping_complete"] is True
    context = json.loads((output / "form-repair-input.json").read_text(encoding="utf-8"))
    assert context["answers"] == [known]
    assert context["missing_fields"][0]["entry_id"] == "202"
    assert context["missing_fields"][0]["review_note"] == missing["review_note"]
    assert context["missing_fields"][0]["gap_reason"] == "missing_source"
    persisted = json.loads((output / "form-answers.json").read_text(encoding="utf-8"))
    assert {row["entry_id"] for row in persisted["answers"]} == {"200", "202"}
    assert not persisted["deferred_answers"]
    retained = generation["retained_form_answers"]
    assert retained["fields"] == [{"form_url": known["form_url"], "entry_id": "200"}]
    assert retained["source_result_sha256"] == context["source_result_sha256"]
    # Keeping old rows is a recorded validation step, never a fabricated model reply.
    assert json.loads((output / "codex-result.json").read_text(encoding="utf-8")) == repaired
    assert generation["result_sha256"] == sha256_file(output / "codex-result.json")
    assert generation["source_files_sha256"]["form-repair-input.json"] == sha256_file(
        output / "form-repair-input.json"
    )
    requests = [
        json.loads(line)
        for line in (tmp_path / "requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    turns = [request for request in requests if request.get("method") == "turn/start"]
    assert len(turns) == 2
    prompt = turns[1]["params"]["input"][0]["text"]
    assert missing["review_note"] in prompt and known["values"][0] in prompt
    assert "题干中的人物经历不是用户个人信息" in prompt
    assert drafting.reusable_review(output)
    atomic_json(output / "form-repair-input.json", {**context, "answers": []})
    assert drafting.reusable_review(output) is None


@pytest.mark.parametrize("replacement", ["omitted", "deferred"])
def test_valid_prior_answers_survive_a_still_incomplete_repair(tmp_path, monkeypatch, replacement):
    package, _, result, form = form_candidate(tmp_path)
    known = answer(form, "200", ["UNIT"])
    result["form_answers"] = [known]
    offline_rpc(tmp_path, monkeypatch, package, result["review"])
    atomic_json(tmp_path / "fixture-result.json", result)
    blank = {**known, "values": [], "needs_user": True, "review_note": "请确认"}

    def progress(message):
        if str(message).startswith("首轮答案"):
            atomic_json(
                tmp_path / "fixture-result.json",
                {**result, "form_answers": [blank] if replacement == "deferred" else []},
            )

    receipt = drafting.generate_answer(
        package, ai_confirmed=True, model="unit-model", effort="high", progress=progress
    )
    assert receipt["draft_sha256"] and receipt["generation"]["form_mapping_complete"] is False
    record = json.loads(
        (Path(receipt["package"]) / "form-answers.json").read_text(encoding="utf-8")
    )
    assert record["answers"] == [known]
    assert not record["deferred_answers"]
