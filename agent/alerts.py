"""Proactive alert engine: scans the data for things a manager should hear about *without* having to ask.

Every alert has a severity, a plain-English headline, the evidence behind it, a recommended action and the question to ask
the agent for more. Thresholds are listed in RULES so they can be shown to (and challenged by) the user.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import data as D
from . import predict as P
from . import tools as T

RULES = [
    ("Delivery risk", "An open order has a predicted late-delivery risk of at least twice the historical late rate (13.7%)."),
    ("Demand dip", "Forecast quotes for next month are below 85% of the trailing 6-month average."),
    ("Unusual month", "A metric in the latest 3 months is 1.4+ typical deviations worse than its median (median / MAD method)."),
    ("Low stock", "A timber's stock level is at or below 3 units at the end of the data."),
    ("Win-rate slide", "Latest-month win rate is 8+ percentage points below the 6-month average."),
    ("Stock shortfall", "Open orders still need more of a material than is on hand."),
    ("Overdue orders", "An open order is already past its due date."),
    ("Penalty exposure", "Expected late-penalty cost on open orders exceeds $1,500."),
]


@dataclass
class Alert:
    severity: str                      # critical | warning | info
    category: str
    headline: str
    evidence: str
    action: str
    ask: str
    metrics: dict = field(default_factory=dict)


ORDER = {"critical": 0, "warning": 1, "info": 2}


def scan() -> list[Alert]:
    ds = D.load()
    out: list[Alert] = []

    # 1. delivery risk on open orders
    lm = P.late_model()
    op = ds.orders[ds.orders["in_progress"]]
    if len(op):
        risk = lm.predict(op)
        high = risk >= 2 * lm.base_rate
        exp_pen = float((risk * op["quotePrice"] * 0.05).sum())
        if high.sum():
            ids = ", ".join(f"#{i}" for i in op.loc[high].assign(r=risk[high]).sort_values("r", ascending=False)["id"].head(3))
            out.append(Alert("critical" if high.sum() >= 5 else "warning", "Delivery risk",
                             f"{int(high.sum())} of {len(op)} open orders are at high risk of finishing late",
                             f"Highest risk: {ids}. About {risk.sum():.1f} orders are expected to be late (model AUC {lm.auc:.2f}).",
                             "Pull the highest-risk orders forward in the schedule and warn those customers before the due date.",
                             "Which orders in progress are most likely to be late?", {"high_risk": int(high.sum()), "open": len(op)}))
        if exp_pen > 1500:
            out.append(Alert("warning", "Penalty exposure", f"Expected late-penalty cost on open orders is about {T.money(exp_pen)}",
                             "Late orders lose 10% of the final payment; exposure is risk x quote x 5% (half the quote is the final payment, 10% of that is the penalty).",
                             "Prioritise the orders with the highest quote value and risk first.", "What do late deliveries cost us?",
                             {"exposure": exp_pen}))

    # 2. demand dip
    m = P.monthly_series()
    f = P.forecast("quotes", 3)
    nxt = float(f.future["forecast"].iloc[0])
    base = float(m["quotes"].tail(6).mean())
    if nxt < 0.85 * base:
        out.append(Alert("warning", "Demand dip", f"Quote volume is forecast to fall to about {nxt:.0f} next month",
                         f"That is {nxt / base - 1:+.0%} versus the 6-month average of {base:.0f} quotes (80% range {f.future['lo'].iloc[0]:.0f}-{f.future['hi'].iloc[0]:.0f}).",
                         "Start outbound follow-up on open quotes now; wages are fixed, so a quiet month turns into a cash dip within 1-2 months.",
                         "Forecast our quotes for the next 3 months.", {"forecast": nxt, "baseline": base}))

    # 3. unusual months (latest 3)
    mm = m[["quotes", "win_rate", "revenue", "profit", "on_time"]]
    med = mm.median()
    mad = (mm - med).abs().median()
    scale = (1.4826 * mad).where(mad > 0, mm.std())
    z = ((mm - med) / scale).clip(-4, 4)
    names = {"quotes": "Quotes", "win_rate": "Win rate", "revenue": "Revenue", "profit": "Profit", "on_time": "On-time rate"}
    for c in z.columns:
        for p_, v in z[c].tail(3).items():
            if v <= -1.4:
                out.append(Alert("warning" if v <= -2 else "info", "Unusual month", f"{names[c]} was unusually weak in {p_.strftime('%B %Y')}",
                                 f"{abs(v):.1f} typical deviations below the median.", "Check with the team whether this was seasonal, a staffing gap or a pricing change.",
                                 "Is there anything unusual or worrying in our numbers?", {"z": float(v)}))

    # 4. low stock
    inv = ds.inventory.sort_values("date").groupby("material").tail(1)
    for _, r in inv.iterrows():
        if r["newLevel"] <= 3:
            out.append(Alert("warning", "Low stock", f"{r['material']} is down to {int(r['newLevel'])} units", "Stock level at the end of the data.",
                             "Raise a reorder now; a stock-out stops production for every order using this timber.", "Which timber runs low most often?", {"level": int(r["newLevel"])}))

    # 4b. stock shortfall vs open-order demand, and overdue orders
    from . import tools_supply as TS
    sc = TS.stock_cover().table
    for _, r in sc[sc["Status"] == "Short"].iterrows():
        out.append(Alert("warning", "Stock shortfall", f"{r['Material']}: open orders need {r['Open-order demand']} units but only {r['On hand']} are on hand",
                         f"Short by {r['Short by']} units; about {r['Days of cover']} days of cover at the current burn rate.",
                         "Raise a purchase order now so stock lands before those jobs reach milling.", "Do we have enough timber for the open orders?", {"short": int(r["Short by"])}))
    od = ds.orders[ds.orders["in_progress"] & (ds.orders["dueDate"] < ds.as_of)]
    if len(od):
        out.append(Alert("critical", "Overdue orders", f"{len(od)} open order(s) are already past their due date",
                         f"Orders {', '.join('#' + str(i) for i in od['id'].head(3))}. Each late order forfeits 10% of its final payment.",
                         "Call the customer today and agree a new date; finish these first.", "What is due for delivery this week?", {"overdue": len(od)}))

    # 5. win-rate slide
    wr_now, wr_base = float(m["win_rate"].iloc[-1]), float(m["win_rate"].tail(7).iloc[:-1].mean())
    if wr_now < wr_base - 0.08:
        out.append(Alert("info", "Win-rate slide", f"Win rate dropped to {wr_now:.0%} (6-month average {wr_base:.0%})", "Latest complete month vs the six months before it.",
                         "Review recent lost quotes with the sales team; check lead times quoted.", "Why are we losing quotes? Does price matter?", {}))

    if not out:
        out.append(Alert("info", "All clear", "Nothing needs attention right now", "No rule in the alert engine fired.", "Keep going.", "Give me a full executive briefing.", {}))
    return sorted(out, key=lambda a: ORDER[a.severity])
