"""Actual pure contracts and real Mongo/DataInterface I/O; no model calls/mocks."""
import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.llm.jev_conditions import ConditionConfig
from app.llm.jev_decisions import (
    POLICY, ExperimentError, StrategyInput, checked_decision, closed_sessions,
    daily_snapshot, decision_questions, evaluate_decision, make_packet,
    prepare_strategy, source_revision,
)
from app.services.jev_condition_config_service import (
    ConditionSettingsUpdate, SettingsRejected, get_condition_settings, update_condition_settings,
)
from app.services.jev_decision_service import (
    PREVIEWS, RUNS, get_experiments, preview_experiment, run_experiment,
)


def document(day=None):
    day = day or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    return {"analysis_id": "decision-validation-"+uuid4().hex, "user_id": "decision-owner",
        "status": "completed", "stock_symbol": "000001", "analysis_date": day.isoformat(),
        "analysis_horizon": "medium", "decision": {"action": "WATCH", "current_entry_limit_pct": 0},
        "reports": {"final_trade_decision": "研究样例，非投资建议。\n收盘超过12元且成交放大才可考虑入场。\n收盘跌破9元时复核退出。\n未取得盈利改善证据，不批准新增。"}}


def strategy(day=None):
    day = day or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    return StrategyInput(entry="收盘超过12元且成交放大才可考虑入场。", exit="收盘跌破9元时复核退出。",
        veto="未取得盈利改善证据，不批准新增。", valid_until=day+timedelta(days=2), complete_conditions_confirmed=True)


def response(entry="WAIT", existing="RESEARCH_REQUIRED", risk="INSUFFICIENT", allowed=False):
    fields = dict(entry_action=entry, existing_action=existing, risk_state=risk)
    return {"model": "jev-1.13.0", "answers": {k: {"type": "choice", "choice": fields[k],
        "confidence": 1., "probabilities": {c: float(c == fields[k]) for c in q["criteria"]}}
        for k, q in decision_questions(allowed).items()}}


def calendar(now):
    # Explicit synthetic exchange calendar fixture, not weekday-inferred live data.
    today = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    return [{"exchange": "SSE", "cal_date": (today-timedelta(days=i)).isoformat(), "is_open": int(i > 0)} for i in range(45)]


def rows(days):
    return [{"symbol": "000001", "market": "CN", "trade_date": d, "period": "daily", "price_basis": "raw",
        "data_source": "test", "volume_unit": "share", "amount_unit": "CNY",
        "open": 10., "high": 11., "low": 9., "close": 10.5, "volume": 1000., "amount": 10500.,
        "updated_at": datetime.now(timezone.utc)} for d in days]


def test_default_off_and_strict_inputs():
    assert not ConditionConfig().decision_experiments_enabled
    with pytest.raises(ValidationError): ConditionConfig(decision_experiments_enabled="true")
    with pytest.raises(ValidationError): StrategyInput(**{**strategy().model_dump(), "account": "private"})


@pytest.mark.parametrize("horizon", ["short", "medium", "long"])
def test_source_bound_compact_projection_never_sends_account(horizon):
    doc = document(); doc["analysis_horizon"] = horizon
    doc["account_position"] = {"shares": 20600, "cost": 3.613}
    doc["reports"]["market_report"] = "do-not-send"*5000
    before = deepcopy(doc)
    packet = make_packet(prepare_strategy(doc, strategy(), date.fromisoformat(doc["analysis_date"])), {})
    text = json.dumps(packet)
    for excluded in ("20600", "3.613", "account_position", "do-not-send", "user_id", "decision-owner"):
        assert excluded not in text
    assert not packet["strategy"]["entry_candidate_permitted"]
    assert doc == before


@pytest.mark.parametrize("value", [0, None, -1, float("nan"), float("inf"), True, "5", 101])
def test_invalid_or_zero_cap_cannot_offer_entry(value):
    doc = document(); doc["decision"].update(current_entry_limit_pct=value,
        position_evidence={"current_entry_limit_pct": {"status": "explicit"}})
    assert not prepare_strategy(doc, strategy(), date.fromisoformat(doc["analysis_date"]))["entry_candidate_permitted"]


def test_positive_cap_requires_explicit_evidence():
    doc = document(); doc["decision"]["current_entry_limit_pct"] = 5
    assert not prepare_strategy(doc, strategy(), date.fromisoformat(doc["analysis_date"]))["entry_candidate_permitted"]
    doc["decision"]["position_evidence"] = {"current_entry_limit_pct": {"status": "explicit"}}
    assert prepare_strategy(doc, strategy(), date.fromisoformat(doc["analysis_date"]))["entry_candidate_permitted"]


def test_partial_clause_cannot_drop_same_line_prerequisite():
    doc = document(); req = strategy(); req.entry = "收盘超过12元"
    with pytest.raises(ExperimentError, match="完整原文行"):
        prepare_strategy(doc, req, date.fromisoformat(doc["analysis_date"]))


@pytest.mark.parametrize("bad", ["new made-up condition", "成本价20元", "http://example.test/secret", "API key: secret", "当前持仓300股", "内网192.168.1.17", "contact@example.com"])
def test_unbound_or_private_passages_rejected(bad):
    doc = document(); req = strategy()
    if bad != "new made-up condition": doc["reports"]["final_trade_decision"] += "\n"+bad
    req.entry = bad
    with pytest.raises(ExperimentError): prepare_strategy(doc, req, date.fromisoformat(doc["analysis_date"]))


PUBLIC_PATHS = """### 4. 非当前授权的双路径研究

**路径A：回撤企稳。**保留7.95元附近历史枢纽、7.69元保护参考、8.43／8.50元目标的假设。须取得新分钟承接、同口径资金与量价改善、日线确认，并纳入可靠经营新证据、账户约束重新评级；仅到价不买入。保护线失效不向下挪动维持原方案。【来源：短线资金报告；主管裁决】

**路径B：突破回测。**完整收盘突破8.50元、后续回测有效且资金承接改善，先重评旧仓续持；8.88元仅为扩展观察位。新增风险还需重新评级、新支撑形成前不编造买入价、保护价或仓位额度。【来源：技术报告；主管裁决】

条件出现只启动更新研究，**不自动把WATCH转为BUY**。"""


@pytest.mark.parametrize("role", ["entry", "exit", "veto"])
def test_public_paths_bound_before_projection_and_no_constraint_lost(role):
    doc = document(); req = strategy()
    doc["reports"]["final_trade_decision"] += "\n" + PUBLIC_PATHS
    setattr(req, role, PUBLIC_PATHS)
    before = deepcopy(doc)
    packet = make_packet(prepare_strategy(doc, req, date.fromisoformat(doc["analysis_date"])), {})
    passage = packet["strategy"]["conditions"][role]
    expected = PUBLIC_PATHS.replace("账户约束", "策略资金约束").replace(
        "不编造买入价、保护价或仓位额度", "策略禁止编造买入价、保护价或仓位额度")
    assert passage["text"] == expected
    assert passage["start_line"] == 5
    assert passage["source_section"] == "final_trade_decision"
    assert "转换" in passage["projection"]
    assert "账户约束" not in json.dumps(packet, ensure_ascii=False)
    assert not packet["strategy"]["entry_candidate_permitted"]
    assert doc == before
    # Public projection must never be accepted as a substitute source quotation.
    setattr(req, role, expected)
    with pytest.raises(ExperimentError, match="逐字一致"):
        prepare_strategy(doc, req, date.fromisoformat(doc["analysis_date"]))


@pytest.mark.parametrize("term", ["账户约束", "账户硬约束", "账户风险约束"])
def test_public_constraints_keep_exit_and_unresolved_dependencies(term):
    from app.llm.jev_decisions import public_decision_passage
    text = f"若{term}触发则按原规则退出；原规则未知，不编造卖出股数。"
    safe, changed = public_decision_passage(text)
    assert changed and safe == text.replace("账户", "策略资金")


@pytest.mark.parametrize("private", [
    "当前持仓300股", "持仓：三百股", "持仓成本7.2元", "账户余额100000元",
    "账户约束：余额为100000元", "账户硬约束：金额100000元",
    "我的持有比例20%", "账户状态：HELD", "账号123456", "API key: secret",
    "Bearer token", "内网192.168.1.17", "https://example.test/data", "contact@example.com",
])
def test_public_prohibition_does_not_bless_mixed_private_material(private):
    from app.llm.jev_decisions import public_decision_passage
    text = "不编造买入价、保护价或仓位额度；" + private
    with pytest.raises(ExperimentError): public_decision_passage(text)


def test_projection_crlf_and_rejection_role_are_explicit():
    from app.llm.jev_decisions import public_decision_passage
    text = "仅在条件确认后研究。\r\n未授权，不自动买入。"
    assert public_decision_passage(text) == (text.replace("\r\n", "\n"), True)
    doc = document(); req = strategy(); req.exit = "成本价20元"
    doc["reports"]["final_trade_decision"] += "\n" + req.exit
    with pytest.raises(ExperimentError, match="退出："):
        prepare_strategy(doc, req, date.fromisoformat(doc["analysis_date"]))


@pytest.mark.parametrize("change", ["unconfirmed", "expired", "future_report", "horizon_expiry", "unknown_horizon"])
def test_strategy_boundary_checks(change):
    today = date(2026, 10, 19); doc = document(today); req = strategy(today)
    if change == "unconfirmed": req.complete_conditions_confirmed = False
    if change == "expired": req.valid_until = today-timedelta(days=1)
    if change == "future_report": doc["analysis_date"] = (today+timedelta(days=1)).isoformat()
    if change == "horizon_expiry": req.valid_until = today+timedelta(days=100)
    if change == "unknown_horizon": doc["analysis_horizon"] = "unknown"
    with pytest.raises(ExperimentError): prepare_strategy(doc, req, today)


def test_calendar_holes_duplicates_and_intraday_are_not_completed_sessions():
    now = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
    cal = calendar(now); cal[0]["is_open"] = 1
    assert closed_sessions(cal, now)[-1] == "2026-10-18"
    assert closed_sessions(cal, now+timedelta(hours=2))[-1] == "2026-10-19"
    assert closed_sessions(cal[1:], now) == []
    assert closed_sessions(cal+[{"cal_date": "2026-10-18", "is_open": 0}], now) == []


@pytest.mark.parametrize("bad", ["missing", "duplicate", "qfq", "unit", "source", "unknown_source", "weekly", "mixed", "zero", "nan", "ohlc", "partial"])
def test_daily_state_abstains_on_unproven_data(bad):
    now = datetime.now(timezone.utc); days = closed_sessions(calendar(now), now)
    data = rows(days); quality = {"status": "success"}
    if bad == "missing": data.pop()
    if bad == "duplicate": data.append(deepcopy(data[-1]))
    if bad == "qfq": data[-1]["price_basis"] = "qfq"
    if bad == "unit": data[-1]["volume_unit"] = None
    if bad == "source": data[-1]["data_source"] = "https://secret"
    if bad == "unknown_source": data[-1]["data_source"] = "unknown"
    if bad == "weekly": data[-1]["period"] = "weekly"
    if bad == "mixed": data[-1]["data_source"] = "another"
    if bad == "zero": data[-1]["volume"] = 0
    if bad == "nan": data[-1]["close"] = float("nan")
    if bad == "ohlc": data[-1]["low"] = 20
    if bad == "partial": quality["status"] = "partial"
    snapshot, guards = daily_snapshot({"data": data, "quality": quality}, "000001", days)
    assert not snapshot and guards


def test_snapshot_projection_is_minimal_and_not_a_live_quote():
    now = datetime.now(timezone.utc); days = closed_sessions(calendar(now), now)
    data = rows(days); data[-1]["private_note"] = "must-not-send"
    snapshot, guards = daily_snapshot({"data": data, "quality": {"status": "success"}}, "000001", days)
    assert not guards and snapshot["as_of"] == days[-1]
    assert snapshot["kind"] == "closed_daily_bars_not_live_quote"
    assert "must-not-send" not in json.dumps(snapshot)


def test_multiple_sources_choose_one_full_series_without_splicing():
    now = datetime.now(timezone.utc); days = closed_sessions(calendar(now), now)
    data = rows(days); second = [{**row, "data_source": "another"} for row in rows(days)]
    snapshot, guards = daily_snapshot({"data": data+second, "quality": {"status": "success"}}, "000001", days)
    assert not guards and len({b["data_source"] for b in snapshot["bars"]}) == 1
    second[-1]["close"] = 10.9
    assert daily_snapshot({"data": data+second, "quality": {"status": "success"}}, "000001", days)[1] == ["DAILY_SOURCE_CONFLICT"]


@pytest.mark.parametrize("args", [("ENTRY_CANDIDATE", "HOLD_CANDIDATE", "INSUFFICIENT", True),
    ("WAIT", "HOLD_CANDIDATE", "INSUFFICIENT", False), ("WAIT", "EXIT_REVIEW", "WITHIN_CONDITIONS", False),
    ("WAIT", "RESEARCH_REQUIRED", "WITHIN_CONDITIONS", False)])
def test_inconsistent_model_choices_rejected(args):
    from app.llm.jev_review import JevReviewError
    with pytest.raises(JevReviewError): checked_decision(response(*args), "jev-1.13.0", args[-1])


def test_model_contract_and_rule_baseline_not_fake_probability():
    value = checked_decision(response(), "jev-1.13.0", False)
    assert set(value["result"]) == {"entry_action", "existing_action", "risk_state"}
    assert "confidence" not in json.dumps(value)
    from app.llm.jev_review import JevReviewError
    with pytest.raises(JevReviewError): checked_decision(response(), "jev-1.12.0", False)
    extra = response(); extra["answers"]["free_text"] = "anything"
    with pytest.raises(JevReviewError): checked_decision(extra, "jev-1.13.0", False)


def test_entry_candidate_is_available_only_under_explicit_hard_permission():
    from app.llm.jev_review import JevReviewError
    candidate = response("ENTRY_CANDIDATE", "HOLD_CANDIDATE", "WITHIN_CONDITIONS", True)
    assert checked_decision(candidate, "jev-1.13.0", True)["result"]["entry_action"] == "ENTRY_CANDIDATE"
    assert "ENTRY_CANDIDATE" not in decision_questions(False)["entry_action"]["criteria"]
    with pytest.raises(JevReviewError): checked_decision(candidate, "jev-1.13.0", False)


@pytest.mark.asyncio
async def test_transport_disabled_has_no_network():
    from app.llm.jev_review import JevReviewError
    with pytest.raises(JevReviewError, match="disabled"):
        await evaluate_decision(ConditionConfig(enabled=True, external_calls_enabled=True), {})


def test_new_switch_requires_separate_consent_and_old_clients_preserve_it(tmp_path):
    path = tmp_path / "conditions.json"; old = tmp_path / "review.json"
    state = get_condition_settings(path, old)
    update = dict(enabled=False, external_calls_enabled=False, max_calls_per_day=5, revision=state["revision"], decision_experiments_enabled=True)
    with pytest.raises(SettingsRejected): update_condition_settings(ConditionSettingsUpdate(**update), path, old)
    state = update_condition_settings(ConditionSettingsUpdate(**update, confirm_external=True), path, old)
    assert state["decision_experiments_enabled"]
    state = update_condition_settings(ConditionSettingsUpdate(enabled=False, external_calls_enabled=False,
        max_calls_per_day=4, revision=state["revision"]), path, old)
    assert state["decision_experiments_enabled"]


@pytest.mark.asyncio
async def test_real_preview_data_path_ownership_and_no_report_mutation(real_mongo_db):
    from app.data.storage.mongo.collections import get_collection_name
    from app.data.core.interface import DataInterface
    db = real_mongo_db
    from isolated_database import require_isolated_database
    require_isolated_database()
    now = datetime.now(timezone.utc); cal = calendar(now); days = closed_sessions(cal, now)
    calendar_coll = db[get_collection_name("trade_calendar", "CN")]
    quotes_coll = db[get_collection_name("daily_quotes", "CN")]
    assert await calendar_coll.count_documents({}) == 0
    assert await quotes_coll.count_documents({"symbol": "000001"}) == 0
    cal_ids = (await calendar_coll.insert_many(cal)).inserted_ids
    quote_ids = (await quotes_coll.insert_many(rows(days))).inserted_ids
    doc = document(); doc["reports"]["final_trade_decision"] += "\n" + PUBLIC_PATHS
    req = strategy(); req.veto = PUBLIC_PATHS
    rid = (await db.analysis_reports.insert_one(doc)).inserted_id
    before = await db.analysis_reports.find_one({"_id": rid})
    DataInterface.reset_instance()
    try:
        preview = await preview_experiment(doc["analysis_id"], doc["user_id"], req, db=db)
        assert preview["status"] == "preview", preview
        assert "策略资金约束" in preview["packet"]["strategy"]["conditions"]["veto"]["text"]
        assert "转换" in preview["packet"]["strategy"]["conditions"]["veto"]["projection"]
        assert preview["packet"]["market_state"]["as_of"] == days[-1]
        assert not preview["external_call_attempted"] and not preview["affects_decision"]
        with pytest.raises(ExperimentError): await get_experiments(doc["analysis_id"], "foreign", db=db)
        assert await get_experiments(doc["analysis_id"], doc["user_id"], db=db) == []
        with pytest.raises(ExperimentError): await run_experiment(doc["analysis_id"], "foreign", preview["id"], True, db=db)
        assert await db.analysis_reports.find_one({"_id": rid}) == before
        assert await db.jev_condition_budgets.count_documents({}) == 0
    finally:
        await calendar_coll.delete_many({"_id": {"$in": cal_ids}})
        await quotes_coll.delete_many({"_id": {"$in": quote_ids}})
        await db.analysis_reports.delete_one({"_id": rid})
        await db[PREVIEWS].delete_many({"analysis_id": doc["analysis_id"]})
        DataInterface.reset_instance()


@pytest.mark.asyncio
async def test_real_at_most_once_budget_expiry_and_revision_guards(real_mongo_db, tmp_path):
    db = real_mongo_db; assert "validation" in db.name
    now = datetime.now(timezone.utc); today = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    doc = document(today); rid = (await db.analysis_reports.insert_one(doc)).inserted_id
    before = await db.analysis_reports.find_one({"_id": rid})
    plan = prepare_strategy(doc, strategy(today), today)
    snapshot, guards = daily_snapshot({"data": rows(closed_sessions(calendar(now), now)), "quality": {"status": "success"}}, "000001", closed_sessions(calendar(now), now))
    assert not guards
    preview = {"_id": uuid4().hex, "analysis_id": doc["analysis_id"], "user_id": doc["user_id"], "status": "preview", "policy": POLICY,
        "packet": make_packet(plan, snapshot), "source_revision": source_revision(doc), "created_at": now,
        "expires_at": now+timedelta(minutes=5), "external_call_attempted": False, "affects_decision": False}
    await db[PREVIEWS].insert_one(preview)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"enabled": True, "external_calls_enabled": True, "decision_experiments_enabled": True,
        "max_calls_per_day": 1, "api_key": "must-never-send"}))
    key = today.isoformat()
    assert await db.jev_condition_budgets.count_documents({"_id": key}) == 0
    await db.jev_condition_budgets.insert_one({"_id": key, "attempts": 1})
    try:
        await db[PREVIEWS].update_one({"_id": preview["_id"]}, {"$set": {"policy": "jev-daily-decision-shadow-v1"}})
        with pytest.raises(ExperimentError, match="规则已更新"):
            await run_experiment(doc["analysis_id"], doc["user_id"], preview["_id"], True, db=db, config_path=config)
        assert await db[RUNS].count_documents({"analysis_id": doc["analysis_id"]}) == 0
        await db[PREVIEWS].update_one({"_id": preview["_id"]}, {"$set": {"policy": POLICY}})
        with pytest.raises(ExperimentError): await run_experiment(doc["analysis_id"], doc["user_id"], preview["_id"], False, db=db, config_path=config)
        results = await asyncio.gather(*(run_experiment(doc["analysis_id"], doc["user_id"], preview["_id"], True, db=db, config_path=config) for _ in range(4)))
        assert len({r["id"] for r in results}) == 1
        history = await get_experiments(doc["analysis_id"], doc["user_id"], db=db)
        assert len(history) == 1 and history[0]["status"] == "not_called"
        assert not history[0]["external_call_attempted"]
        assert (await db.jev_condition_budgets.find_one({"_id": key}))["attempts"] == 1
        assert await db.analysis_reports.find_one({"_id": rid}) == before
        await db[PREVIEWS].update_one({"_id": preview["_id"]}, {"$set": {"packet.strategy.valid_until": (today-timedelta(days=1)).isoformat()}})
        with pytest.raises(ExperimentError, match="有效期已过"): await run_experiment(doc["analysis_id"], doc["user_id"], preview["_id"], True, db=db, config_path=config)
        await db[PREVIEWS].update_one({"_id": preview["_id"]}, {"$set": {"packet.strategy.valid_until": plan["valid_until"]}})
        await db.analysis_reports.update_one({"_id": rid}, {"$set": {"decision.action": "BUY"}})
        with pytest.raises(ExperimentError, match="变化"): await run_experiment(doc["analysis_id"], doc["user_id"], preview["_id"], True, db=db, config_path=config)
        await db[PREVIEWS].update_one({"_id": preview["_id"]}, {"$set": {"expires_at": now-timedelta(seconds=1)}})
        with pytest.raises(ExperimentError, match="过期"): await run_experiment(doc["analysis_id"], doc["user_id"], preview["_id"], True, db=db, config_path=config)
    finally:
        await db[RUNS].delete_many({"analysis_id": doc["analysis_id"]})
        await db[PREVIEWS].delete_one({"_id": preview["_id"]})
        await db.analysis_reports.delete_one({"_id": rid})
        await db.jev_condition_budgets.delete_one({"_id": key})
