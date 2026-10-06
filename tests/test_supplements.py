"""Offline recovery checks; these fixtures never stand in for real browser/model reads."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_forms_student import URL, published_page
from test_frontend import offline_rpc
from test_workflow import ReaderFixture, prepared_fixture

from classroomautowork import drafting, followups, workflow
from classroomautowork.errors import NeedsInput, PermissionDenied, WorkflowError
from classroomautowork.form_capture import authenticated_form_cache, import_form_capture
from classroomautowork.local import atomic_json
from classroomautowork.store import Store
from classroomautowork.supplements import load_supplement, save_supplement

ACCOUNT = "unit-test@example.invalid"


def test_supplement_is_assignment_scoped_and_idempotent_and_invalidates_generated_output(
    tmp_path, monkeypatch
):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    receipt = drafting.generate_review(package, ai_confirmed=True, model="unit-model")
    assert receipt["draft_sha256"]
    value = {"text": "Use my actual notes", "personal_facts": ["A real user record"]}
    save_supplement(tmp_path, "1", "2", value)
    path = tmp_path / "supplements/1/2.json"
    first = path.read_bytes()
    save_supplement(tmp_path, "1", "2", value)
    assert path.read_bytes() == first
    assert load_supplement(tmp_path, "1", "3")["text"] == ""
    assert drafting.reusable_review(Path(receipt["package"]), model="unit-model") is None
    with Store(tmp_path) as db:
        course = workflow.sync_course(ReaderFixture(), SimpleNamespace(data_dir=tmp_path), db, "1")
        updated = workflow.prepare_assignment(SimpleNamespace(data_dir=tmp_path), db, course, "2")
    assert updated["package"] != str(package)
    manifest = json.loads((Path(updated["package"]) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["supplement"]["personal_facts"] == value["personal_facts"]


def test_native_browser_import_survives_401_but_not_403_or_wrong_account_and_discards_auth_data(
    tmp_path,
):
    source = published_page() + "<script>AUTH_SECRET_TOKEN = 'must-not-survive';</script>"
    imported = import_form_capture(tmp_path, ACCOUNT, URL, source, [URL])
    recovered = authenticated_form_cache(tmp_path, ACCOUNT, URL, PermissionDenied("HTTP 401"))
    assert recovered == imported and recovered["page_count"] == 3
    assert recovered["questions"][1]["fields"][0]["entry_id"] == "202"
    file = next((tmp_path / "browser-form-questions").glob("*.json"))
    assert "AUTH_SECRET_TOKEN" not in file.read_text(encoding="utf-8")
    with pytest.raises(PermissionDenied):
        authenticated_form_cache(
            tmp_path, "another@example.invalid", URL, PermissionDenied("HTTP 401")
        )
    with pytest.raises(PermissionDenied, match="403"):
        authenticated_form_cache(tmp_path, ACCOUNT, URL, PermissionDenied("HTTP 403"))
    with pytest.raises(WorkflowError, match="原始来源"):
        import_form_capture(tmp_path, ACCOUNT, URL, published_page(), [])


@pytest.mark.parametrize("changed", ["digest", "expired"])
def test_changed_or_expired_browser_capture_is_rejected(tmp_path, changed):
    import_form_capture(tmp_path, ACCOUNT, URL, published_page(), [URL])
    file = next((tmp_path / "browser-form-questions").glob("*.json"))
    record = json.loads(file.read_text(encoding="utf-8"))
    if changed == "expired":
        record["captured_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        from classroomautowork.form_capture import _digest

        record["sha256"] = _digest({key: value for key, value in record.items() if key != "sha256"})
    else:
        record["form"]["questions"][0]["title"] = "changed"
    atomic_json(file, record)
    with pytest.raises(PermissionDenied, match="过期或校验"):
        authenticated_form_cache(tmp_path, ACCOUNT, URL, PermissionDenied("HTTP 401"))


def test_followup_reads_only_completed_turn_of_exact_recorded_chat(tmp_path, monkeypatch):
    package, _, _, _ = prepared_fixture(tmp_path)
    atomic_json(
        package / "codex-generation.json", {"thread_id": "unit-thread", "turn_id": "original"}
    )
    thread = {
        "id": "unit-thread",
        "cwd": str(package),
        "model": "unit-model",
        "turns": [
            {"id": "original", "status": "completed", "items": []},
            {
                "id": "followup",
                "status": "completed",
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "My actual follow-up"}],
                    },
                    {"type": "agentMessage", "text": "Candidate answer"},
                ],
            },
            {"id": "still-running", "status": "inProgress", "items": []},
        ],
    }
    requests = []

    class Reader:
        def __init__(self, *_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, method, payload):
            requests.append((method, payload))
            assert method == "thread/read"
            return {"thread": thread}

    monkeypatch.setattr(followups, "CodexClient", Reader)
    result = followups.read_followup(package, ["unit-fixture"])
    assert result["turn_id"] == "followup" and result["model"] == "unit-model"
    assert followups.read_followup(package, ["unit-fixture"]) == result
    assert json.loads((package / "codex-generation.json").read_text())["turn_id"] == "original"
    thread["cwd"] = str(tmp_path)
    with pytest.raises(WorkflowError, match="不匹配"):
        followups.read_followup(package, ["unit-fixture"])


def test_missing_actual_questions_produces_actionable_needs_input_not_approved_draft(tmp_path):
    package, _, _, review = prepared_fixture(tmp_path)
    result = {
        "review": review,
        "draft": "candidate",
        "requirements_complete": False,
        "missing_sources": ["HTTP 401"],
        "used_evidence_ids": [],
    }
    generation = {"model": "unit-model", "turn_status": "completed", "source_files_sha256": {}}
    with pytest.raises(NeedsInput, match="题目尚未完整读取"):
        drafting.accept_model_result(package, generation, result, json.dumps(result))
    assert not (package / "review-receipt.json").exists()
