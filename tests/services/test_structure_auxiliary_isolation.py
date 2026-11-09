"""Isolated in-memory research store. Synthetic bars are not exchange data.

The database is SimulatedMongoDB. No network, no user database, and no excluded
audit files are read. Live free-quote behavior is covered separately.
"""

import os

os.environ["DEBUG"] = "true"
os.environ.setdefault("JWT_SECRET", "isolated-test-jwt-not-a-credential")
os.environ.setdefault("CSRF_SECRET", "isolated-test-csrf-not-a-credential")
os.environ.setdefault("MONGODB_HOST", "127.0.0.1")
os.environ.setdefault("MONGODB_PORT", "27018")
os.environ.setdefault("MONGODB_DATABASE", "tradingagents_validation")
os.environ.setdefault("MONGODB_USERNAME", "")
os.environ.setdefault("MONGODB_PASSWORD", "")
os.environ.setdefault("REDIS_HOST", "127.0.0.1")
os.environ.setdefault("REDIS_PORT", "6380")

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.research_tracking import TrackSettings
from app.routers.auth_db import get_current_user
from app.routers.research_tracking import get_structure_service, router
from app.services.research_tracking import TRACKS, VERSIONS
from app.services.structure_auxiliary import OBSERVATIONS, StructureAuxiliaryService
from tests.engine.test_structure_auxiliary import (
    DAY,
    SYMBOL,
    at,
    calendar,
    factors,
    flat,
    session,
)
from tests.test_infra import SimulatedMongoDB

WYCKOFF = {
    "phase": "markdown",
    "trend": "down",
    "status": "candidate",
    "method_version": "wyckoff-method-v2",
}


def packet(rows, *, when=DAY):
    return {
        "rows": rows,
        "calendar": calendar(when) if isinstance(when, str) else when,
        "adjustments": factors((DAY, 1)),
        "daily": [],
        "index_rows": [],
        "index_symbol": "000300.SH",
        "issues": [],
        "evidence_origin": "synthetic_test",
    }


class SyntheticContext(StructureAuxiliaryService):
    def __init__(self, db, rows):
        super().__init__(db)
        self.rows = rows

    async def load_context(self, symbol, now):
        assert symbol == SYMBOL
        visible = []
        for row in self.rows:
            end = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=now.tzinfo)
            if now >= end:
                visible.append(row)
        return packet(visible)


class ForbiddenContext(StructureAuxiliaryService):
    async def load_context(self, symbol, now):
        raise AssertionError("disabled auxiliary must not load quotes")


async def seed(db, *, enabled):
    now = at(DAY, "15:01:00")
    settings = TrackSettings(auto_enabled=False, structure_auxiliary_enabled=enabled).model_dump()
    await db[TRACKS].insert_one(
        {
            "_id": "track",
            "user_id": "alice",
            "symbol": SYMBOL,
            "name": "测试",
            "baseline_analysis_id": "report-1",
            "baseline_date": DAY,
            "baseline": {"analysis_id": "report-1", "decision": {"action": "HOLD"}},
            "created_at": now,
            "latest_version_id": "version-1",
            "settings": settings,
        }
    )
    await db[VERSIONS].insert_one(
        {
            "_id": "version-1",
            "track_id": "track",
            "user_id": "alice",
            "baseline_analysis_id": "report-1",
            "as_of": DAY,
            "modules": {"technical": {"wyckoff": dict(WYCKOFF), "content": "原技术结论"}},
            "review": {"held_action": "HOLD", "unheld_action": "WATCH", "content": "原复核"},
        }
    )
    return settings


@pytest.mark.asyncio
async def test_default_off_does_not_load_or_write():
    db = SimulatedMongoDB()
    await seed(db, enabled=False)
    result = await ForbiddenContext(db).observe("track", "alice", now=at(DAY, "15:01:00"))
    assert result["enabled"] is False and result["synthetic"] is False
    assert await db[OBSERVATIONS].count_documents({}) == 0
    assert await db["analysis_reports"].count_documents({}) == 0
    stored = await db[VERSIONS].find_one({"_id": "version-1"})
    assert stored["modules"]["technical"]["wyckoff"] == WYCKOFF
    assert stored["review"]["held_action"] == "HOLD"


@pytest.mark.asyncio
async def test_synthetic_profile_conflict_and_confirmation_delay_do_not_change_research():
    db = SimulatedMongoDB()
    original_settings = await seed(db, enabled=True)
    prices = [
        (10.0, 10.2, 9.8, 10.0),
        (10.0, 10.6, 9.9, 10.4),
        (10.4, 11.5, 10.3, 11.2),
        (11.0, 11.2, 10.6, 10.8),
        (10.8, 11.0, 10.5, 10.7),
        (10.8, 11.8, 10.7, 11.2),
        (11.1, 11.3, 10.9, 11.0),
        (11.0, 11.2, 10.8, 10.9),
    ]
    service = SyntheticContext(db, session(DAY, prices))
    early = await service.observe("track", "alice", now=at(DAY, "14:01:00"))
    assert early["synthetic"] is True and early["evidence_origin"] == "synthetic_test"
    assert early["model_called"] is False and early["decision_effect"] == "none" and early["trade_authorized"] is False
    assert early["preserved"]["held_action"] == "HOLD"
    sweeps = [event for event in early["smc"]["events"] if event["kind"] == "sweep"]
    assert sweeps and all(event["status"] == "candidate" and event["confirmed_time"] is None for event in sweeps)
    assert early["tpo"]["days"][0]["status"] == "cannot_judge"
    later = await service.observe("track", "alice", now=at(DAY, "15:01:00"))
    assert later["tpo"]["days"][0]["status"] == "ready"
    assert later["tpo"]["label"].startswith("30分钟OHLC")
    confirmed = [
        event for event in later["smc"]["events"] if event["kind"] == "sweep" and event["status"] == "confirmed"
    ]
    assert confirmed and confirmed[0]["event_time"] < confirmed[0]["confirmed_time"]
    conflict_rows = flat(DAY)
    conflict_rows.append(
        {**conflict_rows[3], "open": 10.01, "high": 10.04, "low": 10.0, "close": 10.03, "data_source": "other"}
    )
    conflicted = await SyntheticContext(db, conflict_rows).observe("track", "alice", now=at(DAY, "15:01:00"))
    assert conflicted["tpo"]["days"][0]["status"] == "cannot_judge"
    assert conflicted["sessions"][0]["status"] == "conflict"
    stale_rows = flat(DAY)
    stale = await SyntheticContext(db, stale_rows).observe("track", "alice", now=at("2026-11-06", "15:01:00"))
    assert next(gate["status"] for gate in stale["gates"] if gate["name"] == "新鲜度") == "cannot_judge"
    stored = await db[VERSIONS].find_one({"_id": "version-1"})
    assert stored["modules"]["technical"]["wyckoff"] == WYCKOFF
    assert stored["review"]["held_action"] == "HOLD"
    track = await db[TRACKS].find_one({"_id": "track"})
    assert track["settings"] == original_settings
    assert track["baseline"]["decision"] == {"action": "HOLD"}
    assert await db["analysis_reports"].count_documents({}) == 0
    assert await db[OBSERVATIONS].count_documents({}) == 4


def test_structure_route_is_owner_scoped_and_keeps_legacy_settings():
    import anyio

    db = SimulatedMongoDB()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"id": "alice"}
    app.dependency_overrides[get_structure_service] = lambda: ForbiddenContext(db)

    async def seed_off():
        await seed(db, enabled=False)

    anyio.run(seed_off)
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: {"id": "bob"}
    denied = client.get("/api/research-tracking/track/structure-auxiliary")
    assert denied.status_code == 400
    app.dependency_overrides[get_current_user] = lambda: {"id": "alice"}
    closed = client.get("/api/research-tracking/track/structure-auxiliary")
    assert closed.status_code == 200
    assert closed.json()["data"]["enabled"] is False

    async def enable():
        track = await db[TRACKS].find_one({"_id": "track"})
        settings = dict(track["settings"])
        settings["structure_auxiliary_enabled"] = True
        await db[TRACKS].update_one({"_id": "track"}, {"$set": {"settings": settings}})

    anyio.run(enable)
    app.dependency_overrides[get_structure_service] = lambda: SyntheticContext(db, flat(DAY))
    opened = client.get("/api/research-tracking/track/structure-auxiliary")
    assert opened.status_code == 200
    body = opened.json()["data"]
    assert body["enabled"] is True and body["synthetic"] is True and body["trade_authorized"] is False
    assert body["tpo"]["days"][0]["status"] == "ready"
    assert body["preserved"]["wyckoff_phase"] == "markdown"
