"""Layered privacy guardrails.

Layer 1  data layer     - restricted columns never loaded (data.py)
Layer 2  input check    - block requests for restricted detail or prompt-injection (this file)
Layer 3  output filter  - regex redaction of anything that looks like an ID, key or contact detail (this file)
Layer 4  LLM critic     - a second model pass reviews the draft for leakage and unsupported claims (llm.py)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import MANAGER_MESSAGE, SCOPE_MESSAGE


@dataclass
class Verdict:
    allowed: bool
    category: str = "ok"          # ok | restricted_data | out_of_scope
    reason: str = ""
    message: str = ""


_RESTRICTED = [
    (r"\bcustomer\s*(id|ids|identifier|identifiers|number|numbers|no\.?|#)\b", "customer identifiers"),
    (r"\bcustomer\s*#?\s*\d+\b", "a specific customer"),
    (r"\bcustomer_?id\b", "customer identifiers"),
    (r"\bcustomers?_?data\b", "the customer file"),
    (r"\b(name|names|email|emails|phone|phones|mobile|address|addresses|contact details|surname)\b.{0,40}\bcustomers?\b", "customer contact details"),
    (r"\bcustomers?\b.{0,40}\b(name|names|email|emails|phone|phones|mobile|address|addresses|contact details|surname)\b", "customer contact details"),
    (r"\bwho (is|are|was|were|bought|ordered|placed)\b.{0,40}\b(customer|client|buyer)", "customer identity"),
    (r"\b(which|what) customers?\b.{0,30}\b(bought|ordered|placed|cancel|lost|declined)", "customer identity"),
    (r"\b(top|biggest|best|worst|largest)\s+(customer|client)s?\b", "customer identity"),
    (r"\b[dm]\d{1,2}\b", "an individual employee"),
    (r"\b(employee|staff|designer|maker|worker)\s*(id|ids|#|number)\b", "employee identifiers"),
    (r"\b(employee|staff|designer|maker|worker|person|individual)'?s?\s+(salary|wage|pay|earnings|leave record)", "individual pay/HR records"),
    (r"\b(salary|wage|pay)\s+of\s+(the\s+)?(employee|designer|maker|staff|him|her|them|[a-z]+\d+)", "individual pay"),
    (r"\bhow much (does|do|is|are)\b.{0,30}\b(earn|paid|make)\b", "individual pay"),
    (r"\b(dump|export|download|extract)\b.{0,30}\b(data|csv|ledger|orders|records|table|database|everything)\b", "a raw data export"),
    (r"\b(show|give|print|list|display)\b.{0,15}\b(all|every|the entire|the whole|raw|full)\b.{0,20}\b(rows?|records?|data|csv|ledger|orders|entries|transactions)\b", "a raw data export"),
    (r"\b(personal|private|confidential|sensitive)\b.{0,20}\b(data|information|details)\b", "personal information"),
]
_INJECTION = [
    r"ignore (all |any |the )?(previous|prior|above|earlier|your)\b.{0,20}(instructions|rules|prompt|guidelines)",
    r"(reveal|show|print|repeat|tell me)\b.{0,25}(system prompt|your prompt|your instructions|hidden instructions|your rules)",
    r"\b(api[ _-]?key|secret key|access token|password|credentials|secrets?\.toml)\b",
    r"\b(jailbreak|developer mode|dan mode|do anything now)\b",
    r"\b(pretend|act|behave) (as|like) (if )?(you|an? |the )",
    r"\bpretend (you|to be)\b",
    r"\byou are (now|no longer)\b",
    r"\b(disregard|override|bypass)\b.{0,20}\b(rules|guardrails|restrictions|filters|privacy)\b",
    r"(sql|python|shell|bash)\s+(code|command|query|script)\b.{0,30}\b(run|execute)\b",
]


def check_input(text: str) -> Verdict:
    """Decide whether a question may be answered."""
    t = (text or "").strip()
    if not t:
        return Verdict(False, "out_of_scope", "empty", "Please type a question about the business.")
    if len(t) > 800:
        return Verdict(False, "out_of_scope", "too long", "Please keep questions under about 800 characters.")
    low = t.lower()
    for pat in _INJECTION:
        if re.search(pat, low):
            return Verdict(False, "out_of_scope", "prompt-injection or credential request", SCOPE_MESSAGE)
    for pat, what in _RESTRICTED:
        if re.search(pat, low):
            return Verdict(False, "restricted_data", f"asked for {what}", MANAGER_MESSAGE)
    return Verdict(True)


# --- output filter -------------------------------------------------------------------------------------------
_OUT_PATTERNS = [
    (r"\b[DM]\d{1,2}\b", "[employee ID removed]"),
    (r"\bcustomer\s*(?:id|number|no\.?|#)?\s*[:#]?\s*\d+\b", "[customer detail removed]"),
    (r"\bcustomerId\b", "[restricted field]"),
    (r"AIza[0-9A-Za-z_\-]{30,}", "[key removed]"),
    (r"\bsk-[A-Za-z0-9]{20,}\b", "[key removed]"),
    (r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email removed]"),
    (r"(?<!\d)(?:\+?61|0)[\s-]?[2-478](?:[\s-]?\d){8}(?!\d)", "[phone removed]"),
]


def sanitize_output(text: str) -> tuple[str, list[str]]:
    """Redact anything that looks like a restricted identifier. Returns (clean_text, list_of_redactions)."""
    found: list[str] = []
    out = text or ""
    for pat, repl in _OUT_PATTERNS:
        flags = 0 if pat.startswith(r"\b[DM]") else re.I      # employee IDs are matched case-sensitively; the rest are not
        if re.search(pat, out, flags):
            found.append(repl)
            out = re.sub(pat, repl, out, flags=flags)
    if found:
        out += "\n\n_Some restricted details were removed from this answer. Please contact your manager if you need them._"
    return out, found


RESTRICTED_COLUMN_HINTS = ("customer", "employee", "designerid", "inquirytext", "wage")


def scrub_table(df):
    """Drop any column that looks restricted before a table is displayed."""
    if df is None:
        return None
    keep = [c for c in df.columns if not any(h in str(c).lower() for h in RESTRICTED_COLUMN_HINTS)]
    return df[keep]


# --- PII masking (used by the Security page lab and by the audit log) -------------------------------------------
_PII_EXTRA = [
    (r"\b(?:\d[ -]?){13,16}\b", "[card number]"),
    (r"\b\d{3}[ -]\d{3}[ -]\d{3}\b", "[tax file no.]"),
    (r"\b\d{1,4}\s+[A-Z][a-z]+\s+(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Drive|Dr)\b", "[address]"),
]


def mask_pii(text: str) -> tuple[str, dict[str, int]]:
    """Mask personal data in free text. Returns (masked_text, {label: count})."""
    out, counts = text or "", {}
    for pat, repl in [*_OUT_PATTERNS, *_PII_EXTRA]:
        flags = 0 if pat.startswith(r"\b[DM]") or repl == "[address]" else re.I
        out, n = re.subn(pat, repl, out, flags=flags)
        if n:
            counts[repl.strip("[]")] = counts.get(repl.strip("[]"), 0) + n
    return out, counts
