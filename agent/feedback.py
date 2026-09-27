"""Feedback module: thumbs up/down, issue reports and 'teach the agent' definition suggestions.

Stored in a CSV next to the app. On free hosting the disk is temporary (it resets on restart), which is fine for a
proof of concept; Phase 2 would write to a database (see README).
"""
from __future__ import annotations

import csv
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

FEEDBACK_DIR = Path(__file__).resolve().parent.parent / "feedback"
FEEDBACK_FILE = FEEDBACK_DIR / "feedback_log.csv"
FIELDS = ["timestamp_utc", "kind", "rating", "category", "comment", "question", "tools_used", "mode"]
CATEGORIES = ["Numbers look wrong", "Answer unclear", "Missing information", "Term or definition misunderstood", "Sensitive data concern", "Other"]
_lock = threading.Lock()


def _clean(text: str | None, limit: int = 500) -> str:
    t = re.sub(r"[\r\n]+", " ", str(text or "")).strip()
    return t[:limit]


def log(kind: str, *, rating: str = "", category: str = "", comment: str = "", question: str = "", tools_used: str = "", mode: str = "") -> None:
    """kind: 'rating' | 'issue' | 'definition'"""
    FEEDBACK_DIR.mkdir(exist_ok=True)
    row = {"timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, "rating": rating,
           "category": _clean(category, 80), "comment": _clean(comment), "question": _clean(question, 300),
           "tools_used": _clean(tools_used, 120), "mode": _clean(mode, 60)}
    with _lock:
        new = not FEEDBACK_FILE.exists()
        with FEEDBACK_FILE.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)


def load() -> pd.DataFrame:
    if not FEEDBACK_FILE.exists():
        return pd.DataFrame(columns=FIELDS)
    return pd.read_csv(FEEDBACK_FILE).fillna("")


def summary() -> dict:
    df = load()
    if df.empty:
        return {"total": 0, "up": 0, "down": 0, "issues": 0, "definitions": 0, "df": df}
    return {"total": len(df), "up": int((df["rating"] == "up").sum()), "down": int((df["rating"] == "down").sum()),
            "issues": int((df["kind"] == "issue").sum()), "definitions": int((df["kind"] == "definition").sum()), "df": df}
