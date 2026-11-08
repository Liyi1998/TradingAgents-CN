# data-access-exempt: 应用层缠论计算缓存及所有者校验；行情只经 DataInterface。
"""Bounded deterministic chart sidecar; no model calls or report mutations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import logging

from app.core.database import get_mongo_db
from app.engine.chan_structure import ENGINE, PARAMETERS, calculate, fingerprint, prepare, preview

COLLECTION = "research_chan_snapshots"
logger = logging.getLogger(__name__)
_compute_gate = asyncio.Semaphore(2)


def unavailable(message):
    return {
        "engine": ENGINE,
        "status": "unavailable",
        "notice": message,
        "fractals": [],
        "strokes": [],
        "zones": [],
        "preview": None,
        "limitations": [],
    }


class ChanStructureService:
    def __init__(self, db=None):
        self.db = db

    async def build(self, symbol, market, *, now=None):
        """Input is an internal verified chart/snapshot, never client-supplied bars."""
        now = now or datetime.now(timezone.utc)
        bars, context, as_of = market.get("bars") or [], market.get("chan_context") or {}, market.get("as_of")
        try:
            clean, _, _ = prepare(symbol, bars, context, as_of)
        except (ValueError, TypeError, KeyError) as exc:
            return unavailable(str(exc))
        payload = {
            "engine": ENGINE,
            "parameters": PARAMETERS,
            "symbol": symbol,
            "bars": clean,
            "as_of": as_of,
            "context": {k: v for k, v in context.items() if k != "next_session"},
        }
        key = "chan:" + fingerprint(payload)
        db = None
        row, cache_warning = None, False
        try:
            db = self.db if self.db is not None else get_mongo_db()
            row = await asyncio.wait_for(db[COLLECTION].find_one({"_id": key}), 2)
        except Exception:
            cache_warning = True
            logger.warning("Chan snapshot cache unavailable")
        try:
            async with _compute_gate:
                if row:
                    result = deepcopy(row["result"])
                else:
                    result = await asyncio.wait_for(asyncio.to_thread(calculate, symbol, bars, context, as_of), 5)
                    result.update(snapshot_id=key, input_hash=key[5:], recorded_at=now.isoformat())
                    if not cache_warning:
                        try:
                            prior = await asyncio.wait_for(
                                db[COLLECTION].find_one(
                                    {"symbol": symbol, "engine": ENGINE}, sort=[("created_at", -1), ("_id", -1)]
                                ),
                                2,
                            )
                            result["supersedes_snapshot_id"] = prior["_id"] if prior else None
                            if prior:
                                old = prior["result"]
                                result["revision_reason"] = (
                                    "new_session"
                                    if old["as_of"] != result["as_of"]
                                    else "source_changed"
                                    if old["data_source"] != result["data_source"]
                                    else "input_revised"
                                )
                            # Immutable content-addressed observations. A repeated GET never
                            # advances the recorded time or overwrites an earlier observation.
                            await asyncio.wait_for(
                                db[COLLECTION].update_one(
                                    {"_id": key},
                                    {
                                        "$setOnInsert": {
                                            "symbol": symbol,
                                            "engine": ENGINE,
                                            "created_at": now,
                                            "input": payload,
                                            "result": result,
                                        }
                                    },
                                    upsert=True,
                                ),
                                2,
                            )
                            saved = await asyncio.wait_for(db[COLLECTION].find_one({"_id": key}), 2)
                            result = deepcopy(saved["result"])
                        except Exception:
                            cache_warning = True
                            logger.warning("Chan snapshot could not be persisted")
                result["preview"] = await asyncio.wait_for(
                    asyncio.to_thread(preview, symbol, bars, context, as_of, market.get("live_bar"), result), 5
                )
        except (ImportError, OSError):
            logger.warning("Pinned Chan native core is unavailable")
            return unavailable("缠论计算核心尚未安装或版本不匹配；行情及原研究继续显示")
        except TimeoutError:
            return unavailable("缠论结构计算超时；行情及原研究继续显示")
        except Exception:
            logger.exception("Chan chart computation failed")
            return unavailable("缠论结构计算暂未完成；行情及原研究继续显示")
        if cache_warning:
            result["limitations"].append("本次缠论快照未确认保存，不能作为已留存的历史版本。")
            result["persisted"] = False
        else:
            result["persisted"] = True
        result["notice"] = f"日线结构截至 {result['as_of']}，随行情计算，不消耗模型额度。"
        return result
