# data-access-exempt: 所有者约束与旁路观察记录；行情只经 DataInterface。
"""Opt-in structure auxiliary. Never updates reports, decisions, or Wyckoff."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from app.core.database import get_mongo_db
from app.data.core.interface import DataInterface
from app.engine.structure_auxiliary import analyze
from app.services.research_tracking import VERSIONS, ResearchTrackingService, aware

logger = logging.getLogger(__name__)
OBSERVATIONS = "research_structure_auxiliary"


class StructureAuxiliaryService:
    def __init__(self, db=None):
        self.db = db if db is not None else get_mongo_db()

    async def ensure_indexes(self):
        await self.db[OBSERVATIONS].create_index("expires_at", expireAfterSeconds=0)
        await self.db[OBSERVATIONS].create_index([("track_id", 1), ("user_id", 1), ("created_at", -1)])

    async def observe(self, track_id, user_id, *, now=None):
        now = now or datetime.now(timezone.utc)
        track = await ResearchTrackingService(self.db).owned(track_id, user_id)
        if (track.get("settings") or {}).get("structure_auxiliary_enabled") is not True:
            return {"enabled": False, "model_called": False, "decision_effect": "none", "report_mutated": False}
        version = await self.db[VERSIONS].find_one(
            {
                "_id": track.get("latest_version_id"),
                "track_id": track_id,
                "user_id": user_id,
                "baseline_analysis_id": track["baseline_analysis_id"],
            }
        )
        technical = ((version or {}).get("modules") or {}).get("technical") or {}
        review = (version or {}).get("review") or {}
        try:
            packet = await DataInterface.get_instance().structure_auxiliary_context(track["symbol"], now)
        except Exception as exc:
            logger.warning("Structure auxiliary context unavailable: %s", type(exc).__name__)
            packet = {
                "rows": [],
                "calendar": [],
                "adjustments": [],
                "daily": [],
                "index_rows": [],
                "index_symbol": "000300.SH",
                "issues": ["结构辅助取数失败，原研究保持不变"],
            }
        result = analyze(
            track["symbol"],
            packet["rows"],
            now,
            calendar=packet["calendar"],
            adjustments=packet["adjustments"],
            daily=packet["daily"],
            index_rows=packet["index_rows"],
            index_symbol=packet["index_symbol"],
            wyckoff=technical.get("wyckoff") if isinstance(technical.get("wyckoff"), dict) else None,
            held_action=review.get("held_action"),
            unheld_action=review.get("unheld_action"),
            wyckoff_as_of=(version or {}).get("as_of") or track.get("baseline_date"),
        )
        result["issues"] = [*packet["issues"], *result["issues"]]
        result.update(
            enabled=True,
            model_called=False,
            track_id=track_id,
            baseline_analysis_id=track["baseline_analysis_id"],
            source_revision=(version or {}).get("_id"),
        )
        try:
            await self.ensure_indexes()
            await self.db[OBSERVATIONS].insert_one(
                {
                    "track_id": track_id,
                    "user_id": user_id,
                    "baseline_analysis_id": track["baseline_analysis_id"],
                    "created_at": now,
                    "expires_at": now + timedelta(days=30),
                    "checked_at": aware(now),
                    "result": result,
                }
            )
        except Exception as exc:
            logger.warning("Structure auxiliary observation was not stored: %s", type(exc).__name__)
            result["issues"].append("本次旁路结果未能写入观察记录，计算响应仍返回；研究报告未改动")
        return result
