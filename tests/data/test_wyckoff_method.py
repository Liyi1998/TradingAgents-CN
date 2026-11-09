"""Method regressions through real calculations, submission tools and Mongo I/O."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from app.data.query.volume_comparisons import volume_comparisons
from app.data.query.technical_analysis import build_technical_snapshot
from app.data.query.research_tracking import daily_metrics
from app.engine.research_contract import append_research_contract
from app.engine.research_tracking import validate_update
from app.engine.wyckoff_method import METHOD_CONTRACT, METHOD_VERSION
from app.engine.wyckoff_tracking import checked_assessment, baseline_assessment
from app.engine.wyckoff_submission import capture_technical_snapshot, check_submission
from app.engine.orchestrator.workflow.submission import SubmissionBox, make_submit_report_tool
from app.services.wyckoff_history import previous_wyckoff


def actual_six_sessions():
    # OHLCV audited from the saved 603232 raw daily snapshot, 2026-11-03.
    rows = [("2026-10-26",15.61,17.70,15.02,17.03,35351000),
        ("2026-10-27",16.87,17.36,15.90,15.93,25300300),
        ("2026-10-28",15.95,16.29,15.18,15.80,18641400),
        ("2026-10-29",15.55,15.60,14.52,14.53,20104100),
        ("2026-10-30",14.00,14.37,13.08,13.08,17226800),
        ("2026-11-02",13.09,13.98,12.66,13.79,16722700)]
    return [dict(zip(("date","open","high","low","close","volume"), r),
        data_source="audited_daily", price_basis="raw", volume_unit="shares", evidence_id="daily:"+r[0]) for r in rows]


def method_case():
    # Earlier bars are explicit structural test fixtures, not real stock prices.
    start = date(2026,10,1)
    bars = [{"date": (start+timedelta(days=i)).isoformat(), "open": 14., "close": 14.1,
        "low": 13., "high": 15., "volume": 10000., "evidence_id": f"sample:{i}"} for i in range(25)]
    bars += actual_six_sessions()
    snapshot = {"as_of": "2026-11-02", "price": bars[-1], "bars": bars}
    quote = "趋势背景：下跌；当前阶段：吸筹候选，尚待后续测试；也可能是下跌中继或再派发。"
    sc = "2026-10-30 SC候选：急跌可能释放部分卖压。"
    ar = "2026-11-02 AR候选：收盘反弹，尚不能确认反转。"
    confirm = "等待后续回踩测试和区间上沿突破。"
    invalid = "跌破前低并持续走弱则该停止假说失效。"
    change = "相较上次下跌判断，新增反弹仅支持停止行为候选，尚未证明趋势反转。"
    alt = "也可能是下跌中继或再派发。"
    content = "## 威科夫研究\n" + "\n".join([quote, sc, ar, confirm, invalid, change])
    value = {"method_version": METHOD_VERSION, "trend": "down", "phase": "accumulation", "status": "candidate",
        "source_quote": quote, "evidence_ids": [b["evidence_id"] for b in bars[-6:]],
        "events": [{"kind": "SC", "date": "2026-10-30", "status": "candidate", "source_quote": sc},
            {"kind": "AR", "date": "2026-11-02", "status": "candidate", "source_quote": ar}],
        "confirmation_conditions": [confirm], "invalidation_conditions": [invalid],
        "alternative_scenario": alt, "change_reason": change}
    return value, content, snapshot


def test_dated_comparisons_distinguish_adjacent_day_endpoints_and_monotonicity():
    bars = actual_six_sessions(); days = [b["date"] for b in bars]
    result = volume_comparisons(bars, days)
    assert result["previous_session"]["change_pct"] == pytest.approx(-2.926254)
    assert result["previous_session"]["reference_start"] == "2026-10-30"
    assert result["window_endpoints"]["change_pct"] < -50
    assert result["window_endpoints"]["reference_start"] == "2026-10-26"
    assert result["window_endpoints"]["strictly_decreasing_every_session"] is False
    assert result["window_endpoints"]["increased_on"] == ["2026-10-29"]
    assert result["previous_5_mean"]["reference_end"] == "2026-10-30"
    assert result["previous_5_mean"]["reference_volume"] == sum(b["volume"] for b in bars[:-1])/5
    assert daily_metrics(bars, days)["volume_comparisons"] == result


@pytest.mark.parametrize("defect", ["missing", "duplicate", "source", "basis", "unit", "intraday", "nan", "bool"])
def test_volume_comparisons_do_not_bridge_missing_or_incompatible_observations(defect):
    bars = actual_six_sessions(); days = [b["date"] for b in bars]
    if defect == "missing": bars.pop(3)
    elif defect == "duplicate": bars.append(deepcopy(bars[0]))
    else:
        k, v = {"source": ("data_source","other"), "basis": ("price_basis","qfq"), "unit": ("volume_unit","lots"),
            "intraday": ("status","intraday"), "nan": ("volume",float("nan")), "bool": ("volume",True)}[defect]
        bars[1][k] = v
    assert volume_comparisons(bars, days)["previous_session"] is None


def test_zero_equal_and_future_volumes_do_not_create_infinite_or_false_trend():
    bars = actual_six_sessions(); days = [b["date"] for b in bars]
    for bar in bars: bar["volume"] = 0
    bars.append({**bars[-1], "date": "2026-11-03", "volume": 999999999})
    output = volume_comparisons(bars, days)
    assert output["previous_session"]["change_pct"] is None
    assert output["window_endpoints"]["strictly_decreasing_every_session"] is False
    json.dumps(output, allow_nan=False)


def test_full_technical_snapshot_injects_same_dated_calculations_and_capture_cutoff():
    bars = actual_six_sessions()
    records = [{**b, "trade_date": b["date"], "period": "daily", "symbol": "603232"} for b in bars]
    calendar = [{"cal_date": (date(2026,10,26)+timedelta(days=i)).isoformat(), "is_open": i not in (5,6)} for i in range(8)]
    snap = build_technical_snapshot(records, calendar, "2026-11-02", ["audited_daily"], trajectory_days=0)
    assert snap["volume_comparisons"]["previous_session"]["change_pct"] == pytest.approx(-2.926254)
    context = {"last_completed_trade_date": "2026-10-30"}
    capture_technical_snapshot(json.dumps({"data": json.dumps(snap)}), context)
    assert context["wyckoff_snapshots"]["price"]["date"] == "2026-10-30"
    assert all(b["date"] <= "2026-10-30" for b in context["wyckoff_snapshots"]["bars"])
    snap["price_basis"] = "stored_price_adjustment_unverified"
    capture_technical_snapshot(snap, context)
    assert context["wyckoff_snapshots"]["price"] is None


def test_old_calendar_hole_does_not_erase_recent_valid_comparisons():
    bars = actual_six_sessions()
    records = [{**b, "trade_date":b["date"], "period":"daily"} for b in bars]
    records.insert(0, {**records[0], "trade_date":"2026-09-30"})
    calendar = [{"cal_date":(date(2026,10,26)+timedelta(days=i)).isoformat(), "is_open":i not in (5,6)} for i in range(8)]
    result = build_technical_snapshot(records, calendar, "2026-11-02", ["audited_daily"], trajectory_days=0)
    assert result["calendar_complete"] is False
    assert result["volume_comparisons"]["previous_session"]["change_pct"] == pytest.approx(-2.926254)


def test_sc_ar_can_be_candidates_in_downtrend_without_forcing_or_freezing_phase():
    value, content, snapshot = method_case()
    assert checked_assessment(value, content, snapshot, require_method=True)["trend"] == "down"
    value["status"] = "supported"
    value["source_quote"] = value["source_quote"].replace("吸筹候选，尚待后续测试", "吸筹获得本轮证据支持")
    content += "\n" + value["source_quote"]
    with pytest.raises(ValueError, match="仅SC/AR"):
        checked_assessment(value, content, snapshot, require_method=True)


def test_spring_requires_an_earlier_range_not_an_integer_level():
    value, content, snapshot = method_case()
    event = {"kind":"Spring", "date":"2026-11-02", "status":"observed", "source_quote":"2026-11-02 Spring：下探12.66后收13.79。"}
    value["events"] = [event]; content += "\n"+event["source_quote"]
    with pytest.raises(ValueError, match="先前交易区间"):
        checked_assessment(value, content, snapshot)
    quote = "2026-10-01至2026-10-25区间支撑13.00、阻力15.00。"
    content += "\n"+quote
    value["trading_range"] = {"start":"2026-10-01","end":"2026-10-25","support":13.,"resistance":15.,"source_quote":quote}
    assert checked_assessment(value, content, snapshot)["events"][0]["kind"] == "Spring"
    snapshot["bars"][-1]["low"] = 13.1
    with pytest.raises(ValueError, match="下探后收回"):
        checked_assessment(value, content, snapshot)


def test_supported_accumulation_requires_test_but_does_not_require_a_climax():
    value, content, snapshot = method_case()
    quote = "2026-10-01至2026-10-25区间支撑13.00、阻力15.00。"
    event_quote = "2026-11-02 ST：回踩后的量价支持测试解释。"
    value.update(status="supported", events=[{"kind":"ST", "date":"2026-11-02", "status":"observed", "source_quote":event_quote}],
        trading_range={"start":"2026-10-01","end":"2026-10-25","support":13.,"resistance":15.,"source_quote":quote})
    value["source_quote"] = value["source_quote"].replace("吸筹候选，尚待后续测试", "吸筹获得本轮证据支持")
    content += "\n" + value["source_quote"]
    content += "\n"+quote+"\n"+event_quote
    assert checked_assessment(value, content, snapshot)["status"] == "supported"


def test_downtrend_to_accumulation_transition_is_allowed_with_reason_but_not_automatic_confirmation():
    value, content, snapshot = method_case()
    quote = "趋势背景：下跌；当前阶段：下跌；向吸筹转换的候选仍待确认。"
    value.update(phase="markdown", candidate_phase="accumulation", status="transition", source_quote=quote)
    snapshot["previous_wyckoff"] = {"phase":"markdown", "as_of":"2026-10-30"}
    content += "\n"+quote
    result = checked_assessment(value, content, snapshot, require_method=True)
    assert result["phase"] == "markdown" and result["candidate_phase"] == "accumulation"
    assert result["status"] == "transition"


def test_negated_or_prior_phase_does_not_satisfy_current_stage_and_sc_ar_order_is_checked():
    value, content, snapshot = method_case()
    value["source_quote"] = "趋势背景：下跌；当前阶段：并非吸筹；仍是待确认假说。"
    content += "\n"+value["source_quote"]
    with pytest.raises(ValueError, match="分列当前阶段"):
        checked_assessment(value, content, snapshot)
    value, content, snapshot = method_case()
    value["events"][0].update(status="observed", date="2026-11-02", source_quote="2026-11-02 SC事件解释。")
    value["events"][1].update(status="observed", date="2026-10-30", source_quote="2026-10-30 AR事件解释。")
    content += "\n"+"\n".join(e["source_quote"] for e in value["events"])
    with pytest.raises(ValueError, match="顺序"):
        checked_assessment(value, content, snapshot)


def test_baseline_current_stage_is_separate_from_trend_background():
    value, content, _ = method_case()
    assert baseline_assessment({"reports":{"market_report":content}})["phase"] == "accumulation"


@pytest.mark.parametrize("defect", ["future_event", "missing_id", "missing_change", "missing_alt", "legacy_testing", "hidden_candidate"])
def test_method_inconsistencies_only_limit_optional_assessment(defect):
    value, content, snap = method_case()
    if defect == "future_event": value["events"][0]["date"] = "2026-11-04"
    if defect == "missing_id": value["evidence_ids"] = [snap["price"]["evidence_id"]]
    if defect == "missing_change": snap["previous_wyckoff"] = {"phase":"markdown"}; value.pop("change_reason")
    if defect == "missing_alt": value.pop("alternative_scenario")
    if defect == "legacy_testing": value["phase"] = "testing"; value["source_quote"] += "试盘"; content += "\n"+value["source_quote"]
    if defect == "hidden_candidate": content = content.replace("候选，尚待后续测试", "初期"); value["source_quote"] = value["source_quote"].replace("候选，尚待后续测试", "初期").replace("也可能", "仍是"); content += "\n"+value["source_quote"]
    with pytest.raises(ValueError): checked_assessment(value, content, snap, require_method=True)
    note, fields = check_submission(content, value, {"wyckoff_snapshots":snap, "wyckoff_prior_snapshots":snap.get("previous_wyckoff")})
    assert fields["wyckoff"] is None and content in note
    assert "不能作为已确认阶段" in note
    assert baseline_assessment({"reports":{"market_report":note}}) is None


@pytest.mark.parametrize("horizon", ["short", "medium", "long"])
def test_all_stage_contracts_override_conflicting_templates_without_removing_horizon(horizon):
    for role in ("market", "bull_researcher", "bear_researcher", "research_manager", "trader", "risky_analyst", "safe_analyst", "neutral_analyst", "risk_manager", "summarizer"):
        prompt = append_research_contract("威科夫旧规则：吸筹则买入", horizon, agent_key=role)
        assert prompt.count(METHOD_CONTRACT) == 1 and prompt.endswith(METHOD_CONTRACT)
        assert append_research_contract(prompt, horizon, agent_key=role).count(METHOD_CONTRACT) == 1
    assert METHOD_CONTRACT not in append_research_contract("核验财务", horizon, agent_key="fundamentals")


def test_prior_report_is_injected_once_not_repeated_in_all_role_contexts():
    from app.data.core.analysis_context import prompt_context
    context = {"as_of":"2026-11-02", "wyckoff_prior_snapshots":{"source_quote":"private-method-history-sample"},
        "wyckoff_snapshots":{"bars":[{"date":"2026-11-02"}]}}
    assert "private-method-history-sample" not in prompt_context(context)
    assert "2026-11-02" in prompt_context(context)


@pytest.mark.asyncio
async def test_actual_full_report_submission_keeps_body_when_method_is_bad():
    value, content, snapshot = method_case()
    for valid in (True, False):
        state = {"analysis_context":{"wyckoff_snapshots":snapshot}}; box = SubmissionBox()
        tool = make_submit_report_tool(box, node_type="analyst", agent_key="market", report_key="market_report", state=state)
        assert "wyckoff" in tool.params_schema["properties"]
        supplied = deepcopy(value)
        if not valid: supplied["status"] = "supported"
        await tool.handler(content=content, wyckoff=supplied)
        assert (box.value.fields["wyckoff"] is not None) == valid
        assert content in state["reports"]["market_report"]
    other = make_submit_report_tool(SubmissionBox(), node_type="analyst", agent_key="fundamentals")
    assert "wyckoff" not in other.params_schema["properties"]


@pytest.mark.asyncio
async def test_real_prior_report_lookup_respects_owner_horizon_market_and_historical_cutoff(real_mongo_db):
    db = real_mongo_db; user = "method-test-"+uuid4().hex
    base = {"user_id":user,"stock_symbol":"603232","analysis_horizon":"short","market_type":"CN","status":"completed",
        "analysis_date":"2026-10-30", "created_at":datetime(2026,10,30,8,tzinfo=timezone.utc), "analysis_id":"before",
        "reports":{"market_report":"## 威科夫阶段\n当前阶段：下跌。\n## 账户\n不要传递这段"}}
    rows = [base]
    for key, change in [("owner",{"user_id":user+"-other"}), ("horizon",{"analysis_horizon":"long"}),
        ("market",{"market_type":"HK"}), ("future",{"created_at":datetime(2026,11,3,8,tzinfo=timezone.utc)}),
        ("date",{"analysis_date":"2026-11-04"}), ("failed",{"status":"failed"})]:
        rows.append({**base, "analysis_id":key, **change})
    try:
        await db.analysis_reports.insert_many(rows)
        before = await db.analysis_reports.count_documents({"user_id":user})
        result = await previous_wyckoff(db,user_id=user,symbol="603232",analysis_date="2026-11-02",horizon="short",now=datetime(2026,11,5,tzinfo=timezone.utc))
        assert result["analysis_id"] == "before" and "不要传递" not in result["source_quote"]
        assert await previous_wyckoff(db,user_id="absent",symbol="603232",analysis_date="2026-11-02",horizon="short") is None
        assert await db.analysis_reports.count_documents({"user_id":user}) == before
    finally:
        await db.analysis_reports.delete_many({"user_id":{"$in":[user,user+"-other"]}})


@pytest.mark.asyncio
async def test_real_data_facade_preserves_short_and_long_analysis_with_partial_history(real_mongo_db):
    from app.data.core.interface import DataInterface
    from app.data.storage.mongo.collections import get_collection_name
    assert "test" in real_mongo_db.name
    symbol = "method_"+uuid4().hex
    coll = real_mongo_db[get_collection_name("daily_quotes", "CN")]
    calendar = real_mongo_db[get_collection_name("trade_calendar", "CN")]
    rows = [{**b, "symbol":symbol, "market":"CN", "trade_date":b["date"], "period":"daily", "data_source":"baostock",
        "volume_unit":"share", "amount_unit":"CNY", "amount":b["volume"]*b["close"], "unit_conversion":"baostock:native_share_CNY"}
        for b in actual_six_sessions()]
    inserted = await calendar.insert_many([{"exchange":"SSE", "market":"CN", "data_source":"method_test",
        "cal_date":(date(2026,10,26)+timedelta(days=i)).isoformat(), "is_open": i not in (5,6)} for i in range(8)])
    await coll.insert_many(rows)
    DataInterface.reset_instance()
    try:
        for horizon in ("short", "long"):
            result = await DataInterface.get_instance().technical_snapshot("CN", symbol, "2026-11-02", refresh=False, analysis_horizon=horizon)
            assert result["status"] == "success" and result["recent_daily"][-1]["close"] == 13.79
            assert result["volume_comparisons"]["previous_session"]["change_pct"] == pytest.approx(-2.926254)
            assert result["indicators"]["ma250"] is None
            if horizon == "long": assert result["history_window_status"] == "partial"
    finally:
        await coll.delete_many({"symbol":symbol})
        await calendar.delete_many({"_id":{"$in":inserted.inserted_ids}})
        DataInterface.reset_instance()
