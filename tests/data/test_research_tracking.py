"""Pure contracts and real Mongo persistence. No model calls or database mocks."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.data.query.research_tracking import daily_metrics, domain_block, price_bars
from app.engine.research_tracking import MODULES, POLICY, chart_levels, constrain_review, digest, inherited_modules, plan_modules, validate_strategy, validate_update
from app.engine.orchestrator.invoker import await_node_submission
from app.engine.orchestrator.workflow.submission import SubmissionBox, make_submit_report_tool
from app.llm.core.errors import RateLimitError
from app.llm.failures import describe_failure
from app.models.research_tracking import TrackSettings
from app.services.research_tracking import ResearchTrackingService, TRACKS, VERSIONS, RUNS, STEPS, BUDGETS, TrackingError


@pytest.mark.asyncio
async def test_partial_board_preserves_prior_levels_with_owner_baseline_time_boundaries(real_mongo_db):
    db = real_mongo_db; owner = 'resume-' + uuid4().hex
    service = ResearchTrackingService(db); doc = report(owner)
    await db.analysis_reports.insert_one(doc)
    try:
        public_track = await service.create('002241', owner)
        track = await service.owned(public_track['id'], owner)
        now = datetime.now(timezone.utc)
        base = {'track_id': track['_id'], 'user_id': owner, 'baseline_analysis_id': doc['analysis_id'],
            'as_of': '2026-10-27', 'snapshot': snapshot(), 'modules': {}, 'errors': []}
        good = {**base, '_id': uuid4().hex, 'created_at': now-timedelta(minutes=3), 'status': 'completed', 'review': review()}
        partial = {**base, '_id': uuid4().hex, 'created_at': now, 'status': 'partial', 'review': None}
        await db[VERSIONS].insert_many([good, partial,
            {**good, '_id': uuid4().hex, 'user_id': 'someone-else', 'created_at': now-timedelta(minutes=1)},
            {**good, '_id': uuid4().hex, 'baseline_analysis_id': 'other-baseline', 'created_at': now-timedelta(minutes=1)},
            {**good, '_id': uuid4().hex, 'as_of': '2026-10-28', 'created_at': now-timedelta(minutes=1)},
            {**good, '_id': uuid4().hex, 'created_at': now+timedelta(minutes=1)}])
        await db[TRACKS].update_one({'_id': track['_id']}, {'$set': {'latest_version_id': partial['_id']}})
        before = await db[VERSIONS].find_one({'_id': partial['_id']})
        board = await service.board('002241', owner)
        assert board['version']['review'] is None and board['version']['levels'] == []
        assert board['previous_review']['version_id'] == good['_id']
        point = board['previous_review']['levels'][0]
        assert point['state'] == 'previous_review' and point['valid_until'] == '2026-10-29'
        assert point['expired'] and point['source_quote'] == strategy()['levels'][0]['source_quote']
        assert await db[VERSIONS].find_one({'_id': partial['_id']}) == before
        assert (await service.board('002241', owner, good['_id']))['previous_review'] is None
        # An accepted review that removes its strategy supersedes older points.
        await db[VERSIONS].update_one({'_id': partial['_id']}, {'$set': {'review': {**review(), 'strategy': None}}})
        assert (await service.board('002241', owner))['previous_review'] is None
    finally:
        await db.analysis_reports.delete_one({'analysis_id': doc['analysis_id']})
        if 'track' in locals():
            for name in (TRACKS, VERSIONS, RUNS, STEPS):
                await db[name].delete_many({'_id': track['_id']} if name == TRACKS else {'track_id': track['_id']})


@pytest.mark.asyncio
async def test_daily_usage_is_unlimited_and_counted_by_china_date(real_mongo_db):
    db = real_mongo_db; owner = 'resume-' + uuid4().hex; service = ResearchTrackingService(db)
    doc = report(owner); await db.analysis_reports.insert_one(doc)
    try:
        track = await service.create('002241', owner)
        internal = await service.owned(track['id'], owner)
        now = datetime.now(timezone.utc)
        for _ in range(6): await service.reserve_budget(internal, now)
        budget = await service.budget_state(internal, now)
        assert budget['remaining'] is None and budget['used'] == 6 and budget['unlimited']
        queued = await service.enqueue(track['id'], owner, retry_failed=True)
        saved = await db[RUNS].find_one({'_id': queued['id']})
        await service.release(saved)
        assert await db[RUNS].count_documents({'user_id': owner}) == 1
        assert (await service.budget_state(internal, now+timedelta(days=1)))['used'] == 0
        await db[RUNS].delete_many({'user_id': owner})
    finally:
        await db.analysis_reports.delete_one({'analysis_id': doc['analysis_id']})
        await db[TRACKS].delete_many({'user_id': owner})
        await db[BUDGETS].delete_many({'_id': {'$regex': owner}})


@pytest.mark.asyncio
async def test_resume_executes_saved_snapshot_and_review_input_with_no_model_or_data_fetch(real_mongo_db):
    db = real_mongo_db; owner = 'resume-' + uuid4().hex; service = ResearchTrackingService(db)
    doc = report(owner); await db.analysis_reports.insert_one(doc)
    try:
        public_track = await service.create('002241', owner)
        track = await service.owned(public_track['id'], owner); snap = snapshot()
        modules = {k: {'content': '已完成模块正文，仅为检查点验收。', 'as_of': snap['as_of'], 'inherited': False} for k in MODULES}
        packet = {'original_failed_review_input': '与这次材料对应的旧复核意见，续跑不得替换为null'}
        partial = {'_id': uuid4().hex, 'track_id': track['_id'], 'user_id': owner,
            'baseline_analysis_id': doc['analysis_id'], 'as_of': snap['as_of'], 'created_at': datetime.now(timezone.utc),
            'status': 'partial', 'snapshot': snap, 'modules': modules, 'review': None,
            'fingerprint': digest({'as_of': snap['as_of'], 'domains': snap['domains'], 'policy': POLICY}),
            'pending_inputs': {'review': packet}, 'errors': ['综合复核：暂时不可用']}
        await db[VERSIONS].insert_one(partial)
        original = await db[VERSIONS].find_one({'_id': partial['_id']})
        await db[TRACKS].update_one({'_id': track['_id']}, {'$set': {'latest_version_id': partial['_id']}})
        key = digest({'track': track['_id'], 'policy': POLICY, 'step': 'review', 'packet': packet})
        await db[STEPS].insert_one({'_id': key, 'track_id': track['_id'], 'user_id': owner, 'status': 'completed', 'output': review()})
        run = await service.enqueue(track['_id'], owner, retry_failed=True)
        # execute() must bypass collect_evidence entirely, since this is an
        # explicit continuation of the saved material rather than a fresh update.
        await service.execute(run['id'])
        board = await service.board('002241', owner)
        assert board['run']['status'] == 'completed' and board['run']['model_calls'] == 0
        assert board['version']['snapshot'] == snap and board['version']['review'] == review()
        assert 'pending_inputs' not in board['version']
        assert await db[VERSIONS].find_one({'_id': partial['_id']}) == original
        assert await db[BUDGETS].count_documents({'_id': {'$regex': owner}}) == 0
    finally:
        await db.analysis_reports.delete_one({'analysis_id': doc['analysis_id']})
        for name in (TRACKS, VERSIONS, RUNS, STEPS): await db[name].delete_many({'user_id': owner})


@pytest.mark.asyncio
async def test_preclose_update_is_waiting_not_failed_or_partial(real_mongo_db):
    db = real_mongo_db; owner = 'resume-' + uuid4().hex; service = ResearchTrackingService(db)
    doc = report(owner); doc['analysis_date'] = '2026-10-28'; await db.analysis_reports.insert_one(doc)
    try:
        public_track = await service.create('002241', owner)
        run = await service.enqueue(public_track['id'], owner)
        saved = await db[RUNS].find_one({'_id': run['id']})
        await service.publish_snapshot(saved, await service.owned(public_track['id'], owner), snapshot())
        await service.release(saved)
        assert (await db[RUNS].find_one({'_id': run['id']}))['status'] == 'waiting_data'
        assert await db[VERSIONS].count_documents({'user_id': owner}) == 0
        assert await db[BUDGETS].count_documents({'_id': {'$regex': owner}}) == 0
    finally:
        await db.analysis_reports.delete_one({'analysis_id': doc['analysis_id']})
        for name in (TRACKS, VERSIONS, RUNS, STEPS): await db[name].delete_many({'user_id': owner})


def snapshot():
    return {"as_of": "2026-10-27", "next_sessions": ["2026-10-28", "2026-10-29"],
        "price": {"date": "2026-10-27", "close": 10.2}, "bars": [], "limitations": [],
        "domains": {d: {"rows": [{"id": d+":one", "date": "2026-10-27", "data": {"symbol": "002241"}}],
                         "status": "success", "limitations": []}
                    for d in ("daily_quotes", "daily_indicators", "money_flow", "margin_trading", "news", "financial_data")}}


def strategy():
    return {"as_of": "2026-10-27", "valid_until": "2026-10-29", "price_basis": "raw", "currency": "CNY",
        "levels": [{"kind": "entry", "label": "条件参考", "lower": 10., "upper": 10.5,
                    "condition": "放量且无新增负面事件时再研究", "source_quote": "10.00–10.50 元仅为条件参考，尚未批准买入。"}],
        "conditions": [{"kind": "entry", "text": "放量且经营改善", "state": "unknown", "evidence_ids": []}]}


def review():
    return {"content": "本测试报告明确保留来源与未知条件。10.00–10.50 元仅为条件参考，尚未批准买入。需要继续核实资金与公告证据。",
        "changes": ["价格变化"], "evidence_ids": ["daily_quotes:one"], "limitations": [],
        "requires_full_review": False, "research_view": "neutral", "unheld_action": "WATCH", "held_action": "HOLD", "strategy": strategy()}


def report(owner, symbol="002241"):
    return {"analysis_id": uuid4().hex, "stock_symbol": symbol, "stock_name": "验证股票",
        "market_type": "A股", "status": "completed", "analysis_horizon": "short", "analysis_date": "2026-10-26",
        "created_at": datetime.now(timezone.utc), "user_id": owner, "reports": {"market_report": "完整测试技术报告，数据缺项应如实标记。"}, "decision": {"action": "观望"}}


def test_change_detection_reuses_only_unchanged_modules_and_retries_failed_ones():
    snap = snapshot()
    previous = {"snapshot": deepcopy(snap), "modules": {k: {"content": "old", "as_of": "2026-10-26"} for k in MODULES}}
    assert plan_modules(previous, snap) == []
    snap["domains"]["money_flow"]["rows"][0]["data"]["main_net_inflow"] = 23
    assert plan_modules(previous, snap) == ["capital"]
    previous["modules"]["technical"]["update_error"] = "quota exhausted"
    assert plan_modules(previous, snap) == ["technical", "capital"]


def test_strategy_source_expiry_and_no_backpainting():
    value = strategy()
    assert validate_strategy(value, review()["content"], "2026-10-27")
    value["levels"][0]["upper"] = 99
    assert validate_strategy(value, review()["content"]) is None
    value = strategy()
    assert validate_strategy(value, review()["content"], "2026-10-28") is None
    levels = chart_levels(value, "2026-11-01", "2026-10-28T09:00:00+08:00")
    assert levels[0]["expired"] and levels[0]["effective_from"] == "2026-10-28"
    assert levels[0]["valid_until"] == "2026-10-29"
    value["levels"][0]["source_quote"] = "条件参考10.00–10.50元，仍需量价确认。"
    assert validate_strategy(value, value["levels"][0]["source_quote"])


def test_unknown_evidence_is_not_met_and_future_dates_not_invented():
    row = review()
    assert validate_update(row, snapshot(), final=True)["strategy"]
    row["strategy"]["conditions"][0]["state"] = "met"
    with pytest.raises(ValueError, match="当前证据"):
        validate_update(row, snapshot(), final=True)
    row = review(); row["strategy"]["valid_until"] = "2026-11-01"
    with pytest.raises(ValueError, match="交易日"):
        validate_update(row, snapshot(), final=True)
    row = review(); row["evidence_ids"] = ["future_fact"]
    with pytest.raises(ValueError, match="不存在"):
        validate_update(row, snapshot(), final=True)


def test_missing_quote_preserves_report_but_never_fabricates_prices_or_adds():
    snap = snapshot(); snap["price"] = None
    row = review(); row.update(unheld_action="BUY", held_action="ADD")
    checked = validate_update(row, snap, final=True)
    assert checked["content"] == row["content"]
    assert checked["strategy"] is None
    assert checked["unheld_action"] == checked["held_action"] == "RESEARCH_REQUIRED"


def test_field_dates_and_cache_dates_are_not_interchangeable():
    record = {"symbol": "002241", "announce_date": "2026-10-28", "report_date": "2026-09-30", "revenue": 100}
    out = domain_block("financial_data", {"data": [record], "quality": {"status": "success"}}, "2026-10-27", "002241")
    assert out["rows"] == [] and out["limitations"]
    record = {"symbol": "002241", "publish_time": "2026-10-27 08:00:00", "title": "公开测试新闻", "updated_at": "one"}
    a = domain_block("news", {"data": [record], "quality": {"status": "success"}}, "2026-10-27", "002241")
    record["updated_at"] = "two"
    b = domain_block("news", {"data": [record], "quality": {"status": "success"}}, "2026-10-27", "002241")
    assert a == b
    record["title"] += "更正"
    assert domain_block("news", {"data": [record]}, "2026-10-27", "002241")["rows"][0]["id"] != a["rows"][0]["id"]


def test_raw_price_identity_and_conflicting_sources():
    def raw(source, close):
        return {"symbol": "002241", "trade_date": "2026-10-27", "period": "daily", "price_basis": "raw", "volume_unit": "share",
                "open": 10, "close": close, "low": 9, "high": 12, "volume": 100, "data_source": source}
    result = {"data": [raw("a", 10.2)], "quality": {"status": "partial"}}
    block = domain_block("daily_quotes", result, "2026-10-27", "002241")
    assert price_bars(block, "2026-10-27")[1]["close"] == 10.2
    result["data"].append(raw("b", 11))
    assert price_bars(domain_block("daily_quotes", result, "2026-10-27", "002241"), "2026-10-27")[1] is None
    result["data"][0]["price_basis"] = "qfq"
    result["data"].pop()
    assert price_bars(domain_block("daily_quotes", result, "2026-10-27", "002241"), "2026-10-27") == ([], None)


def test_rolling_values_require_all_verified_sessions_and_noneligible_is_not_missing():
    sessions = [f"2026-10-{n:02}" for n in range(1, 21)]
    bars = [{"date": day, "close": 10., "volume": 100.} for day in sessions]
    values = daily_metrics(bars, sessions)
    assert values["ma20"] == values["ma5"] == 10
    assert values["volume_vs_previous_5_sessions"] == 1
    values = daily_metrics(bars[:-2] + bars[-1:], sessions)
    assert "ma5" not in values and "ma20" not in values
    assert "volume_vs_previous_5_sessions" not in values
    assert domain_block("margin_trading", {"data": [], "quality": {"status": "not_applicable"}}, "2026-10-27", "002241")["status"] == "not_applicable"


def test_retry_bookkeeping_does_not_change_model_checkpoint_input():
    service = ResearchTrackingService.__new__(ResearchTrackingService)
    track = {"symbol": "002241", "baseline_date": "2026-10-26", "baseline": {"reports": {}}}
    previous = {"technical": {"content": "原研究", "as_of": "2026-10-26", "inherited": False}}
    before = service.module_packet(track, previous, "technical", snapshot())
    previous["technical"].update(inherited=True, update_error="上次失败")
    assert service.module_packet(track, previous, "technical", snapshot()) == before


@pytest.mark.asyncio
async def test_accepted_risk_submission_survives_transport_error_not_cancellation():
    box = SubmissionBox()
    tool = make_submit_report_tool(box, node_type="debater")
    await tool.handler(content="完整风险报告：保留持有与未持有的分支，退出条件和当前不确定性需要分别核验。")
    async def failed():
        raise RateLimitError("Individual quota reached", status_code=429)
    assert await await_node_submission(failed(), box) == ""
    assert box.value and "完整风险报告" in box.value.content
    with pytest.raises(RateLimitError):
        await await_node_submission(failed(), SubmissionBox())
    async def cancelled():
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await await_node_submission(cancelled(), box)
    assert describe_failure(RateLimitError("QUOTA_EXHAUSTED"))["kind"] == "quota_exhausted"


@pytest.mark.asyncio
async def test_real_mongo_owner_isolation_version_read_and_budget(real_mongo_db):
    db = real_mongo_db; user = "tracking-test-" + uuid4().hex
    doc = report(user); service = ResearchTrackingService(db)
    inserted = await db.analysis_reports.insert_one(doc)
    try:
        with pytest.raises(TrackingError):
            await service.create("002241", "someone-else", doc["analysis_id"])
        track = await service.create("002241", user)
        assert track["settings"]["auto_enabled"] is False
        assert (await service.create("002241", user))["id"] == track["id"]
        assert (await service.board("002241", "someone-else"))["track"] is None
        with pytest.raises(TrackingError):
            await service.settings(track["id"], "someone-else", TrackSettings(auto_enabled=True))
        run = await service.enqueue(track["id"], user)
        with pytest.raises(TrackingError, match="正在执行"):
            await service.enqueue(track["id"], user)
        saved_run = await db[RUNS].find_one({"_id": run["id"]})
        await service.release(saved_run)
        assert await db[STEPS].count_documents({"user_id": user}) == 0
        owner_track = await service.owned(track["id"], user)
        owner_track["settings"]["max_calls_per_day"] = 1
        await service.reserve_budget(owner_track, datetime.now(timezone.utc))
        await service.reserve_budget(owner_track, datetime.now(timezone.utc))
        assert (await service.budget_state(owner_track))["used"] == 2
        before = await db.analysis_reports.find_one({"_id": inserted.inserted_id})
        board = await service.board("002241", user)
        assert board["version"] is None and board["track"]["baseline_analysis_id"] == doc["analysis_id"]
        assert await db.analysis_reports.find_one({"_id": inserted.inserted_id}) == before
    finally:
        await db.analysis_reports.delete_one({"_id": inserted.inserted_id})
        for name in (TRACKS, VERSIONS, RUNS, STEPS):
            await db[name].delete_many({"user_id": user})
        await db[BUDGETS].delete_many({"_id": {"$regex": user}})


@pytest.mark.asyncio
async def test_real_checkpoint_reuse_never_resolves_or_calls_model(real_mongo_db):
    db = real_mongo_db; user = "tracking-test-" + uuid4().hex
    service = ResearchTrackingService(db); track_id = uuid4().hex
    packet = {"public_test": "已经接受的结构化报告"}
    from app.engine.research_tracking import POLICY
    key = digest({"track": track_id, "policy": POLICY, "step": "technical", "packet": packet})
    output = {"content": "已保存正文", "as_of": "2026-10-27"}
    await db[STEPS].insert_one({"_id": key, "user_id": user, "status": "completed", "output": output})
    try:
        result = await service.model_step({"_id": track_id, "user_id": user}, {}, "technical", packet, snapshot())
        assert result == output
        assert await db[RUNS].count_documents({"user_id": user}) == 0
    finally:
        await db[STEPS].delete_one({"_id": key})


def test_incremental_observations_never_silently_relax_zero_entry_or_veto():
    raw = review(); raw.update(unheld_action="BUY", held_action="ADD")
    output = constrain_review(raw, {"decision": {"current_entry_limit_pct": 0}})
    assert output["unheld_action"] == "WATCH"
    assert output["held_action"] == "RESEARCH_REQUIRED"
    assert raw["unheld_action"] == "BUY"
    raw["strategy"]["conditions"] = [{"kind": "veto", "text": "否决成立", "state": "met", "evidence_ids": ["news:one"]}]
    assert constrain_review(raw, {"decision": {}})["unheld_action"] == "WATCH"


@pytest.mark.asyncio
async def test_real_snapshot_pipeline_publishes_then_deduplicates_without_model_calls(real_mongo_db):
    db = real_mongo_db; user = "tracking-test-" + uuid4().hex
    service = ResearchTrackingService(db); doc = report(user)
    inserted = await db.analysis_reports.insert_one(doc)
    track_id = None
    try:
        original_doc = await db.analysis_reports.find_one({"_id": inserted.inserted_id})
        track = await service.create("002241", user); track_id = track["id"]
        track = await service.owned(track_id, user)
        snap = snapshot(); modules = {}
        async def checkpoint(name, packet, output):
            key = digest({"track": track_id, "policy": POLICY, "step": name, "packet": packet})
            await db[STEPS].insert_one({"_id": key, "user_id": user, "track_id": track_id, "status": "completed", "output": output})
        for name in MODULES:
            packet = service.module_packet(track, modules, name, snap)
            output = validate_update({"content": "本段为持久化提交的验收样例，检查版本合并、证据日期和重复请求处理，不代表真实股票研究结果。",
                "changes": ["验证已接受模块的复用"], "evidence_ids": ["daily_quotes:one"], "limitations": []}, snap)
            await checkpoint(name, packet, output)
            modules[name] = {**output, "as_of": snap["as_of"], "inherited": False}
        await checkpoint("review", service.review_packet(track, modules, None, snap), validate_update(review(), snap, final=True))
        run = await service.enqueue(track_id, user)
        run = await db[RUNS].find_one({"_id": run["id"]})
        await service.publish_snapshot(run, track, snap)
        await service.release(run)
        board = await service.board("002241", user)
        assert board["version"]["status"] == "completed"
        assert board["version"]["as_of"] == "2026-10-27"
        assert len(board["version"]["modules"]) == 4
        assert board["run"]["model_calls"] == 0
        assert await db.analysis_reports.find_one({"_id": inserted.inserted_id}) == original_doc
        saved = await db[VERSIONS].find_one({"_id": board["version"]["id"]})
        run2 = await service.enqueue(track_id, user)
        run2 = await db[RUNS].find_one({"_id": run2["id"]})
        await service.publish_snapshot(run2, await service.owned(track_id, user), snap)
        await service.release(run2)
        assert (await db[RUNS].find_one({"_id": run2["_id"]}))["status"] == "unchanged"
        assert await db[VERSIONS].count_documents({"track_id": track_id}) == 1
        assert await db[VERSIONS].find_one({"_id": saved["_id"]}) == saved
        assert await db[BUDGETS].count_documents({"_id": {"$regex": user}}) == 0
    finally:
        await db.analysis_reports.delete_one({"_id": inserted.inserted_id})
        for name in (TRACKS, VERSIONS, RUNS, STEPS):
            await db[name].delete_many({"user_id": user})


@pytest.mark.asyncio
async def test_daily_budget_is_atomic_under_concurrency(real_mongo_db):
    db = real_mongo_db; user = "tracking-test-" + uuid4().hex
    track = {"_id": uuid4().hex, "user_id": user, "settings": {"max_calls_per_day": 3}}
    service = ResearchTrackingService(db)
    try:
        results = await asyncio.gather(*(service.reserve_budget(track, datetime.now(timezone.utc)) for _ in range(12)), return_exceptions=True)
        assert sum(not isinstance(r, Exception) for r in results) == 12
        row = await db[BUDGETS].find_one({"_id": {"$regex": user}})
        assert row["count"] == row["tracks"][track["_id"]] == 12
    finally:
        await db[BUDGETS].delete_many({"_id": {"$regex": user}})


@pytest.mark.asyncio
async def test_partial_version_preserves_successful_modules_and_retry_reuses_them(real_mongo_db):
    db = real_mongo_db; user = "tracking-test-" + uuid4().hex
    service = ResearchTrackingService(db); doc = report(user)
    inserted = await db.analysis_reports.insert_one(doc)
    try:
        created = await service.create("002241", user)
        track = await service.owned(created["id"], user); snap = snapshot(); modules = {}; keys = {}
        output = validate_update({"content": "本段为隔离验收用的已接受报告，用来验证失败模块不会覆盖已完成研究，也不会重复调用模型获取相同结果。",
            "changes": [], "evidence_ids": [], "limitations": []}, snap)
        for name in MODULES:
            packet = service.module_packet(track, modules, name, snap)
            key = digest({"track": track["_id"], "policy": POLICY, "step": name, "packet": packet}); keys[name] = key
            await db[STEPS].insert_one({"_id": key, "user_id": user, "status": "failed" if name == "capital" else "completed", "output": output, "attempts": 1})
            if name != "capital":
                modules[name] = {**output, "as_of": snap["as_of"], "inherited": False}
        queued = await service.enqueue(track["_id"], user)
        run = await db[RUNS].find_one({"_id": queued["id"]})
        await service.publish_snapshot(run, track, snap); await service.release(run)
        board = await service.board("002241", user)
        assert board["version"]["status"] == "partial" and board["version"]["review"] is None
        assert len(board["version"]["modules"]) == 3 and board["run"]["model_calls"] == 0
        old = await db[VERSIONS].find_one({"_id": board["version"]["id"]})
        await db[STEPS].update_one({"_id": keys["capital"]}, {"$set": {"status": "completed"}})
        ready = inherited_modules(old); ready["capital"] = {**output, "as_of": snap["as_of"], "inherited": False}
        packet = service.review_packet(track, ready, old, snap)
        key = digest({"track": track["_id"], "policy": POLICY, "step": "review", "packet": packet})
        await db[STEPS].insert_one({"_id": key, "user_id": user, "status": "completed", "output": validate_update(review(), snap, final=True)})
        with pytest.raises(TrackingError, match="等待"):
            await service.enqueue(track["_id"], user, retry_failed=True)
        await db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"retry_after_at": datetime.now(timezone.utc)-timedelta(seconds=1)}})
        queued = await service.enqueue(track["_id"], user, retry_failed=True)
        run = await db[RUNS].find_one({"_id": queued["id"]})
        await service.publish_snapshot(run, await service.owned(track["_id"], user), snap); await service.release(run)
        board = await service.board("002241", user)
        assert board["version"]["status"] == "completed"
        assert board["version"]["changed_modules"] == ["capital"]
        assert board["version"]["modules"]["technical"]["inherited"] is True
        assert board["run"]["model_calls"] == 0
        assert await db[VERSIONS].find_one({"_id": old["_id"]}) == old
    finally:
        await db.analysis_reports.delete_one({"_id": inserted.inserted_id})
        for name in (TRACKS, VERSIONS, RUNS, STEPS):
            await db[name].delete_many({"user_id": user})
