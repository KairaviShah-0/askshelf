"""AskShelf web server: a JSON API over the analytics engine, plus the marketing site and the product app.

Everything the app shows is computed from the files a visitor uploads (kept in memory per browser session). Run:  python server.py
"""
from __future__ import annotations

import asyncio
import functools
import html as H
import os
import threading
import time
import tomllib
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from agent import audit, datalab, feedback, guardrails, labai, labreport, rbac, report_docx, report_pdf, report_xlsx
from agent import retail as R
from agent.config import APP_NAME, APP_TAGLINE, DEFAULT_MODELS, MAX_QUESTIONS_PER_SESSION, RESTRICTED_FIELDS
from agent.gemini import GeminiBackend

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
MODELS = [m.strip() for m in (_setting("GEMINI_MODELS") or ",".join(DEFAULT_MODELS)).split(",") if m.strip()]
MAX_Q = int(_setting("MAX_QUESTIONS_PER_SESSION", str(MAX_QUESTIONS_PER_SESSION)) or MAX_QUESTIONS_PER_SESSION)
DAILY_LIMIT = int(_setting("DAILY_AI_LIMIT", "400") or 400)          # shared AI budget per day (protects the Gemini quota)
_lock = threading.Lock()
_daily = {"day": "", "n": 0}
_counts: dict[str, int] = {}
JOBS: dict[str, dict] = {}

_backend = None
if API_KEY:
    try:
        _backend = GeminiBackend(API_KEY, MODELS)
    except Exception:       # SDK missing or bad key format: built-in mode
        _backend = None


def ai_backend():
    """The AI backend while today's shared budget lasts; afterwards None, so the app keeps working in built-in mode."""
    if _backend is None:
        return None
    day = time.strftime("%Y-%m-%d")
    with _lock:
        if _daily["day"] != day:
            _daily.update(day=day, n=0)
        _daily["n"] += 1
        return _backend if _daily["n"] <= DAILY_LIMIT else None


# ------------------------------------------------------------------------------------------ plumbing
def role_of(request: Request) -> str:
    r = request.headers.get("x-role", rbac.DEFAULT_ROLE)
    return r if r in rbac.ROLES else rbac.DEFAULT_ROLE


def sid(request: Request) -> str:
    return (request.headers.get("x-session") or "anon")[:64]


def lab_of(request: Request) -> datalab.Lab:
    return datalab.get_lab(sid(request))


def err(status: int, code: str, message: str, **extra):
    return JSONResponse({"error": code, "message": message, **extra}, status_code=status)


def log(request: Request, what: str, outcome: str = "answered", tools=(), mode: str = "app", ms: int = 0, cost: float = 0.0):
    audit.log(session=sid(request), role=role_of(request), question=what, outcome=outcome, tools=list(tools), mode=mode, latency_ms=ms, est_cost_usd=cost)


def page_gate(request: Request, page: str):
    if not rbac.can_view(role_of(request), page):
        log(request, f"[page] {page}", "denied (rbac)")
        return err(403, "forbidden", rbac.denial_message(role_of(request), page))
    return None


def upload_gate(request: Request):
    if not rbac.ROLES[role_of(request)].can_upload:
        log(request, "[data] upload", "denied (rbac)")
        return err(403, "forbidden", f"Your role ({role_of(request)}) cannot load or reset data. Ask the Owner, Finance Lead or Operations Manager.")
    return None


def export_gate(request: Request):
    if not rbac.ROLES[role_of(request)].can_export:
        return err(403, "forbidden", "Exports need the Owner or Finance Lead role.")
    return None


def analysis(request: Request, page: str, cap: str):
    """Common checks for analysis endpoints: role, data loaded, required columns mapped. Returns (lab, error_response)."""
    if (g := page_gate(request, page)):
        return None, g
    lab = lab_of(request)
    if not lab.loaded:
        return None, err(409, "no_data", "Upload your data first (Data page).")
    caps = R.capabilities(lab)
    if not caps.get(cap):
        return None, err(422, "missing_columns", R.MISSING.get(cap, "Map the required columns on the Data page."), caps=caps)
    return lab, None


def guard(fn):
    """Turn unexpected exceptions into clean JSON errors (sync and async endpoints)."""
    def to_err(e):
        if isinstance(e, ValueError):
            return err(422, "cannot_analyse", str(e))
        return err(500, "server_error", f"{type(e).__name__}: {e}")
    if asyncio.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def aw(request: Request):
            try:
                return await fn(request)
            except Exception as e:      # noqa: BLE001
                return to_err(e)
        return aw

    @functools.wraps(fn)
    def w(request: Request):
        try:
            return fn(request)
        except Exception as e:      # noqa: BLE001
            return to_err(e)
    return w


# ------------------------------------------------------------------------------------------ meta
@guard
def api_meta(request: Request):
    return JSONResponse({"name": APP_NAME, "tagline": APP_TAGLINE, "mode": "generative" if _backend else "built-in", "model": getattr(_backend, "active", None), "max_questions": MAX_Q,
                         "roles": {n: {"blurb": r.blurb, "pages": sorted(r.pages), "can_upload": r.can_upload, "can_export": r.can_export} for n, r in rbac.ROLES.items()},
                         "default_role": rbac.DEFAULT_ROLE, "restricted": list(RESTRICTED_FIELDS.values())})


# ------------------------------------------------------------------------------------------ data: upload, mapping, profile
def dataset_payload(lab: datalab.Lab) -> dict:
    prof = datalab.profile(lab)
    if not lab.loaded:
        return prof | {"mapping": None, "caps": R.capabilities(lab), "fields": []}
    m = R.get_mapping(lab)
    cols = {nm: [{"name": c, "kind": datalab.kind_of(df[c])} for c in df.columns] for nm, df in lab.tables.items()}
    c = R.canon(lab)
    return prof | {"mapping": m, "caps": R.capabilities(lab), "columns": cols,
                   "fields": [{"key": f.key, "label": f.label, "kind": f.kind, "help": f.help, "needed_for": f.needed_for} for f in R.FIELDS],
                   "range": f"{c['date'].min():%d %b %Y} to {c['date'].max():%d %b %Y}" if c is not None else None, "usable_rows": int(len(c)) if c is not None else 0}


@guard
async def api_upload(request: Request):
    if (g := upload_gate(request)):
        return g
    name = request.query_params.get("name", "upload.csv")[:120]
    if int(request.headers.get("content-length") or 0) > datalab.MAX_UPLOAD_MB * 1024 * 1024:
        return err(413, "too_large", f"'{name}' is larger than {datalab.MAX_UPLOAD_MB} MB.")
    body = await request.body()
    lab = lab_of(request)
    try:
        await run_in_threadpool(lab.add_file, name, body)
    except ValueError as e:
        return err(400, "bad_file", str(e))
    except Exception as e:      # noqa: BLE001 - corrupt workbook etc.
        return err(400, "bad_file", f"Could not read '{name}': {type(e).__name__}. Check that it is a valid Excel or CSV file.")
    log(request, f"[upload] {name}", tools=["upload"], mode="data")
    return JSONResponse(await run_in_threadpool(dataset_payload, lab))


@guard
def api_sample(request: Request):
    if (g := upload_gate(request)):
        return g
    lab = lab_of(request)
    lab.reset()
    for f in sorted((ROOT / "sample_data").glob("*.csv")):
        lab.add_file(f.name, f.read_bytes())
    log(request, "[data] load sample", tools=["sample"], mode="data")
    return JSONResponse(dataset_payload(lab))


@guard
def api_dataset(request: Request):
    if (g := page_gate(request, "data")):
        return g
    return JSONResponse(dataset_payload(lab_of(request)))


@guard
async def api_mapping(request: Request):
    if (g := upload_gate(request)):
        return g
    b = await request.json()
    lab = lab_of(request)
    try:
        R.set_mapping(lab, str(b.get("table", "")), b.get("map") or {})
    except ValueError as e:
        return err(400, "bad_mapping", str(e))
    lab.cache.clear()
    log(request, "[data] column mapping saved", tools=["mapping"], mode="data")
    return JSONResponse(dataset_payload(lab))


@guard
def api_reset(request: Request):
    if (g := upload_gate(request)):
        return g
    lab_of(request).reset()
    log(request, "[data] reset", tools=["reset"], mode="data")
    return JSONResponse({"ok": True})


@guard
def api_clear_memory(request: Request):
    lab = lab_of(request)
    lab.qa_log.clear()
    lab.last_result = None
    for k in [k for k in lab.cache if not isinstance(k, str)]:
        lab.cache.pop(k, None)
    return JSONResponse({"ok": True})


@guard
def api_export(request: Request):
    if (g := export_gate(request)):
        return g
    lab = lab_of(request)
    if request.query_params.get("kind") == "sample":
        if not lab.loaded:
            return err(400, "none", "Upload data first.")
        name, df = next(iter(lab.tables.items()))
        return xlsx_response(report_xlsx.table_xlsx(df.head(1000), f"Data sample: {name}"), "data_sample.xlsx")
    lr = lab.last_result
    if not lr:
        return err(400, "none", "Ask a question first, then export its result.")
    return xlsx_response(report_xlsx.table_xlsx(lr["table"], lr["title"], lr.get("sql"), lr.get("question")), "analysis_result.xlsx")


def xlsx_response(data: bytes, name: str):
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename={name}"})


# ------------------------------------------------------------------------------------------ ask
@guard
async def api_ask(request: Request):
    if (g := page_gate(request, "ask")):
        return g
    b = await request.json()
    q = str(b.get("question", "")).strip()[:800]
    if not q:
        return err(400, "empty", "Please type a question.")
    with _lock:
        if _counts.get(sid(request), 0) >= MAX_Q:
            return err(429, "limit", f"This demo session reached its limit of {MAX_Q} questions. Refresh the page to start again.")
        _counts[sid(request)] = _counts.get(sid(request), 0) + 1
    lab = lab_of(request)
    hist = [{"role": "user" if m.get("role") == "user" else "assistant", "text": str(m.get("text", ""))[:300]} for m in (b.get("history") or [])[-6:]]
    t0 = time.time()
    res = await run_in_threadpool(lambda: labai.lab_answer(lab, q, hist, backend=ai_backend()))
    ms = int((time.time() - t0) * 1000)
    log(request, q, "blocked (guardrail)" if res.get("blocked") else ("error" if res.get("error") else "answered"), ["sql" if res.get("kind") == "sql" else res.get("kind") or "ask"], res.get("mode", "rules"), ms,
        0.004 if res.get("mode") == "ai" else 0)
    res["latency_ms"], res["questions_used"], res["questions_max"] = ms, _counts[sid(request)], MAX_Q
    return JSONResponse(res)


# ------------------------------------------------------------------------------------------ analysis pages
def _int(v, d, lo, hi):
    try:
        return max(lo, min(hi, int(float(v))))
    except (TypeError, ValueError):
        return d


def _num(v, d, lo, hi):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return d


@guard
def api_dashboard(request: Request):
    lab, e = analysis(request, "dashboard", "dashboard")
    if e:
        return e
    return JSONResponse(R.dashboard(lab, request.query_params.get("period", "90d")))


@guard
def api_deliveries(request: Request):
    lab, e = analysis(request, "deliveries", "deliveries")
    if e:
        return e
    return JSONResponse(R.deliveries(lab, _int(request.query_params.get("expedite"), 0, 0, 100)))


@guard
async def api_penalties(request: Request):
    lab, e = analysis(request, "penalties", "penalties")
    if e:
        return e
    b = await request.json() if request.method == "POST" else {}
    return JSONResponse(await run_in_threadpool(R.penalties, lab, _int(b.get("expedite"), 0, 0, 100), _num(b.get("rate"), 0.0, 0.0, 100.0)))


@guard
def api_stock(request: Request):
    lab, e = analysis(request, "stock", "stock")
    if e:
        return e
    return JSONResponse(R.stock(lab, _int(request.query_params.get("target"), 30, 7, 180)))


@guard
def api_forecast(request: Request):
    lab, e = analysis(request, "forecasts", "forecasts")
    if e:
        return e
    q = request.query_params
    return JSONResponse(R.forecast(lab, q.get("metric", "revenue"), q.get("group") or None, _int(q.get("periods"), 6, 1, 24), q.get("unit") or None))


@guard
async def api_planner(request: Request):
    lab, e = analysis(request, "planner", "planner")
    if e:
        return e
    b = await request.json()
    return JSONResponse(R.planner(lab, _num(b.get("price"), 0, -50, 100), _num(b.get("volume"), 0, -80, 200), _num(b.get("cost"), 0, -50, 100), str(b.get("elasticity", "none"))))


def visible_alerts(request: Request, lab: datalab.Lab) -> list[dict]:
    role = role_of(request)
    return [a for a in R.alerts(lab) if rbac.can_view(role, rbac.ALERT_PAGE.get(a["page"], "dashboard"))]


@guard
def api_alerts(request: Request):
    if (g := page_gate(request, "alerts")):
        return g
    lab = lab_of(request)
    if not lab.loaded:
        return err(409, "no_data", "Upload your data first (Data page).")
    if R.canon(lab) is None:
        return err(422, "missing_columns", R.MISSING["dashboard"])
    return JSONResponse({"alerts": visible_alerts(request, lab), "rules": R.active_rules(lab)})


@guard
def api_digest(request: Request):
    if (g := export_gate(request)):
        return g
    lab = lab_of(request)
    if not lab.loaded or R.canon(lab) is None:
        return err(409, "no_data", "Upload your data first.")
    col = {"critical": "#d6453a", "warning": "#eda100", "info": "#2a78d6"}
    items = "".join(f"<div style='border-left:5px solid {col[a['severity']]};padding:8px 12px;margin:8px 0;background:#fafafa'><b>{H.escape(a['headline'])}</b><br>"
                    f"<span style='color:#555'>{H.escape(a['evidence'])}</span><br><span style='color:#12805c'>Action: {H.escape(a['action'])}</span></div>" for a in visible_alerts(request, lab))
    html = (f"<html><head><meta charset='utf-8'><title>AskShelf daily digest</title></head><body style='font-family:Inter,Arial,sans-serif;max-width:760px;margin:30px auto;color:#111'>"
            f"<h1>AskShelf daily digest</h1><p style='color:#666'>{H.escape(role_of(request))} &middot; data to {R.as_of(R.canon(lab)):%d %b %Y}</p><h2>Needs attention</h2>{items}</body></html>")
    return Response(html, media_type="text/html", headers={"Content-Disposition": "attachment; filename=askshelf_daily_digest.html"})


# ------------------------------------------------------------------------------------------ reports
def _run_report(job_id: str, lab: datalab.Lab, meta: dict, backend):
    job = JOBS[job_id]
    try:
        rep = labreport.build_report(lab, backend, meta, lambda name, n: job["steps"].append(name))
        job["steps"].append("Typeset the PDF report")
        pdf, pages = report_pdf.build_pdf(rep)
        job["steps"].append("Build the Word document")
        docx_ = report_docx.build_docx(rep, pages)
        job["steps"].append("Build the Excel workbook")
        job["files"] = {"pdf": pdf, "docx": docx_, "xlsx": report_xlsx.report_xlsx(rep)}
        job["summary"] = {"title": rep.title, "sections": len(rep.sections), "figures": len(rep.figures), "tables": len(rep.tables), "model": rep.model,
                          "findings": [{"title": s.title, "finding": s.key_finding} for s in rep.sections], "notes": rep.ai_notes}
        job["status"] = "done"
    except Exception as e:      # noqa: BLE001
        job["status"], job["message"] = "error", f"{type(e).__name__}: {e}"


@guard
async def api_report(request: Request):
    if (g := page_gate(request, "reports")):
        return g
    lab = lab_of(request)
    if not lab.loaded:
        return err(409, "no_data", "Upload data first.")
    b = await request.json()
    meta = {k: str(b.get(k, ""))[:140] for k in ("title", "subtitle", "organisation", "prepared_for", "prepared_by")}
    jid = uuid.uuid4().hex[:12]
    while len(JOBS) >= 10:
        JOBS.pop(next(iter(JOBS)))
    JOBS[jid] = {"status": "running", "steps": [], "files": {}, "session": sid(request)}
    backend = ai_backend()
    threading.Thread(target=_run_report, args=(jid, lab, meta, backend), daemon=True).start()
    log(request, "[report] generate", tools=["report"], mode="ai" if backend else "rules", cost=0.03 if backend else 0)
    return JSONResponse({"id": jid})


def _job(request: Request):
    j = JOBS.get(request.query_params.get("id", ""))
    return j if j and j["session"] == sid(request) else None


@guard
def api_report_status(request: Request):
    if (g := page_gate(request, "reports")):
        return g
    job = _job(request)
    if not job:
        return err(404, "none", "Report not found. It may have expired.")
    return JSONResponse({"status": job["status"], "steps": job["steps"], "message": job.get("message"), "summary": job.get("summary"), "formats": list(job["files"])})


@guard
def api_report_download(request: Request):
    if (g := export_gate(request)):
        return g
    job, fmt = _job(request), request.query_params.get("fmt", "pdf")
    if not job or fmt not in job["files"]:
        return err(404, "none", "That file is not available.")
    mt = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[fmt]
    return Response(job["files"][fmt], media_type=mt, headers={"Content-Disposition": f"attachment; filename=askshelf_report.{fmt}"})


# ------------------------------------------------------------------------------------------ governance, feedback, knowledge
@guard
def api_governance(request: Request):
    if (g := page_gate(request, "governance")):
        return g
    ok, n, bad = audit.verify()
    rows = []
    for e in reversed(audit.read()[-100:]):
        if "corrupt" in e:
            continue
        rows.append({"time": e["ts"].replace("T", " ").replace("+00:00", " UTC"), "role": e["role"], "question": e["question"], "outcome": e["outcome"], "tools": ", ".join(e["tools"]),
                     "mode": e["mode"], "latency_ms": e["latency_ms"], "cost": e["est_cost_usd"], "hash": e["hash"][:10]})
    fs = feedback.summary()
    import json
    return JSONResponse({"chain": {"intact": ok, "entries": n, "bad_line": bad}, "audit": rows, "matrix": json.loads(rbac.access_matrix().to_json(orient="split", index=False)),
                         "restricted": list(RESTRICTED_FIELDS.values()), "feedback": {"up": fs["up"], "down": fs["down"], "reports": fs["issues"] + fs["definitions"]}})


@guard
def api_audit_download(request: Request):
    if (g := export_gate(request)):
        return g
    return Response(audit.AUDIT_FILE.read_bytes() if audit.AUDIT_FILE.exists() else b"", media_type="application/x-ndjson", headers={"Content-Disposition": "attachment; filename=audit_log.jsonl"})


@guard
async def api_feedback(request: Request):
    b = await request.json()
    if b.get("kind", "rating") not in ("rating", "issue", "definition"):
        return err(400, "bad_kind", "Unknown feedback type.")
    feedback.log(b.get("kind", "rating"), rating=b.get("rating", ""), category=b.get("category", ""), comment=b.get("comment", ""), question=b.get("question", ""), tools_used=b.get("tools", ""), mode=b.get("mode", ""))
    return JSONResponse({"ok": True})


GLOSSARY = [
    ("On-time rate", "Share of delivered orders whose delivered date is on or before the due date."),
    ("Late risk / AUC", "A model trained on your own delivered orders estimates each open order's chance of finishing late. AUC measures how well it separates late from on-time orders in cross-validation: 0.50 is a coin flip, 1.00 is perfect. It needs at least 150 delivered orders."),
    ("Penalty exposure", "Late risk x order value x the penalty rate seen in your history (or an assumed rate you enter). A planning estimate, not a guarantee."),
    ("Days of cover", "Stock on hand divided by average units sold per day over the last 90 days. It shows how long stock lasts at the recent sales rate."),
    ("Suggested order", "Sales rate x target days of cover, minus stock on hand."),
    ("Forecast / MAPE", "Prophet projects a measure forward with an 80% range. MAPE is the typical size of its error on a hold-out of the most recent periods."),
    ("Gross margin", "(Sales - cost) / sales. Needs a unit cost or total cost column."),
    ("Elasticity", "How much volume falls when price rises. It is an assumption in the Scenario planner, because sales data alone cannot prove how customers react."),
    ("Column mapping", "The Data page guesses which of your columns is the date, amount, product, supplier and so on. Confirm or change it; every page uses these roles."),
]
FILES = [("Order or sales lines", "One row per order line: date, product, amount, quantity, supplier, store."),
         ("Optional fields", "Cost, due and delivered dates, penalty, stock on hand and reorder level unlock margins, deliveries, penalties and stock."),
         ("Formats", ".xlsx, .xls, .csv, .tsv up to 25 MB. Several files with the same columns are combined automatically.")]


@guard
def api_glossary(request: Request):
    return JSONResponse({"definitions": [{"term": t, "meaning": m} for t, m in GLOSSARY], "files": [{"file": a, "about": b} for a, b in FILES]})


def plotly_js(request: Request):
    import plotly
    return FileResponse(Path(plotly.__file__).parent / "package_data" / "plotly.min.js", media_type="text/javascript", headers={"Cache-Control": "public, max-age=86400"})


def asset_version() -> str:
    """Changes whenever any front-end file changes, so browsers never run stale JavaScript against a newer server."""
    return str(int(max((f.stat().st_mtime for f in (WEB / "static").rglob("*") if f.is_file()), default=0)))


class FreshStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"          # always revalidate; unchanged files still answer 304
        return resp


def page(name: str):
    def handler(request: Request):
        v = asset_version()
        html = (WEB / name).read_text(encoding="utf-8")
        html = html.replace('.css"', f'.css?v={v}"').replace('.js"', f'.js?v={v}"')
        return Response(html, media_type="text/html", headers={"Cache-Control": "no-store"})
    return handler


@asynccontextmanager
async def lifespan(_app):
    yield


routes = [
    Route("/", page("index.html")), Route("/app", page("app.html")), Route("/api/meta", api_meta),
    Route("/api/data", api_dataset), Route("/api/data/upload", api_upload, methods=["POST"]), Route("/api/data/sample", api_sample, methods=["POST"]), Route("/api/data/mapping", api_mapping, methods=["POST"]),
    Route("/api/data/reset", api_reset, methods=["POST"]), Route("/api/data/clear-memory", api_clear_memory, methods=["POST"]), Route("/api/data/export", api_export),
    Route("/api/ask", api_ask, methods=["POST"]), Route("/api/dashboard", api_dashboard), Route("/api/deliveries", api_deliveries), Route("/api/penalties", api_penalties, methods=["GET", "POST"]),
    Route("/api/stock", api_stock), Route("/api/forecast", api_forecast), Route("/api/planner", api_planner, methods=["POST"]), Route("/api/alerts", api_alerts), Route("/api/alerts/digest", api_digest),
    Route("/api/report", api_report, methods=["POST"]), Route("/api/report/status", api_report_status), Route("/api/report/download", api_report_download),
    Route("/api/governance", api_governance), Route("/api/audit/download", api_audit_download), Route("/api/feedback", api_feedback, methods=["POST"]), Route("/api/glossary", api_glossary),
    Route("/static/vendor/plotly.min.js", plotly_js), Mount("/static", FreshStatic(directory=WEB / "static"), name="static"),
]
app = Starlette(routes=routes, lifespan=lifespan)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=False)
