"""Central configuration: brand palette, entity lists, and the data-privacy policy."""
from __future__ import annotations

APP_NAME = "AskShelf"
APP_TAGLINE = "Retail Sales Intelligence Assistant"
COMPANY = "Modern Furniture Co."

# --- Entities (fixed order = fixed colour: colour follows the entity, never its rank) ---
PRODUCTS = ["Bookshelf", "Dining Table", "Media Console", "Office Desk", "Sideboard"]
MATERIALS = ["American Walnut", "Tasmanian Oak", "Victorian Ash"]

# Validated categorical palette (light surface), applied in fixed slot order.
CAT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
PRODUCT_COLORS = dict(zip(PRODUCTS, CAT))
MATERIAL_COLORS = dict(zip(MATERIALS, CAT[:3]))
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SURFACE = "#fcfcfb"
BLUE_SEQ = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]

# --- Business constants taken from the simulator data (verified against the ledger) ---
DEPOSIT_SHARE = 0.5            # 50% deposit at acceptance, 50% on delivery
LATE_PENALTY_PCT = 0.10        # applied to the final payment of a late order
WORK_HOURS_PER_DAY = 8         # assumption used only for the utilisation estimate

# --- Privacy policy: what the agent will never expose ---
# Columns that are never loaded into the analysis layer (see data.py allow-lists).
RESTRICTED_FIELDS = {
    "customerId": "Customer identifier",
    "customers_data.csv": "Customer-to-order mapping (not loaded at all)",
    "inquiryText": "Free-text customer enquiry (contains locations)",
    "employeeId / designerId": "Individual employee identifiers",
    "wage (per person)": "Individual pay - only skill-band rates are shown",
}

MANAGER_MESSAGE = (
    "That detail is restricted, so I can't share it. "
    "Please contact your manager for this information. "
    "I can still help with aggregated insights - for example totals, averages and trends by product, "
    "material, quarter or skill level."
)
SCOPE_MESSAGE = (
    "I can only help with questions about Modern Furniture Co.'s operations data - sales, production, "
    "delivery, people, materials and finances. I can't act on that request."
)

MAX_QUESTIONS_PER_SESSION = 30
DEFAULT_MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]

# --- Cost model assumptions (USD per million tokens) - update to the current price list of your Gemini model ---
LLM_IN_PER_M = 0.30
LLM_OUT_PER_M = 2.50
