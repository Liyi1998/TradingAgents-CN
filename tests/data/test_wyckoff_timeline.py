"""Real Mongo chronology, baseline switching, ownership and immutable old views."""

from copy import deepcopy
from datetime import datetime
from uuid import uuid4

import pytest

from app.engine.research_tracking import validate_update
from app.services.research_tracking import ResearchTrackingService, TRACKS, VERSIONS, BASELINES
from tests.data.test_research_tracking import report, review
from tests.data.test_wyckoff_tracking import baseline, price_snapshot, technical


def at(day, hour=10):
    return datetime.fromisoformat(f"2026-10-{day:02d}T{hour:02d}:00:00+00:00")


@pytest.mark.asyncio
async def test_cross_baseline_timeline_keeps_dates_and_same_day_judgments_without_hindsight(real_mongo_db):
    db = real_mongo_db
    user = "phase-timeline-" + uuid4().hex
    service = ResearchTrackingService(db)
    ids = []

    async def source(day, hour, content):
        doc = report(user)
        doc.update(analysis_date=f"2026-10-{day:02d}", created_at=at(day, hour), reports=content)
        await db.analysis_reports.insert_one(doc)
        ids.append(doc["analysis_id"])
        return doc

    async def daily(tid, origin, key, day, hour, phase="markup", **extra):
        snap = price_snapshot(f"2026-10-{day:02d}")
        value = validate_update(technical(snap["as_of"], phase, "candidate"), snap, technical=True)
        row = dict(
            _id=user + key,
            track_id=tid,
            user_id=user,
            baseline_analysis_id=origin,
            as_of=snap["as_of"],
            created_at=at(day, hour),
            snapshot=snap,
            review=review(),
            modules={"technical": {**value, "as_of": snap["as_of"], **extra}},
            status="completed",
        )
        await db[VERSIONS].insert_one(row)
        return row

    try:
        old = await source(26, 10, baseline()["reports"])
        track = await service.create("002241", user, old["analysis_id"])
        tid = track["id"]
        await db[TRACKS].update_one({"_id": tid}, {"$set": {"created_at": at(26, 11)}})
        first = await daily(tid, ids[0], "-first", 27, 9)
        # Legacy rows with no baseline ID belong only to the first adopted report.
        await db[VERSIONS].update_one({"_id": first["_id"]}, {"$unset": {"baseline_analysis_id": ""}})
        correction = await daily(tid, ids[0], "-correction", 27, 10, "distribution")
        await db[TRACKS].update_one({"_id": tid}, {"$set": {"latest_version_id": correction["_id"]}})
        historical_before = (await service.board("002241", user, first["_id"]))["wyckoff"]
        second = await source(27, 11, baseline("当前阶段：拉升。")["reports"])
        await service.switch_baseline(tid, user, second["analysis_id"], ids[0])
        await db[TRACKS].update_one(
            {"_id": tid}, {"$set": {"baseline_history.0.archived_at": at(27, 12), "baseline_changed_at": at(27, 12)}}
        )
        newer = await daily(tid, ids[1], "-new", 28, 10)
        await db[TRACKS].update_one({"_id": tid}, {"$set": {"latest_version_id": newer["_id"]}})
        board = await service.board("002241", user)
        phases = board["wyckoff"]["timeline"]["history"]
        assert {p["source_analysis_id"] for p in phases} == set(ids)
        same_day = [p for p in phases if p["as_of"] == "2026-10-27"]
        assert {p["source_analysis_id"] for p in same_day} == set(ids)
        assert [p for p in phases if p["display_role"] == "current"][0]["version_id"] == newer["_id"]
        assert next(p for p in phases if p["version_id"] == first["_id"])["known_at"] == at(27, 9).isoformat()
        assert (await service.board("002241", user, first["_id"]))["wyckoff"] == historical_before
        assert board["wyckoff"]["timeline"]["boundaries"][1]["activated_at"] == at(27, 12).isoformat()
        assert board["wyckoff"]["timeline"]["boundaries"][1]["as_of"] == "2026-10-27"

        third = await source(29, 10, {"market_report": "威科夫方法校验：阶段结构未通过校验"})
        await service.switch_baseline(tid, user, third["analysis_id"], ids[1])
        await db[TRACKS].update_one(
            {"_id": tid}, {"$set": {"baseline_history.1.archived_at": at(29, 11), "baseline_changed_at": at(29, 11)}}
        )
        failed = await daily(tid, ids[2], "-failed", 29, 12, wyckoff_error="隔离样例：结构日期未见原文")
        await db[TRACKS].update_one({"_id": tid}, {"$set": {"latest_version_id": failed["_id"]}})
        await daily(tid, ids[1], "-future", 30, 10)
        await daily(tid, ids[0], "-inherited", 28, 9, inherited=True)
        foreign = deepcopy(first)
        foreign.update(_id=user + "-foreign", user_id="another-owner")
        await db[VERSIONS].insert_one(foreign)
        different = deepcopy(first)
        different.update(_id=user + "-other-stock", track_id="another-stock-track")
        await db[VERSIONS].insert_one(different)
        before = {
            n: await db[n].find({"$or": [{"user_id": user}, {"_id": {"$regex": "^" + user}}]}).to_list(None)
            for n in (TRACKS, VERSIONS, BASELINES, "analysis_reports")
        }
        result = await service.board("002241", user)
        value = result["wyckoff"]
        assert value["current"] is None and value["status"] == "failed"
        assert value["timeline"]["history"] and all(
            p["display_role"] == "historical" for p in value["timeline"]["history"]
        )
        assert len(value["timeline"]["boundaries"]) == 3
        assert not any(
            p["version_id"] in {user + s for s in ["-future", "-foreign", "-other-stock", "-inherited", "-failed"]}
            for p in value["timeline"]["history"]
        )
        assert (await service.board("002241", "another-owner"))["track"] is None
        assert (await service.board("002241", user, first["_id"]))["wyckoff"] == historical_before
        after = {
            n: await db[n].find({"$or": [{"user_id": user}, {"_id": {"$regex": "^" + user}}]}).to_list(None)
            for n in before
        }
        assert before == after
        assert await db.token_usage.count_documents({"user_id": user}) == 0
    finally:
        for name in (TRACKS, VERSIONS, BASELINES, "analysis_reports"):
            await db[name].delete_many({"$or": [{"user_id": user}, {"_id": {"$regex": "^" + user}}]})
