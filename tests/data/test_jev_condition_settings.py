"""Real filesystem and real permission dependencies; no model/network test."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.llm.jev_conditions import load_condition_config
from app.services.jev_condition_config_service import (
    ConditionSettingsUpdate, SettingsConflict, SettingsRejected,
    get_condition_settings, update_condition_settings, _locked,
)


def change(state, **values):
    return ConditionSettingsUpdate(enabled=False, external_calls_enabled=False,
        max_calls_per_day=3, revision=state['revision'], **values)


def test_default_status_does_not_write_config_or_expose_key(tmp_path):
    path, legacy = tmp_path / 'new.json', tmp_path / 'old.json'
    legacy.write_text(json.dumps({'api_key': 'synthetic-test-key', 'enabled': False}))
    before = legacy.read_bytes()
    state = get_condition_settings(path, legacy)
    assert not state['enabled'] and not state['external_calls_enabled']
    assert state['key_available'] and state['key_source'] == 'saved_review'
    assert 'synthetic-test-key' not in json.dumps(state) and 'api_key' not in state
    assert not path.exists() and legacy.read_bytes() == before


def test_enable_consent_reuse_disable_and_stale_revision(tmp_path):
    path, legacy = tmp_path / 'new.json', tmp_path / 'old.json'
    legacy.write_text(json.dumps({'api_key': 'synthetic-test-key', 'enabled': False}))
    before = legacy.read_bytes()
    initial = get_condition_settings(path, legacy)
    values = dict(enabled=True, external_calls_enabled=True, max_calls_per_day=3, revision=initial['revision'])
    with pytest.raises(SettingsRejected):
        update_condition_settings(ConditionSettingsUpdate(**values), path, legacy)
    assert not path.exists()
    saved = update_condition_settings(ConditionSettingsUpdate(**values, confirm_external=True), path, legacy)
    assert saved['enabled'] and saved['external_calls_enabled'] and saved['key_source'] == 'conditions'
    assert load_condition_config(path).api_key.get_secret_value() == 'synthetic-test-key'
    assert legacy.read_bytes() == before
    assert 'synthetic-test-key' not in json.dumps(saved)
    if os.name != 'nt':
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(SettingsConflict):
        update_condition_settings(change(initial), path, legacy)
    stopped = update_condition_settings(change(saved), path, legacy)
    assert not stopped['enabled'] and not stopped['external_calls_enabled']
    assert load_condition_config(path).api_key.get_secret_value() == 'synthetic-test-key'
    assert not list(tmp_path.glob('.jev-conditions-*.tmp'))


def test_local_only_needs_no_key_and_external_requires_master(tmp_path):
    path, legacy = tmp_path / 'new.json', tmp_path / 'missing.json'
    initial = get_condition_settings(path, legacy)
    values = dict(enabled=True, external_calls_enabled=False, max_calls_per_day=2, revision=initial['revision'])
    state = update_condition_settings(ConditionSettingsUpdate(**values), path, legacy)
    assert state['enabled'] and not state['key_available']
    for master in (False, True):
        with pytest.raises(SettingsRejected):
            update_condition_settings(ConditionSettingsUpdate(**{**values, 'enabled': master, 'external_calls_enabled': True,
                'revision': state['revision'], 'confirm_external': True}), path, legacy)


@pytest.mark.parametrize('extra', [{'api_key': 'secret'}, {'model': 'other'}, {'mode': 'enforce'}, {'endpoint': 'https://other'}, {'enabled': 'true'}, {'max_calls_per_day': True}, {'max_calls_per_day': 0}, {'max_calls_per_day': 201}])
def test_request_allowlist(extra):
    with pytest.raises(ValidationError):
        ConditionSettingsUpdate(**{ 'enabled': False, 'external_calls_enabled': False,
            'max_calls_per_day': 3, 'revision': 'a' * 64, **extra})


def test_bad_legacy_does_not_block_disable_and_own_settings_preserved(tmp_path):
    path, legacy = tmp_path / 'new.json', tmp_path / 'old.json'
    path.write_text(json.dumps({'enabled': True, 'external_calls_enabled': True, 'api_key': 'own-key',
                               'timeout_seconds': 12, 'model': 'jev-1.13.0'}))
    legacy.write_text('invalid-json')
    saved = update_condition_settings(change(get_condition_settings(path, legacy)), path, legacy)
    assert not saved['enabled'] and saved['timeout_seconds'] == 12
    assert load_condition_config(path).api_key.get_secret_value() == 'own-key'


def test_lock_never_silently_allows_concurrent_write(tmp_path):
    path, legacy = tmp_path / 'new.json', tmp_path / 'missing.json'
    state = get_condition_settings(path, legacy)
    with _locked(path), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(update_condition_settings, change(state), path, legacy)
        with pytest.raises(OSError):
            future.result(timeout=5)
    assert not path.exists()


@pytest.mark.asyncio
async def test_route_admin_guards_and_sanitized_errors():
    from app.routers.auth_db import require_admin
    from app.routers.config.jev_conditions import router, _run
    from fastapi.routing import APIRoute
    for route in router.routes:
        assert isinstance(route, APIRoute)
        assert any(dep.call is require_admin for dep in route.dependant.dependencies)
    with pytest.raises(HTTPException) as denied:
        await require_admin({'is_admin': False})
    assert denied.value.status_code == 403
    assert await require_admin({'is_admin': True}) == {'is_admin': True}
    # Real invalid file operations use the same service and sanitized route wrapper.
    from tempfile import TemporaryDirectory
    from pathlib import Path
    with TemporaryDirectory() as folder:
        path = Path(folder) / 'invalid.json'
        path.write_text('{"api_key":"must-not-leak", "enabled":"invalid"}')
        with pytest.raises(HTTPException) as rejected:
            await _run(get_condition_settings, path, Path(folder) / 'missing')
        assert rejected.value.status_code == 503
        assert 'must-not-leak' not in str(rejected.value)
