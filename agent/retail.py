"""Retail / wholesale analytics that run on whatever the user uploaded.

Nothing here knows about any particular company. A column-mapping step (auto-detected, user-editable) turns an uploaded table into a canonical frame:

    date, amount, qty, product, category, supplier, region, cost, due, delivered, penalty, stock, reorder, order_id

Each analysis declares which fields it needs (`CAPABILITIES`). If a field is missing the page says so instead of guessing, and models are trained on the
user's own history (and refuse to run when there is too little of it).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from . import datalab as D
from .labai import df_to_json, fmt_num
from .ml import Logit, cv_auc

GOOD, WARN, BAD, BLUE = "#1baf7a", "#eda100", "#d6453a", "#2a78d6"
PAL = D.PALETTE


# ============================================================================================ column mapping
@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str                       # date | number | text
    help: str
    patterns: tuple[str, ...]
    needed_for: str


FIELDS: list[Field] = [
    Field("date", "Order date", "date", "When the sale or order happened.", (r"^(order|sale|sales|invoice|transaction|purchase)_?(date|dt|day)$", r"^date$", r"(order|sale|invoice|transaction|purchase|created).*(date|dt)", r"date"), "Everything"),
    Field("amount", "Sales amount", "number", "Revenue of the line (price x quantity).", (r"^(sales|revenue|net_sales|total_sales|line_total|line_amount|total_price|sales_amount|order_total|order_value|gross_sales)$", r"(sales|revenue).*(amount|value|total)", r"^(amount|total|value)$", r"(sales|revenue)"), "Dashboard, Forecasts, Planner"),
    Field("qty", "Quantity", "number", "Units sold or ordered.", (r"^(qty|quantity|units|units_sold|pieces|volume|qty_sold)$", r"(quantity|qty|units)"), "Stock cover, Forecasts"),
    Field("product", "Product / item", "text", "Product name or SKU.", (r"^(product|product_name|item|item_name|sku|description|product_description)$", r"(product|item|sku)(?!.*(id|code)$)", r"(product|item|sku)"), "Stock, product rankings"),
    Field("category", "Category", "text", "Product category or department.", (r"^(category|product_category|department|product_group|family|class|segment)$", r"(category|department|group)"), "Breakdowns"),
    Field("supplier", "Supplier / vendor", "text", "Who supplies or fulfils the item.", (r"^(supplier|vendor|supplier_name|vendor_name|manufacturer|distributor)$", r"(supplier|vendor|manufactur|distributor)"), "Deliveries, supplier rankings"),
    Field("region", "Store / region", "text", "Store, branch, region or channel.", (r"^(store|store_name|region|branch|location|outlet|channel|warehouse|state|city|market)$", r"(store|region|branch|location|outlet|channel|warehouse)"), "Breakdowns"),
    Field("order_id", "Order ID", "text", "Order or invoice number (used to count orders).", (r"^(order_id|order_no|order_number|invoice_no|invoice_number|invoice_id|po_number|po_no|purchase_order|transaction_id|id)$", r"(order|invoice|transaction|po).*(id|no|num)"), "Order counts"),
    Field("unit_price", "Unit price", "number", "Price per unit (only used when Sales amount is missing).", (r"^(unit_price|price|selling_price|sale_price|retail_price)$", r"price"), "Revenue (fallback)"),
    Field("unit_cost", "Unit cost", "number", "Cost per unit, for margins.", (r"^(unit_cost|cost_price|cost_per_unit|cogs_unit|purchase_price|buy_price)$", r"unit.*cost|cost.*unit"), "Margin, Planner"),
    Field("cost", "Total cost", "number", "Total cost of the line (if there is no unit cost).", (r"^(cost|total_cost|cogs|line_cost|cost_of_goods)$", r"cogs|total.*cost"), "Margin, Planner"),
    Field("due", "Due / promised date", "date", "When the order was promised or expected.", (r"^(due_date|promised_date|expected_date|expected_delivery|promise_date|required_date|scheduled_date|eta|due)$", r"(due|promis|expect|scheduled|required|eta)"), "Deliveries"),
    Field("delivered", "Delivered / received date", "date", "When it actually arrived. Leave blank for open orders.", (r"^(delivered_date|delivery_date|received_date|date_received|shipped_date|fulfilled_date|arrival_date|delivered|actual_delivery)$", r"(deliver|receiv|shipped|fulfil|arriv)"), "Deliveries"),
    Field("penalty", "Late penalty", "number", "Penalty or fee charged for a late delivery.", (r"^(late_penalty|penalty|penalty_amount|late_fee|fine|chargeback)$", r"(penalt|late_fee|fine|chargeback)"), "Penalty exposure"),
    Field("stock", "Stock on hand", "number", "Units in stock (latest value per product is used).", (r"^(stock_on_hand|on_hand|stock|inventory|qty_on_hand|stock_level|inventory_level|units_in_stock)$", r"(on_hand|stock|inventory)"), "Stock"),
    Field("reorder", "Reorder level", "number", "Stock level that triggers a reorder.", (r"^(reorder_level|reorder_point|min_stock|safety_stock|par_level|minimum_stock)$", r"(reorder|min_stock|safety|par_level)"), "Stock"),
]
FIELD_BY_KEY = {f.key: f for f in FIELDS}


def _kind_ok(f: Field, s: pd.Series) -> bool:
    k = D.kind_of(s) if len(s) > 1 else "text"
    if f.kind == "date":
        return k == "datetime"
    if f.kind == "number":
        return k == "numeric"
    return k in ("categorical", "text", "boolean") or (pd.api.types.is_numeric_dtype(s) and f.key in ("product", "order_id"))


def detect_mapping(df: pd.DataFrame) -> dict[str, str | None]:
    """Guess which column plays which role. Each column is used at most once (cost fields excepted)."""
    used: set[str] = set()
    out: dict[str, str | None] = {}
    for f in FIELDS:
        pick = None
        for pat in f.patterns:
            for c in df.columns:
                if c in used or not re.search(pat, c):
                    continue
                if not _kind_ok(f, df[c]):
                    continue
                if f.key == "date" and re.search(r"(due|deliver|receiv|promis|expect|ship)", c):
                    continue
                pick = c
                break
            if pick:
                break
        out[f.key] = pick
        if pick:
            used.add(pick)
    if out.get("order_id") and df[out["order_id"]].nunique() < 0.5 * len(df):      # an 'id' that repeats a lot is a customer/product id, not an order id
        out["order_id"] = None
    return out


def get_mapping(lab: D.Lab) -> dict:
    """Current mapping for the main table (auto-detected until the user edits it)."""
    if not lab.loaded:
        return {"table": None, "map": {}, "auto": True}
    if lab.mapping is None or lab.map_table not in lab.tables:
        lab.map_table = max(lab.tables, key=lambda k: len(lab.tables[k]))
        lab.mapping = detect_mapping(lab.tables[lab.map_table])
        lab.mapping_auto = True
        lab.canon_cache = None
    return {"table": lab.map_table, "map": lab.mapping, "auto": lab.mapping_auto}


def set_mapping(lab: D.Lab, table: str, mapping: dict) -> None:
    if table not in lab.tables:
        raise ValueError("Unknown table.")
    cols = set(lab.tables[table].columns)
    clean = {}
    for f in FIELDS:
        v = mapping.get(f.key)
        if v in (None, "", "none"):
            clean[f.key] = None
        elif v in cols:
            if not _kind_ok(f, lab.tables[table][v]) and f.kind in ("date",):
                raise ValueError(f"'{v}' does not look like a date column, so it cannot be used for {f.label}.")
            if f.kind == "number" and not pd.api.types.is_numeric_dtype(lab.tables[table][v]):
                raise ValueError(f"'{v}' is not numeric, so it cannot be used for {f.label}.")
            clean[f.key] = v
        else:
            raise ValueError(f"Column '{v}' was not found in {table}.")
    lab.map_table, lab.mapping, lab.mapping_auto, lab.canon_cache = table, clean, False, None


# ============================================================================================ canonical frame
def canon(lab: D.Lab) -> pd.DataFrame | None:
    """Canonical frame (cached). Returns None when there is no usable date or revenue/quantity column."""
    m = get_mapping(lab)
    if not m["table"]:
        return None
    if lab.canon_cache is not None:
        return lab.canon_cache
    src, mp = lab.tables[m["table"]], m["map"]
    out = pd.DataFrame(index=src.index)
    num = lambda c: pd.to_numeric(src[c], errors="coerce")
    for key in ("date", "due", "delivered"):
        out[key] = pd.to_datetime(src[mp[key]], errors="coerce") if mp.get(key) else pd.NaT
    out["qty"] = num(mp["qty"]) if mp.get("qty") else np.nan
    if mp.get("amount"):
        out["amount"] = num(mp["amount"])
    elif mp.get("unit_price") and mp.get("qty"):
        out["amount"] = num(mp["unit_price"]) * out["qty"]
    else:
        out["amount"] = np.nan
    if mp.get("cost"):
        out["cost"] = num(mp["cost"])
    elif mp.get("unit_cost") and mp.get("qty"):
        out["cost"] = num(mp["unit_cost"]) * out["qty"]
    else:
        out["cost"] = np.nan
    out["penalty"] = num(mp["penalty"]).fillna(0) if mp.get("penalty") else np.nan
    out["stock"] = num(mp["stock"]) if mp.get("stock") else np.nan
    out["reorder"] = num(mp["reorder"]) if mp.get("reorder") else np.nan
    for key in ("product", "category", "supplier", "region", "order_id"):
        out[key] = src[mp[key]].astype("string").str.strip() if mp.get(key) else pd.NA
    out["orders"] = 1.0
    out = out[out["date"].notna()].copy()
    if not len(out):
        lab.canon_cache = None
        return None
    out = out.sort_values("date").reset_index(drop=True)
    lab.canon_cache = out
    return out


def has(c: pd.DataFrame, *cols: str) -> bool:
    return all(col in c.columns and c[col].notna().any() for col in cols)


def capabilities(lab: D.Lab) -> dict:
    c = canon(lab)
    if c is None:
        return {"ready": False, "dashboard": False, "deliveries": False, "stock": False, "forecasts": False, "planner": False, "alerts": False, "penalties": False, "margin": False, "penalty": False}
    rev = has(c, "amount")
    return {"ready": True, "dashboard": rev or has(c, "qty"), "deliveries": has(c, "due", "delivered") or has(c, "due"), "penalties": has(c, "due", "delivered") and rev,
            "stock": has(c, "product", "stock"), "forecasts": rev or has(c, "qty"), "planner": rev, "alerts": True, "margin": rev and has(c, "cost"), "penalty": has(c, "penalty"),
            "dimensions": [k for k in ("category", "product", "supplier", "region") if has(c, k)]}


MISSING = {"dashboard": "Map a Sales amount (or Quantity) column and an Order date column.", "deliveries": "Map a Due date column (and a Delivered date column to measure lateness).",
           "stock": "Map a Product column and a Stock on hand column.", "forecasts": "Map an Order date column and a Sales amount or Quantity column.",
           "planner": "Map a Sales amount column.", "penalties": "Map a Due date, a Delivered date and a Sales amount column.", "alerts": "Upload data first."}


# ============================================================================================ shared chart helpers
def style(fig: go.Figure, title: str = "", h: int = 340, legend: bool = False) -> go.Figure:
    fig.update_layout(title=dict(text=title, x=0, font=dict(size=15)), height=h, margin=dict(l=8, r=16, t=56 if title else 16, b=8), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(family="Inter, system-ui, sans-serif", size=12, color="#52514e"), showlegend=legend, bargap=0.3, legend=dict(orientation="h", y=1.1, x=0),
                      xaxis=dict(showgrid=False, automargin=True), yaxis=dict(gridcolor="#e1e0d9", automargin=True))
    return fig


def fj(fig: go.Figure | None):
    import json
    return json.loads(fig.to_json()) if fig is not None else None


def pct(x, d=1) -> str:
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.{d}f}%"


def delta_txt(cur, prev, kind="pct") -> str | None:
    if prev is None or cur is None or (isinstance(prev, float) and np.isnan(prev)) or (isinstance(cur, float) and np.isnan(cur)):
        return None
    if kind == "pp":
        return f"{(cur - prev) * 100:+.1f} pp"
    if prev == 0:
        return None
    return f"{(cur / prev - 1) * 100:+.1f}%"


def monthly(c: pd.DataFrame, col: str, how: str = "sum") -> pd.Series:
    s = c.set_index("date")[col].astype(float)
    r = s.resample("MS")
    return (r.sum(min_count=1) if how == "sum" else r.mean()).dropna()


def complete_months(c: pd.DataFrame, s: pd.Series) -> pd.Series:
    """Drop the last month when the data stops mid-month (it would look like a collapse)."""
    if not len(s):
        return s
    last = c["date"].max()
    return s.iloc[:-1] if last.day < last.days_in_month and len(s) > 1 else s


def as_of(c: pd.DataFrame) -> pd.Timestamp:
    return c["date"].max().normalize()


PERIODS = {"30d": (30, "Last 30 days"), "90d": (90, "Last 90 days"), "12m": (365, "Last 12 months"), "all": (None, "All time")}


def window(c: pd.DataFrame, key: str):
    n, label = PERIODS.get(key, PERIODS["90d"])
    end = as_of(c)
    if n is None or (c["date"].min() > end - pd.Timedelta(days=n)):
        return c, None, "All time" if n is None else f"{label} (all available data)"
    cur = c[c["date"] > end - pd.Timedelta(days=n)]
    prev = c[(c["date"] <= end - pd.Timedelta(days=n)) & (c["date"] > end - pd.Timedelta(days=2 * n))]
    return cur, (prev if len(prev) >= 5 else None), label


def n_orders(d: pd.DataFrame) -> int:
    if has(d, "order_id") and d["order_id"].nunique() > 0.5 * len(d):
        return int(d["order_id"].nunique())
    return int(len(d))


def kpi_block(d: pd.DataFrame) -> dict:
    rev, units, orders = float(d["amount"].sum()) if has(d, "amount") else None, float(d["qty"].sum()) if has(d, "qty") else None, n_orders(d)
    cost = float(d["cost"].sum()) if has(d, "cost") and has(d, "amount") else None
    margin = (rev - cost) / rev if (cost is not None and rev) else None
    dl = d[d["due"].notna() & d["delivered"].notna()] if has(d, "due", "delivered") else d.iloc[0:0]
    ontime = float((dl["delivered"] <= dl["due"]).mean()) if len(dl) else None
    return {"revenue": rev, "units": units, "orders": orders, "aov": (rev / orders) if (rev is not None and orders) else None, "margin": margin, "ontime": ontime,
            "penalty": float(d["penalty"].sum()) if has(d, "penalty") else None}


# ============================================================================================ dashboard
def dashboard(lab: D.Lab, period: str = "90d") -> dict:
    c = canon(lab)
    cur, prev, label = window(c, period)
    k, kp = kpi_block(cur), (kpi_block(prev) if prev is not None else None)
    g = lambda key: (kp or {}).get(key)
    kpis = []
    if k["revenue"] is not None:
        kpis.append({"label": "Revenue", "value": fmt_num(k["revenue"]), "delta": delta_txt(k["revenue"], g("revenue"))})
    kpis.append({"label": "Orders", "value": f"{k['orders']:,}", "delta": delta_txt(k["orders"], g("orders"))})
    if k["units"] is not None:
        kpis.append({"label": "Units sold", "value": fmt_num(k["units"]), "delta": delta_txt(k["units"], g("units"))})
    if k["aov"] is not None:
        kpis.append({"label": "Avg order value", "value": fmt_num(k["aov"]), "delta": delta_txt(k["aov"], g("aov"))})
    if k["margin"] is not None:
        kpis.append({"label": "Gross margin", "value": pct(k["margin"]), "delta": delta_txt(k["margin"], g("margin"), "pp")})
    if k["ontime"] is not None:
        kpis.append({"label": "On-time delivery", "value": pct(k["ontime"], 0), "delta": delta_txt(k["ontime"], g("ontime"), "pp")})
    if k["penalty"]:
        kpis.append({"label": "Late penalties", "value": fmt_num(k["penalty"]), "delta": delta_txt(k["penalty"], g("penalty")), "good": "down"})
    col = "amount" if has(c, "amount") else "qty"
    ylab = "Revenue" if col == "amount" else "Units"

    charts, insights, actions = [], [], []
    ms = monthly(c, col)
    fig = go.Figure(go.Scatter(x=ms.index, y=ms.values, mode="lines+markers", line=dict(color=BLUE, width=2.6), marker=dict(size=6), fill="tozeroy", fillcolor="rgba(42,120,214,.10)",
                               hovertemplate="%{x|%b %Y}: %{y:,.0f}<extra></extra>"))
    charts.append({"id": "trend", "title": f"Monthly {ylab.lower()}", "fig": fj(style(fig, f"Monthly {ylab.lower()}", 330))})
    dim_order = [d for d in ("product", "category", "supplier", "region") if has(c, d)]
    shown = []
    for dname, title in (("product", "Top products"), ("category", "Category mix"), ("region", "By store / region"), ("supplier", "By supplier")):
        if dname not in dim_order or len(shown) >= 3:
            continue
        agg = cur.groupby(dname)[col].sum().sort_values(ascending=False)
        if agg.empty:
            continue
        if dname in ("category", "region") and len(agg) <= 8 and (dname == "category" or "category" not in dim_order or True):
            fig = go.Figure(go.Pie(labels=agg.index.astype(str), values=agg.values, hole=.58, marker=dict(colors=PAL), textinfo="percent", sort=False, hovertemplate="%{label}: %{value:,.0f}<extra></extra>"))
            fig.update_layout(legend=dict(orientation="v", x=1.0, y=.5))
            style(fig, f"{title} ({ylab.lower()})", 330, True)
            fig.update_layout(legend=dict(orientation="v", x=1.0, y=.5))
        else:
            a = agg.head(10).iloc[::-1]
            fig = go.Figure(go.Bar(y=a.index.astype(str), x=a.values, orientation="h", marker=dict(color=BLUE, cornerradius=4), hovertemplate="%{y}: %{x:,.0f}<extra></extra>"))
            style(fig, f"{title} ({ylab.lower()})", 330)
        charts.append({"id": dname, "title": title, "fig": fj(fig)})
        shown.append(dname)
    if has(c, "cost", "amount") and "category" in dim_order:
        gm = cur.groupby("category").apply(lambda d: (d["amount"].sum() - d["cost"].sum()) / d["amount"].sum() if d["amount"].sum() else np.nan, include_groups=False).dropna().sort_values()
        fig = go.Figure(go.Bar(y=gm.index.astype(str), x=gm.values * 100, orientation="h", marker=dict(color=[BAD if v < .2 else WARN if v < .35 else GOOD for v in gm.values], cornerradius=4), hovertemplate="%{y}: %{x:.1f}%<extra></extra>"))
        charts.append({"id": "margin", "title": "Gross margin by category", "fig": fj(style(fig, "Gross margin by category (%)", 330))})

    if k["revenue"] is not None and kp and g("revenue"):
        ch = (k["revenue"] / g("revenue") - 1) * 100
        insights.append(f"Revenue was {fmt_num(k['revenue'])} over the {label.lower()}, {ch:+.1f}% versus the previous period of the same length.")
    top = None
    for dname in ("product", "category", "supplier", "region"):
        if dname in dim_order:
            agg = cur.groupby(dname)[col].sum().sort_values(ascending=False)
            if len(agg) >= 2 and agg.sum() > 0:
                top = (dname, agg)
                insights.append(f"The top {dname} is {agg.index[0]} with {agg.iloc[0] / agg.sum() * 100:.0f}% of {ylab.lower()}; the top 3 account for {agg.head(3).sum() / agg.sum() * 100:.0f}%.")
                break
    cm = complete_months(c, ms)
    if len(cm) >= 4:
        insights.append(f"The strongest month on record was {cm.idxmax():%B %Y} ({fmt_num(cm.max())}) and the weakest {cm.idxmin():%B %Y} ({fmt_num(cm.min())}).")
    if k["margin"] is not None:
        insights.append(f"Gross margin was {pct(k['margin'])}" + (f" ({delta_txt(k['margin'], g('margin'), 'pp')} versus the previous period)." if kp else "."))
    if top and top[1].head(3).sum() / top[1].sum() > .6:
        actions.append(f"Sales are concentrated: protect stock and supply for {', '.join(map(str, top[1].head(3).index))}.")
    if kp and g("revenue") and k["revenue"] < 0.9 * g("revenue"):
        actions.append("Revenue is down more than 10%: use Ask to break the decline down by product, store and week.")
    if k["ontime"] is not None and k["ontime"] < .85:
        actions.append("On-time delivery is below 85%: open the Deliveries page to see which suppliers are driving lateness.")
    return {"label": label, "period": period, "kpis": kpis, "charts": charts, "insights": insights, "actions": actions, "as_of": f"{as_of(c):%d %b %Y}",
            "range": f"{c['date'].min():%d %b %Y} to {c['date'].max():%d %b %Y}", "compared": prev is not None, "dims": dim_order}


# ============================================================================================ deliveries + late-risk model
MIN_DELIVERED, MIN_MINORITY = 150, 20


def _features(d: pd.DataFrame, ref: pd.DataFrame, cats: dict[str, list[str]]) -> pd.DataFrame:
    X = pd.DataFrame(index=d.index)
    X["Days allowed"] = (d["due"] - d["date"]).dt.days.astype(float)
    if has(d, "qty"):
        X["Quantity"] = d["qty"].fillna(d["qty"].median()).astype(float)
    if has(d, "amount"):
        X["Order value"] = d["amount"].fillna(d["amount"].median()).astype(float)
    day_counts = ref.groupby(ref["date"].dt.normalize()).size()
    roll = day_counts.reindex(pd.date_range(day_counts.index.min(), day_counts.index.max() + pd.Timedelta(days=1), freq="D"), fill_value=0).rolling(7, min_periods=1).sum()
    X["Orders in the prior week"] = roll.reindex(d["date"].dt.normalize()).ffill().fillna(roll.median()).values
    ang = 2 * np.pi * d["date"].dt.month.values / 12
    X["Season (sin)"], X["Season (cos)"] = np.sin(ang), np.cos(ang)
    for col, vals in cats.items():
        for v in vals:
            X[f"{col.title()}: {v}"] = (d[col].astype("string") == v).astype(float).values
    return X.fillna(0)


def train_late_model(c: pd.DataFrame) -> dict:
    """Train on the user's own delivered orders. Returns {'ok': False, 'why': ...} if there is not enough history to be honest about."""
    if not has(c, "due", "delivered"):
        return {"ok": False, "why": "Map both a Due date and a Delivered date to measure lateness."}
    d = c[c["due"].notna() & c["delivered"].notna()].copy()
    d["late"] = (d["delivered"] > d["due"]).astype(int)
    n, pos = len(d), int(d["late"].sum())
    minority = min(pos, n - pos)
    if n < MIN_DELIVERED or minority < MIN_MINORITY:
        return {"ok": False, "why": f"The late-risk model needs at least {MIN_DELIVERED} delivered orders with at least {MIN_MINORITY} late and {MIN_MINORITY} on-time. This data has {n} delivered orders ({pos} late).", "n": n, "late": pos}
    cats = {}
    for col in ("supplier", "region", "category"):
        if has(d, col):
            vc = d[col].value_counts()
            cats[col] = [v for v in vc.index[:8] if vc[v] >= 10]
    d = d[(d["due"] - d["date"]).dt.days.between(0, 365)]
    if len(d) < MIN_DELIVERED:
        return {"ok": False, "why": "Too few orders have a sensible due date after the order date.", "n": len(d), "late": pos}
    X = _features(d, c, cats)
    y = d["late"].values
    auc = cv_auc(X, y, l2=5.0)
    model = Logit(5.0).fit(X, y)
    return {"ok": True, "model": model, "auc": float(auc), "n": len(d), "late": int(y.sum()), "base": float(y.mean()), "cats": cats, "drivers": model.drivers().head(8)}


def late_model(lab: D.Lab) -> dict:
    if "late_model" not in lab.cache:
        lab.cache["late_model"] = train_late_model(canon(lab))
    return lab.cache["late_model"]


def penalty_rate(c: pd.DataFrame) -> float | None:
    if not has(c, "penalty", "amount", "due", "delivered"):
        return None
    late = c[c["due"].notna() & c["delivered"].notna() & (c["delivered"] > c["due"])]
    tot = float(late["amount"].sum())
    return float(late["penalty"].sum() / tot) if tot > 0 and late["penalty"].sum() > 0 else None


def open_orders(lab: D.Lab) -> pd.DataFrame:
    c = canon(lab)
    o = c[c["due"].notna() & c["delivered"].isna()].copy()
    if not len(o):
        return o
    today = as_of(c)
    o["days_left"] = (o["due"] - today).dt.days
    lm = late_model(lab)
    o["risk"] = np.nan
    if lm.get("ok"):
        ok = (o["due"] - o["date"]).dt.days.between(0, 365)
        if ok.any():
            o.loc[ok, "risk"] = lm["model"].predict_proba(_features(o[ok], c, lm["cats"]))
    o["label"] = o["order_id"].astype("string").fillna("") if has(o, "order_id") else (o.index + 1).astype(str)
    return o


def deliveries(lab: D.Lab, expedite: int = 0) -> dict:
    c = canon(lab)
    out = {"kpis": [], "charts": [], "insights": [], "actions": [], "notes": [], "model": None, "table": None, "open_table": None}
    dl = c[c["due"].notna() & c["delivered"].notna()].copy()
    dl["late"] = dl["delivered"] > dl["due"]
    dl["delay"] = (dl["delivered"] - dl["due"]).dt.days.clip(lower=0)
    o = open_orders(lab)
    today = as_of(c)
    overdue = o[o["days_left"] < 0] if len(o) else o
    rate = penalty_rate(c)
    lm = late_model(lab)
    if len(dl):
        late_n = int(dl["late"].sum())
        out["kpis"] += [{"label": "Delivered orders", "value": f"{len(dl):,}"}, {"label": "On-time rate", "value": pct(1 - late_n / len(dl), 1)},
                        {"label": "Avg delay when late", "value": f"{dl.loc[dl['late'], 'delay'].mean():.1f} days" if late_n else "n/a"}]
    out["kpis"] += [{"label": "Open orders", "value": f"{len(o):,}"}, {"label": "Overdue", "value": f"{len(overdue):,}"}]
    if has(c, "penalty"):
        out["kpis"].append({"label": "Penalties paid", "value": fmt_num(float(c["penalty"].sum()))})
    if len(dl) >= 10:
        m = dl.set_index("date")["late"].resample("MS").agg(["mean", "count"])
        m = m[m["count"] >= 5]
        if len(m) >= 2:
            fig = go.Figure(go.Scatter(x=m.index, y=(1 - m["mean"]) * 100, mode="lines+markers", line=dict(color=GOOD, width=2.6), hovertemplate="%{x|%b %Y}: %{y:.0f}% on time<extra></extra>"))
            fig.update_yaxes(range=[max(0, ((1 - m["mean"]) * 100).min() - 10), 100], ticksuffix="%")
            out["charts"].append({"id": "ontime", "title": "On-time rate by month", "fig": fj(style(fig, "On-time rate by month", 320))})
        for dname, title in (("supplier", "Late rate by supplier"), ("region", "Late rate by store / region"), ("category", "Late rate by category")):
            if has(dl, dname):
                g = dl.groupby(dname).agg(late=("late", "mean"), n=("late", "size"), delay=("delay", "mean"))
                g = g[g["n"] >= 8].sort_values("late")
                if len(g) >= 2:
                    fig = go.Figure(go.Bar(y=g.index.astype(str), x=g["late"] * 100, orientation="h", marker=dict(color=[BAD if v > .25 else WARN if v > .15 else GOOD for v in g["late"]], cornerradius=4),
                                           customdata=np.c_[g["n"], g["delay"]], hovertemplate="%{y}: %{x:.0f}% late<br>%{customdata[0]} orders, avg delay %{customdata[1]:.1f} days<extra></extra>"))
                    fig.update_xaxes(ticksuffix="%")
                    out["charts"].append({"id": dname, "title": title, "fig": fj(style(fig, title, 320))})
                    if dname == "supplier":
                        w, b = g.iloc[-1], g.iloc[0]
                        out["insights"].append(f"{g.index[-1]} is late on {w['late'] * 100:.0f}% of orders (average {w['delay']:.1f} days over the due date), against {b['late'] * 100:.0f}% for {g.index[0]}.")
                        if w["late"] > .2:
                            out["actions"].append(f"Review the service terms with {g.index[-1]} or move volume to {g.index[0]}.")
                    break
        late_only = dl[dl["late"]]["delay"]
        if len(late_only) >= 10:
            fig = go.Figure(go.Histogram(x=late_only, marker=dict(color=BAD), nbinsx=min(20, int(late_only.max()) + 1)))
            fig.update_xaxes(title="Days late")
            out["charts"].append({"id": "delay", "title": "How late are late orders?", "fig": fj(style(fig, "How late are late orders?", 320))})
    if len(overdue):
        out["insights"].append(f"{len(overdue)} open order(s) are already past their due date ({today:%d %b %Y} is the latest date in the data).")
        out["actions"].append("Contact the customers on overdue orders today and agree a new date.")
    # ---- model
    if lm.get("ok"):
        out["model"] = {"auc": round(lm["auc"], 2), "n": lm["n"], "base": round(lm["base"] * 100, 1), "verdict": "Strong" if lm["auc"] >= .75 else "Moderate" if lm["auc"] >= .62 else "Weak",
                        "drivers": [{"feature": r.feature, "effect": round(float(r.effect), 2)} for r in lm["drivers"].itertuples()]}
        out["kpis"].append({"label": "Model accuracy (AUC)", "value": f"{lm['auc']:.2f}"})
        if lm["auc"] < .62:
            out["notes"].append(f"The late-risk model is weak (AUC {lm['auc']:.2f}, where 0.50 is a coin flip): lateness in this data is hard to predict from the available columns, so risk scores below are indicative only.")
        if len(o):
            o = o.dropna(subset=["risk"]).copy()
            base = lm["base"]
            o["exposure"] = o["risk"] * o["amount"] * rate if (rate and has(o, "amount")) else np.nan
            ranking = "exposure" if o["exposure"].notna().any() else "risk"
            o = o.sort_values(ranking, ascending=False)
            top = set(o.head(int(max(0, min(expedite, len(o))))).index)
            after = o["risk"].where(~o.index.isin(top), np.minimum(o["risk"], base))
            exp_after = after * o["amount"] * rate if (rate and has(o, "amount")) else None
            hi = int((o["risk"] >= max(2 * base, .4)).sum())
            out["kpis"] += [{"label": "High-risk open orders", "value": f"{hi:,}"}, {"label": "Expected to finish late", "value": f"{o['risk'].sum():.1f}"}]
            if rate and o["exposure"].notna().any():
                tot_now, tot_after = float(o["exposure"].sum()), float(exp_after.sum())
                out["kpis"].append({"label": "Penalty exposure", "value": fmt_num(tot_now)})
                if expedite:
                    out["kpis"].append({"label": "After expediting", "value": fmt_num(tot_after), "delta": f"-{fmt_num(tot_now - tot_after)} saved", "good": "down"})
                out["notes"].append(f"Exposure = predicted late risk x order value x the average penalty rate observed in your history ({rate * 100:.1f}% of late order value). An expedited order is assumed to perform like a typical order ({base * 100:.0f}% late). These are planning estimates, not guarantees.")
            lab_ = o.sort_values("risk", ascending=False).head(12)
            fig = go.Figure(go.Bar(y=[f"{r.label} {r.product if has(o, 'product') and pd.notna(r.product) else ''}".strip() for r in lab_.itertuples()][::-1], x=(lab_["risk"] * 100)[::-1], orientation="h",
                                   marker=dict(color=[BAD if v >= max(2 * base, .4) else WARN for v in lab_["risk"][::-1]], cornerradius=4), hovertemplate="%{y}: %{x:.0f}% late risk<extra></extra>"))
            fig.add_vline(x=base * 100, line=dict(color="#898781", dash="dash"), annotation_text=f"Typical {base * 100:.0f}%", annotation_position="bottom right")
            fig.update_xaxes(ticksuffix="%", range=[0, 100])
            out["charts"].insert(0, {"id": "risk", "title": "Open orders ranked by late risk", "fig": fj(style(fig, "Open orders ranked by late risk", 380))})
            tab = pd.DataFrame({"Order": o["label"], "Product": o["product"] if has(o, "product") else "", "Due": o["due"].dt.strftime("%d %b %Y"), "Days left": o["days_left"].astype(int),
                                "Order value": o["amount"].round(0) if has(o, "amount") else np.nan, "Late risk": (o["risk"] * 100).round(0).astype(int).astype(str) + "%"})
            if rate and has(o, "amount"):
                tab["Exposure"] = o["exposure"].round(0)
                tab["Plan"] = ["Expedite" if i in top else "" for i in o.index]
            out["open_table"] = df_to_json(tab.dropna(axis=1, how="all").head(40))
            if hi:
                out["insights"].append(f"{hi} of {len(o)} open orders are at high risk of finishing late (risk at least double the typical {base * 100:.0f}% rate).")
                out["actions"].append("Pull the highest-risk orders forward and warn those customers before the due date.")
        drivers = [d for d in out["model"]["drivers"][:3]]
        if drivers:
            out["insights"].append("What the model says drives lateness most: " + "; ".join(f"{d['feature']} ({'raises' if d['effect'] > 0 else 'lowers'} risk)" for d in drivers) + ".")
    else:
        out["notes"].append(lm.get("why", "Late-risk model unavailable."))
        if len(o):
            tab = pd.DataFrame({"Order": o["label"], "Product": o["product"] if has(o, "product") else "", "Due": o["due"].dt.strftime("%d %b %Y"), "Days left": o["days_left"].astype(int),
                                "Order value": o["amount"].round(0) if has(o, "amount") else np.nan}).sort_values("Days left")
            out["open_table"] = df_to_json(tab.dropna(axis=1, how="all").head(40))
    out["model_ok"] = bool(lm.get("ok"))
    out["rate"] = rate
    out["n_open"] = int(len(o))
    return out


# ============================================================================================ stock availability
def stock_position(lab: D.Lab, target_days: int = 30) -> pd.DataFrame:
    c = canon(lab)
    c = c[c["product"].notna()]
    end = as_of(c)
    span = max(1, min(90, (end - c["date"].min()).days + 1))
    recent = c[c["date"] > end - pd.Timedelta(days=span)]
    rows = []
    last = c[c["stock"].notna()].sort_values("date").groupby("product").tail(1).set_index("product")
    for p, r in last.iterrows():
        sold = recent[recent["product"] == p]
        units = float(sold["qty"].sum()) if has(sold, "qty") else float(len(sold))
        daily = units / span
        stock = float(r["stock"])
        cover = stock / daily if daily > 0 else np.inf
        reorder = float(r["reorder"]) if pd.notna(r.get("reorder")) else np.nan
        price = float(sold["amount"].sum() / units) if (units and has(sold, "amount")) else np.nan
        status = "Out of stock" if stock <= 0 else "Below reorder" if (not np.isnan(reorder) and stock <= reorder) else "Low cover" if cover < 7 else "Overstock" if cover > 120 else "Healthy"
        rows.append({"product": p, "category": (c[c["product"] == p]["category"].dropna().iloc[-1] if has(c[c["product"] == p], "category") else ""),
                     "supplier": (c[c["product"] == p]["supplier"].dropna().iloc[-1] if has(c[c["product"] == p], "supplier") else ""),
                     "stock": stock, "reorder": reorder, "daily": daily, "cover": cover, "status": status, "price": price,
                     "suggested": max(0, int(np.ceil(daily * target_days - stock))) if daily > 0 else 0, "weekly_risk": (daily * 7 * price if status in ("Out of stock", "Low cover", "Below reorder") and not np.isnan(price) else 0.0)})
    return pd.DataFrame(rows)


def stock(lab: D.Lab, target_days: int = 30) -> dict:
    t = stock_position(lab, target_days)
    c = canon(lab)
    if t.empty:
        raise ValueError("No product has a stock value.")
    order = {"Out of stock": 0, "Below reorder": 1, "Low cover": 2, "Healthy": 3, "Overstock": 4}
    cnt = t["status"].value_counts()
    kpis = [{"label": "Products tracked", "value": f"{len(t):,}"}, {"label": "Out of stock", "value": f"{int(cnt.get('Out of stock', 0))}"},
            {"label": "Below reorder / low cover", "value": f"{int(cnt.get('Below reorder', 0) + cnt.get('Low cover', 0))}"}, {"label": "Overstock", "value": f"{int(cnt.get('Overstock', 0))}"}]
    risk = float(t["weekly_risk"].sum())
    if risk > 0:
        kpis.append({"label": "Weekly sales at risk", "value": fmt_num(risk)})
    sold = t[np.isfinite(t["cover"])].sort_values("cover").head(15).iloc[::-1]
    colors = {"Out of stock": BAD, "Below reorder": "#e0662f", "Low cover": WARN, "Healthy": GOOD, "Overstock": "#7a5af8"}
    charts = []
    if len(sold):
        fig = go.Figure(go.Bar(y=sold["product"].astype(str), x=sold["cover"], orientation="h", marker=dict(color=[colors[s] for s in sold["status"]], cornerradius=4),
                               text=[f"{v:.0f} d" for v in sold["cover"]], textposition="outside", cliponaxis=False, hovertemplate="%{y}: %{x:.1f} days of cover<extra></extra>"))
        fig.update_xaxes(title="Days of cover at the recent sales rate")
        charts.append({"id": "cover", "title": "Products closest to running out", "fig": fj(style(fig, "Products closest to running out", 420))})
    grp = "category" if has(t.replace("", np.nan), "category") else None
    fig = go.Figure()
    if grp:
        pv = t.pivot_table(index=grp, columns="status", values="product", aggfunc="count", fill_value=0)
        for s in order:
            if s in pv.columns:
                fig.add_trace(go.Bar(x=pv.index.astype(str), y=pv[s], name=s, marker=dict(color=colors[s])))
        fig.update_layout(barmode="stack")
        title = "Stock status by category"
    else:
        fig.add_trace(go.Bar(x=list(cnt.index), y=list(cnt.values), marker=dict(color=[colors[s] for s in cnt.index], cornerradius=4)))
        title = "Stock status"
    charts.append({"id": "status", "title": title, "fig": fj(style(fig, title, 420, bool(grp)))})
    out = t.assign(_o=t["status"].map(order)).sort_values(["_o", "cover"]).drop(columns="_o")
    tab = pd.DataFrame({"Product": out["product"], "Status": out["status"], "On hand": out["stock"].round(0), "Reorder level": out["reorder"].round(0),
                        "Avg sold / day": out["daily"].round(2), "Days of cover": out["cover"].replace(np.inf, np.nan).round(1), "Suggested order": out["suggested"]})
    for extra in ("category", "supplier"):
        if out[extra].astype(str).str.len().gt(0).any():
            tab.insert(1, extra.title(), out[extra].values)
    tab = tab.dropna(axis=1, how="all")
    short = out[out["status"].isin(["Out of stock", "Below reorder", "Low cover"])]
    insights, actions = [], []
    if len(short):
        insights.append(f"{len(short)} product(s) need attention now: {', '.join(short['product'].astype(str).head(4))}{'...' if len(short) > 4 else ''}.")
        actions.append(f"Raise purchase orders for {int(short['suggested'].gt(0).sum())} product(s); the table shows a suggested quantity to reach {target_days} days of cover.")
    if cnt.get("Overstock", 0):
        insights.append(f"{int(cnt['Overstock'])} product(s) hold more than 120 days of stock at the current sales rate, which ties up cash.")
        actions.append("Consider promotions or pausing reorders on overstocked products.")
    return {"kpis": kpis, "charts": charts, "table": df_to_json(tab, 500), "insights": insights, "actions": actions, "target": target_days,
            "notes": ["Days of cover = stock on hand / average units sold per day over the last 90 days. The latest stock value per product is used.", "Suggested order = sales rate x target days of cover - stock on hand."]}


# ============================================================================================ forecasts
METRICS = {"revenue": ("Revenue", "amount"), "units": ("Units sold", "qty"), "orders": ("Orders", "orders")}


def forecast(lab: D.Lab, metric: str = "revenue", group: str | None = None, periods: int = 6, unit: str | None = None) -> dict:
    c = canon(lab)
    metric = metric if metric in METRICS and has(c, METRICS[metric][1]) else next(k for k, (_, col) in METRICS.items() if has(c, col))
    label, col = METRICS[metric]
    group = group if group in ("category", "product", "supplier", "region") and has(c, group) else None
    freq = D.FREQ_WORDS.get(unit or "", None)
    res = D.forecast_series(c, "date", col, periods, freq)
    hist, fc = res["history"], res["forecast"]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=hist["ds"], y=hist["y"], name="Actual", mode="lines+markers", line=dict(color=PAL[0], width=2.4), marker=dict(size=5)))
    fig.add_trace(go.Scatter(x=pd.concat([fc["ds"], fc["ds"][::-1]]), y=pd.concat([fc["hi"], fc["lo"][::-1]]), fill="toself", fillcolor="rgba(237,161,0,.18)", line=dict(width=0), name="80% range", hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=fc["ds"], y=fc["yhat"], name="Forecast", mode="lines+markers", line=dict(color="#d97706", width=2.6, dash="dash")))
    style(fig, f"{label}: actual and forecast by {res['unit']}", 400, True)
    chg = res["change_pct"]
    tab = fc.rename(columns={"ds": res["unit"].capitalize(), "yhat": "Forecast", "lo": "Low (80%)", "hi": "High (80%)"})
    tab[res["unit"].capitalize()] = tab[res["unit"].capitalize()].dt.strftime("%d %b %Y" if res["freq"] in ("D", "W") else "%b %Y")
    kpis = [{"label": f"Forecast {label.lower()} ({res['periods']} {res['unit']}s)", "value": fmt_num(fc["yhat"].sum()), "delta": f"{chg:+.1f}% vs previous {res['periods']}" if chg is not None else None},
            {"label": "Back-test error (MAPE)", "value": f"{res['mape']}%" if res["mape"] is not None else "n/a"}, {"label": "History used", "value": f"{res['n_points']} {res['unit']}s"}]
    insights = [f"Expected {label.lower()} over the next {res['periods']} {res['unit']}s is {fmt_num(fc['yhat'].sum())}" + (f", {chg:+.1f}% versus the previous {res['periods']} {res['unit']}s." if chg is not None else "."),
                f"Peak forecast {res['unit']}: {fc.loc[fc['yhat'].idxmax(), 'ds']:%b %Y} at {fmt_num(fc['yhat'].max())}."]
    notes = [f"Method: {res['method']}.", "The shaded band is an 80% prediction range, not a guarantee. Treat forecasts as a planning range." + (f" On the most recent hold-out, the typical error was {res['mape']}%." if res["mape"] is not None else "")]
    if res["mape"] is not None and res["mape"] > 30:
        notes.append("The back-test error is large, so this series is hard to forecast: use the range, not the single line.")
    out = {"kpis": kpis, "chart": fj(fig), "table": df_to_json(tab), "insights": insights, "notes": notes, "metric": metric, "unit": res["unit"], "metrics": {k: v[0] for k, v in METRICS.items() if has(c, v[1])},
           "dims": [k for k in ("category", "product", "supplier", "region") if has(c, k)], "group": group, "group_chart": None, "group_table": None}
    if group:
        try:
            g = D.forecast_groups(c, "date", col, group, res["periods"], 5, res["freq"])
            figg = go.Figure()
            for i, (name, d) in enumerate(g.groupby("group")):
                figg.add_trace(go.Scatter(x=d["ds"], y=d["yhat"], name=str(name), mode="lines+markers", line=dict(color=PAL[i % len(PAL)], width=2.2)))
            out["group_chart"] = fj(style(figg, f"Forecast {label.lower()} by {group} (top 5)", 380, True))
            gt = g.rename(columns={"group": group.title(), "ds": res["unit"].capitalize(), "yhat": "Forecast", "lo": "Low (80%)", "hi": "High (80%)"})
            gt[res["unit"].capitalize()] = gt[res["unit"].capitalize()].dt.strftime("%b %Y")
            out["group_table"] = df_to_json(gt, 200)
        except ValueError as e:
            out["notes"].append(str(e))
    return out


# ============================================================================================ scenario planner
ELASTICITY = {"none": 0.0, "moderate": -1.0, "high": -2.0}


def planner(lab: D.Lab, price: float = 0, volume: float = 0, cost: float = 0, elasticity: str = "none") -> dict:
    c = canon(lab)
    end = as_of(c)
    base = c[c["date"] > end - pd.Timedelta(days=365)]
    months = max(1.0, min(12.0, (end - base["date"].min()).days / 30.44 + 1))
    R = float(base["amount"].sum()) / months
    U = float(base["qty"].sum()) / months if has(base, "qty") else None
    C = float(base["cost"].sum()) / months if has(base, "cost") else None
    pen = float(base["penalty"].sum()) / months if has(base, "penalty") else None
    e = ELASTICITY.get(elasticity, 0.0)

    def scen(p, v, cc):
        p, v, cc = p / 100, v / 100, cc / 100
        mult = max(0.0, (1 + v) * (1 + e * p))                 # volume after the market reacts to the price change
        rev = R * (1 + p) * mult
        cst = C * mult * (1 + cc) if C is not None else None
        return {"revenue": rev, "units": U * mult if U is not None else None, "cost": cst, "profit": (rev - cst) if cst is not None else None, "penalty": pen * mult if pen is not None else None}
    b, s = scen(0, 0, 0), scen(price, volume, cost)
    key = "profit" if C is not None else "revenue"
    kpis = [{"label": "Monthly revenue", "value": fmt_num(s["revenue"]), "delta": delta_txt(s["revenue"], b["revenue"])}]
    if s["units"] is not None:
        kpis.append({"label": "Monthly units", "value": f"{s['units']:,.0f}", "delta": delta_txt(s["units"], b["units"])})
    if C is not None:
        kpis.append({"label": "Monthly gross profit", "value": fmt_num(s["profit"]), "delta": delta_txt(s["profit"], b["profit"]) if b["profit"] else None})
        kpis.append({"label": "Gross margin", "value": pct(s["profit"] / s["revenue"] if s["revenue"] else None), "delta": delta_txt(s["profit"] / s["revenue"] if s["revenue"] else None, b["profit"] / b["revenue"], "pp")})
    if s["penalty"] is not None:
        kpis.append({"label": "Monthly penalties", "value": fmt_num(s["penalty"]), "delta": delta_txt(s["penalty"], b["penalty"]), "good": "down"})
    labels, cur, new = ["Revenue"], [b["revenue"]], [s["revenue"]]
    if C is not None:
        labels += ["Cost", "Gross profit"]
        cur += [b["cost"], b["profit"]]
        new += [s["cost"], s["profit"]]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=labels, y=cur, name="Current (monthly average)", marker=dict(color="#94a3b8", cornerradius=4)))
    fig.add_trace(go.Bar(x=labels, y=new, name="Scenario", marker=dict(color=BLUE, cornerradius=4)))
    fig.update_layout(barmode="group")
    style(fig, "Current vs scenario (per month)", 340, True)
    ps, vs = [-10, -5, 0, 5, 10], [-20, -10, 0, 10, 20]
    z = [[(scen(p_, v_, cost)[key] - b[key]) / abs(b[key]) * 100 if b[key] else 0 for p_ in ps] for v_ in vs]
    hm = go.Figure(go.Heatmap(z=z, x=[f"{p_:+d}%" for p_ in ps], y=[f"{v_:+d}%" for v_ in vs], colorscale=[[0, BAD], [.5, "#f7f6f2"], [1, GOOD]], zmid=0, text=[[f"{x:+.0f}%" for x in r] for r in z], texttemplate="%{text}", showscale=False,
                              hovertemplate="Price %{x}, volume %{y}: %{z:+.1f}%<extra></extra>"))
    hm.update_xaxes(title="Price change")
    hm.update_yaxes(title="Volume change")
    style(hm, f"Change in monthly {key} by price and volume", 340)
    be = None
    if price and C is not None and b["profit"]:
        for v_ in np.arange(-60, 120, 1):
            if scen(price, v_, cost)["profit"] >= b["profit"]:
                be = v_
                break
    insights = [f"Baseline is the monthly average of the last {months:.0f} month(s): revenue {fmt_num(R)}" + (f", gross profit {fmt_num(R - C)}." if C is not None else ".")]
    if price and e == 0:
        insights.append("With no price sensitivity assumed, volume does not react to the price change. Try the stressed options to test the downside.")
    if price and e != 0:
        insights.append(f"With an elasticity of {e:g}, a {price:+.0f}% price change moves volume by about {e * price:+.0f}%.")
    if be is not None and price < 0:
        insights.append(f"To hold gross profit after a {price:.0f}% price cut, volume would need to rise by about {be:+.0f}%.")
    notes = ["Customer price sensitivity is an assumption you choose; uploaded sales data alone cannot prove how customers react to a price change." if e == 0 or True else "",
             "Costs scale with volume. Fixed costs (rent, wages) are not in this data, so profit here means gross profit." if C is not None else "No cost column is mapped, so only revenue is shown. Map a Unit cost or Total cost column to see profit."]
    return {"kpis": kpis, "chart": fj(fig), "heatmap": fj(hm), "insights": insights, "notes": [n for n in notes if n], "has_cost": C is not None, "baseline_months": round(months, 1)}


# ============================================================================================ alerts
RULES = [("Sales drop", "sales", "The latest complete month's revenue is at least 15% below the average of the six months before it (30% = critical)."),
         ("Sales surge", "sales", "The latest complete month is at least 25% above that average."),
         ("Unusual weakness", "sales", "Revenue or orders in a recent month are 1.8+ typical deviations below the median (median / MAD method)."),
         ("Declining products", "sales", "A top-20 product sold at least 35% less in the last 90 days than in the 90 days before."),
         ("Margin squeeze", "sales", "Gross margin fell by 3 percentage points or more versus the previous 90 days (needs cost)."),
         ("Overdue orders", "delivery", "An open order is past its due date (needs due dates)."),
         ("Supplier lateness", "delivery", "A supplier was late on 25%+ of at least 20 deliveries in the last 180 days."),
         ("Delivery risk", "delivery", "Open orders have a predicted late risk of at least double the historical rate (needs enough history to train)."),
         ("Stock-out / low stock", "stock", "A product is out of stock, below its reorder level, or has under 7 days of cover (needs stock).")]
ORDER = {"critical": 0, "warning": 1, "info": 2}


def alerts(lab: D.Lab) -> list[dict]:
    c = canon(lab)
    out: list[dict] = []

    def add(sev, cat, page, head, ev, act, ask):
        out.append({"severity": sev, "category": cat, "page": page, "headline": head, "evidence": ev, "action": act, "ask": ask})
    col = "amount" if has(c, "amount") else "qty"
    nm = "revenue" if col == "amount" else "units"
    ms = complete_months(c, monthly(c, col))
    fired_month = None
    if len(ms) >= 4:
        prior = ms.iloc[-7:-1] if len(ms) >= 7 else ms.iloc[:-1]
        base, last = float(prior.mean()), float(ms.iloc[-1])
        if base > 0:
            ch = last / base - 1
            if ch <= -0.15:
                fired_month = ms.index[-1]
                add("critical" if ch <= -0.30 else "warning", "Sales drop", "sales", f"{nm.capitalize()} in {ms.index[-1]:%B %Y} fell {abs(ch) * 100:.0f}% below the recent average",
                    f"{fmt_num(last)} versus an average of {fmt_num(base)} over the previous {len(prior)} months.", "Check which products, stores or suppliers drove the drop and whether it is seasonal.", f"Why did {nm} drop in {ms.index[-1]:%B %Y}?")
            elif ch >= 0.25:
                add("info", "Sales surge", "sales", f"{nm.capitalize()} in {ms.index[-1]:%B %Y} ran {ch * 100:.0f}% above the recent average", f"{fmt_num(last)} versus {fmt_num(base)}.",
                    "Make sure stock and supply can keep up with the higher demand.", f"What drove the {nm} increase in {ms.index[-1]:%B %Y}?")
    if len(ms) >= 8:
        for name, series in ((nm, ms), ("orders", complete_months(c, monthly(c, "orders")))):
            if len(series) < 8:
                continue
            med = series.median()
            mad = (series - med).abs().median() * 1.4826
            if mad and mad > 0:
                for p_, v in ((p_, (val - med) / mad) for p_, val in series.tail(3).items()):
                    if v <= -1.8 and p_ != fired_month:
                        add("warning" if v <= -2.5 else "info", "Unusual weakness", "sales", f"{name.capitalize()} was unusually weak in {p_:%B %Y}", f"{abs(v):.1f} typical deviations below the median ({fmt_num(series.loc[p_])} vs {fmt_num(med)}).",
                            "Check whether it was seasonal, a stock-out or a supplier problem.", f"Is there anything unusual in {name} around {p_:%B %Y}?")
                        break
    end = as_of(c)
    if has(c, "amount", "product"):
        rec, old = c[c["date"] > end - pd.Timedelta(days=90)], c[(c["date"] <= end - pd.Timedelta(days=90)) & (c["date"] > end - pd.Timedelta(days=180))]
        if len(old) >= 20 and len(rec) >= 20:
            top = old.groupby("product")["amount"].sum().sort_values(ascending=False).head(20)
            now = rec.groupby("product")["amount"].sum().reindex(top.index).fillna(0)
            drop = ((now / top) - 1)
            bad = drop[drop <= -0.35].sort_values()
            if len(bad):
                add("info", "Declining products", "sales", f"{len(bad)} top product(s) are selling much less than a quarter ago", ", ".join(f"{p} ({v * 100:.0f}%)" for p, v in bad.head(3).items()) + ".",
                    "Check pricing, availability and promotions for these products.", f"Why are sales of {bad.index[0]} down in the last 90 days?")
        if has(c, "cost") and len(old) >= 20:
            def gm(d):
                return (d["amount"].sum() - d["cost"].sum()) / d["amount"].sum() if d["amount"].sum() else np.nan
            a, b = gm(rec), gm(old)
            if not (np.isnan(a) or np.isnan(b)) and a - b <= -0.03:
                add("warning", "Margin squeeze", "sales", f"Gross margin fell {abs(a - b) * 100:.1f} points to {a * 100:.1f}%", f"Previous 90 days: {b * 100:.1f}%.", "Review supplier cost changes and discounting.", "Why did gross margin fall in the last 90 days?")
    if has(c, "due"):
        o = c[c["due"].notna() & c["delivered"].isna()]
        od = o[o["due"] < end]
        if len(od):
            add("critical" if len(od) >= 5 else "warning", "Overdue orders", "delivery", f"{len(od)} open order(s) are past their due date", f"Oldest due date: {od['due'].min():%d %b %Y}.", "Contact those customers today and agree a new date.", "Which orders are overdue?")
        dl = c[c["due"].notna() & c["delivered"].notna() & (c["delivered"] > end - pd.Timedelta(days=180))]
        if has(dl, "supplier") and len(dl):
            g = dl.assign(late=dl["delivered"] > dl["due"]).groupby("supplier")["late"].agg(["mean", "size"])
            bad = g[(g["size"] >= 20) & (g["mean"] >= .25)].sort_values("mean", ascending=False)
            if len(bad):
                add("critical" if bad["mean"].iloc[0] >= .4 else "warning", "Supplier lateness", "delivery", f"{bad.index[0]} delivered {bad['mean'].iloc[0] * 100:.0f}% of orders late",
                    f"{int(bad['size'].iloc[0])} deliveries in the last 180 days." + (f" {len(bad) - 1} other supplier(s) are also above 25%." if len(bad) > 1 else ""), "Escalate with the supplier or shift volume.", f"Which suppliers deliver late most often?")
        lm = late_model(lab)
        if lm.get("ok") and len(o):
            oo = open_orders(lab).dropna(subset=["risk"])
            hi = int((oo["risk"] >= max(2 * lm["base"], .4)).sum()) if len(oo) else 0
            if hi:
                rate = penalty_rate(c)
                extra = f" Expected penalty exposure about {fmt_num(float((oo['risk'] * oo['amount'] * rate).sum()))}." if (rate and has(oo, "amount")) else ""
                add("critical" if hi >= 5 else "warning", "Delivery risk", "delivery", f"{hi} of {len(oo)} open orders are at high risk of finishing late", f"Model accuracy (AUC) {lm['auc']:.2f}." + extra,
                    "Pull the highest-risk orders forward and warn those customers.", "Which open orders are most likely to be late?")
    if has(c, "product", "stock"):
        t = stock_position(lab)
        if len(t):
            out_n, low = int((t["status"] == "Out of stock").sum()), int(t["status"].isin(["Below reorder", "Low cover"]).sum())
            if out_n:
                nm_ = ", ".join(t[t["status"] == "Out of stock"]["product"].astype(str).head(3))
                add("critical", "Stock-out / low stock", "stock", f"{out_n} product(s) are out of stock", f"{nm_}{'...' if out_n > 3 else ''}." + (f" About {fmt_num(float(t['weekly_risk'].sum()))} of weekly sales at risk." if t["weekly_risk"].sum() > 0 else ""),
                    "Raise purchase orders now; the Stock page suggests quantities.", "Which products are out of stock or about to run out?")
            if low:
                add("warning", "Stock-out / low stock", "stock", f"{low} product(s) are below their reorder level or have under 7 days of cover", "See the Stock page for days of cover.", "Reorder before they sell out.", "Which products will run out of stock this week?")
    if not out:
        add("info", "All clear", "sales", "Nothing needs attention right now", "No alert rule fired on this data.", "Keep monitoring; upload fresh data to re-check.", "Give me an overview of performance.")
    return sorted(out, key=lambda a: ORDER[a["severity"]])


def active_rules(lab: D.Lab) -> list[dict]:
    c = canon(lab)
    need = {"Margin squeeze": has(c, "cost", "amount"), "Overdue orders": has(c, "due"), "Supplier lateness": has(c, "due", "delivered", "supplier"), "Delivery risk": late_model(lab).get("ok", False),
            "Stock-out / low stock": has(c, "product", "stock"), "Declining products": has(c, "amount", "product")}
    return [{"rule": n, "fires": t, "active": need.get(n, True)} for n, _, t in RULES]


# ============================================================================================ penalty control
def penalties(lab: D.Lab, expedite: int = 0, assumed_rate: float = 0.0) -> dict:
    """What late deliveries cost, where it comes from, and what expediting the riskiest open orders would save.

    Uses the mapped Late penalty column when present; otherwise a penalty rate assumed by the user (percent of order value).
    """
    c = canon(lab)
    dl = c[c["due"].notna() & c["delivered"].notna()].copy()
    dl["late"] = dl["delivered"] > dl["due"]
    mapped = has(c, "penalty") and float(c["penalty"].sum()) > 0
    rate = penalty_rate(c) if mapped else None
    src = "mapped" if rate else ("assumed" if assumed_rate > 0 else "none")
    use_rate = rate if rate else (assumed_rate / 100 if assumed_rate > 0 else None)
    if mapped:
        dl["cost"] = dl["penalty"]
    elif use_rate and has(dl, "amount"):
        dl["cost"] = np.where(dl["late"], dl["amount"] * use_rate, 0.0)
    else:
        dl["cost"] = np.nan
    n_late, n_del = int(dl["late"].sum()), len(dl)
    end = as_of(c)
    paid_all = float(dl["cost"].sum()) if dl["cost"].notna().any() else None
    paid_12 = float(dl[dl["delivered"] > end - pd.Timedelta(days=365)]["cost"].sum()) if paid_all is not None else None
    o = open_orders(lab)
    lm = late_model(lab)
    notes, charts, insights, actions = [], [], [], []
    expedite = int(max(0, min(expedite or 0, max(len(o), 0))))
    kpis = [{"label": "Late orders", "value": f"{n_late:,}"}, {"label": "Late rate", "value": pct(n_late / n_del if n_del else None)}]
    if paid_all is not None:
        kpis = [{"label": "Penalties paid" + ("" if src == "mapped" else " (estimated)"), "value": fmt_num(paid_all)}, {"label": "Last 12 months", "value": fmt_num(paid_12)}] + kpis
    elif use_rate is None:
        notes.append("No Late penalty column is mapped. Map one on the Data page, or enter an assumed penalty rate on the left to estimate what lateness costs.")
    if src == "assumed":
        notes.append(f"Penalty cost is estimated at {assumed_rate:g}% of the value of each late order (your assumption).")
    # ---- history charts
    if paid_all:
        m = dl.set_index("delivered")["cost"].resample("MS").sum()
        fig = go.Figure(go.Bar(x=m.index, y=m.values, marker=dict(color=BAD, cornerradius=4), hovertemplate="%{x|%b %Y}: %{y:,.0f}<extra></extra>"))
        charts.append({"id": "month", "title": "Penalties by month of delivery", "fig": fj(style(fig, "Penalties by month of delivery", 320))})
        for dname, title in (("supplier", "Penalties by supplier"), ("product", "Penalties by product"), ("category", "Penalties by category"), ("region", "Penalties by store / region")):
            if has(dl, dname):
                g = dl.groupby(dname)["cost"].sum().sort_values(ascending=False).head(10).iloc[::-1]
                if len(g) >= 2 and g.sum() > 0:
                    fig = go.Figure(go.Bar(y=g.index.astype(str), x=g.values, orientation="h", marker=dict(color=BLUE, cornerradius=4), hovertemplate="%{y}: %{x:,.0f}<extra></extra>"))
                    charts.append({"id": dname, "title": title, "fig": fj(style(fig, title, 320))})
                    insights.append(f"{g.index[-1]} accounts for the most penalty cost ({fmt_num(float(g.iloc[-1]))}, {g.iloc[-1] / g.sum() * 100:.0f}% of the top 10).")
                    break
    elif n_del:
        g = dl.groupby("supplier")["late"].agg(["mean", "size"]) if has(dl, "supplier") else None
        if g is not None and len(g[g["size"] >= 8]) >= 2:
            g = g[g["size"] >= 8].sort_values("mean").tail(10)
            fig = go.Figure(go.Bar(y=g.index.astype(str), x=g["mean"] * 100, orientation="h", marker=dict(color=BAD, cornerradius=4)))
            fig.update_xaxes(ticksuffix="%")
            charts.append({"id": "supplier", "title": "Late rate by supplier", "fig": fj(style(fig, "Late rate by supplier", 320))})
    # ---- open-order exposure and recovery plan
    table, plan_ok = None, False
    if len(o) and use_rate and has(o, "amount"):
        base = lm["base"] if lm.get("ok") else (n_late / n_del if n_del else 0.0)
        o = o.copy()
        o["risk"] = o["risk"].fillna(base) if lm.get("ok") else base
        o["exposure"] = o["risk"] * o["amount"] * use_rate
        o = o.sort_values("exposure", ascending=False)
        plan_ok = bool(lm.get("ok"))
        top = set(o.head(expedite).index) if plan_ok else set()
        o["after"] = np.where(o.index.isin(top), np.minimum(o["risk"], base), o["risk"]) * o["amount"] * use_rate
        now_t, after_t = float(o["exposure"].sum()), float(o["after"].sum())
        kpis += [{"label": "Open-order exposure", "value": fmt_num(now_t)}]
        if expedite and plan_ok:
            kpis.append({"label": "After your plan", "value": fmt_num(after_t), "delta": f"-{fmt_num(now_t - after_t)} saved", "good": "down"})
        elif not plan_ok:
            notes.append(f"A late-risk model could not be trained ({lm.get('why', 'not enough history')}), so every open order is assumed to carry the average late rate ({base * 100:.0f}%) and the recovery plan is disabled.")
        t12 = o.head(12)
        fig = go.Figure()
        lbl = [f"{r.label} {r.product if has(o, 'product') and pd.notna(r.product) else ''}".strip() for r in t12.itertuples()][::-1]
        fig.add_trace(go.Bar(y=lbl, x=t12["exposure"][::-1], orientation="h", name="Exposure now", marker=dict(color=BAD, cornerradius=4)))
        fig.add_trace(go.Bar(y=lbl, x=t12["after"][::-1], orientation="h", name="After your plan", marker=dict(color=GOOD, cornerradius=4)))
        fig.update_layout(barmode="group")
        charts.insert(0, {"id": "exposure", "title": "Penalty exposure on the riskiest open orders", "fig": fj(style(fig, "Penalty exposure on the riskiest open orders", 440, True))})
        tab = pd.DataFrame({"Order": o["label"], "Product": o["product"] if has(o, "product") else "", "Due": o["due"].dt.strftime("%d %b %Y"), "Order value": o["amount"].round(0),
                            "Late risk": (o["risk"] * 100).round(0).astype(int).astype(str) + "%", "Exposure now": o["exposure"].round(0), "Exposure after plan": o["after"].round(0),
                            "Plan": ["Expedite" if i in top else "" for i in o.index]})
        table = df_to_json(tab.dropna(axis=1, how="all").head(60))
        insights.append(f"The 5 riskiest open orders carry {fmt_num(float(o['exposure'].head(5).sum()))} of the {fmt_num(now_t)} total exposure.")
        actions += ["Expedite orders in table order: the largest exposure first.", "Where exposure stays high after expediting, the supplier or capacity is the cause: see the Deliveries page."]
        notes.append("Exposure = late risk x order value x penalty rate. An expedited order is assumed to perform like a typical order. These are planning estimates, not guarantees.")
    elif not len(o):
        notes.append("There are no open orders (orders with a due date but no delivered date), so there is no forward exposure to manage.")
    return {"kpis": kpis, "charts": charts, "table": table, "insights": insights, "actions": actions, "notes": notes, "plan_ok": plan_ok, "source": src, "n_open": int(len(o)), "mapped": mapped}
