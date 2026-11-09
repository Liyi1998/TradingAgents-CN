# data-access-exempt: 应用层 Jev 试验记录、所有者与预算；行情只经 DataInterface。
"""Explicit, owner-scoped paper experiments, isolated from analysis and trading.

Preview may fetch ONE symbol through DataInterface; GET never fetches or calls.
A stored preview expires and cannot be silently refreshed during model invocation.
No scheduler, task hook, broker, report mutation or retry path is installed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.database import get_mongo_db
from app.data.core.interface import DataInterface
from app.llm.jev_conditions import CONFIG_PATH, digest, load_condition_config
from app.llm.jev_decisions import (
    POLICY, ExperimentError, StrategyInput, closed_sessions, daily_snapshot,
    evaluate_decision, make_packet, prepare_strategy, source_revision,
)
from app.services.jev_condition_service import reserve_daily_call

logger = logging.getLogger(__name__)
PREVIEWS = "jev_decision_previews"
RUNS = "jev_decision_experiments"


async def read_snapshot(symbol: str, now: datetime) -> tuple[dict, list[str]]:
    di = DataInterface.get_instance()
    day = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    try:
        async with asyncio.timeout(20):
            # No market-wide/calendar refresh side effect on this manual path.
            # Missing calendar is explicit; existing ingestion maintains it.
            calendar = await di.read("CN", "trade_calendar", start_date=(day-timedelta(days=45)).isoformat(), end_date=day.isoformat())
            sessions = closed_sessions(calendar.get("data") or [], now)
            if not sessions:
                return {}, ["CALENDAR_UNAVAILABLE"]
            result = await di.read_ready("CN", "daily_quotes", symbol, timeout=12,
                start_date=sessions[0], end_date=sessions[-1],
                filters={"price_basis": "raw", "period": "daily", "expected_dates": sessions,
                         "required_fields": ["open", "high", "low", "close", "volume"]})
            return daily_snapshot(result, symbol, sessions)
    except Exception:
        return {}, ["DATA_UNAVAILABLE"]


async def _owned_report(db, analysis_id: str, user_id: str) -> dict:
    document = await db.analysis_reports.find_one({"analysis_id": analysis_id, "user_id": user_id, "status": "completed"})
    if not document:
        raise ExperimentError("没有可访问的完整报告")
    return document


def _public(row: dict) -> dict:
    # No user IDs, raw responses, keys, arbitrary exception text or confidence-as-win-rate.
    fields = ("status", "reason", "guards", "packet", "result", "model", "usage", "external_call_attempted",
        "created_at", "expires_at", "finished_at", "elapsed_seconds", "baseline", "affects_decision", "model_call_enabled")
    return {"id": str(row["_id"]), **{k: row[k] for k in fields if k in row}}


async def preview_experiment(analysis_id: str, user_id: str, request: StrategyInput, *, db=None) -> dict:
    db = db if db is not None else get_mongo_db()
    document = await _owned_report(db, analysis_id, user_id)
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    strategy = prepare_strategy(document, request, local.date())
    snapshot, guards = await read_snapshot(strategy["symbol"], now)
    packet = make_packet(strategy, snapshot)
    # A preview spanning the daily-close boundary cannot send an older state.
    close_boundary = local.replace(hour=15, minute=10, second=0, microsecond=0)
    if close_boundary <= local:
        close_boundary += timedelta(days=1)
    strategy_expiry = datetime.fromisoformat(strategy["valid_until"]).replace(tzinfo=ZoneInfo("Asia/Shanghai")) + timedelta(days=1)
    expiry = min(now + timedelta(minutes=10), close_boundary.astimezone(timezone.utc), strategy_expiry.astimezone(timezone.utc))
    try:
        config = load_condition_config()
        model_call_enabled = bool(config.enabled and config.external_calls_enabled and config.decision_experiments_enabled and config.api_key.get_secret_value().strip())
    except Exception:
        model_call_enabled = False
    row = {"_id": uuid4().hex, "analysis_id": analysis_id, "user_id": user_id,
        "policy": POLICY, "source_revision": source_revision(document), "packet": packet,
        "created_at": now, "expires_at": expiry, "status": "blocked" if guards else "preview",
        "guards": guards, "external_call_attempted": False, "affects_decision": False,
        "model_call_enabled": model_call_enabled,
        "baseline": {"entry_action": "WAIT", "existing_action": "RESEARCH_REQUIRED",
            "reason": "规则仅核对数据和既有上限，不推断文本条件已经成立"}}
    await db[PREVIEWS].insert_one(row)
    return _public(row)


async def get_experiments(analysis_id: str, user_id: str, *, db=None) -> list[dict]:
    db = db if db is not None else get_mongo_db()
    await _owned_report(db, analysis_id, user_id)
    rows = await db[RUNS].find({"analysis_id": analysis_id, "user_id": user_id}).sort("created_at", -1).limit(10).to_list(10)
    now = datetime.now(timezone.utc)
    for row in rows:
        if row.get("status") == "running" and _aware(row["deadline_at"]) < now:
            row.update(status="incomplete", reason="调用或保存中断，不自动重试")
    return [_public(r) for r in rows]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def run_experiment(analysis_id: str, user_id: str, preview_id: str, confirm_external: bool,
                         *, db=None, config_path: Path = CONFIG_PATH) -> dict:
    if not confirm_external:
        raise ExperimentError("需要明确确认本次外发和可能产生的模型用量")
    db = db if db is not None else get_mongo_db()
    document = await _owned_report(db, analysis_id, user_id)
    preview = await db[PREVIEWS].find_one({"_id": preview_id, "analysis_id": analysis_id, "user_id": user_id})
    now = datetime.now(timezone.utc)
    if not preview or preview["status"] != "preview" or _aware(preview["expires_at"]) <= now:
        raise ExperimentError("预览不可用或已过期，请重新获取状态并确认")
    if preview.get("policy") != POLICY:
        raise ExperimentError("预览校验规则已更新，请重新预览并确认外发材料")
    if preview["packet"]["strategy"]["valid_until"] < now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat():
        raise ExperimentError("策略有效期已过，请重新研究并确认")
    if preview["source_revision"] != source_revision(document):
        raise ExperimentError("原报告已变化，请重新确认策略")
    config = load_condition_config(config_path)
    if not (config.enabled and config.external_calls_enabled and config.decision_experiments_enabled):
        raise ExperimentError("动作试验或外发开关尚未启用")
    if not config.api_key.get_secret_value().strip():
        raise ExperimentError("Jev 密钥不可用")
    # Same exact strategy/state is at-most-once even across tabs/previews.
    record_id = digest({"analysis_id": analysis_id, "user_id": user_id, "policy": POLICY,
                        "packet": preview["packet"], "source_revision": preview["source_revision"]})
    row = {**preview, "_id": record_id, "status": "running", "created_at": now,
           "deadline_at": now+timedelta(seconds=25), "preview_id": preview_id}
    try:
        await db[RUNS].insert_one(row)
    except DuplicateKeyError:
        existing = await db[RUNS].find_one({"_id": record_id, "user_id": user_id})
        if existing["status"] == "running" and _aware(existing["deadline_at"]) < now:
            existing.update(status="incomplete", reason="调用或保存中断，不自动重试")
        return _public(existing)
    started = time.monotonic()
    try:
        async with asyncio.timeout(23):
            latest = load_condition_config(config_path)
            if latest != config:
                result = {"status": "not_called", "reason": "配置已变化，未调用"}
            elif not await reserve_daily_call(db, config.max_calls_per_day):
                result = {"status": "not_called", "reason": "已达共享日调用上限，未调用"}
            else:
                current = await _owned_report(db, analysis_id, user_id)
                if load_condition_config(config_path) != config or source_revision(current) != preview["source_revision"]:
                    result = {"status": "not_called", "reason": "发送前配置或报告变化，已停止；预留尝试次数不退回"}
                elif _aware(preview["expires_at"]) <= datetime.now(timezone.utc):
                    result = {"status": "not_called", "reason": "发送前预览已过期，已停止；预留尝试次数不退回"}
                else:
                    # Mark before the one outbound request; failures remain non-retriable.
                    await db[RUNS].update_one({"_id": record_id}, {"$set": {"external_call_attempted": True}})
                    result = {"status": "completed", **await evaluate_decision(config, preview["packet"])}
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Jev decision experiment unavailable; no original analysis changed")
        result = {"status": "unavailable", "reason": "模型连接、响应或校验失败；不自动重试，原裁决不变"}
    result.update(finished_at=datetime.now(timezone.utc), elapsed_seconds=round(time.monotonic()-started, 3))
    saved = await db[RUNS].find_one_and_update({"_id": record_id}, {"$set": result}, return_document=ReturnDocument.AFTER)
    return _public(saved)
