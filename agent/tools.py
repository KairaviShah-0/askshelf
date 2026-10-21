"""Vetted analysis tools.

The LLM never writes code or touches raw data. It chooses one of these parameterised tools; each tool runs a
fixed pandas calculation and returns facts, a table and a chart. This keeps every number traceable and makes
the privacy guardrail enforceable (tools only ever see the allow-listed, aggregated tables from data.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from . import data as D
from .config import (AXIS, BLUE_SEQ, CAT, DEPOSIT_SHARE, GRID, INK, INK_2, MATERIAL_COLORS, MATERIALS, MUTED,
                     PRODUCT_COLORS, PRODUCTS, SURFACE, WORK_HOURS_PER_DAY)

# ----------------------------------------------------------------------------- helpers


def money(x: float) -> str:
    return f"-${abs(x):,.0f}" if x < 0 else f"${x:,.0f}"


def pct(x: float, d: int = 1) -> str:
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.{d}f}%"


def num(x: float, d: int = 1) -> str:
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:,.{d}f}"


@dataclass
class ToolResult:
    tool: str
    title: str
    period: str
    facts: list[str]
    table: pd.DataFrame | None = None
    chart: go.Figure | None = None
    kpis: list[tuple[str, str, str | None]] = field(default_factory=list)  # (label, value, delta)
    notes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    args: dict = field(default_factory=dict)
    insights: list[str] = field(default_factory=list)                            # "what stands out" (numbers, interpreted)
    actions: list[str] = field(default_factory=list)                             # recommended next steps
    extra_charts: list[tuple[str, go.Figure]] = field(default_factory=list)      # (tab label, figure)
    extra_tables: list[tuple[str, pd.DataFrame]] = field(default_factory=list)   # (caption, table)
    confidence: str | None = None                                                # one-line honesty statement about the method

    def llm_payload(self, max_rows: int = 30) -> str:
        parts = [f"TOOL: {self.tool}", f"TITLE: {self.title}", f"PERIOD: {self.period}", "FACTS:"]
        parts += [f"- {f}" for f in self.facts]
        if self.insights:
            parts += ["INSIGHTS:"] + [f"- {f}" for f in self.insights]
        if self.actions:
            parts += ["SUGGESTED ACTIONS:"] + [f"- {f}" for f in self.actions]
        if self.confidence:
            parts.append(f"METHOD CONFIDENCE: {self.confidence}")
        if self.table is not None and len(self.table):
            parts.append("TABLE (csv):")
            parts.append(self.table.head(max_rows).to_csv(index=False))
        if self.notes:
            parts.append("NOTES: " + " | ".join(self.notes))
        return "\n".join(parts)


# ----------------------------------------------------------------------------- periods


@dataclass
class Period:
    start: pd.Timestamp
    end: pd.Timestamp
    label: str
    note: str | None = None

    def mask(self, s: pd.Series) -> pd.Series:
        return (s >= self.start) & (s <= self.end)


_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
     "November", "December"], 1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})


def _fmt(p_start, p_end):
    return f"{p_start:%d %b %Y} - {p_end:%d %b %Y}"


def resolve_period(spec: str | None) -> Period:
    """Turn 'last_quarter', '2025', '2025-Q3', 'Sep 2025', 'H1 2026', 'last_90_days'... into a date range."""
    ds = D.load()
    lo, hi = ds.start, ds.as_of
    s = (spec or "").strip().lower().replace("_", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s)
    last_day = hi - pd.Timedelta(days=1)          # last fully-recorded day

    def mk(a, b, label, note=None):
        a, b = max(a, lo), min(b, hi)
        return Period(a, b, f"{label} ({_fmt(a, b)})", note)

    if s in ("", "all", "all time", "overall", "everything", "entire period", "whole period", "to date"):
        return Period(lo, hi, f"All data ({_fmt(lo, hi)})")
    if s in ("last quarter", "previous quarter", "latest quarter", "this quarter", "current quarter"):
        q = pd.Period(last_day, "Q")
        return mk(q.start_time, q.end_time.normalize(), f"Q{q.quarter} {q.year}",
                  "'Last quarter' = the last complete quarter in the data.")
    if s in ("last month", "previous month", "this month"):
        m = pd.Period(last_day, "M")
        return mk(m.start_time, m.end_time.normalize(), f"{m.strftime('%B %Y')}")
    if s in ("last year", "this year", "latest year", "previous year"):
        return mk(pd.Timestamp(last_day.year, 1, 1), pd.Timestamp(last_day.year, 12, 31), str(last_day.year))
    m = re.fullmatch(r"(?:last|past) (\d+) days?", s)
    if m:
        n = int(m.group(1))
        return mk(hi - pd.Timedelta(days=n - 1), hi, f"Last {n} days")
    m = re.fullmatch(r"(?:last|past) (\d+) (months?|quarters?)", s)
    if m:
        n = int(m.group(1)) * (3 if m.group(2).startswith("q") else 1)
        return mk((hi - pd.DateOffset(months=n)).normalize(), hi, f"Last {n} months")
    m = re.fullmatch(r"(20\d\d)", s)
    if m:
        y = int(m.group(1))
        return mk(pd.Timestamp(y, 1, 1), pd.Timestamp(y, 12, 31), str(y))
    m = re.fullmatch(r"(?:q([1-4]) (20\d\d)|(20\d\d) ?q([1-4]))", s)
    if m:
        q, y = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(4)), int(m.group(3)))
        p = pd.Period(f"{y}Q{q}")
        return mk(p.start_time, p.end_time.normalize(), f"Q{q} {y}")
    m = re.fullmatch(r"h([12]) (20\d\d)", s)
    if m:
        h, y = int(m.group(1)), int(m.group(2))
        a, b = (pd.Timestamp(y, 1, 1), pd.Timestamp(y, 6, 30)) if h == 1 else (pd.Timestamp(y, 7, 1), pd.Timestamp(y, 12, 31))
        return mk(a, b, f"H{h} {y}")
    m = re.fullmatch(r"(20\d\d) (\d{1,2})", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            p = pd.Period(f"{y}-{mo:02d}")
            return mk(p.start_time, p.end_time.normalize(), p.strftime("%B %Y"))
    m = re.fullmatch(r"([a-z]+) (20\d\d)", s)
    if m and m.group(1) in _MONTHS:
        p = pd.Period(f"{m.group(2)}-{_MONTHS[m.group(1)]:02d}")
        return mk(p.start_time, p.end_time.normalize(), p.strftime("%B %Y"))
    return Period(lo, hi, f"All data ({_fmt(lo, hi)})", f"Could not read the period '{spec}', so the full dataset was used.")


def guess_period(question: str) -> str | None:
    """Best-effort period extraction from free text (used by the offline router)."""
    q = question.lower()
    for pat in (r"\bq[1-4] ?20\d\d\b", r"\b20\d\d ?-? ?q[1-4]\b", r"\bh[12] 20\d\d\b"):
        m = re.search(pat, q)
        if m:
            return m.group(0).replace("-", " ")
    m = re.search(r"\b(" + "|".join(k for k in _MONTHS if len(k) > 3) + r") (20\d\d)\b", q)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    for k in ("last quarter", "previous quarter", "last month", "last year", "this year", "this quarter"):
        if k in q:
            return k
    m = re.search(r"(?:last|past) (\d+) (days?|months?|quarters?)", q)
    if m:
        return m.group(0)
    m = re.search(r"\b(2025|2026)\b", q)
    return m.group(1) if m else None


def resolve_product(name: str | None) -> str | None:
    if not name or str(name).lower() in ("all", "any", "none", "every", ""):
        return None
    n = str(name).lower()
    for p in PRODUCTS:
        if n == p.lower() or n.rstrip("s") == p.lower().rstrip("s"):
            return p
    for key, p in (("book", "Bookshelf"), ("shelf", "Bookshelf"), ("dining", "Dining Table"), ("table", "Dining Table"),
                   ("media", "Media Console"), ("console", "Media Console"), ("tv", "Media Console"),
                   ("desk", "Office Desk"), ("office", "Office Desk"), ("side", "Sideboard")):
        if key in n:
            return p
    return None


def resolve_material(name: str | None) -> str | None:
    if not name or str(name).lower() in ("all", "any", "none", ""):
        return None
    n = str(name).lower()
    for key, m in (("walnut", "American Walnut"), ("oak", "Tasmanian Oak"), ("ash", "Victorian Ash"), ("american", "American Walnut"),
                   ("tasmanian", "Tasmanian Oak"), ("victorian", "Victorian Ash")):
        if key in n:
            return m
    return None


# ----------------------------------------------------------------------------- chart styling


def style(fig: go.Figure, title: str, height: int = 380, legend: bool = True) -> go.Figure:
    fig.update_layout(
        title=dict(text=title, x=0, xanchor="left", font=dict(size=15, color=INK)),
        height=height, margin=dict(l=8, r=8, t=56, b=8),
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(family="system-ui, -apple-system, 'Segoe UI', sans-serif", size=12, color=INK_2),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0, bgcolor="rgba(0,0,0,0)"),
        showlegend=legend, hoverlabel=dict(bgcolor="white", font_size=12), bargap=0.35,
    )
    if len(fig.layout.annotations):                       # multi-panel figure: keep the legend clear of panel titles
        fig.update_layout(margin=dict(t=112), legend=dict(y=1.11))
    fig.update_xaxes(showgrid=False, linecolor=AXIS, tickfont=dict(color=MUTED), zeroline=False)
    fig.update_yaxes(gridcolor=GRID, gridwidth=1, zeroline=False, tickfont=dict(color=MUTED), linecolor="rgba(0,0,0,0)")
    return fig


def _wrap(name: str) -> str:
    return str(name).replace(" ", "<br>")


def _color_for(name: str, i: int = 0) -> str:
    return PRODUCT_COLORS.get(name) or MATERIAL_COLORS.get(name) or CAT[i % len(CAT)]


def _bar_trace(x, y, name, color, fmt=None, **kw):
    return go.Bar(x=x, y=y, name=name, marker=dict(color=color, cornerradius=4), text=fmt, textposition="outside",
                  textfont=dict(color=INK_2, size=11), cliponaxis=False, **kw)


# ----------------------------------------------------------------------------- shared calculations


@lru_cache(maxsize=1)
def _wip_daily() -> pd.Series:
    """Number of accepted orders in production on each day (orders that are open at end of data stay open)."""
    ds = D.load()
    o = ds.orders[ds.orders["won"] & ds.orders["acceptanceDate"].notna()]
    days = pd.date_range(ds.start, ds.as_of, freq="D")
    a = o["acceptanceDate"].values[:, None]
    c = o["completionDate"].fillna(ds.as_of + pd.Timedelta(days=1)).values[:, None]
    d = days.values[None, :]
    return pd.Series(((a <= d) & (c > d)).sum(axis=0), index=days)


def _delivered(p: Period, product: str | None = None) -> pd.DataFrame:
    ds = D.load()
    t = ds.timeline
    t = t[p.mask(t["completionDate"])]
    return t[t["productType"] == product] if product else t


def _created(p: Period, product: str | None = None) -> pd.DataFrame:
    o = D.load().orders
    o = o[p.mask(o["creationDate"])]
    return o[o["productType"] == product] if product else o


def _ledger(p: Period) -> pd.DataFrame:
    l = D.load().ledger
    return l[p.mask(l["date"])]


def _kpis(p: Period) -> dict:
    o_c = _created(p)
    t = _delivered(p)
    l = _ledger(p)
    rev = l.loc[l["type"] == "REVENUE", "amount"].sum()
    cost = l.loc[l["type"] == "COST", "amount"].sum()
    return dict(
        inquiries=len(o_c), won=int(o_c["won"].sum()), win_rate=o_c["won"].mean() if len(o_c) else np.nan,
        delivered=len(t), on_time=1 - t["is_late"].mean() if len(t) else np.nan, late=int(t["is_late"].sum()),
        penalty=t["penalty_amt"].sum(), avg_lead=t["lead_days"].mean() if len(t) else np.nan,
        production_days=t["production_days"].mean() if len(t) else np.nan,
        revenue=rev, cost=cost, profit=rev - cost, margin=(rev - cost) / rev if rev else np.nan,
    )


def _delta(cur, prev, kind):
    if prev is None or cur is None or (isinstance(prev, float) and np.isnan(prev)) or (isinstance(cur, float) and np.isnan(cur)):
        return None
    if kind == "pp":
        return f"{(cur - prev) * 100:+.1f} pp"
    if kind == "days":
        return f"{cur - prev:+.1f} days"
    if kind == "money":
        return f"{'+' if cur - prev >= 0 else '-'}${abs(cur - prev):,.0f}"
    return f"{cur - prev:+,.0f}"


# ----------------------------------------------------------------------------- TOOL 1: business summary


def business_summary(period: str | None = "last_quarter") -> ToolResult:
    ds = D.load()
    p = resolve_period(period)
    k = _kpis(p)
    span = (p.end - p.start).days + 1
    prev_p = Period(p.start - pd.Timedelta(days=span), p.start - pd.Timedelta(days=1), "previous period")
    prev = _kpis(prev_p) if prev_p.start >= ds.start else None
    g = (lambda key: prev[key]) if prev else (lambda key: None)
    rows = [
        ("Quotes issued", k["inquiries"], g("inquiries"), "n"),
        ("Win rate (accepted / quotes)", k["win_rate"], g("win_rate"), "pp"),
        ("Orders delivered", k["delivered"], g("delivered"), "n"),
        ("On-time delivery rate", k["on_time"], g("on_time"), "pp"),
        ("Avg lead time (days)", k["avg_lead"], g("avg_lead"), "days"),
        ("Cash revenue", k["revenue"], g("revenue"), "money"),
        ("Cash costs", k["cost"], g("cost"), "money"),
        ("Cash profit", k["profit"], g("profit"), "money"),
        ("Cash margin", k["margin"], g("margin"), "pp"),
        ("Late-delivery penalties", k["penalty"], g("penalty"), "money"),
    ]

    def show(v, kind, label):
        if v is None:
            return "n/a"
        if "rate" in label.lower() or "margin" in label.lower():
            return pct(v)
        if kind == "money" or "revenue" in label.lower():
            return money(v)
        return num(v, 1 if "days" in label else 0)

    tbl = pd.DataFrame([{"Metric": r[0], "This period": show(r[1], r[3], r[0]),
                         "Previous period": show(r[2], r[3], r[0]) if prev else "n/a",
                         "Change": _delta(r[1], r[2], r[3]) or "n/a"} for r in rows])
    facts = [
        f"{k['inquiries']} quotes issued, {k['won']} accepted (win rate {pct(k['win_rate'])}).",
        f"{k['delivered']} orders delivered; {pct(k['on_time'])} on time ({k['late']} late), average lead time {num(k['avg_lead'])} days.",
        f"Cash revenue {money(k['revenue'])} vs cash costs {money(k['cost'])} = profit {money(k['profit'])} (margin {pct(k['margin'])}).",
        f"Late-delivery penalties cost {money(k['penalty'])} in this period.",
    ]
    kp = [
        ("Cash profit", money(k["profit"]), _delta(k["profit"], g("profit"), "money")),
        ("Cash margin", pct(k["margin"]), _delta(k["margin"], g("margin"), "pp")),
        ("Win rate", pct(k["win_rate"]), _delta(k["win_rate"], g("win_rate"), "pp")),
        ("On-time delivery", pct(k["on_time"]), _delta(k["on_time"], g("on_time"), "pp")),
    ]
    notes = ["Cash basis: revenue = deposits + final payments received in the period; costs = wages, overheads and timber purchases.",
             "Quote metrics use the quote creation date; delivery metrics use the completion date."]
    if p.note:
        notes.append(p.note)
    return ToolResult("business_summary", "Business scorecard", p.label, facts, tbl, None, kp, notes,
                      ["orders_data.csv", "financial_ledger.csv"], {"period": period})


# ----------------------------------------------------------------------------- TOOL 2: product profitability


def product_profitability(period: str | None = "all") -> ToolResult:
    ds = D.load()
    p = resolve_period(period)
    t = _delivered(p)
    if t.empty:
        return ToolResult("product_profitability", "Product profitability", p.label, ["No delivered orders in this period."])
    led = _ledger(p)
    fixed_pool = led.loc[led["category"].isin(["Wages", "Overheads"]), "amount"].sum()
    rate = fixed_pool / t["labour_hours"].sum()                       # $ of wages+overheads per labour hour
    t = t.assign(alloc=t["labour_hours"] * rate)
    g = t.groupby("productType").agg(
        orders=("id", "count"), avg_price=("quotePrice", "mean"), net_revenue=("net_revenue", "sum"),
        material=("materialCost", "sum"), contribution=("contribution", "sum"), hours=("labour_hours", "sum"),
        alloc=("alloc", "sum"), late=("is_late", "mean")).reindex([x for x in PRODUCTS if x in t["productType"].unique()])
    g["contrib_margin"] = g["contribution"] / g["net_revenue"]
    g["profit_full"] = g["contribution"] - g["alloc"]
    g["full_margin"] = g["profit_full"] / g["net_revenue"]
    g["contrib_per_hour"] = g["contribution"] / g["hours"]
    g["hours_per_order"] = g["hours"] / g["orders"]
    tbl = pd.DataFrame({
        "Product": g.index, "Orders delivered": g["orders"].values, "Avg price": g["avg_price"].round(0).values,
        "Net revenue": g["net_revenue"].round(0).values, "Material cost": g["material"].round(0).values,
        "Contribution": g["contribution"].round(0).values, "Contribution margin %": (g["contrib_margin"] * 100).round(1).values,
        "Labour hrs/order": g["hours_per_order"].round(1).values, "Contribution $/labour hr": g["contrib_per_hour"].round(0).values,
        "Fully-loaded profit": g["profit_full"].round(0).values, "Fully-loaded margin %": (g["full_margin"] * 100).round(1).values,
    })
    best_m, worst_m = g["contrib_margin"].idxmax(), g["contrib_margin"].idxmin()
    best_h, worst_h = g["contrib_per_hour"].idxmax(), g["contrib_per_hour"].idxmin()
    best_f, best_fm, worst_fm = g["profit_full"].idxmax(), g["full_margin"].idxmax(), g["full_margin"].idxmin()
    most_hrs, least_hrs = g["hours_per_order"].idxmax(), g["hours_per_order"].idxmin()
    total_full = g["profit_full"].sum()
    lead = (f"{best_f} is the most profitable product after wages and overheads: {money(g.loc[best_f, 'profit_full'])} profit "
            f"({pct(g.loc[best_f, 'full_margin'])} margin)" if best_f == best_fm else
            f"{best_f} earns the most profit in dollars after wages and overheads ({money(g.loc[best_f, 'profit_full'])}), while {best_fm} has the best margin ({pct(g.loc[best_fm, 'full_margin'])})")
    facts = [
        f"{lead}; {worst_fm} is the weakest by margin ({pct(g.loc[worst_fm, 'full_margin'])}, {money(g.loc[worst_fm, 'profit_full'])}).",
        f"On timber cost alone (contribution margin) {best_m} looks best ({pct(g.loc[best_m, 'contrib_margin'])}) and {worst_m} lowest ({pct(g.loc[worst_m, 'contrib_margin'])})"
        + (" - but the ranking changes once labour is counted." if best_m != best_f else "."),
        f"Labour per order ranges from {num(g.loc[least_hrs, 'hours_per_order'])} hours ({least_hrs}) to {num(g.loc[most_hrs, 'hours_per_order'])} hours ({most_hrs}). "
        f"Contribution per labour hour is highest for {best_h} ({money(g.loc[best_h, 'contrib_per_hour'])}/hr) and lowest for {worst_h} ({money(g.loc[worst_h, 'contrib_per_hour'])}/hr).",
        f"Wages + overheads in the period were {money(fixed_pool)}, allocated at {money(rate)} per labour hour; all products together earned {money(total_full)} after that.",
    ]
    fig = make_subplots(rows=1, cols=2, subplot_titles=("Contribution margin (% of net revenue)", "Fully-loaded profit after wages and overheads"))
    labels = [_wrap(x) for x in g.index]
    cols = [PRODUCT_COLORS[x] for x in g.index]
    fig.add_trace(go.Bar(x=labels, y=g["contrib_margin"] * 100, marker=dict(color=cols, cornerradius=4), showlegend=False,
                         text=[f"{v:.0f}%" for v in g["contrib_margin"] * 100], textposition="outside", cliponaxis=False,
                         hovertemplate="%{x}: %{y:.1f}%<extra></extra>"), row=1, col=1)
    fig.add_trace(go.Bar(x=labels, y=g["profit_full"], marker=dict(color=cols, cornerradius=4), showlegend=False,
                         text=[money(v) for v in g["profit_full"]], textposition="outside", cliponaxis=False,
                         hovertemplate="%{x}: $%{y:,.0f}<extra></extra>"), row=1, col=2)
    fig.update_yaxes(ticksuffix="%", rangemode="tozero", range=[0, 100], row=1, col=1)
    fig.update_yaxes(tickprefix="$", tickformat=",.0f", rangemode="tozero", row=1, col=2)
    fig.update_annotations(font=dict(size=12, color=INK_2))
    style(fig, "Product profitability", 400, legend=False)
    notes = ["Contribution = net revenue (after late penalties) minus timber cost. Fully-loaded also subtracts wages and overheads, allocated across "
             "delivered orders by labour hours (an assumption - wages are a fixed daily cost in the ledger).",
             "Based on orders delivered in the period."]
    if p.note:
        notes.append(p.note)
    return ToolResult("product_profitability", "Which product type is most profitable?", p.label, facts, tbl, fig, [], notes,
                      ["orders_data.csv", "financial_ledger.csv"], {"period": period})


# ----------------------------------------------------------------------------- TOOL 3: production bottleneck


def production_bottleneck(period: str | None = "last_quarter") -> ToolResult:
    ds = D.load()
    p = resolve_period(period)
    t = _delivered(p)
    if t.empty:
        return ToolResult("production_bottleneck", "Production bottleneck", p.label, ["No delivered orders in this period."])
    wait, design, prod = t["design_wait_days"].mean(), t["design_days"].mean(), t["production_days"].mean()
    lead = t["lead_days"].mean()
    stage_days = pd.DataFrame({"Stage": ["Waiting for a designer", "Design", "Production (milling, joinery, finishing)"],
                               "Avg days": [wait, design, prod]})
    stage_days["Share of lead time"] = (stage_days["Avg days"] / (wait + design + prod)).map(pct)

    hrs = t[["designHours", "millingHours", "joineryHours", "finishingHours"]].mean()
    sd = ds.staff_daily.loc[p.start:p.end]
    n_days = max(len(sd), 1)
    dem_design, dem_make = t["designHours"].sum(), t["maker_hours"].sum()
    cap_design = (sd["designer"] - sd["designer_leave"]).sum() * WORK_HOURS_PER_DAY
    cap_make = (sd["maker"] - sd["maker_leave"]).sum() * WORK_HOURS_PER_DAY
    util_d, util_m = dem_design / cap_design if cap_design else np.nan, dem_make / cap_make if cap_make else np.nan
    wip = _wip_daily()
    w = wip.loc[p.start:p.end]
    peak_day = w.idxmax()

    # monthly trend across the whole dataset for the chart, plus link between queue size and production time
    mt = ds.timeline.assign(m=ds.timeline["completionDate"].dt.to_period("M").dt.to_timestamp())
    mt = mt.groupby("m").agg(prod=("production_days", "mean"), late=("is_late", "mean"), n=("id", "count"))
    mt = mt[mt["n"] >= 5]
    wm = wip.resample("MS").mean().reindex(mt.index)
    corr = np.corrcoef(wm.values, mt["prod"].values)[0, 1]
    corr_late = np.corrcoef(wm.values, mt["late"].values)[0, 1]
    worst_m = mt["prod"].idxmax()

    open_orders = ds.orders[ds.orders["in_progress"]]
    stages = open_orders["currentStage"].value_counts()
    top_stage = stages.idxmax() if len(stages) else None

    facts = [
        f"Average lead time was {num(lead)} days: {num(wait)} waiting for a designer, {num(design)} in design and {num(prod)} in production "
        f"(production = {pct(prod / (wait + design + prod), 0)} of the elapsed time).",
        f"Production is the bottleneck: designers were never the constraint (waiting time {num(wait, 2)} days).",
        f"Estimated utilisation (hours of work / available staff hours at {WORK_HOURS_PER_DAY} h/day): designers {pct(util_d, 0)}, makers {pct(util_m, 0)}.",
        f"Within the maker stage, joinery takes the most hours per order ({num(hrs['joineryHours'])} h), then milling ({num(hrs['millingHours'])} h) and finishing ({num(hrs['finishingHours'])} h).",
        f"Orders in production at once averaged {num(w.mean())} in this period and peaked at {int(w.max())} on {peak_day:%d %b %Y}.",
        f"Across the dataset, months with more orders in production had longer production times (correlation {num(corr, 2)}) and more late deliveries "
        f"(correlation {num(corr_late, 2)}). The slowest month was {worst_m:%B %Y} ({num(mt.loc[worst_m, 'prod'])} production days, {pct(mt.loc[worst_m, 'late'], 0)} late).",
    ]
    if top_stage:
        facts.append(f"At the end of the data {len(open_orders)} accepted orders were still open; most ({stages.iloc[0]}) were in {top_stage.title()}.")
    tbl = stage_days.copy()

    fig = make_subplots(rows=2, cols=2, column_widths=[0.42, 0.58], row_heights=[0.5, 0.5], shared_xaxes=False, vertical_spacing=0.2, horizontal_spacing=0.1,
                        specs=[[{"rowspan": 2}, {}], [None, {}]],
                        subplot_titles=("Where an order's time goes (average days)", "Orders in production at once (monthly average)", "Average production days per order"))
    short = ["Waiting for designer", "Design", "Production"]
    fig.add_trace(go.Bar(x=[wait, design, prod], y=short, orientation="h", marker=dict(color=[CAT[2], CAT[0], CAT[1]], cornerradius=4),
                         text=[f"{v:.1f}" for v in (wait, design, prod)], textposition="outside", cliponaxis=False, showlegend=False,
                         hovertemplate="%{y}: %{x:.1f} days<extra></extra>"), row=1, col=1)
    fig.add_trace(go.Bar(x=wm.index, y=wm.values, marker=dict(color=CAT[0], cornerradius=3), showlegend=False,
                         hovertemplate="%{x|%b %Y}: %{y:.1f} orders<extra></extra>"), row=1, col=2)
    fig.add_trace(go.Scatter(x=mt.index, y=mt["prod"], mode="lines+markers", line=dict(color=CAT[1], width=2), showlegend=False,
                             marker=dict(size=6, line=dict(color=SURFACE, width=2)), hovertemplate="%{x|%b %Y}: %{y:.1f} days<extra></extra>"), row=2, col=2)
    fig.update_yaxes(autorange="reversed", row=1, col=1)
    fig.update_xaxes(range=[0, max(wait, design, prod) * 1.3], row=1, col=1)
    fig.update_yaxes(rangemode="tozero", row=1, col=2)
    fig.update_yaxes(rangemode="tozero", row=2, col=2)
    fig.update_annotations(font=dict(size=12, color=INK_2))
    style(fig, "Production bottleneck analysis", 440, legend=False)
    notes = ["Utilisation is an estimate (assumes an 8-hour working day, 7 days a week, since wages are paid daily). It is only a relative indicator.",
             "Production days = design finished to order completed (milling, joinery and finishing are done by makers; the data does not time each step).",
             "Correlation shows association, not proof of cause."]
    if p.note:
        notes.append(p.note)
    return ToolResult("production_bottleneck", "What is the biggest production bottleneck?", p.label, facts, tbl, fig, [], notes,
                      ["orders_data.csv", "design_log.csv", "hr_roster.csv", "hr_events_log.csv"], {"period": period})


# ----------------------------------------------------------------------------- TOOL 4: late deliveries

_LATE_GROUPS = {"product": "productType", "quarter": "completed_q", "month": "completed_m", "material": "materialType",
                "complexity": "complexity_band", "designer_skill": "designerSkill"}


def late_delivery_analysis(period: str | None = "all", group_by: str = "product") -> ToolResult:
    p = resolve_period(period)
    t = _delivered(p)
    gb = group_by if group_by in _LATE_GROUPS else "product"
    if t.empty:
        return ToolResult("late_delivery_analysis", "Late deliveries", p.label, ["No delivered orders in this period."])
    col = _LATE_GROUPS[gb]
    t = t.assign(designerSkill=t["designerSkill"].map({"high": "High-skill designer", "low": "Low-skill designer"}).fillna("Unknown"))
    g = t.groupby(col).agg(delivered=("id", "count"), late=("is_late", "sum"), late_rate=("is_late", "mean"),
                           penalty=("penalty_amt", "sum"), prod=("production_days", "mean"))
    late_only = t[t["is_late"]].groupby(col)["lateDays"].mean()
    g["avg_days_late"] = late_only
    g = g.reset_index()
    if gb == "product":
        g = g.set_index(col).reindex([x for x in PRODUCTS if x in g[col].values]).reset_index()
    tbl = pd.DataFrame({gb.replace("_", " ").title(): g[col], "Delivered": g["delivered"], "Late": g["late"].astype(int),
                        "Late rate %": (g["late_rate"] * 100).round(1), "Avg days late": g["avg_days_late"].round(1),
                        "Penalties $": g["penalty"].round(0), "Avg production days": g["prod"].round(1)})
    tot_late, tot = int(t["is_late"].sum()), len(t)
    gr = g[g["delivered"] >= 8] if (g["delivered"] >= 8).any() else g
    worst = gr.sort_values("late_rate", ascending=False).iloc[0]
    most = gr.sort_values("late", ascending=False).iloc[0]
    g = gr if gb in ("quarter", "month") else g
    facts = [
        f"{tot_late} of {tot} delivered orders were late ({pct(tot_late / tot)}), costing {money(t['penalty_amt'].sum())} in penalties (10% off the final payment).",
        f"Highest late rate by {gb.replace('_', ' ')}: {worst[col]} ({pct(worst['late_rate'])}, {int(worst['late'])} of {int(worst['delivered'])}).",
        f"Most late orders in absolute terms: {most[col]} ({int(most['late'])} orders).",
        f"When orders are late they average {num(t.loc[t['is_late'], 'lateDays'].mean())} days late." if tot_late else "No late orders in this period.",
    ]
    fig = go.Figure()
    colors = [_color_for(str(v), i) if gb in ("product", "material") else CAT[0] for i, v in enumerate(g[col])]
    fig.add_trace(go.Bar(x=[_wrap(v) if gb == "product" else str(v) for v in g[col]], y=g["late_rate"] * 100, marker=dict(color=colors, cornerradius=4), showlegend=False,
                         text=[f"{v * 100:.0f}% ({int(n)}/{int(d)})" for v, n, d in zip(g["late_rate"], g["late"], g["delivered"])],
                         textposition="outside", textfont=dict(color=INK_2, size=11), cliponaxis=False,
                         hovertemplate="%{x}: %{y:.1f}% late<extra></extra>"))
    fig.update_layout(yaxis_title="Late rate", yaxis_ticksuffix="%")
    style(fig, f"Late-delivery rate by {gb.replace('_', ' ')}", legend=False)
    notes = ["Late = completed after the due date. Based on orders completed in the period. Groups with fewer than 8 delivered orders are left out of rankings and the chart."]
    if gb == "designer_skill":
        notes.append("Designer skill is the skill of the designer who finished the design stage; it is one factor among several.")
    if p.note:
        notes.append(p.note)
    return ToolResult("late_delivery_analysis", "Late deliveries", p.label, facts, tbl, fig, [], notes,
                      ["orders_data.csv", "design_log.csv"], {"period": period, "group_by": gb})


# ----------------------------------------------------------------------------- TOOL 5: resignations & staffing


def resignation_impact(period: str | None = "all", window_days: int = 30) -> ToolResult:
    ds = D.load()
    p = resolve_period(period)
    window = int(min(max(window_days or 30, 14), 120))
    t_all = ds.timeline
    ev = ds.people_events
    res = ev[ev["event"] == "Resignation"].copy()
    res["Role"] = (res["skillLevel"].str.title() + "-skill " + res["role"]).values
    in_p = res[p.mask(res["date"])]
    t_p = t_all[p.mask(t_all["completionDate"])]

    def after_mask(dates):
        m = pd.Series(False, index=t_all.index)
        for d in dates:
            m |= (t_all["completionDate"] >= d) & (t_all["completionDate"] < d + pd.Timedelta(days=window))
        return m

    masks = {r: after_mask(in_p.loc[in_p["role"] == r, "date"]) for r in ("maker", "designer")}
    any_mask = masks["maker"] | masks["designer"]
    base = t_p[~any_mask.loc[t_p.index]]
    stat = lambda df: (len(df), 1 - df["is_late"].mean() if len(df) else np.nan, df["production_days"].mean() if len(df) else np.nan)
    n_base, ot_base, pd_base = stat(base)

    per = []
    for _, r in in_p.iterrows():
        pre = t_all[(t_all["completionDate"] >= r["date"] - pd.Timedelta(days=window)) & (t_all["completionDate"] < r["date"])]
        post = t_all[(t_all["completionDate"] >= r["date"]) & (t_all["completionDate"] < r["date"] + pd.Timedelta(days=window))]
        if len(post) < 5:
            continue          # too few orders to say anything (e.g. a resignation on the last day of data)
        per.append({"Resignation": r["Role"], "Date": r["date"].strftime("%Y-%m-%d"),
                    f"On-time % before ({window}d)": round((1 - pre["is_late"].mean()) * 100, 1) if len(pre) else None,
                    f"On-time % after ({window}d)": round((1 - post["is_late"].mean()) * 100, 1),
                    "Prod. days before": round(pre["production_days"].mean(), 1) if len(pre) else None,
                    "Prod. days after": round(post["production_days"].mean(), 1), "Orders after": len(post)})
    tbl = pd.DataFrame(per)

    sdq = ds.staff_daily.resample("QE").mean()
    sdq.index = [f"{i.year}-Q{i.quarter}" for i in sdq.index]
    ev_q = ev.assign(q=D.qlabel(ev["date"]))
    q_res = ev_q[ev_q["event"] == "Resignation"].groupby("q").size()
    tq = t_all.groupby("completed_q").agg(on_time=("is_late", lambda s: 1 - s.mean()), n=("id", "count"))
    tq = tq[tq["n"] >= 10]
    q_tbl = tq.join(sdq[["designer", "maker"]]).join(q_res.rename("resignations")).fillna({"resignations": 0})
    q_tbl = q_tbl[[bool(p.start <= pd.Period(i).end_time and pd.Period(i).start_time <= p.end) for i in q_tbl.index]]

    sdp = ds.staff_daily.loc[p.start:p.end]
    hires_p = int(ds.people_events[p.mask(ds.people_events["date"])]["event"].eq("Hired").sum())
    by_role = in_p.groupby("Role").size().sort_values(ascending=False)
    facts = [
        f"{len(in_p)} resignations in the period ({', '.join(f'{v} {k.lower()}' for k, v in by_role.items()) or 'none'}) and {hires_p} hires; "
        f"average headcount was {num(sdp['designer'].mean())} designers and {num(sdp['maker'].mean())} makers.",
    ]
    gaps = {}
    for r in ("maker", "designer"):
        n_r = int((in_p["role"] == r).sum())
        if not n_r:
            continue
        n_a, ot_a, pd_a = stat(t_p[masks[r].loc[t_p.index]])
        gaps[r] = (ot_a - ot_base) * 100 if n_a >= 10 and n_base >= 10 else np.nan
        facts.append(f"Orders completed within {window} days after one of {n_r} {r} resignations: {pct(ot_a)} on time ({n_a} orders, {num(pd_a)} production days).")
    facts.append(f"Orders not within {window} days after any resignation: {pct(ot_base)} on time ({n_base} orders, {num(pd_base)} production days).")
    for r, g in gaps.items():
        if np.isnan(g):
            continue
        if g <= -5:
            facts.append(f"On-time delivery was {abs(g):.0f} points lower after {r} resignations than at other times.")
        elif g >= 5:
            facts.append(f"On-time delivery was {g:.0f} points higher after {r} resignations than at other times (see the per-resignation table for individual events).")
        else:
            facts.append(f"No clear difference in on-time delivery after {r} resignations.")
    sev = tbl[tbl.iloc[:, 3] <= tbl.iloc[:, 2] - 10] if len(tbl) else tbl
    if len(sev):
        facts.append(f"{len(sev)} resignation(s) were followed by a clear drop in on-time delivery (10+ points): "
                     + "; ".join(f"{r['Resignation']} on {r['Date']} ({r.iloc[2]:.0f}% -> {r.iloc[3]:.0f}%)" for _, r in sev.iterrows()) + ".")
    late_all, late_out = int(t_p["is_late"].sum()), int(base["is_late"].sum())
    if late_all:
        facts.append(f"{late_out} of the {late_all} late orders in the period were completed outside any {window}-day post-resignation window, "
                     "so workload and other factors matter too - see the bottleneck analysis.")

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.12, row_heights=[0.45, 0.55],
                        subplot_titles=("Average headcount", "On-time delivery rate"))
    x = list(q_tbl.index)
    fig.add_trace(go.Scatter(x=x, y=q_tbl["designer"], name="Designers", mode="lines+markers", line=dict(color=CAT[0], width=2),
                             marker=dict(size=8, line=dict(color=SURFACE, width=2))), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=q_tbl["maker"], name="Makers", mode="lines+markers", line=dict(color=CAT[1], width=2),
                             marker=dict(size=8, line=dict(color=SURFACE, width=2))), row=1, col=1)
    fig.add_trace(go.Bar(x=x, y=q_tbl["on_time"] * 100, name="On-time %", showlegend=False, marker=dict(color=CAT[2], cornerradius=4),
                         text=[f"{v * 100:.0f}%" for v in q_tbl["on_time"]], textposition="outside", cliponaxis=False,
                         customdata=q_tbl["resignations"], hovertemplate="%{x}: %{y:.1f}% on time<br>Resignations: %{customdata:.0f}<extra></extra>"), row=2, col=1)
    fig.update_yaxes(range=[0, 10], row=1, col=1)
    fig.update_yaxes(range=[0, 115], ticksuffix="%", row=2, col=1)
    fig.update_annotations(font=dict(size=12, color=INK_2))
    style(fig, "Staffing vs on-time delivery, by quarter", 520)
    notes = [f"Comparison uses a {window}-day window after each resignation. With only about 12 resignations the results are indicative, not statistical proof.",
             "Individual employees are never shown - only role and skill level.", "Correlation is not causation."]
    if p.note:
        notes.append(p.note)
    return ToolResult("resignation_impact", "Impact of resignations on on-time delivery", p.label, facts, tbl, fig, [], notes,
                      ["hr_events_log.csv", "hr_roster.csv", "orders_data.csv", "design_log.csv"], {"period": period, "window_days": window})


# ----------------------------------------------------------------------------- TOOL 6: workforce overview


def workforce_overview(period: str | None = "all") -> ToolResult:
    ds = D.load()
    p = resolve_period(period)
    sd = ds.staff_daily.loc[p.start:p.end]
    ev = ds.people_events[p.mask(ds.people_events["date"])]
    groups = [("designer", "high"), ("designer", "low"), ("maker", "high"), ("maker", "low")]
    rows = []
    for role, skill in groups:
        c = f"{role}_{skill}"
        h = ev[(ev["role"] == role) & (ev["skillLevel"] == skill)]
        rate = ds.wage_rates[(ds.wage_rates["role"] == role) & (ds.wage_rates["skillLevel"] == skill)]["hourly_rate"].iloc[0]
        rows.append({"Group": f"{skill.title()}-skill {role}s", "Start": int(sd[c].iloc[0]), "Hired": int((h["event"] == "Hired").sum()),
                     "Resigned": int((h["event"] == "Resignation").sum()), "End": int(sd[c].iloc[-1]),
                     "Avg leave days/yr": round(sd[f"{c}_leave"].sum() / max(len(sd) / 365, 0.01), 0), "Hourly rate": f"${rate:,.0f}"})
    tbl = pd.DataFrame(rows)
    led = _ledger(p)
    wages = led.loc[led["category"] == "Wages", "amount"].sum()
    rev = led.loc[led["type"] == "REVENUE", "amount"].sum()
    avg_hc = sd[["designer", "maker"]].sum(axis=1).mean()
    n_res = int((ev["event"] == "Resignation").sum())
    years = max(len(sd) / 365, 0.01)
    facts = [
        f"Team size averaged {num(avg_hc)} staff ({num(sd['designer'].mean())} designers, {num(sd['maker'].mean())} makers).",
        f"{int((ev['event'] == 'Hired').sum())} hires and {n_res} resignations in the period - an annualised attrition rate of about {pct(n_res / years / avg_hc)}.",
        f"Wages cost {money(wages)} ({pct(wages / rev) if rev else 'n/a'} of cash revenue).",
        f"Low-skill designers were {pct(sd['designer_low'].iloc[0] / max(sd['designer'].iloc[0], 1), 0)} of designers at the start of the period and "
        f"{pct(sd['designer_low'].iloc[-1] / max(sd['designer'].iloc[-1], 1), 0)} at the end.",
    ]
    evq = ev.assign(q=D.qlabel(ev["date"]))
    qh = evq.groupby(["q", "event"]).size().unstack(fill_value=0).reindex(columns=["Hired", "Resignation"], fill_value=0)
    days_in_q = sd.groupby(sd.index.to_period("Q")).size()
    full_q = {f"{i.year}-Q{i.quarter}" for i, n in days_in_q.items() if n >= 60}
    qh = qh[qh.index.isin(full_q)] if len(full_q) else qh
    fig = make_subplots(rows=1, cols=2, column_widths=[0.6, 0.4], subplot_titles=("Hires and resignations by quarter", "Average headcount by group"))
    fig.add_trace(go.Bar(x=list(qh.index), y=qh["Hired"], name="Hired", marker=dict(color=CAT[0], cornerradius=3)), row=1, col=1)
    fig.add_trace(go.Bar(x=list(qh.index), y=qh["Resignation"], name="Resigned", marker=dict(color=CAT[1], cornerradius=3)), row=1, col=1)
    names = [f"{s.title()}-skill<br>{r}s" for r, s in groups]
    avgs = [sd[f"{r}_{s}"].mean() for r, s in groups]
    fig.add_trace(go.Bar(x=names, y=avgs, marker=dict(color=CAT[:4], cornerradius=4), showlegend=False,
                         text=[f"{v:.1f}" for v in avgs], textposition="outside", cliponaxis=False,
                         hovertemplate="%{x}: %{y:.1f}<extra></extra>"), row=1, col=2)
    fig.update_layout(barmode="group")
    fig.update_yaxes(rangemode="tozero", dtick=1, row=1, col=1)
    fig.update_yaxes(rangemode="tozero", range=[0, max(avgs) * 1.2], row=1, col=2)
    fig.update_annotations(font=dict(size=12, color=INK_2))
    style(fig, "Team makeup and turnover", 400)
    notes = ["Only role and skill-level aggregates are shown; individual pay, leave and IDs are restricted."]
    if p.note:
        notes.append(p.note)
    return ToolResult("workforce_overview", "Workforce overview", p.label, facts, tbl, fig, [], notes,
                      ["hr_roster.csv", "hr_events_log.csv", "financial_ledger.csv"], {"period": period})


# ----------------------------------------------------------------------------- TOOL 7: inventory


def inventory_analysis(period: str | None = "all", material: str | None = None) -> ToolResult:
    ds = D.load()
    p = resolve_period(period)
    mat = resolve_material(material)
    inv = ds.inventory[p.mask(ds.inventory["date"])]
    led = _ledger(p)
    buys = led[led["category"] == "Material purchases"].copy()
    buys["material"] = buys["description"].str.extract(r"of (.+)$", expand=False)
    rows = []
    for m in MATERIALS:
        if mat and m != mat:
            continue
        i = inv[inv["material"] == m]
        c = i[i["type"] == "CONSUME"]
        r = i[i["type"] == "REORDER"]
        rows.append({"Material": m, "Units used": int(c["quantity"].sum()), "Reorders": len(r), "Units reordered": int(r["quantity"].sum()),
                     "Purchase spend": round(buys.loc[buys["material"] == m, "amount"].sum(), 0),
                     "Avg stock level": round(i["newLevel"].mean(), 1), "Lowest level": int(i["newLevel"].min()),
                     "Stock-outs (level 0)": int((i["newLevel"] <= 0).sum()), "Low-stock events (<=2)": int((i["newLevel"] <= 2).sum())})
    tbl = pd.DataFrame(rows)
    top = tbl.sort_values("Low-stock events (<=2)", ascending=False).iloc[0]
    most_used = tbl.sort_values("Units used", ascending=False).iloc[0]
    facts = [
        f"{most_used['Material']} is the most-used timber ({most_used['Units used']} units), and {tbl.sort_values('Purchase spend', ascending=False).iloc[0]['Material']} is the biggest spend "
        f"({money(tbl['Purchase spend'].max())}).",
        f"{top['Material']} runs low most often: {top['Low-stock events (<=2)']} low-stock events (level <= 2 units) and {top['Stock-outs (level 0)']} stock-out event(s).",
        f"Total timber purchases were {money(tbl['Purchase spend'].sum())} across {int(tbl['Reorders'].sum())} reorders.",
        f"Average stock level was lowest for {tbl.sort_values('Avg stock level').iloc[0]['Material']} ({num(tbl['Avg stock level'].min())} units on average).",
    ]
    days_q = led.groupby("quarter")["date"].nunique()
    bq = buys.assign(qtr=buys["quarter"]).groupby(["qtr", "material"])["amount"].sum().unstack(fill_value=0)
    bq = bq[bq.index.isin(days_q[days_q >= 60].index)]
    fig = make_subplots(rows=1, cols=2, column_widths=[0.38, 0.62],
                        subplot_titles=("Low-stock events (2 units or fewer)", "Timber purchases by quarter"))
    fig.add_trace(go.Bar(x=[_wrap(m) for m in tbl["Material"]], y=tbl["Low-stock events (<=2)"], showlegend=False,
                         marker=dict(color=[MATERIAL_COLORS[m] for m in tbl["Material"]], cornerradius=4),
                         text=tbl["Low-stock events (<=2)"], textposition="outside", cliponaxis=False,
                         hovertemplate="%{x}: %{y} events<extra></extra>"), row=1, col=1)
    for m in MATERIALS:
        if (mat and m != mat) or m not in bq.columns:
            continue
        fig.add_trace(go.Bar(x=list(bq.index), y=bq[m], name=m, marker=dict(color=MATERIAL_COLORS[m], cornerradius=3),
                             hovertemplate="%{x}: $%{y:,.0f}<extra>" + m + "</extra>"), row=1, col=2)
    fig.update_layout(barmode="group")
    fig.update_yaxes(rangemode="tozero", row=1, col=1)
    fig.update_yaxes(tickprefix="$", tickformat=",.0f", rangemode="tozero", row=1, col=2)
    fig.update_annotations(font=dict(size=12, color=INK_2))
    style(fig, "Timber: how often stock runs low, and what we spend", 400)
    notes = ["Stock level = 'newLevel' after each consume/reorder event in the inventory log."]
    if p.note:
        notes.append(p.note)
    return ToolResult("inventory_analysis", "Inventory and materials", p.label, facts, tbl, fig, [], notes,
                      ["inventory_log.csv", "financial_ledger.csv"], {"period": period, "material": mat})


# ----------------------------------------------------------------------------- TOOL 8: sales & pricing


def sales_pricing_analysis(period: str | None = "all", product: str | None = None) -> ToolResult:
    p = resolve_period(period)
    prod = resolve_product(product)
    o = _created(p, prod)
    if o.empty:
        return ToolResult("sales_pricing_analysis", "Sales and pricing", p.label, ["No quotes in this period."])
    g = o.groupby("productType").agg(quotes=("id", "count"), won=("won", "sum"), win_rate=("won", "mean"), avg_quote=("quotePrice", "mean"))
    g = g.reindex([x for x in PRODUCTS if x in g.index])
    won_p = o[o["won"]].groupby("productType")["quotePrice"].mean()
    lost_p = o[~o["won"]].groupby("productType")["quotePrice"].mean()
    g["avg_won"], g["avg_lost"] = won_p, lost_p
    lost_value = o.loc[~o["won"], "quotePrice"].sum()
    band_order = ["Under $8k", "$8-10k", "$10-12k", "$12-14k", "$14k+"]
    bands = o.groupby("price_band").agg(quotes=("id", "count"), win_rate=("won", "mean")).reindex(band_order).dropna()
    tbl = pd.DataFrame({"Product": g.index, "Quotes": g["quotes"].values, "Accepted": g["won"].astype(int).values,
                        "Win rate %": (g["win_rate"] * 100).round(1).values, "Avg quote": g["avg_quote"].round(0).values,
                        "Avg quote (won)": g["avg_won"].round(0).values, "Avg quote (lost)": g["avg_lost"].round(0).values})
    best, worst = g["win_rate"].idxmax(), g["win_rate"].idxmin()
    diff = o.loc[o["won"], "quotePrice"].mean() - o.loc[~o["won"], "quotePrice"].mean()
    qn = o.groupby("created_q")["won"].agg(["mean", "count"])
    qs = qn.loc[qn["count"] >= 30, "mean"] if (qn["count"] >= 30).any() else qn["mean"]
    facts = [
        f"{len(o)} quotes issued, {int(o['won'].sum())} accepted: overall win rate {pct(o['won'].mean())}. Roughly {pct(1 - o['won'].mean(), 0)} of quotes are lost.",
        f"Best win rate: {best} ({pct(g.loc[best, 'win_rate'])}); weakest: {worst} ({pct(g.loc[worst, 'win_rate'])}).",
        f"Accepted quotes averaged {money(o.loc[o['won'], 'quotePrice'].mean())} vs {money(o.loc[~o['won'], 'quotePrice'].mean())} for lost quotes "
        f"(difference {money(diff)}), so price alone does not separate wins from losses." if abs(diff) < 800 else
        f"Accepted quotes averaged {money(o.loc[o['won'], 'quotePrice'].mean())} vs {money(o.loc[~o['won'], 'quotePrice'].mean())} for lost quotes.",
        f"Lost quotes were worth {money(lost_value)} in total.",
        f"Win rate by price band ranged from {pct(bands['win_rate'].min(), 0)} to {pct(bands['win_rate'].max(), 0)}.",
        f"Quarterly win rate ranged from {pct(qs.min(), 0)} to {pct(qs.max(), 0)}.",
    ]
    fig = make_subplots(rows=1, cols=2, subplot_titles=("Win rate by product", "Win rate by quote price band"))
    fig.add_trace(go.Bar(x=[_wrap(x) for x in g.index], y=g["win_rate"] * 100, marker=dict(color=[PRODUCT_COLORS[x] for x in g.index], cornerradius=4), showlegend=False,
                         text=[f"{v * 100:.0f}%" for v in g["win_rate"]], textposition="outside", cliponaxis=False,
                         hovertemplate="%{x}: %{y:.1f}%<extra></extra>"), row=1, col=1)
    fig.add_trace(go.Bar(x=list(bands.index), y=bands["win_rate"] * 100, marker=dict(color=CAT[0], cornerradius=4), showlegend=False,
                         text=[f"{v * 100:.0f}%" for v in bands["win_rate"]], textposition="outside", cliponaxis=False,
                         customdata=bands["quotes"], hovertemplate="%{x}: %{y:.1f}% (%{customdata} quotes)<extra></extra>"), row=1, col=2)
    fig.update_yaxes(range=[0, max(60, float(g["win_rate"].max() * 100) + 15)], ticksuffix="%")
    fig.update_annotations(font=dict(size=12, color=INK_2))
    style(fig, "Quote conversion", 380, legend=False)
    notes = ["Win rate = accepted quotes / quotes issued, by quote creation date. The data does not record why a customer declined."]
    if p.note:
        notes.append(p.note)
    return ToolResult("sales_pricing_analysis", "Sales conversion and pricing", p.label, facts, tbl, fig, [], notes,
                      ["orders_data.csv"], {"period": period, "product": prod})


def _wip_link() -> str:
    ds = D.load()
    mt = ds.timeline.assign(m=ds.timeline["completionDate"].dt.to_period("M").dt.to_timestamp()).groupby("m").agg(late=("is_late", "mean"), n=("id", "count"))
    mt = mt[mt["n"] >= 5]
    wm = _wip_daily().resample("MS").mean().reindex(mt.index)
    hi = wm >= 12
    if hi.sum() and (~hi).sum():
        return (f"Capacity link: in months when 12+ orders were in production at once, {pct(mt.loc[hi, 'late'].mean(), 0)} of deliveries were late "
                f"vs {pct(mt.loc[~hi, 'late'].mean(), 0)} in other months, so demand above the outlook needs extra maker capacity.")
    return "Capacity link: not enough variation in workload to compare."


# ----------------------------------------------------------------------------- TOOL 9: demand outlook


def demand_forecast(product: str | None = None, horizon_months: int = 3) -> ToolResult:
    ds = D.load()
    prod = resolve_product(product)
    h = int(min(max(horizon_months or 3, 1), 6))
    o = ds.orders if not prod else ds.orders[ds.orders["productType"] == prod]
    last_full = pd.Period(ds.as_of - pd.Timedelta(days=1), "M")
    m = o.groupby(o["creationDate"].dt.to_period("M")).size()
    m = m[m.index <= last_full].reindex(pd.period_range(m.index.min(), last_full, freq="M"), fill_value=0)
    recent, prior = m.iloc[-6:].sum(), m.iloc[-18:-12].sum() if len(m) >= 18 else np.nan
    growth = recent / prior if prior and not np.isnan(prior) else 1.0
    fut = pd.period_range(last_full + 1, periods=h, freq="M")
    fc = pd.Series([m.get(f - 12, m.iloc[-12:].mean()) * growth for f in fut], index=fut)
    yoy_year = m[m.index.year == last_full.year].sum(); prev_year = m[m.index.year == last_full.year - 1].sum()
    peak = m.groupby(m.index.month).mean().idxmax()
    tbl = pd.DataFrame({"Month": [str(x) for x in fut], "Expected quotes": fc.round(0).astype(int).values,
                        "Same month last year": [int(m.get(f - 12, 0)) for f in fut]})
    facts = [
        f"{'All products' if not prod else prod}: {int(yoy_year)} quotes in {last_full.year} vs {int(prev_year)} in {last_full.year - 1} ({(yoy_year / prev_year - 1) * 100:+.0f}%).",
        f"Indicative outlook for the next {h} months: about {int(fc.sum())} quotes (roughly {int(round(fc.sum() * o['won'].mean()))} accepted at the historical win rate of {pct(o['won'].mean(), 0)}).",
        f"On average the busiest month for new quotes is {pd.Timestamp(2000, int(peak), 1):%B}.",
        _wip_link(),
    ]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[x.to_timestamp() for x in m.index], y=m.values, name="Actual quotes", mode="lines+markers", line=dict(color=CAT[0], width=2),
                             marker=dict(size=6, line=dict(color=SURFACE, width=2))))
    fig.add_trace(go.Scatter(x=[m.index[-1].to_timestamp()] + [x.to_timestamp() for x in fut], y=[m.iloc[-1]] + list(fc.values), name="Outlook", mode="lines+markers",
                             line=dict(color=CAT[1], width=2, dash="dash"), marker=dict(size=6, line=dict(color=SURFACE, width=2))))
    fig.update_yaxes(rangemode="tozero", title="Quotes per month")
    fig.update_xaxes(type="date", tickformat="%b %Y", dtick="M3", tickangle=0)
    style(fig, f"Monthly quote demand and {h}-month outlook" + (f": {prod}" if prod else ""))
    notes = ["Simple, explainable outlook: same month last year scaled by the change in the last 6 months vs the same 6 months a year earlier. "
             "Only two years of data exist, so treat this as directional, not a statistical forecast."]
    return ToolResult("demand_forecast", "Demand outlook", f"{m.index.min()} to {last_full} + {h} months", facts, tbl, fig, [], notes,
                      ["orders_data.csv"], {"product": prod, "horizon_months": h})


# ----------------------------------------------------------------------------- TOOL 10: financial trend


def financial_trend(period: str | None = "all", granularity: str = "month") -> ToolResult:
    p = resolve_period(period)
    l = _ledger(p)
    gcol = "quarter" if granularity == "quarter" else "month"
    days_per = l.groupby(gcol)["date"].nunique()
    if len(days_per) > 1 and days_per.iloc[-1] < (60 if gcol == "quarter" else 15):
        l = l[l[gcol] != days_per.index[-1]]     # drop a trailing partial period (e.g. the single day of Jan 2027)
    pv = l.pivot_table(index=gcol, columns="category", values="amount", aggfunc="sum").fillna(0)
    for c in ["Deposits", "Final payments", "Wages", "Overheads", "Material purchases"]:
        if c not in pv:
            pv[c] = 0.0
    pv["Revenue"] = pv["Deposits"] + pv["Final payments"]
    pv["Costs"] = pv["Wages"] + pv["Overheads"] + pv["Material purchases"]
    pv["Profit"] = pv["Revenue"] - pv["Costs"]
    pv["Margin %"] = pv["Profit"] / pv["Revenue"] * 100
    tbl = pv[["Revenue", "Costs", "Wages", "Overheads", "Material purchases", "Profit", "Margin %"]].round(0).reset_index()
    tbl = tbl.rename(columns={gcol: gcol.title()})
    tot = pv[["Revenue", "Costs", "Wages", "Overheads", "Material purchases", "Profit"]].sum()
    best, worst = pv["Profit"].idxmax(), pv["Profit"].idxmin()
    facts = [
        f"Revenue {money(tot['Revenue'])}, costs {money(tot['Costs'])}, profit {money(tot['Profit'])} (margin {pct(tot['Profit'] / tot['Revenue'])}).",
        f"Cost mix: wages {pct(tot['Wages'] / tot['Costs'], 0)}, timber purchases {pct(tot['Material purchases'] / tot['Costs'], 0)}, overheads {pct(tot['Overheads'] / tot['Costs'], 0)}.",
        f"Best {gcol}: {best} ({money(pv.loc[best, 'Profit'])} profit); weakest: {worst} ({money(pv.loc[worst, 'Profit'])}).",
        f"{int((pv['Profit'] < 0).sum())} of {len(pv)} {gcol}s made a cash loss.",
        "Wages are a fixed daily cost, so profit depends mostly on how many orders are accepted and delivered.",
    ]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.14, row_heights=[0.55, 0.45],
                        subplot_titles=("Revenue vs costs", "Profit"))
    fig.add_trace(go.Scatter(x=pv.index, y=pv["Revenue"], name="Revenue", mode="lines+markers", line=dict(color=CAT[0], width=2), marker=dict(size=6, line=dict(color=SURFACE, width=2))), row=1, col=1)
    fig.add_trace(go.Scatter(x=pv.index, y=pv["Costs"], name="Costs", mode="lines+markers", line=dict(color=CAT[1], width=2), marker=dict(size=6, line=dict(color=SURFACE, width=2))), row=1, col=1)
    fig.add_trace(go.Bar(x=pv.index, y=pv["Profit"], name="Profit", marker=dict(color=CAT[2], cornerradius=4), showlegend=False,
                         hovertemplate="%{x}: $%{y:,.0f}<extra></extra>"), row=2, col=1)
    fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    fig.update_xaxes(tickangle=-45)
    fig.update_annotations(font=dict(size=12, color=INK_2))
    style(fig, f"Financial trend by {gcol}", 520)
    notes = ["Cash basis from the financial ledger: deposits arrive when a quote is accepted, final payments on delivery; timber is expensed when re-ordered."]
    if p.note:
        notes.append(p.note)
    return ToolResult("financial_trend", "Financial trend", p.label, facts, tbl, fig, [], notes, ["financial_ledger.csv"],
                      {"period": period, "granularity": gcol})


# ----------------------------------------------------------------------------- TOOL 11: data dictionary

DEFINITIONS = {
    "win rate": "Accepted quotes divided by quotes issued (status LOST = the customer did not accept).",
    "on-time rate": "Share of delivered orders completed on or before the due date.",
    "lead time": "Days from quote acceptance to completion.",
    "production days": "Days from design finished to order completed - the milling, joinery and finishing work done by makers.",
    "design wait": "Days between quote acceptance and a designer starting.",
    "late penalty": "A 10% reduction of the final (50%) payment on a late order.",
    "contribution margin": "Net revenue (after penalties) minus timber cost, as a share of net revenue.",
    "fully-loaded margin": "Contribution minus wages and overheads allocated to each order by labour hours.",
    "cash margin": "Ledger revenue minus ledger costs in the period, over ledger revenue.",
    "orders in production": "Accepted orders that are not yet completed on a given day.",
    "skill level": "High or low. Designers and makers are paid $45/h (high) or $30/h (low).",
    "complexity": "A 4-11 score. Design hours = 2x, milling 3x, joinery 4x and finishing 1x the complexity score.",
}
FILES = {
    "orders_data.csv": "One row per quote (1,166): product, status, hours per stage, timber, price, dates, delivery status, late days, penalty. Customer IDs are excluded.",
    "financial_ledger.csv": "Daily cash ledger (2,507 rows): deposits, final payments, wages, overheads, timber purchases.",
    "design_log.csv": "Design-stage assignments: start/end dates, duration and designer skill. Designer IDs are excluded.",
    "hr_events_log.csv + hr_roster.csv": "Hires, resignations and leave, with role and skill level. Employee IDs and individual pay are excluded.",
    "inventory_log.csv": "Timber consume/reorder events and stock levels for three timbers.",
}


def data_dictionary(topic: str | None = "all") -> ToolResult:
    ds = D.load()
    t = (topic or "all").lower()
    facts = [f"Data covers {ds.start:%d %b %Y} to {ds.as_of:%d %b %Y} (the simulator's 2-year run). 'Last quarter' means the last complete quarter in the data."]
    matched = [f"{k.title()}: {v}" for k, v in DEFINITIONS.items() if t != "all" and (t in k or k in t or any(w in k for w in t.split() if len(w) > 3))]
    if matched:
        facts += matched
    if t in ("all", "files", "data", "sources", "dataset") or not matched:
        facts += [f"{k}: {v}" for k, v in FILES.items()]
    if t in ("all", "metrics", "definitions", "glossary") or not matched:
        facts += [f"{k.title()}: {v}" for k, v in DEFINITIONS.items()]
    return ToolResult("data_dictionary", "Data dictionary and definitions", "n/a", facts, None, None, [],
                      ["Restricted and never shown: customer IDs, customer details, individual employee IDs, individual pay and leave."],
                      ["all six CSV files"], {"topic": topic})


# ----------------------------------------------------------------------------- registry

REGISTRY = {
    "business_summary": dict(fn=business_summary, group="Overview",
        desc="Scorecard of headline KPIs (quotes, win rate, deliveries, on-time rate, revenue, costs, profit, margin, penalties) for a period, compared with the previous period. Use for general 'how are we doing' questions.",
        params={"period": ("string", "Period, e.g. 'last_quarter', '2025', '2026-Q1', 'Sep 2025', 'last_90_days', 'all'. Default last_quarter.")}),
    "product_profitability": dict(fn=product_profitability, group="Profitability",
        desc="Profit and margin by product type (contribution margin, fully-loaded margin after wages/overheads, profit per labour hour). Use for 'which product is most profitable / worth making'.",
        params={"period": ("string", "Period; default 'all'.")}),
    "production_bottleneck": dict(fn=production_bottleneck, group="Operations",
        desc="Finds the production bottleneck: how many days orders spend waiting for design, in design and in production, staff utilisation, orders in production at once, and how workload relates to slow/late months.",
        params={"period": ("string", "Period, e.g. 'last_quarter'.")}),
    "late_delivery_analysis": dict(fn=late_delivery_analysis, group="Operations",
        desc="Late deliveries and penalties, grouped by product, quarter, month, material, complexity or designer_skill.",
        params={"period": ("string", "Period; default 'all'."),
                "group_by": ("string", "One of: product, quarter, month, material, complexity, designer_skill.")}),
    "resignation_impact": dict(fn=resignation_impact, group="People",
        desc="Impact of employee resignations on on-time delivery and production time: before/after each resignation, quarterly headcount vs on-time rate.",
        params={"period": ("string", "Period; default 'all'."), "window_days": ("integer", "Days after a resignation to examine (default 30).")}),
    "workforce_overview": dict(fn=workforce_overview, group="People",
        desc="Headcount by role and skill, hires, resignations, attrition, leave and wage cost as aggregates. Never individuals.",
        params={"period": ("string", "Period; default 'all'.")}),
    "inventory_analysis": dict(fn=inventory_analysis, group="Materials",
        desc="Timber stock levels, usage, reorders, purchase spend, low-stock events and stock-outs for American Walnut, Tasmanian Oak and Victorian Ash.",
        params={"period": ("string", "Period; default 'all'."), "material": ("string", "Optional: walnut, oak or ash.")}),
    "sales_pricing_analysis": dict(fn=sales_pricing_analysis, group="Sales",
        desc="Quote conversion: win rate by product and price band, average price of won vs lost quotes, value of lost quotes.",
        params={"period": ("string", "Period; default 'all'."), "product": ("string", "Optional product name.")}),
    "demand_forecast": dict(fn=demand_forecast, group="Sales",
        desc="Monthly quote demand, seasonality, year-on-year change and a simple 1-6 month outlook (optionally for one product).",
        params={"product": ("string", "Optional product name."), "horizon_months": ("integer", "Months to project, 1-6 (default 3).")}),
    "financial_trend": dict(fn=financial_trend, group="Profitability",
        desc="Revenue, costs (wages, overheads, timber) and profit by month or quarter from the financial ledger.",
        params={"period": ("string", "Period; default 'all'."), "granularity": ("string", "'month' or 'quarter'.")}),
    "data_dictionary": dict(fn=data_dictionary, group="Help",
        desc="Explains metric definitions, data files, coverage and assumptions. Use when the user asks what a term means or what data the agent uses.",
        params={"topic": ("string", "Term or topic, e.g. 'production days', 'win rate', 'files', 'all'.")}),
}


def run_tool(name: str, args: dict | None) -> ToolResult:
    """Validate arguments and run a tool. Errors become a readable ToolResult, never an exception."""
    spec = REGISTRY.get(name)
    if not spec:
        return ToolResult(name, "Unknown tool", "n/a", [f"There is no analysis called '{name}'."])
    clean = {}
    for k, (typ, _) in spec["params"].items():
        if args and args.get(k) not in (None, ""):
            v = args[k]
            try:
                clean[k] = int(float(v)) if typ == "integer" else float(v) if typ == "number" else str(v)
            except (TypeError, ValueError):
                pass
    try:
        return spec["fn"](**clean)
    except Exception as e:   # noqa: BLE001 - surface a safe message rather than crash the chat
        return ToolResult(name, "Analysis error", "n/a", [f"The analysis could not be completed ({type(e).__name__})."],
                          notes=[str(e)[:200]])


# advanced predictive tools register themselves here (tools_pro imports helpers from this module, so it must load last)
from . import tools_pro as _pro  # noqa: E402
from . import tools_supply as _supply  # noqa: E402

REGISTRY.update(_pro.PRO_REGISTRY)
REGISTRY.update(_supply.SUPPLY_REGISTRY)
