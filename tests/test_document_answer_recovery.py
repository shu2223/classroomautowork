"""Offline recovery fixtures use invented labels and answers, never course data."""

from copy import deepcopy

import pytest

from classroomautowork.document_answer_recovery import recover_labeled_answers


def field(field_id="a", section="■設問①　練習の見出し", label="説明：", **extra):
    return {
        "id": field_id,
        "tab_id": "tab",
        "section": section,
        "section_id": section,
        "kind": "labeled_answer",
        "label": label,
        "context": section + "\n" + label,
        "context_sha256": "hash-" + field_id,
        "current": "",
        **extra,
    }


def form(*fields):
    return {"document_id": "offline-doc", "fields": list(fields), "unrecognized_questions": []}


def mapped(target, text="既存の回答。", **extra):
    return {
        "field_id": target["id"],
        "context_sha256": target["context_sha256"],
        "text": text,
        **extra,
    }


def test_stale_mapping_recovers_all_labeled_answers_in_their_own_sections():
    target = form(
        field("a", label="説明："),
        field("b", section="■設問②　別の練習", label="選択番号（2つ）：", kind="number_selection"),
        field("c", section="■設問②　別の練習", label="説明："),
    )
    old = [{"field_id": "tab/after:stale", "context_sha256": "stale", "text": "旧回答"}]
    draft = "設問①\n説明：第一の説明。 [E:source-a]\n\n設問②\n選択番号(2つ)：1、3\n説明：第二の説明。 [E:source-b]"
    actual = recover_labeled_answers(target, draft, old)
    assert [answer["text"] for answer in actual] == ["第一の説明。", "1、3", "第二の説明。"]
    assert [answer["field_id"] for answer in actual] == ["a", "b", "c"]
    assert all(answer["document_id"] == "offline-doc" for answer in actual)
    assert old[0]["field_id"] == "tab/after:stale"


def test_usable_mapping_and_provenance_are_preserved_without_mutating_inputs():
    first, second = field(), field("b", label="別の説明：")
    target = form(first, second)
    original = mapped(first, model_note="preserved-source")
    before = deepcopy(original)
    actual = recover_labeled_answers(
        target, "設問①\n説明：異なる下書き。\n別の説明：補足。", [original]
    )
    assert actual == [before, {"document_id": "offline-doc", **mapped(second, "補足。")}]
    assert original == before
    assert actual[0] is not original


def test_changed_context_hash_is_replaced_only_from_exact_labeled_draft():
    target = field()
    actual = recover_labeled_answers(
        form(target), "設問①\n説明：確認済みの説明。", [mapped(target, context_sha256="old-hash")]
    )
    assert actual == [{"document_id": "offline-doc", **mapped(target, "確認済みの説明。")}]


def test_whitespace_and_full_width_parentheses_and_colon_are_normalized():
    target = field(label="選択番号 （2つ） ：", kind="number_selection")
    actual = recover_labeled_answers(form(target), "## 設問１\n選択番号(2つ): 1、2", [])
    assert actual[0]["text"] == "1、2"


@pytest.mark.parametrize(
    "draft",
    [
        "設問②\n説明：別の設問。",
        "課題①\n説明：別の種類。",
        "説明：設問がない。",
        "設問①\n異なる説明：ラベルが違う。",
        "設問①\n説明：一つ。\n説明：二つ。",
        "設問①\n説明：一つ。\n設問①\n補足：二つ。",
        "設問①\n説明：[E:only-evidence]",
        "設問①\n説明：回答。 [E:unfinished",
        "設問①\n```text\n説明：コード内。\n```",
        "設問①\n## 参考情報\n説明：参考情報。",
        "設問①\n- 説明：リストは未対応。",
    ],
)
def test_ambiguous_missing_or_nonliteral_draft_does_not_guess(draft):
    assert recover_labeled_answers(form(field()), draft, []) is None


def test_all_required_fields_need_mappings_even_when_the_document_has_content():
    target = form(field(), field("b", label="補足：", current="既に入力済み"))
    assert recover_labeled_answers(target, "設問①\n説明：一つ。", []) is None


def test_duplicate_labels_in_one_form_section_are_ambiguous():
    assert (
        recover_labeled_answers(form(field(), field("b")), "設問①\n説明：どちらか不明。", [])
        is None
    )


def test_same_numbered_section_repeated_in_form_is_ambiguous_even_with_distinct_labels():
    target = form(field(section_id="s1"), field("b", label="補足：", section_id="s2"))
    assert recover_labeled_answers(target, "設問①\n説明：一つ。\n補足：二つ。", []) is None


def test_unknown_layout_and_unrecognized_question_are_not_treated_as_complete():
    assert recover_labeled_answers(form(field(kind="paragraph")), "設問①\n説明：一つ。", []) is None
    target = form(field())
    target["unrecognized_questions"] = [{"label": "別の設問"}]
    assert recover_labeled_answers(target, "設問①\n説明：一つ。", []) is None


@pytest.mark.parametrize("label", ["氏　名：", "学籍番号：", "学号：", "Student ID:"])
def test_identity_cannot_be_recovered_into_an_ordinary_academic_field(label):
    assert (
        recover_labeled_answers(form(field(label=label)), "設問①\n" + label + "個人情報", [])
        is None
    )


def test_explicit_identity_stays_in_separate_trusted_profile_flow():
    target = form(field(), field("identity", label="氏名：", kind="identity", identity_key="name"))
    actual = recover_labeled_answers(target, "設問①\n説明：回答。\n氏名：個人情報", [])
    assert len(actual) == 1 and actual[0]["field_id"] == "a"


def test_unchanged_mapping_does_not_require_reinterpreting_a_draft():
    target = field(kind="paragraph")
    original = mapped(target)
    assert recover_labeled_answers(form(target), "元の書式の初稿", [original]) == [original]


def test_optional_checkbox_reason_is_required_only_for_selected_alternative():
    choice = field("option/choice", label="代替の選択", kind="choice")
    reason = field("option/reason", label="代替の理由", kind="reason")
    target = form(field(), choice, reason)
    draft = "設問①\n説明：回答。"
    assert len(recover_labeled_answers(target, draft, [])) == 1
    assert recover_labeled_answers(target, draft, [mapped(choice, "☑")]) is None


@pytest.mark.parametrize(
    "answers",
    [
        None,
        ["invalid"],
        [{}],
        [mapped(field()), mapped(field())],
        [mapped(field(), "")],
        [mapped(field(), "一行。\n別の行。")],
        [mapped(field(), "回答。 [E:id]")],
        [mapped(field(), document_id="different-document")],
    ],
)
def test_invalid_existing_mapping_is_not_silently_reinterpreted(answers):
    assert recover_labeled_answers(form(field()), "設問①\n説明：回答。", answers) is None
