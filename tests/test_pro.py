"""Tests for the predictive models and advanced tools."""
import numpy as np
import pytest

from agent import llm, predict as P, tools as T


def test_late_model_is_strong_and_quote_win_model_is_honest():
    assert P.late_model().auc > 0.9
    assert 0.35 < P.win_model().auc < 0.65          # no measurable signal: the app must say so, not fake it


def test_price_elasticity_is_not_significant():
    el = P.price_elasticity()
    assert el["lo"] < el["estimate"] < el["hi"] and not el["significant"]


def test_simulator_baseline_and_directions():
    base = P.sim_baseline()
    assert abs(base["profit"]) < 60_000 and 0.5 < base["on_time"] < 1
    assert P.simulate(P.Scenario(quote_volume_change_pct=30))["on_time"] < base["on_time"]
    assert P.simulate(P.Scenario(extra_makers=2))["on_time"] > base["on_time"]
    assert P.simulate(P.Scenario(timber_cost_change_pct=20))["profit"] < base["profit"]
    assert P.simulate(P.Scenario(price_change_pct=10, price_elasticity=-4))["win_rate"] < base["win_rate"]


@pytest.mark.parametrize("metric", ["revenue", "profit", "costs", "quotes", "accepted", "win_rate", "on_time"])
def test_forecast_bands_bracket_the_forecast(metric):
    f = P.forecast(metric, 4)
    assert len(f.future) == 4 and (f.future["lo"] <= f.future["forecast"]).all() and (f.future["forecast"] <= f.future["hi"]).all()
    assert f.method in P.METHODS


@pytest.mark.parametrize("name,args", [("late_risk_radar", {}), ("forecast_outlook", {"metric": "quotes"}), ("scenario_simulator", {"price_change_pct": "10"}),
                                        ("capacity_planner", {}), ("price_strategy_analysis", {}), ("profit_bridge", {}), ("anomaly_scan", {}),
                                        ("quote_funnel", {}), ("executive_briefing", {})])
def test_pro_tools_return_chart_insights_and_no_restricted_columns(name, args):
    r = T.run_tool(name, args)
    assert r.title != "Analysis error", r.notes
    assert r.chart is not None and r.facts and r.table is not None
    cols = " ".join(str(c).lower() for c in r.table.columns)
    assert not any(h in cols for h in ("customer", "employee", "wage"))


def test_routing_of_predictive_questions_offline():
    assert llm.offline_route("What if we raise prices by 10% and demand grows 20%?")[0][1] == {"price_change_pct": 10.0, "quote_volume_change_pct": 20.0}
    assert llm.offline_route("Which orders are most likely to be late?")[0][0] == "late_risk_radar"
    assert llm.offline_route("Forecast our profit for the next 6 months")[0] == ("forecast_outlook", {"metric": "profit", "horizon_months": 6})
    assert llm.offline_route("Give me a full executive briefing")[0][0] == "executive_briefing"


def test_report_is_self_contained_html():
    from agent import report
    html = report.build_html("last_quarter")
    assert "Executive briefing" in html and "plotly" in html.lower() and "customerId" not in html
