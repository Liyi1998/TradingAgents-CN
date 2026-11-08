# data-access-exempt: 应用层研究版本、所有者、预算及任务租约；行情只经 DataInterface。
"""Incremental research sidecar; never rewrites analysis reports or starts trades.

Only explicit POST or an opted-in track may fetch data/call models. Reads are
owner-scoped and side-effect free. Successful module submissions are durable
checkpoints; a failed later module never discards them.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import logging
from uuid import uuid4
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.database import get_mongo_db
from app.data.query.research_tracking import collect_evidence
from app.engine.research_tracking import (
    MODULES, POLICY, ROLE_CONTRACTS, SYSTEM, chart_levels, constrain_review, digest, inherited_modules,
    iso_day, plan_modules, submission_contract, tool_schema, validate_update,
)
from app.models.research_tracking import DailyReview, ModuleUpdate, TechnicalUpdate, TrackSettings
from app.engine.report_chart_levels import report_chart_levels
from app.engine.wyckoff_tracking import CONTRACT as WYCKOFF_CONTRACT, WYCKOFF_POLICY, baseline_assessment, wyckoff_board

logger = logging.getLogger(__name__)
TRACKS = "research_tracks"
VERSIONS = "research_versions"
RUNS = "research_update_runs"
STEPS = "research_update_steps"
BUDGETS = "research_update_budgets"
BASELINES = "research_baselines"
CN = ZoneInfo("Asia/Shanghai")
MAX_INPUT_BYTES = 240000
MAX_OUTPUT_TOKENS = 5000


def submission_failure(exc):
    """Preserve actionable diagnostics without storing rejected input values."""
    from pydantic import ValidationError
    if isinstance(exc, ValidationError):
        return "; ".join(".".join(map(str, e['loc'])) + ":" + e['type']
            for e in exc.errors(include_input=False, include_url=False)[:8])
    return str(exc)[:300]


class TrackingError(ValueError):
    pass


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def public(row):
    result = {"id": row["_id"], **{k: v for k, v in row.items() if k not in {"_id", "user_id", "lease_token", "baseline", "pending_inputs"}}}
    if isinstance(result.get("retry_after_at"), datetime):
        # Mongo's naive UTC must not become the browser's local wall time.
        result["retry_after_at"] = aware(result["retry_after_at"])
    return result


def report_baseline(report, now):
    day = iso_day(report.get("analysis_date"))
    if not day:
        raise TrackingError("原报告缺少有效分析日期")
    original = report.get("reports") or {}
    return {"analysis_id": report["analysis_id"], "as_of": day, "reports": original,
        "created_at": report.get("created_at", now), "decision": report.get("decision") or {},
        "strategy": report.get("tracking_strategy"),
        "source_hash": digest({"reports": original, "decision": report.get("decision")})}


def report_query(symbol, user_id):
    return {"stock_symbol": symbol, "user_id": user_id, "status": "completed", "analysis_horizon": "short",
        "market_type": {"$in": ["A股", "CN", "A", "中国A股"]}}


class ResearchTrackingService:
    def __init__(self, db=None):
        self.db = db if db is not None else get_mongo_db()

    async def owned(self, track_id, user_id):
        row = await self.db[TRACKS].find_one({"_id": track_id, "user_id": user_id})
        if not row:
            raise TrackingError("跟踪不存在或无权访问")
        return row

    async def list_tracks(self, user_id):
        rows = []
        async for row in self.db[TRACKS].find({"user_id": user_id}, {
                "symbol": 1, "name": 1, "baseline_date": 1, "settings": 1, "created_at": 1
        }).sort([("created_at", -1), ("_id", 1)]).limit(20):
            rows.append({"symbol": row["symbol"], "name": row.get("name") or row["symbol"],
                "baseline_date": row["baseline_date"], "auto_enabled": bool((row.get("settings") or {}).get("auto_enabled"))})
        return rows

    async def create(self, symbol, user_id, analysis_id=None):
        if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
            raise TrackingError("第一版仅支持六位代码的 A 股")
        query = report_query(symbol, user_id)
        if analysis_id:
            query["analysis_id"] = analysis_id
        report = await self.db.analysis_reports.find_one(query, sort=[("created_at", -1)])
        if not report or report.get("market_type") not in {"A股", "CN", "A", "中国A股"}:
            raise TrackingError("需要一份本账号的完整 A 股短线报告作为研究起点")
        day = iso_day(report.get("analysis_date"))
        if not day:
            raise TrackingError("原报告缺少有效分析日期")
        now = datetime.now(timezone.utc)
        track_id = digest({"user_id": user_id, "symbol": symbol})
        if await self.db[TRACKS].find_one({"_id": track_id, "user_id": user_id}):
            return public(await self.owned(track_id, user_id))
        if await self.db[TRACKS].count_documents({"user_id": user_id}) >= 20:
            raise TrackingError("第一版每个账号最多跟踪20只股票")
        baseline = report_baseline(report, now)
        row = {"_id": track_id, "user_id": user_id, "symbol": symbol, "name": report.get("stock_name", symbol),
            "baseline_analysis_id": report["analysis_id"], "baseline_date": day, "baseline": baseline,
            "settings": TrackSettings().model_dump(), "created_at": now, "latest_version_id": None}
        try:
            await self.db[TRACKS].insert_one(row)
        except DuplicateKeyError:
            row = await self.owned(track_id, user_id)
        return public(row)

    async def baseline_metadata(self, track, baseline):
        created = baseline.get("created_at")
        if not isinstance(created, datetime):
            source = await self.db.analysis_reports.find_one({"analysis_id": baseline["analysis_id"], "user_id": track["user_id"],
                "stock_symbol": track["symbol"]}, {"created_at": 1})
            created = (source or {}).get("created_at")
        return {"analysis_id": baseline["analysis_id"], "as_of": baseline["as_of"],
            "created_at": aware(created).astimezone(timezone.utc).isoformat() if isinstance(created, datetime) else None,
            "is_current": baseline["analysis_id"] == track["baseline_analysis_id"]}

    async def newer_report(self, track):
        current = await self.baseline_metadata(track, track["baseline"])
        query = report_query(track["symbol"], track["user_id"])
        async for row in self.db.analysis_reports.find(query, {"analysis_id": 1, "analysis_date": 1, "created_at": 1}).sort(
                [("analysis_date", -1), ("created_at", -1)]).limit(30):
            if row["analysis_id"] == current["analysis_id"]:
                continue
            day = iso_day(row.get("analysis_date"))
            created = row.get("created_at")
            if not day or not isinstance(created, datetime):
                continue
            stamp = aware(created).astimezone(timezone.utc).isoformat()
            if day > current["as_of"] or (day == current["as_of"] and current["created_at"] and stamp > current["created_at"]):
                return {"analysis_id": row["analysis_id"], "as_of": day, "created_at": stamp}
        return None

    async def switch_baseline(self, track_id, user_id, analysis_id, expected_baseline_analysis_id):
        track = await self.owned(track_id, user_id)
        if track["baseline_analysis_id"] == analysis_id:
            return public(track)  # Idempotent retry after a lost HTTP response.
        if track["baseline_analysis_id"] != expected_baseline_analysis_id:
            raise TrackingError("研究起点已变化，请刷新后核对再切换")
        query = {**report_query(track["symbol"], user_id), "analysis_id": analysis_id}
        report = await self.db.analysis_reports.find_one(query)
        if not report:
            raise TrackingError("只能选择本账号、同一股票的完整 A 股短线报告")
        now = datetime.now(timezone.utc)
        baseline = report_baseline(report, now)
        current = await self.baseline_metadata(track, track["baseline"])
        target = await self.baseline_metadata(track, baseline)
        if not (target["as_of"] > current["as_of"] or (target["as_of"] == current["as_of"] and target["created_at"]
                and current["created_at"] and target["created_at"] > current["created_at"])):
            raise TrackingError("只能切换到更新的完整报告；旧起点可在历史记录中查看")
        if track.get("lease_until") and aware(track["lease_until"]) >= now:
            raise TrackingError("该股票研究更新正在执行，请完成后再切换起点")
        # Save the old frozen report before the atomic switch. Orphan snapshots
        # after a losing CAS are harmless and never listed without a track ref.
        archive_id = digest({"track": track_id, "baseline": track["baseline"]})
        await self.db[BASELINES].update_one({"_id": archive_id}, {"$setOnInsert": {
            "track_id": track_id, "user_id": user_id, "baseline": track["baseline"], "archived_at": now}}, upsert=True)
        history = {**current, "archive_id": archive_id, "is_current": False, "archived_at": now,
            "latest_version_id": track.get("latest_version_id")}
        changed = await self.db[TRACKS].find_one_and_update({"_id": track_id, "user_id": user_id,
            "baseline_analysis_id": expected_baseline_analysis_id,
            "$or": [{"lease_until": {"$exists": False}}, {"lease_until": {"$lt": now}}]},
            {"$set": {"baseline_analysis_id": analysis_id, "baseline_date": baseline["as_of"], "baseline": baseline,
                      "baseline_changed_at": now, "latest_version_id": None},
             "$push": {"baseline_history": history}, "$unset": {"last_as_of": "", "lease_token": "", "lease_until": ""}},
            return_document=ReturnDocument.AFTER)
        if not changed:
            latest = await self.owned(track_id, user_id)
            if latest["baseline_analysis_id"] == analysis_id:
                return public(latest)
            raise TrackingError("研究起点或运行状态已变化，请刷新后重试")
        return public(changed)

    async def previous_phase_reference(self, track, baseline, version):
        """An older baseline is a separate reference, never today's chart phase."""
        from app.engine.wyckoff_tracking import checked_assessment, annotation, baseline_assessment
        history = track.get("baseline_history", [])
        ids = [h["analysis_id"] for h in history] + [track["baseline_analysis_id"]]
        previous = ids[:ids.index(baseline["analysis_id"])]
        if not previous:
            return None
        cutoff = (version or {}).get("created_at") or baseline.get("created_at")
        if not isinstance(cutoff, datetime):
            return None
        scope = {"$or": [{"baseline_analysis_id": {"$in": previous}},
            {"baseline_analysis_id": {"$exists": False}}]}
        query = {"track_id": track["_id"], "user_id": track["user_id"], **scope,
            "created_at": {"$lte": cutoff}, "as_of": {"$lte": (version or baseline)["as_of"]},
            "modules.technical.wyckoff": {"$type": "object"}}
        async for row in self.db[VERSIONS].find(query).sort([("created_at", -1), ("_id", -1)]).limit(90):
            tech = row["modules"]["technical"]
            if tech.get("inherited") or tech.get("update_error") or tech.get("wyckoff_error") or tech.get("as_of") != row["as_of"]:
                continue
            try:
                value = checked_assessment(tech["wyckoff"], tech["content"], row["snapshot"])
            except (ValueError, TypeError, KeyError):
                continue
            return annotation(value, as_of=tech["as_of"], known_at=row["created_at"], source_kind="daily",
                analysis_id=row.get("baseline_analysis_id") or ids[0], version_id=str(row["_id"]))
        for ref in reversed(history[:len(previous)]):
            row = await self.db[BASELINES].find_one({"_id": ref["archive_id"], "track_id": track["_id"], "user_id": track["user_id"]})
            old = (row or {}).get("baseline") or {}
            if not old.get("created_at") or aware(old["created_at"]) > aware(cutoff) or old["as_of"] > (version or baseline)["as_of"]:
                continue
            value = baseline_assessment(old)
            if value:
                return annotation(value, as_of=old["as_of"], known_at=old["created_at"],
                    source_kind="report", analysis_id=old["analysis_id"])
        return None

    async def board(self, symbol, user_id, version_id=None, baseline_analysis_id=None):
        track = await self.db[TRACKS].find_one({"symbol": symbol, "user_id": user_id})
        if not track:
            return {"track": None, "versions": [], "version": None, "run": None}
        history = track.get("baseline_history", [])
        original_id = history[0]["analysis_id"] if history else track["baseline_analysis_id"]
        owned_versions = {"track_id": track["_id"], "user_id": user_id}
        version = await self.db[VERSIONS].find_one({"_id": version_id, **owned_versions}) if version_id else None
        if version_id and version is None:
            raise TrackingError("研究版本不存在或无权访问")
        selected_baseline = (version.get("baseline_analysis_id") or original_id) if version else (baseline_analysis_id or track["baseline_analysis_id"])
        if version and baseline_analysis_id and selected_baseline != baseline_analysis_id:
            raise TrackingError("所选版本不属于该研究起点")
        baseline = track["baseline"]
        latest_version_id = track.get("latest_version_id")
        if selected_baseline != track["baseline_analysis_id"]:
            reference = next((h for h in history if h["analysis_id"] == selected_baseline), None)
            archived = await self.db[BASELINES].find_one({"_id": reference["archive_id"], "track_id": track["_id"], "user_id": user_id}) if reference else None
            if not archived:
                raise TrackingError("历史研究起点不存在或无权访问")
            baseline = archived["baseline"]
            latest_version_id = reference.get("latest_version_id")
        scope = {"$or": [{"baseline_analysis_id": selected_baseline}, {"baseline_analysis_id": {"$exists": False}}]} if selected_baseline == original_id else {"baseline_analysis_id": selected_baseline}
        rows = await self.db[VERSIONS].find({**owned_versions, **scope},
            {"_id": 1, "as_of": 1, "created_at": 1, "status": 1, "changed_modules": 1}).sort("created_at", -1).limit(90).to_list(90)
        if not version and latest_version_id:
            version = await self.db[VERSIONS].find_one({"_id": latest_version_id, **owned_versions, **scope})
        current_scope = {"baseline_analysis_id": track["baseline_analysis_id"]} if history else {"$or": [
            {"baseline_analysis_id": track["baseline_analysis_id"]}, {"baseline_analysis_id": {"$exists": False}}]}
        run = await self.db[RUNS].find_one({**owned_versions, **current_scope}, sort=[("created_at", -1)])
        if run and run["status"] in {"pending", "running"} and aware(run["deadline_at"]) < datetime.now(timezone.utc):
            run = {**run, "status": "interrupted", "message": "服务中断或超时；已完成模块保留，可显式重试"}
        # Freeze visibility at the selected version, including same-day retries.
        # Query the cutoff before the limit so older selections cannot lose their history.
        phase_versions = []
        if version:
            phase_versions = await self.db[VERSIONS].find({**owned_versions, **scope,
                "$and": [{"$or": [{"created_at": {"$lt": version["created_at"]}},
                    {"created_at": version["created_at"], "_id": {"$lte": version["_id"]}}]}],
                "as_of": {"$lte": version["as_of"]}},
                {"as_of": 1, "created_at": 1, "modules.technical": 1,
                 "snapshot.as_of": 1, "snapshot.bars": 1, "snapshot.price": 1}).sort(
                    [("created_at", -1), ("_id", -1)]).limit(90).to_list(90)
        phases = wyckoff_board(baseline, phase_versions, version)
        from app.services.wyckoff_timeline import phase_timeline
        phases["timeline"] = await phase_timeline(self.db, track, baseline, version, phases)
        if not phases["current"]:
            phases["previous_reference"] = await self.previous_phase_reference(track, baseline, version)
        previous_review = None
        if version and not version.get("review"):
            # A failed refresh must not erase the last review, nor promote it to
            # a current verdict. Keep baseline/owner/time boundaries exact.
            prior = await self.db[VERSIONS].find_one({**owned_versions, **scope,
                "created_at": {"$lt": version["created_at"]}, "as_of": {"$lte": version["as_of"]},
                "review": {"$type": "object"}, "status": {"$in": ["completed", "review_required"]}},
                sort=[("created_at", -1), ("_id", -1)])
            if prior:
                levels = chart_levels(prior["review"].get("strategy"), datetime.now(CN).date().isoformat(),
                    aware(prior["created_at"]).astimezone(CN).isoformat())
                previous_review = {"version_id": prior["_id"], "as_of": prior["as_of"],
                    "created_at": prior["created_at"], "levels": [{**p, "state": "previous_review"} for p in levels]}
        if version:
            version = public(version)
            strategy = (version.get("review") or {}).get("strategy")
            version["levels"] = chart_levels(strategy, datetime.now(CN).date().isoformat(), aware(version["created_at"]).astimezone(CN).isoformat())
        known_at = baseline.get("created_at")
        if not isinstance(known_at, datetime):
            known_at = track["created_at"]
        reference_levels, reference_notice = report_chart_levels(baseline, aware(known_at).astimezone(CN).isoformat())
        from app.engine.research_price_evidence import target_comparison
        prior_targets, prior_date = [], None
        if version and version.get("review"):
            prior_row = await self.db[VERSIONS].find_one({**owned_versions, **scope,
                "created_at": {"$lt": aware(version["created_at"])}, "review": {"$type": "object"}},
                sort=[("created_at", -1)])
            if prior_row:
                prior_targets = chart_levels(prior_row["review"].get("strategy"), prior_row["as_of"], aware(prior_row["created_at"]).isoformat())
                prior_date = prior_row["as_of"]
            else:
                prior_targets, prior_date = reference_levels, baseline["as_of"]
        else:
            candidates = [h for h in history if h["analysis_id"] != selected_baseline and str(h["as_of"]) <= baseline["as_of"]]
            selected_index = next((i for i, h in enumerate(history) if h["analysis_id"] == selected_baseline), len(history))
            candidates = [h for h in history[:selected_index] if h in candidates]
            if candidates:
                archive = await self.db[BASELINES].find_one({"_id": candidates[-1]["archive_id"], **owned_versions})
                if archive:
                    old = archive["baseline"]
                    prior_targets, _ = report_chart_levels(old, aware(old.get("created_at") or track["created_at"]).isoformat())
                    prior_date = old["as_of"]
        current_review = (version or {}).get("review")
        comparison = target_comparison(
            version["levels"] if current_review else reference_levels, prior_targets,
            current_date=version["as_of"] if current_review else baseline["as_of"], previous_date=prior_date,
            known_at=version["created_at"] if current_review else known_at,
            content=current_review["content"] if current_review else "\n".join(baseline.get("reports", {}).get(k, "") for k in ("market_report", "final_trade_decision")),
            bars=((version or {}).get("snapshot") or {}).get("bars", []))
        current_metadata = await self.baseline_metadata(track, track["baseline"])
        return {"track": public(track), "versions": [public(r) for r in rows], "version": version,
                "wyckoff": phases, "target_comparison": comparison,
                "previous_review": previous_review, "budget": await self.budget_state(track),
                "manual_options": await self.manual_options(track),
                "run": public(run) if run else None,
                "newer_report": await self.newer_report(track),
                "baselines": [current_metadata, *reversed(history)],
                "baseline": {**await self.baseline_metadata(track, baseline), "levels": reference_levels, "level_notice": reference_notice}}

    async def settings(self, track_id, user_id, value: TrackSettings):
        await self.owned(track_id, user_id)
        row = await self.db[TRACKS].find_one_and_update({"_id": track_id, "user_id": user_id},
            {"$set": {"settings": value.model_dump()}}, return_document=ReturnDocument.AFTER)
        return public(row)

    async def manual_options(self, track):
        """Bounded call estimates, not a promise of model success or token cost."""
        maximum = 2 * (len(MODULES) + 1)
        continuation = maximum
        previous = await self.db[VERSIONS].find_one({"_id": track.get("latest_version_id"),
            "track_id": track["_id"], "user_id": track["user_id"], "status": "partial"})
        if (previous and previous.get("baseline_analysis_id") == track["baseline_analysis_id"]
                and previous.get("pending_inputs")):
            pending = set(previous["pending_inputs"]) & set(MODULES)
            continuation = min(maximum, 2 * (len(pending) + 1))
        return {"update_max_calls": maximum, "continue_max_calls": continuation,
                "max_extra_calls": maximum}

    async def enqueue(self, track_id, user_id, *, automatic=False, retry_failed=False,
                      manual_extra_calls=0, expected_baseline_analysis_id=None):
        track = await self.owned(track_id, user_id)
        if type(manual_extra_calls) is not int or not 0 <= manual_extra_calls <= 10:
            raise TrackingError("本次手动追加调用上限必须在0至10次之间")
        if automatic and manual_extra_calls:
            raise TrackingError("自动更新不能使用手动追加额度")
        if expected_baseline_analysis_id and expected_baseline_analysis_id != track["baseline_analysis_id"]:
            raise TrackingError("研究起点已变化，请刷新后重新确认")
        if automatic and not track["settings"]["auto_enabled"]:
            return None
        budget = await self.budget_state(track)
        now = datetime.now(timezone.utc)
        if retry_failed:
            last = await self.db[RUNS].find_one({"track_id": track_id, "user_id": user_id,
                "baseline_analysis_id": track["baseline_analysis_id"]}, sort=[("created_at", -1)])
            if last and last.get("status") in {"failed", "partial"} and last.get("retry_after_at"):
                wait = (aware(last["retry_after_at"])-now).total_seconds()
                if wait > 0:
                    raise TrackingError(f"上次更新刚结束，请等待{int(wait)+1}秒后继续；已完成内容保留")
        token = uuid4().hex
        claimed = await self.db[TRACKS].find_one_and_update({"_id": track_id, "user_id": user_id,
            "baseline_analysis_id": track["baseline_analysis_id"],
            "$or": [{"lease_until": {"$exists": False}}, {"lease_until": {"$lt": now}}]},
            {"$set": {"lease_until": now+timedelta(minutes=20), "lease_token": token}}, return_document=ReturnDocument.AFTER)
        if not claimed:
            raise TrackingError("该股票已有更新正在执行或研究起点已变化，请刷新后继续")
        run = {"_id": uuid4().hex, "track_id": track_id, "user_id": user_id, "status": "pending",
            "baseline_analysis_id": claimed["baseline_analysis_id"],
            "automatic": automatic, "retry_failed": retry_failed, "created_at": now,
            "manual_extra_calls": manual_extra_calls, "manual_extra_day": budget["date"],
            "deadline_at": now+timedelta(minutes=20), "lease_token": token, "model_calls": 0,
            "message": "等待获取增量数据"}
        try:
            await self.db[RUNS].insert_one(run)
        except Exception:
            await self.release(run)
            raise
        return public(run)

    async def budget_state(self, track, now=None):
        now = now or datetime.now(timezone.utc)
        local = now.astimezone(CN)
        row = await self.db[BUDGETS].find_one({"_id": f"owner:{track['user_id']}:{local.date().isoformat()}"}) or {}
        used = row.get("tracks", {}).get(track["_id"], 0)
        total = row.get("count", 0)
        reset = (local+timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        return {"date": local.date().isoformat(), "used": used, "limit": None, "owner_used": total,
            "owner_limit": None, "remaining": None, "unlimited": True,
            "manual_extra_used": row.get("manual_extra_count", 0), "resets_at": reset,
            "message": f"今日研究模型调用：此股 {used} 次，账号合计 {total} 次。已取消每日次数上限；继续记录用量，已完成模块复用。"}


    async def release(self, run):
        await self.db[TRACKS].update_one({"_id": run["track_id"], "lease_token": run["lease_token"]},
            {"$unset": {"lease_token": "", "lease_until": ""}})

    async def reserve_budget(self, track, now, run=None):
        day = now.astimezone(CN).date().isoformat()
        # Usage accounting only. Historical budget settings remain readable but
        # cannot block either manual or scheduled work.
        key = f"owner:{track['user_id']}:{day}"
        field = "tracks."+track["_id"]
        try:
            await self.db[BUDGETS].insert_one({"_id": key, "count": 0, "tracks": {}, "created_at": now})
        except DuplicateKeyError:
            pass
        await self.db[BUDGETS].update_one({"_id": key}, {"$inc": {"count": 1, field: 1}})


    async def reserve_step_call(self, track, run, key, name, model, *, repair=False):
        # Preserve ownership, lease and scheduler opt-out checks before accounting.
        current = await self.owned(track["_id"], track["user_id"])
        if run.get("lease_token") and (current.get("lease_token") != run["lease_token"]
                or current["baseline_analysis_id"] != track["baseline_analysis_id"]):
            raise TrackingError("研究起点或更新租约已变化，停止旧任务调用")
        if run["automatic"] and not current["settings"]["auto_enabled"]:
            raise TrackingError("自动更新已关闭，停止后续模型调用")
        await self.reserve_budget(current, datetime.now(timezone.utc), run)
        await self.db[STEPS].update_one({"_id": key}, {"$set": {"track_id": track["_id"], "user_id": track["user_id"],
            "step": name, "status": "running", "model": model, "updated_at": datetime.now(timezone.utc)},
            "$inc": {"attempts": 1, "repair_calls": int(repair)}}, upsert=True)
        await self.db[RUNS].update_one({"_id": run["_id"]}, {"$inc": {"model_calls": 1},
            "$set": {"message": ("正在纠正" if repair else "正在更新") + ("综合复核" if name == "review" else MODULES[name]["title"])}})

    def submission_tool(self, track, key, snapshot, *, final=False, technical=False):
        from app.llm.core.types import ToolDef
        accepted, rejected = [], []

        async def submit_update(**value):
            if accepted:
                raise ValueError("已提交，请勿重复")
            try:
                output = validate_update(value, snapshot, final=final, technical=technical, require_method=technical)
            except ValueError as exc:
                reason = submission_failure(exc)
                rejected.append(reason)
                await self.db[STEPS].update_one({"_id": key}, {"$set": {"validation_errors": rejected}})
                raise ValueError(reason) from None
            if final:
                output = constrain_review(output, track["baseline"])
            # Persist at the acceptance boundary, before any later event/closure.
            await self.db[STEPS].update_one({"_id": key}, {"$set": {"status": "completed", "output": output,
                "completed_at": datetime.now(timezone.utc)}})
            accepted.append(output)
            return "已保存，本模块完成"

        tool = ToolDef(name="submit_update", description="提交本模块完整的每日更新，必须包含来源、变化与限制",
            params_schema=tool_schema(DailyReview if final else TechnicalUpdate if technical else ModuleUpdate), handler=submit_update,
            is_concurrency_safe=False)
        return tool, accepted, rejected

    async def prepare_review_experience(self, track, run, key, packet, snapshot, existing, context_window):
        from app.services.experience_service import ExperienceService, experience_text
        experiences = ExperienceService(self.db)
        try:
            # Keep a failed step's exact historical input on retry. A
            # completed checkpoint returns above without another lookup.
            saved = await experiences.by_id(track["user_id"], (existing or {}).get("experience_usage_id"))
            experience = await experiences.reuse(saved, track["user_id"], "tracking:" + run["_id"]) if saved else await experiences.prepare(
                owner=track["user_id"], context_id="tracking:" + run["_id"], kind="daily_research",
                stage="review", as_of=snapshot["as_of"], horizon_days=7,
                situation=track["symbol"] + "\n" + "\n".join(m.get("content", "")[:3000] for m in packet.get("modules", {}).values()))
            addition = experience_text(experience)
            enriched = json.dumps({**packet, "historical_outcome_lessons": addition}, ensure_ascii=False, default=str)
            if addition and (len(enriched.encode()) > MAX_INPUT_BYTES or len(enriched.encode()) + 12000 > context_window * .6):
                experience.update(entries=[], status="unavailable", reason="可选经验超出本轮输入余量，保留原分析完整依据")
                await self.db["analysis_experience_usage"].update_one({"_id": experience["id"]}, {"$set": {
                    k: experience[k] for k in ("entries", "status", "reason")}})
                addition = ""
            await self.db[STEPS].update_one({"_id": key}, {"$set": {"experience_usage_id": experience["id"]},
                "$setOnInsert": {"status": "preparing", "user_id": track["user_id"], "track_id": track["_id"]}}, upsert=True)
        except Exception:
            logger.warning("每日研究复盘经验未接入，继续原复核", exc_info=True)
            return None, "", ""
        return experience, addition, enriched

    async def model_step(self, track, run, name, packet, snapshot, *, final=False):
        from app.llm.runner import run_conversation
        from app.llm.providers import resolve_task_override_bundle
        from app.llm.compact.auto_compactor import CompactConfig
        from app.services.analysis_service import resolve_analysis_models_sync

        body = json.dumps(packet, ensure_ascii=False, default=str, allow_nan=False)
        if len(body.encode()) > MAX_INPUT_BYTES:
            raise TrackingError("本次完整依据超出增量研究输入上限，需重新完整分析；未截断关键条件")
        key = digest({"track": track["_id"], "policy": POLICY, "step": name, "packet": packet})
        existing = await self.db[STEPS].find_one({"_id": key})
        if existing and existing["status"] == "completed":
            return existing["output"]
        if existing and not run.get("retry_failed"):
            raise TrackingError("相同材料的该模块此前未完成；请使用重试继续，避免自动重复消耗")
        models = await asyncio.to_thread(resolve_analysis_models_sync, None, None)
        model = models[1] if final else models[0]
        bundle = await resolve_task_override_bundle(model)
        if bundle is None:
            raise TrackingError("当前默认分析模型不可用")
        context_window = bundle.context_window or 128000
        if len(body.encode()) + 12000 > context_window * .6:
            raise TrackingError("当前模型上下文不足以完整容纳增量依据；未额外调用模型压缩，请选择更大上下文模型")
        experience = None
        if final:
            from app.services.experience_service import ExperienceService
            experiences = ExperienceService(self.db)
            experience, addition, enriched = await self.prepare_review_experience(
                track, run, key, packet, snapshot, existing, context_window)
        validation_snapshot = {**snapshot, "previous_wyckoff": packet.get("previous_wyckoff")} if name == "technical" else snapshot
        tool, accepted, rejected = self.submission_tool(track, key, validation_snapshot, final=final, technical=name == "technical")
        system = SYSTEM + "\n" + ROLE_CONTRACTS[name] + "\n" + submission_contract(snapshot, final=final)
        if name == "technical":
            system += "\n" + WYCKOFF_CONTRACT
        elif final:
            from app.engine.wyckoff_method import METHOD_CONTRACT
            system += "\n" + METHOD_CONTRACT
        history = None
        try:
            for attempt in range(2):
                await self.reserve_step_call(track, run, key, name, model, repair=attempt > 0)
                if attempt == 0 and experience:
                    if await experiences.submitted(experience, track["user_id"]):
                        if addition:
                            body = enriched
                    else:
                        experience = None
                rejection_count = len(rejected)
                async with asyncio.timeout(180):
                    result = await run_conversation(bundle.primary, body, history=history, system=system,
                        tools=[tool], max_turns=1, max_tool_calls=1, max_tokens=MAX_OUTPUT_TOKENS,
                        retry_times=0, temperature=.2, user_id=track["user_id"], task_id="tracking:"+run["_id"],
                        agent_key="tracking_"+name, phase="daily_research", stop_when=lambda: bool(accepted),
                        enable_skill_listing=False,
                        compact_config=CompactConfig(context_window=context_window, max_output_tokens=MAX_OUTPUT_TOKENS))
                await self.db[STEPS].update_one({"_id": key}, {"$inc": {"tokens": result.total_tokens},
                    "$set": {"stop_reason": result.stop_reason}})
                if accepted:
                    output = accepted[0]
                    if final and experience:
                        await experiences.completed(experience, track["user_id"], output.get("content", ""))
                        output["experience_usage_id"] = experience["id"]
                        await self.db[STEPS].update_one({"_id": key}, {"$set": {"output": output}})
                    if name == "technical":
                        if output.get("wyckoff_error") and attempt == 0:
                            output = await self.repair_technical_output(track, run, key, model, bundle.primary,
                                output, validation_snapshot)
                        output["wyckoff_submission_policy"] = WYCKOFF_POLICY
                        await self.db[STEPS].update_one({"_id": key}, {"$set": {"status": "completed", "output": output}})
                    return output
                if len(rejected) == rejection_count:
                    raise TrackingError("模型未提交有效的结构化更新（停止原因：" + result.stop_reason + "）；已完成模块保留")
                if attempt == 1:
                    raise TrackingError("模型纠正后仍未通过校验：" + rejected[-1] + "；已完成模块保留")
                # One correction of the rejected submission, not a fresh study.
                # Keep the original evidence and the paired tool error in history.
                history = result.messages
                body = "上次提交未保存，校验错误：" + rejected[-1] + "。仅纠正不符合提交规则的字段并再次完整提交；不能删除必要条件或伪造证据以通过校验。证据不足时如实保留未知、策略可为null。"
        except Exception as exc:
            saved = await self.db[STEPS].find_one({"_id": key, "status": "completed"})
            if saved:
                return saved["output"]
            await self.db[STEPS].update_one({"_id": key}, {"$set": {"status": "failed", "error": self.failure_message(exc)}})
            raise

    async def repair_technical_output(self, track, run, key, model, client, output, snapshot):
        from app.engine.wyckoff_repair import correction_prompt, make_repair_tool, record_correction_result
        from app.llm.runner import run_conversation

        rejections = []
        tool, corrected = make_repair_tool(output["content"], snapshot, rejections=rejections)
        system, body = correction_prompt(output["content"], output["wyckoff_error"], snapshot)
        record = {"attempted": False, "succeeded": False}
        result = None
        try:
            if len(body.encode()) > MAX_INPUT_BYTES:
                raise TrackingError("局部纠正材料超过输入上限，原技术正文已保留")
            await self.reserve_step_call(track, run, key, "technical", model, repair=True)
            record["attempted"] = True
            async with asyncio.timeout(120):
                result = await run_conversation(client, body, system=system, tools=[tool], max_turns=1,
                    max_tool_calls=1, max_tokens=MAX_OUTPUT_TOKENS, retry_times=0, temperature=.2,
                    user_id=track["user_id"], task_id="tracking:"+run["_id"], agent_key="tracking_technical",
                    phase="daily_research", stop_when=lambda: bool(corrected), enable_skill_listing=False,
                    allow_compaction=False)
            await self.db[STEPS].update_one({"_id": key}, {"$inc": {"tokens": result.total_tokens}})
        except Exception as exc:
            record["error"] = self.failure_message(exc)
        record_correction_result(record, result, corrected, rejections)
        if corrected:
            output = {**output, **corrected[0]}
            output.pop("wyckoff_error", None)
            record["succeeded"] = True
        output["wyckoff_repair"] = record
        # A failed correction/budget reservation must not discard valid technical work.
        await self.db[STEPS].update_one({"_id": key}, {"$set": {"status": "completed", "output": output}})
        return output

    async def execute(self, run_id):
        run = await self.db[RUNS].find_one_and_update({"_id": run_id, "status": "pending"},
            {"$set": {"status": "running"}}, return_document=ReturnDocument.AFTER)
        if not run:
            return
        try:
            track = await self.owned(run["track_id"], run["user_id"])
            if run.get("baseline_analysis_id", track["baseline_analysis_id"]) != track["baseline_analysis_id"] or track.get("lease_token") != run["lease_token"]:
                raise TrackingError("研究起点或更新租约已变化，未继续旧任务")
            snapshot = await self.resume_snapshot(run, track)
            if snapshot is None:
                snapshot = await collect_evidence(track["symbol"], datetime.now(timezone.utc))
                from app.services.chan_structure import ChanStructureService
                snapshot["chan"] = await ChanStructureService(self.db).build(track["symbol"], snapshot)
            await self.publish_snapshot(run, track, snapshot)
        except asyncio.CancelledError:
            await self.finish(run, "interrupted", "任务被中断；已完成模块保留")
            raise
        except Exception as exc:
            logger.warning("Daily research failed (%s)", type(exc).__name__)
            await self.finish(run, "failed", self.failure_message(exc))
        finally:
            await self.release(run)

    async def resume_snapshot(self, run, track):
        if not run.get("retry_failed"):
            return None
        previous = await self.db[VERSIONS].find_one({"_id": track.get("latest_version_id"),
            "track_id": track["_id"], "user_id": track["user_id"], "status": "partial"})
        if previous and previous.get("baseline_analysis_id", track["baseline_analysis_id"]) == track["baseline_analysis_id"]:
            return deepcopy(previous["snapshot"])
        return None

    async def publish_snapshot(self, run, track, snapshot):
        """Apply a frozen evidence snapshot; shared by live collection and audited replay."""
        run_id = run["_id"]
        if snapshot["as_of"] < track["baseline_date"]:
            await self.finish(run, "waiting_data", "最近已结束交易日早于原报告，等待收盘数据后再更新；当前行情可单独刷新", as_of=snapshot["as_of"])
            return
        fingerprint = digest({"as_of": snapshot["as_of"], "domains": snapshot["domains"], "policy": POLICY})
        previous = await self.db[VERSIONS].find_one({"_id": track.get("latest_version_id"), "user_id": run["user_id"]})
        from app.engine.research_tracking import wyckoff_needs_refresh
        if previous and previous["status"] in {"completed", "review_required"} and previous["fingerprint"] == fingerprint and not wyckoff_needs_refresh(previous):
            await self.finish(run, "unchanged", "本次数据与已发布版本一致，未调用模型")
            return
        # Catch-up publishes only the latest actual session. No historical
        # daily LLM replay when the Mac has been asleep or Docker was stopped.
        if run["automatic"] and await self.db[RUNS].find_one({"track_id": track["_id"], "automatic": True,
                "as_of": snapshot["as_of"], "_id": {"$ne": run_id}}):
            await self.finish(run, "unchanged", "该交易日已有自动更新记录，不重复调用")
            return
        await self.db[RUNS].update_one({"_id": run_id}, {"$set": {"as_of": snapshot["as_of"], "fingerprint": fingerprint}})
        changed = plan_modules(previous, snapshot)
        modules = inherited_modules(previous)
        errors = []
        pending_inputs = {}
        resuming = bool(run.get("retry_failed") and previous and previous.get("fingerprint") == fingerprint)
        for name in changed:
            spec = MODULES[name]
            packet = self.module_packet(track, modules, name, snapshot)
            if resuming:
                packet = deepcopy(previous.get("pending_inputs", {}).get(name, packet))
            try:
                output = await self.model_step(track, run, name, packet, snapshot)
                modules[name] = {**output, "as_of": snapshot["as_of"], "inherited": False}
            except Exception as exc:
                pending_inputs[name] = packet
                errors.append(f"{spec['title']}：{self.failure_message(exc)}")
                # Keep the old content dated; never mark failed updates current.
                if name in modules:
                    modules[name]["update_error"] = errors[-1]
        review = None
        if not errors:
            packet = self.review_packet(track, modules, previous, snapshot)
            if resuming:
                packet = deepcopy(previous.get("pending_inputs", {}).get("review", packet))
            try:
                review = await self.model_step(track, run, "review", packet, snapshot, final=True)
            except Exception as exc:
                pending_inputs["review"] = packet
                errors.append("综合复核："+self.failure_message(exc))
        status = "partial" if errors else "review_required" if review["requires_full_review"] else "completed"
        if previous and status == "partial" and previous.get("fingerprint") == fingerprint and previous.get("errors") == errors and modules == inherited_modules(previous):
            await self.finish(run, status, "；".join(errors), version_id=previous["_id"])
            return
        now = datetime.now(timezone.utc)
        version = {"_id": uuid4().hex, "track_id": track["_id"], "user_id": run["user_id"],
            "baseline_analysis_id": track["baseline_analysis_id"], "baseline_date": track["baseline_date"],
            "parent_version_id": track.get("latest_version_id"), "as_of": snapshot["as_of"], "created_at": now,
            "policy": POLICY, "status": status, "fingerprint": fingerprint, "snapshot": snapshot,
            "modules": modules, "review": review, "errors": errors, "changed_modules": changed,
            "pending_inputs": pending_inputs}
        try:
            from app.services.experience_service import ExperienceService
            experiences = ExperienceService(self.db)
            receipts = await experiences.usage(run["user_id"], "tracking:" + run_id)
            reused = await experiences.by_id(run["user_id"], (review or {}).get("experience_usage_id"))
            if reused and not any(r["id"] == reused["id"] for r in receipts):
                receipts.append(reused)
            # A version keeps its own historical receipt. Browsing old versions
            # never fetches today's experience or calls a model.
            version["experience_usage"] = receipts
        except Exception:
            logger.warning("每日经验回执未读取，继续保存研究", exc_info=True)
        await self.db[VERSIONS].insert_one(version)
        # CAS: an expired lease/process must not overwrite a newer version.
        saved = await self.db[TRACKS].update_one({"_id": track["_id"], "lease_token": run["lease_token"], "baseline_analysis_id": track["baseline_analysis_id"]},
            {"$set": {"latest_version_id": version["_id"], "last_as_of": snapshot["as_of"]}})
        if saved.modified_count != 1:
            raise TrackingError("更新租约失效；该版本已保留，未覆盖当前看板")
        message = "；".join(errors) if errors else "每日研究版本已保存"
        technical = modules.get("technical", {})
        if not errors and technical.get("wyckoff_error"):
            reason = (technical.get("wyckoff_repair") or {}).get("error") or technical["wyckoff_error"]
            message += "；威科夫阶段未更新：" + reason
        await self.finish(run, status, message, version_id=version["_id"])

    @staticmethod
    def module_packet(track, modules, name, snapshot):
        spec = MODULES[name]
        previous = modules.get(name)
        if previous:
            # UI bookkeeping does not change the actual research input or the
            # checkpoint key when explicitly retrying the same failed update.
            previous = {k: v for k, v in previous.items() if k not in {"inherited", "update_error"}}
        packet = {"task": spec["title"], "as_of": snapshot["as_of"], "symbol": track["symbol"],
            "baseline_date": track["baseline_date"],
            "baseline_reports": {k: track["baseline"]["reports"][k] for k in spec["reports"] if k in track["baseline"]["reports"]},
            "previous": previous, "evidence": {d: snapshot["domains"][d] for d in spec["domains"]},
            "limitations": snapshot["limitations"],
            "daily_metrics": snapshot.get("daily_metrics") if name == "technical" else None}
        if name == "technical":
            origin = baseline_assessment(track["baseline"])
            packet.update(wyckoff_policy=WYCKOFF_POLICY, baseline_wyckoff=origin,
                previous_wyckoff=({"as_of": previous.get("as_of"), "assessment": previous["wyckoff"]}
                    if previous and previous.get("wyckoff") else {"as_of": track["baseline_date"], "assessment": origin} if origin else None))
        return packet

    @staticmethod
    def review_packet(track, modules, previous, snapshot):
        baseline_plan = {k: v for k, v in track["baseline"]["reports"].items() if k in {"final_trade_decision", "trader_investment_plan", "risk_manager_decision"}}
        return {"task": "综合复核：生成今天完整的跟踪报告，分别核验入场、退出、否决条件，解释点位变化。不是完整多轮辩论。",
            "symbol": track["symbol"], "as_of": snapshot["as_of"], "next_sessions": snapshot["next_sessions"],
            "baseline_date": track["baseline_date"], "original_plan": baseline_plan,
            "original_decision": track["baseline"]["decision"], "modules": modules,
            "previous_review": (previous or {}).get("review"), "current_price": snapshot["price"],
            "evidence_catalog": [{"id": r["id"], "date": r["date"], "domain": d,
                "data_source": r["data"].get("data_source"), "status": block["status"]}
                for d, block in snapshot["domains"].items() for r in block["rows"]],
            "limitations": snapshot["limitations"],
            "contract": "原有0%/未批准不能仅靠价格触及改成买入；未核验的账户依赖保持未知。新点位必须在本轮正文中逐字引用、有明确前提及有限有效期。技术模块中的威科夫阶段是有日期的推断；复核其与关键点位的冲突，不将转换候选当作已确认阶段。阶段缺项不阻塞其他结论。"}

    @staticmethod
    def failure_message(exc):
        if isinstance(exc, TrackingError):
            return str(exc)
        from app.llm.failures import describe_failure
        failure = describe_failure(exc)
        return str(failure.get("message") or failure.get("kind") or "连接或数据校验失败")

    async def finish(self, run, status, message, **fields):
        if status in {"failed", "partial"}:
            fields["retry_after_at"] = datetime.now(timezone.utc) + timedelta(seconds=60)
        await self.db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"status": status, "message": message,
            "finished_at": datetime.now(timezone.utc), **fields}})


async def run_due_updates():
    """Periodic recovery scan; opt-in only, serialized, one attempt per session."""
    now = datetime.now(CN)
    if now.hour < 19:
        return
    service = ResearchTrackingService()
    tracks = await service.db[TRACKS].find({"settings.auto_enabled": True}).limit(20).to_list(20)
    for track in tracks:
        last = await service.db[RUNS].find_one({"track_id": track["_id"], "automatic": True,
            "created_at": {"$gte": now.replace(hour=0, minute=0, second=0, microsecond=0)}})
        if last:
            continue
        try:
            row = await service.enqueue(track["_id"], track["user_id"], automatic=True)
            if row:
                await service.execute(row["id"])
        except TrackingError:
            continue
        except Exception:
            logger.warning("Automatic daily research unavailable", exc_info=True)
