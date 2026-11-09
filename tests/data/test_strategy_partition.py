"""Real submission -> persisted source -> outbound contract; no mock/model calls."""
import asyncio
from copy import deepcopy
import json

import pytest

from app.engine.strategy_partition import MARKERS, partition_strategy, local_blocked_branches
from app.engine.orchestrator.workflow.submission import SubmissionBox, make_submit_report_tool, build_tool_description
from app.llm.jev_final_action import build_final_packet, checked_action
from app.services.jev_final_action_service import final_source_revision, get_final_action, COLLECTION
from tests.data.test_jev_conditions import source_document
from tests.data.test_jev_final_action import response


PUBLIC = "## 公开策略\n当前 WATCH，新增上限0%。\n完整收盘超过8.50元且回测有效、资金改善、经营证据可靠后，才重新研究；并非当前买入授权。\n跌破7.60元且反抽未收复时重评退出。\n重大经营风险否决新增；未知不得当作条件成立。"


def separated(public=PUBLIC, unheld="", held="既有止损规则尚未提供；不得用研究失效位替代。"):
    return f"{MARKERS[0]}\n{public}\n{MARKERS[1]}\n{unheld}\n{MARKERS[2]}\n{held}\n{MARKERS[3]}"


def doc_with_partition(text):
    doc = source_document()
    doc["reports"]["final_trade_decision"] = text
    doc["strategy_partition"] = partition_strategy(text)
    return doc


def test_lossless_partition_excludes_only_local_blocks_and_binds_all_source():
    text = separated(held="实际持仓20600股，成本3.613元；原止损规则未知。")
    doc = doc_with_partition(text); before = deepcopy(doc)
    packet, guards = build_final_packet(doc)
    assert not guards and packet["blocked_branches"] == ["held"]
    assert packet["plan"] == PUBLIC
    assert doc["strategy_partition"]["account_constraints"]["held"] in text
    for secret in ("20600", "3.613", "实际持仓", "原止损规则未知"):
        assert secret not in json.dumps(packet, ensure_ascii=False)
    assert doc == before


@pytest.mark.parametrize("transform", [
    lambda s: "不得买入\n"+s,
    lambda s: s+"\n另有重要否决条件",
    lambda s: s.replace(MARKERS[3], ""),
    lambda s: s.replace(MARKERS[2], MARKERS[1]),
    lambda s: s.replace(MARKERS[0], "```\n"+MARKERS[0]),
    lambda s: s.replace(PUBLIC, PUBLIC+"\n<!-- strategy:unexpected -->"),
    lambda s: s.replace(PUBLIC, ""),
    lambda s: s.replace(MARKERS[1], MARKERS[1]+"\n"+MARKERS[1]),
])
def test_incomplete_partition_never_silently_omits_constraints(transform):
    text=transform(separated())
    doc=doc_with_partition(text)
    assert build_final_packet(doc)[1] == ["PARTITION_INCOMPLETE"]
    assert doc['reports']['final_trade_decision']==text


@pytest.mark.parametrize("secret", ["实际持仓20600股", "成本价3.613元", "账户余额88888元", "API key: secret", "内网192.168.1.17"])
def test_private_public_misclassification_still_blocks(secret):
    packet, guards = build_final_packet(doc_with_partition(separated(PUBLIC+"\n若"+secret+"，则退出。")))
    assert "PLAN_CONSTRAINTS_REDACTED" in guards
    assert secret not in json.dumps(packet, ensure_ascii=False)


def test_citation_links_remain_local_without_losing_same_line_veto():
    text=separated(PUBLIC+'\n未核验公告前不得新增；来源：https://example.test/report?token=secret，正文证据未知。')
    doc=doc_with_partition(text)
    packet,guards=build_final_packet(doc)
    assert not guards
    assert '不得新增' in packet['plan'] and '正文证据未知' in packet['plan']
    assert 'http' not in packet['plan'] and 'token=secret' not in packet['plan']
    assert doc['reports']['final_trade_decision']==text


def test_shared_or_misfiled_account_rules_do_not_grant_independent_entry():
    doc=doc_with_partition(separated(held="新增额度须结合实际余额核对。"))
    assert "ACCOUNT_RULES_UNRESOLVED" in build_final_packet(doc)[1]
    assert local_blocked_branches(partition_strategy(separated(unheld="持仓集中度未知", held=""))) == ['held','unheld']


def test_source_and_partition_tampering_fails_not_legacy_fallback():
    doc=doc_with_partition(separated()); original=final_source_revision(doc)
    doc['strategy_partition']['account_constraints']['held']=''
    assert final_source_revision(doc)!=original
    assert build_final_packet(doc)[1]==['PARTITION_SOURCE_CHANGED']
    doc=doc_with_partition(separated());doc['reports']['final_trade_decision']+='\n新的否决'
    assert build_final_packet(doc)[1]==['PARTITION_SOURCE_CHANGED']


@pytest.mark.parametrize('held', ['ADD','HOLD','REDUCE','SELL','RESEARCH_REQUIRED'])
def test_local_unknown_held_rule_does_not_erase_supported_unheld_wait(held):
    row=checked_action(response('WAIT',held,'SUPPORTED'),'jev-1.13.0',False,['held'])
    assert row['effective']['unheld_action']=='WAIT'
    assert row['effective']['held_action']=='RESEARCH_REQUIRED'
    assert 'HELD_ACCOUNT_RULE_UNRESOLVED' in row['guards']
    assert row['suggested']['held_action']==held


def test_blocked_unheld_cannot_buy_but_independent_exit_is_retained():
    row=checked_action(response('BUY','SELL','SUPPORTED'),'jev-1.13.0',True,['unheld'])
    assert row['effective']['unheld_action']=='RESEARCH_REQUIRED'
    assert row['effective']['held_action']=='SELL'
    row=checked_action(response('BUY','RESEARCH_REQUIRED','SUPPORTED'),'jev-1.13.0',False,['held'])
    assert row['effective']['unheld_action']=='WAIT'  # Original cap still enforced.


def test_missing_contract_preserves_legacy_behavior_but_new_reports_mark_missing():
    doc=source_document()
    assert not build_final_packet(doc)[1]
    doc['strategy_partition']=partition_strategy(doc['reports']['final_trade_decision'])
    assert build_final_packet(doc)[1]==['PARTITION_INCOMPLETE']


@pytest.mark.asyncio
@pytest.mark.parametrize('horizon',['short','medium','long'])
@pytest.mark.parametrize('node_type',['trader','terminal'])
async def test_actual_submit_save_dispatch_no_original_mutation(real_mongo_db, horizon, node_type):
    from app.llm.jev_conditions import CONFIG_PATH
    from app.services.analysis_service import AnalysisService
    from app.services.jev_final_action_service import _tasks
    db=real_mongo_db;assert 'validation' in db.name
    assert not CONFIG_PATH.exists()
    text=separated(); state={'analysis_horizon':horizon}
    box=SubmissionBox()
    tool=make_submit_report_tool(box,node_type=node_type,report_key='final_trade_decision',state=state)
    assert MARKERS[0] in build_tool_description(node_type)
    assert MARKERS[0] not in build_tool_description('analyst')
    await tool.handler(content=text,action='观望')
    assert box.value.content==text and box.value.fields=={'action':'观望'}
    assert state['reports']['final_trade_decision']==text
    assert state['strategy_partitions']['final_trade_decision']==partition_strategy(text)
    doc=source_document();doc.update(analysis_horizon=horizon,task_id=doc['analysis_id'],state=state)
    doc['reports']['final_trade_decision']=text
    await db.analysis_tasks.insert_one({'task_id':doc['task_id'],'analysis_horizon':horizon,'user_id':doc['user_id']})
    CONFIG_PATH.write_text(json.dumps({'enabled':True,'final_action_enabled':True}))
    service=AnalysisService()
    try:
        await service._save_analysis_result_web_style(doc['task_id'],doc,doc['user_id'])
        saved=await db.analysis_reports.find_one({'analysis_id':doc['analysis_id']})
        assert saved['reports']['final_trade_decision']==text
        assert saved['strategy_partition']==partition_strategy(text)
        baseline=deepcopy(saved)
        await asyncio.gather(*list(_tasks))
        check=await get_final_action(doc['analysis_id'],doc['user_id'],db=db)
        assert check['status']=='not_called' and check['reason']=='EXTERNAL_DISABLED'
        assert await db.analysis_reports.find_one({'_id':saved['_id']})==baseline
        row=await db[COLLECTION].find_one({'analysis_id':doc['analysis_id']})
        assert row['packet']['plan']==PUBLIC and row['packet']['blocked_branches']==['held']
        assert not row['external_call_attempted']
    finally:
        if _tasks:await asyncio.gather(*list(_tasks))
        CONFIG_PATH.unlink();service._shutdown_pool(wait=True)
        await db[COLLECTION].delete_many({'analysis_id':doc['analysis_id']})
        await db.analysis_reports.delete_many({'analysis_id':doc['analysis_id']})
        await db.analysis_tasks.delete_many({'task_id':doc['task_id']})


@pytest.mark.asyncio
async def test_bad_partition_does_not_fail_submission():
    box=SubmissionBox();state={}
    text='原报告正文保留，分区格式未生成。'
    await make_submit_report_tool(box,node_type='terminal',state=state,report_key='final_trade_decision').handler(content=text,action='观望')
    assert box.value.content==text and state['strategy_partitions']['final_trade_decision']['status']=='missing'
