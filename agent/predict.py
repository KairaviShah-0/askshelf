"""Predictive modelling engine (numpy + pandas only, so it deploys anywhere).

Everything here is deliberately small and explainable: regularised logistic regression with standardised inputs,
honest cross-validated accuracy (AUC), and a seasonal-trend forecaster with prediction bands. No employee or customer
identifiers are ever used as features.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
import pandas as pd

from . import data as D
from .config import PRODUCTS, MATERIALS


# ----------------------------------------------------------------------------------------------- core maths
def auc_score(y: np.ndarray, p: np.ndarray) -> float:
    """Area under the ROC curve via rank statistics (0.5 = coin flip, 1.0 = perfect)."""
    y = np.asarray(y).astype(int)
    pos, neg = (y == 1).sum(), (y == 0).sum()
    if pos == 0 or neg == 0:
        return float("nan")
    ranks = pd.Series(p).rank().values
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2) / (pos * neg))


class Logit:
    """L2-regularised logistic regression fitted by Newton's method on standardised features."""

    def __init__(self, l2: float = 1.0):
        self.l2, self.mu, self.sd, self.w, self.cols = l2, None, None, None, []

    def fit(self, X: pd.DataFrame, y) -> "Logit":
        self.cols = list(X.columns)
        A = X.values.astype(float)
        self.mu, self.sd = A.mean(0), A.std(0)
        self.sd[self.sd == 0] = 1.0
        Z = np.c_[np.ones(len(A)), (A - self.mu) / self.sd]
        y = np.asarray(y, float)
        w = np.zeros(Z.shape[1])
        reg = np.eye(Z.shape[1]) * self.l2
        reg[0, 0] = 0
        for _ in range(60):
            p = 1 / (1 + np.exp(-np.clip(Z @ w, -30, 30)))
            g = Z.T @ (p - y) + reg @ w
            H = (Z * (p * (1 - p))[:, None]).T @ Z + reg + 1e-6 * np.eye(len(w))
            step = np.linalg.solve(H, g)
            w -= step
            if np.abs(step).max() < 1e-7:
                break
        self.w = w
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        A = X[self.cols].values.astype(float)
        Z = np.c_[np.ones(len(A)), (A - self.mu) / self.sd]
        return 1 / (1 + np.exp(-np.clip(Z @ self.w, -30, 30)))

    def drivers(self) -> pd.DataFrame:
        """Standardised coefficients: effect on log-odds of a one-standard-deviation increase."""
        return pd.DataFrame({"feature": self.cols, "effect": self.w[1:]}).sort_values("effect", key=abs, ascending=False)


def cv_auc(X: pd.DataFrame, y, l2: float = 1.0, k: int = 5, seed: int = 7) -> float:
    """Stratified k-fold cross-validated AUC (pooled out-of-fold predictions)."""
    y = np.asarray(y).astype(int)
    rng = np.random.default_rng(seed)
    fold = np.zeros(len(y), int)
    for cls in (0, 1):
        idx = np.where(y == cls)[0]
        rng.shuffle(idx)
        fold[idx] = np.arange(len(idx)) % k
    oof = np.zeros(len(y))
    for f in range(k):
        tr, te = fold != f, fold == f
        oof[te] = Logit(l2).fit(X[tr], y[tr]).predict_proba(X[te])
    return auc_score(y, oof)


# ----------------------------------------------------------------------------------------------- workload features
@lru_cache(maxsize=1)
def _context() -> pd.DataFrame:
    """Daily shop-floor context: orders in production and staffed capacity (used as model features)."""
    from .tools import _wip_daily
    ds = D.load()
    ctx = pd.DataFrame({"wip": _wip_daily()})
    ctx["makers"] = ds.staff_daily["maker"] - ds.staff_daily["maker_leave"]
    ctx["designers"] = ds.staff_daily["designer"] - ds.staff_daily["designer_leave"]
    ctx["wip_per_maker"] = ctx["wip"] / ctx["makers"].clip(lower=1)
    return ctx


def _ctx_at(dates: pd.Series) -> pd.DataFrame:
    c = _context()
    return c.reindex(pd.DatetimeIndex(dates).normalize()).ffill().bfill().reset_index(drop=True)


def _dummies(df: pd.DataFrame, col: str, cats: list[str], prefix: str) -> pd.DataFrame:
    return pd.DataFrame({f"{prefix}: {c}": (df[col] == c).astype(float).values for c in cats[1:]}, index=df.index)


# ----------------------------------------------------------------------------------------------- quote win model
@dataclass
class WinModel:
    model: Logit
    auc: float
    n: int
    base_rate: float
    ref: dict = field(default_factory=dict)      # product -> mean price (for relative price)

    def features(self, df: pd.DataFrame, months_index: pd.Series | None = None) -> pd.DataFrame:
        X = pd.DataFrame(index=df.index)
        X["Price ($k)"] = df["quotePrice"] / 1000
        X["Price vs product average"] = df["quotePrice"] / df["productType"].map(self.ref) - 1
        X["Complexity"] = df["complexity"]
        X["Timber cost share"] = df["materialCost"] / df["quotePrice"]
        return pd.concat([X, _dummies(df, "productType", PRODUCTS, "Product"), _dummies(df, "materialType", MATERIALS, "Timber")], axis=1)

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        return self.model.predict_proba(self.features(df))


@lru_cache(maxsize=1)
def win_model() -> WinModel:
    o = D.load().orders.copy()
    ref = o.groupby("productType")["quotePrice"].mean().to_dict()
    m = WinModel(Logit(2.0), 0.0, len(o), float(o["won"].mean()), ref)
    X = m.features(o)
    m.auc = cv_auc(X, o["won"], l2=2.0)
    m.model.fit(X, o["won"])
    return m


# ----------------------------------------------------------------------------------------------- late-delivery risk model
@dataclass
class LateModel:
    model: Logit
    auc: float
    n: int
    base_rate: float

    @staticmethod
    def features(df: pd.DataFrame) -> pd.DataFrame:
        c = _ctx_at(df["acceptanceDate"])
        X = pd.DataFrame(index=df.index)
        X["Orders per available maker"] = c["wip_per_maker"].values
        X["Days allowed (quote to due)"] = (df["dueDate"] - df["acceptanceDate"]).dt.days.values
        X["Maker hours"] = df["maker_hours"].values
        X["Complexity"] = df["complexity"].values
        return pd.concat([X, _dummies(df.reset_index(drop=True), "productType", PRODUCTS, "Product").set_index(df.index)], axis=1)

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        return self.model.predict_proba(self.features(df))


@lru_cache(maxsize=1)
def late_model() -> LateModel:
    o = D.load().orders
    d = o[o["delivered"]].reset_index(drop=True)
    X = LateModel.features(d)
    y = d["is_late"].astype(int)
    auc = cv_auc(X, y, l2=5.0)
    return LateModel(Logit(5.0).fit(X, y), auc, len(d), float(y.mean()))


# ----------------------------------------------------------------------------------------------- price response
def price_response(product: str, complexity: int, material: str, prices: np.ndarray) -> np.ndarray:
    """Model-implied win probability across a range of prices for one type of order."""
    m = win_model()
    base = D.load().orders
    mat_cost = base.loc[base["productType"] == product, "materialCost"].median()
    df = pd.DataFrame({"productType": product, "materialType": material, "complexity": complexity,
                       "quotePrice": prices, "materialCost": mat_cost})
    return m.predict(df)


def price_uncertainty(product: str, complexity: int, material: str, prices: np.ndarray, n_boot: int = 120, seed: int = 11):
    """Bootstrap band (10th-90th percentile) around the model-implied win probability."""
    m = win_model()
    o = D.load().orders.copy()
    X = m.features(o)
    y = o["won"].astype(int).values
    mat_cost = o.loc[o["productType"] == product, "materialCost"].median()
    df = pd.DataFrame({"productType": product, "materialType": material, "complexity": complexity,
                       "quotePrice": prices, "materialCost": mat_cost})
    Xn = m.features(df)
    rng = np.random.default_rng(seed)
    draws = []
    for _ in range(n_boot):
        idx = rng.integers(0, len(o), len(o))
        draws.append(Logit(2.0).fit(X.iloc[idx], y[idx]).predict_proba(Xn))
    d = np.array(draws)
    return np.percentile(d, 10, axis=0), np.percentile(d, 90, axis=0)


# ----------------------------------------------------------------------------------------------- price sensitivity
@lru_cache(maxsize=1)
def price_elasticity(n_boot: int = 300, seed: int = 5) -> dict:
    """Data-estimated price elasticity of the win rate: % change in win rate for a 1% price rise (bootstrap 80% interval)."""
    o = D.load().orders
    x = np.log(o["quotePrice"] / o["productType"].map(o.groupby("productType")["quotePrice"].mean())).values
    y = o["won"].astype(float).values
    Xd = pd.DataFrame({"x": x})

    def est(idx):
        m = Logit(0.5).fit(Xd.iloc[idx], y[idx])
        beta = m.w[1] / m.sd[0]
        return beta * (1 - y[idx].mean())

    rng = np.random.default_rng(seed)
    full = est(np.arange(len(y)))
    boots = [est(rng.integers(0, len(y), len(y))) for _ in range(n_boot)]
    lo, hi = np.percentile(boots, [10, 90])
    return {"estimate": float(full), "lo": float(lo), "hi": float(hi), "significant": bool(lo > 0 or hi < 0)}


# ----------------------------------------------------------------------------------------------- monthly series + forecasting
def monthly_series() -> pd.DataFrame:
    """Complete calendar months only: quotes, accepted, win rate, revenue, costs, profit, on-time rate, deliveries."""
    ds = D.load()
    o, l = ds.orders, ds.ledger
    last_full = pd.Period(ds.as_of - pd.Timedelta(days=1), "M")
    q = o.groupby(o["creationDate"].dt.to_period("M")).agg(quotes=("id", "count"), accepted=("won", "sum"))
    q["win_rate"] = q["accepted"] / q["quotes"]
    lm = l.assign(m=l["date"].dt.to_period("M")).pivot_table(index="m", columns="type", values="amount", aggfunc="sum").fillna(0)
    t = ds.timeline
    d = t.groupby(t["completionDate"].dt.to_period("M")).agg(delivered=("id", "count"), late=("is_late", "sum"))
    d["on_time"] = 1 - d["late"] / d["delivered"]
    out = q.join(lm.rename(columns={"REVENUE": "revenue", "COST": "costs"}), how="outer").join(d, how="left")
    out["profit"] = out["revenue"] - out["costs"]
    return out[out.index <= last_full]


def _seasonal_trend(y: pd.Series, h: int, damp: float = 0.6) -> np.ndarray:
    """Level + damped linear trend x shrunk calendar-month seasonality."""
    idx = y.index
    months = np.array([p.month for p in idx])
    seas = pd.Series(y.values / y.mean(), index=months).groupby(level=0).mean().reindex(range(1, 13)).fillna(1.0)
    seas = 1 + 0.5 * (seas - 1)                        # shrink: only ~2 observations per calendar month
    t = np.arange(len(y))
    slope, icpt = np.polyfit(t, y.values / seas.reindex(months).values, 1)
    fut_idx = pd.period_range(idx[-1] + 1, periods=h, freq="M")
    trend = icpt + slope * (len(y) - 1) + slope * np.cumsum(damp ** np.arange(1, h + 1))
    return trend * seas.reindex([p.month for p in fut_idx]).values


def _method_flat(y, h):
    return np.full(h, y.iloc[-12:].mean())


def _method_seasonal_naive(y, h):
    fut = pd.period_range(y.index[-1] + 1, periods=h, freq="M")
    lvl = y.iloc[-12:].mean() / max(y.iloc[-24:-12].mean(), 1e-9) if len(y) >= 24 else 1.0
    return np.array([y.get(p - 12, y.iloc[-12:].mean()) * (0.5 + 0.5 * lvl) for p in fut])


METHODS = {
    "Recent average": _method_flat,
    "Same month last year": _method_seasonal_naive,
    "Seasonal trend": _seasonal_trend,
    "Blend (trend + recent average)": lambda y, h: 0.5 * _seasonal_trend(y, h) + 0.5 * _method_flat(y, h),
}


def _rolling_errors(y: pd.Series, h: int) -> dict[str, np.ndarray]:
    errs = {k: [] for k in METHODS}
    for cut in range(12, len(y) - h + 1):
        tr, te = y.iloc[:cut], y.iloc[cut:cut + h]
        for k, fn in METHODS.items():
            errs[k].append(te.values - fn(tr, h))
    return {k: np.array(v) for k, v in errs.items()}


@dataclass
class Forecast:
    history: pd.Series
    future: pd.DataFrame            # index=Period, columns forecast, lo, hi
    method: str
    backtest: pd.DataFrame          # one row per method: rolling-origin error
    windows: int


def forecast(metric: str, horizon: int = 3) -> Forecast:
    """Pick the method that would have forecast best in a rolling back-test, and size the 80% band from its real errors."""
    y = monthly_series()[metric].astype(float).dropna()
    h = int(min(max(horizon, 1), 6))
    errs = _rolling_errors(y, min(h, 3))
    scale = max(np.abs(y).mean(), 1e-9)
    rows = [{"Method": k, "Typical error": float(np.abs(v).mean()), "Error %": float(np.abs(v).mean() / scale)} for k, v in errs.items()]
    bt = pd.DataFrame(rows).sort_values("Typical error").reset_index(drop=True)
    best = bt.iloc[0]["Method"]
    vals = METHODS[best](y, h)
    band = float(np.percentile(np.abs(errs[best]), 80))
    widen = np.sqrt(1 + 0.1 * np.arange(h))
    fut = pd.DataFrame({"forecast": vals, "lo": vals - band * widen, "hi": vals + band * widen},
                       index=pd.period_range(y.index[-1] + 1, periods=h, freq="M"))
    if metric in ("win_rate", "on_time"):
        fut = fut.clip(0, 1)
    elif metric != "profit":
        fut = fut.clip(lower=0)
    return Forecast(y, fut, best, bt, len(errs[best]))


# ----------------------------------------------------------------------------------------------- what-if simulator
@dataclass
class Scenario:
    price_change_pct: float = 0.0
    quote_volume_change_pct: float = 0.0
    extra_makers: float = 0.0
    timber_cost_change_pct: float = 0.0
    price_elasticity: float | None = None       # None = use the data-estimated value


@lru_cache(maxsize=1)
def baseline() -> dict:
    """Unit economics of the most recent 12 complete months (the launch point for every scenario)."""
    ds = D.load()
    o, l, sd_ = ds.orders, ds.ledger, ds.staff_daily
    end = pd.Timestamp(ds.as_of - pd.Timedelta(days=1)).to_period("M").to_timestamp("M")
    start = (end - pd.DateOffset(months=12) + pd.Timedelta(days=1)).normalize()
    w = o[(o["creationDate"] >= start) & (o["creationDate"] <= end)]
    won = w[w["won"]]
    lw = l[(l["date"] >= start) & (l["date"] <= end)]
    t = ds.timeline
    tw = t[(t["completionDate"] >= start) & (t["completionDate"] <= end)]
    sdw = sd_[(sd_.index >= start) & (sd_.index <= end)]
    rates = ds.wage_rates
    rev = lw.loc[lw["type"] == "REVENUE", "amount"].sum()
    cost = lw.loc[lw["type"] == "COST", "amount"].sum()
    return dict(
        start=start, end=end, quotes_pm=len(w) / 12, win_rate=float(w["won"].mean()), avg_price=float(won["quotePrice"].mean()),
        fixed_pm=float(lw.loc[lw["category"].isin(["Wages", "Overheads"]), "amount"].sum() / 12),
        materials_pm=float(lw.loc[lw["category"] == "Material purchases", "amount"].sum() / 12),
        revenue_pm_actual=float(rev / 12), profit_pm_actual=float((rev - cost) / 12),
        late_rate=float(max(tw["is_late"].mean(), 0.01)), lead_days=float(tw["lead_days"].mean()),
        makers=float((sdw["maker"] - sdw["maker_leave"]).mean()), designers=float((sdw["designer"] - sdw["designer_leave"]).mean()),
        maker_rate=float(rates.loc[rates["role"] == "maker", "hourly_rate"].mean()),
        designer_rate=float(rates.loc[rates["role"] == "designer", "hourly_rate"].mean()),
        penalty_share=0.5 * 0.10, n_delivered=len(tw),
    )


@lru_cache(maxsize=1)
def _late_frame() -> pd.DataFrame:
    """Feature rows for orders delivered in the baseline window - the population scenarios are applied to."""
    b = baseline()
    d = D.load().orders
    d = d[d["delivered"] & (d["completionDate"] >= b["start"]) & (d["completionDate"] <= b["end"])].reset_index(drop=True)
    return LateModel.features(d)


def _mean_late(scale_wip: float, makers: float) -> float:
    lm = late_model()
    X = _late_frame().copy()
    X["Orders per available maker"] *= scale_wip * baseline()["makers"] / max(makers, 1)
    return float(lm.model.predict_proba(X).mean())


def simulate(sc: Scenario) -> dict:
    """Steady-state monthly economics under a scenario: transparent unit economics + the fitted late-delivery model."""
    b = baseline()
    el = price_elasticity()["estimate"] if sc.price_elasticity is None else sc.price_elasticity
    price = b["avg_price"] * (1 + sc.price_change_pct / 100)
    win = float(np.clip(b["win_rate"] * (1 + sc.price_change_pct / 100) ** el, 0.01, 0.95))
    quotes = b["quotes_pm"] * (1 + sc.quote_volume_change_pct / 100)
    orders = quotes * win
    base_orders = b["quotes_pm"] * b["win_rate"]
    makers = b["makers"] + sc.extra_makers
    p_base = _mean_late(1.0, b["makers"])
    p_scn = _mean_late(orders / base_orders, makers)
    late = float(np.clip(b["late_rate"] * p_scn / max(p_base, 1e-6), 0.0, 1.0))
    gross = orders * price
    penalties = gross * late * b["penalty_share"]
    revenue = gross - penalties
    materials = b["materials_pm"] * (orders / base_orders) * (1 + sc.timber_cost_change_pct / 100)
    hires = sc.extra_makers * b["maker_rate"] * 8 * 30.4
    costs = b["fixed_pm"] + hires + materials
    return dict(price=price, win_rate=win, quotes=quotes, orders=orders, wip=orders * b["lead_days"] / 30.4, late_rate=late,
                on_time=1 - late, revenue=revenue, penalties=penalties, materials=materials, fixed=b["fixed_pm"] + hires,
                costs=costs, profit=revenue - costs, margin=(revenue - costs) / revenue if revenue else float("nan"),
                makers=makers)


def sim_baseline() -> dict:
    return simulate(Scenario())
