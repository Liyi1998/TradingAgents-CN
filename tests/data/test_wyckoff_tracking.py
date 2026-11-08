"""Actual contracts, tool submission and Mongo versions; no model requests/mocks."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.engine.research_tracking import MODULES, POLICY, digest, validate_update, tool_schema
from app.engine.wyckoff_tracking import baseline_assessment, checked_assessment, wyckoff_board
from app.models.research_tracking import TechnicalUpdate
from app.services.research_tracking import ResearchTrackingService, TRACKS, VERSIONS, RUNS, STEPS, BUDGETS, BASELINES
from tests.data.test_research_tracking import report, review, snapshot


def price_snapshot(day="2026-10-27"):
    result = snapshot(); result["as_of"] = day
    dates = [date.fromisoformat(day)-timedelta(days=i) for i in range(40)]
    dates = [d.isoformat() for d in sorted(dates) if d.weekday() < 5][-25:]
    result["bars"] = [{"date": d, "open": 10., "close": 10.2, "high": 10.5, "low": 9.8, "volume": 10000.,
        "data_source": "explicit_validation_sample", "evidence_id": "daily_quotes:"+d} for d in dates]
    result["price"] = result["bars"][-1]
    result["domains"]["daily_quotes"] = {"status": "success", "latest_date": day, "limitations": [],
        "rows": [{"id": b["evidence_id"], "date": b["date"], "data": {"symbol": "002241"}} for b in result["bars"]]}
    return result


def technical(day="2026-10-27", phase="testing", status="candidate"):
    label = {"testing": "试盘", "markup": "拉升", "distribution": "派发", "uncertain": "待判断"}[phase]
    content = f"本段为隔离验收样例，不代表真实行情研究。当前阶段：{label}。观察结构始于2026-10-20。确认条件：放量站稳区间上沿。失效条件：收盘跌破支撑且后续不能收回。日线不能确认盘中承接。"
    return {"content": content, "changes": ["更新阶段观察"], "evidence_ids": ["daily_quotes:"+day], "limitations": [],
        "wyckoff": {"phase": phase, "candidate_phase": None, "status": status, "source_quote": content,
            "evidence_ids": ["daily_quotes:"+day], "structure_start": "2026-10-20",
            "confirmation_conditions": ["放量站稳区间上沿。"],
            "invalidation_conditions": ["收盘跌破支撑且后续不能收回。"], "limitations": ["日线不能确认盘中承接"]}}


def baseline(text="- **阶段定性**：当前标的处于 **【阶段二：试盘】向【阶段三：拉升】过渡验证期**。"):
    return {"analysis_id": "baseline-sample", "as_of": "2026-10-26", "created_at": datetime(2026,10,26,10,tzinfo=timezone.utc),
        "reports": {"market_report": "## 威科夫四阶段识别\n\n"+text+"\n\n## 其他分析\n当前阶段：派发"}}


def test_baseline_uses_current_judgment_not_earlier_events_or_later_headings():
    row = baseline_assessment(baseline())
    assert (row["phase"], row["candidate_phase"], row["status"]) == ("testing", "markup", "transition")
    assert row["structure_start"] is None and row["source_line"] == 3
    assert row["source_quote"] in baseline()["reports"]["market_report"]


@pytest.mark.parametrize("line", [
    "若突破成功，当前阶段：拉升。", "例如当前阶段：吸筹。", "### 阶段一：吸筹", "前期出现吸筹、目前观察。"]) 
def test_baseline_does_not_turn_forecasts_examples_or_headings_into_current_phase(line):
    assert baseline_assessment(baseline(line)) is None


@pytest.mark.parametrize("line", ["当前阶段：并非吸筹。", "阶段判断：吸筹与派发存在分歧。", "当前阶段：尚未进入拉升。"])
def test_baseline_preserves_negation_and_competing_views(line):
    result = baseline_assessment(baseline(line))
    assert result["phase"] == result["status"] == "uncertain"


def test_multiple_current_claims_are_not_arbitrarily_selected():
    assert baseline_assessment(baseline("当前阶段：吸筹。\n当前阶段：派发。")) is None


def test_daily_contract_exposes_structured_phase_and_validates_evidence():
    value = technical(); result = validate_update(value, price_snapshot(), technical=True)
    assert result["wyckoff"]["phase"] == "testing" and "wyckoff_error" not in result
    assert "wyckoff" in tool_schema(TechnicalUpdate)["properties"]
    assert "$ref" not in str(tool_schema(TechnicalUpdate))


@pytest.mark.parametrize("change", [
    {"source_quote": "这段话没有出现在本次正文，不可以用来绑定阶段。"},
    {"structure_start": "2026-10-30"}, {"structure_start": "2026-10-19"},
    {"phase": "distribution"}, {"evidence_ids": ["news:one"]},
    {"evidence_ids": ["daily_quotes:2026-10-26"]},
    {"status": "transition"}, {"confirmation_conditions": []},
    {"invalidation_conditions": ["额外编造的退出条件"]}, {"phase": "uncertain", "status": "supported"},
])
def test_invalid_phase_is_isolated_without_losing_valid_technical_content(change):
    value = technical(); value["wyckoff"].update(change)
    result = validate_update(value, price_snapshot(), technical=True)
    assert result["content"] == value["content"] and result["wyckoff"] is None and result["wyckoff_error"]


def test_missing_or_conflicting_price_volume_and_intraday_cannot_support_phase():
    for field in ["short_window", "missing_volume", "price_conflict", "intraday"]:
        snap = price_snapshot()
        if field == "short_window": snap["bars"] = snap["bars"][-10:]
        if field == "missing_volume": snap["bars"][-2]["volume"] = None
        if field == "price_conflict": snap["price"] = None
        if field == "intraday": snap["bars"][-1]["status"] = "intraday"
        assert validate_update(technical(), snap, technical=True)["wyckoff"] is None
    value = technical(phase="uncertain", status="uncertain")
    value["wyckoff"].update(evidence_ids=[], structure_start=None, limitations=["本轮日线不足"])
    snap = price_snapshot(); snap.update(bars=[], price=None)
    assert validate_update(value, snap, technical=True)["wyckoff"]["status"] == "uncertain"


def test_unchanged_or_failed_updates_do_not_retimestamp_old_stage():
    now = datetime(2026,10,27,11,tzinfo=timezone.utc)
    snap = price_snapshot(); value = validate_update(technical(), snap, technical=True)
    row = {"_id": "one", "as_of": snap["as_of"], "created_at": now, "snapshot": snap,
        "modules": {"technical": {**value, "as_of": snap["as_of"], "inherited": False}}, "review": review()}
    inherited = deepcopy(row); inherited.update(_id="two", created_at=now+timedelta(days=1), as_of="2026-10-28")
    inherited["modules"]["technical"]["inherited"] = True
    result = wyckoff_board(baseline(), [row, inherited], inherited)
    assert result["status"] == "inherited" and result["current"]["known_at"] == now.isoformat()
    inherited["modules"]["technical"]["update_error"] = "上游不可用"
    result = wyckoff_board(baseline(), [row, inherited], inherited)
    assert result["status"] == "failed" and result["current"]["as_of"] == "2026-10-27"
    assert len(result["history"]) == 2


@pytest.mark.asyncio
async def test_real_submission_saves_bad_optional_stage_but_preserves_report(real_mongo_db):
    user = "wyckoff-test-"+uuid4().hex; key = uuid4().hex
    service = ResearchTrackingService(real_mongo_db)
    try:
        await real_mongo_db[STEPS].insert_one({"_id": key, "user_id": user, "status": "running"})
        tool, accepted, rejected = service.submission_tool({"user_id": user}, key, price_snapshot(), technical=True)
        value = technical(); value["wyckoff"]["phase"] = "made-up-phase"
        await tool.handler(**value)
        saved = await real_mongo_db[STEPS].find_one({"_id": key})
        assert saved["status"] == "completed" and saved["output"]["content"] == value["content"]
        assert saved["output"]["wyckoff"] is None and len(accepted) == 1 and not rejected
    finally:
        await real_mongo_db[STEPS].delete_one({"_id": key})


@pytest.mark.asyncio
async def test_real_pipeline_history_cutoff_baseline_switch_and_read_only_board(real_mongo_db):
    db = real_mongo_db; user = "wyckoff-test-"+uuid4().hex
    service = ResearchTrackingService(db); doc = report(user)
    checkpoint_keys = []
    doc.update(reports=baseline()["reports"], created_at=baseline()["created_at"])
    await db.analysis_reports.insert_one(doc)
    try:
        created = await service.create("002241", user); tid = created["id"]
        first = await service.board("002241", user)
        assert first["wyckoff"]["current"]["phase"] == "testing" and first["version"] is None
        track = await service.owned(tid, user); snap = price_snapshot(); modules = {}
        for name in MODULES:
            packet = service.module_packet(track, modules, name, snap)
            raw = technical() if name == "technical" else {k: v for k, v in technical().items() if k != "wyckoff"}
            if name == "technical":
                # New submissions use v2; the other tests still exercise v1
                # historical replay without silently upgrading old records.
                raw["content"] = raw["content"].replace("当前阶段：试盘", "当前阶段：吸筹候选") + "也可能是再派发，需后续验证。"
                raw["wyckoff"].update(method_version="wyckoff-method-v2", phase="accumulation",
                    source_quote=raw["content"], alternative_scenario="也可能是再派发，需后续验证。")
            key = digest({"track": tid, "policy": POLICY, "step": name, "packet": packet})
            checkpoint_keys.append(key)
            await db[STEPS].insert_one({"_id": key, "user_id": user, "track_id": tid, "status": "running"})
            tool, accepted, rejected = service.submission_tool(track, key, snap, technical=name == "technical")
            await tool.handler(**raw)
            modules[name] = {**accepted[0], "as_of": snap["as_of"], "inherited": False}
        raw_review = review(); raw_review["evidence_ids"] = [snap["price"]["evidence_id"]]
        packet = service.review_packet(track, modules, None, snap)
        key = digest({"track": tid, "policy": POLICY, "step": "review", "packet": packet})
        checkpoint_keys.append(key)
        await db[STEPS].insert_one({"_id": key, "user_id": user, "track_id": tid, "status": "running"})
        tool, _, _ = service.submission_tool(track, key, snap, final=True)
        await tool.handler(**raw_review)
        run = await service.enqueue(tid, user); run = await db[RUNS].find_one({"_id": run["id"]})
        await service.publish_snapshot(run, track, snap); await service.release(run)
        first = await service.board("002241", user); v1 = await db[VERSIONS].find_one({"_id": first["version"]["id"]})
        assert first["wyckoff"]["status"] == "current" and first["wyckoff"]["current"]["phase"] == "accumulation"
        assert first["run"]["model_calls"] == 0
        later = deepcopy(v1); later.update(_id=uuid4().hex, as_of="2026-10-28", created_at=v1["created_at"]+timedelta(minutes=1), snapshot=price_snapshot("2026-10-28"))
        later["modules"]["technical"] = {**validate_update(technical("2026-10-28", "markup", "supported"), later["snapshot"], technical=True), "as_of": later["as_of"], "inherited": False}
        await db[VERSIONS].insert_one(later)
        await db[TRACKS].update_one({"_id": tid}, {"$set": {"latest_version_id": later["_id"]}})
        assert (await service.board("002241", user))["wyckoff"]["current"]["phase"] == "markup"
        old_view = await service.board("002241", user, v1["_id"])
        assert old_view["wyckoff"]["current"]["phase"] == "accumulation"
        assert all(x["as_of"] <= v1["as_of"] for x in old_view["wyckoff"]["history"])
        assert (await service.board("002241", "not-owner"))["track"] is None
        new_doc = report(user); new_doc.update(analysis_date="2026-10-29", reports=baseline("当前阶段：派发。")['reports'])
        await db.analysis_reports.insert_one(new_doc)
        await service.switch_baseline(tid, user, new_doc["analysis_id"], doc["analysis_id"])
        new_view = await service.board("002241", user)
        assert len(new_view["wyckoff"]["history"]) == 1 and new_view["wyckoff"]["current"]["phase"] == "distribution"
        assert (await service.board("002241", user, v1["_id"]))["wyckoff"] == old_view["wyckoff"]
        assert await db[VERSIONS].find_one({"_id": v1["_id"]}) == v1
        assert await db[BUDGETS].count_documents({"_id": {"$regex": user}}) == 0
    finally:
        await db.analysis_reports.delete_many({"user_id": user})
        for name in (TRACKS, VERSIONS, RUNS, BASELINES):
            await db[name].delete_many({"user_id": user})
        # Tool checkpoints intentionally do not need an LLM reservation in this test.
        for key in checkpoint_keys:
            await db[STEPS].delete_one({"_id": key})
