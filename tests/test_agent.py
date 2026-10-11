import json

import pytest

from rootline.agent import AgentConfig, AgentError, analyze
from rootline.context import LocalContext
from rootline.prefilter import prefilter
from rootline.redact import Redactor
from rootline.result import ANALYSIS_SCHEMA
from rootline.tools import Toolbox

from .conftest import FakeClient, response, text_block, tool_use

FINAL = {
    "summary": "Locale regression in format_total",
    "root_cause": "format_total() dropped the locale argument.",
    "category": "regression",
    "confidence": "high",
    "evidence": [{"source": "log", "reference": "log L89", "excerpt": "AssertionError"}],
    "suggested_fix": "Pass the locale again.",
    "patch": "",
}


@pytest.fixture
def toolbox(tmp_path, fixture_text):
    (tmp_path / "invoicer").mkdir()
    (tmp_path / "invoicer" / "format.py").write_text("def format_total(invoice):\n    return 1\n")
    (tmp_path / ".env").write_text("STRIPE_KEY=sk_live_abcdefghijklmnop1234\n")
    pre = prefilter(fixture_text("gha_pytest.log"))
    ctx = LocalContext(log_path="ci.log", repo_dir=tmp_path)
    return Toolbox(log_lines=pre.lines, context=ctx, redactor=Redactor()), pre


def test_agent_loop_with_tools_and_caching(toolbox):
    tb, pre = toolbox
    client = FakeClient(
        [
            response(
                [
                    text_block("Let me look."),
                    tool_use("t1", "get_file", {"path": "invoicer/format.py"}),
                    tool_use("t2", "search_log", {"pattern": "AssertionError", "context": 0}),
                ],
                "tool_use",
            ),
            response([tool_use("t3", "get_file", {"path": ".env"})], "tool_use"),
            response([text_block(json.dumps(FINAL))], "end_turn"),
        ]
    )
    out = analyze(
        client, AgentConfig(model="claude-sonnet-5-5"), tb, {"mode": "local"}, pre.excerpt
    )

    assert out.analysis.category == "regression"
    assert out.model == "claude-sonnet-5-5"
    assert out.turns == 3
    assert out.usage["input_tokens"] == 300 and out.usage["output_tokens"] == 150
    assert [c["tool"] for c in tb.calls] == ["get_file", "search_log", "get_file"]
    assert tb.calls[2]["is_error"]  # .env is on the denylist

    calls = client.messages.calls
    first = calls[0]
    # structured output + model id + cache breakpoints on tools, system and the log block
    assert first["output_config"]["format"] == {"type": "json_schema", "schema": ANALYSIS_SCHEMA}
    assert first["model"] == "claude-sonnet-5-5"
    assert "cache_control" in first["tools"][-1]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "<log_excerpt>" in first["messages"][0]["content"][0]["text"]
    assert "temperature" not in first and "tool_choice" not in first

    # tool results were sent back, file content numbered; never more than 4 breakpoints
    second = calls[1]["messages"]
    results = second[-1]["content"]
    assert results[0]["tool_use_id"] == "t1" and "    1| def format_total" in results[0]["content"]
    assert results[1]["content"].startswith("2 matches") or "matches" in results[1]["content"]
    third = calls[2]["messages"]
    assert "blocked by Rootline's sensitive-file denylist" in third[-1]["content"][0]["content"]
    assert third[-1]["content"][0]["is_error"] is True
    for call in calls:
        n = sum(1 for t in call["tools"] if "cache_control" in t) + sum(
            1 for s in call["system"] if "cache_control" in s
        )
        for m in call["messages"]:
            if isinstance(m["content"], list):
                n += sum(1 for b in m["content"] if isinstance(b, dict) and "cache_control" in b)
        assert n <= 4


def test_max_turns_forces_final_answer(toolbox):
    tb, pre = toolbox
    client = FakeClient(
        [
            response([tool_use("t1", "get_log_lines", {"start": 1, "end": 5})], "tool_use"),
            response([text_block(json.dumps(FINAL))], "end_turn"),
        ]
    )
    analyze(client, AgentConfig(max_turns=2), tb, {}, pre.excerpt)
    last_user = client.messages.calls[1]["messages"][-1]["content"]
    assert any(b.get("type") == "text" and "Tool budget exhausted" in b["text"] for b in last_user)


def test_escalates_on_low_confidence(toolbox):
    tb, pre = toolbox
    low = dict(FINAL, confidence="low", category="unknown")
    client = FakeClient(
        [
            response([text_block(json.dumps(low))], "end_turn"),
            response([text_block(json.dumps(FINAL))], "end_turn"),
        ]
    )
    cfg = AgentConfig(model="claude-sonnet-5-5", escalation_model="claude-opus-5-5")
    out = analyze(client, cfg, tb, {}, pre.excerpt)
    assert out.models_tried == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert out.model == "claude-opus-5-5"
    assert out.analysis.confidence == "high"
    assert client.messages.calls[1]["model"] == "claude-opus-5-5"


def test_no_escalation_without_config(toolbox):
    tb, pre = toolbox
    client = FakeClient(
        [response([text_block(json.dumps(dict(FINAL, confidence="low")))], "end_turn")]
    )
    out = analyze(client, AgentConfig(), tb, {}, pre.excerpt)
    assert out.models_tried == ["claude-sonnet-5-5"]


def test_refusal_and_bad_json_raise(toolbox):
    tb, pre = toolbox
    with pytest.raises(AgentError, match="declined"):
        analyze(
            FakeClient([response([text_block("")], "refusal")]), AgentConfig(), tb, {}, pre.excerpt
        )
    with pytest.raises(AgentError, match="not valid JSON"):
        analyze(
            FakeClient([response([text_block("not json")], "end_turn")]),
            AgentConfig(),
            tb,
            {},
            pre.excerpt,
        )


def test_effort_and_env_config():
    cfg = AgentConfig.from_env(
        {
            "ROOTLINE_MODEL": "claude-haiku-5-5",
            "ROOTLINE_EFFORT": "medium",
            "ROOTLINE_MAX_TURNS": "4",
        }
    )
    assert (cfg.model, cfg.effort, cfg.max_turns) == ("claude-haiku-5-5", "medium", 4)
    assert AgentConfig.from_env({}).model == "claude-sonnet-5-5"
