# data-access-exempt: 应用层 Jev 核验记录、所有者与预算；行情只经 DataInterface。
"""Bounded automatic post-adjudication sidecar; never changes the saved verdict."""
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import time

from pymongo.errors import DuplicateKeyError

from app.core.database import get_mongo_db
from app.llm.jev_conditions import CONFIG_PATH, digest, load_condition_config
from app.llm.jev_decisions import source_revision
from app.llm.jev_final_action import POLICY, build_final_packet, evaluate_final_action
from app.services.jev_condition_service import record_condition_check, reserve_daily_call, original_reference, _tasks

logger = logging.getLogger(__name__)
COLLECTION = "jev_final_actions"


def final_source_revision(document):
    return digest({"report": source_revision(document), "partition": document.get("strategy_partition")})


def schedule_jev_after_report(db, report_id, *, config_path: Path = CONFIG_PATH):
    if len(_tasks) >= 4:
        logger.warning("Jev final observer capacity exhausted; original report unaffected")
        return
    task = asyncio.create_task(dispatch_jev_after_report(db, report_id, config_path=config_path))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def dispatch_jev_after_report(db, report_id, *, config_path: Path = CONFIG_PATH):
    try:
        config = load_condition_config(config_path)
        if config.final_action_enabled:
            return await record_final_action(db, report_id, config_path=config_path)
        # Exactly one automatic path: old behavior remains until separately enabled.
        return await record_condition_check(db, report_id, config_path=config_path)
    except Exception:
        logger.warning("Jev observer unavailable; original report unaffected")
        return None


async def record_final_action(db, report_id, *, config_path: Path = CONFIG_PATH):
    record_id = digest({"report_id": str(report_id), "policy": POLICY})
    inserted = False
    started = time.monotonic()
    try:
        async with asyncio.timeout(25):
            config = load_condition_config(config_path)
            if not (config.enabled and config.final_action_enabled):
                return None
            doc = await db.analysis_reports.find_one({"_id": report_id})
            if not doc or doc.get("status") != "completed" or not doc.get("user_id"):
                return None
            packet, guards = build_final_packet(doc)
            now = datetime.now(timezone.utc)
            row = {"_id": record_id, "analysis_id": doc.get("analysis_id"), "user_id": str(doc["user_id"]),
                   "policy": POLICY, "status": "running", "created_at": now,
                   "deadline_at": now + timedelta(seconds=25), "packet": packet,
                   "source_revision": final_source_revision(doc), "input_guards": guards,
                   "original_reference": original_reference(doc), "analysis_date": doc.get("analysis_date"),
                   "external_call_attempted": False, "affects_decision": False}
            try:
                await db[COLLECTION].insert_one(row)
                inserted = True
            except DuplicateKeyError:
                return record_id
            if guards:
                result = {"status": "skipped", "reason": "INPUT_NOT_READY"}
            elif not config.external_calls_enabled:
                result = {"status": "not_called", "reason": "EXTERNAL_DISABLED"}
            elif not config.api_key.get_secret_value().strip():
                result = {"status": "not_called", "reason": "KEY_MISSING"}
            elif load_condition_config(config_path) != config:
                result = {"status": "not_called", "reason": "CONFIG_CHANGED"}
            elif not await reserve_daily_call(db, config.max_calls_per_day):
                result = {"status": "not_called", "reason": "DAILY_LIMIT"}
            else:
                current = await db.analysis_reports.find_one({"_id": report_id})
                if not current or load_condition_config(config_path) != config or final_source_revision(current) != row["source_revision"]:
                    result = {"status": "not_called", "reason": "SOURCE_OR_CONFIG_CHANGED"}
                else:
                    await db[COLLECTION].update_one({"_id": record_id}, {"$set": {"external_call_attempted": True}})
                    result = {"status": "completed", **await evaluate_final_action(config, packet)}
            result.update(finished_at=datetime.now(timezone.utc), elapsed_seconds=round(time.monotonic()-started, 3))
            await db[COLLECTION].update_one({"_id": record_id}, {"$set": result})
            return record_id
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Jev final action failed; no retry, original report unaffected")
        if inserted:
            try:
                async with asyncio.timeout(2):
                    await db[COLLECTION].update_one({"_id": record_id}, {"$set": {
                        "status": "unavailable", "reason": "CALL_OR_VALIDATION_FAILED",
                        "finished_at": datetime.now(timezone.utc)}})
            except Exception:
                logger.warning("Jev final result could not be saved")
        return record_id if inserted else None


async def get_final_action(analysis_id: str, user_id: str, *, db=None):
    db = db if db is not None else get_mongo_db()
    doc = await db.analysis_reports.find_one({"analysis_id": analysis_id, "user_id": user_id}, {"_id": 1})
    if not doc:
        return None
    row = await db[COLLECTION].find_one({"_id": digest({"report_id": str(doc["_id"]), "policy": POLICY}), "user_id": user_id})
    if not row:
        return None
    if row["status"] == "running":
        deadline = row["deadline_at"]
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline < datetime.now(timezone.utc):
            row = {**row, "status": "incomplete", "reason": "INTERRUPTED"}
    keys = ("status", "reason", "analysis_date", "created_at", "finished_at", "elapsed_seconds",
            "original_reference", "suggested", "effective", "guards", "input_guards", "model",
            "external_call_attempted", "affects_decision")
    return {k: row[k] for k in keys if k in row}
