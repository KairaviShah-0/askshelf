"""Supply-side tools for distributors and wholesalers: stock availability (days of cover vs open-order demand) and the delivery schedule.

Same contract as tools.py: fixed, vetted calculations; each result carries facts, insights, actions, charts and a table.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from . import data as D
from . import predict as P
from .config import MATERIAL_COLORS, MATERIALS
from .tools import ToolResult, money, num, pct, style

GOOD, WARN, BAD = "#1baf7a", "#eda100", "#d6453a"


def stock_cover() -> ToolResult:
    """How many days each timber lasts at the recent burn rate, and whether open orders will outrun stock on hand."""
    ds = D.load()
    inv = ds.inventory.sort_values("date")
    end = ds.as_of
    days = pd.date_range(end - pd.Timedelta(days=119), end, freq="D")
    cons = inv[inv["type"] == "CONSUME"]
    op = ds.orders[ds.orders["in_progress"]]
    rows, series = [], {}
    for m in MATERIALS:
        im = inv[inv["material"] == m]
        lvl = im.groupby("date")["newLevel"].last().reindex(pd.date_range(inv["date"].min(), end, freq="D")).ffill().bfill()
        series[m] = lvl.reindex(days)
        now = float(lvl.iloc[-1])
        burn = float(cons[(cons["material"] == m) & (cons["date"] > end - pd.Timedelta(days=90))]["quantity"].sum()) / 90
        cover = now / burn if burn > 0 else float("inf")
        committed = float(op[op["materialType"] == m]["materialQty"].sum())
        ro = im[im["type"] == "REORDER"]["date"]
        cadence = float(ro.diff().dt.days.dropna().tail(12).mean()) if len(ro) > 2 else float("nan")
        year = im[im["date"] > end - pd.Timedelta(days=365)]
        low = int(((year["newLevel"] <= 2)).sum())
        status = "Short" if committed > now else "Tight" if cover < 5 or now <= 3 else "Healthy"
        rows.append({"Material": m, "On hand": int(now), "Open-order demand": int(committed), "Short by": int(max(committed - now, 0)),
                     "Days of cover": "n/a" if not np.isfinite(cover) else f"{cover:.1f}", "Burn / day": round(burn, 2),
                     "Reorder every (days)": "n/a" if np.isnan(cadence) else f"{cadence:.1f}", "Low-stock events (12m)": low, "Status": status})
    tab = pd.DataFrame(rows)

    fig = go.Figure()
    for m in MATERIALS:
        fig.add_trace(go.Scatter(x=series[m].index, y=series[m].values, mode="lines", name=m, line=dict(color=MATERIAL_COLORS[m], width=2, shape="hv")))
    fig.add_hline(y=2, line=dict(color=BAD, dash="dot", width=1), annotation_text="Low-stock line", annotation_position="top left")
    style(fig, "Stock level by material, last 120 days (units)", 340)
    fig.update_yaxes(title="Units on hand", rangemode="tozero")
    fig2 = go.Figure()
    fig2.add_trace(go.Bar(x=tab["Material"], y=tab["On hand"], name="On hand", marker=dict(color="#2a78d6", cornerradius=5)))
    fig2.add_trace(go.Bar(x=tab["Material"], y=tab["Open-order demand"], name="Needed by open orders", marker=dict(color="#eda100", cornerradius=5)))
    fig2.update_layout(barmode="group")
    style(fig2, "Stock on hand vs what open orders still need", 340)

    short = tab[tab["Status"] == "Short"]
    tight = tab[tab["Status"] == "Tight"]
    facts = [f"{len(short)} of 3 materials are short against open-order demand; {len(tight)} more are tight.",
             *[f"{r['Material']}: {r['On hand']} on hand, {r['Open-order demand']} needed by open orders, about {r['Days of cover']} days of cover." for _, r in tab.iterrows()]]
    insights = []
    if len(short):
        insights.append(f"{', '.join(short['Material'])} cannot cover every open order from stock on hand: reorders must land before those jobs reach milling.")
    worst = tab.sort_values("Low-stock events (12m)", ascending=False).iloc[0]
    insights.append(f"{worst['Material']} ran low most often in the last 12 months ({worst['Low-stock events (12m)']} low-stock readings).")
    insights.append("Timber is reordered in small batches, so cover depends on how fast the reorder lands, not on the batch size.")
    actions = [f"Raise a purchase order now for {', '.join(short['Material'])}." if len(short) else "No immediate shortfall: keep the current reorder cadence.",
               "Move the reorder point up for any material that hit the low-stock line more than a few times this year."]
    return ToolResult("stock_cover", "Stock availability and cover", f"to {ds.as_of:%d %b %Y}", facts[:6], tab, fig, [], ["Burn rate uses the last 90 days of consumption; open-order demand is the material quantity of orders still in progress."],
                      ["inventory_log.csv", "orders_data.csv"], {}, insights[:3], actions, [("Material mix", fig2)])


def delivery_schedule() -> ToolResult:
    """Open orders on a timeline from acceptance to due date, coloured by predicted lateness risk, with what is due this week and next."""
    ds = D.load()
    lm = P.late_model()
    op = ds.orders[ds.orders["in_progress"]].copy()
    op["risk"] = lm.predict(op)
    today = ds.as_of
    op["level"] = np.where(op["risk"] >= 2 * lm.base_rate, "High", np.where(op["risk"] >= lm.base_rate, "Elevated", "Low"))
    op = op.sort_values("dueDate")
    overdue = op[op["dueDate"] < today]
    wk1 = op[(op["dueDate"] >= today) & (op["dueDate"] < today + pd.Timedelta(days=7))]
    wk2 = op[(op["dueDate"] >= today + pd.Timedelta(days=7)) & (op["dueDate"] < today + pd.Timedelta(days=14))]
    col = {"High": BAD, "Elevated": WARN, "Low": GOOD}
    fig = go.Figure()
    for lv in ("High", "Elevated", "Low"):
        d = op[op["level"] == lv]
        if not len(d):
            continue
        fig.add_trace(go.Bar(base=d["acceptanceDate"], x=(d["dueDate"] - d["acceptanceDate"]).dt.total_seconds() * 1000, y=[f"#{i} {p}" for i, p in zip(d["id"], d["productType"])],
                             orientation="h", name=f"{lv} risk", marker=dict(color=col[lv], cornerradius=4), customdata=np.c_[d["risk"] * 100, d["quotePrice"]],
                             hovertemplate="%{y}<br>Late risk %{customdata[0]:.0f}%<br>Quote $%{customdata[1]:,.0f}<extra></extra>"))
    fig.add_shape(type="line", x0=today, x1=today, y0=0, y1=1, yref="paper", line=dict(color="#0f172a", width=1.5, dash="dash"))
    fig.add_annotation(x=today, y=1.02, yref="paper", text="Today", showarrow=False, font=dict(size=11))
    style(fig, "Open orders: accepted to due date, coloured by late risk", 120 + 26 * len(op))
    fig.update_xaxes(type="date")
    fig.update_yaxes(autorange="reversed")
    wk = op.assign(week=op["dueDate"].dt.to_period("W-SUN").dt.start_time).groupby(["week", "level"]).size().unstack(fill_value=0).reindex(columns=["High", "Elevated", "Low"], fill_value=0)
    fig2 = go.Figure()
    for lv in ("High", "Elevated", "Low"):
        fig2.add_trace(go.Bar(x=[f"{w:%d %b}" for w in wk.index], y=wk[lv], name=lv, marker=dict(color=col[lv], cornerradius=4)))
    fig2.update_layout(barmode="stack")
    style(fig2, "Deliveries due per week (week starting)", 300)
    tab = pd.DataFrame({"Order": op["id"], "Product": op["productType"], "Stage": op["currentStage"].fillna("Queued").str.title(), "Due": op["dueDate"].dt.strftime("%d %b %Y"),
                        "Days left": (op["dueDate"] - today).dt.days, "Late risk": (op["risk"] * 100).round().astype(int).astype(str) + "%", "Risk level": op["level"],
                        "Quote value": op["quotePrice"].map(money), "Penalty exposure": (op["risk"] * op["quotePrice"] * 0.05).map(money)})
    exposure = float((op["risk"] * op["quotePrice"] * 0.05).sum())
    facts = [f"{len(op)} open orders; {len(wk1)} due in the next 7 days and {len(wk2)} the week after.",
             f"{len(overdue)} order(s) are already past their due date.", f"{int((op['level'] == 'High').sum())} orders are high risk of finishing late; expected penalty exposure {money(exposure)}.",
             f"Value of orders due in the next 7 days: {money(float(wk1['quotePrice'].sum()))}."]
    hot = wk1[wk1["level"] == "High"]
    insights = [f"{len(hot)} of the {len(wk1)} orders due this week are high risk." if len(wk1) else "Nothing is due in the next 7 days.",
                "Risk is driven mainly by how many jobs are in production at once, so pulling one job forward can lift the risk on the others."]
    actions = ["Warn the customers on high-risk orders due this week before the due date passes.", "Re-sequence the shop: finish the highest-value, highest-risk orders first."]
    return ToolResult("delivery_schedule", "Delivery schedule", f"from {today:%d %b %Y}", facts, tab, fig, [], ["Late risk comes from the cross-validated late-delivery model; penalty exposure = risk x quote x 5%."],
                      ["orders_data.csv"], {}, insights, actions, [("Deliveries per week", fig2)])


def penalty_control(expedite_n: int = 0, extra_makers: float = 0) -> ToolResult:
    """What late deliveries have cost, which open orders carry the exposure, and what expediting them or adding capacity would save.

    Savings are model-based: adding makers lowers the 'orders per maker' load the late-risk model uses; an expedited order is assumed to
    perform like a typical order (the historical late rate). Both are stated assumptions, not guarantees.
    """
    ds = D.load()
    lm = P.late_model()
    o, end = ds.orders, ds.as_of
    deliv = o[o["delivered"]].copy()
    deliv["month"] = deliv["completionDate"].dt.to_period("M").astype(str)
    paid_all = float(deliv["penalty_amt"].sum())
    paid_12 = float(deliv[deliv["completionDate"] > end - pd.Timedelta(days=365)]["penalty_amt"].sum())
    n_late, n_del = int(deliv["is_late"].sum()), len(deliv)
    by_m = deliv.groupby("month")["penalty_amt"].sum()
    by_p = deliv.groupby("productType").agg(pen=("penalty_amt", "sum"), late=("is_late", "mean"))

    expedite_n = int(max(0, min(expedite_n or 0, 17)))
    extra_makers = float(max(0, min(extra_makers or 0, 5)))
    op = o[o["in_progress"]].copy()
    X = lm.features(op)
    base_risk = lm.model.predict_proba(X)
    ctx = P._context().iloc[-1]
    col = "Orders per available maker"
    delta = float(ctx["wip"] / ctx["makers"] - ctx["wip"] / (ctx["makers"] + extra_makers)) if extra_makers else 0.0
    X2 = X.copy()
    X2[col] = (X2[col] - delta).clip(lower=0.5)
    risk_cap = lm.model.predict_proba(X2) if extra_makers else base_risk.copy()
    exp_now = base_risk * op["quotePrice"].values * 0.05
    exp_cap = risk_cap * op["quotePrice"].values * 0.05
    rank = np.argsort(-exp_cap)
    risk_after = risk_cap.copy()
    top = rank[:expedite_n]
    risk_after[top] = np.minimum(risk_after[top], lm.base_rate)
    exp_after = risk_after * op["quotePrice"].values * 0.05
    now_total, after_total = float(exp_now.sum()), float(exp_after.sum())
    saved = now_total - after_total

    tab = pd.DataFrame({"Order": op["id"].values, "Product": op["productType"].values, "Due": op["dueDate"].dt.strftime("%d %b %Y").values,
                        "Quote value": op["quotePrice"].map(money).values, "Late risk now": (base_risk * 100).round().astype(int).astype(str) + "%",
                        "Late risk after plan": (risk_after * 100).round().astype(int).astype(str) + "%", "Exposure now": exp_now.round(0), "Exposure after plan": exp_after.round(0),
                        "Action": ["Expedite" if i in set(top) else "" for i in range(len(op))]})
    tab = tab.sort_values("Exposure now", ascending=False)
    tab["Exposure now"] = tab["Exposure now"].map(money)
    tab["Exposure after plan"] = tab["Exposure after plan"].map(money)

    fig = go.Figure(go.Bar(x=[f"{m[5:]}/{m[2:4]}" for m in by_m.index], y=by_m.values, marker=dict(color=BAD, cornerradius=4), hovertemplate="%{x}: $%{y:,.0f}<extra></extra>"))
    style(fig, "Penalties paid by month of delivery", 300, legend=False)
    fig.update_yaxes(tickprefix="$")
    top12 = np.argsort(-exp_now)[:12]
    lab = [f"#{op['id'].iloc[i]} {op['productType'].iloc[i]}" for i in top12]
    fig2 = go.Figure()
    fig2.add_trace(go.Bar(y=lab, x=exp_now[top12], orientation="h", name="Exposure now", marker=dict(color=BAD, cornerradius=4)))
    fig2.add_trace(go.Bar(y=lab, x=exp_after[top12], orientation="h", name="After your plan", marker=dict(color=GOOD, cornerradius=4)))
    fig2.update_layout(barmode="group")
    fig2.update_yaxes(autorange="reversed")
    fig2.update_xaxes(tickprefix="$")
    style(fig2, "Penalty exposure on the 12 riskiest open orders", 440)
    fig3 = go.Figure(go.Bar(x=by_p.index, y=by_p["pen"], marker=dict(color="#2a78d6", cornerradius=4), customdata=by_p["late"] * 100,
                            hovertemplate="%{x}<br>$%{y:,.0f} paid<br>%{customdata:.0f}% late<extra></extra>"))
    style(fig3, "Penalties paid by product", 300, legend=False)
    fig3.update_yaxes(tickprefix="$")

    worst = by_p["pen"].idxmax()
    kpis = [("Paid, all time", money(paid_all), None), ("Paid, last 12 months", money(paid_12), None), ("Late rate", pct(n_late / max(n_del, 1)), None),
            ("Open-order exposure", money(now_total), None), ("After your plan", money(after_total), f"-{money(saved)} saved" if saved > 0.5 else None)]
    facts = [f"Late deliveries have cost {money(paid_all)} ({n_late} of {n_del} delivered orders were late); {money(paid_12)} of that in the last 12 months.",
             f"{worst} orders account for the most penalties ({money(float(by_p.loc[worst, 'pen']))}).",
             f"Expected penalty exposure on {len(op)} open orders is {money(now_total)}."]
    if expedite_n or extra_makers:
        facts.append(f"Plan: expedite the top {expedite_n} orders" + (f" and add {extra_makers:g} maker(s)" if extra_makers else "") + f" -> exposure {money(after_total)}, saving {money(saved)}.")
    insights = [f"The 5 riskiest open orders carry {money(float(np.sort(exp_now)[::-1][:5].sum()))} of the {money(now_total)} exposure.",
                "Penalty is 10% of the final payment, so high-value orders matter most when you decide what to expedite first."]
    actions = ["Expedite orders in the table's order: the biggest exposure first.", "If exposure stays high after expediting, the workshop is overloaded: test extra makers in the plan."]
    return ToolResult("penalty_control", "Penalty control", f"to {end:%d %b %Y}", facts, tab, fig2, kpis,
                      ["Exposure = predicted late risk x quote x 5% (the final payment is half the quote; the penalty is 10% of that).",
                       "Savings are a model-based estimate: extra makers reduce the orders-per-maker load in the late-risk model, and an expedited order is assumed to perform like a typical order (historical late rate). Treat them as planning ranges."],
                      ["orders_data.csv", "financial_ledger.csv", "hr_roster.csv"], {"expedite_n": expedite_n, "extra_makers": extra_makers}, insights, actions,
                      [("Penalties by month", fig), ("Penalties by product", fig3)], confidence="Late-risk model AUC %.2f; savings assume an expedited order performs like a typical one." % lm.auc)


SUPPLY_REGISTRY = {
    "stock_cover": dict(fn=stock_cover, group="Materials",
        desc="STOCK AVAILABILITY: days of cover for each timber at the recent burn rate, stock on hand versus what open orders still need (shortfalls), reorder cadence and low-stock events. Use for 'will we run out of stock', 'do we have enough timber for open orders', 'stock cover', 'what should we reorder'.",
        params={}),
    "delivery_schedule": dict(fn=delivery_schedule, group="Operations",
        desc="DELIVERY SCHEDULE: open orders on a timeline to their due date coloured by predicted late risk, what is due this week and next, overdue orders and penalty exposure. Use for 'what is due this week', 'delivery schedule', 'upcoming deliveries', 'overdue orders'.",
        params={}),
    "penalty_control": dict(fn=penalty_control, group="Operations",
        desc="PENALTY CONTROL: what late deliveries have cost (by month and product), penalty exposure on open orders, and the estimated saving from expediting the highest-exposure orders and/or adding makers. Use for 'how much could we save on penalties', 'which orders should we expedite first', 'penalty exposure', 're-sequence the schedule'.",
        params={"expedite_n": ("integer", "How many of the highest-exposure open orders to expedite (0-17, default 0)."), "extra_makers": ("number", "Extra makers added (0-5, default 0).")}),
}
