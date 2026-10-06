"""Offline fixtures for native responder prefill; not live Google/model proof."""

import copy
import json
from dataclasses import asdict
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from test_forms_student import URL, published_page
from test_workflow import prepared_fixture

from classroomautowork import auth, drafting, form_fill, google_read
from classroomautowork.errors import WorkflowError
from classroomautowork.form_read import form_chunks, parse_form
from classroomautowork.local import atomic_json, sha256_file
from classroomautowork.policy import confirmed_gate
from classroomautowork.student import StudentProfile


def native_form(kind=2):
    form = parse_form(published_page(kind=kind), URL)
    form["context_sha256"] = form_fill.context_digest(form)
    return form


def answer(form, entry="202", values=None, **extra):
    return {
        "form_url": URL,
        "entry_id": entry,
        "context_sha256": form["context_sha256"],
        "values": values or ["A"],
        "needs_user": False,
        "review_note": "",
        **extra,
    }


def test_native_checkbox_prefill_preserves_all_values_and_cannot_submit():
    form = native_form(4)
    url = form_fill.prefill_url(form, [answer(form, values=["A", "B"])], account="unit@school.test")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "docs.google.com" and parsed.path.endswith("/viewform")
    assert "formResponse" not in url and "submit" not in query
    assert query["entry.202"] == ["A", "B"]
    assert query["authuser"] == ["unit@school.test"] and query["srd"] == ["true"]
    assert query["emailAddress"] == ["unit@school.test"]


@pytest.mark.parametrize(
    "change",
    [
        {"form_url": "https://attacker.test/form"},
        {"entry_id": "999"},
        {"context_sha256": "old-question"},
        {"values": ["invented option"]},
        {"values": ["A", "B"]},
        {"values": ["[E:internal]"]},
        {"values": [{}]},
    ],
)
def test_untrusted_targets_mappings_options_and_internal_audit_text_rejected(change):
    form = native_form()
    with pytest.raises(WorkflowError):
        form_fill.prefill_url(form, [answer(form, **change)])


def test_identity_cannot_override_private_user_config():
    form = native_form()
    form["questions"][0]["title"] = "学籍番号"
    form["context_sha256"] = form_fill.context_digest(form)
    profile = StudentProfile(student_id="UNIT-STUDENT")
    with pytest.raises(WorkflowError, match="身份"):
        form_fill.validate_answers([form], [answer(form, "200", ["INVENTED"])], profile)
    assert form_fill.validate_answers([form], [answer(form, "200", [profile.student_id])], profile)


def test_empty_optional_feedback_does_not_discard_real_assignment_answers():
    form = native_form()
    empty = answer(form, "204")
    empty["values"] = []
    validated = form_fill.validate_answers([form], [answer(form), empty])
    assert len(validated) == 1 and validated[0]["entry_id"] == "202"
    query = parse_qs(urlparse(form_fill.prefill_url(form, validated)).query)
    assert query["entry.202"] == ["A"] and "entry.204" not in query


def test_missing_required_personal_fact_stays_blank_without_discarding_other_answers():
    form = native_form()
    missing = answer(form, "200", needs_user=True)
    missing["values"] = []
    validated = form_fill.validate_answers([form], [missing, answer(form)])
    query = parse_qs(urlparse(form_fill.prefill_url(form, validated)).query)
    assert query["entry.202"] == ["A"] and "entry.200" not in query
    missing["needs_user"] = False
    with pytest.raises(WorkflowError):
        form_fill.validate_answers([form], [missing, answer(form)])


def test_already_submitted_assignment_does_not_allow_browser_filling():
    reader = SimpleNamespace(
        own_submissions=lambda _: [{"courseWorkId": "2", "state": "TURNED_IN"}],
        assignment=lambda *args: pytest.fail("Must stop before reading another destination"),
    )
    with pytest.raises(WorkflowError, match="待完成"):
        form_fill.current_response_urls(reader, "1", "2")


def fill_fixture(tmp_path, monkeypatch):
    package, _, _, _ = prepared_fixture(tmp_path)
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(ai_confirmation=True, student_profile=asdict(StudentProfile()))
    manifest["policy"] = confirmed_gate(manifest["policy"], True)
    atomic_json(package / "manifest.json", manifest)
    form = native_form()
    requirements = json.loads((package / "requirements.json").read_text(encoding="utf-8"))
    requirements["response_forms"] = [{"url": URL}]
    requirements["sources"].extend(form_chunks(form))
    atomic_json(package / "requirements.json", requirements)
    atomic_json(package / "form-answers.json", {"answers": [answer(form)]})
    generation = {
        "status": "completed",
        "form_answers_sha256": sha256_file(package / "form-answers.json"),
    }
    # Explicit boundary fixture; real model/OAuth checks are performed outside these tests.
    monkeypatch.setattr(
        drafting, "reusable_review", lambda _: {"draft_sha256": "unit", "generation": generation}
    )
    monkeypatch.setattr(auth, "credentials_for", lambda _: (object(), {}))
    reader = SimpleNamespace(
        own_submissions=lambda _: [{"courseWorkId": "2", "state": "CREATED"}],
        assignment=lambda *args: {"materials": [{"link": {"url": URL}}]},
    )
    monkeypatch.setattr(google_read, "GoogleReader", lambda _: reader)
    monkeypatch.setattr(form_fill, "read_form", lambda _: form)
    settings = SimpleNamespace(data_dir=tmp_path, school_email="unit@school.test")
    return package, settings, form, generation


def test_opens_actual_prefill_once_records_unverified_browser_state_and_reuses(
    tmp_path, monkeypatch
):
    package, settings, _, _ = fill_fixture(tmp_path, monkeypatch)
    opened = []
    first = form_fill.fill_response_forms(
        settings, package, opener=lambda url, **kw: opened.append(url) or True
    )
    assert first["status"] == "opened_native_prefill" and len(opened) == 1
    assert first["forms"][0]["prefilled_fields"] == 1
    assert first["forms"][0]["browser_values_verified"] is False
    assert first["forms"][0]["draft_saved_verified"] is False
    assert first["submission"] == "manual_only"
    second = form_fill.fill_response_forms(
        settings,
        package,
        opener=lambda *args, **kw: pytest.fail("Don't reopen and overwrite reviewed answers"),
    )
    assert second["reused"] and len(opened) == 1


def test_changed_original_or_tampered_answers_stop_before_browser_open(tmp_path, monkeypatch):
    package, settings, form, _ = fill_fixture(tmp_path, monkeypatch)
    changed = copy.deepcopy(form)
    changed["questions"][1]["title"] = "Modified actual question"
    monkeypatch.setattr(form_fill, "read_form", lambda _: changed)

    def never_open(*args, **kw):
        pytest.fail("Must not open a mismapped form")

    with pytest.raises(WorkflowError, match="修改"):
        form_fill.fill_response_forms(settings, package, opener=never_open)
    atomic_json(package / "form-answers.json", {"answers": [answer(form, values=["B"])]})
    with pytest.raises(WorkflowError, match="逐栏"):
        form_fill.fill_response_forms(settings, package, opener=never_open)


def test_browser_open_failure_keeps_retryable_plan_without_claiming_filled(tmp_path, monkeypatch):
    package, settings, _, _ = fill_fixture(tmp_path, monkeypatch)
    with pytest.raises(WorkflowError, match="浏览器打开失败"):
        form_fill.fill_response_forms(settings, package, opener=lambda *args, **kw: False)
    record = json.loads((package / "form-fill.json").read_text(encoding="utf-8"))
    assert record["status"] == "browser_open_failed" and not record["forms"][0]["opened"]
    assert record["forms"][0]["url"].startswith(URL + "?")
    assert (
        form_fill.fill_response_forms(settings, package, opener=lambda *args, **kw: True)["status"]
        == "opened_native_prefill"
    )
