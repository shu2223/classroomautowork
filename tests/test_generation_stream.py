import json

import pytest
from test_frontend import offline_rpc
from test_workflow import prepared_fixture

from classroomautowork import drafting, generation_stream
from classroomautowork.errors import WorkflowError
from classroomautowork.generation_stream import GenerationStream


def test_silent_turn_and_endless_deltas_have_independent_deadlines():
    now = [0]
    stream = GenerationStream(clock=lambda: now[0])
    now[0] = 901
    with pytest.raises(WorkflowError, match="15 分钟"):
        stream.remaining()
    assert stream.remaining(approval_wait=100) == 99
    stream = GenerationStream(clock=lambda: now[0])
    stream.start_item({"type": "agentMessage", "id": "answer", "phase": "final_answer"})
    stream.append({"itemId": "answer", "delta": "Actual incomplete answer"})
    now[0] += 301
    stream.append({"itemId": "answer", "delta": " still streaming"})
    with pytest.raises(WorkflowError, match="5 分钟"):
        stream.remaining()
    assert stream.metadata()["validated"] is False


def test_commentary_does_not_start_final_output_deadline_and_buffers_are_bounded(monkeypatch):
    now = [0]
    stream = GenerationStream(clock=lambda: now[0])
    stream.start_item({"type": "agentMessage", "phase": "commentary"})
    stream.append({"delta": "Actual progress"})
    now[0] = 400
    assert stream.remaining() == 500
    assert stream.metadata()["output_started_at"] is None
    monkeypatch.setattr(generation_stream, "OUTPUT_CHARACTERS", 25)
    stream.start_item({"type": "agentMessage", "id": "answer", "phase": "final_answer"})
    with pytest.raises(WorkflowError, match="字符"):
        stream.append({"itemId": "answer", "delta": "x" * 50})
    assert len(stream.text) == 25


def test_overlong_real_protocol_stream_is_private_partial_not_validated_result(
    tmp_path, monkeypatch
):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    script = tmp_path / "offline_rpc.py"
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            " if m=='turn/start':\n",
            " if m=='turn/start':\n"
            "  send({'method':'item/started','params':{'threadId':'unit-thread','item':{'type':'agentMessage','id':'stream','phase':'final_answer'}}})\n"
            "  send({'method':'item/agentMessage/delta','params':{'threadId':'unit-thread','itemId':'stream','delta':'incomplete'*10}})\n",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(generation_stream, "OUTPUT_CHARACTERS", 40)
    with pytest.raises(WorkflowError, match="字符"):
        drafting.generate_review(package, ai_confirmed=True, model="unit-model", effort="high")
    partial = list(package.parent.glob("*/codex-output.partial.txt"))
    assert len(partial) == 1 and len(partial[0].read_text(encoding="utf-8")) == 40
    variant = partial[0].parent
    record = json.loads((variant / "codex-generation.json").read_text(encoding="utf-8"))
    assert record["status"] == "failed" and record.get("turn_status") != "completed"
    assert record["output_stream"]["validated"] is False
    assert not (variant / "review-receipt.json").exists()


def test_compact_input_preserves_actual_required_sources_and_local_full_index(tmp_path):
    package, _, _, _ = prepared_fixture(tmp_path)
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    manifest["evidence_ids"] = ["unused-id"] * 10_000
    skill = tmp_path / "skill"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("Offline skill", encoding="utf-8")
    (skill / "references/review-format.md").write_text("Offline format", encoding="utf-8")
    inputs, receipt = drafting.build_input(package, skill, manifest)
    assert len(inputs[0]["text"]) < 25_000
    assert set(manifest["requirement_source_ids"]) <= set(receipt["source_ids"])
    assert receipt["prompt_characters"] == len(inputs[0]["text"])
    assert receipt["evidence_character_budget"] == 60_000
    assert "unused-id" not in inputs[0]["text"]
    assert (package / "evidence.json").is_file()


def test_silent_protocol_turn_times_out_without_claiming_success(tmp_path, monkeypatch):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    script = tmp_path / "offline_rpc.py"
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            " if m=='turn/start':\n", " if m=='turn/start':\n  import time; time.sleep(5)\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(generation_stream, "TURN_SECONDS", 0.05)
    with pytest.raises(WorkflowError, match="异常等待"):
        drafting.generate_review(package, ai_confirmed=True, model="unit-model", effort="high")
    records = list(package.parent.glob("*/codex-generation.json"))
    assert len(records) == 1
    generation = json.loads(records[0].read_text(encoding="utf-8"))
    assert generation["status"] == "failed" and generation.get("turn_status") != "completed"
    assert not (records[0].parent / "review-receipt.json").exists()
