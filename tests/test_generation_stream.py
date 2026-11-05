import hashlib
import json
import sys
from pathlib import Path

import pytest
from test_frontend import offline_rpc
from test_openai_compat import Endpoint, chunks, reply_json, reply_stream
from test_workflow import prepared_fixture

from classroomautowork import claude_agent, drafting, providers
from classroomautowork.errors import ConfigurationError, RequestFailed, WorkflowError
from classroomautowork.local import atomic_json, sha256_file

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


# ---------------------------------------------------------------- the OpenAI-compatible provider

OPENAI_KEY = "sk-endpoint-" + "g" * 32 + "LAST"


def schema_result(package, review):
    """The fixture's review, completed to the full output schema the endpoint is held to."""
    return {
        "review": {
            "requirement_checks": [
                {**check, "draft_location": "全文"} for check in review["requirement_checks"]
            ],
            "claim_checks": [
                {**claim, "personal_fact_indices": []} for claim in review["claim_checks"]
            ],
            "questions": [],
            "ai_policy_checked": True,
            "policy_notes": "",
            "suggested_disclosure": "",
        },
        "draft": (package / "draft.md").read_text(encoding="utf-8"),
        "summary": "Offline OpenAI-compatible result",
        "requirements_complete": True,
        "missing_sources": [],
        "used_evidence_ids": review["claim_checks"][0]["evidence_ids"],
        "document_answers": [],
        "form_answers": [],
    }


def openai_package(tmp_path, monkeypatch, endpoint):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(
        tmp_path, monkeypatch, package, review
    )  # the offline skill; its Claude stub stays unused
    monkeypatch.setattr(
        claude_agent,
        "claude_command",
        lambda: pytest.fail("The OpenAI-compatible job must not start Claude"),
    )
    providers.save_openai(
        tmp_path,
        {"base_url": endpoint.base, "api_key": OPENAI_KEY, "models": "unit-model, second-model"},
        verify=lambda *_: {"listed": True, "ids": ["unit-model"], "status": 200},
    )
    return package, schema_result(package, review)


def test_an_openai_compatible_answer_is_recorded_without_the_key(tmp_path, monkeypatch):
    endpoint = Endpoint()
    try:
        package, result = openai_package(tmp_path, monkeypatch, endpoint)
        usage = {
            "prompt_tokens": 900,
            "completion_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 100},
        }
        endpoint.replies.append(
            reply_stream(
                chunks(json.dumps(result, ensure_ascii=False), model="unit-model-0601", usage=usage)
            )
        )
        messages = []
        receipt = drafting.generate_review(
            package,
            ai_confirmed=True,
            model="unit-model",
            effort="low",
            provider="openai",
            progress=messages.append,
        )
        assert receipt["status"] == "ready_for_human_review" and receipt["draft_sha256"]
        variant = Path(receipt["package"])
        generation = json.loads((variant / "generation.json").read_text(encoding="utf-8"))
        host = endpoint.base.split("/")[2]
        assert generation["provider"] == "openai_compatible" and generation["host"] == host
        assert (
            generation["requested_model"] == "unit-model"
            and generation["model"] == "unit-model-0601"
        )
        assert (
            generation["reasoning_effort"] == "low"
            and generation["api_key_source"] == "openai_compatible"
        )
        assert (
            generation["structured_output_mode"] == "json_schema"
            and generation["degradations"] == []
        )
        assert generation["images_sent"] == 0 and generation["images_dropped"] == 0
        assert generation["token_usage"] == {
            "input_tokens": 800,
            "output_tokens": 120,
            "cache_read_input_tokens": 100,
        }
        assert generation["cost_usd"] is None and isinstance(generation["duration_ms"], int)
        assert generation["status"] == "completed" and generation["thread_id"] == "chatcmpl-unit"
        assert any(
            f"已向 OpenAI 兼容接口（{host}）发起请求（unit-model）" in str(x) for x in messages
        )
        body = endpoint.requests[0]["body"]
        assert body["model"] == "unit-model" and body["reasoning_effort"] == "low"
        schema = body["response_format"]["json_schema"]["schema"]
        assert schema == drafting.output_schema(
            json.loads((package / "manifest.json").read_text())["requirement_source_ids"]
        )
        for path in tmp_path.rglob("*"):
            if path.is_file() and path.name != providers.OPENAI_FILE:
                data = path.read_bytes()
                assert OPENAI_KEY.encode() not in data and OPENAI_KEY[:-4].encode() not in data
        # The same input, model and endpoint: reused without another request.
        again = drafting.generate_review(
            package, ai_confirmed=True, model="unit-model", effort="low", provider="openai"
        )
        assert again["reused"] and len(endpoint.requests) == 1
        # Claude with the same model name never reuses the endpoint's answer, and the reverse.
        assert (
            drafting.equivalent_review(
                package,
                json.loads((variant / "manifest.json").read_text()),
                model="unit-model",
                effort="low",
            )
            is None
        )
    finally:
        endpoint.close()


def test_an_openai_job_without_settings_stops_before_any_request(tmp_path, monkeypatch):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    with pytest.raises(ConfigurationError, match="设置已经删除.*重试未完成"):
        drafting.generate_review(package, ai_confirmed=True, model="unit-model", provider="openai")
    endpoint = Endpoint()
    try:
        openai_package(tmp_path, monkeypatch, endpoint)
        for model, effort, words in [
            ("bad model", None, "OpenAI 兼容接口的模型"),
            ("unit-model", "minimal", "推理强度"),
        ]:
            with pytest.raises(WorkflowError, match=words):
                drafting.generate_review(
                    package, ai_confirmed=True, model=model, effort=effort, provider="openai"
                )
        assert not endpoint.requests
    finally:
        endpoint.close()


def test_a_failed_validation_of_an_openai_answer_recovers_by_the_requested_model(
    tmp_path, monkeypatch
):
    endpoint = Endpoint()
    try:
        package, result = openai_package(tmp_path, monkeypatch, endpoint)
        endpoint.replies.append(reply_stream(chunks(json.dumps(result), model="unit-model-0601")))
        monkeypatch.setattr(drafting, "OUTPUT_CHARACTERS", 40)
        with pytest.raises(WorkflowError, match="字符"):
            drafting.generate_review(
                package, ai_confirmed=True, model="unit-model", provider="openai"
            )
        monkeypatch.setattr(drafting, "OUTPUT_CHARACTERS", 64_000)
        variant = next(package.parent.glob("*/generation.json")).parent
        generation = json.loads((variant / "generation.json").read_text())
        # A turn that completed but failed local checks: its bytes are kept for recovery.
        (variant / "model-result.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
        generation.update(
            result_file="model-result.json",
            result_sha256=sha256_file(variant / "model-result.json"),
        )
        atomic_json(variant / "generation.json", generation)
        assert generation["status"] == "failed" and generation["model"] == "unit-model-0601"
        recovered = drafting.recover_completed_result(variant, model="unit-model", effort=None)
        assert recovered and recovered["generation"]["status"] == "completed"
        assert (
            drafting.recover_completed_result(
                variant, model="unit-model", effort=None, provider="anthropic"
            )
            is None
        )
        assert len(endpoint.requests) == 1
    finally:
        endpoint.close()


def test_the_claude_record_is_unchanged(tmp_path, monkeypatch):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    receipt = drafting.generate_review(
        package, ai_confirmed=True, model="unit-model", effort="high"
    )
    generation = receipt["generation"]
    assert generation["provider"] == "anthropic" and "host" not in generation
    assert "structured_output_mode" not in generation
    # Claude's cache identity is the one it always had, so earlier results stay reusable.
    manifest = json.loads((Path(receipt["package"]) / "manifest.json").read_text(encoding="utf-8"))
    identity = hashlib.sha256(
        json.dumps(
            [
                manifest["fingerprint"],
                "unit-model",
                "high",
                "claude-cli-v1",
                True,
                manifest["generation_context"],
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()[:24]
    assert Path(receipt["package"]).name == identity


# ---------------------------------------------------------------- after review (2026-11-05)


def test_a_failed_request_is_not_repaired_but_an_answer_that_does_not_fit_is(tmp_path, monkeypatch):
    endpoint = Endpoint()
    try:
        package, result = openai_package(tmp_path, monkeypatch, endpoint)
        endpoint.replies += [reply_json({"error": {"message": "Rate limit reached"}}, 429)] * 2
        messages = []
        options = dict(ai_confirmed=True, model="unit-model", provider="openai")
        with pytest.raises(RequestFailed, match="限流"):
            drafting.generate_answer(package, progress=messages.append, **options)
        # One request, and no claim that the first answer was incomplete.
        assert len(endpoint.requests) == 1
        assert not any("自动修复" in str(x) for x in messages)
        endpoint.requests.clear()
        endpoint.replies.clear()
        wrong = json.loads(json.dumps(result))
        wrong["review"]["requirement_checks"][0]["status"] = "done"
        endpoint.replies += [
            reply_stream(chunks(json.dumps(wrong, ensure_ascii=False))),
            reply_stream(chunks(json.dumps(result, ensure_ascii=False))),
        ]
        receipt = drafting.generate_answer(package, progress=messages.append, **options)
        assert receipt["status"] == "ready_for_human_review" and len(endpoint.requests) == 2
        assert any("自动修复" in str(x) for x in messages)
    finally:
        endpoint.close()


def test_an_endpoint_turn_is_told_which_model_answers(tmp_path, monkeypatch):
    endpoint = Endpoint()
    try:
        package, result = openai_package(tmp_path, monkeypatch, endpoint)
        endpoint.replies.append(reply_stream(chunks(json.dumps(result, ensure_ascii=False))))
        drafting.generate_review(
            package, ai_confirmed=True, model="second-model", provider="openai"
        )
        content = endpoint.requests[0]["body"]["messages"][0]["content"]
        prompt = content if isinstance(content, str) else content[0]["text"]
        line = drafting.answering_line("second-model", endpoint.base.split("/")[2])
        assert prompt.split("\n")[1] + "\n" == line
        assert "不要写 Claude" in line and "suggested_disclosure" in line
    finally:
        endpoint.close()


def test_the_claude_prompt_has_no_model_line(tmp_path, monkeypatch):
    package, _, _, review = prepared_fixture(tmp_path)
    offline_rpc(tmp_path, monkeypatch, package, review)
    drafting.generate_review(package, ai_confirmed=True, model="unit-model", effort="high")
    sent = json.loads((tmp_path / "requests.jsonl").read_text().splitlines()[0])["params"]["input"]
    prompt = sent[0]["text"]
    assert prompt.split("\n")[1].startswith("本次模式：") and "本次作答的模型是" not in prompt
