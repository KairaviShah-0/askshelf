"""Role-based access control (RBAC).

Each role may use only some analysis tools. The check runs inside the pipeline *after* the LLM has chosen its tools and
*before* any tool runs, so a role can never receive data it is not cleared for, whatever the question says.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import tools as T


@dataclass(frozen=True)
class Role:
    name: str
    blurb: str
    groups: frozenset[str]
    can_export: bool = False


ROLES: dict[str, Role] = {
    "Owner / General Manager": Role("Owner / General Manager", "Full access to every analysis and export.",
                                    frozenset({"Overview", "Profitability", "Operations", "People", "Materials", "Sales", "Predictive", "Help"}), True),
    "Sales Manager": Role("Sales Manager", "Sales, quotes, pricing, demand and forecasts. No workforce or cost-structure detail.",
                          frozenset({"Overview", "Sales", "Predictive", "Help"})),
    "Operations Supervisor": Role("Operations Supervisor", "Production, delivery, materials and capacity. No pricing or profit detail.",
                                  frozenset({"Operations", "Materials", "People", "Help"})),
    "Finance Lead": Role("Finance Lead", "Profit, margins, cash trends and forecasts. Can export reports.",
                         frozenset({"Overview", "Profitability", "Predictive", "Help"}), True),
}
DEFAULT_ROLE = "Owner / General Manager"

# Predictive tools that expose another function's data are narrowed per role (group-level access is too coarse for these).
_TOOL_DENY = {
    "Sales Manager": {"capacity_planner", "late_risk_radar", "penalty_control"},
    "Finance Lead": {"capacity_planner"},
}
_TOOL_ALLOW_EXTRA = {"Operations Supervisor": {"late_risk_radar", "capacity_planner"}, "Finance Lead": {"penalty_control"}}


def allowed_tools(role: str) -> set[str]:
    r = ROLES.get(role, ROLES[DEFAULT_ROLE])
    out = {n for n, s in T.REGISTRY.items() if s["group"] in r.groups}
    out -= _TOOL_DENY.get(r.name, set())
    out |= _TOOL_ALLOW_EXTRA.get(r.name, set())
    if r.name == "Operations Supervisor":
        out -= {"forecast_outlook", "scenario_simulator", "price_strategy_analysis"}
    return out


def can(role: str, tool: str) -> bool:
    return tool in allowed_tools(role)


def denial_message(role: str, tools: list[str]) -> str:
    return (f"Your role ({role}) is not cleared for this analysis ({', '.join(t.replace('_', ' ') for t in tools)}). "
            "Please ask the Owner / General Manager for access. I can still help with: "
            + ", ".join(sorted({T.REGISTRY[t]['group'].lower() for t in allowed_tools(role) if t != 'data_dictionary'})) + ".")


def access_matrix():
    """Rows = tools, columns = roles (for the Security page)."""
    import pandas as pd
    rows = []
    for n, s in T.REGISTRY.items():
        row = {"Analysis": n.replace("_", " ").title(), "Area": s["group"]}
        for rn in ROLES:
            row[rn] = "Allowed" if can(rn, n) else "Blocked"
        rows.append(row)
    return pd.DataFrame(rows)
