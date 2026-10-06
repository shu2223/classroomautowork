"""Offline question editor/overlay checks; never counted as real school answers."""

import json
from urllib.parse import parse_qs, urlparse

import pytest
from test_form_fill import answer, fill_fixture
from test_frontend import offline_rpc, unit_settings
from test_workflow import prepared_fixture

from classroomautowork import drafting, user_answers
from classroomautowork.errors import WorkflowError
from classroomautowork.local import atomic_json
from classroomautowork.student import StudentProfile
from classroomautowork.ui_jobs import FrontendJobs


def input_value(form, entry="202", values=None):
    return {
        k: v
        for k, v in answer(form, entry, values).items()
        if k not in {"needs_user", "review_note"}
    }


def test_user_answers_override_candidates_preserve_model_bytes_and_reopen_only_on_change(
    tmp_path, monkeypatch
):
    package, settings, form, _ = fill_fixture(tmp_path, monkeypatch)
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    original = (package / "form-answers.json").read_bytes()
    opened = []

    def fill():
        from classroomautowork.form_fill import fill_response_forms

        return fill_response_forms(
            settings, package, opener=lambda url, **kw: opened.append(url) or True
        )

    fill()
    values = [input_value(form, values=["B"]), input_value(form, "200", ["Actual user entry"])]
    first = user_answers.save_user_answers(tmp_path, manifest, [form], StudentProfile(), values)
    result = fill()
    query = parse_qs(urlparse(result["forms"][0]["url"]).query)
    assert query["entry.202"] == ["B"] and query["entry.200"] == ["Actual user entry"]
    assert result["user_answer_count"] == 2 and result["submission"] == "manual_only"
    assert (package / "form-answers.json").read_bytes() == original
    second = user_answers.save_user_answers(tmp_path, manifest, [form], StudentProfile(), values)
    assert second["sha256"] == first["sha256"] and fill()["reused"] and len(opened) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"values": ["Invented option"]},
        {"entry_id": "999"},
        {"context_sha256": "stale"},
        {"form_url": "https://evil.invalid"},
        {"submit": True},
    ],
)
def test_input_rejects_unknown_destinations_options_and_permissions(tmp_path, monkeypatch, change):
    package, _, form, _ = fill_fixture(tmp_path, monkeypatch)
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    with pytest.raises(WorkflowError):
        user_answers.save_user_answers(
            tmp_path, manifest, [form], StudentProfile(), [{**input_value(form), **change}]
        )
    assert not (tmp_path / "user-form-answers/1/2.json").exists()


def test_saving_inputs_during_active_work_preserves_generation_and_does_not_start_ai(
    tmp_path, monkeypatch
):
    package, _, form, _ = fill_fixture(tmp_path, monkeypatch)
    jobs = FrontendJobs(unit_settings(tmp_path))
    jobs._active = "unit-active"
    jobs._jobs = {
        "unit-active": {"status": "running", "items": [{"course_id": "1", "assignment_id": "2"}]}
    }
    monkeypatch.setattr(jobs, "package_path", lambda *_: package)
    monkeypatch.setattr(
        jobs, "fill_existing", lambda *_: pytest.fail("Don't interrupt active work")
    )
    monkeypatch.setattr(
        drafting, "codex_command", lambda: pytest.fail("No AI regeneration for user inputs")
    )
    result = jobs.save_form_inputs(
        "unit-job", "1:2", {"answers": [input_value(form)], "apply": True}
    )
    assert result["processing_active"] and result["answer_count"] == 1 and result["apply_queued"]
    queue_path = jobs.root / "answer-fill-requests.json"
    assert json.loads(queue_path.read_text()) == {"1:2": "unit-active"}
    jobs._active = None
    jobs._jobs["unit-active"]["status"] = "paused"
    assert jobs._next_answer_fill() is None and jobs._answer_requests
    jobs._jobs["unit-active"]["status"] = "completed"
    monkeypatch.setattr(jobs, "fill_existing", lambda *args: {"kind": "fill", "target": args})
    assert jobs._next_answer_fill() == {"kind": "fill", "target": ("unit-active", "1:2")}
    assert json.loads(queue_path.read_text()) == {}


def test_completed_turn_validation_recovery_keeps_actual_model_and_refuses_tampering(
    tmp_path, monkeypatch
):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    receipt = drafting.generate_review(
        package, ai_confirmed=True, model="unit-model", effort="high"
    )
    from pathlib import Path

    variant = Path(receipt["package"])
    raw = (variant / "codex-result.json").read_bytes()
    generation = receipt["generation"]
    generation.update(status="failed", error="Offline local validation failure")
    atomic_json(variant / "codex-generation.json", generation)
    monkeypatch.setattr(drafting, "codex_command", lambda: pytest.fail("No second model call"))
    restored = drafting.generate_review(
        package, ai_confirmed=True, model="unit-model", effort="high"
    )
    assert restored["reused"] and restored["generation"]["turn_id"] == "unit-turn"
    assert (
        restored["generation"]["model"] == "unit-model"
        and (variant / "codex-result.json").read_bytes() == raw
    )
    generation["status"] = "failed"
    atomic_json(variant / "codex-generation.json", generation)
    (variant / "codex-result.json").write_bytes(b"tampered")
    assert drafting.recover_completed_result(variant, model="unit-model", effort="high") is None


def test_explicit_deferred_fill_survives_restart_and_paused_work_stays_paused(
    tmp_path, monkeypatch
):
    jobs = FrontendJobs(unit_settings(tmp_path))
    job_id = "a" * 32
    source = {
        "id": job_id,
        "status": "completed",
        "items": [{"course_id": "1", "assignment_id": "2"}],
    }
    jobs._save(source)
    atomic_json(jobs.root / "answer-fill-requests.json", {"1:2": job_id})
    filled = []
    monkeypatch.setattr(
        FrontendJobs, "fill_existing", lambda self, *args: filled.append(args) or {"kind": "fill"}
    )
    restored = FrontendJobs(unit_settings(tmp_path))
    assert filled == [(job_id, "1:2")] and not restored._answer_requests
    source["status"] = "paused"
    jobs._save(source)
    atomic_json(jobs.root / "answer-fill-requests.json", {"1:2": job_id})
    restored = FrontendJobs(unit_settings(tmp_path))
    assert restored._answer_requests == {"1:2": job_id} and len(filled) == 1
