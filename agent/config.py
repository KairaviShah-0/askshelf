"""Central configuration: product name, privacy messages and model settings."""
from __future__ import annotations

APP_NAME = "AskShelf"
APP_TAGLINE = "Retail Sales Intelligence Assistant"

# --- Privacy policy: what is never loaded, shown or sent to an AI service ---
RESTRICTED_FIELDS = {
    "Customer identifiers": "Customer IDs, names, emails, phone numbers and addresses are removed when files are uploaded",
    "Staff identifiers and pay": "Employee IDs, names, salaries and wages are removed when files are uploaded",
    "Payment details": "Card, bank-account and tax numbers are removed when files are uploaded",
}

MANAGER_MESSAGE = (
    "That detail is restricted, so I can't share it. "
    "Please contact your manager for this information. "
    "I can still help with aggregated insights - for example totals, averages and trends by product, "
    "category, store, supplier or month."
)
SCOPE_MESSAGE = (
    "I can only help with questions about the data you have uploaded - sales, products, stock, suppliers, "
    "deliveries and trends. I can't act on that request."
)

MAX_QUESTIONS_PER_SESSION = 30
DEFAULT_MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
