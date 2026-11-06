"""Every prefillable Google Forms type: how the page data is read, which values are accepted and
the viewform parameters they become. Submitting is never part of it (see test_no_submit.py)."""

import json
from urllib.parse import parse_qsl, urlsplit

import pytest

from classroomautowork import completion, drafting, form_fill
from classroomautowork.errors import WorkflowError
from classroomautowork.form_read import OTHER_CHOICE, parse_form

URL = "https://docs.google.com/forms/d/e/unit-every-type-form/viewform"
OTHER = ["", None, None, None, 1]


def grid(entry, columns, label, *, several=0, required=1):
    """A grid row: its label at [3], the checkbox-grid flag at [11]."""
    return [entry, [[c] for c in columns], required, [label], *[None] * 7, [several]]


def page(items):
    body = [None] * 26
    body[0], body[1], body[8] = "Every type", items, "Unit every type form"
    return "<script>var FB_PUBLIC_LOAD_DATA_ = " + json.dumps([None, body]) + ";</script>"


ITEMS = [
    [1, "Name", None, 0, [[100, None, 1]]],
    [2, "Essay", None, 1, [[101, None, 0]]],
    [3, "Pick", None, 2, [[102, [["B"], ["A"], OTHER], 1]]],
    [4, "Menu", None, 3, [[103, [["x"], ["y"]], 1]]],
    [5, "Tick", None, 4, [[104, [["red"], ["blue"], OTHER], 0]]],
    [6, "Scale", None, 5, [[105, [["1"], ["2"], ["3"]], 1, ["low", "high"]]]],
    [
        7,
        "Grid",
        None,
        7,
        [grid(106, ["yes", "no"], "row one"), grid(107, ["yes", "no"], "row two")],
    ],
    [8, "Box grid", None, 7, [grid(108, ["a", "b", "c"], "first", several=1, required=0)]],
    [9, "Day", None, 9, [[109, None, 1, None, None, None, None, [0, 1]]]],
    [10, "Birthday", None, 9, [[110, None, 0, None, None, None, None, [0, 0]]]],
    [11, "Day and time", None, 9, [[111, None, 0, None, None, None, None, [1, 1]]]],
    [12, "Month day and time", None, 9, [[112, None, 0, None, None, None, None, [1, 0]]]],
    [13, "Clock", None, 10, [[113, None, 0, None, None, None, [0]]]],
    [14, "Duration", None, 10, [[114, None, 0, None, None, None, [1]]]],
    [15, "Clock (flag at 7)", None, 10, [[115, None, 0, None, None, None, None, [0]]]],
    [16, "Upload", None, 13, [[116, None, 1]]],
]


@pytest.fixture
def form():
    value = parse_form(page(ITEMS), URL)
    value["context_sha256"] = form_fill.context_digest(value)
    return value


def field(form, entry):
    return next(
        (q, f) for q in form["questions"] for f in q["fields"] if f["entry_id"] == str(entry)
    )


def answer(form, entry, values, **extra):
    return {
        "form_url": form["url"],
        "entry_id": str(entry),
        "context_sha256": form["context_sha256"],
        "values": values,
        "needs_user": False,
        "review_note": "",
        **extra,
    }


def test_the_page_data_gives_rows_formats_and_other(form):
    types = {q["title"]: q["type"] for q in form["questions"]}
    assert types["Grid"] == "grid" and types["Box grid"] == "checkbox_grid"
    assert [f["row"] for f in field(form, 106)[0]["fields"]] == ["row one", "row two"]
    assert field(form, 106)[1]["choices"] == ["yes", "no"]  # grid columns keep their order
    formats = {e: field(form, e)[1]["format"] for e in range(109, 116)}
    assert formats == {
        109: "YYYY-MM-DD",
        110: "MM-DD",
        111: "YYYY-MM-DD HH:MM",
        112: "MM-DD HH:MM",
        113: "HH:MM",
        114: "HH:MM:SS",
        115: "HH:MM",
    }
    assert field(form, 102)[1]["other"] and OTHER_CHOICE in field(form, 102)[1]["choices"]
    assert field(form, 104)[1]["other"]
    # A file upload is read like any question; uploading stays the student's, so it is no gap.
    assert types["Upload"] == "file_upload" and form["read_complete"] and not form["warnings"]


def test_classic_questions_are_read_exactly_as_before(form):
    """Text, plain choices and scales gain no keys, so their answers and digests stay valid."""
    for entry in (100, 101, 103, 105):
        assert set(field(form, entry)[1]) == {"entry_id", "choices", "required", "constraints"}
    assert field(form, 102)[1]["choices"] == sorted(["B", "A", OTHER_CHOICE])


ACCEPTED = [
    (102, ["A"]),
    (102, [OTHER_CHOICE + "my own words"]),
    (102, [OTHER_CHOICE + "：after a colon"]),
    (104, ["red", OTHER_CHOICE + "green"]),
    (105, ["2"]),
    (106, ["no"]),
    (108, ["a", "c"]),
    (109, ["2026-11-05"]),
    (109, ["2024-02-29"]),
    (110, ["02-29"]),
    (111, ["2026-11-05 14:30"]),
    (112, ["11-05 09:05"]),
    (113, ["23:59"]),
    (114, ["01:30:00"]),
    (114, ["72:00:00"]),
]
REFUSED = [
    (102, ["C"], "不存在的选项"),
    (102, [OTHER_CHOICE], "没有写内容"),
    (102, [OTHER_CHOICE + "  "], "没有写内容"),
    (103, [OTHER_CHOICE + "x"], "不存在的选项"),  # a dropdown has no "Other"
    (104, [OTHER_CHOICE + "a", OTHER_CHOICE + "b"], "只能有一项"),
    (106, ["yes", "no"], "不支持的题型"),  # a multiple-choice grid row takes one column
    (106, ["maybe"], "不存在的选项"),
    (108, ["d"], "不存在的选项"),
    (109, ["2026-02-30"], "YYYY-MM-DD"),
    (109, ["2026/11/05"], "YYYY-MM-DD"),
    (109, ["11-05"], "YYYY-MM-DD"),
    (110, ["2026-11-05"], "MM-DD"),
    (111, ["2026-11-05"], "YYYY-MM-DD HH:MM"),
    (111, ["2026-11-05T14:30"], "YYYY-MM-DD HH:MM"),
    (113, ["24:00"], "HH:MM"),
    (113, ["2:30 PM"], "HH:MM"),
    (114, ["01:60:00"], "HH:MM:SS"),
]


@pytest.mark.parametrize(("entry", "values"), ACCEPTED)
def test_values_in_the_field_format_are_accepted(form, entry, values):
    assert form_fill.validate_answers([form], [answer(form, entry, values)])


@pytest.mark.parametrize(("entry", "values", "message"), REFUSED)
def test_other_values_are_refused_before_any_link(form, entry, values, message):
    with pytest.raises(WorkflowError, match=message):
        form_fill.validate_answers([form], [answer(form, entry, values)])


def test_a_file_upload_left_to_the_student_is_no_error(form):
    blank = answer(form, 116, [], needs_user=True, review_note="upload the photo")
    assert form_fill.validate_answers([form], [blank]) == []


def test_a_value_written_for_an_upload_is_dropped_not_fatal(form):
    """Never prefilled, whatever the model wrote; the other answers still count (no repair
    turn spent on it) and the upload stays listed for the student."""
    answers = [answer(form, 116, ["photo.jpg"]), answer(form, 100, ["Wang"])]
    assert [x["entry_id"] for x in form_fill.validate_answers([form], answers)] == ["100"]
    coverage = form_fill.form_answer_coverage([form], answers)
    assert [x["entry_id"] for x in coverage["unsupported_fields"]] == ["116"]
    assert "photo.jpg" not in form_fill.prefill_url(form, answers)


def test_prefill_parameters_for_every_type(form):
    answers = [
        answer(form, 100, ["Wang"]),
        answer(form, 102, [OTHER_CHOICE + "own words"]),
        answer(form, 104, ["blue", OTHER_CHOICE + "green"]),
        answer(form, 105, ["3"]),
        answer(form, 106, ["yes"]),
        answer(form, 107, ["no"]),
        answer(form, 108, ["a", "b"]),
        answer(form, 109, ["2026-11-05"]),
        answer(form, 111, ["2026-11-05 14:30"]),
        answer(form, 113, ["08:15"]),
        answer(form, 114, ["00:45:00"]),
    ]
    url = form_fill.prefill_url(form, answers, account="student@school.example")
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https",
        "docs.google.com",
        urlsplit(URL).path,
    )
    params = parse_qsl(parts.query)
    assert params[:4] == [
        ("usp", "pp_url"),
        ("srd", "true"),
        ("authuser", "student@school.example"),
        ("emailAddress", "student@school.example"),
    ]
    assert params[4:] == [
        ("entry.100", "Wang"),
        ("entry.102", "__other_option__"),
        ("entry.102.other_option_response", "own words"),
        ("entry.104", "blue"),
        ("entry.104", "__other_option__"),
        ("entry.104.other_option_response", "green"),
        ("entry.105", "3"),
        ("entry.106", "yes"),
        ("entry.107", "no"),
        ("entry.108", "a"),
        ("entry.108", "b"),
        ("entry.109", "2026-11-05"),
        ("entry.111", "2026-11-05 14:30"),
        ("entry.113", "08:15"),
        ("entry.114", "00:45:00"),
    ]
    assert "formResponse" not in url and "/viewform?" in url


def test_coverage_lists_only_the_file_upload_as_not_prefillable(form):
    coverage = form_fill.form_answer_coverage([form], [answer(form, 100, ["Wang"])])
    assert [x["entry_id"] for x in coverage["unsupported_fields"]] == ["116"]
    missing = {x["entry_id"] for x in coverage["missing_fields"]}
    # Required grid rows and dates are answerable now, so an unanswered one is a real gap.
    assert {"106", "107", "109"} <= missing and "116" not in missing


def test_a_required_upload_is_left_to_the_student_in_words():
    receipt = {
        "forms": [
            {
                "remaining_answer_fields": [],
                "user_input_fields": [],
                "unsupported_fields": [
                    {"title": "Upload the photo", "type": "file_upload", "required": True}
                ],
            }
        ]
    }
    outcome = completion.form_fill_outcome(receipt, {"form_mapping_complete": True})
    # Uploading is the student's own last step in the form, like submitting: the item is done
    # (form_opened, so 重试未完成 and 作答 do not pick it again); the result lists the upload.
    assert outcome == {"status": "form_opened", "form_requires_regeneration": False, "error": None}
    assert not completion.form_answers_incomplete({"form_fill": receipt, "status": "form_opened"})
    # Any other field the form needs by hand still keeps the item open.
    receipt["forms"][0]["unsupported_fields"].append(
        {"title": "Odd", "type": "unknown", "required": True}
    )
    outcome = completion.form_fill_outcome(receipt, {"form_mapping_complete": True})
    assert outcome["status"] == "needs_form" and "1 栏必填内容需要在原页面完成" in outcome["error"]


def test_the_model_is_told_every_value_format():
    schema = drafting.output_schema(["coursework:1"])
    values = schema["properties"]["form_answers"]["items"]["properties"]["values"]
    assert values["type"] == "array" and values["items"] == {"type": "string"}
    for text in (drafting.VALUES_DESCRIPTION, drafting.FORM_VALUES):
        for token in (OTHER_CHOICE, "YYYY-MM-DD", "MM-DD", "HH:MM", "HH:MM:SS"):
            assert token in text
    assert values["description"] == drafting.VALUES_DESCRIPTION
    for token in ("checkbox_grid", "file_upload", "rating", "unknown"):
        assert token in drafting.FORM_VALUES
    skill = (drafting.SKILL / "SKILL.md").read_text(encoding="utf-8")
    for token in (
        OTHER_CHOICE,
        "checkbox_grid",
        "YYYY-MM-DD HH:MM",
        "HH:MM:SS",
        "file_upload",
        "rating",
        "unknown",
    ):
        assert token in skill


# --------------------------------------------------------------------------- reviewer findings


def test_derived_keys_do_not_change_the_digest_of_an_older_answer():
    """C5: other/row/format come from what the field already holds, so an answer prepared
    before the reader added them (c052ca0 parsed the same page without them) still fits."""
    import copy

    current = parse_form(page(ITEMS), URL)
    older = copy.deepcopy(current)
    for question in older["questions"]:
        for field in question["fields"]:
            for key in ("other", "row", "format"):
                field.pop(key, None)
    assert form_fill.context_digest(older) == form_fill.context_digest(current)
    # Real changes still count: another choice, another row label.
    changed = copy.deepcopy(current)
    changed["questions"][2]["fields"][0]["choices"].append("C")
    assert form_fill.context_digest(changed) != form_fill.context_digest(current)
    changed = copy.deepcopy(current)
    changed["questions"][6]["fields"][0]["constraints"][0] = ["another row"]
    assert form_fill.context_digest(changed) != form_fill.context_digest(current)


def test_a_rating_with_its_values_is_read_and_prefilled_like_a_scale():
    """C3: a Rating question (type 18) is kept; with its values listed it is answerable."""
    items = [*ITEMS[:1], [20, "Stars", None, 18, [[120, [["1"], ["2"], ["3"], ["4"], ["5"]], 1]]]]
    form = parse_form(page(items), URL)
    form["context_sha256"] = form_fill.context_digest(form)
    rating = form["questions"][-1]
    assert rating["type"] == "rating" and rating["fields"][0]["choices"] == list("12345")
    assert form["read_complete"]
    url = form_fill.prefill_url(form, [answer(form, 120, ["4"])])
    assert ("entry.120", "4") in parse_qsl(urlsplit(url).query)
    with pytest.raises(WorkflowError, match="不存在的选项"):
        form_fill.validate_answers([form], [answer(form, 120, ["6"])])


@pytest.mark.parametrize(
    "item",
    [
        [20, "Stars", None, 18, [[120, None, 1, None, None, None, None, None, None, None, [5]]]],
        [20, "Stars", None, 18, [[120, "five", 1]]],
        [20, "Mystery", None, 42, [[120, None, 1]]],
    ],
)
def test_an_unknown_question_is_kept_for_the_student_not_dropped(item):
    """C3: a rating without listed values, or a type the reader does not know, stays a question
    (type unknown) that coverage lists as not prefillable, even when required; the form is
    marked not completely read, and nothing is ever prefilled into it."""
    form = parse_form(page([*ITEMS[:1], item]), URL)
    form["context_sha256"] = form_fill.context_digest(form)
    unknown = form["questions"][-1]
    assert unknown["type"] == "unknown" and unknown["title"] == item[1]
    assert unknown["fields"] == [{"entry_id": "120", "choices": [], "required": True}]
    assert not form["read_complete"] and any("未知题型" in w for w in form["warnings"])
    coverage = form_fill.form_answer_coverage([form], [answer(form, 100, ["Wang"])])
    assert [(x["entry_id"], x["required"]) for x in coverage["unsupported_fields"]] == [
        ("120", True)
    ]
    assert form_fill.validate_answers([form], [answer(form, 120, ["4"])]) == []
    # It does not count in the digest: the reader used to drop it, and older answers still fit.
    without = parse_form(page(ITEMS[:1]), URL)
    assert form_fill.context_digest(without) == form["context_sha256"]


def test_an_unknown_item_without_fields_stays_only_a_warning():
    form = parse_form(page([*ITEMS[:1], [20, "Video", None, 12, None]]), URL)
    assert [q["title"] for q in form["questions"]] == ["Name"] and not form["read_complete"]


def test_a_blank_option_is_other_only_with_the_other_flag():
    """C13a: option[4] is the is-other flag where the page data has it."""
    flagged = [3, "Pick", None, 2, [[102, [["A", None, None, None, 0], OTHER], 1]]]
    blank = [
        3,
        "Pick",
        None,
        2,
        [[102, [["A", None, None, None, 0], ["", None, None, None, 0]], 1]],
    ]
    legacy = [3, "Pick", None, 2, [[102, [["A"], [""]], 1]]]
    fields = [
        parse_form(page([item]), URL)["questions"][0]["fields"][0]
        for item in (flagged, blank, legacy)
    ]
    assert fields[0]["other"] is True and OTHER_CHOICE in fields[0]["choices"]
    assert "other" not in fields[1] and fields[1]["choices"] == ["", "A"]
    assert fields[2]["other"] is True


def test_a_closed_form_is_not_reported_as_a_login_wall():
    """C1: Google sends a closed form's viewform to its own /closedform page; that is no login
    (needs_login false, its own reason), while a redirect to the sign-in still is."""
    from urllib.request import Request

    from classroomautowork.form_capture import needs_login
    from classroomautowork.form_read import FormClosed, _ResponderRedirects

    handler = _ResponderRedirects()
    with pytest.raises(FormClosed) as closed:
        handler.redirect_request(
            Request(URL), None, 302, "Found", {}, URL.replace("viewform", "closedform")
        )
    assert not needs_login(closed.value) and closed.value.reason == "form_closed"
    with pytest.raises(WorkflowError) as other_page:
        handler.redirect_request(
            Request(URL), None, 302, "Found", {}, URL.replace("viewform", "alreadyresponded")
        )
    assert not needs_login(other_page.value)
    for target in (
        "https://accounts.google.com/ServiceLogin?continue=" + URL,
        "https://docs.google.com/forms/d/e/another-form-0000/closedform",
    ):
        with pytest.raises(WorkflowError) as login:
            handler.redirect_request(Request(URL), None, 302, "Found", {}, target)
        assert needs_login(login.value)


# The live form of 2026-11-06: one paragraph question with a picture added to the question itself
# (index 9 of the item), not a separate image item. It was read as text only, so the model was
# told the picture was missing.
LOOK = [
    1124768656,
    "このルックを、見ていない人に言葉で伝えてください。",
    None,
    1,
    [[1688752510, None, 1]],
    None,
    None,
    None,
    None,
    [["s-blob-v1-IMAGE-7vFIkBsonRc", None, [740, 1110, 0]]],
    None,
    [None, "このルックを、見ていない人に言葉で伝えてください。"],
]


def test_a_picture_added_to_a_question_is_its_material():
    value = parse_form(page([LOOK]), URL)
    assert [q["item_id"] for q in value["questions"]] == ["1124768656"]
    assert value["questions"][0]["type"] == "paragraph"
    assert value["media"] == [
        {
            "item_id": "1124768656",
            "page": 1,
            "title": LOOK[1],
            "description": "",
            "type": "question_image",
            "media_id": "s-blob-v1-IMAGE-7vFIkBsonRc",
            "width": 740,
            "height": 1110,
        }
    ]
    # Until its pixels are attached the picture is a gap, as an image item is.
    assert not value["read_complete"] and "图片材料尚未读取" in value["warnings"][0]


@pytest.mark.parametrize(
    "attached",
    [
        [["bad id!", None, [740, 1110, 0]]],
        [["s-blob-v1-IMAGE-x1234567", None, [0, 1110, 0]]],
        [["s-blob-v1-IMAGE-x1234567", None, [740, 1110]]] * 2,
        "s-blob-v1-IMAGE-x1234567",
    ],
)
def test_a_question_picture_that_cannot_be_read_is_a_warning(attached):
    item = [*LOOK[:9], attached, *LOOK[10:]]
    value = parse_form(page([item]), URL)
    assert value["media"] == [] and len(value["questions"]) == 1
    assert "表单包含无法识别的图像，需在原页面核对。" in value["warnings"]


def test_questions_without_a_picture_are_unchanged(form):
    assert form["media"] == [] and all(q["type"] != "question_image" for q in form["questions"])


def test_a_link_too_long_for_google_leaves_the_longest_text_out(form):
    """2026-11-06, live: three Japanese reaction answers made a 10,276-character link and Google
    answered 400. The link now stays within the limit; the longest text answer is left out for
    the student to paste, the choices and the shorter text stay in."""
    long = "授業で学んだことを自分の言葉で書く。" * 50
    answers = [
        answer(form, "101", [long]),
        answer(form, "100", ["名前" * 40]),
        answer(form, "102", ["A"]),
    ]
    with pytest.raises(WorkflowError, match="长度上限"):
        form_fill.prefill_url(form, answers)
    url, left_out = form_fill.fit_prefill(form, answers, account="student@school.example")
    assert len(url) <= form_fill.MAX_PREFILL_URL
    assert [x["entry_id"] for x in left_out] == ["101"] and left_out[0]["values"] == [long]
    query = dict(parse_qsl(urlsplit(url).query))
    assert "entry.101" not in query and query["entry.102"] == "A" and "entry.100" in query
    assert query["emailAddress"] == "student@school.example"


def test_a_short_link_leaves_nothing_out(form):
    answers = [answer(form, "101", ["短い答え"]), answer(form, "102", ["A"])]
    url, left_out = form_fill.fit_prefill(form, answers)
    assert left_out == [] and url == form_fill.prefill_url(form, answers)


def test_every_text_may_be_left_out_but_never_a_choice(form, monkeypatch):
    monkeypatch.setattr(form_fill, "MAX_PREFILL_URL", 170)
    texts = [answer(form, "101", ["x" * 300]), answer(form, "100", ["y" * 200])]
    url, left_out = form_fill.fit_prefill(form, texts)
    assert {x["entry_id"] for x in left_out} == {"100", "101"} and "entry." not in url
    monkeypatch.setattr(form_fill, "MAX_PREFILL_URL", 60)
    with pytest.raises(WorkflowError, match="选择题"):
        form_fill.fit_prefill(form, [answer(form, "102", ["A"])])
