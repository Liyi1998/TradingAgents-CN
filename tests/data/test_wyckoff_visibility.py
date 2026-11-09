"""Failure visibility and cross-baseline history retain dates and owner scope."""

from datetime import timedelta
from uuid import uuid4
import pytest
from app.engine.wyckoff_tracking import wyckoff_board
from app.engine.research_tracking import validate_update
from app.services.research_tracking import ResearchTrackingService, VERSIONS, BASELINES
from tests.data.test_wyckoff_tracking import baseline, price_snapshot, technical


def failed_version():
    return {
        "_id": "failed",
        "as_of": "2026-10-27",
        "created_at": baseline()["created_at"] + timedelta(days=1),
        "review": {},
        "modules": {"technical": {"wyckoff": None, "wyckoff_error": "3 validation errors for WyckoffAssessment"}},
    }


def test_failure_notice_does_not_claim_to_keep_a_nonexistent_phase():
    origin = baseline()
    origin["reports"] = {}
    board = wyckoff_board(origin, [], failed_version())
    assert board["current"] is None and "暂无" in board["notice"] and "保留上次判断" not in board["notice"]
    assert board["error_details_missing"] and board["error"]
    board = wyckoff_board(baseline(), [], failed_version())
    assert board["current"]["as_of"] == "2026-10-26" and "保留上次判断" in board["notice"]


def test_field_errors_are_actionable_without_exposing_rejected_input():
    value = technical()
    value["wyckoff"].update(phase="secret-rejected-value", events=[{"kind": "invalid-secret"}])
    result = validate_update(value, price_snapshot(), technical=True)
    assert result["content"] == value["content"] and result["wyckoff"] is None
    assert "phase (literal_error)" in result["wyckoff_error"] and "events.0" in result["wyckoff_error"]
    assert "secret" not in result["wyckoff_error"]


@pytest.mark.asyncio
async def test_older_baseline_reference_never_becomes_current_or_crosses_owner_cutoff(real_mongo_db):
    uid = "wyckoff_visibility_" + uuid4().hex
    old = baseline()
    new = {
        **old,
        "analysis_id": "new",
        "as_of": "2026-10-28",
        "created_at": old["created_at"] + timedelta(days=2),
        "reports": {},
    }
    track = dict(
        _id=uid,
        user_id=uid,
        symbol="603232",
        baseline_analysis_id="new",
        baseline=new,
        baseline_history=[dict(analysis_id=old["analysis_id"], archive_id=uid)],
    )
    service = ResearchTrackingService(real_mongo_db)
    try:
        await real_mongo_db[BASELINES].insert_one(dict(_id=uid, track_id=uid, user_id=uid, baseline=old))
        value = validate_update(technical(), price_snapshot(), technical=True)
        row = dict(
            _id=uid,
            track_id=uid,
            user_id=uid,
            baseline_analysis_id=old["analysis_id"],
            as_of="2026-10-27",
            created_at=old["created_at"] + timedelta(days=1),
            snapshot=price_snapshot(),
            modules={"technical": {**value, "as_of": "2026-10-27"}},
        )
        await real_mongo_db[VERSIONS].insert_one(row)
        reference = await service.previous_phase_reference(track, new, None)
        assert reference["version_id"] == uid and reference["as_of"] == "2026-10-27"
        assert wyckoff_board(new, [], None)["current"] is None
        assert await service.previous_phase_reference({**track, "user_id": "other"}, new, None) is None
        historical = {**old, "created_at": old["created_at"] - timedelta(days=1)}
        assert await service.previous_phase_reference(track, historical, None) is None
        await real_mongo_db[VERSIONS].update_one(
            {"_id": uid}, {"$set": {"created_at": new["created_at"] + timedelta(days=1)}}
        )
        reference = await service.previous_phase_reference(track, new, None)
        assert reference["source_kind"] == "report" and reference["as_of"] == old["as_of"]
        assert await real_mongo_db[BASELINES].find_one({"_id": uid})
    finally:
        await real_mongo_db[BASELINES].delete_many({"_id": uid})
        await real_mongo_db[VERSIONS].delete_many({"_id": uid})
