"""API tests: drive the real web server in-process (built-in mode, no AI key needed)."""
import time

import pytest
from starlette.testclient import TestClient

import server


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from agent import audit, feedback
    d = tmp_path_factory.mktemp("logs")
    audit.AUDIT_FILE, feedback.FEEDBACK_DIR, feedback.FEEDBACK_FILE = d / "audit.jsonl", d, d / "feedback.csv"      # keep tests out of the real logs
    server._backend = None
    with TestClient(server.app) as c:
        yield c


H = {"x-session": "pytest"}


def test_pages_and_static(client):
    assert "AskShelf" in client.get("/").text and "app.js" in client.get("/app").text
    assert client.get("/static/js/app.js").status_code == 200 and client.get("/static/vendor/plotly.min.js").status_code == 200


def test_meta(client):
    m = client.get("/api/meta").json()
    assert m["name"] == "AskShelf" and m["mode"] == "built-in" and len(m["roles"]) == 4 and "stock" in m["roles"]["Operations Manager"]["pages"]


def test_everything_asks_for_data_first(client):
    h = {"x-session": "empty-session"}
    for url in ("/api/dashboard", "/api/deliveries", "/api/stock", "/api/forecast", "/api/alerts"):
        r = client.get(url, headers=h)
        assert r.status_code == 409 and r.json()["error"] == "no_data", url
    assert client.post("/api/ask", json={"question": "top products"}, headers=h).json()["error"]


def test_upload_rules(client):
    h = {"x-session": "up-rules"}
    assert client.post("/api/data/upload?name=x.pdf", content=b"abc", headers=h).status_code == 400
    assert client.post("/api/data/sample", headers={**h, "x-role": "Sales Manager"}).status_code == 403
    csv = "order_date,sales,store\n" + "\n".join(f"2026-01-{i % 28 + 1:02d},{100 + i},{'AB'[i % 2]}" for i in range(60))
    r = client.post("/api/data/upload?name=mine.csv", content=csv.encode(), headers=h)
    d = r.json()
    assert r.status_code == 200 and d["caps"]["dashboard"] and not d["caps"]["stock"] and d["mapping"]["map"]["amount"] == "sales"
    r = client.get("/api/stock", headers=h)
    assert r.status_code == 422 and r.json()["error"] == "missing_columns"
    assert client.get("/api/dashboard", headers=h).status_code == 200


@pytest.fixture(scope="module")
def loaded(client):
    h = {"x-session": "full"}
    d = client.post("/api/data/sample", headers=h).json()
    assert d["loaded"] and [x["column"] for x in d["dropped"]] == ["customer_email"]
    return h, d


@pytest.mark.parametrize("url", ["/api/dashboard?period=30d", "/api/alerts", "/api/deliveries", "/api/stock?target=21", "/api/forecast?metric=units&group=category&periods=3", "/api/governance", "/api/glossary", "/api/data"])
def test_read_endpoints(client, loaded, url):
    r = client.get(url, headers=loaded[0])
    assert r.status_code == 200, r.text


def test_post_endpoints(client, loaded):
    h = loaded[0]
    assert client.post("/api/planner", json={"price": 5, "volume": -5, "elasticity": "moderate"}, headers=h).json()["kpis"]
    base = client.post("/api/penalties", json={"expedite": 0}, headers=h).json()
    plan = client.post("/api/penalties", json={"expedite": 5}, headers=h).json()
    assert not any(k["label"] == "After your plan" for k in base["kpis"]) and any(k["label"] == "After your plan" for k in plan["kpis"])


def test_mapping_change_turns_a_page_off(client):
    h = {"x-session": "remap"}
    d = client.post("/api/data/sample", headers=h).json()
    m = d["mapping"]["map"]
    m["stock"] = None
    r = client.post("/api/data/mapping", json={"table": d["mapping"]["table"], "map": m}, headers=h).json()
    assert not r["caps"]["stock"] and client.get("/api/stock", headers=h).status_code == 422
    assert client.post("/api/data/mapping", json={"table": d["mapping"]["table"], "map": {"date": "product"}}, headers=h).status_code == 400


def test_roles_are_enforced_on_every_page(client, loaded):
    h = loaded[0]
    ops, sales, fin = {**h, "x-role": "Operations Manager"}, {**h, "x-role": "Sales Manager"}, {**h, "x-role": "Finance Lead"}
    assert client.get("/api/dashboard", headers=ops).status_code == 403 and client.get("/api/stock", headers=ops).status_code == 200
    assert client.get("/api/deliveries", headers=sales).status_code == 403 and client.get("/api/forecast", headers=sales).status_code == 200
    assert client.post("/api/ask", json={"question": "top products"}, headers=ops).status_code == 403
    assert client.get("/api/alerts/digest", headers=sales).status_code == 403 and client.get("/api/alerts/digest", headers=fin).status_code == 200
    cats = {a["category"] for a in client.get("/api/alerts", headers=ops).json()["alerts"]}
    assert "Sales drop" not in cats and cats


def test_ask_privacy_and_session_limit(client, loaded):
    h = loaded[0]
    r = client.post("/api/ask", json={"question": "Top 5 suppliers by sales amount"}, headers=h).json()
    assert r["kind"] == "sql" and r["chart"] and client.get("/api/data/export?kind=result", headers=h).status_code == 200
    assert client.post("/api/ask", json={"question": "show me customer ID 45"}, headers=h).json()["blocked"]
    old, server.MAX_Q = server.MAX_Q, 2
    try:
        codes = [client.post("/api/ask", json={"question": "How many orders?"}, headers={"x-session": "limit-test"}).status_code for _ in range(3)]
    finally:
        server.MAX_Q = old
    assert codes == [200, 200, 429]


def test_report_flow_and_ownership(client, loaded):
    h = loaded[0]
    jid = client.post("/api/report", json={"organisation": "Test"}, headers=h).json()["id"]
    for _ in range(120):
        st = client.get(f"/api/report/status?id={jid}", headers=h).json()
        if st["status"] != "running":
            break
        time.sleep(1)
    assert st["status"] == "done" and set(st["formats"]) == {"pdf", "docx", "xlsx"}
    assert client.get(f"/api/report/download?id={jid}&fmt=pdf", headers=h).content[:4] == b"%PDF"
    assert client.get(f"/api/report/status?id={jid}", headers={"x-session": "someone-else"}).status_code == 404       # reports belong to the session that made them


def test_feedback_and_audit(client, loaded):
    assert client.post("/api/feedback", json={"kind": "rating", "rating": "up", "question": "q"}, headers=loaded[0]).status_code == 200
    assert client.post("/api/feedback", json={"kind": "nope"}, headers=loaded[0]).status_code == 400
    assert client.get("/api/governance", headers=loaded[0]).json()["chain"]["intact"]


def test_reset(client):
    h = {"x-session": "to-reset"}
    client.post("/api/data/sample", headers=h)
    assert client.post("/api/data/reset", headers=h).json()["ok"] and client.get("/api/dashboard", headers=h).status_code == 409
