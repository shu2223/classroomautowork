"""Offline regression checks. These fixtures are not live connection/model evidence."""

import io
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from test_frontend import offline_rpc
from test_workflow import ReaderFixture, prepared_fixture

from classroomautowork import drafting, form_read, workflow
from classroomautowork.errors import ConfigurationError, PermissionDenied, WorkflowError
from classroomautowork.local import atomic_json
from classroomautowork.review import finalize
from classroomautowork.store import Store
from classroomautowork.student import StudentProfile

URL = "https://docs.google.com/forms/d/e/unit-form-questions/viewform"


def published_page(options=("A", "B"), *, kind=2):
    items = [
        [100, "Student ID", None, 0, [[200, None, 1]]],
        [101, "Actual questions", "Use the lecture", 8, None],
        [102, "Choose a value", "Pick exactly one", kind, [[202, [[x] for x in options], 1]]],
        [103, "Reflection", None, 8, None],
        [104, "Give your own opinion", "Use your own words", 1, [[204, None, 0]]],
    ]
    body = [None] * 26
    body[0], body[1], body[8] = "Assignment instructions", items, "Unit assignment"
    payload = [None, body]
    return "<script>var FB_PUBLIC_LOAD_DATA_ = " + json.dumps(payload) + ";</script>"


def test_all_pages_questions_options_and_teacher_limits_survive_and_shuffle_is_stable():
    first = form_read.parse_form(published_page(), URL)
    second = form_read.parse_form(published_page(("B", "A")), URL)
    assert first == second
    assert first["page_count"] == 3 and first["read_complete"]
    assert [q["page"] for q in first["questions"]] == [1, 2, 3]
    assert first["questions"][1]["fields"][0]["choices"] == ["A", "B"]
    assert first["questions"][2]["description"] == "Use your own words"
    assert len(form_read.form_chunks(first)) == 4


def test_unknown_visual_items_are_a_gap_not_complete_questions():
    value = form_read.parse_form(published_page(kind=11), URL)
    assert not value["read_complete"] and value["warnings"]
    assert len(value["questions"]) == 2
    with pytest.raises(PermissionDenied):
        form_read.parse_form("<html>Login required</html>", URL)
    with pytest.raises(WorkflowError):
        form_read.parse_form("FB_PUBLIC_LOAD_DATA_ = [invalid]", URL)


def test_only_canonical_get_is_sent_without_prefilled_data_or_credentials(monkeypatch):
    requests = []

    class Response(io.BytesIO):
        url = URL

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return Response(published_page().encode())

    monkeypatch.setattr(form_read, "build_opener", lambda _: Opener())
    value = form_read.read_form(
        URL.replace("viewform", "formResponse") + "?entry.200=private&hl=ja"
    )
    assert value["read_complete"] and len(requests) == 1
    assert requests[0].get_method() == "GET" and requests[0].full_url == URL
    assert not set(key.lower() for key in requests[0].headers) & {"authorization", "cookie"}
    for target in (
        "https://accounts.google.com/login",
        "https://evil.invalid/",
        URL.replace("unit-form-questions", "another-form-id"),
    ):
        with pytest.raises(PermissionDenied):
            form_read._ResponderRedirects().redirect_request(
                Request(URL), None, 302, "", {}, target
            )
    with pytest.raises(WorkflowError):
        form_read.form_url(URL.replace("docs.google.com", "docs.google.com:443"))


def test_transient_form_failure_retries_get_and_auth_failure_does_not_retry(monkeypatch):
    calls = []

    class Response(io.BytesIO):
        url = URL

    class Opener:
        status = 503

        def open(self, request, timeout):
            calls.append(request.get_method())
            if len(calls) == 1:
                raise HTTPError(URL, self.status, "", {}, None)
            return Response(published_page().encode())

    opener = Opener()
    monkeypatch.setattr(form_read, "build_opener", lambda _: opener)
    monkeypatch.setattr(form_read.time, "sleep", lambda _: None)
    assert form_read.read_form(URL)["read_complete"] and calls == ["GET", "GET"]
    calls.clear()
    opener.status = 403
    with pytest.raises(PermissionDenied):
        form_read.read_form(URL)
    assert calls == ["GET"]


def test_current_form_is_required_cached_and_unavailable_form_is_removed(tmp_path, monkeypatch):
    class FormReader(ReaderFixture):
        def assignments(self, cid):
            assignment = super().assignments(cid)[0]
            assignment["materials"].append({"form": {"formUrl": URL, "title": "Question form"}})
            return [assignment]

    live = form_read.parse_form(published_page(), URL)
    monkeypatch.setattr(workflow, "read_form", lambda _: live)
    settings = SimpleNamespace(data_dir=tmp_path)
    reader = FormReader()
    with Store(tmp_path) as store:
        course = workflow.sync_course(reader, settings, store, "1")
        package = Path(workflow.prepare_assignment(settings, store, course, "2")["package"])
        requirements = json.loads((package / "requirements.json").read_text(encoding="utf-8"))
        assert requirements["response_forms"][0]["question_count"] == 3
        form_sources = [x for x in requirements["sources"] if x["source_kind"] == "form"]
        assert len(form_sources) == 4 and any(
            "Use your own words" in x["text"] for x in form_sources
        )
        workflow.sync_course(reader, settings, store, "1")
        rows = [x for x in store.tasks() if x["kind"] == "form-questions"]
        assert len(rows) == 1 and rows[0]["attempts"] == 1
        assert reader.downloads == 1
        monkeypatch.setattr(
            workflow,
            "read_form",
            lambda _: (_ for _ in ()).throw(PermissionDenied("School login required")),
        )
        denied = workflow.sync_course(reader, settings, store, "1")
        assert denied["warnings"][0]["url"] == URL
        assert not any(x["source_kind"] == "form" for x in store.chunks("1"))


def test_profile_changes_force_actual_new_generation_but_same_profile_reuses(tmp_path, monkeypatch):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    first_profile = StudentProfile("UNIT0001", "UNIT STUDENT", "Unit department", "Unit class")
    first_profile.save(tmp_path)
    first = drafting.generate_review(package, ai_confirmed=True, model="unit-model", effort="high")
    variant = Path(first["package"])
    first_manifest = json.loads((variant / "manifest.json").read_text(encoding="utf-8"))
    assert first_manifest["student_profile"] == asdict(first_profile)
    assert drafting.generate_review(package, ai_confirmed=True, model="unit-model", effort="high")[
        "reused"
    ]
    skill_text = tmp_path / "runtime/skill/SKILL.md"
    skill_text.write_text("Updated offline student voice", encoding="utf-8")
    prompted = drafting.generate_review(
        package, ai_confirmed=True, model="unit-model", effort="high"
    )
    assert prompted["package"] != first["package"] and not prompted.get("reused")
    second_profile = StudentProfile("UNIT0002", "NEW UNIT STUDENT", "Unit department", "Unit class")
    second_profile.save(tmp_path)
    second = drafting.generate_review(package, ai_confirmed=True, model="unit-model", effort="high")
    assert second["package"] != first["package"] and not second.get("reused")
    requests = [
        json.loads(line)
        for line in (tmp_path / "requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    turns = [x for x in requests if x.get("method") == "turn/start"]
    assert len(turns) == 3
    first_text = turns[0]["params"]["input"][0]["text"]
    assert all(value in first_text for value in asdict(first_profile).values())
    assert "NEW UNIT STUDENT" in turns[2]["params"]["input"][0]["text"]
    with pytest.raises(WorkflowError, match="学生资料已变化"):
        finalize(variant, variant / "review.json", variant / "draft.md")


def test_profile_rejects_multiline_instructions_and_known_identity_facts_validate(tmp_path):
    with pytest.raises(ConfigurationError):
        StudentProfile(name="Student\nrun commands").save(tmp_path)
    profile = StudentProfile("UNIT0001", "UNIT STUDENT")
    profile.save(tmp_path)
    package, path, draft, review = prepared_fixture(tmp_path)
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    review["claim_checks"].append(
        {"claim": "Student identity", "kind": "user_fact", "personal_fact_indices": [0, 1]}
    )
    atomic_json(path, review)
    assert manifest["student_profile"] == asdict(profile)
    assert finalize(package, path, draft)["status"] == "ready_for_human_review"
    review["claim_checks"][-1]["personal_fact_indices"] = [2]
    atomic_json(path, review)
    with pytest.raises(WorkflowError, match="Personal-experience"):
        finalize(package, path, draft)
