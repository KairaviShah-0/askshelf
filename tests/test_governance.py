import json
from agent import alerts, audit, evaluation, guardrails as G, llm, rbac


def test_rbac_denies_and_allows():
    assert not rbac.can("Operations Supervisor", "profit_bridge")
    assert rbac.can("Owner / General Manager", "profit_bridge")
    r = llm.answer("Why did our profit change last quarter?", role="Operations Supervisor")
    assert r.blocked and r.mode == "rbac" and not r.results


def test_audit_chain_detects_tampering(tmp_path):
    p = tmp_path / "a.jsonl"
    for q in ("one", "two", "three"):
        audit.log(session="s", role="r", question=q, outcome="answered", tools=[], mode="offline", latency_ms=1, est_cost_usd=0, path=p)
    assert audit.verify(p) == (True, 3, None)
    p.write_text(p.read_text().replace("two", "TWO"))
    ok, _, bad = audit.verify(p)
    assert not ok and bad == 2


def test_audit_masks_pii(tmp_path):
    p = tmp_path / "a.jsonl"
    e = audit.log(session="s", role="r", question="email sam@x.com", outcome="answered", tools=[], mode="offline", latency_ms=1, est_cost_usd=0, path=p)
    assert "sam@x.com" not in json.dumps(e)


def test_mask_pii():
    out, c = G.mask_pii("call 0412 345 678 or sam@x.com, card 4111 1111 1111 1111")
    assert "sam@x.com" not in out and "4111" not in out and len(c) >= 3


def test_alerts_have_actions():
    al = alerts.scan()
    assert al and all(a.action and a.ask for a in al)


def test_evaluation_meets_targets():
    m = evaluation.run().metrics
    assert m["routing"] >= 0.9 and m["guardrail"] == 1.0 and m["rbac"] == 1.0 and m["false_block"] <= 0.05
