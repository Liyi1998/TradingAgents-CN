"""Real Mongo flows for immutable research baselines, ownership and races."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.engine.research_tracking import digest
from app.services.research_tracking import BASELINES, BUDGETS, RUNS, STEPS, TRACKS, VERSIONS, ResearchTrackingService, TrackingError
from tests.data.test_research_tracking import report, snapshot


def full_report(owner, day, hour=7, price=7.1, **patch):
    row = report(owner, '000503')
    row.update(analysis_date=day, created_at=datetime.fromisoformat(day).replace(hour=hour, tzinfo=timezone.utc),
        reports={'final_trade_decision': f'## 关键价格点位（人民币，不复权）\n- 保护止损位：{price:.2f} 元（仍需核对退出条件）'}, **patch)
    return row


@pytest.fixture
async def baseline_flow(real_mongo_db):
    db = real_mongo_db; owner = 'baseline-test-' + uuid4().hex
    service = ResearchTrackingService(db)
    old = full_report(owner, '2026-10-26')
    await db.analysis_reports.insert_one(deepcopy(old))
    made = await service.create('000503', owner)
    track = await service.owned(made['id'], owner)
    new = full_report(owner, '2026-10-27', price=6.8)
    await db.analysis_reports.insert_one(deepcopy(new))
    try:
        yield db, service, owner, track, old, new
    finally:
        for name in ['analysis_reports', BASELINES, TRACKS, VERSIONS, RUNS, STEPS]:
            await db[name].delete_many({'user_id': owner})
        await db[BUDGETS].delete_many({'_id': {'$regex': owner}})


@pytest.mark.asyncio
async def test_switch_keeps_old_versions_quotes_and_baseline_binding(baseline_flow):
    db, service, owner, track, old, new = baseline_flow
    snap = snapshot()
    version = {'_id': uuid4().hex, 'track_id': track['_id'], 'user_id': owner, 'as_of': '2026-10-26',
        'baseline_analysis_id': old['analysis_id'], 'created_at': datetime(2026, 10, 27, 5, tzinfo=timezone.utc),
        'status': 'partial', 'snapshot': snap, 'review': None, 'modules': {}, 'errors': ['旧复核未完成']}
    await db[VERSIONS].insert_one(deepcopy(version))
    await db[TRACKS].update_one({'_id': track['_id']}, {'$set': {'latest_version_id': version['_id'], 'settings.auto_enabled': True}})
    queued = await service.enqueue(track['_id'], owner)
    run = await db[RUNS].find_one({'_id': queued['id']})
    await service.finish(run, 'partial', '旧任务'); await service.release(run)
    immutable = {name: digest(await db[name].find({'user_id': owner}).to_list(100)) for name in ['analysis_reports', VERSIONS, RUNS, STEPS]}
    before = await service.board('000503', owner)
    assert before['newer_report']['analysis_id'] == new['analysis_id']
    assert before['baseline']['as_of'] == '2026-10-26'
    await service.switch_baseline(track['_id'], owner, new['analysis_id'], old['analysis_id'])
    current = await service.board('000503', owner)
    assert current['track']['baseline_analysis_id'] == new['analysis_id']
    assert current['track']['settings'] == {'auto_enabled': True, 'max_calls_per_day': 6, 'intraday_enabled': True, 'structure_auxiliary_enabled': False}
    assert current['version'] is None and current['run'] is None and current['versions'] == []
    assert current['newer_report'] is None
    assert [b['analysis_id'] for b in current['baselines']] == [new['analysis_id'], old['analysis_id']]
    assert current['baseline']['levels'][0]['lower'] == 6.8
    archived = await service.board('000503', owner, baseline_analysis_id=old['analysis_id'])
    assert archived['version']['id'] == version['_id']
    assert archived['baseline']['analysis_id'] == old['analysis_id']
    assert archived['baseline']['levels'][0]['lower'] == 7.1
    assert archived['baseline']['levels'][0]['source_date'] == '2026-10-26'
    direct = await service.board('000503', owner, version_id=version['_id'])
    assert direct['baseline'] == archived['baseline']
    for name, checksum in immutable.items():
        assert digest(await db[name].find({'user_id': owner}).to_list(100)) == checksum
    assert await db[BUDGETS].count_documents({'_id': {'$regex': owner}}) == 0
    # New work uses the new baseline and starts with no inherited daily version.
    next_run = await service.enqueue(track['_id'], owner)
    assert next_run['baseline_analysis_id'] == new['analysis_id']
    await service.release(await db[RUNS].find_one({'_id': next_run['id']}))


@pytest.mark.asyncio
async def test_same_day_new_report_and_all_report_eligibility_checks(baseline_flow):
    db, service, owner, track, old, new = baseline_flow
    await service.switch_baseline(track['_id'], owner, new['analysis_id'], old['analysis_id'])
    later = full_report(owner, '2026-10-27', hour=9)
    await db.analysis_reports.insert_one(deepcopy(later))
    assert (await service.board('000503', owner))['newer_report']['analysis_id'] == later['analysis_id']
    for patch in [{'status': 'failed'}, {'analysis_horizon': 'long'}, {'stock_symbol': '603232'}, {'market_type': '港股'}]:
        invalid = full_report(owner, '2026-10-28', **patch)
        await db.analysis_reports.insert_one(deepcopy(invalid))
        with pytest.raises(TrackingError, match='同一股票'):
            await service.switch_baseline(track['_id'], owner, invalid['analysis_id'], new['analysis_id'])
    assert (await service.board('000503', owner))['newer_report']['analysis_id'] == later['analysis_id']
    with pytest.raises(TrackingError, match='无权'):
        await service.switch_baseline(track['_id'], 'someone-else', later['analysis_id'], new['analysis_id'])
    with pytest.raises(TrackingError, match='更新的完整报告'):
        await service.switch_baseline(track['_id'], owner, old['analysis_id'], new['analysis_id'])


@pytest.mark.asyncio
async def test_active_update_blocks_switch_then_duplicate_requests_are_idempotent(baseline_flow):
    db, service, owner, track, old, new = baseline_flow
    queued = await service.enqueue(track['_id'], owner)
    run = await db[RUNS].find_one({'_id': queued['id']})
    with pytest.raises(TrackingError, match='正在执行'):
        await service.switch_baseline(track['_id'], owner, new['analysis_id'], old['analysis_id'])
    assert await db[BASELINES].count_documents({'user_id': owner}) == 0
    await service.release(run)
    results = await asyncio.gather(*(service.switch_baseline(track['_id'], owner, new['analysis_id'], old['analysis_id']) for _ in range(2)))
    assert all(r['baseline_analysis_id'] == new['analysis_id'] for r in results)
    assert len((await service.owned(track['_id'], owner))['baseline_history']) == 1
    assert await db[BASELINES].count_documents({'user_id': owner}) == 1
    assert await db[BUDGETS].count_documents({'_id': {'$regex': owner}}) == 0


@pytest.mark.asyncio
async def test_two_different_switches_cannot_overwrite_each_other_or_reuse_old_lease(baseline_flow):
    db, service, owner, track, old, new = baseline_flow
    newer = full_report(owner, '2026-10-27', hour=10, price=6.6)
    await db.analysis_reports.insert_one(deepcopy(newer))
    # Expired work may remain in a dying process; it must lose its lease.
    await db[TRACKS].update_one({'_id': track['_id']}, {'$set': {'lease_until': datetime.now(timezone.utc)-timedelta(minutes=1), 'lease_token': 'expired'}})
    results = await asyncio.gather(*(service.switch_baseline(track['_id'], owner, target['analysis_id'], old['analysis_id']) for target in [new, newer]), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, TrackingError) for r in results) == 1
    changed = await service.owned(track['_id'], owner)
    assert len(changed['baseline_history']) == 1 and 'lease_token' not in changed
    with pytest.raises(TrackingError, match='旧任务'):
        await service.reserve_step_call(track, {'lease_token': 'expired'}, uuid4().hex, 'review', 'unused')
    assert await db[BUDGETS].count_documents({'_id': {'$regex': owner}}) == 0


@pytest.mark.asyncio
async def test_legacy_version_without_baseline_id_stays_on_original_history(baseline_flow):
    db, service, owner, track, old, new = baseline_flow
    version = {'_id': uuid4().hex, 'track_id': track['_id'], 'user_id': owner, 'as_of': '2026-10-26',
        'created_at': datetime.now(timezone.utc), 'status': 'partial', 'review': None, 'snapshot': snapshot()}
    await db[VERSIONS].insert_one(deepcopy(version))
    await db[TRACKS].update_one({'_id': track['_id']}, {'$set': {'latest_version_id': version['_id']}})
    await service.switch_baseline(track['_id'], owner, new['analysis_id'], old['analysis_id'])
    assert (await service.board('000503', owner))['version'] is None
    assert (await service.board('000503', owner, version['_id']))['baseline']['analysis_id'] == old['analysis_id']
    with pytest.raises(TrackingError, match='不属于'):
        await service.board('000503', owner, version['_id'], new['analysis_id'])
    with pytest.raises(TrackingError, match='无权'):
        await service.board('000503', owner, baseline_analysis_id='another-owner-report')
