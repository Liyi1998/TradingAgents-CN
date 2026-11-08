"""Explicit one-run overflow, using isolated real Mongo and no model calls."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.research_tracking import TrackSettings, UpdateRequest
from app.services.research_tracking import (
    BUDGETS, RUNS, STEPS, TRACKS, VERSIONS, ResearchTrackingService, TrackingError, public,
)
from tests.data.test_research_tracking import report


@pytest.mark.parametrize("value", [-1, 11, True, 1.5, "2"])
def test_extra_allowance_requires_bounded_explicit_integer(value):
    with pytest.raises(ValidationError):
        UpdateRequest(manual_extra_calls=value)


@pytest.mark.asyncio
async def test_legacy_grants_no_longer_cap_usage_or_change_saved_settings(real_mongo_db):
    db = real_mongo_db
    owner = "manual-budget-" + uuid4().hex
    service = ResearchTrackingService(db)
    doc = report(owner)
    await db.analysis_reports.insert_one(doc)
    original_report = await db.analysis_reports.find_one({"analysis_id": doc["analysis_id"]})
    try:
        created = await service.create("002241", owner)
        await service.settings(created["id"], owner, TrackSettings(auto_enabled=True, max_calls_per_day=1))
        track = await service.owned(created["id"], owner)
        now = datetime.now(timezone.utc)
        await service.reserve_budget(track, now)
        with pytest.raises(TrackingError, match="自动更新不能"):
            await service.enqueue(track["_id"], owner, automatic=True, manual_extra_calls=2)
        with pytest.raises(TrackingError, match="起点已变化"):
            await service.enqueue(track["_id"], owner, manual_extra_calls=2, expected_baseline_analysis_id="old")
        with pytest.raises(TrackingError):
            await service.enqueue(track["_id"], "other-owner", manual_extra_calls=2)
        assert await db[RUNS].count_documents({"user_id": owner}) == 0

        queued = await service.enqueue(track["_id"], owner, retry_failed=True, manual_extra_calls=2,
                                       expected_baseline_analysis_id=track["baseline_analysis_id"])
        track = await service.owned(track["_id"], owner)
        run = await db[RUNS].find_one({"_id": queued["id"]})
        # A forged in-memory allowance cannot enlarge the persisted approval.
        forged = {**run, "manual_extra_calls": 10}
        results = await asyncio.gather(*(service.reserve_budget(track, now, forged) for _ in range(8)),
                                       return_exceptions=True)
        assert sum(not isinstance(r, Exception) for r in results) == 8
        budget = await service.budget_state(track, now)
        assert budget["used"] == budget["owner_used"] == 9
        assert budget["manual_extra_used"] == 0 and budget["remaining"] is None and budget["unlimited"]
        assert track["settings"] == {"auto_enabled": True, "max_calls_per_day": 1, "intraday_enabled": True, "structure_auxiliary_enabled": False}
        await service.finish(run, "completed", "仅预算验收，无模型调用")
        await service.release(run)
        queued_auto = await service.enqueue(track["_id"], owner, automatic=True)
        await service.release(await db[RUNS].find_one({"_id": queued_auto["id"]}))
        tomorrow = now + timedelta(days=1)
        await service.reserve_budget(track, tomorrow)
        await service.reserve_budget(track, tomorrow, run)
        assert (await service.budget_state(track, tomorrow))["used"] == 2
        assert (await db[RUNS].find_one({"_id": run["_id"]}))["model_calls"] == 0
        assert await db[STEPS].count_documents({"user_id": owner}) == 0
        assert await db.analysis_reports.find_one({"analysis_id": doc["analysis_id"]}) == original_report
    finally:
        await db.analysis_reports.delete_one({"analysis_id": doc["analysis_id"]})
        for name in (TRACKS, RUNS, VERSIONS, STEPS):
            await db[name].delete_many({"user_id": owner})
        await db[BUDGETS].delete_many({"_id": {"$regex": owner}})


@pytest.mark.asyncio
async def test_usage_above_old_ceiling_preserves_lease_owner_and_cooldown(real_mongo_db):
    db = real_mongo_db
    owner = "manual-budget-" + uuid4().hex
    service = ResearchTrackingService(db)
    doc = report(owner)
    await db.analysis_reports.insert_one(doc)
    try:
        created = await service.create("002241", owner)
        track = await service.owned(created["id"], owner)
        now = datetime.now(timezone.utc)
        day = (await service.budget_state(track, now))["date"]
        await db[BUDGETS].insert_one({"_id": f"owner:{owner}:{day}", "count": 24, "tracks": {}})
        queued = await service.enqueue(track["_id"], owner, manual_extra_calls=1)
        run = await db[RUNS].find_one({"_id": queued["id"]})
        track = await service.owned(track["_id"], owner)
        with pytest.raises(TrackingError):
            await service.reserve_step_call(track, {**run, "lease_token": "obsolete"}, uuid4().hex, "review", "local-test-no-call")
        await service.reserve_step_call(track, run, uuid4().hex, "review", "local-test-no-call")
        assert (await service.budget_state(track, now))["owner_used"] == 25
        await service.reserve_step_call(track, run, uuid4().hex, "review", "local-test-no-call")
        assert (await service.budget_state(track, now))["owner_used"] == 26
        assert (await db[RUNS].find_one({"_id": run["_id"]}))["model_calls"] == 2
        # Count is a reservation, not an actual provider call in this test.
        await service.finish(run, "partial", "验证延迟重试")
        await service.release(run)
        saved = await db[RUNS].find_one({"_id": run["_id"]})
        assert public(saved)["retry_after_at"].utcoffset() == timedelta(0)
        with pytest.raises(TrackingError, match="等待"):
            await service.enqueue(track["_id"], owner, retry_failed=True, manual_extra_calls=2)
        assert await db[RUNS].count_documents({"user_id": owner}) == 1
    finally:
        await db.analysis_reports.delete_one({"analysis_id": doc["analysis_id"]})
        for name in (TRACKS, RUNS, VERSIONS, STEPS):
            await db[name].delete_many({"user_id": owner})
        await db[BUDGETS].delete_many({"_id": {"$regex": owner}})


@pytest.mark.asyncio
async def test_continuation_estimate_uses_pending_modules_in_current_baseline_only(real_mongo_db):
    db = real_mongo_db
    owner = "manual-budget-" + uuid4().hex
    service = ResearchTrackingService(db)
    doc = report(owner)
    await db.analysis_reports.insert_one(doc)
    try:
        created = await service.create("002241", owner)
        track = await service.owned(created["id"], owner)
        row = {"_id": uuid4().hex, "track_id": track["_id"], "user_id": owner,
               "status": "partial", "baseline_analysis_id": track["baseline_analysis_id"],
               "pending_inputs": {"review": {"private": "not returned"}}}
        await db[VERSIONS].insert_one(row)
        track["latest_version_id"] = row["_id"]
        assert (await service.manual_options(track))["continue_max_calls"] == 2
        await db[VERSIONS].update_one({"_id": row["_id"]}, {"$set": {"pending_inputs.capital": {}}})
        assert (await service.manual_options(track))["continue_max_calls"] == 4
        assert (await service.manual_options({**track, "baseline_analysis_id": "new"}))["continue_max_calls"] == 10
        assert (await service.manual_options({**track, "user_id": "other"}))["continue_max_calls"] == 10
    finally:
        await db.analysis_reports.delete_one({"analysis_id": doc["analysis_id"]})
        for name in (TRACKS, VERSIONS):
            await db[name].delete_many({"user_id": owner})
