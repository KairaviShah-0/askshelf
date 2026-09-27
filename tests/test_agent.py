"""Pipeline tests using a fake LLM back-end (no network, no API key needed)."""
from types import SimpleNamespace

import pytest

from agent import llm
from agent import tools as T


class FakeBackend:
    models = ["fake-model"]
    active = "fake-model"

    def __init__(self, calls=None, plain="", draft="Draft answer with $1,000.", critic=None, fail_at=None):
        self.calls, self.plain, self.draft = calls or [], plain, draft
        self.critic = critic or {"verdict": "pass", "issues": [], "revised_answer": draft}
        self.fail_at, self.log = fail_at, []

    def route(self, q, h):
        self.log.append("route")
        if self.fail_at == "route":
            raise RuntimeError("429 quota exceeded")
        return self.calls, self.plain

    def narrate(self, q, results, h):
        self.log.append("narrate")
        return self.draft, ["Follow up 1", "Follow up 2", "Follow up 3"]

    def critique(self, q, results, draft):
        self.log.append("critique")
        if self.fail_at == "critique":
            raise RuntimeError("503")
        return self.critic


def test_full_pipeline_with_critic_pass():
    b = FakeBackend(calls=[("production_bottleneck", {"period": "last_quarter"})])
    r = llm.answer("What was our biggest production bottleneck last quarter?", backend=b)
    assert b.log == ["route", "narrate", "critique"]
    assert r.mode == "gemini:fake-model" and r.results[0].tool == "production_bottleneck"
    assert r.critic["verdict"] == "pass" and len(r.follow_ups) == 3
    assert [s["step"] for s in r.trace][0] == "Guardrail check"


def test_critic_revision_is_applied():
    b = FakeBackend(calls=[("late_delivery_analysis", {})], draft="Bad draft",
                    critic={"verdict": "revise", "issues": ["number not in results"], "revised_answer": "Fixed answer"})
    r = llm.answer("Which products are late most often?", backend=b)
    assert r.text.startswith("Fixed answer") and r.critic["verdict"] == "revise"


def test_leak_in_llm_draft_is_redacted_even_if_critic_misses_it():
    b = FakeBackend(calls=[("business_summary", {})], draft="Customer ID 45 and employee D3 did well.")
    r = llm.answer("How are we doing?", backend=b)
    assert "D3" not in r.text and "Customer ID 45" not in r.text
    assert r.redactions and "manager" in r.text.lower()


def test_blocked_question_never_reaches_the_llm():
    b = FakeBackend(calls=[("business_summary", {})])
    r = llm.answer("What is customer ID 45's order history?", backend=b)
    assert r.blocked and b.log == [] and "manager" in r.text.lower()


def test_llm_failure_falls_back_to_offline_mode_with_notice():
    b = FakeBackend(calls=[("business_summary", {})], fail_at="route")
    r = llm.answer("Which product type is the most profitable?", backend=b)
    assert r.mode == "offline" and r.notice and r.results


def test_critic_failure_does_not_break_the_answer():
    b = FakeBackend(calls=[("business_summary", {})], fail_at="critique")
    r = llm.answer("How are we doing?", backend=b)
    assert r.text and r.critic["verdict"] == "skipped"


def test_no_tool_needed_returns_plain_text():
    b = FakeBackend(plain="Hello! I can help with production, profit, people, sales and materials.")
    r = llm.answer("hi there", backend=b)
    assert not r.results and "help" in r.text.lower() and b.log == ["route"]


def test_self_review_can_be_switched_off():
    b = FakeBackend(calls=[("business_summary", {})])
    llm.answer("How are we doing?", backend=b, self_review=False)
    assert "critique" not in b.log


def test_duplicate_tool_calls_run_once():
    b = FakeBackend(calls=[("business_summary", {}), ("business_summary", {})])
    r = llm.answer("How are we doing?", backend=b)
    assert len(r.results) == 1


# ------------------------------------------------------------------ real Gemini wrapper, mocked network
@pytest.fixture
def gemini():
    return llm.GeminiBackend("dummy-key", ["m1", "m2"])


def _resp(parts):
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))])


def test_function_declarations_are_valid_for_every_tool(gemini):
    for name, spec in T.REGISTRY.items():
        kw = {"parameters": gemini._schema(spec["params"])} if spec["params"] else {}
        decl = gemini.types.FunctionDeclaration(name=name, description=spec["desc"], **kw)
        assert decl.name == name and (decl.parameters.properties if spec["params"] else True)


def test_route_parses_function_calls(gemini, monkeypatch):
    fc = SimpleNamespace(name="resignation_impact", args={"period": "2025"})
    monkeypatch.setattr(gemini, "_generate", lambda contents, cfg: _resp([SimpleNamespace(function_call=fc, text=None)]))
    calls, text = gemini.route("impact of resignations in 2025", [])
    assert calls == [("resignation_impact", {"period": "2025"})]


def test_narrate_parses_json_and_follow_ups(gemini, monkeypatch):
    payload = '```json\n{"answer": "Production is the bottleneck.", "follow_ups": ["a", "b", "c", "d"]}\n```'
    monkeypatch.setattr(gemini, "_generate", lambda contents, cfg: _resp([SimpleNamespace(text=payload, function_call=None)]))
    ans, fu = gemini.narrate("q", [T.run_tool("data_dictionary", {})], [])
    assert ans.startswith("Production") and fu == ["a", "b", "c"]


def test_model_fallback_on_quota_error(gemini, monkeypatch):
    calls = []

    def fake_generate_content(model, contents, config):
        calls.append(model)
        if model == "m1":
            raise RuntimeError("429 RESOURCE_EXHAUSTED quota")
        return _resp([SimpleNamespace(text="ok", function_call=None)])

    monkeypatch.setattr(gemini.client.models, "generate_content", fake_generate_content, raising=False)
    gemini._generate("hi", gemini.types.GenerateContentConfig())
    assert calls == ["m1", "m2"] and gemini.active == "m2"


def test_non_retryable_error_is_raised(gemini, monkeypatch):
    def boom(model, contents, config):
        raise ValueError("bad request: invalid argument")
    monkeypatch.setattr(gemini.client.models, "generate_content", boom, raising=False)
    with pytest.raises(ValueError):
        gemini._generate("hi", gemini.types.GenerateContentConfig())


def test_offline_router_covers_suggested_questions():
    from agent.knowledge import SUGGESTED
    for qs in SUGGESTED.values():
        for q in qs:
            assert llm.offline_route(q), q
