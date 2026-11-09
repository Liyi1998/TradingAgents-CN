"""Independent, bounded post-save shadow records. Never writes reports/tasks/decisions."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pymongo.errors import DuplicateKeyError

from app.llm.jev_conditions import (
    CONFIG_PATH, POLICY, build_packet, condition_questions, digest,
    evaluate_conditions, load_condition_config,
)

logger = logging.getLogger(__name__)
COLLECTION = "jev_condition_checks"
_tasks: set[asyncio.Task] = set()
MAX_PENDING = 4


def schedule_condition_check(db, report_id, *, config_path: Path = CONFIG_PATH) -> None:
    """No config/DB/network I/O on the analysis completion path; bounded memory."""
    if len(_tasks) >= MAX_PENDING:
        logger.warning("Jev condition shadow capacity exhausted; original analysis unaffected")
        return
    task = asyncio.create_task(record_condition_check(db, report_id, config_path=config_path))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def original_reference(document: dict) -> dict:
    """Local comparison only. Never infer account state or use it in outgoing input."""
    decision = document.get("decision") or {}
    allowed = {"WATCH", "BUY", "SELL", "HOLD", "REDUCE", "观望", "买入", "卖出", "持有", "减仓"}
    return {k: decision[k] for k in ("action", "unheld_action", "held_action") if decision.get(k) in allowed}


async def reserve_daily_call(db, limit: int) -> bool:
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    coll = db.jev_condition_budgets
    try:
        await coll.update_one({"_id": day}, {"$setOnInsert": {"attempts": 0}}, upsert=True)
    except DuplicateKeyError:
        pass
    result = await coll.update_one({"_id": day, "attempts": {"$lt": limit}}, {"$inc": {"attempts": 1}})
    return result.modified_count == 1


async def record_condition_check(db, report_id, *, config_path: Path = CONFIG_PATH) -> str | None:
    """At most once per saved report/policy. A crash is not silently retried/billed."""
    record_id = digest({"report_id": str(report_id), "policy": POLICY})
    started = time.monotonic()
    coll = db[COLLECTION]
    inserted = False
    try:
        async with asyncio.timeout(25):
            config = load_condition_config(config_path)
            if not config.enabled:
                return None
            document = await db.analysis_reports.find_one({"_id": report_id})
            if not document or document.get("status") != "completed" or not document.get("user_id"):
                return None
            packet, guards = build_packet(document)
            now = datetime.now(timezone.utc)
            record = {"_id": record_id, "report_id": str(report_id),
                "analysis_id": document.get("analysis_id"), "user_id": str(document["user_id"]),
                "mode": "shadow", "policy_version": POLICY, "status": "running",
                "created_at": now, "deadline_at": now + timedelta(seconds=25),
                "input": packet, "input_sha256": digest(packet), "questions": condition_questions(),
                "requested_model": config.model, "input_guards": guards,
                "original_reference": original_reference(document),
                "rule_baseline": {"condition_status": "insufficient_evidence",
                    "reason_codes": guards or ["SEMANTIC_CONDITIONS_REQUIRE_REVIEW"]},
                "external_call_attempted": False, "affects_decision": False,
                "limitations": ["模型置信度不是胜率；仅检查所给文本，不核验外部事实。",
                    "纯规则基线只核验输入完整性，不将未知条件判为不满足。"]}
            try:
                await coll.insert_one(record)
                inserted = True
            except DuplicateKeyError:
                return record_id
            if guards:
                result = {"status": "skipped", "reason": "insufficient_input",
                    "result": {"condition_status": "insufficient_evidence", "evidence_conflict": "unknown", "reason_codes": guards}}
            elif not config.external_calls_enabled:
                result = {"status": "not_called", "reason": "external_calls_disabled"}
            elif not config.api_key.get_secret_value().strip():
                result = {"status": "not_called", "reason": "key_missing"}
            else:
                # Re-read immediately before sending; disabling never triggers a retry.
                latest = load_condition_config(config_path)
                if not latest.enabled or not latest.external_calls_enabled or latest != config:
                    result = {"status": "not_called", "reason": "configuration_changed"}
                elif not await reserve_daily_call(db, config.max_calls_per_day):
                    result = {"status": "not_called", "reason": "daily_limit"}
                else:
                    await coll.update_one({"_id": record_id}, {"$set": {"external_call_attempted": True}})
                    async with asyncio.timeout(config.timeout_seconds):
                        response = await evaluate_conditions(config, packet)
                    result = {"status": "completed", **response}
            result["finished_at"] = datetime.now(timezone.utc)
            result["elapsed_seconds"] = round(time.monotonic() - started, 3)
            await coll.update_one({"_id": record_id}, {"$set": result})
    except asyncio.CancelledError:
        # Shutdown records stay visibly incomplete. Never retry a possibly billed request.
        raise
    except Exception as exc:
        logger.warning("Jev condition shadow unavailable (%s); original analysis unaffected", type(exc).__name__)
        # Only our constant error codes may be persisted, never provider bodies.
        code = str(exc)
        reason = code if re.fullmatch(r"condition_(?:http_\d{3}|model_version_changed|answers_inconsistent|answer_keys_invalid|key_missing|external_calls_disabled)", code) else type(exc).__name__
        if inserted:
            try:
                async with asyncio.timeout(2):
                    await coll.update_one({"_id": record_id}, {"$set": {"status": "unavailable",
                        "reason": reason, "finished_at": datetime.now(timezone.utc),
                        "elapsed_seconds": round(time.monotonic() - started, 3)}})
            except Exception:
                logger.warning("Jev condition shadow result could not be saved")
    return record_id if inserted else None


async def get_condition_check(db, analysis_id: str, user_id: str) -> dict | None:
    """Owner-scoped read, no legacy migrations, resends or report modification."""
    document = await db.analysis_reports.find_one({"analysis_id": analysis_id, "user_id": str(user_id)}, {"_id": 1})
    if not document:
        return None
    record_id = digest({"report_id": str(document["_id"]), "policy": POLICY})
    row = await db[COLLECTION].find_one({"_id": record_id, "user_id": str(user_id)})
    if row and row.get("status") == "running":
        deadline = row["deadline_at"]
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline < datetime.now(timezone.utc):
            row["status"] = "incomplete"
            row["reason"] = "interrupted_or_result_not_saved_no_auto_retry"
    return row
