"""Fail fast and explain (2026-11-06): an assignment whose own Google Form was not read stops
before any model call, and an empty answer that names what it lacks does not spend the
automatic repair. Both say what is missing and how to supply it."""

import json
from pathlib import Path

import pytest
from test_frontend import seed_pending, unit_settings
from test_school_forms import URL, FakeBrowser, FormReader, raise_login

from classroomautowork import drafting, school_browser, workflow
from classroomautowork.errors import FormsUnread, MissingSources, RequestFailed
from classroomautowork.ui_jobs import FrontendJobs


def package_with(tmp_path, warnings, assignment_id="2"):
    package = tmp_path / "package"
    package.mkdir(parents=True, exist_ok=True)
    (package / "manifest.json").write_text(
        json.dumps({"assignment_id": assignment_id, "warnings": warnings}), encoding="utf-8"
    )
    return package


def unread(reason, role="coursework", assignment_id="2"):
    return {
        "source": "form:abc",
        "url": URL,
        "origins": [{"source_id": "x", "assignment_id": assignment_id, "role": role}],
        "error": "原始错误：表单读取失败（HTTP 500）。",
        "form_unread": reason,
    }


@pytest.mark.parametrize(
    ("reason", "words"),
    [
        ("login_required", "需要学校账号登录才能读取题目"),
        ("login_expired", "登录已过期"),
        ("account_mismatch", "与设置里的学校邮箱不一致"),
        ("read_failed", "没有读到题目"),
    ],
)
def test_an_unread_own_form_stops_before_any_model_call(tmp_path, monkeypatch, reason, words):
    monkeypatch.setattr(drafting, "generate_review", lambda *a, **k: pytest.fail("no model call"))
    events = []
    package = package_with(tmp_path, [unread(reason)])
    with pytest.raises(FormsUnread) as caught:
        drafting.generate_answer(package, ai_confirmed=True, progress=events.append)
    message = str(caught.value)
    first, _, detail = message.partition("\n")
    assert words in first and "这次没有调用 AI" in first
    if reason != "read_failed":
        assert "设置 → 学校账号" in first and detail == URL
    else:
        assert detail == "原始错误：表单读取失败（HTTP 500）。"
    assert any("没有调用 AI" in str(e) for e in events)


def test_a_form_other_posts_link_to_does_not_stop_the_assignment(tmp_path, monkeypatch):
    calls = []

    def review(package, **options):
        calls.append(options.get("repair_attempt"))
        raise RequestFailed("stop here")

    monkeypatch.setattr(drafting, "generate_review", review)
    warnings = [
        unread("login_required", role="announcement", assignment_id=None),
        unread("login_required", assignment_id="9"),
    ]
    with pytest.raises(RequestFailed):
        drafting.generate_answer(package_with(tmp_path, warnings), ai_confirmed=True)
    assert calls == [0]


def test_materials_only_runs_are_not_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(drafting, "generate_review", lambda *a, **k: {"status": "materials"})
    package = package_with(tmp_path, [unread("login_required")])
    assert drafting.generate_answer(package, ai_confirmed=False) == {"status": "materials"}


def empty_answer(missing, complete=False):
    return {
        "status": "needs_user_input",
        "draft_sha256": None,
        "generation": {"requirements_complete": complete, "missing_sources": missing},
    }


def test_an_empty_answer_naming_what_it_lacks_is_not_repaired(tmp_path, monkeypatch):
    calls = []
    missing = [
        "Google フォームの設問と選択肢（ログインが必要なため取得できていない） " * 3,
        "設問で参照されている写真",
        "第3回の講義資料",
        "課題の評価基準",
    ]

    def review(package, **options):
        calls.append(options.get("repair_attempt"))
        return empty_answer(missing)

    monkeypatch.setattr(drafting, "generate_review", review)
    with pytest.raises(MissingSources) as caught:
        drafting.generate_answer(package_with(tmp_path, []), ai_confirmed=True)
    assert calls == [0]
    message = str(caught.value)
    assert "設問で参照されている写真" in message and "第3回の講義資料" in message
    assert "課題の評価基準" not in message and "（共 4 项）" in message  # three named, then a count
    assert "…" in message  # a long item is shortened
    assert "设置 → 学校账号" in message and "补充说明" in message and "没有自动重试" in message


def test_without_a_form_the_advice_is_the_supplement_only(tmp_path, monkeypatch):
    monkeypatch.setattr(drafting, "generate_review", lambda *a, **k: empty_answer(["講義の録画"]))
    with pytest.raises(MissingSources) as caught:
        drafting.generate_answer(package_with(tmp_path, []), ai_confirmed=True)
    assert "学校账号" not in str(caught.value) and "补充说明" in str(caught.value)


@pytest.mark.parametrize("answer", [empty_answer([]), empty_answer(["x"], complete=True)])
def test_other_empty_answers_still_get_their_one_repair(tmp_path, monkeypatch, answer):
    calls = []

    def review(package, **options):
        calls.append(options.get("repair_attempt"))
        return answer

    monkeypatch.setattr(drafting, "generate_review", review)
    with pytest.raises(Exception, match="AI 未生成实际答案"):
        drafting.generate_answer(package_with(tmp_path, []), ai_confirmed=True)
    assert calls == [0, 1]


class PendingFormReader(FormReader):
    def own_submissions(self, _course):
        return [{"courseWorkId": "2", "state": "CREATED"}]


def test_the_item_waits_for_the_login_and_the_retry_reads_the_form(tmp_path, monkeypatch):
    """No login: needs_form before any model call. Signed in: the retry reads the form with the
    school-login browser and the answer turn starts."""
    settings = unit_settings(tmp_path)
    jobs = FrontendJobs(settings)
    seed_pending(jobs)
    monkeypatch.setattr(workflow.KnowledgeClient, "from_settings", lambda _s: PendingFormReader())
    monkeypatch.setattr(workflow, "read_form", raise_login)
    turns = []

    def review(package, **options):
        manifest = json.loads((Path(package) / "manifest.json").read_text(encoding="utf-8"))
        turns.append(manifest)
        raise RequestFailed("unit: the turn would start here")

    monkeypatch.setattr(drafting, "generate_review", review)
    first = jobs.start_selected([{"course_id": "1", "assignment_id": "2"}], ai_confirmed=True)
    assert jobs.wait_idle()
    item = jobs.job(first["id"])["items"][0]
    assert item["status"] == "needs_form" and not turns
    assert "设置 → 学校账号" in item["error"] and "这次没有调用 AI" in item["error"]
    assert jobs.job(first["id"])["status"] == "completed_with_issues"

    school_browser._write_record(
        settings.data_dir,
        {
            "email": settings.school_email,
            "verified_at": "2026-11-06T00:00:00+00:00",
            "state": "signed_in",
        },
    )
    browser = FakeBrowser()
    monkeypatch.setattr(school_browser, "manager", lambda _settings: browser)
    second = jobs.retry(first["id"])
    assert jobs.wait_idle()
    assert browser.calls and len(turns) == 1
    assert turns[0]["response_forms"] and not drafting.unread_own_forms(turns[0])
    assert jobs.job(second["id"])["items"][0]["status"] == "failed"  # the stand-in turn's error


def test_a_closed_own_form_stops_with_its_own_words(tmp_path, monkeypatch):
    """C1: a closed form is said to be closed, not to need the login."""
    monkeypatch.setattr(drafting, "generate_review", lambda *a, **k: pytest.fail("no model call"))
    with pytest.raises(FormsUnread) as caught:
        drafting.generate_answer(package_with(tmp_path, [unread("form_closed")]), ai_confirmed=True)
    first = str(caught.value).split("\n")[0]
    assert "已关闭" in first and "学校账号" not in first and "这次没有调用 AI" in first


def test_the_login_reason_leads_when_several_own_forms_are_unread():
    """C8: signing in is the step to take as soon as one own form needs it."""
    message = drafting.unread_forms_message([unread("read_failed"), unread("login_required")])
    assert "设置 → 学校账号" in message.split("\n")[0] and "共 2 份" in message


@pytest.mark.parametrize(
    ("missing", "advice"),
    [
        (["background information of the lecture"], False),
        (["the format of the report"], False),
        (["the questions of the Google Form"], True),
        (["forms linked from the post"], True),
        (["フォームの設問"], True),
        (["表单里的图片"], True),
    ],
)
def test_the_form_advice_needs_the_word_form(tmp_path, monkeypatch, missing, advice):
    """C8: 'information' or 'format' is no form."""
    monkeypatch.setattr(drafting, "generate_review", lambda *a, **k: empty_answer(missing))
    with pytest.raises(MissingSources) as caught:
        drafting.generate_answer(package_with(tmp_path, []), ai_confirmed=True)
    assert ("学校账号" in str(caught.value)) is advice
