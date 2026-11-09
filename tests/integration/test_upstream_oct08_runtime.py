"""Actual Mongo/Redis validation. No mocks, no paid API requests."""
import asyncio
from datetime import datetime, timezone, timedelta
import os
import uuid

import pytest
import pytest_asyncio

EXPECTED_DB = 'tradingagents_latest_validation_20261008'

# Opt-in only: these tests write ephemeral records and require dedicated Redis.
pytestmark = pytest.mark.skipif(
    os.environ.get('LATEST_VALIDATION_DB') != EXPECTED_DB
    or os.environ.get('MONGODB_DATABASE') != EXPECTED_DB
    or os.environ.get('REDIS_HOST') != '127.0.0.1'
    or os.environ.get('REDIS_PORT') != '26380',
    reason='Requires explicitly isolated upstream validation Mongo DB and Redis',
)


async def connect_real():
    import app.core.database as database
    from app.core.config import settings
    from app.data.storage.mongo.client import reset_client
    assert os.environ.get('LATEST_VALIDATION_DB') == EXPECTED_DB
    assert settings.MONGO_DB == EXPECTED_DB
    assert settings.REDIS_HOST == '127.0.0.1' and settings.REDIS_PORT == 26380
    reset_client()
    await database.db_manager.init_mongodb()
    await database.db_manager.init_redis()
    database.mongo_client = database.db_manager.mongo_client
    database.mongo_db = database.db_manager.mongo_db
    database.redis_client = database.db_manager.redis_client
    database.redis_pool = database.db_manager.redis_pool
    assert database.get_mongo_db().name == EXPECTED_DB
    await database.get_mongo_db().command('ping')
    assert await database.get_redis_client().ping()
    return database


@pytest_asyncio.fixture
async def real_io():
    from app.data.storage.mongo.client import reset_client
    from app.services.queue_service import QueueService
    from app.services.queue import TASK_PREFIX, READY_LIST
    database = await connect_real()
    db, r = database.get_mongo_db(), database.get_redis_client()
    ids = []
    owner = 'validation-owner-' + uuid.uuid4().hex

    async def seed(status='processing', redis_status=None, include_owner=True):
        task_id = 'validation-' + uuid.uuid4().hex
        ids.append(task_id)
        doc = {'task_id': task_id, 'status': status, 'symbol': '000001',
               'created_at': datetime.now(timezone.utc), 'progress': 10}
        if include_owner:
            doc['user_id'] = owner
        await db.analysis_tasks.insert_one(doc)
        if redis_status:
            await r.hset(TASK_PREFIX + task_id, mapping={
                'id': task_id, 'status': redis_status, 'user': owner, 'symbol': '000001'})
            if redis_status == 'queued':
                await r.lpush(READY_LIST, task_id)
        return task_id

    try:
        yield db, r, QueueService(r), seed, owner
    finally:
        await db.analysis_tasks.delete_many({'task_id': {'$in': ids}})
        for task_id in ids:
            await r.delete(TASK_PREFIX + task_id)
            await r.lrem(READY_LIST, 0, task_id)
        await database.close_database()
        reset_client()


@pytest.mark.asyncio
async def test_cancel_running_without_redis_persists(real_io):
    from app.services.queue import TASK_PREFIX
    db, r, svc, seed, owner = real_io
    task_id = await seed()
    assert not await r.exists(TASK_PREFIX + task_id)
    assert await svc.cancel_task(task_id) is True
    row = await db.analysis_tasks.find_one({'task_id': task_id})
    assert row['status'] == 'cancelled' and row['cancel_requested_at'] > 0


@pytest.mark.asyncio
async def test_cancel_queued_persists_both_stores(real_io):
    from app.services.queue import TASK_PREFIX, READY_LIST
    db, r, svc, seed, owner = real_io
    task_id = await seed('queued', 'queued')
    assert await svc.cancel_task(task_id) is True
    assert (await db.analysis_tasks.find_one({'task_id': task_id}))['status'] == 'cancelled'
    assert await r.hget(TASK_PREFIX + task_id, 'status') == 'cancelled'
    assert task_id not in await r.lrange(READY_LIST, 0, -1)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
@pytest.mark.parametrize('with_redis', [False, True])
async def test_cancel_terminal_preserves_state(real_io, status, with_redis):
    from app.services.queue import TASK_PREFIX
    db, r, svc, seed, owner = real_io
    task_id = await seed(status, status if with_redis else None)
    assert await svc.cancel_task(task_id) is False
    assert (await db.analysis_tasks.find_one({'task_id': task_id}))['status'] == status
    if with_redis:
        assert await r.hget(TASK_PREFIX + task_id, 'status') == status


@pytest.mark.asyncio
async def test_cancel_absent_returns_false(real_io):
    _, _, svc, _, _ = real_io
    assert await svc.cancel_task('validation-absent-' + uuid.uuid4().hex) is False


@pytest.mark.asyncio
@pytest.mark.parametrize('ownership', ['own', 'wrong', 'missing'])
async def test_cancel_route_ownership_without_redis(real_io, ownership):
    from fastapi import HTTPException
    from app.routers.analysis import cancel_task
    db, r, svc, seed, owner = real_io
    task_id = await seed(include_owner=ownership != 'missing')
    user = {'id': owner if ownership != 'wrong' else 'validation-wrong-user'}
    if ownership == 'own':
        result = await cancel_task(task_id, user=user, svc=svc)
        assert result['success'] is True
        assert (await db.analysis_tasks.find_one({'task_id': task_id}))['status'] == 'cancelled'
    else:
        with pytest.raises(HTTPException) as denied:
            await cancel_task(task_id, user=user, svc=svc)
        assert denied.value.status_code == 404
        assert (await db.analysis_tasks.find_one({'task_id': task_id}))['status'] == 'processing'


@pytest.mark.asyncio
@pytest.mark.parametrize('ownership', ['own', 'wrong', 'missing'])
async def test_cancel_route_redis_ownership(real_io, ownership):
    from fastapi import HTTPException
    from app.routers.analysis import cancel_task
    from app.services.queue import TASK_PREFIX
    db, r, svc, seed, owner = real_io
    task_id = await seed('queued', 'queued')
    if ownership == 'missing':
        await r.hdel(TASK_PREFIX + task_id, 'user')
    user = {'id': owner if ownership != 'wrong' else 'validation-wrong-user'}
    if ownership == 'own':
        result = await cancel_task(task_id, user=user, svc=svc)
        assert result['success'] is True
        assert (await db.analysis_tasks.find_one({'task_id': task_id}))['status'] == 'cancelled'
    else:
        with pytest.raises(HTTPException) as denied:
            await cancel_task(task_id, user=user, svc=svc)
        assert denied.value.status_code == 404
        assert (await db.analysis_tasks.find_one({'task_id': task_id}))['status'] == 'queued'
        assert await r.hget(TASK_PREFIX + task_id, 'status') == 'queued'


def test_system_service_reconnect_across_actual_event_loops():
    from app.services.config.system_service import SystemService
    from app.models.config import SystemConfig
    from app.data.storage.mongo.client import reset_client
    service = SystemService()
    marker = 'validation-' + uuid.uuid4().hex
    old_client = []

    async def first_loop():
        database = await connect_real()
        db = database.get_mongo_db()
        try:
            config = SystemConfig(config_name=marker, config_type='system', version=900001,
                                  system_settings={'validation_marker': marker})
            row = config.model_dump(by_alias=True)
            row.pop('_id', None)
            row['is_active'] = True
            await db.system_configs.insert_one(row)
            result = await service.get_system_config()
            assert result is not None and result.system_settings['validation_marker'] == marker
            old_client.append((await service._get_db()).client)
        finally:
            await database.close_database()
            reset_client()

    async def second_loop():
        database = await connect_real()
        db = database.get_mongo_db()
        try:
            active = await service._get_db()
            assert active.client is not old_client[0], 'SystemService retained a closed Motor client'
            result = await service.get_system_config()
            assert result is not None and result.system_settings['validation_marker'] == marker
            await active.command('ping')
        finally:
            await db.system_configs.delete_many({'config_name': marker})
            await database.close_database()
            reset_client()

    asyncio.run(first_loop())
    asyncio.run(second_loop())


@pytest.mark.asyncio
async def test_actual_calendar_drives_data_interface_and_freshness(real_io):
    from app.data.core.interface import DataInterface
    from app.data.core.reader import Reader
    from app.data.scheduler.freshness_patrol import _expected_trading_date
    from app.data.schema.base.enums import FreshnessState
    db, r, svc, seed, owner = real_io
    DataInterface.reset_instance()
    try:
        data = await DataInterface.get_instance().read(
            'CN', 'trade_calendar', start_date='2026-09-01', end_date='2026-10-08')
        rows = data.get('data') or []
        assert rows, 'Real calendar did not flow through DataInterface'
        holiday = [row for row in rows if row.get('cal_date') == '2026-10-07']
        assert holiday and all(not row['is_open'] for row in holiday)
        expected = await _expected_trading_date('CN', 'daily_quotes')
        assert expected is not None
        result = await Reader().check_freshness('CN', '000001', 'daily_quotes', [{
            'trade_date': expected.isoformat(), 'updated_at': '2000-01-01T00:00:00Z'}])
        assert result == FreshnessState.FRESH
        stale = await Reader().check_freshness('CN', '000001', 'daily_quotes', [{
            'trade_date': (expected - timedelta(days=30)).isoformat(),
            'updated_at': datetime.now(timezone.utc).isoformat()}])
        assert stale == FreshnessState.STALE
    finally:
        DataInterface.reset_instance()
