# data-access-exempt: 所有者约束与旁路观察记录；行情只经 DataInterface。
"""Opt-in structure auxiliary. Never updates reports, decisions, or Wyckoff."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path

from app.core.config import settings
from app.core.database import get_mongo_db
from app.data.core.interface import DataInterface
from app.engine.structure_auxiliary import analyze
from app.services.research_tracking import VERSIONS, ResearchTrackingService, aware

logger = logging.getLogger(__name__)
OBSERVATIONS = "research_structure_auxiliary"


def synthetic_fixture_packet(symbol: str, root: str) -> dict | None:
    """Return a synthetic packet when an isolated fixture directory is configured.

    An empty directory setting keeps the live single-symbol path. A configured
    directory never falls through to an external quote request.
    """
    if not str(root or "").strip():
        return None
    path = Path(root) / f"{symbol}.json"
    empty = {
        "rows": [],
        "calendar": [],
        "adjustments": [],
        "daily": [],
        "index_rows": [],
        "index_symbol": "000300.SH",
        "issues": ["隔离合成目录已启用，但没有该代码的合成文件，因此不请求外部行情"],
        "evidence_origin": "synthetic_test",
    }
    if not path.is_file():
        return empty
    packet = json.loads(path.read_text(encoding="utf-8"))
    packet["evidence_origin"] = "synthetic_test"
    packet["issues"] = ["合成测试行情，不是交易所回报", *list(packet.get("issues") or [])]
    return packet


class StructureAuxiliaryService:
    def __init__(self, db=None):
        self.db = db if db is not None else get_mongo_db()

    async def ensure_indexes(self):
        await self.db[OBSERVATIONS].create_index("expires_at", expireAfterSeconds=0)
        await self.db[OBSERVATIONS].create_index([("track_id", 1), ("user_id", 1), ("created_at", -1)])

    async def load_context(self, symbol, now):
        """Live single-symbol read. Tests replace this method; the algorithm stays real."""
        fixture = synthetic_fixture_packet(symbol, settings.STRUCTURE_AUXILIARY_SYNTHETIC_DIR)
        if fixture is not None:
            return fixture
        try:
            packet = await DataInterface.get_instance().structure_auxiliary_context(symbol, now)
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
                "evidence_origin": "live_fetch_empty",
            }
        packet.setdefault("evidence_origin", "live_free_single_symbol" if packet.get("rows") else "live_fetch_empty")
        return packet

    async def observe(self, track_id, user_id, *, now=None):
        now = now or datetime.now(timezone.utc)
        track = await ResearchTrackingService(self.db).owned(track_id, user_id)
        if (track.get("settings") or {}).get("structure_auxiliary_enabled") is not True:
            return {
                "enabled": False,
                "model_called": False,
                "decision_effect": "none",
                "report_mutated": False,
                "synthetic": False,
            }
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
        packet = await self.load_context(track["symbol"], now)
        origin = packet.get("evidence_origin")
        if origin not in {"live_free_single_symbol", "live_fetch_empty", "synthetic_test"}:
            origin = "unspecified"
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
            synthetic=origin == "synthetic_test",
            evidence_origin=origin,
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
