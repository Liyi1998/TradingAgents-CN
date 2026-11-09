"""Readiness rejects real closed/unreachable clients, with real Docker storage."""

import asyncio
import time

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.core import database
from app.core.config import settings
from app.core.service_lifecycle import ServiceLifecycle
from app.routers.health import router


@pytest_asyncio.fixture
async def ready_application():
    assert 'lifecycle_validation' in settings.MONGO_DB, 'Dedicated real test database required'
    await database.init_database()
    app = FastAPI()
    app.state.service_lifecycle = ServiceLifecycle()
    app.state.service_lifecycle.mark_ready()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://localhost') as client:
        try:
            yield app, client
        finally:
            app.state.service_lifecycle.mark_stopping()
            await database.close_db()
            app.state.service_lifecycle.mark_stopped()


@pytest.mark.asyncio
async def test_live_storage_and_lifecycle_are_ready(ready_application):
    app, client = ready_application
    for path in ('/health', '/api/health', '/readyz', '/api/readyz'):
        response = await client.get(path)
        assert response.status_code == 200
    assert (await client.get('/readyz')).json()['dependencies'] == {'mongodb': 'healthy', 'redis': 'healthy'}


@pytest.mark.asyncio
async def test_missing_lifecycle_never_claims_ready():
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://localhost') as client:
        for path in ('/api/health', '/api/healthz', '/api/readyz'):
            assert (await client.get(path)).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['starting', 'stopping', 'stopped', 'failed'])
async def test_non_ready_lifecycle_is_not_business_ready(ready_application, phase):
    app, client = ready_application
    app.state.service_lifecycle.phase = phase
    assert (await client.get('/api/health')).status_code == 503
    assert (await client.get('/api/readyz')).status_code == 503
    expected = 200 if phase == 'starting' else 503
    assert (await client.get('/api/healthz')).status_code == expected


@pytest.mark.asyncio
async def test_real_closed_storage_is_not_ready_even_if_process_is_alive(ready_application):
    app, client = ready_application
    # The production incident: lifecycle was lost but uvicorn was still serving.
    await database.close_db()
    response = await client.get('/api/readyz')
    assert response.status_code == 503
    assert response.json()['dependencies'] == {'mongodb': 'disconnected', 'redis': 'disconnected'}
    assert (await client.get('/api/health')).status_code == 503
    assert (await client.get('/api/healthz')).status_code == 200  # only liveness


@pytest.mark.asyncio
async def test_real_closed_motor_client_is_detected(ready_application):
    app, client = ready_application
    database.db_manager.mongo_client.close()
    response = await client.get('/api/readyz')
    assert response.status_code == 503
    assert response.json()['dependencies']['mongodb'] == 'unhealthy'


@pytest.mark.asyncio
async def test_real_unreachable_redis_is_bounded_not_reported_ready(ready_application):
    app, client = ready_application
    old = database.db_manager.redis_client
    unavailable = Redis(host='127.0.0.1', port=1, socket_connect_timeout=.2, socket_timeout=.2,
                        retry=Retry(NoBackoff(), 0))
    database.db_manager.redis_client = unavailable
    try:
        started = time.monotonic()
        response = await client.get('/api/readyz')
        assert response.status_code == 503 and time.monotonic() - started < 2.5
        assert response.json()['dependencies']['mongodb'] == 'healthy'
        assert response.json()['dependencies']['redis'] == 'unhealthy'
    finally:
        database.db_manager.redis_client = old
        await unavailable.aclose()


@pytest.mark.asyncio
async def test_silent_tcp_peer_cannot_hang_readiness(ready_application):
    app, client = ready_application
    accepted = []

    async def hold_socket(reader, writer):
        accepted.append(writer)
        try:
            await reader.read()
        finally:
            writer.close()

    server = await asyncio.start_server(hold_socket, '127.0.0.1', 0)
    old = database.db_manager.redis_client
    blocked = Redis(host='127.0.0.1', port=server.sockets[0].getsockname()[1],
                    socket_connect_timeout=10, socket_timeout=10)
    database.db_manager.redis_client = blocked
    try:
        started = time.monotonic()
        request = asyncio.create_task(client.get('/api/readyz'))
        await asyncio.sleep(.05)
        app.state.service_lifecycle.mark_stopping()
        response = await request
        assert response.status_code == 503 and time.monotonic() - started < 2.5
        assert response.json()['dependencies'] == {'mongodb': 'healthy', 'redis': 'timeout'}
        assert response.json()['phase'] == 'stopping'
    finally:
        database.db_manager.redis_client = old
        await blocked.aclose()
        server.close()
        await server.wait_closed()
        for writer in accepted:
            writer.close()
            await writer.wait_closed()
