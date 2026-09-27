from agent import feedback


def test_feedback_log_and_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "FEEDBACK_DIR", tmp_path)
    monkeypatch.setattr(feedback, "FEEDBACK_FILE", tmp_path / "log.csv")
    assert feedback.summary()["total"] == 0
    feedback.log("rating", rating="up", question="q1", tools_used="business_summary", mode="offline")
    feedback.log("rating", rating="down", question="q2")
    feedback.log("definition", category="Term or definition misunderstood", comment="lead time\nshould exclude weekends " + "x" * 900)
    s = feedback.summary()
    assert (s["total"], s["up"], s["down"], s["definitions"]) == (3, 1, 1, 1)
    assert "\n" not in s["df"]["comment"].iloc[-1] and len(s["df"]["comment"].iloc[-1]) <= 500
