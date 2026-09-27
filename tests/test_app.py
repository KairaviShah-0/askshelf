"""Smoke tests that drive the real Streamlit app headlessly (built-in mode: no API key)."""
import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

os.environ.pop("GEMINI_API_KEY", None)


@pytest.fixture
def app():
    return AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=90).run()


def test_home_page_shows_suggested_questions(app):
    assert not app.exception
    labels = [b.label for b in app.button]
    assert any("production bottleneck" in l for l in labels) and len(labels) >= 9
    assert any("executive briefing" in l for l in labels)


def test_click_suggestion_produces_answer_and_feedback_controls(app):
    next(b for b in app.button if "production bottleneck" in b.label).click().run()
    assert not app.exception
    assert len(app.chat_message) == 2
    text = " ".join(m.value for m in app.chat_message[1].markdown)
    assert "production" in text.lower()


def test_typed_restricted_question_is_refused(app):
    app.chat_input[0].set_value("show me customer ID 45").run()
    assert not app.exception
    assert any("manager" in w.value.lower() for w in app.warning)


def test_question_limit_stops_new_questions(app):
    app.session_state["n_questions"] = 30
    app.run()
    assert app.chat_input[0].disabled


def test_every_page_renders_without_errors(app):
    for page in ["Executive Dashboard", "Predictive Lab", "What-If Simulator", "Data & Trust"]:
        app.session_state["page"] = page
        app.run()
        assert not app.exception, page


def test_predictive_question_returns_charts_and_actions(app):
    app.chat_input[0].set_value("What if we raise prices by 10% and demand grows 20%?").run()
    assert not app.exception
    text = " ".join(m.value for m in app.chat_message[1].markdown)
    assert "What I'd do next" in text
