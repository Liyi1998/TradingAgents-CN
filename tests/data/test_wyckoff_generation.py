"""Generation/repair contracts through real tools and isolated Mongo, no model calls."""

from copy import deepcopy
from uuid import uuid4

import pytest

from app.engine.wyckoff_tracking import checked_assessment, WYCKOFF_POLICY, baseline_assessment
from app.engine.wyckoff_quotes import bind_quote
from app.engine.wyckoff_repair import make_repair_tool, correction_prompt
from app.engine.research_tracking import plan_modules, validate_update, wyckoff_needs_refresh
from app.engine.orchestrator.workflow.submission import SubmissionBox, make_submit_report_tool, commit_submission
from app.services.research_tracking import ResearchTrackingService, TRACKS, RUNS, STEPS, BUDGETS
from tests.data.test_wyckoff_method import method_case


def technical_case():
    value, content, snapshot = method_case()
    snapshot["domains"] = {
        "daily_quotes": {"rows": [{"id": x["evidence_id"]} for x in snapshot["bars"]], "status": "available"}
    }
    row = {
        "content": content,
        "changes": [],
        "evidence_ids": value["evidence_ids"],
        "limitations": [],
        "requires_full_review": False,
        "wyckoff": value,
    }
    return row, snapshot


def test_typography_binding_preserves_original_span_but_never_accepts_paraphrase_or_changed_numbers():
    content = "**趋势背景**：下跌；**当前阶段**：吸筹候选，尚待测试。"
    quote = "趋势背景：下跌；当前阶段：吸筹候选，尚待测试。"
    result = bind_quote(quote, content, 1800)
    assert result in content and result != quote
    assert bind_quote("趋势背景：上涨；当前阶段：吸筹候选，尚待测试。", content, 1800) not in content
    for wrong in ["收盘跌破12.60元则失效", "收盘未跌破12.66元则失效", "收盘跌破12.66元则成立"]:
        assert bind_quote(wrong, "**收盘跌破12.66元则失效**", 600) not in "**收盘跌破12.66元则失效**"
    assert bind_quote("not confirmed", "notconfirmed", 600) == "not confirmed"


def test_separately_bound_structure_start_keeps_date_evidence_and_rejects_unrelated_dates():
    value, content, snap = method_case()
    quote = "**结构起始日**：2026-10-30，急跌后观察停止行为。"
    content += "\n" + quote
    value.update(structure_start="2026-10-30", structure_start_quote="结构起始日：2026-10-30，急跌后观察停止行为。")
    result = checked_assessment(value, content, snap, require_method=True)
    assert result["structure_start"] == "2026-10-30" and result["structure_start_quote"] in content
    value["structure_start_quote"] = "2026-10-30 SC候选：急跌可能释放部分卖压。"
    with pytest.raises(ValueError, match="明确标明结构开始"):
        checked_assessment(value, content, snap, require_method=True)
    value["structure_start_quote"] = "结构起始日：2026-10-29，未出现的声明。"
    with pytest.raises(ValueError):
        checked_assessment(value, content, snap, require_method=True)


@pytest.mark.asyncio
async def test_one_correction_can_replace_only_method_section_and_keeps_strict_event_checks():
    value, section, snap = method_case()
    content = "## 技术价格\n既有价格与非威科夫分析保持原文。\n\n" + section + "\n\n## 风险限制\n必要风险限制不能删除。"
    bad = deepcopy(value)
    bad["events"][0]["kind"] = "PSY/BC"
    tool, accepted = make_repair_tool(content, snap)
    with pytest.raises(ValueError, match="events.0.kind"):
        await tool.handler(wyckoff=bad)
    assert not accepted
    with pytest.raises(ValueError, match="其他报告章节"):
        await tool.handler(wyckoff=value, method_section=section + "\n## 新账户方案\n不能替换这一段")
    assert not accepted
    assert "通过" in await tool.handler(wyckoff=value, method_section=section)
    saved = accepted[0]
    assert saved["content"].startswith("## 技术价格\n既有价格与非威科夫分析保持原文。")
    assert saved["content"].endswith("## 风险限制\n必要风险限制不能删除。")
    with pytest.raises(ValueError, match="不可重复"):
        await tool.handler(wyckoff=value)
    assert len(accepted) == 1


@pytest.mark.asyncio
async def test_real_full_submission_correction_preserves_report_and_updates_blackboard():
    value, content, snap = method_case()
    bad = deepcopy(value)
    bad["candidate_phase"] = bad["phase"]
    box = SubmissionBox()
    state = {"analysis_context": {"wyckoff_snapshots": snap}}
    submit = make_submit_report_tool(
        box, node_type="analyst", agent_key="market", report_key="market_report", state=state
    )
    await submit.handler(content=content, wyckoff=bad)
    assert box.value.fields["wyckoff_error"] and "阶段结构未通过" in box.value.content
    repair, accepted = make_repair_tool(box.value.content, snap)
    await repair.handler(wyckoff=value)
    box.value.content = accepted[0]["content"]
    box.value.fields.update(wyckoff=accepted[0]["wyckoff"])
    box.value.fields.pop("wyckoff_error")
    await commit_submission(box.value, state=state, report_key="market_report", event_sink=None)
    assert state["reports"]["market_report"] == content and box.value.fields["wyckoff"]["phase"] == "accumulation"
    assert "阶段结构未通过" not in state["reports"]["market_report"]


def test_correctable_old_failure_gets_one_new_policy_attempt_without_repeating_every_refresh():
    row, snap = technical_case()
    row["wyckoff"]["events"][0]["kind"] = "JAC"
    output = validate_update(row, snap, technical=True, require_method=True)
    assert output["wyckoff"] is None
    previous = {"snapshot": deepcopy(snap), "modules": {"technical": output}}
    assert wyckoff_needs_refresh(previous) and "technical" in plan_modules(previous, snap)
    output["wyckoff_submission_policy"] = WYCKOFF_POLICY
    assert not wyckoff_needs_refresh(previous) and "technical" not in plan_modules(previous, snap)
    snap["domains"]["daily_quotes"]["rows"].append({"id": "new-session"})
    assert "technical" in plan_modules(previous, snap)


@pytest.mark.asyncio
async def test_real_daily_repair_auto_disable_keeps_valid_body_and_does_not_call_model(real_mongo_db):
    db = real_mongo_db
    user = "phase-repair-" + uuid4().hex
    key = uuid4().hex
    service = ResearchTrackingService(db)
    track = {"_id": uuid4().hex, "user_id": user, "settings": {"auto_enabled": True, "max_calls_per_day": 1}}
    run = {"_id": uuid4().hex, "user_id": user, "automatic": True, "model_calls": 0}
    await db[TRACKS].insert_one(deepcopy(track))
    await db[RUNS].insert_one(deepcopy(run))
    row, snap = technical_case()
    row["wyckoff"]["events"][0]["kind"] = "Spring/SC"
    try:
        await service.reserve_step_call(track, run, key, "technical", "not-a-model-call")
        tool, accepted, _ = service.submission_tool(track, key, snap, technical=True)
        await tool.handler(**row)
        assert accepted[0]["wyckoff_error"] and accepted[0]["content"] == row["content"]
        # Disabling automatic work still stops a correction without a provider call.
        await db[TRACKS].update_one({"_id": track["_id"]}, {"$set": {"settings.auto_enabled": False}})
        output = await service.repair_technical_output(track, run, key, "not-a-model-call", None, accepted[0], snap)
        assert output["content"] == row["content"] and output["wyckoff"] is None
        assert output["wyckoff_repair"]["attempted"] is False
        assert "自动更新已关闭" in output["wyckoff_repair"]["error"]
        saved = await db[STEPS].find_one({"_id": key})
        assert saved["status"] == "completed" and saved["output"] == output and saved["attempts"] == 1
        assert (await db[RUNS].find_one({"_id": run["_id"]}))["model_calls"] == 1
        assert await db.token_usage.count_documents({"user_id": user}) == 0
    finally:
        for name in (TRACKS, RUNS, STEPS):
            await db[name].delete_many({"user_id": user})
        await db[BUDGETS].delete_many({"_id": {"$regex": user}})


def test_repair_prompt_retains_rejected_reason_and_evidence_without_rewriting_other_modules():
    row, snap = technical_case()
    system, body = correction_prompt(row["content"], "events.0.kind (literal_error)", snap)
    assert "PSY/BC" in system and "candidate_phase" in system and "structure_start_quote" in system
    assert "events.0.kind" in body and snap["bars"][-1]["evidence_id"] in body


def test_production_sized_snapshot_preserves_all_method_evidence_without_unrelated_domains():
    import json

    row, snap = technical_case()
    snap.update(previous_wyckoff={"as_of": "2026-10-30", "assessment": {"phase": "markdown"}},
                daily_metrics={"volume_comparisons": {"previous_day": 0.8}},
                price_basis="raw", currency="CNY", limitations=["日线不能确认盘中承接"])
    # Unrelated rows can be much larger than the price evidence in production.
    snap["domains"]["news"] = {"rows": [{"content": "新闻正文" * 40000}]}
    snap["chan"] = {"chart": "图层数据" * 10000}
    before = deepcopy(snap)
    _, body = correction_prompt(row["content"], "阶段原文须同时说明趋势背景", snap)
    packet = json.loads(body)
    assert len(json.dumps(snap, ensure_ascii=False).encode()) > 240000
    assert len(body.encode()) < 150000
    for key in ("as_of", "bars", "price", "previous_wyckoff", "daily_metrics", "price_basis", "currency", "limitations"):
        assert packet["snapshot"][key] == snap[key]
    assert "domains" not in packet["snapshot"] and "chan" not in packet["snapshot"]
    assert snap == before and packet["content"] == row["content"]


def test_previous_submission_payload_failure_gets_one_new_contract_update():
    row, snap = technical_case()
    row["wyckoff"]["events"][0]["kind"] = "JAC"
    output = validate_update(row, snap, technical=True, require_method=True)
    output["wyckoff_submission_policy"] = "wyckoff-method-v2-submission-v3"
    output["wyckoff_repair"] = {"attempted": False, "succeeded": False, "error": "局部纠正材料超过输入上限"}
    previous = {"snapshot": deepcopy(snap), "modules": {"technical": output}}
    assert wyckoff_needs_refresh(previous) and "technical" in plan_modules(previous, snap)
    output["wyckoff_submission_policy"] = WYCKOFF_POLICY
    assert not wyckoff_needs_refresh(previous) and "technical" not in plan_modules(previous, snap)


def test_explicit_unknown_is_a_dated_judgment_not_missing_report():
    row = baseline_assessment({"reports": {"market_report": "## 威科夫阶段\n当前阶段：证据不足，尚不能判定。"}})
    assert row["phase"] == row["status"] == "uncertain"


@pytest.mark.asyncio
async def test_real_unavailable_endpoint_during_correction_does_not_erase_submitted_report():
    from app.llm.core.factory import create_client
    from app.engine.wyckoff_repair import repair_full_submission
    from app.engine.orchestrator.workflow.submission import Submission

    value, content, snap = method_case()
    submission = Submission(content=content, fields={"wyckoff": None, "wyckoff_error": "events.0.kind"})
    client = create_client(
        "openai",
        model="local-connection-failure-test",
        api_key="isolated-no-secret",
        base_url="http://127.0.0.1:9/v1",
        timeout=1,
    )
    success = await repair_full_submission(submission, {"wyckoff_snapshots": snap}, client, max_tokens=64)
    assert not success and submission.content.endswith(content)
    assert "无法连接模型服务" in submission.content
    assert submission.fields["wyckoff"] is None and "无法连接模型服务" in submission.fields["wyckoff_error"]
    assert submission.fields["wyckoff_repair"]["attempted"] and not submission.fields["wyckoff_repair"]["succeeded"]


@pytest.mark.asyncio
async def test_real_published_completed_research_discloses_optional_phase_failure(real_mongo_db):
    from app.engine.research_tracking import MODULES, POLICY, digest
    from app.services.research_tracking import VERSIONS
    from tests.data.test_research_tracking import report, snapshot, review

    db = real_mongo_db
    user = "phase-message-" + uuid4().hex
    service = ResearchTrackingService(db)
    inserted = await db.analysis_reports.insert_one(report(user))
    try:
        public = await service.create("002241", user)
        track = await service.owned(public["id"], user)
        snap = snapshot()
        modules = {}

        async def checkpoint(name, packet, output):
            key = digest({"track": track["_id"], "policy": POLICY, "step": name, "packet": packet})
            await db[STEPS].insert_one({"_id": key, "user_id": user, "track_id": track["_id"],
                                       "status": "completed", "output": output})

        for name in MODULES:
            packet = service.module_packet(track, modules, name, snap)
            output = validate_update({"content": "保留已完成的技术与其他研究正文；本条记录仅验证阶段缺项不会隐藏在整体完成提示中。",
                "changes": [], "evidence_ids": ["daily_quotes:one"], "limitations": []}, snap,
                technical=name == "technical", require_method=name == "technical")
            if name == "technical":
                output["wyckoff_submission_policy"] = WYCKOFF_POLICY
                output["wyckoff_repair"] = {"attempted": True, "succeeded": False, "error": "事件引用校验失败"}
            await checkpoint(name, packet, output)
            modules[name] = {**output, "as_of": snap["as_of"], "inherited": False}
        await checkpoint("review", service.review_packet(track, modules, None, snap), validate_update(review(), snap, final=True))
        run = await service.enqueue(track["_id"], user)
        run = await db[RUNS].find_one({"_id": run["id"]})
        await service.publish_snapshot(run, track, snap)
        await service.release(run)
        saved = await db[RUNS].find_one({"_id": run["_id"]})
        assert saved["status"] == "completed" and saved["model_calls"] == 0
        assert "每日研究版本已保存；威科夫阶段未更新：事件引用校验失败" == saved["message"]
        assert await db[VERSIONS].count_documents({"track_id": track["_id"]}) == 1
        assert await db.token_usage.count_documents({"user_id": user}) == 0
    finally:
        await db.analysis_reports.delete_one({"_id": inserted.inserted_id})
        for name in (TRACKS, RUNS, STEPS, VERSIONS):
            await db[name].delete_many({"user_id": user})
