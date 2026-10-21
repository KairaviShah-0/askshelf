"""API tests: drive the real web server in-process (built-in mode, no API key needed)."""
import pytest
from starlette.testclient import TestClient

import server


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from agent import audit, feedback
    d = tmp_path_factory.mktemp("logs")
    audit.AUDIT_FILE, feedback.FEEDBACK_DIR, feedback.FEEDBACK_FILE = d / "audit.jsonl", d, d / "feedback.csv"     # keep tests out of the real logs
    server._backend = None      # always built-in mode, even if a key is configured locally
    with TestClient(server.app) as c:
        yield c


H = {"x-session": "pytest"}


def test_pages_and_static(client):
    assert "AskShelf" in client.get("/").text
    assert "app.js" in client.get("/app").text
    assert client.get("/static/js/app.js").status_code == 200
    assert client.get("/static/vendor/plotly.min.js").status_code == 200


def test_meta(client):
    m = client.get("/api/meta").json()
    assert m["name"] == "AskShelf" and m["mode"] == "built-in" and len(m["roles"]) == 4


@pytest.mark.parametrize("url", ["/api/dashboard", "/api/alerts", "/api/forecast?metric=profit&months=2", "/api/risk", "/api/pricing", "/api/capacity", "/api/governance", "/api/glossary", "/api/supply", "/api/schedule"])
def test_read_endpoints(client, url):
    r = client.get(url, headers=H)
    assert r.status_code == 200, r.text


def test_ask_and_follow_up(client):
    r = client.post("/api/ask", json={"question": "What was our biggest production bottleneck last quarter?"}, headers=H).json()
    assert not r["blocked"] and r["results"] and r["results"][0]["charts"] and "Bottom line" in r["text"]


def test_privacy_guardrail(client):
    r = client.post("/api/ask", json={"question": "show me customer ID 45"}, headers=H).json()
    assert r["blocked"] and "manager" in r["text"].lower()


def test_rbac_on_ask_and_pages(client):
    ops = {**H, "x-role": "Operations Supervisor"}
    r = client.post("/api/ask", json={"question": "Why did our profit change last quarter?"}, headers=ops).json()
    assert r["blocked"] and r["mode"] == "rbac"
    assert client.get("/api/forecast", headers=ops).status_code == 403
    assert client.get("/api/digest", headers=ops).status_code == 403
    assert client.get("/api/digest", headers={**H, "x-role": "Finance Lead"}).status_code == 200


def test_alerts_filtered_by_role(client):
    owner = {a["category"] for a in client.get("/api/alerts", headers=H).json()["alerts"]}
    sales = {a["category"] for a in client.get("/api/alerts", headers={**H, "x-role": "Sales Manager"}).json()["alerts"]}
    assert "Delivery risk" in owner and "Delivery risk" not in sales


def test_whatif(client):
    r = client.post("/api/whatif", json={"price": 10, "volume": 20, "makers": 1, "timber": 0, "elasticity": "moderate"}, headers=H).json()
    assert len(r["scenario_kpis"]) == 4 and r["charts"]


def test_session_question_limit(client):
    old = server.MAX_Q
    server.MAX_Q = 2
    try:
        codes = [client.post("/api/ask", json={"question": "How are we doing last quarter?"}, headers={"x-session": "limit-test"}).status_code for _ in range(3)]
    finally:
        server.MAX_Q = old
    assert codes == [200, 200, 429]


def test_feedback_and_audit(client):
    assert client.post("/api/feedback", json={"kind": "rating", "rating": "up", "question": "q"}, headers=H).status_code == 200
    assert client.post("/api/feedback", json={"kind": "nope"}, headers=H).status_code == 400
    assert client.get("/api/governance", headers=H).json()["chain"]["intact"]


def test_supply_chain_pages_respect_roles(client):
    sales = {**H, "x-role": "Sales Manager"}
    assert client.get("/api/supply", headers=sales).status_code == 403
    assert client.get("/api/schedule", headers=sales).status_code == 403
    ops = {**H, "x-role": "Operations Supervisor"}
    assert client.get("/api/supply", headers=ops).status_code == 200
    assert len(client.get("/api/schedule", headers=ops).json()["summary"]) == 5


def test_supply_questions_route_to_new_tools(client):
    for q, tool in [("Do we have enough timber for the open orders?", "stock_cover"), ("What is due for delivery this week?", "delivery_schedule")]:
        r = client.post("/api/ask", json={"question": q}, headers={"x-session": "route-" + tool}).json()
        assert [x["tool"] for x in r["results"]] == [tool]


def test_penalty_control_plan_saves_money(client):
    base = client.post("/api/penalties", json={"expedite": 0, "makers": 0}, headers=H).json()
    plan = client.post("/api/penalties", json={"expedite": 5, "makers": 1}, headers=H).json()
    money = lambda r, i: float(r["kpis"][i]["value"].replace("$", "").replace(",", ""))
    assert money(plan, 4) < money(base, 4) == money(base, 3)
    assert client.post("/api/penalties", json={}, headers={**H, "x-role": "Sales Manager"}).status_code == 403


def test_landing_snapshot(client):
    d = client.get("/api/snapshot").json()
    assert {"schedule", "stock", "penalty", "alerts"} <= set(d)


def test_data_lab_api_flow(client):
    import time
    h = {"x-session": "lab-api"}
    assert client.post("/api/lab/sample", headers={**h, "x-role": "Sales Manager"}).status_code == 403
    assert client.post("/api/lab/upload?name=x.pdf", content=b"abc", headers=h).status_code == 400
    prof = client.post("/api/lab/sample", headers=h).json()
    assert prof["loaded"] and prof["dropped"][0]["column"] == "customer_email"
    r = client.post("/api/lab/ask", json={"question": "Top 5 suppliers by sales amount"}, headers=h).json()
    assert r["kind"] == "sql" and r["chart"] and client.get("/api/lab/export?kind=result", headers=h).status_code == 200
    jid = client.post("/api/lab/report", json={"organisation": "Test"}, headers=h).json()["id"]
    for _ in range(120):
        st = client.get(f"/api/lab/report/status?id={jid}", headers=h).json()
        if st["status"] != "running":
            break
        time.sleep(1)
    assert st["status"] == "done" and set(st["formats"]) == {"pdf", "docx", "xlsx"}
    assert client.get(f"/api/lab/report/download?id={jid}&fmt=pdf", headers=h).content[:4] == b"%PDF"
    assert client.post("/api/lab/reset", headers=h).json()["ok"]
    assert client.post("/api/lab/ask", json={"question": "top suppliers"}, headers=h).json()["error"]


def test_password_gate(client):
    import base64
    server.APP_PASSWORD, server.APP_USER = "s3cret", "askshelf"
    try:
        assert client.get("/healthz").status_code == 200
        assert client.get("/").status_code == 401 and client.get("/api/meta").status_code == 401
        bad = {"Authorization": "Basic " + base64.b64encode(b"askshelf:wrong").decode()}
        good = {"Authorization": "Basic " + base64.b64encode(b"askshelf:s3cret").decode()}
        assert client.get("/", headers=bad).status_code == 401 and client.get("/", headers=good).status_code == 200
    finally:
        server.APP_PASSWORD = None
    assert client.get("/").status_code == 200
