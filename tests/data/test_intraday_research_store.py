"""Real Mongo owner isolation, replay persistence and immutable report checks."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.research_tracking import TrackSettings
from app.services.research_tracking import ResearchTrackingService, TRACKS, TrackingError
from app.services.intraday_research import IntradayResearchService, CACHE, OBSERVATIONS
from tests.engine.test_intraday_research import snapshot


@pytest.mark.asyncio
async def test_company_name_resolves_after_awaiting_real_data_interface(real_mongo_db):
    from app.data.storage.mongo.collections import get_collection_name
    from app.engine.orchestrator.agents import _resolve_company
    await real_mongo_db[get_collection_name("basic_info", "CN")].insert_one({
        "symbol": "000503", "market": "CN", "name": "国新健康", "data_source": "tencent"})
    assert await _resolve_company("000503") == "国新健康"


@pytest.mark.asyncio
async def test_real_observations_owner_dedup_settings_and_baseline(real_mongo_db):
    db = real_mongo_db
    now = datetime(2026, 11, 5, 7, 2, tzinfo=timezone.utc)
    frozen = {"analysis_id": "original", "as_of": "2026-11-04", "reports": {}, "decision": {}}
    await db[TRACKS].insert_one({"_id": "track", "user_id": "alice", "symbol": "000503", "baseline_analysis_id": "original",
        "baseline_date": "2026-11-04", "baseline": frozen, "created_at": now-timedelta(days=1), "latest_version_id": None,
        "settings": TrackSettings(auto_enabled=False).model_dump()})
    await db[CACHE].insert_one({"_id": "000503:2026-11-05", "result": snapshot(), "next_check": now+timedelta(hours=1), "expires_at": now+timedelta(days=3)})
    service = IntradayResearchService(db)
    with pytest.raises(TrackingError):
        await service.observe("track", "bob", now=now)
    first = await service.observe("track", "alice", now=now)
    again = await service.observe("track", "alice", now=now+timedelta(seconds=10))
    assert first["zones"] == again["zones"]
    assert await db[OBSERVATIONS].count_documents({}) == 1
    assert (await db[TRACKS].find_one({"_id": "track"}))["baseline"] == frozen
    assert first["model_review_enabled"] is False and first["trade_authorized"] is False
    await db[TRACKS].update_one({"_id": "track"}, {"$set": {"baseline_analysis_id": "new", "baseline.analysis_id": "new"}})
    switched = await service.observe("track", "alice", now=now+timedelta(seconds=20))
    assert len(switched["history"]) == 1
    assert await db[OBSERVATIONS].count_documents({}) == 2
    await ResearchTrackingService(db).settings("track", "alice", TrackSettings(intraday_enabled=False, auto_enabled=True, max_calls_per_day=4))
    assert (await service.observe("track", "alice", now=now))["enabled"] is False
    row = await db[TRACKS].find_one({"_id": "track"})
    assert row["settings"]["auto_enabled"] is True and row["settings"]["max_calls_per_day"] == 4
    indexes = await db[OBSERVATIONS].index_information()
    assert any(v.get("expireAfterSeconds") == 0 for v in indexes.values())
