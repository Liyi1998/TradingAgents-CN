"""Real parser/filesystem/Mongo checks. No external model or fake HTTP client."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.llm.jev_conditions import (
    ConditionConfig, build_packet, checked_result, condition_questions,
    evaluate_conditions, load_condition_config,
)
from app.llm.jev_review import JevReviewError
from app.services.jev_condition_service import (
    COLLECTION, get_condition_check, record_condition_check,
    reserve_daily_call, schedule_condition_check, _tasks,
)


def source_document():
    return {"analysis_id": "shadow-test-" + uuid4().hex, "status": "completed",
        "user_id": "shadow-owner", "stock_symbol": "300124", "analysis_date": "2026-10-14",
        "analysis_horizon": "medium", "decision": {"action": "WATCH", "current_entry_limit_pct": 0},
        "reports": {"final_trade_decision": "未来1–3个月条件方案：49.24–49.95元承接确认，须盈利和回款证据支持；未授权买入。",
                    "market_report": "2026-10-13日线收盘50.89元，未验证当日行情，不能证明承接已确认。"}}


def response_document(status="insufficient_evidence", conflict="unknown", reason="MISSING_EVIDENCE"):
    choices = dict(condition_status=status, evidence_conflict=conflict, reason_codes=reason)
    return {"model": "jev-1.13.0", "answers": {k: {"type": "choice", "choice": choices[k],
        "confidence": 1.0, "probabilities": {v: float(v == choices[k]) for v in q["criteria"]}}
        for k, q in condition_questions().items()}, "usage": {"input_tokens": 2, "output_tokens": 3}}


def test_defaults_and_fixed_shadow_mode(tmp_path):
    config = load_condition_config(tmp_path / "absent.json")
    assert not config.enabled and not config.external_calls_enabled
    for bad in ({"mode": "enforce"}, {"enabled": "true"}, {"max_calls_per_day": True}, {"timeout_seconds": 100}):
        with pytest.raises(ValidationError):
            ConditionConfig(**bad)


def test_three_fields_are_typed_and_independent():
    result = checked_result(response_document(), "jev-1.13.0")
    assert result["result"] == {"condition_status": "insufficient_evidence", "evidence_conflict": "unknown", "reason_codes": ["MISSING_EVIDENCE"]}
    assert set(condition_questions()) == {"condition_status", "evidence_conflict", "reason_codes"}


@pytest.mark.parametrize("choices", [("met", "present", "SUPPORTED"), ("met", "not_evident", "MISSING_EVIDENCE"),
    ("not_met", "unknown", "SUPPORTED"), ("insufficient_evidence", "unknown", "SUPPORTED")])
def test_inconsistent_outputs_never_become_valid(choices):
    with pytest.raises(JevReviewError):
        checked_result(response_document(*choices), "jev-1.13.0")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, 2, True, "0.9"])
def test_invalid_probabilities_rejected(bad):
    value = response_document()
    value["answers"]["condition_status"]["confidence"] = bad
    with pytest.raises(JevReviewError):
        checked_result(value, "jev-1.13.0")


def test_extra_keys_and_model_drift_rejected():
    with pytest.raises(JevReviewError):
        checked_result(response_document(), "jev-1.12.0")
    value = response_document()
    value["answers"]["action"] = "BUY"
    with pytest.raises(JevReviewError):
        checked_result(value, "jev-1.13.0")


def test_public_projection_excludes_personal_data_and_preserves_source():
    doc = source_document()
    doc.update(api_key="credential", account_position={"shares": 20600, "cost": 3.613})
    doc["reports"]["final_trade_decision"] += "\n当前持仓20600股，成本3.613\nAPI key: credential\n内网192.168.1.17"
    doc["reports"]["private_report"] = "private-note"
    doc["structured_summary"] = {"analysis_summary": "private-summary"}
    original = deepcopy(doc)
    packet, guards = build_packet(doc)
    value = json.dumps(packet, ensure_ascii=False)
    for token in ("20600", "3.613", "credential", "192.168.1.17", "private-note", "private-summary", "user_id"):
        assert token not in value
    assert "49.24" in value and "REDACTED_INPUT" in guards
    assert doc == original


@pytest.mark.parametrize("change,guard", [({"stock_symbol": "unsafe\nsecret"}, "UNSUPPORTED_SYMBOL"),
    ({"analysis_date": "nonsense"}, "INVALID_ANALYSIS_DATE"), ({"analysis_horizon": "unknown"}, "UNKNOWN_HORIZON"),
    ({"reports": {}}, "MISSING_PLAN")])
def test_missing_inputs_are_explicit(change, guard):
    assert guard in build_packet({**source_document(), **change})[1]


def test_truncation_abstains():
    doc = source_document()
    doc["reports"]["market_report"] = "公开行情" * 2000
    assert "TRUNCATED_INPUT" in build_packet(doc)[1]


@pytest.mark.asyncio
async def test_transport_disabled_before_any_network():
    for cfg in (ConditionConfig(), ConditionConfig(enabled=True)):
        with pytest.raises(JevReviewError, match="disabled"):
            await evaluate_conditions(cfg, {})


@pytest.mark.asyncio
async def test_real_shadow_record_ownership_idempotency_and_no_report_mutation(real_mongo_db, tmp_path):
    db = real_mongo_db
    assert "validation" in db.name or "test" in db.name
    doc = source_document()
    saved = await db.analysis_reports.insert_one(doc)
    before = await db.analysis_reports.find_one({"_id": saved.inserted_id})
    config_path = tmp_path / "conditions.json"
    config_path.write_text(json.dumps({"enabled": True, "external_calls_enabled": False}))
    record_id = None
    try:
        assert await record_condition_check(db, saved.inserted_id, config_path=tmp_path / "absent") is None
        record_id = await record_condition_check(db, saved.inserted_id, config_path=config_path)
        row = await get_condition_check(db, doc["analysis_id"], "shadow-owner")
        assert row["status"] == "not_called" and row["reason"] == "external_calls_disabled"
        assert row["affects_decision"] is False and row["external_call_attempted"] is False
        assert row["original_reference"] == {"action": "WATCH"}
        assert row["rule_baseline"]["condition_status"] == "insufficient_evidence"
        assert await get_condition_check(db, doc["analysis_id"], "other-owner") is None
        assert await record_condition_check(db, saved.inserted_id, config_path=config_path) == record_id
        assert await db[COLLECTION].count_documents({"_id": record_id}) == 1
        assert await db.analysis_reports.find_one({"_id": saved.inserted_id}) == before
        await db[COLLECTION].update_one({"_id": record_id}, {"$set": {"status": "running", "deadline_at": datetime.now(timezone.utc)-timedelta(seconds=1)}})
        assert (await get_condition_check(db, doc["analysis_id"], "shadow-owner"))["status"] == "incomplete"
        assert (await db[COLLECTION].find_one({"_id": record_id}))["status"] == "running"
    finally:
        await db.analysis_reports.delete_one({"_id": saved.inserted_id})
        if record_id:
            await db[COLLECTION].delete_one({"_id": record_id})


@pytest.mark.asyncio
async def test_real_budget_is_atomic_and_fail_closed(real_mongo_db):
    from zoneinfo import ZoneInfo
    db = real_mongo_db
    assert "validation" in db.name or "test" in db.name
    key = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    assert await db.jev_condition_budgets.count_documents({"_id": key}) == 0
    try:
        reservations = await asyncio.gather(*(reserve_daily_call(db, 3) for _ in range(10)))
        assert sum(reservations) == 3
        assert (await db.jev_condition_budgets.find_one({"_id": key}))["attempts"] == 3
    finally:
        await db.jev_condition_budgets.delete_one({"_id": key})


@pytest.mark.asyncio
async def test_background_disabled_and_bad_config_never_escape(real_mongo_db, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"api_key":"must-not-leak", "mode":"enforce"}')
    schedule_condition_check(real_mongo_db, "absent", config_path=bad)
    schedule_condition_check(real_mongo_db, "absent", config_path=tmp_path / "absent")
    await asyncio.gather(*list(_tasks))
    assert not _tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("horizon", ["short", "medium", "long"])
async def test_actual_report_save_hook_records_separately(real_mongo_db, horizon):
    from app.llm.jev_conditions import CONFIG_PATH
    from app.services.analysis_service import AnalysisService
    db = real_mongo_db
    assert "validation" in db.name or "test" in db.name
    assert not CONFIG_PATH.exists(), "Never replace an operator's config"
    task_id = "shadow-save-" + uuid4().hex
    doc = source_document()
    doc.update(analysis_horizon=horizon, stock_name="验收样例", task_id=task_id)
    await db.analysis_tasks.insert_one({"task_id": task_id, "analysis_horizon": horizon, "user_id": "shadow-owner"})
    service = AnalysisService()
    CONFIG_PATH.write_text(json.dumps({"enabled": True, "external_calls_enabled": False}))
    try:
        await service._save_analysis_result_web_style(task_id, doc, "shadow-owner")
        assert _tasks  # Saver returned without waiting for the classifier.
        report = await db.analysis_reports.find_one({"analysis_id": doc["analysis_id"]})
        task = await db.analysis_tasks.find_one({"task_id": task_id})
        await asyncio.gather(*list(_tasks))
        check = await get_condition_check(db, doc["analysis_id"], "shadow-owner")
        assert check["mode"] == "shadow" and check["status"] == "not_called"
        assert check["input"]["analysis_horizon"] == horizon
        assert await db.analysis_reports.find_one({"_id": report["_id"]}) == report
        assert await db.analysis_tasks.find_one({"task_id": task_id}) == task
        assert "jev_condition_checks" not in report and "jev_review_report" not in report["reports"]
        assert report["reports"] == doc["reports"]
    finally:
        if _tasks:
            await asyncio.gather(*list(_tasks))
        CONFIG_PATH.unlink()
        service._shutdown_pool(wait=True)
        await db[COLLECTION].delete_many({"analysis_id": doc["analysis_id"]})
        await db.analysis_reports.delete_many({"analysis_id": doc["analysis_id"]})
        await db.analysis_tasks.delete_many({"task_id": task_id})


@pytest.mark.asyncio
async def test_redacted_input_never_sends_even_when_enabled(real_mongo_db, tmp_path):
    db = real_mongo_db
    doc = source_document()
    doc["reports"]["market_report"] += "\n我的持仓20600股"
    saved = await db.analysis_reports.insert_one(doc)
    config = tmp_path / "condition.json"
    config.write_text(json.dumps({"enabled": True, "external_calls_enabled": True, "api_key": "must-not-send"}))
    record_id = None
    try:
        record_id = await record_condition_check(db, saved.inserted_id, config_path=config)
        row = await db[COLLECTION].find_one({"_id": record_id})
        assert row["status"] == "skipped" and not row["external_call_attempted"]
        assert row["result"]["condition_status"] == "insufficient_evidence"
    finally:
        await db.analysis_reports.delete_one({"_id": saved.inserted_id})
        if record_id:
            await db[COLLECTION].delete_one({"_id": record_id})
