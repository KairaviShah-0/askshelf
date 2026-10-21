"""AskShelf web server: a JSON API over the analytics engine plus the marketing site and the product app.

Run:  python server.py        (or: uvicorn server:app --port 8000)
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
import tomllib
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.middleware import Middleware
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from agent import alerts as AL
from agent import datalab, labai, labreport, report_docx, report_pdf, report_xlsx
from agent import audit, data, feedback, guardrails, llm, rbac, report
from agent import predict as P
from agent import tools as T
from agent import tools_pro as PRO
from agent.config import APP_NAME, APP_TAGLINE, COMPANY, DEFAULT_MODELS, MAX_QUESTIONS_PER_SESSION, RESTRICTED_FIELDS
from agent.knowledge import SUGGESTED

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"


# ------------------------------------------------------------------------------------------ configuration
def _setting(name: str, default: str | None = None) -> str | None:
    """Environment variable, then .env, then .streamlit/secrets.toml (so an existing key keeps working)."""
    if os.environ.get(name):
        return os.environ[name]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name and v.strip():
                return v.strip().strip("\"'")
    sec = ROOT / ".streamlit" / "secrets.toml"
    if sec.exists():
        try:
            v = tomllib.loads(sec.read_text()).get(name)
            if v:
                return str(v)
        except tomllib.TOMLDecodeError:
            pass
    return default


API_KEY = _setting("GEMINI_API_KEY")
APP_USER = _setting("APP_USER", "askshelf") or "askshelf"
APP_PASSWORD = _setting("APP_PASSWORD")        # when set, the whole site asks for this shared password (HTTP Basic)
MODELS = [m.strip() for m in (_setting("GEMINI_MODELS") or ",".join(DEFAULT_MODELS)).split(",") if m.strip()]
MAX_Q = int(_setting("MAX_QUESTIONS_PER_SESSION", str(MAX_QUESTIONS_PER_SESSION)) or MAX_QUESTIONS_PER_SESSION)

DAILY_LIMIT = int(_setting("DAILY_AI_LIMIT", "400") or 400)      # total AI-backed answers per day across all visitors (protects your Gemini quota)
_daily = {"day": "", "n": 0}


def ai_allowed() -> bool:
    """True while today's shared AI budget lasts; afterwards the app keeps working in built-in mode instead of failing."""
    day = time.strftime("%Y-%m-%d")
    with _lock:
        if _daily["day"] != day:
            _daily.update(day=day, n=0)
        _daily["n"] += 1
        return _daily["n"] <= DAILY_LIMIT


_backend = None
if API_KEY:
    try:
        _backend = llm.GeminiBackend(API_KEY, MODELS)
    except Exception:       # SDK missing or bad key format: built-in mode
        _backend = None

_counts: dict[str, int] = {}
_lock = threading.Lock()
_tool_cache: dict[str, T.ToolResult] = {}


def tool(name: str, **args) -> T.ToolResult:
    key = name + json.dumps(args, sort_keys=True, default=str)
    if key not in _tool_cache:
        _tool_cache[key] = T.run_tool(name, args)
    return _tool_cache[key]


# ------------------------------------------------------------------------------------------ serialisers
def fig_json(fig) -> dict | None:
    return json.loads(fig.to_json()) if fig is not None else None


def table_json(df: pd.DataFrame | None, limit: int = 200) -> dict | None:
    if df is None or not len(df):
        return None
    return json.loads(df.head(limit).to_json(orient="split", date_format="iso", index=False))


def result_json(r: T.ToolResult, full: bool = True) -> dict:
    charts = []
    if r.chart is not None:
        charts.append({"title": r.title, "fig": fig_json(r.chart)})
    charts += [{"title": t, "fig": fig_json(f)} for t, f in r.extra_charts]
    out = {"tool": r.tool, "title": r.title, "period": r.period, "facts": r.facts, "insights": r.insights, "actions": r.actions,
           "kpis": [{"label": a, "value": b, "delta": c} for a, b, c in r.kpis], "charts": charts, "confidence": r.confidence}
    if full:
        out.update(table=table_json(r.table), extra_tables=[{"caption": c, "table": table_json(t)} for c, t in r.extra_tables],
                   notes=r.notes, sources=r.sources, args=r.args)
    return out


def reply_json(reply: llm.AgentReply) -> dict:
    return {"text": reply.text, "blocked": reply.blocked, "mode": reply.mode, "notice": reply.notice, "follow_ups": reply.follow_ups,
            "results": [result_json(r) for r in reply.results], "trace": reply.trace, "critic": reply.critic, "role": reply.role,
            "latency_ms": reply.latency_ms, "est_cost_usd": reply.est_cost_usd, "denied": reply.denied}


def role_of(request: Request) -> str:
    r = request.headers.get("x-role", rbac.DEFAULT_ROLE)
    return r if r in rbac.ROLES else rbac.DEFAULT_ROLE


def gate(request: Request, tool_name: str):
    """Return a 403 response if the caller's role may not use this analysis."""
    role = role_of(request)
    if not rbac.can(role, tool_name):
        audit.log(session=request.headers.get("x-session", "-"), role=role, question=f"[page] {tool_name}", outcome="denied (rbac)", tools=[], mode="page",
                  latency_ms=0, est_cost_usd=0)
        return JSONResponse({"error": "forbidden", "message": rbac.denial_message(role, [tool_name])}, status_code=403)
    return None


def _guard(fn):
    """Turn unexpected exceptions into clean JSON errors (works for sync and async endpoints)."""
    import asyncio
    import functools

    def err(e):
        return JSONResponse({"error": "server_error", "message": f"{type(e).__name__}: {e}"}, status_code=500)

    if asyncio.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(request: Request):
            try:
                return await fn(request)
            except Exception as e:      # noqa: BLE001
                return err(e)
        return awrapper

    @functools.wraps(fn)
    def wrapper(request: Request):
        try:
            return fn(request)
        except Exception as e:      # noqa: BLE001
            return err(e)
    return wrapper


# ------------------------------------------------------------------------------------------ endpoints
@_guard
def api_meta(request: Request):
    lm = P.late_model()
    wm = P.win_model()
    ds = data.load()
    return JSONResponse({
        "name": APP_NAME, "tagline": APP_TAGLINE, "company": COMPANY, "mode": "generative" if _backend else "built-in", "model": getattr(_backend, "active", None),
        "max_questions": MAX_Q, "coverage": data.coverage_text(), "as_of": f"{ds.as_of:%d %b %Y}",
        "roles": {n: {"blurb": r.blurb, "can_export": r.can_export, "allowed": sorted(rbac.allowed_tools(n))} for n, r in rbac.ROLES.items()},
        "default_role": rbac.DEFAULT_ROLE, "suggested": SUGGESTED, "restricted": list(RESTRICTED_FIELDS.values()), "tool_count": len(T.REGISTRY),
        "stats": {"late_auc": round(lm.auc, 2), "win_auc": round(wm.auc, 2), "orders": int(len(ds.orders)), "delivered": int(ds.orders["delivered"].sum()),
                  "late_orders": int(ds.orders["is_late"].sum()), "penalties": round(float(ds.orders["penalty_amt"].sum()))},
    })


@_guard
async def api_ask(request: Request):
    body = await request.json()
    q = str(body.get("question", "")).strip()[:800]
    if not q:
        return JSONResponse({"error": "empty", "message": "Please type a question."}, status_code=400)
    sid = (request.headers.get("x-session") or uuid.uuid4().hex)[:64]
    role = role_of(request)
    with _lock:
        if _counts.get(sid, 0) >= MAX_Q:
            return JSONResponse({"error": "limit", "message": f"This demo session reached its limit of {MAX_Q} questions. Refresh the page to start again."}, status_code=429)
        _counts[sid] = _counts.get(sid, 0) + 1
    hist = [{"role": "user" if m.get("role") == "user" else "assistant", "text": str(m.get("text", ""))[:400]} for m in (body.get("history") or [])[-8:]]
    from starlette.concurrency import run_in_threadpool
    be = _backend if (_backend and ai_allowed()) else None
    reply = await run_in_threadpool(lambda: llm.answer(q, hist, backend=be, self_review=bool(be), role=role))
    audit.log(session=sid, role=role, question=q, outcome="denied (rbac)" if reply.mode == "rbac" else "blocked (guardrail)" if reply.blocked else "answered",
              tools=[r.tool for r in reply.results], mode=reply.mode, latency_ms=reply.latency_ms, est_cost_usd=reply.est_cost_usd)
    out = reply_json(reply)
    out["questions_used"], out["questions_max"] = _counts[sid], MAX_Q
    return JSONResponse(out)


PERIODS = {"last_quarter": "Last quarter", "last_12_months": "Last 12 months", "2026": "2026", "2025": "2025", "all": "All time"}


@_guard
def api_dashboard(request: Request):
    if (g := gate(request, "business_summary")):
        return g
    period = request.query_params.get("period", "last_quarter")
    period = period if period in PERIODS else "last_quarter"
    hs = PRO.health_score(period)
    k, kp = hs["kpis"], hs["prev"]

    def d(cur, prev, kind):
        return T._delta(cur, prev, kind) if kp else None
    kpis = [("Cash profit", T.money(k["profit"]), d(k["profit"], kp and kp["profit"], "money")), ("Cash revenue", T.money(k["revenue"]), d(k["revenue"], kp and kp["revenue"], "money")),
            ("Cash margin", T.pct(k["margin"]), d(k["margin"], kp and kp["margin"], "pp")), ("Win rate", T.pct(k["win_rate"]), d(k["win_rate"], kp and kp["win_rate"], "pp")),
            ("On-time delivery", T.pct(k["on_time"]), d(k["on_time"], kp and kp["on_time"], "pp")),
            ("Avg lead time", f"{k['avg_lead']:.1f} days" if k["avg_lead"] == k["avg_lead"] else "n/a", d(k["avg_lead"], kp and kp["avg_lead"], "days"))]
    bridge, fin = tool("profit_bridge", period=period), tool("financial_trend", period="all", granularity="quarter")
    return JSONResponse({"period": hs["period"], "compared": bool(kp), "score": round(hs["score"]), "label": hs["label"], "components": hs["components"],
                         "kpis": [{"label": a, "value": b, "delta": c} for a, b, c in kpis],
                         "money": {"bridge": result_json(bridge, False), "trend": result_json(fin, False)},
                         "sales": {"funnel": result_json(tool("quote_funnel", period=period), False), "forecast": result_json(tool("forecast_outlook", metric="quotes", horizon_months=3), False)},
                         "ops": result_json(tool("late_risk_radar", top_n=8), True),
                         "warnings": result_json(tool("anomaly_scan"), True)})


ALERT_TOOL = {"Delivery risk": "late_risk_radar", "Penalty exposure": "late_risk_radar", "Demand dip": "forecast_outlook", "Unusual month": "anomaly_scan",
              "Low stock": "inventory_analysis", "Stock shortfall": "stock_cover", "Overdue orders": "delivery_schedule",  "Win-rate slide": "sales_pricing_analysis"}


def alerts_for(role: str) -> list[AL.Alert]:
    """Alerts the role is cleared to see (each alert is backed by an analysis the role must be allowed to run)."""
    return [a for a in AL.scan() if rbac.can(role, ALERT_TOOL.get(a.category, "business_summary"))]


@_guard
def api_alerts(request: Request):
    al = alerts_for(role_of(request))
    return JSONResponse({"alerts": [a.__dict__ for a in al], "rules": [{"rule": a, "fires": b} for a, b in AL.RULES]})


@_guard
def api_digest(request: Request):
    role = role_of(request)
    if not rbac.ROLES[role].can_export:
        return JSONResponse({"error": "forbidden", "message": "Exports need the Owner or Finance Lead role."}, status_code=403)
    import html as H
    col = {"critical": "#d6453a", "warning": "#eda100", "info": "#2a78d6"}
    items = "".join(f"<div style='border-left:5px solid {col[a.severity]};padding:8px 12px;margin:8px 0;background:#fafafa'><b>{H.escape(a.headline)}</b><br>"
                    f"<span style='color:#555'>{H.escape(a.evidence)}</span><br><span style='color:#12805c'>Action: {H.escape(a.action)}</span></div>" for a in alerts_for(role))
    html = (f"<html><head><meta charset='utf-8'><title>AskShelf daily digest</title></head><body style='font-family:Inter,Arial,sans-serif;max-width:760px;margin:30px auto;color:#111'>"
            f"<h1>AskShelf daily digest</h1><p style='color:#666'>{COMPANY} &middot; {H.escape(role)} &middot; data to {data.load().as_of:%d %b %Y}</p><h2>Needs attention</h2>{items}</body></html>")
    return Response(html, media_type="text/html", headers={"Content-Disposition": "attachment; filename=askshelf_daily_digest.html"})


@_guard
def api_report(request: Request):
    role = role_of(request)
    if not rbac.ROLES[role].can_export:
        return JSONResponse({"error": "forbidden", "message": "Exports need the Owner or Finance Lead role."}, status_code=403)
    period = request.query_params.get("period", "last_quarter")
    return Response(report.build_html(period if period in PERIODS else "last_quarter"), media_type="text/html",
                    headers={"Content-Disposition": "attachment; filename=askshelf_executive_briefing.html"})


@_guard
def api_forecast(request: Request):
    if (g := gate(request, "forecast_outlook")):
        return g
    metric = request.query_params.get("metric", "revenue")
    metric = metric if metric in PRO.METRIC_LABELS else "revenue"
    hz = max(1, min(6, int(request.query_params.get("months", 3))))
    return JSONResponse(result_json(tool("forecast_outlook", metric=metric, horizon_months=hz)) | {"metrics": {k: v[0] for k, v in PRO.METRIC_LABELS.items()}})


@_guard
def api_risk(request: Request):
    if (g := gate(request, "late_risk_radar")):
        return g
    lm, ds = P.late_model(), data.load()
    op = ds.orders[ds.orders["in_progress"]]
    risk = lm.predict(op)
    r = tool("late_risk_radar", top_n=max(5, min(17, int(request.query_params.get("n", 10)))))
    return JSONResponse(result_json(r) | {"summary": [
        {"label": "Orders in progress", "value": str(len(op))}, {"label": "Expected to finish late", "value": f"{risk.sum():.1f}"},
        {"label": "High-risk orders", "value": str(int((risk >= 2 * lm.base_rate).sum()))},
        {"label": "Penalty exposure", "value": T.money(float((risk * op['quotePrice'] * 0.05).sum()))}, {"label": "Model accuracy (AUC)", "value": f"{lm.auc:.2f}"}]})


@_guard
def api_pricing(request: Request):
    if (g := gate(request, "price_strategy_analysis")):
        return g
    lm, wm, el = P.late_model(), P.win_model(), P.price_elasticity()
    card = [{"name": "Late-delivery risk model", "verdict": "Strong", "evidence": f"AUC {lm.auc:.2f} (5-fold cross-validated) on {lm.n} delivered orders.", "meaning": "Workload per maker is the dominant driver. Safe for prioritising orders."},
            {"name": "Quote-win model", "verdict": "No signal", "evidence": f"AUC {wm.auc:.2f} on {wm.n:,} quotes - no better than a coin flip.", "meaning": "Price, product, complexity and timber do not explain which quotes are accepted, so individual quotes are not predicted."},
            {"name": "Price sensitivity", "verdict": "Uncertain", "evidence": f"Elasticity {el['estimate']:+.2f} (80% interval {el['lo']:+.2f} to {el['hi']:+.2f}).", "meaning": "The data cannot separate it from zero, so scenarios use three reaction assumptions."}]
    return JSONResponse(result_json(tool("price_strategy_analysis")) | {"report_card": card})


ELASTICITY = {"data": None, "moderate": -1.0, "high": -2.0}


@_guard
async def api_whatif(request: Request):
    if (g := gate(request, "scenario_simulator")):
        return g
    b = await request.json()
    el = ELASTICITY.get(b.get("elasticity", "data"))
    args = dict(price_change_pct=b.get("price", 0), quote_volume_change_pct=b.get("volume", 0), extra_makers=b.get("makers", 0), timber_cost_change_pct=b.get("timber", 0))
    if el is not None:
        args["price_elasticity"] = el
    r = tool("scenario_simulator", **args)
    base = P.sim_baseline()
    scn = P.simulate(PRO._sc(args["price_change_pct"], args["quote_volume_change_pct"], args["extra_makers"], args["timber_cost_change_pct"], el))
    sign = lambda v: f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"
    kp = [{"label": "Monthly profit", "value": T.money(scn["profit"]), "delta": sign(scn["profit"] - base["profit"])},
          {"label": "On-time delivery", "value": T.pct(scn["on_time"], 0), "delta": f"{(scn['on_time'] - base['on_time']) * 100:+.1f} pp"},
          {"label": "Win rate", "value": T.pct(scn["win_rate"], 1), "delta": f"{(scn['win_rate'] - base['win_rate']) * 100:+.1f} pp"},
          {"label": "Orders per month", "value": f"{scn['orders']:.1f}", "delta": f"{scn['orders'] - base['orders']:+.1f}"}]
    return JSONResponse(result_json(r) | {"scenario_kpis": kp})


@_guard
def api_capacity(request: Request):
    if (g := gate(request, "capacity_planner")):
        return g
    return JSONResponse(result_json(tool("capacity_planner")))


@_guard
def api_supply(request: Request):
    if (g := gate(request, "stock_cover")):
        return g
    return JSONResponse(result_json(tool("stock_cover")))


@_guard
def api_schedule(request: Request):
    if (g := gate(request, "delivery_schedule")):
        return g
    r = tool("delivery_schedule")
    t = r.table
    days = t["Days left"]
    cash = lambda col: float(t[col].str.replace(r"[$,]", "", regex=True).astype(float).sum())
    summary = [{"label": "Open orders", "value": str(len(t))}, {"label": "Due in 7 days", "value": str(int(((days >= 0) & (days < 7)).sum()))},
               {"label": "Overdue", "value": str(int((days < 0).sum()))}, {"label": "High risk", "value": str(int((t["Risk level"] == "High").sum()))},
               {"label": "Penalty exposure", "value": T.money(cash("Penalty exposure"))}]
    return JSONResponse(result_json(r) | {"summary": summary})


@_guard
async def api_penalties(request: Request):
    if (g := gate(request, "penalty_control")):
        return g
    b = await request.json() if request.method == "POST" else {}
    return JSONResponse(result_json(tool("penalty_control", expedite_n=int(b.get("expedite", 0) or 0), extra_makers=float(b.get("makers", 0) or 0))))


@_guard
def api_snapshot(request: Request):
    """Aggregate, non-sensitive live numbers for the public landing page cards."""
    sc, ds_, pc = tool("stock_cover"), tool("delivery_schedule"), tool("penalty_control")
    t = ds_.table
    return JSONResponse({"schedule": f"{int(((t['Days left'] >= 0) & (t['Days left'] < 7)).sum())} due this week, {int((t['Days left'] < 0).sum())} overdue",
                         "stock": f"{int((sc.table['Status'] == 'Short').sum())} of {len(sc.table)} materials short for open orders",
                         "penalty": f"{pc.kpis[3][1]} exposure on open orders", "alerts": f"{len([a for a in AL.scan() if a.severity != 'info'])} alerts need attention"})



# ------------------------------------------------------------------------------------------ Data Lab (bring your own data)
JOBS: dict[str, dict] = {}


def lab_gate(request: Request):
    """The Data Lab runs free-form analysis on uploaded files, so it is limited to roles that may also export (Owner, Finance Lead)."""
    role = role_of(request)
    if not rbac.ROLES[role].can_export:
        audit.log(session=request.headers.get("x-session", "-"), role=role, question="[data lab]", outcome="denied (rbac)", tools=[], mode="page", latency_ms=0, est_cost_usd=0)
        return JSONResponse({"error": "forbidden", "message": "The Data Lab needs the Owner or Finance Lead role because it analyses uploaded files without the built-in restrictions."}, status_code=403)
    return None


def _lab(request: Request) -> datalab.Lab:
    return datalab.get_lab((request.headers.get("x-session") or "anon")[:64])


@_guard
async def api_lab_upload(request: Request):
    if (g := lab_gate(request)):
        return g
    name = request.query_params.get("name", "upload.csv")[:120]
    if int(request.headers.get("content-length") or 0) > datalab.MAX_UPLOAD_MB * 1024 * 1024:
        return JSONResponse({"error": "too_large", "message": f"'{name}' is larger than {datalab.MAX_UPLOAD_MB} MB."}, status_code=413)
    body = await request.body()
    lab = _lab(request)
    from starlette.concurrency import run_in_threadpool
    try:
        await run_in_threadpool(lab.add_file, name, body)
    except ValueError as e:
        return JSONResponse({"error": "bad_file", "message": str(e)}, status_code=400)
    except Exception as e:      # noqa: BLE001 - corrupt workbook etc.
        return JSONResponse({"error": "bad_file", "message": f"Could not read '{name}': {type(e).__name__}. Check that it is a valid Excel or CSV file."}, status_code=400)
    audit.log(session=request.headers.get("x-session", "-"), role=role_of(request), question=f"[upload] {name}", outcome="answered", tools=["lab_upload"], mode="lab", latency_ms=0, est_cost_usd=0)
    return JSONResponse(datalab.profile(lab))


@_guard
def api_lab_sample(request: Request):
    if (g := lab_gate(request)):
        return g
    lab = _lab(request)
    lab.reset()
    for f in sorted((ROOT / "sample_data").glob("*.csv")):
        lab.add_file(f.name, f.read_bytes())
    return JSONResponse(datalab.profile(lab))


@_guard
def api_lab_dataset(request: Request):
    if (g := lab_gate(request)):
        return g
    lab = _lab(request)
    return JSONResponse(datalab.profile(lab) | {"qa": lab.qa_log[-20:]})


@_guard
def api_lab_reset(request: Request):
    if (g := lab_gate(request)):
        return g
    _lab(request).reset()
    return JSONResponse({"ok": True})


@_guard
def api_lab_clear_memory(request: Request):
    if (g := lab_gate(request)):
        return g
    lab = _lab(request)
    lab.cache.clear()
    lab.qa_log.clear()
    lab.last_result = None
    return JSONResponse({"ok": True})


@_guard
async def api_lab_ask(request: Request):
    if (g := lab_gate(request)):
        return g
    b = await request.json()
    q = str(b.get("question", "")).strip()[:800]
    if not q:
        return JSONResponse({"error": "empty", "message": "Please type a question."}, status_code=400)
    lab = _lab(request)
    hist = [{"role": "user" if m.get("role") == "user" else "assistant", "text": str(m.get("text", ""))[:300]} for m in (b.get("history") or [])[-6:]]
    from starlette.concurrency import run_in_threadpool
    t0 = time.time()
    be = _backend if (_backend and ai_allowed()) else None
    res = await run_in_threadpool(lambda: labai.lab_answer(lab, q, hist, backend=be))
    ms = int((time.time() - t0) * 1000)
    audit.log(session=request.headers.get("x-session", "-"), role=role_of(request), question=q, outcome="blocked (guardrail)" if res.get("blocked") else ("error" if res.get("error") else "answered"),
              tools=["lab_forecast" if res.get("kind") == "forecast" else "lab_sql"], mode=res.get("mode", "lab"), latency_ms=ms, est_cost_usd=0.004 if res.get("mode") == "ai" else 0)
    res["latency_ms"] = ms
    return JSONResponse(res)


@_guard
def api_lab_export(request: Request):
    if (g := lab_gate(request)):
        return g
    lab = _lab(request)
    kind = request.query_params.get("kind", "result")
    if kind == "sample":
        if not lab.loaded:
            return JSONResponse({"error": "none", "message": "Upload data first."}, status_code=400)
        name, df = next(iter(lab.tables.items()))
        data_ = report_xlsx.table_xlsx(df.head(1000), f"Data sample: {name}")
        fn = "data_sample.xlsx"
    else:
        lr = lab.last_result
        if not lr:
            return JSONResponse({"error": "none", "message": "Ask a question first, then export its result."}, status_code=400)
        data_ = report_xlsx.table_xlsx(lr["table"], lr["title"], lr.get("sql"), lr.get("question"))
        fn = "analysis_result.xlsx"
    return Response(data_, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename={fn}"})


def _run_report(job_id: str, lab: datalab.Lab, meta: dict):
    job = JOBS[job_id]
    try:
        def progress(name, n):
            job["steps"].append(name)
        rep = labreport.build_report(lab, _backend if (_backend and ai_allowed()) else None, meta, progress)
        job["steps"].append("Typeset the PDF report")
        pdf, pages = report_pdf.build_pdf(rep)
        job["steps"].append("Build the Word document")
        docx_ = report_docx.build_docx(rep, pages)
        job["steps"].append("Build the Excel workbook")
        xlsx = report_xlsx.report_xlsx(rep)
        job["files"] = {"pdf": pdf, "docx": docx_, "xlsx": xlsx}
        job["summary"] = {"title": rep.title, "sections": len(rep.sections), "figures": len(rep.figures), "tables": len(rep.tables), "pages": len(pages) and None, "model": rep.model,
                          "findings": [{"title": s.title, "finding": s.key_finding} for s in rep.sections], "notes": rep.ai_notes}
        job["status"] = "done"
    except Exception as e:      # noqa: BLE001
        job["status"], job["message"] = "error", f"{type(e).__name__}: {e}"


@_guard
async def api_lab_report(request: Request):
    if (g := lab_gate(request)):
        return g
    lab = _lab(request)
    if not lab.loaded:
        return JSONResponse({"error": "none", "message": "Upload data first."}, status_code=400)
    b = await request.json()
    meta = {k: str(b.get(k, ""))[:140] for k in ("title", "subtitle", "organisation", "prepared_for", "prepared_by")}
    jid = uuid.uuid4().hex[:12]
    while len(JOBS) >= 10:
        JOBS.pop(next(iter(JOBS)))
    JOBS[jid] = {"status": "running", "steps": [], "files": {}, "created": time.time(), "session": request.headers.get("x-session", "")}
    threading.Thread(target=_run_report, args=(jid, lab, meta), daemon=True).start()
    audit.log(session=request.headers.get("x-session", "-"), role=role_of(request), question="[report] generate", outcome="answered", tools=["lab_report"], mode="ai" if _backend else "rules", latency_ms=0, est_cost_usd=0.03 if _backend else 0)
    return JSONResponse({"id": jid})


@_guard
def api_lab_report_status(request: Request):
    if (g := lab_gate(request)):
        return g
    job = JOBS.get(request.query_params.get("id", ""))
    if not job:
        return JSONResponse({"error": "none", "message": "Report not found. It may have expired."}, status_code=404)
    return JSONResponse({"status": job["status"], "steps": job["steps"], "message": job.get("message"), "summary": job.get("summary"), "formats": list(job["files"])})


@_guard
def api_lab_report_download(request: Request):
    if (g := lab_gate(request)):
        return g
    job = JOBS.get(request.query_params.get("id", ""))
    fmt = request.query_params.get("fmt", "pdf")
    if not job or fmt not in job["files"]:
        return JSONResponse({"error": "none", "message": "That file is not available."}, status_code=404)
    mt = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[fmt]
    return Response(job["files"][fmt], media_type=mt, headers={"Content-Disposition": f"attachment; filename=askshelf_report.{fmt}"})


@_guard
def api_governance(request: Request):
    ok, n, bad = audit.verify()
    ents = audit.read()
    rows = []
    for e in reversed(ents[-100:]):
        if "corrupt" in e:
            continue
        rows.append({"time": e["ts"].replace("T", " ").replace("+00:00", " UTC"), "role": e["role"], "question": e["question"], "outcome": e["outcome"],
                     "tools": ", ".join(e["tools"]), "mode": e["mode"], "latency_ms": e["latency_ms"], "cost": e["est_cost_usd"], "hash": e["hash"][:10]})
    m = rbac.access_matrix()
    fs = feedback.summary()
    return JSONResponse({"chain": {"intact": ok, "entries": n, "bad_line": bad}, "audit": rows, "matrix": json.loads(m.to_json(orient="split", index=False)),
                         "roles": list(rbac.ROLES), "restricted": list(RESTRICTED_FIELDS.values()),
                         "feedback": {"up": fs["up"], "down": fs["down"], "reports": fs["issues"] + fs["definitions"]}})


@_guard
def api_audit_download(request: Request):
    if not rbac.ROLES[role_of(request)].can_export:
        return JSONResponse({"error": "forbidden", "message": "Exports need the Owner or Finance Lead role."}, status_code=403)
    body = audit.AUDIT_FILE.read_bytes() if audit.AUDIT_FILE.exists() else b""
    return Response(body, media_type="application/x-ndjson", headers={"Content-Disposition": "attachment; filename=audit_log.jsonl"})


@_guard
async def api_feedback(request: Request):
    b = await request.json()
    kind = b.get("kind", "rating")
    if kind not in ("rating", "issue", "definition"):
        return JSONResponse({"error": "bad_kind"}, status_code=400)
    feedback.log(kind, rating=b.get("rating", ""), category=b.get("category", ""), comment=b.get("comment", ""), question=b.get("question", ""),
                 tools_used=b.get("tools", ""), mode=b.get("mode", ""))
    return JSONResponse({"ok": True})


@_guard
def api_glossary(request: Request):
    return JSONResponse({"definitions": [{"term": k.title(), "meaning": v} for k, v in T.DEFINITIONS.items()], "files": [{"file": k, "about": v} for k, v in T.FILES.items()]})


def plotly_js(request: Request):
    import plotly
    return FileResponse(Path(plotly.__file__).parent / "package_data" / "plotly.min.js", media_type="text/javascript",
                        headers={"Cache-Control": "public, max-age=86400"})


def page(name: str):
    def handler(request: Request):
        return FileResponse(WEB / name, headers={"Cache-Control": "no-cache"})
    return handler


@asynccontextmanager
async def lifespan(_app):
    # warm the data and models so the first visitor does not wait
    data.load()
    for warm in (P.late_model, P.win_model, P.baseline, P.price_elasticity):
        warm()
    yield


def healthz(request: Request):
    return Response("ok", media_type="text/plain")


routes = [
    Route("/healthz", healthz), Route("/", page("index.html")), Route("/app", page("app.html")),
    Route("/api/meta", api_meta), Route("/api/ask", api_ask, methods=["POST"]), Route("/api/dashboard", api_dashboard), Route("/api/alerts", api_alerts),
    Route("/api/digest", api_digest), Route("/api/report", api_report), Route("/api/forecast", api_forecast), Route("/api/risk", api_risk),
    Route("/api/pricing", api_pricing), Route("/api/whatif", api_whatif, methods=["POST"]), Route("/api/capacity", api_capacity),
    Route("/api/penalties", api_penalties, methods=["GET", "POST"]), Route("/api/snapshot", api_snapshot),
    Route("/api/supply", api_supply), Route("/api/schedule", api_schedule),
    Route("/api/governance", api_governance), Route("/api/audit/download", api_audit_download), Route("/api/feedback", api_feedback, methods=["POST"]),
    Route("/api/glossary", api_glossary),
    Route("/api/lab/upload", api_lab_upload, methods=["POST"]), Route("/api/lab/sample", api_lab_sample, methods=["POST"]), Route("/api/lab/dataset", api_lab_dataset),
    Route("/api/lab/reset", api_lab_reset, methods=["POST"]), Route("/api/lab/clear-memory", api_lab_clear_memory, methods=["POST"]), Route("/api/lab/ask", api_lab_ask, methods=["POST"]),
    Route("/api/lab/export", api_lab_export), Route("/api/lab/report", api_lab_report, methods=["POST"]), Route("/api/lab/report/status", api_lab_report_status),
    Route("/api/lab/report/download", api_lab_report_download),
    Route("/static/vendor/plotly.min.js", plotly_js),
    Mount("/static", StaticFiles(directory=WEB / "static"), name="static"),
]
class PasswordGate:
    """Optional shared-password gate for public deployments. Off unless APP_PASSWORD is set. /healthz stays open for the host's health checks."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and APP_PASSWORD and scope["path"] != "/healthz":
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            ok = False
            if auth.startswith("Basic "):
                try:
                    user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                    ok = secrets.compare_digest(user, APP_USER) and secrets.compare_digest(pw, APP_PASSWORD)
                except Exception:       # noqa: BLE001 - malformed header
                    ok = False
            if not ok:
                await Response("Sign in required.", status_code=401, headers={"WWW-Authenticate": 'Basic realm="AskShelf"'})(scope, receive, send)
                return
        await self.app(scope, receive, send)


app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(PasswordGate)])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=False)
