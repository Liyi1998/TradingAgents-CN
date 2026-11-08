# data-access-exempt: 所有者约束、盘中观察记录与共享取数租约；行情只经DataInterface。
"""Opt-out deterministic intraday tracking; never modifies reports or calls LLMs."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.database import get_mongo_db
from app.data.core.interface import DataInterface
from app.engine.intraday_research import CN, ENDS, ENGINE, evaluate, prepare
from app.engine.research_tracking import chart_levels, digest
from app.services.research_tracking import ResearchTrackingService, TRACKS, VERSIONS, aware
from app.engine.report_chart_levels import report_chart_levels

logger = logging.getLogger(__name__)
CACHE = "research_intraday_market"
OBSERVATIONS = "research_intraday_observations"
_gate = asyncio.Semaphore(2)


class IntradayResearchService:
    def __init__(self, db=None):
        self.db = db if db is not None else get_mongo_db()

    async def ensure_indexes(self):
        await self.db[CACHE].create_index("expires_at", expireAfterSeconds=0)
        await self.db[OBSERVATIONS].create_index("expires_at", expireAfterSeconds=0)
        await self.db[OBSERVATIONS].create_index([("track_id", 1), ("user_id", 1), ("created_at", -1)])

    async def market(self, symbol, now):
        key = f"{symbol}:{now.astimezone(CN).date().isoformat()}"
        cache = await self.db[CACHE].find_one({"_id": key})
        if cache and cache.get("result", {}).get("engine") == ENGINE and aware(cache["next_check"]) > now:
            return cache["result"]
        token = digest([key, now.isoformat()])
        try:
            lease = await self.db[CACHE].find_one_and_update(
                {"_id": key, "$or": [{"lease_until": {"$lt": now}}, {"lease_until": {"$exists": False}}]},
                {"$set": {"lease_until": now+timedelta(seconds=40), "token": token,
                          "expires_at": now+timedelta(days=3)}}, upsert=True, return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:
            lease = None
        if not lease:
            # Do not present an old successful cached state as a current observation.
            result = prepare(symbol, [], {"date": now.astimezone(CN).date().isoformat(), "is_open": True}, now)
            result["issues"] = ["分钟行情正在由另一请求刷新，请稍后查看"]
            return result
        try:
            async with _gate:
                result = await asyncio.wait_for(DataInterface.get_instance().intraday_research(symbol, now), 30)
        except Exception as exc:
            logger.warning("Intraday evidence unavailable: %s", type(exc).__name__)
            result = prepare(symbol, [], {}, now)
            result["issues"] = ["本次分钟行情或交易日历不可用；原研究继续保留"]
        local = now.astimezone(CN)
        future = [local.replace(hour=m//60, minute=m%60, second=1, microsecond=0)+timedelta(minutes=1)
                  for m in ENDS if local < local.replace(hour=m//60, minute=m%60, second=1, microsecond=0)+timedelta(minutes=1)]
        # Post-close amount/funds may arrive after the first complete minute set.
        next_check = min(future) if future else now+timedelta(minutes=5)
        if result["status"] != "ready":
            next_check = min(next_check, now+timedelta(minutes=3))
        await self.db[CACHE].update_one({"_id": key, "token": token}, {"$set": {
            "result": result, "next_check": next_check, "lease_until": now-timedelta(seconds=1)}})
        return result

    async def observe(self, track_id, user_id, *, now=None):
        now = now or datetime.now(timezone.utc)
        track = await ResearchTrackingService(self.db).owned(track_id, user_id)
        await self.ensure_indexes()
        if (track.get("settings") or {}).get("intraday_enabled", True) is False:
            return {"enabled": False, "model_review_enabled": False}
        version = await self.db[VERSIONS].find_one({"_id": track.get("latest_version_id"), "track_id": track_id,
            "user_id": user_id, "baseline_analysis_id": track["baseline_analysis_id"]})
        strategy = ((version or {}).get("review") or {}).get("strategy")
        source_date = version["as_of"] if strategy else track["baseline_date"]
        revision = version["_id"] if strategy else track["baseline_analysis_id"]
        known_at = aware(version["created_at"] if strategy else track["baseline"].get("created_at") or track["created_at"]).isoformat()
        if strategy:
            levels = chart_levels(strategy, now.astimezone(CN).date().isoformat(), aware(version["created_at"]).isoformat())
            levels = [{**level, "source_research_date": source_date} for level in levels]
        else:
            levels, _ = report_chart_levels(track["baseline"], known_at)
        conditions = strategy.get("conditions", []) if strategy and strategy["valid_until"] >= now.astimezone(CN).date().isoformat() else []
        snapshot = await self.market(track["symbol"], now)
        evaluation = evaluate(snapshot, levels, conditions)
        from app.engine.intraday_alerts import price_alerts
        from app.engine.research_price_evidence import with_price_conflicts
        content = version["review"].get("content", "") if strategy else "\n".join(track["baseline"].get("reports", {}).get(k, "") for k in ("market_report", "final_trade_decision"))
        alert_levels = with_price_conflicts(levels, content, ((version or {}).get("snapshot") or {}).get("bars", []))
        evaluation["alerts"] = price_alerts(snapshot, alert_levels, known_at=known_at)
        for item in evaluation["conditions"]:
            item.setdefault("source_date", source_date)
        scope = {"track_id": track_id, "user_id": user_id, "baseline_analysis_id": track["baseline_analysis_id"]}
        result = {"enabled": True, "model_review_enabled": False, "source_date": source_date,
                  "source_revision": revision, "baseline_analysis_id": track["baseline_analysis_id"],
                  "snapshot": snapshot, **evaluation}
        previous = await self.db[OBSERVATIONS].find_one(scope, sort=[("created_at", -1)])
        changes = []
        old = {r["id"]: r for r in (previous or {}).get("result", {}).get("conditions", [])}
        if previous and previous["result"].get("source_revision") == revision:
            for row in evaluation["conditions"]:
                prior = old.get(row["id"])
                if prior and prior["state"] != row["state"]:
                    changes.append({"label": row["label"], "from": prior["state"], "to": row["state"], "at": snapshot["as_of"] or snapshot["checked_at"]})
        # A late response after a baseline switch cannot become current.
        current = await self.db[TRACKS].find_one({"_id": track_id, "user_id": user_id,
            "baseline_analysis_id": track["baseline_analysis_id"], "latest_version_id": track.get("latest_version_id")})
        if not current:
            return {"enabled": True, "changed_basis": True, "model_review_enabled": False}
        # Publish only fresh evidence during this session. Historical replay and
        # newly adopted after-close reports remain visible without noisy pushes.
        if 10*60 <= now.astimezone(CN).hour*60+now.astimezone(CN).minute <= 15*60+10:
            from app.services.notifications_service import NotificationsService
            from app.models.notification import NotificationCreate
            for alert in evaluation["alerts"]:
                stamp = datetime.fromisoformat(alert["evidence_at"]).replace(tzinfo=CN)
                if timedelta(0) <= now-stamp <= timedelta(minutes=10):
                    try:
                        await NotificationsService(db=self.db).create_and_publish(NotificationCreate(
                            user_id=user_id, type="alert", severity="warning", source="intraday_research",
                            title=f"{track['symbol']} {alert['title']}：{alert['label']}",
                            content=f"{alert['lower']}–{alert['upper']}元；证据时点{alert['evidence_at']}。{alert['message']}",
                            link=f"/analysis/research?symbol={track['symbol']}",
                            metadata={"track_id": track_id, "revision": revision, **alert}),
                            dedupe_key=digest([user_id, track_id, revision, alert["id"]]))
                    except Exception as exc:
                        logger.warning("Intraday notification unavailable: %s", type(exc).__name__)
        identity = digest({**scope, "revision": revision, "date": snapshot["date"], "as_of": snapshot["as_of"],
            "bars": snapshot["bars"], "status": snapshot["status"], "issues": snapshot["issues"], "evaluation": evaluation})
        await self.db[OBSERVATIONS].update_one({"_id": identity}, {"$setOnInsert": {
            **scope, "created_at": now, "expires_at": now+timedelta(days=30), "result": result, "changes": changes}}, upsert=True)
        await self.db[TRACKS].update_one({"_id": track_id, "user_id": user_id}, {"$set": {"intraday_checked_at": now}})
        history = await self.db[OBSERVATIONS].find(scope, {"created_at": 1, "changes": 1, "result.snapshot.as_of": 1,
            "result.snapshot.status": 1, "result.source_date": 1, "result.source_revision": 1}).sort("created_at", -1).limit(20).to_list(20)
        result["history"] = [{"id": r["_id"], "created_at": r["created_at"], "changes": r.get("changes", []),
            "as_of": r["result"]["snapshot"]["as_of"], "status": r["result"]["snapshot"]["status"],
            "source_date": r["result"]["source_date"]} for r in history]
        for item in result["history"]:
            item["created_at"] = aware(item["created_at"]).isoformat()
        return result


async def run_intraday_tracking():
    """Bounded scan. Market fetches share a per-symbol lease/cache across owners."""
    now = datetime.now(timezone.utc)
    local = now.astimezone(CN)
    if local.weekday() >= 5 or not (10*60 <= local.hour*60+local.minute <= 15*60+10):
        return
    service = IntradayResearchService()
    await service.ensure_indexes()
    # Old tracks inherit the read-only feature; it can be disabled independently
    # of the existing paid 19:00 update setting. No changes to model budgets.
    tracks = await service.db[TRACKS].find({"settings.intraday_enabled": {"$ne": False}},
        {"user_id": 1}).sort("intraday_checked_at", 1).limit(100).to_list(100)
    for track in tracks:
        if (datetime.now(timezone.utc)-now).total_seconds() > 45:
            break
        try:
            await service.observe(track["_id"], track["user_id"])
        except Exception as exc:
            logger.warning("Intraday track unavailable: %s", type(exc).__name__)
