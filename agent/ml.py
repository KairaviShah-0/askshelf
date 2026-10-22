"""Small, explainable modelling helpers (numpy + pandas only): regularised logistic regression and honest cross-validated AUC."""
from __future__ import annotations

import numpy as np
import pandas as pd


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
