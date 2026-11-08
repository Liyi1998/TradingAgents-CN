"""Real pure contracts/filesystem/Mongo lifecycle; no live model or mocks."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest
from pydantic import ValidationError

from app.llm.jev_conditions import ConditionConfig
from app.llm.jev_final_action import build_final_packet, checked_action, action_questions, evaluate_final_action
from app.llm.jev_review import JevReviewError
from app.services.jev_final_action_service import COLLECTION, dispatch_jev_after_report, get_final_action
from app.services.jev_condition_config_service import ConditionSettingsUpdate, SettingsRejected, get_condition_settings, update_condition_settings
from tests.data.test_jev_conditions import source_document


def response(unheld="WAIT", held="RESEARCH_REQUIRED", evidence="INSUFFICIENT"):
    fields = dict(unheld_action=unheld, held_action=held, evidence_state=evidence)
    return {"model": "jev-1.13.0", "answers": {k: {"type": "choice", "choice": fields[k],
        "confidence": 1., "probabilities": {c: float(c == fields[k]) for c in q["criteria"]}}
        for k, q in action_questions().items()}}


@pytest.mark.parametrize("horizon", ["short", "medium", "long"])
def test_complete_report_plan_and_dated_evidence_no_manual_selection(horizon):
    doc = source_document(); doc["analysis_horizon"] = horizon
    before = deepcopy(doc)
    packet, guards = build_final_packet(doc)
    assert not guards
    assert packet["plan"] == doc["reports"]["final_trade_decision"]
    assert packet["analysis_horizon"] == horizon
    assert "50.89" in packet["evidence"]["market_report"]
    assert packet["evidence_quality"]["fundamentals_report"] == "missing"
    assert not packet["new_exposure_permitted"]
    assert doc == before


@pytest.mark.parametrize("private", ["当前持仓20600股，成本价3.613元", "账户余额88888元", "API key: credential", "内网192.168.1.17", "https://private.test/credential"])
def test_private_and_mixed_prerequisites_never_leak(private):
    doc = source_document(); doc["account_position"] = {"cost": 777.7}
    doc["reports"]["final_trade_decision"] += "\n若"+private+"，则退出。"
    packet, guards = build_final_packet(doc)
    assert "PLAN_CONSTRAINTS_REDACTED" in guards
    text = json.dumps(packet, ensure_ascii=False)
    for value in ("20600", "3.613", "88888", "credential", "192.168", "777.7"):
        assert value not in text


@pytest.mark.parametrize("value", [0, None, True, "5", -1, 101, float("inf"), float("nan")])
def test_unproven_or_invalid_caps_never_authorize_new_exposure(value):
    doc = source_document(); doc["decision"].update(current_entry_limit_pct=value,
        position_evidence={"current_entry_limit_pct": {"status": "explicit"}})
    assert not build_final_packet(doc)[0]["new_exposure_permitted"]


def test_partial_evidence_is_marked_not_silently_complete():
    doc = source_document(); doc["reports"]["market_report"] = "公开行情"*2000
    packet, guards = build_final_packet(doc)
    assert not guards and packet["evidence_quality"]["market_report"] == "partial"
    doc["reports"]["final_trade_decision"] *= 2000
    assert "PLAN_TOO_LARGE" in build_final_packet(doc)[1]


@pytest.mark.parametrize("held", ["ADD", "HOLD", "REDUCE", "SELL"])
def test_valid_actions_and_local_ceiling(held):
    row = checked_action(response("BUY", held, "SUPPORTED"), "jev-1.13.0", False)
    assert row["suggested"]["unheld_action"] == "BUY"
    assert row["effective"]["unheld_action"] == "WAIT"
    assert row["effective"]["held_action"] == ("RESEARCH_REQUIRED" if held == "ADD" else held)
    assert "confidence" not in json.dumps(row)
    allowed = checked_action(response("BUY", held, "SUPPORTED"), "jev-1.13.0", True)
    assert allowed["effective"] == allowed["suggested"]


@pytest.mark.parametrize("state", ["INSUFFICIENT", "CONFLICT"])
def test_uncertainty_is_not_default_hold_or_action(state):
    row = checked_action(response("BUY", "SELL", state), "jev-1.13.0", True)
    assert row["effective"]["unheld_action"] == row["effective"]["held_action"] == "RESEARCH_REQUIRED"


def test_strict_schema_and_explicit_configuration_consent(tmp_path):
    assert not ConditionConfig().final_action_enabled
    with pytest.raises(ValidationError): ConditionConfig(final_action_enabled="true")
    with pytest.raises(JevReviewError): checked_action(response(), "wrong-model", True)
    bad = response(); bad["answers"]["account"] = {}
    with pytest.raises(JevReviewError): checked_action(bad, "jev-1.13.0", True)
    path=tmp_path/'conditions.json'; review=tmp_path/'review.json'
    state=get_condition_settings(path, review)
    values=dict(enabled=False, external_calls_enabled=False, max_calls_per_day=5, revision=state['revision'], final_action_enabled=True)
    with pytest.raises(SettingsRejected): update_condition_settings(ConditionSettingsUpdate(**values), path, review)
    state=update_condition_settings(ConditionSettingsUpdate(**values, confirm_external=True), path, review)
    assert state['final_action_enabled']
    state=update_condition_settings(ConditionSettingsUpdate(enabled=False, external_calls_enabled=False, max_calls_per_day=5, revision=state['revision']), path, review)
    assert state['final_action_enabled']  # old clients preserve new switch


@pytest.mark.asyncio
async def test_disabled_transport_has_no_network():
    with pytest.raises(JevReviewError, match="disabled"):
        await evaluate_final_action(ConditionConfig(enabled=True, external_calls_enabled=True), {})


@pytest.mark.asyncio
async def test_real_dispatch_only_one_path_owner_readonly_and_dedup(real_mongo_db, tmp_path):
    db=real_mongo_db; assert 'validation' in db.name
    doc=source_document(); rid=(await db.analysis_reports.insert_one(doc)).inserted_id
    before=await db.analysis_reports.find_one({'_id':rid})
    path=tmp_path/'config.json'; path.write_text(json.dumps({'enabled':True,'final_action_enabled':True}))
    try:
        ids=await asyncio.gather(*(dispatch_jev_after_report(db,rid,config_path=path) for _ in range(3)))
        assert len(set(ids))==1
        assert await db[COLLECTION].count_documents({'analysis_id':doc['analysis_id']})==1
        assert await db.jev_condition_checks.count_documents({'analysis_id':doc['analysis_id']})==0
        row=await get_final_action(doc['analysis_id'],doc['user_id'],db=db)
        assert row['status']=='not_called' and row['reason']=='EXTERNAL_DISABLED'
        assert not row['external_call_attempted'] and not row['affects_decision']
        assert 'packet' not in row and 'user_id' not in row
        assert await get_final_action(doc['analysis_id'],'foreign',db=db) is None
        assert await get_final_action(doc['analysis_id'],doc['user_id'],db=db)==row
        assert await db.analysis_reports.find_one({'_id':rid})==before
        assert await db.jev_condition_budgets.count_documents({})==0
        await db[COLLECTION].update_one({'_id':ids[0]},{'$set':{'status':'running','deadline_at':datetime.now(timezone.utc)-timedelta(seconds=1)}})
        assert (await get_final_action(doc['analysis_id'],doc['user_id'],db=db))['status']=='incomplete'
        assert (await db[COLLECTION].find_one({'_id':ids[0]}))['status']=='running'
    finally:
        await db[COLLECTION].delete_many({'analysis_id':doc['analysis_id']})
        await db.analysis_reports.delete_one({'_id':rid})


@pytest.mark.asyncio
@pytest.mark.parametrize('horizon',['short','medium','long'])
async def test_actual_save_hook_automatic_without_selections(real_mongo_db,horizon):
    from app.llm.jev_conditions import CONFIG_PATH
    from app.services.analysis_service import AnalysisService
    from app.services.jev_final_action_service import _tasks
    db=real_mongo_db; assert 'validation' in db.name
    assert not CONFIG_PATH.exists()
    doc=source_document(); doc.update(analysis_horizon=horizon,stock_name='隔离自动建议验收',task_id=doc['analysis_id'])
    task_id=doc['task_id']
    await db.analysis_tasks.insert_one({'task_id':task_id,'analysis_horizon':horizon,'user_id':doc['user_id']})
    CONFIG_PATH.write_text(json.dumps({'enabled':True,'final_action_enabled':True}))
    service=AnalysisService()
    try:
        await service._save_analysis_result_web_style(task_id,doc,doc['user_id'])
        assert _tasks
        report=await db.analysis_reports.find_one({'analysis_id':doc['analysis_id']})
        task=await db.analysis_tasks.find_one({'task_id':task_id})
        await asyncio.gather(*list(_tasks))
        row=await get_final_action(doc['analysis_id'],doc['user_id'],db=db)
        assert row['status']=='skipped' and row['input_guards']==['PARTITION_INCOMPLETE']
        assert await db.jev_condition_checks.count_documents({'analysis_id':doc['analysis_id']})==0
        assert await db.analysis_reports.find_one({'_id':report['_id']})==report
        assert await db.analysis_tasks.find_one({'task_id':task_id})==task
    finally:
        if _tasks: await asyncio.gather(*list(_tasks))
        CONFIG_PATH.unlink(); service._shutdown_pool(wait=True)
        await db[COLLECTION].delete_many({'analysis_id':doc['analysis_id']})
        await db.analysis_reports.delete_many({'analysis_id':doc['analysis_id']})
        await db.analysis_tasks.delete_many({'task_id':task_id})


@pytest.mark.asyncio
async def test_real_enabled_but_exhausted_budget_never_calls(real_mongo_db,tmp_path):
    from zoneinfo import ZoneInfo
    db=real_mongo_db; assert 'validation' in db.name
    key=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    assert await db.jev_condition_budgets.count_documents({})==0
    doc=source_document(); rid=(await db.analysis_reports.insert_one(doc)).inserted_id
    path=tmp_path/'enabled.json'
    path.write_text(json.dumps({'enabled':True,'external_calls_enabled':True,'final_action_enabled':True,'max_calls_per_day':1,'api_key':'must-not-send'}))
    await db.jev_condition_budgets.insert_one({'_id':key,'attempts':1})
    try:
        await dispatch_jev_after_report(db,rid,config_path=path)
        row=await get_final_action(doc['analysis_id'],doc['user_id'],db=db)
        assert row['reason']=='DAILY_LIMIT' and not row['external_call_attempted']
        assert (await db.jev_condition_budgets.find_one({'_id':key}))['attempts']==1
        assert await db.jev_condition_checks.count_documents({'analysis_id':doc['analysis_id']})==0
    finally:
        await db.jev_condition_budgets.delete_one({'_id':key})
        await db[COLLECTION].delete_many({'analysis_id':doc['analysis_id']})
        await db.analysis_reports.delete_one({'_id':rid})
