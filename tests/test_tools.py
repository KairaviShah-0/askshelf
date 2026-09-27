"""Checks the analysis tools against independent calculations made straight from the raw CSVs."""
import re

import pandas as pd
import pytest

from agent import data as D
from agent import tools as T

RAW = D.DATA_DIR


@pytest.fixture(scope="module")
def raw():
    o = pd.read_csv(RAW / "orders_data.csv")
    l = pd.read_csv(RAW / "financial_ledger.csv")
    return o, l


def test_headline_counts_match_raw(raw):
    o, _ = raw
    ds = D.load()
    assert len(ds.orders) == len(o) == 1166
    assert int(ds.orders["won"].sum()) == (o["status"] != "LOST").sum() == 477
    assert int(ds.orders["delivered"].sum()) == (o["status"] == "DELIVERED").sum() == 460
    assert int(ds.orders["is_late"].sum()) == ((o["status"] == "DELIVERED") & (o["deliveryStatus"] != "On Time")).sum() == 63


def test_penalty_matches_ledger(raw):
    """Penalty derived from orders must equal 10% off the final payment as recorded in the ledger."""
    o, l = raw
    late_final = l[l["description"].str.contains("late penalty")]
    assert len(late_final) == 63
    ledger_penalty = (late_final["amount"] / 0.9 - late_final["amount"]).sum()   # final payment was already reduced by 10%
    assert D.load().orders["penalty_amt"].sum() == pytest.approx(ledger_penalty, rel=1e-6)


def test_ledger_totals(raw):
    _, l = raw
    r = T.run_tool("financial_trend", {"period": "all", "granularity": "quarter"})
    # the single trailing day (1 Jan 2027) is deliberately excluded from the quarterly chart, so allow that small gap
    tot_rev = l.loc[l["type"] == "REVENUE", "amount"].sum()
    assert abs(r.table["Revenue"].sum() - tot_rev) < 60_000
    k = T.run_tool("business_summary", {"period": "all"})
    assert f"${tot_rev:,.0f}" in " ".join(k.facts)


def test_product_profitability_covers_all_delivered():
    r = T.run_tool("product_profitability", {"period": "all"})
    assert r.table["Orders delivered"].sum() == 460
    assert set(r.table["Product"]) == set(T.PRODUCTS)


def test_late_analysis_totals():
    r = T.run_tool("late_delivery_analysis", {"group_by": "product"})
    assert r.table["Late"].sum() == 63
    assert r.table["Delivered"].sum() == 460


@pytest.mark.parametrize("spec,start,end", [
    ("last_quarter", "2026-10-01", "2026-12-31"),
    ("2025-Q3", "2025-07-01", "2025-09-30"),
    ("Q1 2026", "2026-01-01", "2026-03-31"),
    ("Sep 2025", "2025-09-01", "2025-09-30"),
    ("H1 2026", "2026-01-01", "2026-06-30"),
    ("2025", "2025-01-02", "2025-12-31"),   # clamped to the first day of data
    ("last_month", "2026-12-01", "2026-12-31"),
])
def test_period_parsing(spec, start, end):
    p = T.resolve_period(spec)
    assert p.start == pd.Timestamp(start) and p.end == pd.Timestamp(end)


def test_bad_period_falls_back_with_note():
    p = T.resolve_period("during the boom years")
    assert p.note and p.start == D.load().start


def test_product_and_material_resolution():
    assert T.resolve_product("desks") == "Office Desk"
    assert T.resolve_product("tv console") == "Media Console"
    assert T.resolve_material("walnut") == "American Walnut"


def test_bottleneck_identifies_production_stage():
    r = T.run_tool("production_bottleneck", {"period": "all"})
    days = dict(zip(r.table["Stage"], r.table["Avg days"]))
    prod = next(v for k, v in days.items() if k.startswith("Production"))
    assert prod > days["Design"] and prod > days["Waiting for a designer"]


def test_unknown_tool_and_bad_args_do_not_crash():
    assert "no analysis" in T.run_tool("drop_table", {}).facts[0].lower()
    r = T.run_tool("late_delivery_analysis", {"period": "2025", "group_by": "'; DROP TABLE"})
    assert r.facts and r.table is not None          # invalid group_by falls back to a safe default


ALL_CALLS = [("business_summary", {}), ("product_profitability", {}), ("production_bottleneck", {}), ("late_delivery_analysis", {}),
             ("resignation_impact", {}), ("workforce_overview", {}), ("inventory_analysis", {}), ("sales_pricing_analysis", {}),
             ("demand_forecast", {}), ("financial_trend", {}), ("data_dictionary", {})]


def test_every_tool_runs_and_never_leaks_identifiers():
    """Leakage test: no employee IDs (D1, M12...), no customer fields anywhere in any tool output."""
    ds = D.load()
    for col in ("customerId", "inquiryText"):
        assert col not in ds.orders.columns
    assert "designerId" not in ds.design.columns
    pat = re.compile(r"\b[DM]\d{1,2}\b|customer\s*(id|#)?\s*#?\d+|customerId", re.I)
    for name, args in ALL_CALLS:
        r = T.run_tool(name, args)
        assert "error" not in r.title.lower(), (name, r.facts)
        blob = r.llm_payload(max_rows=10_000)
        assert not pat.search(blob), f"{name} leaked: {pat.search(blob).group(0)}"
        if r.table is not None:
            assert not any("customer" in c.lower() or "employee" in c.lower() for c in r.table.columns)
