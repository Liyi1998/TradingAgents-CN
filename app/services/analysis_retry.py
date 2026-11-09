"""Owner-checked, same-day resumption using complete persisted analyst reports."""
# data-access-exempt: 应用层 analysis_tasks/analysis_events 的归属校验与重试租约；不读取行情业务数据。

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.core.database import get_mongo_db
from app.engine.orchestrator.pipeline import _is_invalid_report_content
from app.llm.events import REPORT_CONTENT_MAX_CHARS
from app.models.analysis import AnalysisParameters, SingleAnalysisRequest
from app.services.analysis_horizon import horizon_from_snapshot, require_matching_horizon

CN = ZoneInfo("Asia/Shanghai")


def reusable_reports(events: list, selected_keys: set[str]) -> dict[str, str]:
    """Newest event wins. A truncated event must never stand in for a full report."""
    found, seen = {}, set()
    for event in sorted(events, key=lambda e: e.get("seq", 0), reverse=True):
        payload = event.get("payload") or {}
        key, content = payload.get("report_key"), payload.get("content")
        if key not in selected_keys or key in seen:
            continue
        seen.add(key)
        if not _is_invalid_report_content(content) and len(content) < REPORT_CONTENT_MAX_CHARS:
            found[key] = content
    return found


def same_day_parameters(source: dict, now: datetime) -> AnalysisParameters:
    snapshot = dict(source.get("parameters") or {})
    horizon = horizon_from_snapshot(source)
    if "analysis_horizon" in snapshot:
        require_matching_horizon(source, snapshot)
    snapshot["analysis_horizon"] = horizon
    parameters = AnalysisParameters.model_validate(snapshot)
    date = parameters.analysis_date
    if date is None:
        created = source.get("created_at")
        if not isinstance(created, datetime):
            raise ValueError("原任务缺少分析日期，请重新发起完整分析")
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        day = created.astimezone(CN).date()
    else:
        day = date.astimezone(CN).date() if date.tzinfo else date.date()
    if day != now.astimezone(CN).date():
        raise ValueError("原任务已跨分析日期，不能复用旧报告作为今日分析，请重新发起完整分析")
    parameters.analysis_date = datetime.combine(day, datetime.min.time())
    return parameters


async def create_retry_task(service, source_task_id: str, user_id: str):
    """Return (task data, request or None). None means an existing child is reused."""
    db = get_mongo_db()
    # Ownership is checked in the query, before reading any source report content.
    source = await db.analysis_tasks.find_one({"task_id": source_task_id, "user_id": user_id})
    if not source:
        raise PermissionError("任务不存在或无权访问")
    if source.get("status") != "failed":
        raise ValueError("仅失败任务可以重试；正在运行或完成的任务不重复提交")
    child_id = source.get("retry_task_id")
    if child_id:
        child = await db.analysis_tasks.find_one({"task_id": child_id, "user_id": user_id})
        if child:
            require_matching_horizon(source, child)
            return {"task_id": child_id, "status": child.get("status"), "existing": True,
                    "analysis_horizon": horizon_from_snapshot(child),
                    "reused_report_keys": child.get("reused_report_keys", [])}, None

    now = datetime.now(timezone.utc)
    parameters = same_day_parameters(source, now)
    if parameters.analysis_horizon != "short":
        import asyncio
        from app.engine.orchestrator.workflow.research import prepare_workflow
        selected_nodes = (parameters.selected_nodes if parameters.selected_nodes is not None
                          else parameters.selected_analysts)
        _, current_hash, _ = await asyncio.to_thread(
            prepare_workflow, {**parameters.model_dump(),
                               "workflow_slug": source.get("parameters", {}).get("workflow_slug")}, selected_nodes,
        )
        snapshot = source.get("workflow_snapshot") or {}
        if not snapshot:
            snapshot_event = await db.analysis_events.find_one(
                {"task_id": source_task_id, "event_type": "research_workflow_snapshot"},
                sort=[("seq", -1)],
            )
            snapshot = ((snapshot_event or {}).get("payload") or {}).get("snapshot") or {}
        if snapshot.get("spec_hash") != current_hash or not snapshot.get("research_profile"):
            raise ValueError("中长线研究流程或提示词已更新，旧报告不能混用，请重新发起完整分析")
    from app.engine.agents.analysts.dynamic_analyst import DynamicAnalystFactory

    lookup = DynamicAnalystFactory.build_lookup_map()
    selected = set()
    for name in (parameters.selected_nodes if parameters.selected_nodes is not None else parameters.selected_analysts):
        slug = (lookup.get(name) or {}).get("slug") or name
        selected.add(slug.replace("-analyst", "").replace("-", "_") + "_report")
    events = await db.analysis_events.find(
        {"task_id": source_task_id, "event_type": "report_ready"},
        {"_id": 0, "seq": 1, "payload.report_key": 1, "payload.content": 1},
    ).sort("seq", -1).limit(200).to_list(length=200)
    reports = reusable_reports(events, selected)
    if not reports:
        raise ValueError("没有完整且有效的分析师报告可复用，请重新发起完整分析")

    # A short DB lease prevents double-click/concurrent API submissions. A crashed
    # request cannot leave a permanent lock; no brokerage or existing report is touched.
    claim = str(uuid4())
    claimed = await db.analysis_tasks.find_one_and_update(
        {"task_id": source_task_id, "user_id": user_id, "status": "failed",
         "retry_task_id": child_id,
         "$or": [{"retry_claim_until": {"$exists": False}}, {"retry_claim_until": {"$lt": now}}]},
        {"$set": {"retry_claim": claim, "retry_claim_until": now + timedelta(minutes=2)}},
    )
    if not claimed:
        raise ValueError("重试任务正在创建或已经创建，请刷新任务详情后查看")
    try:
        request = SingleAnalysisRequest(
            symbol=source.get("symbol") or source.get("stock_symbol") or source.get("stock_code"),
            parameters=parameters,
            workflow_slug=source.get("parameters", {}).get("workflow_slug"),
        )
        request._resume_reports = reports
        request._resume_source_task_id = source_task_id
        result = await service.create_analysis_task(user_id, request)
        # Do not schedule a child that failed to persist (legacy create_task may swallow I/O errors).
        persisted = await db.analysis_tasks.find_one(
            {"task_id": result["task_id"], "user_id": user_id},
            {"analysis_horizon": 1, "parameters.analysis_horizon": 1},
        )
        if not persisted:
            raise RuntimeError("重试任务未成功保存，未启动分析")
        require_matching_horizon(source, persisted)
        await db.analysis_tasks.update_one(
            {"task_id": source_task_id, "retry_claim": claim},
            {"$set": {"retry_task_id": result["task_id"]}},
        )
        result.update(reused_report_keys=list(reports), existing=False)
        return result, request
    finally:
        await db.analysis_tasks.update_one(
            {"task_id": source_task_id, "retry_claim": claim},
            {"$unset": {"retry_claim": "", "retry_claim_until": ""}},
        )
