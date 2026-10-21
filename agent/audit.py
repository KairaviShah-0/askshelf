"""Tamper-evident audit log.

Every question is recorded with who asked (role), what ran, how it ended and what it cost. Each entry stores the hash
of the previous one, so editing or deleting any past line breaks the chain and `verify()` reports exactly where.
Questions are PII-masked before they are written. The file is JSON Lines and is git-ignored.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import guardrails as G

AUDIT_DIR = Path(__file__).resolve().parent.parent / "audit"
AUDIT_FILE = AUDIT_DIR / "audit_log.jsonl"
_lock = threading.Lock()
GENESIS = "0" * 64


def _hash(prev: str, body: dict) -> str:
    return hashlib.sha256((prev + json.dumps(body, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()


def _last_hash(path: Path) -> str:
    if not path.exists():
        return GENESIS
    last = GENESIS
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                last = json.loads(line)["hash"]
            except (ValueError, KeyError):
                pass
    return last


def log(*, session: str, role: str, question: str, outcome: str, tools: list[str], mode: str, latency_ms: int, est_cost_usd: float,
        path: Path | None = None) -> dict:
    path = path or AUDIT_FILE
    masked, _ = G.sanitize_output(question[:300])
    masked = masked.split("\n\n_Some restricted")[0]
    body = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "session": session[:8], "role": role, "question": masked,
            "outcome": outcome, "tools": tools, "mode": mode, "latency_ms": latency_ms, "est_cost_usd": round(est_cost_usd, 6)}
    with _lock:
        path.parent.mkdir(exist_ok=True)
        prev = _last_hash(path)
        entry = {**body, "prev": prev, "hash": _hash(prev, body)}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    return entry


def read(path: Path | None = None) -> list[dict]:
    path = path or AUDIT_FILE
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"corrupt": line[:80]})
    return out


def verify(path: Path | None = None) -> tuple[bool, int, int | None]:
    """Returns (intact, entries_checked, first_bad_line_number)."""
    prev = GENESIS
    entries = read(path)
    for i, e in enumerate(entries, 1):
        if "corrupt" in e or e.get("prev") != prev:
            return False, i - 1, i
        body = {k: v for k, v in e.items() if k not in ("prev", "hash")}
        if _hash(prev, body) != e.get("hash"):
            return False, i - 1, i
        prev = e["hash"]
    return True, len(entries), None
