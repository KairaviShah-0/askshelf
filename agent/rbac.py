"""Role-based access control (RBAC).

Roles decide which pages (and therefore which analyses) a person can open, whether they may load or reset data, and whether they may export.
The check runs on every API call, so hiding a menu item in the browser is never the only protection.
"""
from __future__ import annotations

from dataclasses import dataclass

PAGES = ["data", "ask", "dashboard", "alerts", "deliveries", "penalties", "stock", "forecasts", "planner", "reports", "governance", "knowledge"]


@dataclass(frozen=True)
class Role:
    name: str
    blurb: str
    pages: frozenset[str]
    can_upload: bool = False
    can_export: bool = False


_ALL = frozenset(PAGES)
ROLES: dict[str, Role] = {
    "Owner / General Manager": Role("Owner / General Manager", "Full access: every page, data loading and exports.", _ALL, True, True),
    "Sales Manager": Role("Sales Manager", "Sales, forecasts and planning. Cannot load data or see delivery, penalty or stock detail.",
                          frozenset({"data", "ask", "dashboard", "alerts", "forecasts", "planner", "knowledge"})),
    "Operations Manager": Role("Operations Manager", "Deliveries, penalties and stock. Can load data. No sales planning or reports.",
                               frozenset({"data", "alerts", "deliveries", "penalties", "stock", "knowledge"}), True),
    "Finance Lead": Role("Finance Lead", "Dashboard, forecasts, planning, penalties and reports. Can load data and export.",
                         frozenset({"data", "ask", "dashboard", "alerts", "penalties", "forecasts", "planner", "reports", "governance", "knowledge"}), True, True),
}
DEFAULT_ROLE = "Owner / General Manager"
ALERT_PAGE = {"sales": "dashboard", "delivery": "deliveries", "stock": "stock"}


def can_view(role: str, page: str) -> bool:
    return page in ROLES.get(role, ROLES[DEFAULT_ROLE]).pages


def denial_message(role: str, page: str) -> str:
    return f"Your role ({role}) cannot open the {page} page. Ask the Owner / General Manager for access."


def access_matrix():
    """Rows = pages, columns = roles (for the Governance page)."""
    import pandas as pd
    labels = {"data": "Data (upload, mapping)", "ask": "Ask", "dashboard": "Dashboard", "alerts": "Alerts", "deliveries": "Deliveries", "penalties": "Penalties", "stock": "Stock",
              "forecasts": "Forecasts", "planner": "Scenario planner", "reports": "Reports", "governance": "Governance", "knowledge": "Knowledge base"}
    rows = []
    for p in PAGES:
        row = {"Page": labels[p]}
        for rn, r in ROLES.items():
            row[rn] = "Allowed" if p in r.pages else "Blocked"
        rows.append(row)
    for label, attr in (("Upload / reset data", "can_upload"), ("Export files", "can_export")):
        rows.append({"Page": label, **{rn: ("Allowed" if getattr(r, attr) else "Blocked") for rn, r in ROLES.items()}})
    return pd.DataFrame(rows)
