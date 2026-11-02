import json
import sys

import pytest
from test_frontend import offline_rpc
from test_workflow import prepared_fixture

from classroomautowork import claude_agent, drafting
from classroomautowork.errors import WorkflowError

INIT = {
    "type": "system",
    "subtype": "init",
    "model": "unit-model",
    "apiKeySource": "ANTHROPIC_API_KEY",
    "tools": [],
}


def test_overlong_result_is_kept_private_and_not_validated(tmp_path, monkeypatch):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    monkeypatch.setattr(drafting, "OUTPUT_CHARACTERS", 40)
    with pytest.raises(WorkflowError, match="字符"):
        drafting.generate_review(package, ai_confirmed=True, model="unit-model", effort="high")
    records = list(package.parent.glob("*/generation.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert not (records[0].parent / "review-receipt.json").exists()


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
    assert receipt["evidence_character_budget"] == drafting.EVIDENCE_CHARACTERS
    assert "unused-id" not in inputs[0]["text"]
    assert (package / "evidence.json").is_file()


def test_silent_turn_times_out_without_claiming_success(tmp_path, monkeypatch):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    script = tmp_path / "silent.py"
    script.write_text("import sys,time\nsys.stdin.readline()\ntime.sleep(5)\n", encoding="utf-8")
    monkeypatch.setattr(claude_agent, "claude_command", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(claude_agent, "IDLE_SECONDS", 0.05)
    with pytest.raises(WorkflowError, match="没有任何输出"):
        drafting.generate_review(package, ai_confirmed=True, model="unit-model", effort="high")
    records = list(package.parent.glob("*/generation.json"))
    assert len(records) == 1
    generation = json.loads(records[0].read_text(encoding="utf-8"))
    assert generation["status"] == "failed" and generation.get("turn_status") != "completed"
    assert not (records[0].parent / "review-receipt.json").exists()


@pytest.mark.parametrize(
    "events, message",
    [
        ([{"type": "system", "subtype": "init", "model": "unit-model", "tools": []}], "API 密钥"),
        (
            [{**INIT, "apiKeySource": "none"}],
            "API 密钥",
        ),
        ([{**INIT, "model": "other-model"}], "不一致"),
        ([{**INIT, "tools": ["Bash"]}], "额外工具"),
        (
            [
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "result": "Invalid API key",
                }
            ],
            "Invalid API key",
        ),
        (
            [
                {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}},
                {"type": "result", "subtype": "error_during_execution", "is_error": True},
            ],
            "用量已达上限",
        ),
        ([{"type": "result", "subtype": "success", "is_error": False}], "结构化结果"),
    ],
)
def test_unexpected_cli_events_never_count_as_an_answer(tmp_path, events, message):
    script = tmp_path / "events.py"
    script.write_text(
        "import json,sys\nsys.stdin.readline()\n"
        f"for event in {events!r}: print(json.dumps(event), flush=True)\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowError, match=message):
        claude_agent.run_turn(
            [{"type": "text", "text": "unit"}],
            {"type": "object"},
            model="unit-model",
            effort=None,
            cwd=tmp_path,
            command=[sys.executable, str(script)],
        )


def test_saved_key_reaches_the_cli_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    script = tmp_path / "env.py"
    script.write_text(
        "import json,os,sys\nsys.stdin.readline()\n"
        "key=os.environ.get('ANTHROPIC_API_KEY','')\n"
        "print(json.dumps({'type':'system','subtype':'init','model':'unit-model',"
        "'apiKeySource':'ANTHROPIC_API_KEY' if key else 'none','tools':[]}),flush=True)\n"
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
        "'structured_output':{'key_tail':key[-4:]}}),flush=True)\n",
        encoding="utf-8",
    )
    options = dict(
        model="unit-model", effort=None, cwd=tmp_path, command=[sys.executable, str(script)]
    )
    with pytest.raises(WorkflowError, match="API 密钥"):
        claude_agent.run_turn([{"type": "text", "text": "unit"}], {"type": "object"}, **options)
    outcome = claude_agent.run_turn(
        [{"type": "text", "text": "unit"}],
        {"type": "object"},
        api_key="sk-ant-unit-1234",
        **options,
    )
    assert outcome["output"] == {"key_tail": "1234"}
    assert outcome["api_key_source"] == "ANTHROPIC_API_KEY"
