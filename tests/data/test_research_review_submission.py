"""Real tool execution and Mongo checkpoints; no model calls or mocked clients."""
from copy import deepcopy
from uuid import uuid4

import pytest

from app.engine.research_tracking import submission_contract, tool_schema, validate_update
from app.llm.core.types import ToolUseBlock
from app.llm.runner import _execute_partitioned
from app.llm.tools.registry import ToolRegistry
from app.models.research_tracking import DailyReview
from app.services.research_tracking import BUDGETS, RUNS, STEPS, TRACKS, ResearchTrackingService, TrackingError
from tests.data.test_research_tracking import review, snapshot


def test_review_contract_exposes_semantic_checks_and_missing_price_path():
    snap = snapshot()
    contract = submission_contract(snap, final=True)
    assert '2026-10-29' in contract and 'source_quote' in contract
    assert 'RESEARCH_REQUIRED' in contract and '"verified_close_available": true' in contract
    snap['price'] = None
    assert '"verified_close_available": false' in submission_contract(snap, final=True)
    row = review(); row['strategy'] = None
    output = validate_update(row, snap, final=True)
    assert output['content'] == row['content']
    assert output['strategy'] is None and output['unheld_action'] == 'RESEARCH_REQUIRED'
    schema = tool_schema(DailyReview)
    assert '$defs' not in schema and schema['additionalProperties'] is False


@pytest.mark.parametrize('change,error', [
    ({'source_quote': '10.00–10.50元，仅出现在旧报告中。'}, 'strategy.levels.0.source_quote'),
    ({'upper': 12.0}, '两个价格'),
])
def test_rejected_price_references_have_actionable_errors_without_relaxing_checks(change, error):
    row = review(); row['strategy']['levels'][0].update(change)
    with pytest.raises(ValueError, match=error):
        validate_update(row, snapshot(), final=True)


@pytest.mark.asyncio
async def test_real_rejected_tool_submission_can_be_corrected_and_persisted_once(real_mongo_db):
    db = real_mongo_db; user = 'review-fix-' + uuid4().hex; key = uuid4().hex
    service = ResearchTrackingService(db)
    track = {'_id': uuid4().hex, 'user_id': user, 'settings': {'auto_enabled': True, 'max_calls_per_day': 6},
             'baseline': {'decision': {'current_entry_limit_pct': 0}}}
    run = {'_id': uuid4().hex, 'user_id': user, 'automatic': False, 'model_calls': 0}
    await db[TRACKS].insert_one(deepcopy(track)); await db[RUNS].insert_one(deepcopy(run))
    tool, accepted, rejected = service.submission_tool(track, key, snapshot(), final=True)
    registry = ToolRegistry()

    async def submit(value):
        return await _execute_partitioned(registry, [ToolUseBlock(id=uuid4().hex, name=tool.name, input=value)],
            {tool.name: False}, extra_defs={tool.name: tool})

    try:
        await service.reserve_step_call(track, run, key, 'review', 'contract-test')
        bad = review(); bad['held_action'] = 'WATCH'
        result = await submit(bad)
        assert '工具执行失败' in result[0] and not accepted
        assert rejected == ['held_action:literal_error']
        saved = await db[STEPS].find_one({'_id': key})
        assert saved['status'] == 'running' and 'output' not in saved
        assert saved['validation_errors'] == rejected
        await service.reserve_step_call(track, run, key, 'review', 'contract-test', repair=True)
        valid = review(); valid.update(unheld_action='BUY', held_action='ADD')
        assert await submit(valid) == ['已保存，本模块完成']
        saved = await db[STEPS].find_one({'_id': key})
        assert saved['status'] == 'completed' and saved['attempts'] == 2 and saved['repair_calls'] == 1
        assert saved['output']['unheld_action'] == 'WATCH'
        assert saved['output']['held_action'] == 'RESEARCH_REQUIRED'
        assert (await db[RUNS].find_one({'_id': run['_id']}))['model_calls'] == 2
        assert (await db[BUDGETS].find_one({'_id': {'$regex': user}}))['count'] == 2
        assert '已提交' in (await submit(bad))[0]
        assert await db[STEPS].find_one({'_id': key}) == saved
    finally:
        for name in [TRACKS, RUNS, STEPS]:
            await db[name].delete_many({'user_id': user})
        await db[BUDGETS].delete_many({'_id': {'$regex': user}})


@pytest.mark.asyncio
async def test_schema_repair_counts_usage_beyond_old_limit_but_respects_disabled_automation(real_mongo_db):
    db = real_mongo_db; user = 'review-fix-' + uuid4().hex; key = uuid4().hex
    service = ResearchTrackingService(db)
    track = {'_id': uuid4().hex, 'user_id': user, 'settings': {'auto_enabled': True, 'max_calls_per_day': 1}}
    run = {'_id': uuid4().hex, 'user_id': user, 'automatic': True, 'model_calls': 0}
    await db[TRACKS].insert_one(deepcopy(track)); await db[RUNS].insert_one(deepcopy(run))
    try:
        await service.reserve_step_call(track, run, key, 'review', 'contract-test')
        await service.reserve_step_call(track, run, key, 'review', 'contract-test', repair=True)
        await db[TRACKS].update_one({'_id': track['_id']}, {'$set': {'settings.auto_enabled': False, 'settings.max_calls_per_day': 6}})
        with pytest.raises(TrackingError, match='自动更新已关闭'):
            await service.reserve_step_call(track, run, key, 'review', 'contract-test', repair=True)
        assert (await db[STEPS].find_one({'_id': key}))['attempts'] == 2
        assert (await db[RUNS].find_one({'_id': run['_id']}))['model_calls'] == 2
    finally:
        for name in [TRACKS, RUNS, STEPS]:
            await db[name].delete_many({'user_id': user})
        await db[BUDGETS].delete_many({'_id': {'$regex': user}})
