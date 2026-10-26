"""Offline native-Doc boundary tests, never represented as OAuth/write verification."""

from copy import deepcopy
from datetime import UTC
from types import SimpleNamespace

import pytest

from classroomautowork.document_fill import (
    digest,
    fill_personal_document,
    inspect_form,
    legacy_answers,
    missing_answer_fields,
    plan_fill,
    required_answer_fields,
    verify_fill,
)
from classroomautowork.errors import PermissionDenied, WorkflowError
from classroomautowork.student import StudentProfile


def native_document():
    counter = 1

    def paragraph(text):
        nonlocal counter
        index = counter
        counter += len(text) + 1
        return {
            "startIndex": index,
            "endIndex": counter,
            "paragraph": {
                "elements": [
                    {
                        "startIndex": index,
                        "endIndex": counter,
                        "textRun": {
                            "content": text + "\n",
                            "textStyle": {"fontSize": {"magnitude": 10, "unit": "PT"}},
                        },
                    }
                ],
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            },
        }

    intro = paragraph("氏名：")
    rows = []
    for values in [
        ("〇×", "ちがいについての状況", "判断の理由"),
        ("", "[1]例示された状況", ""),
        ("", "[2]別の状況", ""),
    ]:
        rows.append(
            {
                "tableCells": [
                    {
                        "content": [paragraph(v)],
                        "tableCellStyle": {"backgroundColor": {"color": {"rgbColor": {"red": 1}}}},
                    }
                    for v in values
                ]
            }
        )
    body = [
        intro,
        {"table": {"tableRows": rows, "columns": 3, "rows": 3}},
        paragraph("■まとめ"),
        paragraph(""),
    ]
    return {
        "documentId": "offline-doc",
        "revisionId": "offline-revision",
        "tabs": [
            {
                "tabProperties": {"tabId": "t.0", "title": "課題", "index": 0},
                "documentTab": {"body": {"content": body}},
            }
        ],
    }


def answers(document):
    return [
        {
            "field_id": f["id"],
            "context_sha256": f["context_sha256"],
            "text": "〇" if f["label"] == "〇×" else "授業資料から考えた理由。",
        }
        for f in inspect_form(document)["fields"]
    ]


def apply_insertions(document, plan):
    updated = deepcopy(document)

    def paragraphs(value):
        if isinstance(value, dict):
            if "paragraph" in value:
                yield value
            for item in value.values():
                yield from paragraphs(item)
        elif isinstance(value, list):
            for item in value:
                yield from paragraphs(item)

    by_index = {p["startIndex"]: p for p in paragraphs(updated)}
    for req in plan["requests"]:
        text = req["insertText"]
        by_index[text["location"]["index"]]["paragraph"]["elements"][0]["textRun"]["content"] = (
            text["text"] + "\n"
        )
    updated["revisionId"] = "offline-after"
    return updated


def inline_document(prefix=()):
    doc = native_document()
    sample = doc["tabs"][0]["documentTab"]["body"]["content"][0]
    content, offset = [], 1
    for text in [
        *prefix,
        "課題①　希望する体験を選ぶ",
        "□音楽🎵体験　(　　　)",
        "□工房見学　(　　　)",
        "課題②　イメージ",
        "キーワード1",
        "キーワード2",
        "課題③　考え",
        "自分の考えを書いてください。100字程度。",
        "",
    ]:
        element = deepcopy(sample)
        length = len((text + "\n").encode("utf-16-le")) // 2
        element.update(startIndex=offset, endIndex=offset + length)
        run = element["paragraph"]["elements"][0]
        run.update(startIndex=offset, endIndex=offset + length)
        run["textRun"]["content"] = text + "\n"
        content.append(element)
        offset += length
    doc["tabs"][0]["documentTab"]["body"]["content"] = content
    return doc


def communication_document():
    """Match the actual spaced question/option/reason worksheet structure."""
    document = inline_document()
    sample = document["tabs"][0]["documentTab"]["body"]["content"][0]
    lines = [
        "氏名：",
        "■設問①　コミュニケーションのイメージ",
        "キーワード1",
        "キーワード2",
        "■設問②　コミュニケーションは何人で行うのか。",
        "コミュニケーションとは最低何人で行うもの？その理由は？",
        "",
        "",
        "■設問③　共通点",
        "コミュニケーションの特徴を３つ挙げてください。",
        "",
        "■設問④　ケーススタディ",
        "同じクラスのＡさんとＢさんが大喧嘩をしました。翌日二人は、お互いを無視して何も"
        "言葉を交わさずに通り過ぎました。さて、この二人の間にコミュニケーションはあったでしょうか？",
        "",
        "①あった　　　②なかった",
        "",
        "その理由は？",
        "",
    ]
    content, offset = [], 1
    for text in lines:
        element = deepcopy(sample)
        length = len((text + "\n").encode("utf-16-le")) // 2
        element.update(startIndex=offset, endIndex=offset + length)
        run = element["paragraph"]["elements"][0]
        run.update(startIndex=offset, endIndex=offset + length)
        run["textRun"]["content"] = text + "\n"
        content.append(element)
        offset += length
    document["tabs"][0]["documentTab"]["body"]["content"] = content
    return document


def apply_inline_edits(document, plan):
    """Exercise Google UTF-16 offsets, including shifts after each request."""
    from classroomautowork.document_slots import utf16_length

    updated = deepcopy(document)
    body = updated["tabs"][0]["documentTab"]["body"]["content"]
    for request in plan["requests"]:
        if "insertText" in request:
            start = end = request["insertText"]["location"]["index"]
            replacement = request["insertText"]["text"]
        else:
            span = request["deleteContentRange"]["range"]
            start, end, replacement = span["startIndex"], span["endIndex"], ""
        paragraph = next(p for p in body if p["startIndex"] <= start < p["endIndex"])
        run = paragraph["paragraph"]["elements"][0]
        text = run["textRun"]["content"].encode("utf-16-le")
        a, b = start - paragraph["startIndex"], end - paragraph["startIndex"]
        run["textRun"]["content"] = (
            text[: 2 * a] + replacement.encode("utf-16-le") + text[2 * b :]
        ).decode("utf-16-le")
        offset = 1
        for p in body:
            r = p["paragraph"]["elements"][0]
            length = utf16_length(r["textRun"]["content"])
            p.update(startIndex=offset, endIndex=offset + length)
            r.update(startIndex=offset, endIndex=offset + length)
            offset += length
    updated["revisionId"] = "after-inline-write"
    return updated


def labeled_list_document():
    """Synthetic worksheet with native list metadata, never private course text."""
    document = native_document()
    sample = document["tabs"][0]["documentTab"]["body"]["content"][0]
    lines = [
        ("学籍番号：", None),
        ("氏名：", None),
        ("■設問①　観察🎵", None),
        ("観察の結果について理由を説明してください。", None),
        ("判断した理由：", None),
        ("", None),
        ("■設問②　色", None),
        ("候補から番号を２つ選び、理由を書いてください。", None),
        *[(f"色の候補 {i}", "colors") for i in range(1, 5)],
        ("", None),
        ("選択した番号(2つ)：", None),
        ("選んだ理由：", None),
        ("", None),
        ("■設問③　形", None),
        ("好きな形を選びます。", None),
        *[(f"形の候補 {i}", "shapes") for i in range(1, 13)],
        ("選んだ番号(複数回答可) ：", None),
        ("", None),
        ("■設問④　音", None),
        ("候補から番号を１つ選び、理由を書いてください。", None),
        *[(f"音🎵の候補 {i}", "sounds") for i in range(1, 5)],
        ("　選んだ番号(1つ)：", None),
        ("　選んだ理由：", None),
        ("", None),
    ]
    content, offset = [], 1
    for text, list_id in lines:
        element = deepcopy(sample)
        length = len((text + "\n").encode("utf-16-le")) // 2
        element.update(startIndex=offset, endIndex=offset + length)
        run = element["paragraph"]["elements"][0]
        run.update(startIndex=offset, endIndex=offset + length)
        run["textRun"]["content"] = text + "\n"
        if list_id:
            element["paragraph"]["bullet"] = {"listId": list_id, "nestingLevel": 0}
        content.append(element)
        offset += length
    document["tabs"][0]["documentTab"]["body"]["content"] = content
    return document


def test_labeled_answers_after_native_numbered_lists_fill_all_six_and_preserve_template():
    before = labeled_list_document()
    form = inspect_form(before)
    fields = form["fields"]
    assert len(fields) == len(required_answer_fields(form)) == 6
    assert form["unrecognized_questions"] == []
    assert [field["kind"] for field in fields] == [
        "labeled_answer",
        "number_selection",
        "labeled_answer",
        "number_selection",
        "number_selection",
        "labeled_answer",
    ]
    assert all(not field["current"] and "/labeled:" in field["id"] for field in fields)
    repeated = [field for field in fields if field["label"] == "選んだ理由："]
    assert len(repeated) == 2
    assert repeated[0]["id"] != repeated[1]["id"]
    assert repeated[0]["section_id"] != repeated[1]["section_id"]
    body = before["tabs"][0]["documentTab"]["body"]["content"]
    for field in fields:
        paragraph = next(p for p in body if p["startIndex"] == field["paragraph_index"])
        text = paragraph["paragraph"]["elements"][0]["textRun"]["content"]
        assert "bullet" not in paragraph["paragraph"]
        assert field["index"] == paragraph["startIndex"] + len(text[:-1].encode("utf-16-le")) // 2
        assert field["index"] == field["end_index"]
    supplied = [
        {
            "field_id": field["id"],
            "context_sha256": field["context_sha256"],
            "text": "1、2" if field["kind"] == "number_selection" else "観察から考えた理由🎵。",
        }
        for field in fields
    ]
    profile = StudentProfile(student_id="12345", name="Sample Student")
    plan = plan_fill(before, supplied, student_profile=profile)
    after = apply_inline_edits(before, plan)
    receipt = verify_fill(before, after, plan)
    assert receipt["status"] == "filled_verified"
    assert receipt["inserted_fields"] == receipt["verified_fields"] == 8
    assert receipt["verified_by_kind"] == {
        "identity": 2,
        "number_selection": 3,
        "labeled_answer": 3,
    }
    assert inspect_form(after)["question_sha256"] == form["question_sha256"]
    assert plan_fill(after, supplied, student_profile=profile)["requests"] == []
    after_body = after["tabs"][0]["documentTab"]["body"]["content"]
    for original, actual in zip(body, after_body, strict=True):
        if "bullet" in original["paragraph"]:
            assert original["paragraph"]["bullet"] == actual["paragraph"]["bullet"]
            assert (
                original["paragraph"]["elements"][0]["textRun"]
                == (actual["paragraph"]["elements"][0]["textRun"])
            )


def test_labeled_answers_keep_user_contents_and_reject_changed_prompts_or_label_styles():
    before = labeled_list_document()
    field = inspect_form(before)["fields"][0]
    answer = {
        "field_id": field["id"],
        "context_sha256": field["context_sha256"],
        "text": "本人の回答。",
    }
    plan = plan_fill(before, [answer])
    after = apply_inline_edits(before, plan)
    with pytest.raises(WorkflowError, match="保留你已填写"):
        plan_fill(after, [{**answer, "text": "別の回答。"}])
    changed = deepcopy(before)
    changed["tabs"][0]["documentTab"]["body"]["content"][3]["paragraph"]["elements"][0]["textRun"][
        "content"
    ] = "新しい観察の理由を説明してください。\n"
    with pytest.raises(WorkflowError, match="原题"):
        plan_fill(changed, [answer])
    changed = deepcopy(after)
    changed["tabs"][0]["documentTab"]["body"]["content"][4]["paragraph"]["elements"][0]["textRun"][
        "textStyle"
    ]["bold"] = True
    with pytest.raises(WorkflowError, match="非答案"):
        verify_fill(before, changed, plan)


@pytest.mark.parametrize("prompt", ["観察した理由を説明してください。", "観察した理由は？"])
def test_native_numbered_questions_keep_existing_answers_and_options_protected(prompt):
    before = native_document()
    sample = before["tabs"][0]["documentTab"]["body"]["content"][0]
    lines = [
        (prompt, "questions"),
        ("", None),
        ("もう一つの理由を説明してください。", "questions"),
        ("本人がすでに書いた理由。", None),
        ("候補から番号を選んでください。", None),
        ("色の候補 1", "colors"),
        ("色の候補 2", "colors"),
        ("", None),
        ("選んだ番号：", None),
    ]
    content, offset = [], 1
    for text, list_id in lines:
        element = deepcopy(sample)
        length = len((text + "\n").encode("utf-16-le")) // 2
        element.update(startIndex=offset, endIndex=offset + length)
        run = element["paragraph"]["elements"][0]
        run.update(startIndex=offset, endIndex=offset + length)
        run["textRun"]["content"] = text + "\n"
        if list_id:
            element["paragraph"]["bullet"] = {"listId": list_id, "nestingLevel": 0}
        content.append(element)
        offset += length
    before["tabs"][0]["documentTab"]["body"]["content"] = content
    form = inspect_form(before)
    assert [field["label"] for field in form["fields"]] == [
        prompt,
        "もう一つの理由を説明してください。",
        "選んだ番号：",
    ]
    assert form["unrecognized_questions"] == []
    supplied = [
        {
            "field_id": field["id"],
            "context_sha256": field["context_sha256"],
            "text": "1" if field.get("kind") == "number_selection" else "観察から考えた理由。",
        }
        for field in form["fields"]
        if not field["current"]
    ]
    plan = plan_fill(before, supplied)
    after = apply_inline_edits(before, plan)
    receipt = verify_fill(before, after, plan)
    assert receipt["status"] == "filled_verified"
    assert receipt["inserted_fields"] == 2
    assert inspect_form(after)["fields"][1]["current"] == "本人がすでに書いた理由。"
    after_body = after["tabs"][0]["documentTab"]["body"]["content"]
    for original, actual in zip(content, after_body, strict=True):
        if "bullet" in original["paragraph"]:
            assert original["paragraph"]["bullet"] == actual["paragraph"]["bullet"]
            assert (
                original["paragraph"]["elements"][0]["textRun"]
                == (actual["paragraph"]["elements"][0]["textRun"])
            )


def test_saved_identity_fills_only_blank_labels_and_retry_is_idempotent():
    before = inline_document(["学籍番号：", "氏　　名："])
    fields = inspect_form(before)["fields"]
    assert not any(f.get("identity_key") for f in fields)
    expected = [
        {
            "field_id": f["id"],
            "context_sha256": f["context_sha256"],
            "text": "☑" if f.get("kind") == "choice" else "回答。",
        }
        for f in fields
        if "工房" not in f["label"]
    ]
    # Reproduce the reported case: answers already exist, but identity is still blank.
    answered = apply_inline_edits(before, plan_fill(before, expected))
    profile = StudentProfile(student_id="TEST019", name="TEST STUDENT")
    plan = plan_fill(answered, expected, student_profile=profile)
    assert len(plan["requests"]) == 2
    after = apply_inline_edits(answered, plan)
    receipt = verify_fill(answered, after, plan)
    assert receipt["inserted_fields"] == 2
    assert receipt["verified_fields"] == 7
    assert receipt["verified_by_kind"] == {
        "choice": 1,
        "reason": 1,
        "keyword": 2,
        "paragraph": 1,
        "identity": 2,
    }
    assert receipt["identity_warnings"] == []
    identities = [
        f["current"]
        for f in inspect_form(after, include_identity=True)["fields"]
        if f.get("identity_key")
    ]
    assert identities == ["TEST019", "TEST STUDENT"]
    assert plan_fill(after, expected, student_profile=profile)["requests"] == []
    assert inspect_form(answered)["question_sha256"] == inspect_form(after)["question_sha256"]


def test_missing_or_conflicting_identity_is_preserved_and_reported():
    before = inline_document(["学籍番号：", "氏　　名： USER EDIT"])
    expected = [answers(before)[-1]]
    profile = StudentProfile(student_id="", name="SAVED NAME")
    plan = plan_fill(before, expected, student_profile=profile)
    after = apply_inline_edits(before, plan)
    receipt = verify_fill(before, after, plan)
    assert len(receipt["identity_warnings"]) == 2
    assert "尚无已保存" in receipt["identity_warnings"][0]
    assert "已保留" in receipt["identity_warnings"][1]
    assert "identity" not in receipt["verified_by_kind"]
    body = after["tabs"][0]["documentTab"]["body"]["content"]
    assert (
        body[0]["paragraph"] == before["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]
    )
    body[1]["paragraph"]["elements"][0]["textRun"]["content"] = "氏　　名： CHANGED\n"
    with pytest.raises(WorkflowError, match="非答案"):
        verify_fill(before, after, plan)


def test_model_cannot_supply_identity_even_when_it_knows_the_field_id():
    before = inline_document(["学籍番号：", "氏名："])
    field = inspect_form(before, include_identity=True)["fields"][0]
    expected = [
        {"field_id": field["id"], "context_sha256": field["context_sha256"], "text": "FAKE"}
    ]
    with pytest.raises(WorkflowError, match="身份栏只能"):
        plan_fill(before, expected, student_profile=StudentProfile(student_id="TEST019"))
    with pytest.raises(WorkflowError, match="映射"):
        plan_fill(before, expected)


def test_inline_choices_reasons_keywords_and_written_answer_roundtrip():
    before = inline_document()
    fields = inspect_form(before)["fields"]
    assert len(fields) == 7
    chosen = [f for f in fields if "工房" not in f["label"]]
    expected = [
        {
            "field_id": f["id"],
            "context_sha256": f["context_sha256"],
            "text": "☑" if f.get("kind") == "choice" else "候補の回答🎵。",
        }
        for f in chosen
    ]
    plan = plan_fill(before, expected)
    after = apply_inline_edits(before, plan)
    receipt = verify_fill(before, after, plan)
    assert receipt["verified_fields"] == receipt["inserted_fields"] == 5
    assert plan_fill(after, expected)["requests"] == []
    with pytest.raises(WorkflowError, match="保留你已填写"):
        plan_fill(after, [{**expected[1], "text": "別の回答"}])
    with pytest.raises(WorkflowError, match="选中标记"):
        plan_fill(before, [{**expected[0], "text": " arbitrary "}])
    changed = deepcopy(after)
    run = changed["tabs"][0]["documentTab"]["body"]["content"][2]["paragraph"]["elements"][0]
    run["textRun"]["content"] = run["textRun"]["content"].replace("□", "☑")
    with pytest.raises(WorkflowError, match="非答案"):
        verify_fill(before, changed, plan)
    changed = deepcopy(before)
    run = changed["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0]
    run["textRun"]["content"] = "課題①　別の設問\n"
    with pytest.raises(WorkflowError, match="原题"):
        plan_fill(changed, expected)


def test_inline_readback_allows_split_runs_but_not_changed_label_style():
    before = inline_document()
    field = inspect_form(before)["fields"][1]
    expected = [
        {"field_id": field["id"], "context_sha256": field["context_sha256"], "text": "理由"}
    ]
    plan = plan_fill(before, expected)
    after = apply_inline_edits(before, plan)
    p = after["tabs"][0]["documentTab"]["body"]["content"][1]["paragraph"]
    first = p["elements"][0]
    second = deepcopy(first)
    content = first["textRun"]["content"]
    first["textRun"]["content"], second["textRun"]["content"] = content[:4], content[4:]
    p["elements"] = [first, second]
    verify_fill(before, after, plan)
    first["textRun"]["textStyle"]["bold"] = True
    with pytest.raises(WorkflowError, match="非答案"):
        verify_fill(before, after, plan)


def test_instruction_followed_by_a_reason_header_is_not_an_answer_paragraph():
    doc = inline_document()
    body = doc["tabs"][0]["documentTab"]["body"]["content"]
    body[-1]["paragraph"]["elements"][0]["textRun"]["content"] = "   〔理由〕\n"
    assert all("/after:" not in f["id"] for f in inspect_form(doc)["fields"])


def test_targeted_reverse_insert_preserves_questions_styles_and_second_tab():
    before = native_document()
    before["tabs"].append(
        {
            "tabProperties": {"tabId": "t.1", "title": "変更しない", "index": 1},
            "documentTab": {"body": {"content": []}},
        }
    )
    plan = plan_fill(before, answers(before))
    indices = [r["insertText"]["location"]["index"] for r in plan["requests"]]
    assert indices == sorted(indices, reverse=True)
    assert plan["write_control"] == {"requiredRevisionId": "offline-revision"}
    after = apply_insertions(before, plan)
    receipt = verify_fill(before, after, plan)
    assert receipt["status"] == "filled_verified" and receipt["verified_fields"] == 5
    assert after["tabs"][1] == before["tabs"][1]
    assert plan_fill(after, answers(before))["requests"] == []
    after["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0]["textRun"][
        "content"
    ] = "modified name\n"
    with pytest.raises(WorkflowError, match="非答案"):
        verify_fill(before, after, plan)


def test_existing_user_answer_is_never_overwritten_and_changed_question_is_rejected():
    before = native_document()
    expected = answers(before)
    changed = deepcopy(before)
    cell = changed["tabs"][0]["documentTab"]["body"]["content"][1]["table"]["tableRows"][1][
        "tableCells"
    ][0]
    cell["content"][0]["paragraph"]["elements"][0]["textRun"]["content"] = "本人の回答\n"
    with pytest.raises(WorkflowError, match="保留你已填写"):
        plan_fill(changed, expected)
    changed = deepcopy(before)
    cells = changed["tabs"][0]["documentTab"]["body"]["content"][1]["table"]["tableRows"][1][
        "tableCells"
    ]
    cells[1]["content"][0]["paragraph"]["elements"][0]["textRun"]["content"] = "[1]題目が変わった\n"
    with pytest.raises(WorkflowError, match="原题"):
        plan_fill(changed, expected)


def test_unknown_mapping_private_use_controls_and_internal_citations_fail_closed():
    before = native_document()
    expected = answers(before)
    with pytest.raises(WorkflowError, match="映射"):
        plan_fill(before, [{**expected[0], "field_id": "teacher-template/anywhere"}])
    with pytest.raises(WorkflowError, match="证据"):
        plan_fill(before, [{**expected[0], "text": "回答 [E:internal]"}])
    before["tabs"][0]["documentTab"]["body"]["content"][1]["table"]["tableRows"][1]["tableCells"][
        0
    ]["content"][0]["paragraph"]["elements"].append({"textRun": {"content": "\ue907"}})
    with pytest.raises(WorkflowError, match="映射"):
        plan_fill(before, expected)


class PersonalReader:
    state = "CREATED"
    owned = True

    def personal_document_metadata(self, document_id):
        return {
            "id": document_id,
            "mimeType": "application/vnd.google-apps.document",
            "ownedByMe": self.owned,
            "capabilities": {"canEdit": True},
        }

    def own_submissions(self, course_id):
        assert course_id == "1"
        return [
            {
                "courseWorkId": "2",
                "state": self.state,
                "assignmentSubmission": {"attachments": [{"driveFile": {"id": "offline-doc"}}]},
            }
        ]


class DocsFixture:
    def __init__(self, *, timeout_after_commit=False):
        self.document = native_document()
        self.writes = 0
        self.timeout_after_commit = timeout_after_commit

    def get(self, document_id):
        assert document_id == "offline-doc"
        return deepcopy(self.document)

    def batch(self, plan):
        self.writes += 1
        self.document = apply_insertions(self.document, plan)
        if self.timeout_after_commit:
            raise TimeoutError("offline unknown response")


def test_personal_allowlist_submission_state_unknown_outcome_and_idempotent_retry(tmp_path):
    reader, docs = PersonalReader(), DocsFixture(timeout_after_commit=True)
    expected = answers(docs.document)
    kwargs = {
        "course_id": "1",
        "assignment_id": "2",
        "document_id": "offline-doc",
        "answers": expected,
        "record_dir": tmp_path / "records",
    }
    with pytest.raises(PermissionDenied, match="本人文档"):
        fill_personal_document(reader, docs, **{**kwargs, "document_id": "teacher-template"})
    assert docs.writes == 0
    reader.owned = False
    with pytest.raises(PermissionDenied, match="共享的教师原件"):
        fill_personal_document(reader, docs, **kwargs)
    reader.owned = True
    assert docs.writes == 0
    result = fill_personal_document(reader, docs, **kwargs)
    assert result["status"] == "filled_verified" and docs.writes == 1
    assert (
        fill_personal_document(reader, docs, **kwargs)["inserted_fields"] == 0 and docs.writes == 1
    )
    reader.state = "TURNED_IN"
    with pytest.raises(WorkflowError, match="待完成状态"):
        fill_personal_document(reader, docs, **kwargs)
    assert docs.writes == 1


def test_docs_oauth_is_separate_and_never_broadens_classroom_reader(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timedelta

    from google.oauth2.credentials import Credentials

    from classroomautowork import auth
    from classroomautowork.config import Settings

    client = tmp_path / "client.json"
    client.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "offline-id",
                    "client_secret": "offline-fixture",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        )
    )
    settings = Settings("offline@example.invalid", client, tmp_path).validated()

    def offline_credentials(scopes):
        return Credentials(
            "offline-token",
            refresh_token="offline-refresh",
            client_id="offline-id",
            client_secret="offline-fixture",
            scopes=scopes,
            expiry=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
        ).to_json()

    stored = {
        auth.SERVICE: offline_credentials(auth.SCOPES),
        auth.DOCUMENT_SERVICE: offline_credentials(auth.DOCUMENT_SCOPES),
    }
    original_reader = stored[auth.SERVICE]
    monkeypatch.setattr(auth, "secure_keyring", lambda: None)
    monkeypatch.setattr(
        auth, "Request", lambda: pytest.fail("Offline OAuth fixture must never connect to Google")
    )
    monkeypatch.setattr(auth.keyring, "get_password", lambda service, _: stored.get(service))
    monkeypatch.setattr(
        auth.keyring, "set_password", lambda service, _, value: stored.update({service: value})
    )
    monkeypatch.setattr(auth, "verify_identity", lambda *_: {"offline_fixture": True})
    assert set(auth.credentials_for(settings, documents=True)[0].scopes) == set(
        auth.DOCUMENT_SCOPES
    )
    assert stored[auth.SERVICE] == original_reader
    assert set(auth.credentials_for(settings)[0].scopes) == set(auth.SCOPES)
    stored[auth.SERVICE] = stored[auth.DOCUMENT_SERVICE]
    with pytest.raises(PermissionDenied):
        auth.credentials_for(settings)


def test_concurrent_fill_returns_retryable_message_without_any_write(tmp_path):
    from filelock import FileLock

    reader, docs = PersonalReader(), DocsFixture()
    records = tmp_path / "records"
    records.mkdir()
    with FileLock(str(records / "fill.lock")), pytest.raises(WorkflowError, match="正在填入"):
        fill_personal_document(
            reader,
            docs,
            course_id="1",
            assignment_id="2",
            document_id="offline-doc",
            answers=answers(docs.document),
            record_dir=records,
        )
    assert docs.writes == 0


def test_previous_draft_migration_checks_actual_original_questions():
    document = native_document()
    draft = "| 1 | 〇 | 理由一 [E:offline] |\n| 2 | × | 理由二 [E:offline] |\n## まとめ\n\n要約。\n"
    original_text = "[1]例示された状況 [2]別の状況 ■まとめ"
    migrated = legacy_answers(document, draft, original_text=original_text)
    assert len(migrated) == 5 and all("[E:" not in a["text"] for a in migrated)
    with pytest.raises(WorkflowError, match="原题"):
        legacy_answers(
            document, draft, original_text=original_text.replace("例示された状況", "変更された状況")
        )


def test_interrogative_question_and_numbered_case_choice_and_reason_are_all_required():
    before = communication_document()
    form = inspect_form(before)
    assert len(form["fields"]) == 6
    assert len(required_answer_fields(form)) == 6
    assert form["unrecognized_questions"] == []
    q2, selection, reason = form["fields"][2], form["fields"][4], form["fields"][5]
    assert "最低何人" in q2["label"]
    assert selection["kind"] == "selection"
    assert selection["options"] == ["①あった", "②なかった"]
    assert "■設問④" in reason["context"] and "大喧嘩" in reason["context"]
    assert all("あったでしょうか？" != field["label"] for field in form["fields"])
    body = before["tabs"][0]["documentTab"]["body"]["content"]
    assert selection["index"] == body[15]["startIndex"]
    assert reason["index"] == body[17]["startIndex"]
    supplied = [
        {
            "field_id": field["id"],
            "context_sha256": field["context_sha256"],
            "text": "①あった" if field.get("kind") == "selection" else "授業に基づいた回答。",
        }
        for field in form["fields"]
    ]
    assert missing_answer_fields(form, supplied) == []
    missing = missing_answer_fields(form, supplied[:2] + supplied[3:4])
    assert [field["id"] for field in missing] == [q2["id"], selection["id"], reason["id"]]
    plan = plan_fill(before, supplied)
    after = apply_inline_edits(before, plan)
    receipt = verify_fill(before, after, plan)
    assert receipt["status"] == "filled_verified"
    assert receipt["remaining_required_fields"] == []
    assert inspect_form(after)["question_sha256"] == form["question_sha256"]
    assert plan_fill(after, supplied)["requests"] == []
    assert body[14] == before["tabs"][0]["documentTab"]["body"]["content"][14]
    # Prior cached mappings use these exact hashes; the newly recognized section
    # headings must not change already supported keyword or written-answer fields.
    assert form["fields"][0]["context_sha256"] == digest(["", "キーワード1<keyword>\n"])
    q3 = form["fields"][3]
    assert q3["id"] == f"t.0/after:{digest(q3['label'])[:16]}"
    assert q3["context_sha256"] == digest(q3["label"])
    with pytest.raises(WorkflowError, match="原题列出"):
        plan_fill(before, [{**supplied[4], "text": "選択以外の内容。"}])


def test_partial_write_receipt_does_not_claim_unanswered_questions_are_complete():
    before = communication_document()
    form = inspect_form(before)
    old_answers = [
        {
            "field_id": field["id"],
            "context_sha256": field["context_sha256"],
            "text": "既存のキーワードと第３問の回答。",
        }
        for field in form["fields"]
        if field.get("kind") == "keyword" or "３つ挙げ" in field["label"]
    ]
    plan = plan_fill(before, old_answers)
    after = apply_inline_edits(before, plan)
    receipt = verify_fill(before, after, plan)
    assert receipt["status"] == "partial"
    assert len(receipt["remaining_required_fields"]) == 3
    assert any("最低何人" in field["label"] for field in receipt["remaining_required_fields"])
    assert all(
        set(field) == {"id", "label", "context"} for field in receipt["remaining_required_fields"]
    )
    # Filling the newly discovered fields preserves the already supplied old mapping.
    assert plan_fill(after, old_answers)["requests"] == []


def test_repeated_reason_labels_are_unique_and_survive_readback():
    before = communication_document()
    body = before["tabs"][0]["documentTab"]["body"]["content"]
    extra = deepcopy(body[-4:])
    extra[0]["paragraph"]["elements"][0]["textRun"]["content"] = "■設問⑤　次の例\n"
    body.extend(extra)
    offset = 1
    for paragraph in body:
        run = paragraph["paragraph"]["elements"][0]
        length = len(run["textRun"]["content"].encode("utf-16-le")) // 2
        paragraph.update(startIndex=offset, endIndex=offset + length)
        run.update(startIndex=offset, endIndex=offset + length)
        offset += length
    form = inspect_form(before)
    reasons = [field for field in form["fields"] if field["label"] == "その理由は？"]
    assert len(reasons) == 2 and reasons[0]["id"] != reasons[1]["id"]
    assert reasons[0]["context_sha256"] != reasons[1]["context_sha256"]
    expected = [
        {
            "field_id": field["id"],
            "context_sha256": field["context_sha256"],
            "text": "何が伝わったか？",
        }
        for field in reasons
    ]
    plan = plan_fill(before, expected)
    after = apply_inline_edits(before, plan)
    verify_fill(before, after, plan)
    assert inspect_form(after)["question_sha256"] == form["question_sha256"]
    assert plan_fill(after, expected)["requests"] == []


def test_unknown_question_without_safe_answer_container_is_reported():
    before = communication_document()
    body = before["tabs"][0]["documentTab"]["body"]["content"]
    body.pop()  # The final reason has no answer paragraph at all.
    form = inspect_form(before)
    assert form["unrecognized_questions"][0]["label"] == "その理由は？"
    expected = [
        {
            "field_id": field["id"],
            "context_sha256": field["context_sha256"],
            "text": "①あった" if field.get("kind") == "selection" else "授業資料からの回答。",
        }
        for field in form["fields"]
    ]
    plan = plan_fill(before, expected)
    receipt = verify_fill(before, apply_inline_edits(before, plan), plan)
    assert receipt["status"] == "partial"
    assert receipt["remaining_required_fields"] == []
    assert receipt["unrecognized_questions"][0]["label"] == "その理由は？"


def test_required_fields_exclude_identity_and_unchecked_alternative_reasons():
    before = inline_document(["学籍番号："])
    form = inspect_form(before, include_identity=True)
    required = required_answer_fields(form)
    assert len(required) == 3
    assert all(field.get("kind") not in {"identity", "choice", "reason"} for field in required)
    choice = next(field for field in form["fields"] if field.get("kind") == "choice")
    supplied = [{"field_id": choice["id"], "context_sha256": choice["context_sha256"], "text": "☑"}]
    required = required_answer_fields(form, answers=supplied)
    assert len(required) == 4 and sum(field.get("kind") == "reason" for field in required) == 1
    with pytest.raises(WorkflowError, match="重复"):
        missing_answer_fields(form, supplied * 2)
    with pytest.raises(WorkflowError, match="空答案"):
        missing_answer_fields(form, [{**supplied[0], "text": "   "}])


def test_fill_review_aggregates_missing_questions_and_reports_partial(tmp_path, monkeypatch):
    import json

    from classroomautowork import auth, document_fill, drafting, google_read
    from classroomautowork.local import sha256_file

    package = tmp_path / "package"
    package.mkdir()
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "course_id": "1",
                "assignment_id": "2",
                "ai_confirmation": True,
                "policy": {"can_draft": True},
            }
        ),
        encoding="utf-8",
    )
    answer_path = package / "document-answers.json"
    answer_path.write_text(
        json.dumps({"answers": [{"document_id": "offline-doc"}]}), encoding="utf-8"
    )
    (package / "codex-generation.json").write_text(
        json.dumps({"document_answers_sha256": sha256_file(answer_path)}), encoding="utf-8"
    )
    reader = PersonalReader()
    reader.file_metadata = lambda _: {"mimeType": "application/vnd.google-apps.document"}
    monkeypatch.setattr(auth, "credentials_for", lambda *a, **kw: (None, None))
    monkeypatch.setattr(drafting, "reusable_review", lambda _: {"draft_sha256": "offline"})
    monkeypatch.setattr(google_read, "GoogleReader", lambda _: reader)
    monkeypatch.setattr(document_fill, "GoogleDocuments", lambda _: None)
    missing = {"id": "q2", "label": "最低何人？", "context": "設問②"}
    unknown = {"label": "その理由は？", "context": "設問④"}
    monkeypatch.setattr(
        document_fill,
        "fill_personal_document",
        lambda *a, **kw: {
            "status": "partial",
            "document_id": "offline-doc",
            "remaining_required_fields": [missing],
            "unrecognized_questions": [unknown],
        },
    )
    progress = []
    result = document_fill.fill_review(
        SimpleNamespace(data_dir=tmp_path), package, progress=progress.append
    )
    assert result["status"] == "partial"
    assert result["remaining_required_fields"] == [{**missing, "document_id": "offline-doc"}]
    assert result["unrecognized_questions"] == [{**unknown, "document_id": "offline-doc"}]
    assert progress[-1] == "已填入部分答案，仍有题目待补全"
    assert (
        json.loads((package / "document-fill.json").read_text(encoding="utf-8"))["status"]
        == "partial"
    )
