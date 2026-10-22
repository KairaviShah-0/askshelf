"""Data Lab question answering: plan (AI or rules) -> validated SQL / Prophet -> table + chart -> plain-English explanation."""
from __future__ import annotations

import json
import re
import time

import numpy as np
import pandas as pd

from . import datalab as D
from . import guardrails as G
from .gemini import _json_loads

PLAN_SYSTEM = """You are a careful senior data analyst. Translate the user's question about an uploaded dataset into ONE read-only SQLite query,
or a forecast request. Rules:
- Use ONLY the tables and columns listed in the schema. Never invent columns. Quote nothing unless needed.
- SQLite dialect. Date columns are TEXT 'YYYY-MM-DD': group with strftime('%Y-%m', col), strftime('%Y', col), strftime('%Y-Q', col) is NOT valid.
- Aggregates need GROUP BY for every other selected column. Give every computed column a clear snake_case alias.
- Rankings: ORDER BY the measure DESC and LIMIT (default 10). Never return more than 200 rows unless it is an aggregate.
- Percentages: 100.0 * part / NULLIF(whole, 0). Round money to 2 decimals with ROUND().
- Use a CTE or subquery instead of anything that writes. The query must be a single SELECT (or WITH ... SELECT).
- For trend questions return the time bucket first, then the measure(s).
- For distribution or relationship questions return the raw values for a RANDOM sample: ORDER BY RANDOM() LIMIT 2000. Never take the first N rows, which would only cover the earliest period.
- If the user asks to forecast / predict / project a measure over time, return action "forecast".
- If the question cannot be answered from this data, return action "none" with a short message.
Return JSON only:
{"action":"sql"|"forecast"|"none","title":"short chart title","sql":"SELECT ...","chart":{"type":"bar|line|scatter|hist","x":"column in result","y":"column in result"},
 "forecast":{"value":"numeric column","time":"date column","group":null,"periods":6,"unit":"month|week|day|quarter|year"},"message":""}"""

EXPLAIN_SYSTEM = """Do not state or assume a currency (no 'dollars' or '$') unless the column name says so; write plain numbers. You are a senior business analyst writing for a busy, non-technical manager. Use ONLY the numbers in the data provided; never invent figures.
Return JSON only: {"summary":"one-sentence bottom line with the key number","bullets":["3 to 5 short bullets with specific numbers, comparisons and what it means"],
"follow_ups":["3 useful follow-up questions the manager could ask next"]}. Australian spelling. No markdown symbols inside strings."""


def fmt_num(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(v) >= 1e9:
        return f"{v / 1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.2f}M"
    if abs(v) >= 1e3:
        return f"{v:,.0f}"
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"


def df_to_json(df: pd.DataFrame, limit: int = 200) -> dict:
    d = G.scrub_table(df.head(limit).copy()) if len(df) else df
    out = d.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d")
    out = out.astype(object).where(out.notna(), None)
    return {"columns": list(out.columns), "data": [[(round(v, 2) if isinstance(v, float) else v) for v in r] for r in out.values.tolist()], "total_rows": int(len(df))}


def stats_text(df: pd.DataFrame, max_rows: int = 25) -> str:
    parts = [f"ROWS: {len(df)}", "TABLE (csv):", df.head(max_rows).to_csv(index=False)]
    nums = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    for c in nums[:3]:
        parts.append(f"{c}: total={fmt_num(df[c].sum())}, mean={fmt_num(df[c].mean())}, min={fmt_num(df[c].min())}, max={fmt_num(df[c].max())}")
    return "\n".join(parts)


def offline_explain(df: pd.DataFrame, title: str = "") -> dict:
    if df.empty:
        return {"summary": "The query returned no rows.", "bullets": [], "follow_ups": []}
    times = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    nums = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    cats = [c for c in df.columns if c not in nums and c not in times]
    bullets, summary = [], f"{len(df):,} rows returned."
    nice = lambda c: c.replace("_", " ")
    is_avg = lambda c: bool(re.match(r"(avg|average|mean|rate|pct|percent|ratio|median)", c))
    if times and nums and len(df) > 2:
        t, m = times[0], nums[0]
        d = df.sort_values(t)
        first, last = float(d[m].iloc[0]), float(d[m].iloc[-1])
        pk, lo = d.loc[d[m].idxmax()], d.loc[d[m].idxmin()]
        chg = (last / first - 1) * 100 if first else None
        summary = f"{nice(m).capitalize()} moved from {fmt_num(first)} to {fmt_num(last)} over {len(d)} periods" + (f" ({chg:+.0f}%)." if chg is not None else ".")
        bullets = [f"Peak: {pk[t]:%Y-%m} at {fmt_num(pk[m])}.", f"Low point: {lo[t]:%Y-%m} at {fmt_num(lo[m])}.", f"Average per period: {fmt_num(d[m].mean())}."]
        if not is_avg(m):
            bullets.append(f"Total across the period: {fmt_num(d[m].sum())}.")
        if len(d) >= 6:
            h = len(d) // 2
            a1, a2 = d[m].iloc[:h].mean(), d[m].iloc[h:].mean()
            if a1:
                bullets.append(f"The second half averaged {fmt_num(a2)} per period versus {fmt_num(a1)} in the first half ({(a2 / a1 - 1) * 100:+.0f}%).")
    elif nums and cats and len(df) > 1:
        m, c = nums[0], cats[0]
        d = df.sort_values(m, ascending=False)
        top, bot = d.iloc[0], d.iloc[-1]
        tot = float(d[m].sum())
        share = (not is_avg(m)) and tot > 0
        summary = f"{top[c]} is highest on {nice(m)} at {fmt_num(top[m])}" + (f" ({top[m] / tot * 100:.0f}% of the total shown)." if share else f", versus {fmt_num(bot[m])} for {bot[c]}.")
        bullets = [f"Highest: {top[c]} at {fmt_num(top[m])}.", f"Lowest: {bot[c]} at {fmt_num(bot[m])}.", f"Gap between highest and lowest: {fmt_num(top[m] - bot[m])}."]
        bullets.append(f"Total across the {len(d)} rows shown: {fmt_num(tot)}." if share else f"Simple average of the {len(d)} values: {fmt_num(d[m].mean())}.")
        if share and len(d) >= 4:
            bullets.append(f"The top 3 account for {d[m].head(3).sum() / tot * 100:.0f}% of the total.")
    elif nums:
        c = nums[0]
        summary = f"{nice(c).capitalize()}: total {fmt_num(df[c].sum())}, average {fmt_num(df[c].mean())}."
        bullets = [f"Minimum {fmt_num(df[c].min())}, maximum {fmt_num(df[c].max())}, median {fmt_num(df[c].median())}."]
    return {"summary": summary, "bullets": bullets, "follow_ups": []}


def explain(backend, question: str, df: pd.DataFrame, title: str) -> tuple[dict, str]:
    if backend is not None and not df.empty:
        try:
            raw = backend.complete(EXPLAIN_SYSTEM, f"QUESTION: {question}\nCHART TITLE: {title}\n\n{stats_text(df)}", json_mode=True, temperature=0.2)
            d = _json_loads(raw) or {}
            if d.get("summary"):
                from .labreport import tidy_numbers
                clean = lambda t: tidy_numbers(G.sanitize_output(str(t))[0])
                return {"summary": clean(d["summary"]), "bullets": [clean(b) for b in d.get("bullets", [])][:6], "follow_ups": [str(f) for f in d.get("follow_ups", [])][:3]}, "ai"
        except Exception:       # noqa: BLE001 - never fail an answer because the narrator failed
            pass
    return offline_explain(df, title), "rules"


def plan_with_ai(backend, lab: D.Lab, question: str, history: list[dict], error: str | None = None, prev_sql: str | None = None) -> dict:
    ctx = "\n".join(f"{'Q' if h['role'] == 'user' else 'A'}: {h['text'][:200]}" for h in history[-4:])
    prompt = f"SCHEMA:\n{D.schema_text(lab)}\n\nRECENT CONVERSATION:\n{ctx or '(none)'}\n\nQUESTION: {question}"
    if error:
        prompt += f"\n\nYour previous query failed.\nPREVIOUS SQL: {prev_sql}\nERROR: {error}\nFix it and return the JSON again."
    d = _json_loads(backend.complete(PLAN_SYSTEM, prompt, json_mode=True, temperature=0.0))
    if not d or "action" not in d:
        raise ValueError("The AI did not return a usable plan.")
    return d


def lab_answer(lab: D.Lab, question: str, history: list[dict] | None = None, backend=None) -> dict:
    """Answer one question about the uploaded data. Always returns a dict (errors become a readable `error` field)."""
    t0, trace = time.time(), []
    history = history or []
    v = G.check_input(question)
    if not v.allowed:
        return {"blocked": True, "text": v.message, "trace": [{"step": "Guardrail check", "detail": f"blocked ({v.reason})", "ms": 0}]}
    trace.append({"step": "Guardrail check", "detail": "passed", "ms": int((time.time() - t0) * 1000)})
    if not lab.loaded:
        return {"error": "Upload a file first, then ask your question."}
    key = (question.strip().lower(), len(lab.frames), sum(len(f["df"]) for f in lab.frames), bool(backend))
    if key in lab.cache:
        return lab.cache[key]

    t1, plan, mode, sql_err = time.time(), None, "rules", None
    if backend is not None:
        try:
            plan, mode = plan_with_ai(backend, lab, question, history), "ai"
            trace.append({"step": "Plan the analysis (AI)", "detail": f"{plan.get('action')}: {plan.get('title', '')}", "ms": int((time.time() - t1) * 1000)})
        except Exception as e:      # noqa: BLE001
            trace.append({"step": "Plan the analysis (AI)", "detail": f"unavailable ({type(e).__name__}); used built-in rules", "ms": int((time.time() - t1) * 1000)})
    if plan is None:
        plan = D.heuristic_plan(lab, question)
        trace.append({"step": "Plan the analysis (rules)", "detail": f"{plan['action']}: {plan.get('title', '')}", "ms": int((time.time() - t1) * 1000)})
    if plan.get("action") == "none":
        return {"error": plan.get("message") or "That cannot be answered from the uploaded data.", "trace": trace}

    out = {"title": plan.get("title") or "Result", "mode": mode, "trace": trace, "question": question}
    base = D.pick_columns(lab, question)
    # ---------- forecast
    if plan.get("action") == "forecast":
        f = plan.get("forecast") or plan
        tdf = base["df"]
        val = f.get("value") if f.get("value") in tdf.columns else base["measure"]
        tcol = f.get("time") if f.get("time") in tdf.columns else base["time"]
        if not val or not tcol:
            return {"error": "I need a date column and a numeric column to forecast. This data doesn't have both.", "trace": trace}
        unit = f.get("unit") or "month"
        freq = D.FREQ_WORDS.get(str(unit).rstrip("s"), f.get("freq")) if not f.get("freq") else f.get("freq")
        t2 = time.time()
        try:
            grp = f.get("group") if f.get("group") in tdf.columns else None
            res = D.forecast_series(tdf, tcol, val, int(f.get("periods") or 6), freq)
            gtab = D.forecast_groups(tdf, tcol, val, grp, res["periods"], 5, res["freq"]) if grp else None
        except ValueError as e:
            return {"error": str(e), "trace": trace}
        fc = res["forecast"].copy()
        tab = fc.rename(columns={"ds": res["unit"], "yhat": "forecast", "lo": "low_80", "hi": "high_80"})
        tab[res["unit"]] = tab[res["unit"]].dt.strftime("%Y-%m-%d")
        spec = {"type": "line", "x": "ds", "y": "y", "color": None}
        import plotly.graph_objects as go
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=res["history"]["ds"], y=res["history"]["y"], name="Actual", mode="lines+markers", line=dict(color=D.PALETTE[0], width=2.4), marker=dict(size=5)))
        fig.add_trace(go.Scatter(x=pd.concat([fc["ds"], fc["ds"][::-1]]), y=pd.concat([fc["hi"], fc["lo"][::-1]]), fill="toself", fillcolor="rgba(237,161,0,.18)", line=dict(width=0), name="80% range", hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=fc["ds"], y=fc["yhat"], name="Forecast", mode="lines+markers", line=dict(color="#d97706", width=2.6, dash="dash")))
        fig.update_layout(title=dict(text=f"Forecast of {val.replace('_', ' ')} (next {res['periods']} {res['unit']}s)", x=0, font=dict(size=15)), height=400, margin=dict(l=8, r=16, t=56, b=8),
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font=dict(family="Inter, system-ui, sans-serif", size=12, color="#52514e"),
                          legend=dict(orientation="h", y=1.1, x=0), yaxis=dict(gridcolor="#e1e0d9"), xaxis=dict(showgrid=False))
        sm = {"summary": f"Expected {val.replace('_', ' ')} over the next {res['periods']} {res['unit']}s is {fmt_num(fc['yhat'].sum())}"
                         + (f", {res['change_pct']:+.1f}% versus the previous {res['periods']} {res['unit']}s." if res["change_pct"] is not None else "."),
              "bullets": [f"Method: {res['method']} on {res['n_points']} {res['unit']}s of history.",
                          f"Back-test error (MAPE) on the last {max(2, min(6, res['n_points'] // 4))} {res['unit']}s: {res['mape']}%." if res["mape"] is not None else "Back-test error could not be computed.",
                          f"Peak forecast {res['unit']}: {fc.loc[fc['yhat'].idxmax(), 'ds']:%Y-%m-%d} at {fmt_num(fc['yhat'].max())}.",
                          "Shaded band is an 80% prediction range, not a guarantee; treat the forecast as a planning range."], "follow_ups": []}
        out |= {"kind": "forecast", "table": df_to_json(tab), "chart": json.loads(fig.to_json()), "explanation": sm, "forecast": {"mape": res["mape"], "method": res["method"], "unit": res["unit"], "change_pct": res["change_pct"]},
                "group_table": df_to_json(gtab) if gtab is not None else None, "sql": None}
        out["trace"].append({"step": "Forecast", "detail": f"{res['method']} on {val} by {res['unit']}", "ms": int((time.time() - t2) * 1000)})
        out["follow_ups"] = ["Which product category is growing fastest?", "What drove the biggest month?", "Show the top 5 by total"]
        lab.last_result = {"title": out["title"], "table": tab, "question": question, "sql": None}
        lab.qa_log.append({"question": question, "title": out["title"], "sql": None, "rows": len(tab), "mode": mode})
        lab.cache[key] = out
        return out

    # ---------- SQL (with one AI self-repair attempt)
    sql, df, err = plan.get("sql", ""), None, None
    for attempt in range(2):
        t2 = time.time()
        try:
            df = D.run_select(lab, sql)
            trace.append({"step": "Run query (read-only)", "detail": f"{len(df):,} rows" + (" (after fixing the first attempt)" if attempt else ""), "ms": int((time.time() - t2) * 1000)})
            err = None
            break
        except (ValueError, D.UnsafeSQL) as e:
            err = str(e)
            trace.append({"step": "Run query (read-only)", "detail": f"rejected: {err[:140]}", "ms": int((time.time() - t2) * 1000)})
            if attempt == 0 and backend is not None and mode == "ai":
                try:
                    plan = plan_with_ai(backend, lab, question, history, err, sql)
                    sql = plan.get("sql", "")
                    continue
                except Exception:       # noqa: BLE001
                    pass
            if attempt == 0 and mode == "ai":
                plan = D.heuristic_plan(lab, question)
                if plan.get("action") == "sql":
                    sql, mode = plan["sql"], "rules"
                    trace.append({"step": "Fallback", "detail": "used built-in rules after the AI query failed", "ms": 0})
                    continue
            break
    if df is None:
        return {"error": f"I couldn't run that analysis: {err}", "sql": sql, "trace": trace}
    spec = D.chart_spec(df, plan.get("chart"))
    fig = D.plotly_fig(df, spec, out["title"]) if spec else None
    t3 = time.time()
    ex, how = explain(backend, question, df, out["title"])
    trace.append({"step": "Explain the result", "detail": "AI" if how == "ai" else "built-in summary", "ms": int((time.time() - t3) * 1000)})
    ex["follow_ups"] = ex.get("follow_ups") or ["Break this down by region", "Show the monthly trend", "What are the bottom 5?"]
    out |= {"kind": "sql", "sql": D.clean_sql(sql), "table": df_to_json(df), "chart": json.loads(fig.to_json()) if fig is not None else None, "explanation": ex,
            "follow_ups": ex["follow_ups"], "mode": mode}
    lab.last_result = {"title": out["title"], "table": df, "question": question, "sql": out["sql"]}
    lab.qa_log.append({"question": question, "title": out["title"], "sql": out["sql"], "rows": len(df), "mode": mode})
    lab.cache[key] = out
    return out
