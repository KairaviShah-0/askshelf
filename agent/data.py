"""Data layer: loads the simulator CSVs, applies the privacy allow-lists, and builds derived tables.

Privacy by design: restricted columns (customerId, inquiryText, employee IDs) are dropped or
aggregated HERE, so nothing downstream - tools, LLM prompts, charts - can ever see them.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from .config import DEPOSIT_SHARE

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Allow-list: only these order columns are ever loaded (customerId & inquiryText are excluded).
SAFE_ORDER_COLUMNS = [
    "id", "productType", "status", "currentStage", "complexity", "designHours", "millingHours",
    "joineryHours", "finishingHours", "materialType", "materialQty", "materialCost", "quotePrice",
    "creationDate", "completionDate", "acceptanceDate", "dueDate", "deliveryStatus", "lateDays",
    "penaltyPct",
]
ORDER_DATES = ["creationDate", "completionDate", "acceptanceDate", "dueDate"]


def qlabel(ts: pd.Series | pd.Timestamp):
    """Quarter label such as '2025-Q4'."""
    if isinstance(ts, pd.Series):
        return ts.dt.year.astype("Int64").astype(str) + "-Q" + ts.dt.quarter.astype("Int64").astype(str)
    return f"{ts.year}-Q{ts.quarter}"


@dataclass
class Dataset:
    orders: pd.DataFrame        # one row per quote/order, with derived fields
    ledger: pd.DataFrame        # daily financial ledger
    timeline: pd.DataFrame      # delivered orders joined with design-stage timing
    design: pd.DataFrame        # design assignments (no designer IDs)
    staff_daily: pd.DataFrame   # daily headcount / leave by role & skill (no employee IDs)
    people_events: pd.DataFrame # hires / resignations by role & skill (no employee IDs)
    inventory: pd.DataFrame
    wage_rates: pd.DataFrame    # skill-band hourly rates (aggregate only)
    start: pd.Timestamp
    as_of: pd.Timestamp


def _load_orders() -> pd.DataFrame:
    o = pd.read_csv(DATA_DIR / "orders_data.csv", usecols=SAFE_ORDER_COLUMNS, parse_dates=ORDER_DATES)
    o["won"] = o["status"] != "LOST"
    o["delivered"] = o["status"] == "DELIVERED"
    o["in_progress"] = o["won"] & ~o["delivered"]
    o["is_late"] = o["delivered"] & (o["deliveryStatus"] != "On Time")
    # Penalty is a cut of the FINAL payment only (verified against the ledger).
    o["penalty_amt"] = np.where(o["delivered"], o["quotePrice"] * (1 - DEPOSIT_SHARE) * o["penaltyPct"], 0.0)
    o["net_revenue"] = np.where(o["delivered"], o["quotePrice"] - o["penalty_amt"], np.nan)
    o["contribution"] = o["net_revenue"] - o["materialCost"]
    o["maker_hours"] = o["millingHours"] + o["joineryHours"] + o["finishingHours"]
    o["labour_hours"] = o["designHours"] + o["maker_hours"]
    o["lead_days"] = (o["completionDate"] - o["acceptanceDate"]).dt.days
    o["complexity_band"] = pd.cut(
        o["complexity"], bins=[0, 6, 8, 99], labels=["Low (4-6)", "Medium (7-8)", "High (9-11)"]
    ).astype(str)
    o["price_band"] = pd.cut(
        o["quotePrice"], bins=[0, 8000, 10000, 12000, 14000, 1e9],
        labels=["Under $8k", "$8-10k", "$10-12k", "$12-14k", "$14k+"],
    ).astype(str)
    o["created_q"] = qlabel(o["creationDate"])
    o["created_m"] = o["creationDate"].dt.strftime("%Y-%m")
    o["completed_q"] = qlabel(o["completionDate"])
    o["completed_m"] = o["completionDate"].dt.strftime("%Y-%m")
    return o


def _load_design_and_timeline(orders: pd.DataFrame):
    d = pd.read_csv(DATA_DIR / "design_log.csv", parse_dates=["startDate", "endDate"])
    # Rows with no end date are re-assignments/abandoned starts: keep them but flag them.
    d["reassigned"] = d["endDate"].isna()
    done = d[~d["reassigned"]].sort_values("endDate").groupby("orderId").last().reset_index()
    tl = orders[orders["delivered"]].merge(
        done[["orderId", "startDate", "endDate", "designerSkill", "durationDays"]],
        left_on="id", right_on="orderId", how="left",
    )
    tl["design_wait_days"] = (tl["startDate"] - tl["acceptanceDate"]).dt.days
    tl["design_days"] = tl["durationDays"]
    tl["production_days"] = (tl["completionDate"] - tl["endDate"]).dt.days
    design_public = d.drop(columns=["designerId"])  # designer IDs never leave this module
    return design_public, tl.drop(columns=["orderId"])


def _load_people(start: pd.Timestamp, end: pd.Timestamp):
    """Build daily headcount and leave by role/skill, then drop every employee identifier."""
    roster = pd.read_csv(DATA_DIR / "hr_roster.csv")
    ev = pd.read_csv(DATA_DIR / "hr_events_log.csv", parse_dates=["date"])
    info = roster.set_index("employeeId")[["role", "skillLevel"]]
    ev = ev.join(info, on="employeeId")
    hired = ev[ev["event"] == "Hired"].set_index("employeeId")["date"]
    resigned = ev[ev["event"] == "Resignation"].set_index("employeeId")["date"]

    days = pd.date_range(start, end, freq="D")
    cols = {}
    for eid, row in info.iterrows():
        s = hired.get(eid, start)
        e = resigned.get(eid, end + pd.Timedelta(days=1))
        active = (days >= s) & (days < e)          # resignation day = last day counted out
        key = f"{row['role']}_{row['skillLevel']}"
        cols[key] = cols.get(key, 0) + active.astype(int)
    hc = pd.DataFrame(cols, index=days).fillna(0).astype(int)

    # Leave: "Leave Start" + "On leave for N days" covers start .. start+N-1.
    leave = ev[ev["event"] == "Leave Start"].copy()
    leave["n"] = leave["details"].str.extract(r"(\d+)", expand=False).astype(int)
    lv = pd.DataFrame(0, index=days, columns=[f"{c}_leave" for c in hc.columns])
    for _, r in leave.iterrows():
        rng = pd.date_range(r["date"], periods=r["n"], freq="D")
        rng = rng[(rng >= start) & (rng <= end)]
        lv.loc[rng, f"{r['role']}_{r['skillLevel']}_leave"] += 1
    sd = hc.join(lv)
    for role in ("designer", "maker"):
        sd[role] = sd[[c for c in hc.columns if c.startswith(role)]].sum(axis=1)
        sd[f"{role}_leave"] = sd[[c for c in lv.columns if c.startswith(role)]].sum(axis=1)
    sd.index.name = "date"

    pe = ev[ev["event"].isin(["Hired", "Resignation"])][["date", "event", "role", "skillLevel"]].reset_index(drop=True)
    wages = roster.groupby(["role", "skillLevel"])["wage"].agg(["first", "count"]).reset_index()
    wages.columns = ["role", "skillLevel", "hourly_rate", "staff_ever_employed"]
    return sd, pe, wages


@lru_cache(maxsize=1)
def load() -> Dataset:
    orders = _load_orders()
    ledger = pd.read_csv(DATA_DIR / "financial_ledger.csv", parse_dates=["date"])
    ledger["month"] = ledger["date"].dt.strftime("%Y-%m")
    ledger["quarter"] = qlabel(ledger["date"])
    ledger["category"] = np.select(
        [
            ledger["type"].eq("REVENUE") & ledger["description"].str.startswith("Deposit"),
            ledger["type"].eq("REVENUE"),
            ledger["description"].eq("Daily Wages"),
            ledger["description"].eq("Daily Overheads"),
            ledger["description"].str.startswith("Reordered"),
        ],
        ["Deposits", "Final payments", "Wages", "Overheads", "Material purchases"],
        default="Other",
    )
    design, timeline = _load_design_and_timeline(orders)
    start, as_of = ledger["date"].min(), ledger["date"].max()
    staff_daily, people_events, wages = _load_people(start, as_of)
    inv = pd.read_csv(DATA_DIR / "inventory_log.csv", parse_dates=["date"])
    inv["month"] = inv["date"].dt.strftime("%Y-%m")
    return Dataset(orders, ledger, timeline, design, staff_daily, people_events, inv, wages, start, as_of)


def coverage_text() -> str:
    ds = load()
    o = ds.orders
    return (
        f"{ds.start:%d %b %Y} - {ds.as_of:%d %b %Y} | {len(o):,} quotes | "
        f"{int(o['won'].sum()):,} accepted | {int(o['delivered'].sum()):,} delivered"
    )
