"""Advanced analysis tools: predictive risk model, forecasts, what-if simulator, capacity and pricing strategy,
profit bridge, anomaly scan, quote funnel and the executive briefing.

Same contract as tools.py: fixed, vetted calculations (never LLM-written code); every result carries facts, insights,
recommended actions, a chart and a table. Models live in predict.py and only ever see allow-listed, non-identifying data.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from . import data as D
from . import predict as P
from .config import CAT, GRID, INK, INK_2, MUTED, PRODUCT_COLORS, PRODUCTS, SURFACE
from .tools import (Period, ToolResult, _created, _delivered, _kpis, _ledger, _wip_daily, business_summary, money, num, pct,
                    resolve_period, style)

GOOD, WARN, BAD = "#1baf7a", "#eda100", "#d6453a"
METRIC_LABELS = {
    "revenue": ("Cash revenue", "$"), "costs": ("Cash costs", "$"), "profit": ("Cash profit", "$"), "quotes": ("Quotes issued", "n"),
    "accepted": ("Accepted quotes", "n"), "win_rate": ("Win rate", "%"), "on_time": ("On-time delivery rate", "%"),
}


def _fmt(v: float, kind: str) -> str:
    return money(v) if kind == "$" else pct(v, 0) if kind == "%" else num(v, 0)


def _wilson(k: int, n: int, z: float = 1.28) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def _waterfall(labels: list[str], values: list[float], first: str, last: str, title: str, height: int = 420) -> go.Figure:
    x = [first] + labels + [last]
    fig = go.Figure(go.Waterfall(
        x=x, measure=["absolute"] + ["relative"] * (len(x) - 2) + ["total"], y=list(values) + [None],
        connector=dict(line=dict(color=GRID)), increasing=dict(marker=dict(color=GOOD)), decreasing=dict(marker=dict(color=BAD)),
        totals=dict(marker=dict(color=CAT[0])), texttemplate="%{y:$,.0f}", textposition="outside", cliponaxis=False))
    fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    return style(fig, title, height, legend=False)


# ============================================================================================ 1. late-delivery risk radar
def late_risk_radar(top_n: int = 10) -> ToolResult:
    ds = D.load()
    lm = P.late_model()
    op = ds.orders[ds.orders["in_progress"]].reset_index(drop=True).copy()
    if op.empty:
        return ToolResult("late_risk_radar", "Late-delivery risk radar", "now", ["There are no orders in progress."])
    op["risk"] = lm.predict(op)
    base = lm.base_rate
    op["level"] = np.where(op["risk"] >= 2 * base, "High", np.where(op["risk"] >= base, "Elevated", "Low"))
    op["exposure"] = op["risk"] * op["quotePrice"] * 0.5 * 0.10
    op = op.sort_values("risk", ascending=False)
    n = int(min(max(top_n or 10, 3), 20))
    show = op.head(n)
    color = {"High": BAD, "Elevated": WARN, "Low": GOOD}

    fig = go.Figure(go.Bar(
        x=show["risk"][::-1], y=[f"Order {i} - {p}" for i, p in zip(show["id"][::-1], show["productType"][::-1])], orientation="h",
        marker=dict(color=[color[l] for l in show["level"][::-1]], cornerradius=4), text=[pct(r, 0) for r in show["risk"][::-1]],
        textposition="outside", cliponaxis=False, hovertemplate="%{y}: %{x:.0%} chance of being late<extra></extra>"))
    fig.add_vline(x=base, line=dict(color=MUTED, dash="dash"), annotation_text=f"Typical: {pct(base, 0)}", annotation_position="bottom right")
    fig.update_xaxes(tickformat=".0%", range=[0, min(1.0, float(show["risk"].max()) * 1.25 + 0.05)])
    style(fig, "Orders in progress ranked by chance of finishing late", 60 + 34 * len(show) + 60, legend=False)

    # risk vs shop load: empirical late rate by workload bucket + model curve
    d = ds.orders[ds.orders["delivered"]].reset_index(drop=True)
    X = lm.features(d)
    d["load"] = P._ctx_at(d["acceptanceDate"])["wip"].values
    d["pred"] = lm.model.predict_proba(X)
    d["bucket"] = pd.qcut(d["load"], 5, duplicates="drop")
    g = d.groupby("bucket", observed=True).agg(load=("load", "mean"), late=("is_late", "mean"), pred=("pred", "mean"), n=("id", "count")).reset_index(drop=True)
    f2 = go.Figure()
    f2.add_trace(go.Bar(x=[f"{v:.0f}" for v in g["load"]], y=g["late"], name="Actual late rate", marker=dict(color=CAT[0], cornerradius=4),
                        text=[pct(v, 0) for v in g["late"]], textposition="outside", cliponaxis=False))
    f2.add_trace(go.Scatter(x=[f"{v:.0f}" for v in g["load"]], y=g["pred"], name="Model estimate", mode="lines+markers", line=dict(color=CAT[1], width=2)))
    f2.update_yaxes(tickformat=".0%", title="Share delivered late")
    f2.update_xaxes(title="Average orders in production when the order started")
    style(f2, "Lateness climbs steeply once the workshop fills up", 380)

    show = show.assign(currentStage=show["currentStage"].where(show["currentStage"].notna(), "In progress"))
    tab = show[["id", "productType", "currentStage", "dueDate", "risk", "level", "exposure"]].copy()
    tab.columns = ["Order", "Product", "Stage", "Due date", "Late risk", "Risk level", "Penalty exposure"]
    tab["Due date"] = tab["Due date"].dt.strftime("%d %b %Y")
    tab["Late risk"] = tab["Late risk"].map(lambda v: pct(v, 0))
    tab["Penalty exposure"] = tab["Penalty exposure"].map(money)

    exp_late = float(op["risk"].sum())
    high = op[op["level"] == "High"]
    lo_b, hi_b = g.iloc[0], g.iloc[-1]
    drivers = lm.model.drivers().head(5).copy()
    drivers["effect"] = drivers["effect"].round(2)
    drivers.columns = ["Factor", "Pull toward late (higher = riskier)"]
    wip = _wip_daily()
    facts = [
        f"{int(wip.iloc[-1])} orders are in production now against a two-year peak of {int(wip.max())} and an average of {wip.mean():.0f}.",
        f"{len(op)} orders are in progress; the model expects about {exp_late:.1f} of them to finish late (typical rate {pct(base, 0)}).",
        f"{len(high)} orders are high risk (at least double the typical rate); together they carry about {money(high['exposure'].sum())} of expected penalty.",
        f"Highest-risk order: Order {int(op.iloc[0]['id'])} ({op.iloc[0]['productType']}, {(str(op.iloc[0]['currentStage']).title() if isinstance(op.iloc[0]['currentStage'], str) else 'In progress')}) at {pct(op.iloc[0]['risk'], 0)}.",
    ]
    insights = [
        f"Workshop load is the dominant driver: orders that started when about {hi_b['load']:.0f} jobs were in production were late {pct(hi_b['late'], 0)} of the time, versus {pct(lo_b['late'], 0)} when about {lo_b['load']:.0f} were.",
        f"Tight schedules matter too: the fewer days between acceptance and the due date, the higher the late risk.",
        f"The model separates late from on-time orders well (cross-validated AUC {lm.auc:.2f} on {lm.n} delivered orders; 0.50 is a coin flip).",
    ]
    actions = [
        "Review the High-risk orders first: confirm finishing capacity this week and warn those customers before the due date.",
        f"Treat about {hi_b['load']:.0f}+ concurrent orders as the amber line: when the shop is that full, quote longer lead times or pause new acceptances.",
    ]
    return ToolResult("late_risk_radar", "Late-delivery risk radar", f"orders in progress at {ds.as_of:%d %b %Y}", facts, tab, fig, [],
                      ["Risk is estimated from the workshop load when each order was accepted, available makers, days allowed, hours and product; "
                       "it is a probability, not a certainty."], ["orders_data.csv", "hr_events_log.csv", "hr_roster.csv"], {"top_n": n},
                      insights, actions, [("Load vs lateness", f2)], [("What pushes an order toward late", drivers)],
                      f"Logistic model, 5-fold cross-validated AUC {lm.auc:.2f}, {lm.n} delivered orders.")


# ============================================================================================ 2. forecast outlook
def forecast_outlook(metric: str = "revenue", horizon_months: int = 3) -> ToolResult:
    key = (metric or "revenue").lower().replace(" ", "_").replace("-", "_")
    alias = {"sales": "revenue", "demand": "quotes", "orders": "accepted", "win": "win_rate", "winrate": "win_rate", "ontime": "on_time",
             "on_time_rate": "on_time", "cost": "costs", "expenses": "costs", "cash": "profit"}
    key = alias.get(key, key)
    if key not in METRIC_LABELS:
        key = "revenue"
    label, kind = METRIC_LABELS[key]
    h = int(min(max(horizon_months or 3, 1), 6))
    fc = P.forecast(key, h)
    hist, fut = fc.history, fc.future
    hx, fx = [p.to_timestamp() for p in hist.index], [p.to_timestamp() for p in fut.index]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=fx + fx[::-1], y=list(fut["hi"]) + list(fut["lo"][::-1]), fill="toself", fillcolor="rgba(235,104,52,0.15)",
                             line=dict(width=0), name="80% range", hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=hx, y=hist.values, name="Actual", mode="lines+markers", line=dict(color=CAT[0], width=2.5), marker=dict(size=6, line=dict(color=SURFACE, width=2))))
    fig.add_trace(go.Scatter(x=[hx[-1]] + fx, y=[hist.iloc[-1]] + list(fut["forecast"]), name="Forecast", mode="lines+markers",
                             line=dict(color=CAT[1], width=2.5, dash="dash"), marker=dict(size=7, line=dict(color=SURFACE, width=2))))
    fig.update_xaxes(type="date", tickformat="%b %Y", dtick="M3", tickangle=0)
    fig.update_yaxes(tickprefix="$" if kind == "$" else "", tickformat=".0%" if kind == "%" else ",.0f", rangemode="tozero" if key != "profit" else "normal")
    style(fig, f"{label}: history and {h}-month forecast with 80% range", 420)

    tab = pd.DataFrame({"Month": [p.strftime("%b %Y") for p in fut.index], "Forecast": [_fmt(v, kind) for v in fut["forecast"]],
                        "Low (10%)": [_fmt(v, kind) for v in fut["lo"]], "High (90%)": [_fmt(v, kind) for v in fut["hi"]],
                        "Same month last year": [_fmt(hist.get(p - 12, np.nan), kind) if (p - 12) in hist.index else "n/a" for p in fut.index]})
    bt = fc.backtest.copy()
    bt["Typical error"] = bt["Typical error"].map(lambda v: _fmt(v, kind))
    bt["Error %"] = bt["Error %"].map(lambda v: pct(v, 0))
    bt.insert(0, "Chosen", ["yes" if m == fc.method else "" for m in bt["Method"]])

    best_err = fc.backtest.iloc[0]["Typical error"]
    total = fut["forecast"].sum() if kind in ("$", "n") else fut["forecast"].mean()
    prev = sum(hist.get(p - 12, np.nan) for p in fut.index)
    prev = prev if kind in ("$", "n") else np.nanmean([hist.get(p - 12, np.nan) for p in fut.index])
    verb = "total" if kind in ("$", "n") else "average"
    facts = [f"Next {h} months ({fut.index[0].strftime('%b %Y')} - {fut.index[-1].strftime('%b %Y')}): {verb} {label.lower()} of about {_fmt(total, kind)} "
             f"(80% range {_fmt(fut['lo'].sum() if kind in ('$', 'n') else fut['lo'].mean(), kind)} to {_fmt(fut['hi'].sum() if kind in ('$', 'n') else fut['hi'].mean(), kind)})."]
    if not np.isnan(prev) and prev:
        facts.append(f"That is {(total / prev - 1) * 100:+.0f}% versus the same months last year ({_fmt(prev, kind)}).")
    by_month = hist.groupby([p.month for p in hist.index]).mean()
    insights = [f"Best method for this metric: '{fc.method}'. In {fc.windows} rolling back-tests it missed by about {_fmt(best_err, kind)} a month on average.",
                f"Seasonality: the strongest month in the history is {pd.Timestamp(2000, int(by_month.idxmax()), 1):%B} and the weakest is {pd.Timestamp(2000, int(by_month.idxmin()), 1):%B}."]
    actions = []
    if key in ("quotes", "accepted", "revenue"):
        b = P.baseline()
        qf = fut["forecast"].sum() / h if key == "quotes" else None
        if key == "quotes" and qf:
            sim = P.simulate(P.Scenario(quote_volume_change_pct=(qf / b["quotes_pm"] - 1) * 100))
            insights.append(f"Capacity check: if quotes arrive at the forecast rate ({qf:.0f}/month vs {b['quotes_pm']:.0f} over the last 12 months), "
                            f"about {sim['orders']:.0f} orders a month would be accepted and on-time delivery would be roughly {pct(sim['on_time'], 0)}.")
            if sim["on_time"] < P.simulate(P.Scenario())["on_time"] - 0.02:
                actions.append("Demand is heading above what the current team delivers on time: plan overtime or a temporary maker before the peak (see Capacity planner).")
    actions.append("Use the low end of the range for cash planning and the high end for capacity planning.")
    conf = (f"Method chosen automatically by rolling back-test over {fc.windows} windows; the 80% range is the empirical 80th-percentile miss. "
            "Only 24 months of history exist, so treat this as a planning range, not a promise.")
    return ToolResult("forecast_outlook", f"{label} outlook", f"{hist.index[0].strftime('%b %Y')} - {hist.index[-1].strftime('%b %Y')} + {h} months",
                      facts, tab, fig, [], [conf], ["orders_data.csv", "financial_ledger.csv"], {"metric": key, "horizon_months": h}, insights, actions,
                      [], [("How each forecasting method scored in back-testing", bt)], conf)


# ============================================================================================ 3. what-if simulator
def _sc(price_change_pct=0.0, quote_volume_change_pct=0.0, extra_makers=0.0, timber_cost_change_pct=0.0, price_elasticity=None) -> P.Scenario:
    def f(v, lo, hi, d=0.0):
        try:
            return float(min(max(float(v), lo), hi))
        except (TypeError, ValueError):
            return d
    return P.Scenario(f(price_change_pct, -30, 40), f(quote_volume_change_pct, -50, 100), f(extra_makers, -3, 6), f(timber_cost_change_pct, -30, 50),
                      None if price_elasticity in (None, "") else f(price_elasticity, -4, 1))


def scenario_simulator(price_change_pct: float = 0.0, quote_volume_change_pct: float = 0.0, extra_makers: float = 0.0,
                       timber_cost_change_pct: float = 0.0, price_elasticity: float | None = None) -> ToolResult:
    sc = _sc(price_change_pct, quote_volume_change_pct, extra_makers, timber_cost_change_pct, price_elasticity)
    base, scn = P.sim_baseline(), P.simulate(sc)
    b = P.baseline()
    el = P.price_elasticity()
    steps = [("Start: current monthly profit", base["profit"])]
    cum = P.Scenario()
    for name, upd in [("Price change", dict(price_change_pct=sc.price_change_pct, price_elasticity=sc.price_elasticity)),
                      ("Quote volume", dict(quote_volume_change_pct=sc.quote_volume_change_pct)),
                      ("Extra makers", dict(extra_makers=sc.extra_makers)), ("Timber cost", dict(timber_cost_change_pct=sc.timber_cost_change_pct))]:
        prev = P.simulate(cum)["profit"]
        for k, v in upd.items():
            setattr(cum, k, v)
        now = P.simulate(cum)["profit"]
        if abs(now - prev) > 1:
            steps.append((name, now - prev))
    labels = [s[0] for s in steps[1:]]
    if labels:
        fig = _waterfall(labels, [steps[0][1]] + [s[1] for s in steps[1:]], steps[0][0], "Scenario monthly profit", "How the scenario changes monthly profit")
    else:
        fig = go.Figure(go.Bar(x=["Current", "Scenario"], y=[base["profit"], scn["profit"]], marker=dict(color=[CAT[0], CAT[1]], cornerradius=4)))
        fig.update_yaxes(tickprefix="$", tickformat=",.0f")
        style(fig, "Monthly profit: current vs scenario", 380, legend=False)

    def row(label, k, fmt):
        return {"Metric (per month)": label, "Current": fmt(base[k]), "Scenario": fmt(scn[k]),
                "Change": (f"{scn[k] - base[k]:+,.0f}" if fmt is num0 else f"{(scn[k] - base[k]) * 100:+.1f} pp" if fmt is pct1 else
                           f"{'+' if scn[k] >= base[k] else '-'}${abs(scn[k] - base[k]):,.0f}")}
    tab = pd.DataFrame([row("Quotes issued", "quotes", num0), row("Win rate", "win_rate", pct1), row("Orders accepted", "orders", num0),
                        row("On-time delivery", "on_time", pct1), row("Revenue after penalties", "revenue", money), row("Late penalties", "penalties", money),
                        row("Total costs", "costs", money), row("Profit", "profit", money)])

    stress = []
    if sc.price_change_pct:
        for lab, e in [("Data-estimated sensitivity", None), ("Moderately price-sensitive (-1.0)", -1.0), ("Highly price-sensitive (-2.0)", -2.0)]:
            s2 = P.Scenario(**{**sc.__dict__, "price_elasticity": e})
            r = P.simulate(s2)
            stress.append({"If customers are...": lab, "Win rate": pct(r["win_rate"], 1), "Monthly profit": money(r["profit"]), "vs current": money(r["profit"] - base["profit"])})
    dp = scn["profit"] - base["profit"]
    desc = [t for t, v in [(f"prices {sc.price_change_pct:+.0f}%", sc.price_change_pct), (f"quote volume {sc.quote_volume_change_pct:+.0f}%", sc.quote_volume_change_pct),
                           (f"{sc.extra_makers:+.0f} makers", sc.extra_makers), (f"timber cost {sc.timber_cost_change_pct:+.0f}%", sc.timber_cost_change_pct)] if v]
    facts = [f"Scenario ({', '.join(desc) or 'no change'}): monthly profit moves from {money(base['profit'])} to {money(scn['profit'])} ({'+' if dp >= 0 else '-'}${abs(dp):,.0f}).",
             f"On-time delivery goes from {pct(base['on_time'], 0)} to {pct(scn['on_time'], 0)}; win rate from {pct(base['win_rate'], 1)} to {pct(scn['win_rate'], 1)}."]
    insights = [f"Price sensitivity in the data is {el['estimate']:+.2f} (80% interval {el['lo']:+.2f} to {el['hi']:+.2f}): statistically indistinguishable from zero, "
                "so the data cannot prove that customers walk away when prices rise, nor that they will not."]
    if sc.quote_volume_change_pct > 0:
        insights.append(f"More volume fills the workshop (about {scn['wip']:.0f} orders in production vs {base['wip']:.0f}), which is what drives late deliveries in the data.")
    if sc.extra_makers:
        insights.append(f"Each extra maker costs about {money(b['maker_rate'] * 8 * 30.4)} a month; the model credits them only through lower workshop load per maker.")
    actions = ["Run this scenario again with a more price-sensitive assumption before committing to a price change." if sc.price_change_pct else
               "Change one lever at a time to see which one moves profit most, then combine them."]
    notes = [f"Baseline = the last 12 complete months ({b['start']:%b %Y} - {b['end']:%b %Y}). The simulated baseline profit of {money(base['profit'])}/month compares with "
             f"{money(b['profit_pm_actual'])}/month actual (timing differences between cash and orders)."]
    return ToolResult("scenario_simulator", "What-if scenario", f"steady-state month vs last 12 months", facts, tab, fig, [], notes,
                      ["orders_data.csv", "financial_ledger.csv"], {"price_change_pct": sc.price_change_pct, "quote_volume_change_pct": sc.quote_volume_change_pct,
                                                                   "extra_makers": sc.extra_makers, "timber_cost_change_pct": sc.timber_cost_change_pct},
                      insights, actions, [], [("Price-sensitivity stress test", pd.DataFrame(stress))] if stress else [],
                      "Transparent unit economics + the fitted late-delivery model. Price effects rest on an elasticity the data cannot pin down; staffing effects are assumption-based.")


def num0(v):
    return f"{v:,.0f}"


def pct1(v):
    return pct(v, 1)


# ============================================================================================ 4. capacity planner
def capacity_planner() -> ToolResult:
    vols = [-10, 0, 15, 30, 50]
    ms = [0, 1, 2, 3]
    prof = np.array([[P.simulate(P.Scenario(quote_volume_change_pct=v, extra_makers=m))["profit"] for v in vols] for m in ms])
    ont = np.array([[P.simulate(P.Scenario(quote_volume_change_pct=v, extra_makers=m))["on_time"] for v in vols] for m in ms])
    ylab = [f"{m:+d} makers" if m else "Current team" for m in ms]
    xlab = [f"{v:+d}% quotes" if v else "Current demand" for v in vols]
    fig = go.Figure(go.Heatmap(z=prof, x=xlab, y=ylab, colorscale=[[0, "#f7d5d1"], [0.5, "#fff6e5"], [1, "#b9e8d5"]], zmid=0,
                               text=[[money(v) for v in r] for r in prof], texttemplate="%{text}", hovertemplate="%{y}, %{x}: %{text}<extra></extra>", showscale=False))
    style(fig, "Monthly profit by demand level and team size", 360, legend=False)
    fig.update_yaxes(autorange="reversed")
    f2 = go.Figure(go.Heatmap(z=ont, x=xlab, y=ylab, colorscale=[[0, "#f7d5d1"], [0.85, "#fff6e5"], [1, "#b9e8d5"]], zmin=0.6, zmax=1.0,
                              text=[[pct(v, 0) for v in r] for r in ont], texttemplate="%{text}", hovertemplate="%{y}, %{x}: %{text}<extra></extra>", showscale=False))
    style(f2, "On-time delivery by demand level and team size", 360, legend=False)
    f2.update_yaxes(autorange="reversed")
    tab = pd.DataFrame([{"Team": ylab[i], **{xlab[j]: f"{money(prof[i, j])} | {pct(ont[i, j], 0)} on time" for j in range(len(vols))}} for i in range(len(ms))])
    best = [(vols[j], ms[int(np.argmax(prof[:, j]))]) for j in range(len(vols))]
    cost = P.baseline()["maker_rate"] * 8 * 30.4
    facts = [f"Current team at current demand: {money(prof[0, 1])} profit a month with {pct(ont[0, 1], 0)} on time.",
             f"If demand grows {vols[3]}%, the current team falls to {pct(ont[0, 3], 0)} on time; adding 2 makers changes that to {pct(ont[2, 3], 0)}.",
             f"One extra maker costs about {money(cost)} a month."]
    growth = [(v, m) for v, m in best if v >= 15 and m > 0]
    need = []
    for j, v in enumerate(vols):
        ok = [m for i, m in enumerate(ms) if ont[i, j] >= 0.90]
        need.append(f"{v:+d}% quotes -> {ok[0] if ok else '4+'}" if v > 0 else None)
    insights = ["Makers needed to keep 90% of orders on time: " + "; ".join(n for n in need if n) + "."]
    if max(m for _, m in best) == 0:
        insights.append("Late penalties alone (5% of revenue on late orders) are smaller than a maker's wage, so hiring is justified by protecting customer relationships and repeat business, which the data cannot measure.")
    else:
        insights.append("At today's demand an extra maker mostly adds cost; the payoff appears only when demand pushes the workshop past its comfortable load.")
    actions = ["Use temporary or overtime capacity first if a demand peak is forecast; hire permanently only if the higher demand is sustained." + (
        f" The data supports hiring once demand runs about {growth[0][0]}% above today's level." if growth else "")]
    return ToolResult("capacity_planner", "Capacity and hiring planner", "steady-state month, last-12-month baseline", facts, tab, fig, [],
                      ["Staffing effects are assumption-based: headcount barely changed in the data (7-8 makers), so the model treats an extra maker as lowering workload per maker."],
                      ["orders_data.csv", "financial_ledger.csv", "hr_events_log.csv"], {}, insights, actions, [("On-time delivery", f2)], [],
                      "Scenario grid from the what-if simulator; staffing effect is an assumption, not observed.")


# ============================================================================================ 5. pricing strategy
def price_strategy_analysis() -> ToolResult:
    ds = D.load()
    o = ds.orders
    el = P.price_elasticity()
    wm = P.win_model()
    base = P.sim_baseline()
    grid = list(range(-10, 21, 2))
    assumptions = [("Data-estimated sensitivity", None, CAT[0]), ("Moderately price-sensitive (-1.0)", -1.0, CAT[3]), ("Highly price-sensitive (-2.0)", -2.0, CAT[1])]
    fig = go.Figure()
    curves = {}
    for lab, e, colr in assumptions:
        ys = [P.simulate(P.Scenario(price_change_pct=g, price_elasticity=e))["profit"] for g in grid]
        curves[lab] = ys
        fig.add_trace(go.Scatter(x=grid, y=ys, name=lab, mode="lines", line=dict(color=colr, width=2.5)))
    fig.add_hline(y=base["profit"], line=dict(color=MUTED, dash="dot"))
    fig.update_xaxes(title="Price change (%)", ticksuffix="%")
    fig.update_yaxes(title="Monthly profit", tickprefix="$", tickformat=",.0f")
    style(fig, "Monthly profit vs price change under three customer-reaction assumptions", 420)

    qb = pd.qcut(o["quotePrice"], 5, duplicates="drop")
    g = o.groupby(qb, observed=True).agg(win=("won", "mean"), k=("won", "sum"), n=("won", "size"), price=("quotePrice", "mean")).reset_index(drop=True)
    ci = [_wilson(int(r.k), int(r.n)) for r in g.itertuples()]
    f2 = go.Figure(go.Bar(x=[money(v) for v in g["price"]], y=g["win"], marker=dict(color=CAT[0], cornerradius=4),
                          error_y=dict(type="data", symmetric=False, array=[c[1] - w for c, w in zip(ci, g["win"])], arrayminus=[w - c[0] for c, w in zip(ci, g["win"])], color=MUTED),
                          text=[pct(v, 0) for v in g["win"]], textposition="outside", cliponaxis=False))
    f2.update_yaxes(tickformat=".0%", range=[0, 0.7], title="Win rate")
    f2.update_xaxes(title="Average quote price in each fifth of quotes")
    style(f2, "Win rate is flat across price levels (bars show 80% uncertainty)", 380, legend=False)

    def breakeven(chg: float) -> float:
        lo, hi = -6.0, 0.5
        for _ in range(40):
            mid = (lo + hi) / 2
            if P.simulate(P.Scenario(price_change_pct=chg, price_elasticity=mid))["profit"] > base["profit"]:
                hi = mid          # still profitable at this sensitivity: the break-even is more negative
            else:
                lo = mid
        return (lo + hi) / 2
    be5, be10 = breakeven(5), breakeven(10)
    tab = pd.DataFrame([{"Price change": f"{g_:+d}%", **{lab.split(" (")[0]: money(curves[lab][i]) for lab, _, _ in assumptions}} for i, g_ in enumerate(grid) if g_ % 4 == 0])
    facts = [f"Across {len(o):,} quotes, win rate barely moves with price: the estimated price sensitivity is {el['estimate']:+.2f} (80% interval {el['lo']:+.2f} to {el['hi']:+.2f}).",
             f"A model using price, product, complexity and timber cannot predict which quotes are accepted (AUC {wm.auc:.2f}; 0.50 = coin flip).",
             f"A 5% price rise only loses profit if each 1% of price costs more than {abs(be5):.1f}% of wins; for a 10% rise the threshold is {abs(be10):.1f}%."]
    insights = [f"Prices in this business are close to cost-plus (about 1.8x cost), so most price variation reflects product and complexity, not tests of what customers will pay.",
                "Quote losses are therefore most likely driven by things the data does not record: competitor quotes, customer budgets and timing.",
                f"With typical B2B sensitivity (-1.0), a 10% rise adds about {money(curves['Moderately price-sensitive (-1.0)'][grid.index(10)] - base['profit'])} a month; "
                f"at -2.0 the same rise changes profit by {money(curves['Highly price-sensitive (-2.0)'][grid.index(10)] - base['profit'])} a month."]
    actions = ["Run a controlled pilot: raise quotes by about 5% on one or two products for a quarter and compare win rate against the others.",
               "Start recording the reason a quote is lost (price, timing, competitor) so the next model can find real drivers."]
    return ToolResult("price_strategy_analysis", "Pricing strategy", f"all quotes ({ds.start:%b %Y} - {ds.as_of:%b %Y})", facts, tab, fig, [],
                      ["Price effects beyond the observed range (roughly +/-20% of the product average) are extrapolation."], ["orders_data.csv"], {},
                      insights, actions, [("Win rate by price level", f2)], [], f"Bootstrap on {len(o):,} quotes; win-rate model AUC {wm.auc:.2f} (no measurable driver).")


# ============================================================================================ 6. profit bridge
def profit_bridge(period: str | None = "last_quarter") -> ToolResult:
    ds = D.load()
    p = resolve_period(period)
    span = (p.end - p.start).days + 1
    prev = Period(p.start - pd.Timedelta(days=span), p.start - pd.Timedelta(days=1), "previous period")
    if prev.start < ds.start:
        return ToolResult("profit_bridge", "Profit bridge", p.label, ["There is no earlier period of the same length to compare against."])
    cats = ["Deposits", "Final payments", "Wages", "Overheads", "Material purchases"]

    def amounts(pp: Period) -> dict:
        l = _ledger(pp)
        return {c: l.loc[l["category"] == c, "amount"].sum() for c in cats}
    a, b = amounts(p), amounts(prev)
    sign = {"Deposits": 1, "Final payments": 1, "Wages": -1, "Overheads": -1, "Material purchases": -1}
    labels_map = {"Deposits": "Deposits (new orders)", "Final payments": "Final payments (deliveries)", "Wages": "Labour cost", "Overheads": "Overheads", "Material purchases": "Timber purchases"}
    deltas = {c: sign[c] * (a[c] - b[c]) for c in cats}
    p_prev, p_cur = sum(sign[c] * b[c] for c in cats), sum(sign[c] * a[c] for c in cats)
    fig = _waterfall([labels_map[c] for c in cats], [p_prev] + [deltas[c] for c in cats], "Previous period profit", "This period profit",
                     f"What moved profit: {prev.start:%b %Y} - {prev.end:%b %Y} to {p.start:%b %Y} - {p.end:%b %Y}", 440)
    tab = pd.DataFrame([{"Line": labels_map[c], "Previous period": money(b[c]), "This period": money(a[c]), "Effect on profit": f"{'+' if deltas[c] >= 0 else '-'}${abs(deltas[c]):,.0f}"} for c in cats])
    big = max(cats, key=lambda c: abs(deltas[c]))
    kp, kc = _kpis(prev), _kpis(p)
    facts = [f"Profit {money(p_prev)} -> {money(p_cur)} ({'+' if p_cur - p_prev >= 0 else '-'}${abs(p_cur - p_prev):,.0f}).",
             f"Biggest mover: {labels_map[big].lower()} ({'+' if deltas[big] >= 0 else '-'}${abs(deltas[big]):,.0f})."]
    insights = [f"Orders accepted: {kc['won']} vs {kp['won']} (quotes {kc['inquiries']} vs {kp['inquiries']}); deliveries {kc['delivered']} vs {kp['delivered']}.",
                "Labour and overheads are fixed daily costs, so nearly all profit movement comes from cash received (deposits and final payments) and timber purchases."]
    if abs(deltas["Material purchases"]) > 0.2 * abs(p_cur - p_prev):
        insights.append("Timber purchase timing (re-orders are lumpy) explains a large part of the change and can reverse next period.")
    actions = ["Compare deposit movement (future work) with final payments (past work): if deposits fall first, expect final payments to follow 2-4 weeks later."]
    return ToolResult("profit_bridge", "Profit bridge", p.label, facts, tab, fig, [], ["Cash basis; the previous period is the equal-length window immediately before."],
                      ["financial_ledger.csv"], {"period": period}, insights, actions)


# ============================================================================================ 7. anomaly scan
def anomaly_scan() -> ToolResult:
    m = P.monthly_series()[["quotes", "win_rate", "revenue", "profit", "on_time"]].copy()
    names = {"quotes": "Quotes", "win_rate": "Win rate", "revenue": "Revenue", "profit": "Profit", "on_time": "On-time rate"}
    med = m.median()
    mad = (m - med).abs().median()
    scale = (1.4826 * mad).where(mad > 0, m.std())
    z = (m - med) / scale
    z = z.clip(-4, 4)
    xl = [p.strftime("%b %y") for p in m.index]
    fig = go.Figure(go.Heatmap(z=z.T.values, x=xl, y=[names[c] for c in z.columns], colorscale=[[0, BAD], [0.5, "#f7f6f2"], [1, GOOD]], zmid=0, zmin=-4, zmax=4,
                               hovertemplate="%{y}, %{x}: %{z:.1f} typical-deviations from normal<extra></extra>", showscale=False))
    style(fig, "Monthly health map: green = better than usual, red = worse than usual", 360, legend=False)
    flag = []
    for c in z.columns:
        for p_, v in z[c].items():
            if abs(v) >= 1.4 and not np.isnan(v):
                flag.append({"Month": p_.strftime("%b %Y"), "Metric": names[c], "Value": money(m.loc[p_, c]) if c in ("revenue", "profit") else pct(m.loc[p_, c], 0) if c in ("win_rate", "on_time") else num(m.loc[p_, c], 0),
                             "Typical": money(med[c]) if c in ("revenue", "profit") else pct(med[c], 0) if c in ("win_rate", "on_time") else num(med[c], 0),
                             "Direction": "Better" if v > 0 else "Worse", "Strength": f"{abs(v):.1f}x"})
    tab = pd.DataFrame(flag).sort_values("Month", key=lambda s: pd.to_datetime(s, format="%b %Y")) if flag else pd.DataFrame(columns=["Month", "Metric", "Value", "Typical", "Direction", "Strength"])
    worse = tab[tab["Direction"] == "Worse"] if len(tab) else tab
    trough = z["revenue"].idxmin()
    facts = [f"{len(tab)} unusual monthly readings found across {len(m)} months and 5 metrics (1.4+ typical deviations from normal).",
             f"Weakest revenue month: {trough.strftime('%B %Y')} ({money(m.loc[trough, 'revenue'])} vs typical {money(med['revenue'])})."]
    insights = []
    if len(worse):
        top_m = worse["Month"].value_counts().head(1)
        insights.append(f"{top_m.index[0]} had the most warning signs ({int(top_m.iloc[0])} metric{'s' if top_m.iloc[0] != 1 else ''} worse than usual).")
    q_trough = m["quotes"].idxmin()
    insights.append(f"Quote volume was lowest in {q_trough.strftime('%B %Y')} ({num(m.loc[q_trough, 'quotes'], 0)} quotes); revenue follows demand with a lag of about a month.")
    insights.append("Because wages are fixed, months with few quotes tend to show up as cash losses one to two months later.")
    insights = insights[:3]
    actions = ["Set an early-warning trigger on quotes issued: when a month runs below about 70% of normal, start outbound follow-up on open quotes.",
               "Investigate the flagged months with the team: was it a seasonal dip, a resignation, or a pricing change?"]
    return ToolResult("anomaly_scan", "Anomaly scan", f"{xl[0]} - {xl[-1]}", facts, tab, fig, [], ["Deviations use the median and median absolute deviation, so a single bad month does not distort what counts as normal."],
                      ["orders_data.csv", "financial_ledger.csv"], {}, insights, actions)


# ============================================================================================ 8. quote funnel
def quote_funnel(period: str | None = "all") -> ToolResult:
    p = resolve_period(period)
    o = _created(p)
    if o.empty:
        return ToolResult("quote_funnel", "Quote funnel", p.label, ["No quotes in this period."])
    q, w, d = len(o), int(o["won"].sum()), int(o["delivered"].sum())
    ot = int((o["delivered"] & ~o["is_late"]).sum())
    fig = go.Figure(go.Funnel(y=["Quotes issued", "Accepted by customer", "Delivered so far", "Delivered on time"], x=[q, w, d, ot], textinfo="value+percent initial",
                              marker=dict(color=[CAT[0], "#3987e5", CAT[2], "#0d366b"])))
    style(fig, "From quote to on-time delivery", 380, legend=False)
    g = o.groupby("productType").agg(quotes=("id", "count"), accepted=("won", "sum"), value=("quotePrice", "sum"))
    g["lost_value"] = o[~o["won"]].groupby("productType")["quotePrice"].sum()
    g["win"] = g["accepted"] / g["quotes"]
    g = g.reindex([x for x in PRODUCTS if x in g.index])
    f2 = go.Figure(go.Bar(x=g.index, y=g["win"], marker=dict(color=[PRODUCT_COLORS[i] for i in g.index], cornerradius=4), text=[pct(v, 0) for v in g["win"]], textposition="outside", cliponaxis=False))
    f2.update_yaxes(tickformat=".0%", range=[0, min(1, g["win"].max() * 1.3)])
    style(f2, "Win rate by product", 340, legend=False)
    tab = pd.DataFrame({"Product": g.index, "Quotes": g["quotes"].astype(int).values, "Accepted": g["accepted"].astype(int).values, "Win rate": [pct(v, 0) for v in g["win"]],
                        "Value of lost quotes": [money(v) for v in g["lost_value"].fillna(0)]})
    lost_val = float(o.loc[~o["won"], "quotePrice"].sum())
    best, worst = g["win"].idxmax(), g["win"].idxmin()
    facts = [f"{q} quotes -> {w} accepted ({pct(w / q, 0)}) -> {d} delivered so far -> {ot} on time.",
             f"Lost quotes were worth {money(lost_val)} in total; best product to win: {best} ({pct(g.loc[best, 'win'], 0)}), weakest: {worst} ({pct(g.loc[worst, 'win'], 0)})."]
    insights = [f"Every 1 point of win rate is worth about {money(q * 0.01 * o['quotePrice'].mean())} of extra orders over this period.",
                f"The largest pool of lost value is {g['lost_value'].idxmax()} ({money(g['lost_value'].max())})."]
    actions = [f"Focus win-rate work on {g['lost_value'].idxmax()}: review how quotes are followed up and how fast they go out.",
               "Log why quotes are lost; the data cannot say whether it was price, timing or a competitor."]
    return ToolResult("quote_funnel", "Quote-to-delivery funnel", p.label, facts, tab, fig, [], ["Cohort view: quotes created in the period and what has happened to them since."],
                      ["orders_data.csv"], {"period": period}, insights, actions, [("Win rate by product", f2)])


# ============================================================================================ 9. health score + executive briefing
def health_score(period: str | None = "last_quarter") -> dict:
    """0-100 composite of five explicit components (documented on the Data & Trust page)."""
    p = resolve_period(period)
    k = _kpis(p)
    span = (p.end - p.start).days + 1
    prev = Period(p.start - pd.Timedelta(days=span), p.start - pd.Timedelta(days=1), "prev")
    kp = _kpis(prev) if prev.start >= D.load().start else None

    def scale(v, lo, hi):
        return float(np.clip((v - lo) / (hi - lo), 0, 1) * 100) if v == v else 50.0
    op = D.load().orders
    risk = float(P.late_model().predict(op[op["in_progress"]]).mean()) if op["in_progress"].any() else P.late_model().base_rate
    mom = (k["revenue"] / kp["revenue"] - 1) if kp and kp["revenue"] else 0.0
    comp = {"Profitability": scale(k["margin"], -0.10, 0.20), "On-time delivery": scale(k["on_time"], 0.70, 1.0), "Sales conversion": scale(k["win_rate"], 0.25, 0.55),
            "Capacity headroom": scale(0.40 - risk, 0.0, 0.30), "Momentum": scale(mom, -0.20, 0.20)}
    score = float(np.mean(list(comp.values())))
    return dict(score=score, components=comp, label="Strong" if score >= 70 else "Steady" if score >= 50 else "Needs attention", period=p.label, kpis=k, prev=kp)


def executive_briefing(period: str | None = "last_quarter") -> ToolResult:
    bs = business_summary(period)
    hs = health_score(period)
    bridge = profit_bridge(period)
    radar = late_risk_radar(8)
    fc = forecast_outlook("revenue", 3)
    anom = anomaly_scan()
    facts = [f"Business health score: {hs['score']:.0f}/100 ({hs['label']}) for {hs['period']}."] + bs.facts[:3]
    insights = [i for r in (bridge, radar, fc, anom) for i in r.insights[:1]] + [bridge.facts[-1]] if bridge.facts else []
    insights = [x for x in insights if x][:6]
    actions = [a for r in (radar, fc, anom) for a in r.actions[:1]][:4]
    comp = pd.DataFrame({"Component": list(hs["components"]), "Score (0-100)": [round(v) for v in hs["components"].values()]})
    return ToolResult("executive_briefing", "Executive briefing", bs.period, facts, bs.table, bridge.chart, bs.kpis, bs.notes,
                      ["orders_data.csv", "financial_ledger.csv", "hr_events_log.csv"], {"period": period}, insights, actions,
                      [("Late-risk radar", radar.chart), ("Revenue outlook", fc.chart), ("Health map", anom.chart)],
                      [("Health score components", comp), ("Revenue outlook", fc.table)],
                      "Composite of scorecard, profit bridge, risk model, forecast and anomaly scan; each part carries its own method note.")


# ============================================================================================ registry
PRO_REGISTRY = {
    "late_risk_radar": dict(fn=late_risk_radar, group="Predictive",
        desc="PREDICTIVE: scores every order currently in progress for its chance of finishing late (machine-learning risk model), with expected penalty exposure, risk drivers and how shop load drives lateness. Use for 'which orders are at risk', 'will we deliver on time', 'what is the risk right now'.",
        params={"top_n": ("integer", "How many orders to show (default 10).")}),
    "forecast_outlook": dict(fn=forecast_outlook, group="Predictive",
        desc="PREDICTIVE: back-tested forecast with an 80% range for revenue, costs, profit, quotes, accepted orders, win rate or on-time rate, 1-6 months ahead, plus a capacity check. Use for 'forecast', 'predict', 'next quarter', 'outlook', 'what will happen'.",
        params={"metric": ("string", "One of: revenue, costs, profit, quotes, accepted, win_rate, on_time. Default revenue."), "horizon_months": ("integer", "Months ahead, 1-6 (default 3).")}),
    "scenario_simulator": dict(fn=scenario_simulator, group="Predictive",
        desc="WHAT-IF: simulates monthly profit, win rate, on-time delivery and penalties when prices, quote volume, maker headcount or timber costs change. Use for 'what if we raise prices 10%', 'what if we hire 2 makers', 'what if demand grows 30%'.",
        params={"price_change_pct": ("number", "Price change in percent, e.g. 10 or -5."), "quote_volume_change_pct": ("number", "Change in quotes issued per month, in percent."),
                "extra_makers": ("number", "Makers added (negative = fewer)."), "timber_cost_change_pct": ("number", "Change in timber cost, in percent."),
                "price_elasticity": ("number", "Optional customer price sensitivity, e.g. -1.0. Leave empty to use the data estimate.")}),
    "capacity_planner": dict(fn=capacity_planner, group="Predictive",
        desc="Hiring and capacity planning grid: monthly profit and on-time delivery for different demand levels and team sizes. Use for 'do we need to hire', 'how many makers', 'can we handle more demand', 'capacity planning'.",
        params={}),
    "price_strategy_analysis": dict(fn=price_strategy_analysis, group="Predictive",
        desc="Pricing strategy: does price drive winning quotes, estimated price sensitivity, and profit under different customer reactions. Use for 'should we raise prices', 'price elasticity', 'optimal pricing', 'what drives winning quotes'.",
        params={}),
    "profit_bridge": dict(fn=profit_bridge, group="Profitability",
        desc="Waterfall that explains WHY profit changed versus the previous period of equal length (deposits, final payments, labour, overheads, timber). Use for 'why did profit change/drop/rise', 'what drove profit'.",
        params={"period": ("string", "Period to explain, e.g. 'last_quarter', '2026-Q3'. Default last_quarter.")}),
    "anomaly_scan": dict(fn=anomaly_scan, group="Overview",
        desc="Scans monthly quotes, win rate, revenue, profit and on-time rate for unusual months and shows a health heat map. Use for 'anything unusual', 'red flags', 'what should I worry about', 'anomalies'.",
        params={}),
    "quote_funnel": dict(fn=quote_funnel, group="Sales",
        desc="Funnel from quotes issued to accepted to delivered to on-time, with win rate and lost value by product. Use for 'conversion', 'sales funnel', 'how much value did we lose'.",
        params={"period": ("string", "Period; default 'all'.")}),
    "executive_briefing": dict(fn=executive_briefing, group="Overview",
        desc="One-page executive briefing: health score, KPIs, profit bridge, late-risk radar, revenue outlook and anomaly map with top insights and actions. Use for 'give me a full review', 'board summary', 'brief me', 'everything I should know'.",
        params={"period": ("string", "Period; default last_quarter.")}),
}
