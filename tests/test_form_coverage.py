"""Offline coverage checks distinguish omitted work from optional/personal blanks."""

import copy
import json

import pytest
from test_completion import candidate_result
from test_form_fill import answer, fill_fixture, native_form
from test_forms_student import URL
from test_frontend import offline_rpc, unit_settings

from classroomautowork import drafting, form_fill
from classroomautowork.form_read import form_chunks
from classroomautowork.local import atomic_json, sha256_file
from classroomautowork.student import StudentProfile
from classroomautowork.ui_jobs import FrontendJobs
from classroomautowork.user_answers import save_user_answers


def test_required_question_omission_is_detected_but_optional_feedback_is_optional():
    form = native_form()
    coverage = form_fill.form_answer_coverage([form], [answer(form, "200", ["UNIT"])])
    assert [x["entry_id"] for x in coverage["missing_fields"]] == ["202"]
    assert [x["entry_id"] for x in coverage["optional_empty_fields"]] == ["204"]
    coverage = form_fill.form_answer_coverage([form], [answer(form, "200", ["UNIT"]), answer(form)])
    assert not coverage["missing_fields"]


def test_unknown_identity_and_explicit_deferred_fact_remain_user_inputs():
    form = native_form()
    form["questions"][0]["title"] = "学年"
    deferred = answer(form, "202", needs_user=True, review_note="実際の記録を本人が確認")
    deferred["values"] = []
    coverage = form_fill.form_answer_coverage([form], [deferred], StudentProfile())
    assert not coverage["missing_fields"]
    assert [x["entry_id"] for x in coverage["user_input_fields"]] == ["200", "202"]
    assert "本人" in coverage["user_input_fields"][1]["review_note"]


@pytest.mark.parametrize(
    ("note", "reason"),
    [
        ("缺少原文及听力音频，无法判断，需要本人核对。", "missing_source"),
        ("当前包中未包含阅读文章。", "missing_source"),
        ("The listening transcript is not provided.", "missing_source"),
        ("本文と音声が未収録のため確認できません。", "missing_source"),
        ("第3页第1题：缺少冰旅馆原文，无法判断受欢迎程度。", "missing_source"),
        ("第3页第2题：缺少原文中的旅馆规模说明。", "missing_source"),
        ("第4页第1题：缺少会话中的目的地信息，无法判断是否为海滩。", "missing_source"),
        ("第4页第8题：缺少女子对住宿的评价，无法判断是否失望。", "missing_source"),
        ("请本人核对这道题。", "missing_answer"),
        ("", "missing_answer"),
    ],
)
def test_review_flag_does_not_relabel_academic_question_as_personal_information(note, reason):
    form = native_form()
    form["questions"][1]["title"] = "6. Why do you need to wear gloves in the bar?"
    deferred = answer(form, needs_user=True, review_note=note)
    deferred["values"] = []
    coverage = form_fill.form_answer_coverage([form], [answer(form, "200", ["UNIT"]), deferred])
    assert not coverage["user_input_fields"]
    assert coverage["missing_fields"][0]["gap_reason"] == reason
    assert coverage["missing_fields"][0]["review_note"] == note
    assert [x["entry_id"] for x in coverage["missing_fields"]] == ["202"]


def test_candidate_with_review_flag_still_fills_academic_answer():
    form = native_form()
    candidate = answer(form, needs_user=True, review_note="请核对教材表述。")
    coverage = form_fill.form_answer_coverage([form], [answer(form, "200", ["UNIT"]), candidate])
    assert not coverage["user_input_fields"] and not coverage["missing_fields"]
    assert candidate in form_fill.validate_answers([form], [candidate])


@pytest.mark.parametrize(
    ("title", "note"),
    [
        ("健康状況", "缺少本人健康资料，需要本人提供实际记录。"),
        ("本人の実際の出席日数", "資料に含まれていません。"),
        ("Your own health record", "The source material is not available."),
    ],
)
def test_missing_personal_records_are_not_repaired_as_missing_teaching_material(title, note):
    assert form_fill.form_gap_reason(title, note) == "personal_fact"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("1) 氏名", "name"),
        ("2) 所属学科", "department"),
        ("3) 学年", "year"),
        ("１） 学籍番号", "student_id"),
        ("①氏名", "name"),
        ("学年制度を説明して", None),
    ],
)
def test_numbered_identity_labels_are_exact_fields_not_academic_question_matches(label, expected):
    assert form_fill.identity_key(label) == expected


def test_prompt_requests_ordinary_optional_feedback_without_invented_personal_facts(tmp_path):
    package, _, _ = candidate_result(tmp_path)
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    skill = tmp_path / "skill"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("Offline skill", encoding="utf-8")
    (skill / "references/review-format.md").write_text("Offline format", encoding="utf-8")
    inputs, _ = drafting.build_input(package, skill, manifest)
    prompt = inputs[0]["text"]
    assert "每个必填栏都要返回一条记录" in prompt
    assert "普通的非必填感想、自由意见或其他意见栏也给出自然的候选回答" in prompt
    assert "不要声称用户没有照顾需求" in prompt


def form_candidate(tmp_path):
    package, generation, result = candidate_result(tmp_path)
    form = native_form()
    requirements_path = package / "requirements.json"
    requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
    requirements["response_forms"] = [{"url": URL}]
    requirements["sources"].extend(form_chunks(form))
    atomic_json(requirements_path, requirements)
    generation["source_files_sha256"]["requirements.json"] = sha256_file(requirements_path)
    return package, generation, result, form


def test_accepting_valid_candidate_keeps_concrete_missing_form_fields(tmp_path):
    package, generation, result, form = form_candidate(tmp_path)
    result["form_answers"] = [answer(form, "200", ["UNIT"])]
    receipt = drafting.accept_model_result(package, generation, result, json.dumps(result))
    assert receipt["draft_sha256"]
    assert receipt["generation"]["form_mapping_complete"] is False
    assert [x["entry_id"] for x in receipt["generation"]["missing_form_fields"]] == ["202"]
    assert (package / "form-answers.json").is_file()


def test_deferred_native_rows_survive_validation_and_are_not_lost_from_receipt(tmp_path):
    package, generation, result, form = form_candidate(tmp_path)
    deferred = answer(form, "200", needs_user=True, review_note="本人の学籍番号を確認")
    deferred["values"] = []
    result["form_answers"] = [deferred, answer(form)]
    receipt = drafting.accept_model_result(package, generation, result, json.dumps(result))
    assert receipt["generation"]["form_mapping_complete"] is True
    assert receipt["generation"]["form_user_input_fields"][0]["entry_id"] == "200"
    saved = json.loads((package / "form-answers.json").read_text(encoding="utf-8"))
    assert len(saved["answers"]) == 1 and saved["deferred_answers"] == [deferred]


def test_cached_source_deferral_is_reclassified_without_changing_model_provenance(tmp_path):
    package, generation, result, form = form_candidate(tmp_path)
    deferred = answer(form, needs_user=True, review_note="缺少阅读文章和听力音频，无法作答。")
    deferred["values"] = []
    result["form_answers"] = [answer(form, "200", ["UNIT"]), deferred]
    drafting.accept_model_result(package, generation, result, json.dumps(result))
    path = package / "codex-generation.json"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    source_gap = legacy["missing_form_fields"][0]
    legacy.update(
        form_mapping_complete=True, missing_form_fields=[], form_user_input_fields=[source_gap]
    )
    atomic_json(path, legacy)
    original = {
        name: (package / name).read_bytes() for name in ("codex-result.json", "form-answers.json")
    }
    receipt = drafting.reusable_review(package)
    assert receipt["generation"]["form_mapping_complete"] is False
    assert not receipt["generation"]["form_user_input_fields"]
    assert receipt["generation"]["missing_form_fields"][0]["gap_reason"] == "missing_source"
    assert {name: (package / name).read_bytes() for name in original} == original
    assert receipt["generation"]["result_sha256"] == legacy["result_sha256"]


@pytest.mark.parametrize("aggregate", [False, True])
def test_loading_legacy_job_migrates_browser_coverage_without_replacing_answers(
    tmp_path, aggregate
):
    settings = unit_settings(tmp_path)
    jobs = FrontendJobs(settings)
    identifier = "3" * 32
    fields = {
        "remaining_answer_fields": [],
        "user_input_fields": [
            {
                "entry_id": "200",
                "title": "氏名",
                "required": True,
                "review_note": "本人の氏名を確認",
            },
            {
                "entry_id": "202",
                "title": "8. The woman is disappointed in the accommodations.",
                "required": True,
                "review_note": "第4页第8题：缺少女子对住宿的评价，无法判断是否失望。",
            },
        ],
        "unsupported_fields": [{"entry_id": "203", "title": "添付", "required": True}],
    }
    original_url = "https://docs.google.com/forms/d/e/offline/viewform"
    prefilled_url = original_url + "?entry.201=retained"
    receipt = {
        "form_mapping_complete": True,
        "answers_sha256": "existing-answers-hash",
        "forms": [{"url": prefilled_url, "original_url": original_url, "prefilled_fields": 7}],
    }
    (receipt if aggregate else receipt["forms"][0]).update(fields)
    path = jobs.root / "jobs" / (identifier + ".json")
    atomic_json(
        path,
        {
            "id": identifier,
            "kind": "review",
            "status": "completed_with_issues",
            "created_at": "offline",
            "items": [
                {
                    "course_id": "1",
                    "assignment_id": "2",
                    "status": "needs_form",
                    "form_fill": receipt,
                }
            ],
        },
    )
    restored = FrontendJobs(settings).job(identifier)
    item = restored["items"][0]
    fill = item["form_fill"]
    browser_fields = fill if aggregate else fill["forms"][0]
    assert [field["entry_id"] for field in browser_fields["user_input_fields"]] == ["200"]
    assert [field["entry_id"] for field in browser_fields["remaining_answer_fields"]] == ["202"]
    assert browser_fields["remaining_answer_fields"][0]["gap_reason"] == "missing_source"
    assert browser_fields["unsupported_fields"] == fields["unsupported_fields"]
    assert fill["answers_sha256"] == "existing-answers-hash"
    assert fill["forms"][0]["url"] == prefilled_url and fill["forms"][0]["prefilled_fields"] == 7
    assert fill["form_mapping_complete"] is False and item["form_requires_regeneration"]
    persisted = json.loads(path.read_text(encoding="utf-8"))["items"][0]["form_fill"]
    assert persisted == fill
    assert FrontendJobs(settings).job(identifier)["items"][0]["form_fill"] == fill


@pytest.mark.parametrize("deferred", [False, True])
def test_verified_legacy_reviews_recompute_field_coverage_without_rewriting_model_output(
    tmp_path, deferred
):
    package, generation, result, form = form_candidate(tmp_path)
    result["form_answers"] = [answer(form, "200", ["UNIT"])]
    if deferred:
        unanswered = answer(form, needs_user=True, review_note="本人の事実が必要")
        unanswered["values"] = []
        result["form_answers"].append(unanswered)
    drafting.accept_model_result(package, generation, result, json.dumps(result))
    generation_path = package / "codex-generation.json"
    legacy = json.loads(generation_path.read_text(encoding="utf-8"))
    for key in ("form_mapping_complete", "missing_form_fields", "form_user_input_fields"):
        legacy.pop(key)
    atomic_json(generation_path, legacy)
    before = {
        name: (package / name).read_bytes() for name in ("codex-result.json", "form-answers.json")
    }
    reused = drafting.reusable_review(package)
    assert reused["generation"]["form_mapping_complete"] is deferred
    assert reused["generation"]["form_answers_sha256"] == legacy["form_answers_sha256"]
    assert reused["generation"]["result_sha256"] == legacy["result_sha256"]
    assert reused["generation"]["model"] == legacy["model"]
    assert reused["generation"]["form_coverage_revalidated_at"]
    missing = reused["generation"]["missing_form_fields"]
    pending = reused["generation"]["form_user_input_fields"]
    assert ([x["entry_id"] for x in missing] == ["202"]) is not deferred
    assert ([x["entry_id"] for x in pending] == ["202"]) is deferred
    assert {name: (package / name).read_bytes() for name in before} == before
    persisted = json.loads(generation_path.read_text(encoding="utf-8"))
    assert persisted["form_mapping_complete"] is deferred


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_required_form_mapping_triggers_one_repair_and_keeps_useful_partial_candidate(
    monkeypatch, repair_succeeds
):
    calls = []

    def generate(*args, **options):
        calls.append(options)
        return {
            "draft_sha256": "actual-candidate",
            "generation": {"form_mapping_complete": repair_succeeds and len(calls) > 1},
        }

    monkeypatch.setattr(drafting, "generate_review", generate)
    receipt = drafting.generate_answer("offline", ai_confirmed=True, model="chosen-model")
    assert receipt["draft_sha256"] and len(calls) == 2
    assert receipt["generation"]["form_mapping_complete"] is repair_succeeds
    assert all(x["model"] == "chosen-model" for x in calls)


def test_retry_does_not_reuse_the_same_incomplete_repair_forever(tmp_path, monkeypatch):
    package, _, result, form = form_candidate(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, result["review"])
    result["form_answers"] = [answer(form, "200", ["UNIT"])]
    atomic_json(tmp_path / "fixture-result.json", result)
    first = drafting.generate_answer(package, ai_confirmed=True, model="unit-model", effort="high")
    assert first["generation"]["form_mapping_complete"] is False
    for generation_path in package.parent.glob("*/codex-generation.json"):
        legacy = json.loads(generation_path.read_text(encoding="utf-8"))
        for key in ("form_mapping_complete", "missing_form_fields", "form_user_input_fields"):
            legacy.pop(key, None)
        atomic_json(generation_path, legacy)
    second = drafting.generate_answer(package, ai_confirmed=True, model="unit-model", effort="high")
    assert second["draft_sha256"] and second["generation"]["form_mapping_complete"] is False
    requests = [
        json.loads(line)
        for line in (tmp_path / "requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len([r for r in requests if r.get("method") == "turn/start"]) == 3


def test_prefill_receipt_separates_missing_work_optional_blanks_and_unknown_identity(
    tmp_path, monkeypatch
):
    package, settings, _, _ = fill_fixture(tmp_path, monkeypatch)
    receipt = form_fill.fill_response_forms(settings, package, opener=lambda *a, **k: True)
    plan = receipt["forms"][0]
    assert not plan["remaining_answer_fields"]
    assert plan["user_input_fields"][0]["entry_id"] == "200"
    assert plan["optional_empty_fields"][0]["entry_id"] == "204"
    assert {x["entry_id"] for x in plan["remaining_fields"]} == {"200", "204"}
    assert receipt["form_mapping_complete"]
    old = copy.deepcopy(receipt)
    old.pop("form_mapping_complete")
    old["forms"][0].pop("remaining_answer_fields")
    atomic_json(package / "form-fill.json", old)
    reused = form_fill.fill_response_forms(
        settings, package, opener=lambda *a, **k: pytest.fail("Do not reopen reviewed forms")
    )
    assert reused["reused"] and reused["form_mapping_complete"]
    assert "remaining_answer_fields" in reused["forms"][0]


def test_user_answer_resolves_missing_native_mapping_without_regeneration(tmp_path, monkeypatch):
    package, settings, form, generation = fill_fixture(tmp_path, monkeypatch)
    form["questions"][0]["title"] = "First required academic question"
    form["context_sha256"] = form_fill.context_digest(form)
    requirements_path = package / "requirements.json"
    requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
    requirements["sources"] = [
        source for source in requirements["sources"] if source.get("source_kind") != "form"
    ] + form_chunks(form)
    atomic_json(requirements_path, requirements)
    atomic_json(package / "form-answers.json", {"answers": [answer(form)]})
    generation["form_answers_sha256"] = sha256_file(package / "form-answers.json")
    first = form_fill.fill_response_forms(settings, package, opener=lambda *a, **k: True)
    assert not first["form_mapping_complete"]
    assert first["forms"][0]["remaining_answer_fields"][0]["entry_id"] == "200"
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    native = answer(form, "200", ["User's own answer"])
    native = {k: v for k, v in native.items() if k not in {"needs_user", "review_note"}}
    save_user_answers(tmp_path, manifest, [form], StudentProfile(), [native])
    resolved = form_fill.fill_response_forms(settings, package, opener=lambda *a, **k: True)
    assert resolved["form_mapping_complete"]
    assert not resolved["forms"][0]["remaining_answer_fields"]
    assert resolved["forms"][0]["prefilled_fields"] == 2
