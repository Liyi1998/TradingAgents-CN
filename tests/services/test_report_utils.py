from app.services.report_utils import dedupe_report_aliases


def test_dedupe_only_removes_exact_known_aliases():
    reports, aliases = dedupe_report_aliases({
        "research_team_decision": "same research",
        "investment_plan": "same research",
        "final_trade_decision": "same final",
        "risk_management_decision": "same final",
        "risk_manager_decision": "same final",
        "market_report": "independent",
    })
    assert set(reports) == {"research_team_decision", "final_trade_decision", "market_report"}
    assert aliases["investment_plan"] == "research_team_decision"
    assert aliases["risk_manager_decision"] == "final_trade_decision"


def test_dedupe_preserves_different_stage_content():
    reports, aliases = dedupe_report_aliases({
        "research_team_decision": "research",
        "investment_plan": "different trader plan",
    })
    assert "investment_plan" in reports
    assert aliases == {}
