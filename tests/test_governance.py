import json

from agent import audit, guardrails as G


def test_audit_chain_detects_tampering(tmp_path):
    p = tmp_path / "a.jsonl"
    for q in ("one", "two", "three"):
        audit.log(session="s", role="r", question=q, outcome="answered", tools=[], mode="rules", latency_ms=1, est_cost_usd=0, path=p)
    assert audit.verify(p) == (True, 3, None)
    p.write_text(p.read_text().replace("two", "TWO"))
    ok, _, bad = audit.verify(p)
    assert not ok and bad == 2


def test_audit_masks_pii(tmp_path):
    p = tmp_path / "a.jsonl"
    e = audit.log(session="s", role="r", question="email sam@x.com", outcome="answered", tools=[], mode="rules", latency_ms=1, est_cost_usd=0, path=p)
    assert "sam@x.com" not in json.dumps(e)


def test_mask_pii():
    out, c = G.mask_pii("call 0412 345 678 or sam@x.com, card 4111 1111 1111 1111")
    assert "sam@x.com" not in out and "4111" not in out and len(c) >= 3
