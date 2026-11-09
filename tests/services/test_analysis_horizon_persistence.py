"""Horizon snapshots, independent reports and retries using real MongoDB I/O.

No analysis is executed and no external model is called by these tests.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from bson import ObjectId
from pydantic import ValidationError

from app.models.analysis import AnalysisParameters, AnalysisTask, SingleAnalysisRequest
from app.services.analysis_horizon import (
    horizon_from_snapshot,
    require_matching_horizon,
    task_report_directory,
)
from app.services.analysis_retry import create_retry_task, same_day_parameters
from app.services.analysis_service import AnalysisService
from app.services.reports_service import ReportsService


@pytest.mark.parametrize("horizon", ["short", "medium", "long"])
def test_request_and_batch_task_snapshot_preserve_horizon(horizon):
    request = SingleAnalysisRequest.model_validate({
        "symbol": "000001", "parameters": {"analysis_horizon": horizon},
    })
    task = AnalysisTask(task_id="horizon-task", user_id=ObjectId(), symbol="000001", parameters=request.parameters)
    assert task.model_dump()["analysis_horizon"] == horizon
    assert task.model_dump()["parameters"]["analysis_horizon"] == horizon
    with pytest.raises(ValidationError, match="frozen"):
        request.parameters.analysis_horizon = "long" if horizon != "long" else "short"


def test_legacy_default_and_invalid_horizon():
    assert AnalysisParameters().analysis_horizon == "short"
    assert horizon_from_snapshot({}) == "short"
    with pytest.raises(ValidationError):
        SingleAnalysisRequest(symbol="000001", parameters={"analysis_horizon": "weekly"})


@pytest.mark.parametrize("horizon", ["short", "medium", "long"])
def test_retry_retains_immutable_horizon(horizon):
    original = {"analysis_horizon": horizon, "parameters": {"analysis_date": "2026-10-13"}}
    parameters = same_day_parameters(original, datetime(2026, 10, 13, 6, tzinfo=timezone.utc))
    assert parameters.analysis_horizon == horizon
    assert "analysis_horizon" not in original["parameters"]
    with pytest.raises(ValueError, match="分析周期"):
        require_matching_horizon(original, {"analysis_horizon": "medium" if horizon != "medium" else "long"})


def test_contradictory_retry_snapshot_is_rejected():
    with pytest.raises(ValueError, match="分析周期"):
        same_day_parameters(
            {"analysis_horizon": "long", "parameters": {"analysis_horizon": "short", "analysis_date": "2026-10-13"}},
            datetime(2026, 10, 13, 6, tzinfo=timezone.utc),
        )


def test_archives_are_distinct_for_horizon_and_task(tmp_path):
    paths = {
        task_report_directory(tmp_path, "000001", "2026-10-13", horizon, task_id)
        for horizon in ("short", "medium", "long") for task_id in ("first-task", "second-task")
    }
    assert len(paths) == 6
    for path in paths:
        assert path.is_relative_to(tmp_path)
        assert path.name == "reports"
    with pytest.raises(ValueError, match="归档标识"):
        task_report_directory(tmp_path, "000001", "2026-10-13", "long", "../other-task")


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_report_roundtrip_and_task_fallback_preserve_independent_horizons(real_mongo_db):
    db = real_mongo_db
    owner = f"horizon-test-{uuid4()}"
    service = AnalysisService()
    reports = ReportsService(db)
    task_ids = [str(uuid4()) for _ in range(4)]
    horizons = ["short", "medium", "long", "long"]
    try:
        for task_id, horizon in zip(task_ids, horizons):
            await db.analysis_tasks.insert_one({
                "task_id": task_id, "user_id": owner, "symbol": "000001", "stock_name": "平安银行",
                "status": "completed", "parameters": {"analysis_horizon": horizon},
                "analysis_horizon": horizon, "created_at": datetime(2026, 10, 13),
            })
            result = {
                "analysis_horizon": horizon, "stock_symbol": "000001", "stock_name": "平安银行",
                "analysis_date": "2026-10-13", "decision": {"action": "WATCH"},
                "reports": {"market_report": f"{horizon} 报告 {task_id}"},
            }
            await service._save_analysis_result_web_style(task_id, result, owner)
            assert (await service.get_task_overview(task_id, owner))["analysis_horizon"] == horizon
            assert (await service.get_task_with_status_fallback(task_id, owner))["analysis_horizon"] == horizon
            assert (await service.get_task_result_data(task_id, owner))["analysis_horizon"] == horizon
            detail = await reports.get_report_by_task_id(task_id, owner)
            assert detail["analysis_horizon"] == horizon
            assert detail["reports"]["market_report"] == f"{horizon} 报告 {task_id}"
            assert (await reports.download_report(task_id, owner))["analysis_horizon"] == horizon
        listing = await reports.list_reports(user_id=owner)
        assert listing["total"] == 4
        assert len({r["analysis_id"] for r in listing["reports"]}) == 4
        assert {r["analysis_horizon"] for r in listing["reports"]} == {"short", "medium", "long"}
        tasks = await service.query_user_tasks(owner)
        assert {r["analysis_horizon"] for r in tasks["tasks"]} == {"short", "medium", "long"}
        # Delete only these test reports to exercise the real task-result fallback.
        await db.analysis_reports.delete_many({"task_id": {"$in": task_ids}})
        for task_id, horizon in zip(task_ids, horizons):
            assert (await service.get_task_result_data(task_id, owner))["analysis_horizon"] == horizon
            assert (await reports.get_report_by_task_id(task_id, owner))["analysis_horizon"] == horizon
    finally:
        await db.analysis_reports.delete_many({"task_id": {"$in": task_ids}})
        await db.analysis_tasks.delete_many({"task_id": {"$in": task_ids}})
        service._shutdown_pool()


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_legacy_reports_read_as_short_without_rewriting(real_mongo_db):
    db = real_mongo_db
    task_id, owner = str(uuid4()), str(uuid4())
    document = {
        "task_id": task_id, "user_id": owner, "analysis_id": f"legacy-{task_id}",
        "stock_symbol": "000001", "stock_name": "平安银行", "created_at": datetime(2026, 10, 8),
        "reports": {"market_report": "历史分析正文"},
    }
    try:
        await db.analysis_reports.insert_one(document)
        service = ReportsService(db)
        assert (await service.get_report(task_id, owner))["analysis_horizon"] == "short"
        assert (await service.list_reports(user_id=owner))["reports"][0]["analysis_horizon"] == "short"
        assert (await service.download_report(task_id, owner))["analysis_horizon"] == "short"
        stored = await db.analysis_reports.find_one({"task_id": task_id})
        assert "analysis_horizon" not in stored
        assert stored["reports"] == document["reports"]
    finally:
        await db.analysis_reports.delete_many({"task_id": task_id})


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_cross_horizon_persistence_and_existing_retry_are_rejected(real_mongo_db):
    db = real_mongo_db
    source_id, child_id, owner = str(uuid4()), str(uuid4()), str(uuid4())
    service = AnalysisService()
    try:
        await db.analysis_tasks.insert_many([
            {"task_id": source_id, "user_id": owner, "status": "failed", "analysis_horizon": "long", "retry_task_id": child_id},
            {"task_id": child_id, "user_id": owner, "status": "pending", "analysis_horizon": "short"},
        ])
        with pytest.raises(ValueError, match="分析周期"):
            await create_retry_task(service, source_id, owner)
        with pytest.raises(ValueError, match="分析周期"):
            await service._save_analysis_result_web_style(source_id, {
                "analysis_horizon": "short", "stock_symbol": "000001", "stock_name": "平安银行",
                "reports": {"market_report": "不能作为长线结果保存"},
            }, owner)
        assert await db.analysis_reports.count_documents({"task_id": source_id}) == 0
    finally:
        await db.analysis_reports.delete_many({"task_id": {"$in": [source_id, child_id]}})
        await db.analysis_tasks.delete_many({"task_id": {"$in": [source_id, child_id]}})
        service._shutdown_pool()


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_task_creation_and_owned_retry_keep_original_horizon(real_mongo_db):
    db = real_mongo_db
    owner = f"horizon-create-{uuid4()}"
    model = f"horizon-test-model-{uuid4()}"
    service = AnalysisService()
    config_id = None
    try:
        # Real local configuration resolution, with no execution/model request.
        config_id = (await db.system_configs.insert_one({
            "is_active": True, "version": 1_000_000_000,
            "llm_configs": [{"model_name": model, "provider": "openai", "enabled": True,
                             "api_key": "local-test-not-a-real-key", "suitable_roles": ["both"]}],
        })).inserted_id
        for horizon in ("short", "medium", "long"):
            request = SingleAnalysisRequest(symbol="000001", parameters=AnalysisParameters(
                analysis_horizon=horizon, selected_analysts=["market-analyst"],
                analyst_model=model, debate_model=model,
            ))
            created = await service.create_analysis_task(owner, request)
            task_id = created["task_id"]
            assert created["analysis_horizon"] == horizon
            snapshot = await db.analysis_tasks.find_one({"task_id": task_id})
            assert snapshot["analysis_horizon"] == snapshot["parameters"]["analysis_horizon"] == horizon
            assert (await service.get_task_status(task_id))["analysis_horizon"] == horizon
            await db.analysis_tasks.update_one({"task_id": task_id}, {"$set": {"status": "failed"}})
            await db.analysis_events.insert_one({
                "task_id": task_id, "seq": 1, "event_type": "report_ready",
                "payload": {"report_key": "market_report", "content": f"{horizon}周期完整报告，证据及缺项如实列明。"},
            })
            if horizon != "short":
                # Legacy adaptation reports cannot be mixed into the new research
                # workflow. A real execution records its frozen plan before roles run.
                with pytest.raises(ValueError, match="旧报告不能混用"):
                    await create_retry_task(service, task_id, owner)
                from app.engine.orchestrator.workflow.research import prepare_workflow
                spec, _, plan = prepare_workflow(
                    {**request.parameters.model_dump(), "workflow_slug": request.workflow_slug},
                    request.parameters.selected_nodes,
                )
                await db.analysis_events.insert_one({
                    "task_id": task_id, "seq": 0, "event_type": "research_workflow_snapshot",
                    "payload": {"snapshot": {**plan.snapshot(), "research_profile": {
                        "horizon": horizon, "revision": spec.research_profile.revision,
                    }}},
                })
            child, retry_request = await create_retry_task(service, task_id, owner)
            assert child["task_id"] != task_id
            assert retry_request.parameters.analysis_horizon == horizon
            assert retry_request._resume_reports["market_report"].startswith(horizon)
            reused, again = await create_retry_task(service, task_id, owner)
            assert reused["task_id"] == child["task_id"]
            assert reused["analysis_horizon"] == horizon
            assert again is None
    finally:
        task_ids = [row["task_id"] async for row in db.analysis_tasks.find({"user_id": owner}, {"task_id": 1})]
        for task_id in task_ids:
            await service.memory_manager.delete_task(task_id)
        await db.analysis_events.delete_many({"task_id": {"$in": task_ids}})
        await db.analysis_tasks.delete_many({"user_id": owner})
        if config_id is not None:
            await db.system_configs.delete_one({"_id": config_id})
        service._shutdown_pool()
