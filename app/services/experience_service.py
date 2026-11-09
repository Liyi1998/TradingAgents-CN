# data-access-exempt: owner-scoped application receipts and settled lessons, not market data.
"""Optional outcome lessons with owner/date isolation and a durable input receipt.

A receipt proves which text was prepared for a model request, not that the model
accepted its advice. Failure of this enrichment never prevents ordinary research.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import logging
from uuid import uuid4

from app.core.config import settings
from app.core.database import get_mongo_db

logger = logging.getLogger(__name__)
USAGE = "analysis_experience_usage"
RECORDS = "decision_settlements"
CONTRACT = (
    "以下是已结算的历史复盘经验，仅供反证和检查遗漏，不是本次行情、财务或交易授权的证据。"
    "不可用历史经验替代当前数据、改变原日期或放宽账户约束；不适用时忽略。"
    "若正文明确采用某条经验，请保留其【复盘经验 ID】以便追溯；没有引用不必强行引用。"
)


def public_receipt(row):
    return {k: v for k, v in row.items() if k not in {"_id", "user_id"}} | {"id": str(row["_id"])}


def experience_text(receipt):
    entries = receipt.get("entries", [])
    if not entries:
        return ""
    return CONTRACT + "\n" + "\n\n".join(
        f"【复盘经验 {e['id']}】\n标的 {e['symbol']}，决策日 {e['decision_date']}，"
        f"结果可用日 {e['available_date']}，评估窗口 {e['horizon_days']} 交易日\n"
        f"<historical_lesson>\n{e['lesson']}\n</historical_lesson>" for e in entries
    )


class ExperienceService:
    def __init__(self, db=None):
        self.db = db if db is not None else get_mongo_db()

    async def prepare(self, *, owner, context_id, kind, stage, as_of, horizon_days, situation):
        row = {"_id": uuid4().hex, "user_id": str(owner or ""), "context_id": context_id,
               "kind": kind, "stage": stage, "as_of": as_of, "horizon_days": horizon_days,
               "created_at": datetime.now(timezone.utc), "status": "disabled", "entries": [],
               "delivery": "prepared", "citation_status": "not_checked"}
        if not settings.SETTLEMENT_REFLECTION_ENABLED:
            row["reason"] = "结果复盘经验功能未启用"
        elif not owner or not as_of or not context_id:
            row.update(status="unavailable", reason="缺少所有者、分析日期或任务标识，未检索经验")
        else:
            try:
                async with asyncio.timeout(settings.KB_EMBEDDING_TIMEOUT_SECONDS + 3):
                    from app.engine.agents.utils.outcome_memory import search_lessons
                    # Bound the retrieval query only; no report or model input
                    # is truncated. Embedding is an optional local lookup.
                    lessons = await asyncio.to_thread(search_lessons, situation[:12000], str(owner), horizon_days, as_of, 2)
                    for lesson in lessons:
                        # Recheck the authoritative record: stale/orphan vectors
                        # cannot expose another owner's or uncompleted lesson.
                        query = {"user_id": str(owner), "horizon_days": horizon_days, "status": "settled",
                                 "reflection_status": "done", "reflection_available_date": {"$lte": as_of},
                                 "reflection_lesson": lesson["recommendation"],
                                 "symbol": lesson.get("symbol", ""), "decision_date": lesson.get("decision_date", "")}
                        candidates = await self.db[RECORDS].find(query).limit(20).to_list(20)
                        source = next((r for r in candidates if hashlib.sha256(str(r["_id"]).encode()).hexdigest() == lesson.get("source_identity")), None)
                        if not source or len(source["reflection_lesson"]) > 12000:
                            continue
                        row["entries"].append({"id": str(source["_id"]), "task_id": source["task_id"],
                            "symbol": source["symbol"], "decision_date": source["decision_date"],
                            "available_date": source["reflection_available_date"], "horizon_days": horizon_days,
                            "lesson": source["reflection_lesson"], "explicitly_cited": False})
                row.update(status="ready" if row["entries"] else "no_matches",
                           reason="已选择符合当前用户、评估周期和分析日期的经验" if row["entries"] else "暂无符合当前用户、评估周期和分析日期的可用复盘经验")
            except Exception:
                logger.warning("可选复盘经验检索不可用，继续原研究", exc_info=True)
                row.update(status="unavailable", reason="复盘经验检索暂不可用，继续原分析", entries=[])
        try:
            async with asyncio.timeout(2):
                await self.db[USAGE].create_index([("user_id", 1), ("context_id", 1), ("created_at", 1)])
                await self.db[USAGE].insert_one(row)
        except Exception:
            logger.warning("经验输入记录未保存，本次跳过可选经验", exc_info=True)
            row.update(status="unavailable", reason="经验记录未保存，本次未注入经验", entries=[])
        return public_receipt(row)

    async def submitted(self, receipt, owner):
        """Called immediately before invoking the existing model request."""
        try:
            async with asyncio.timeout(2):
                result = await self.db[USAGE].update_one({"_id": receipt["id"], "user_id": str(owner)},
                    {"$set": {"delivery": "submitted", "submitted_at": datetime.now(timezone.utc)}})
                return result.matched_count == 1
        except Exception:
            logger.warning("经验请求记录未保存，跳过可选经验", exc_info=True)
            return False

    async def completed(self, receipt, owner, content):
        try:
            entries = [{**e, "explicitly_cited": f"【复盘经验 {e['id']}】" in str(content)} for e in receipt["entries"]]
            async with asyncio.timeout(2):
                await self.db[USAGE].update_one({"_id": receipt["id"], "user_id": str(owner)}, {"$set": {
                    "delivery": "completed", "citation_status": "checked", "entries": entries,
                    "completed_at": datetime.now(timezone.utc)}})
        except Exception:
            logger.warning("经验引用回执未完成，保留请求记录", exc_info=True)

    async def usage(self, owner, context_id):
        rows = await self.db[USAGE].find({"user_id": str(owner), "context_id": context_id}).sort("created_at", 1).limit(100).to_list(100)
        return [public_receipt(r) for r in rows]

    async def by_id(self, owner, identity):
        row = await self.db[USAGE].find_one({"_id": identity, "user_id": str(owner)}) if identity else None
        return public_receipt(row) if row else None

    async def reuse(self, receipt, owner, context_id):
        """Retry the exact saved context, without looking into newer lessons."""
        if not settings.SETTLEMENT_REFLECTION_ENABLED:
            return await self.prepare(owner=owner, context_id=context_id, kind=receipt["kind"],
                stage=receipt["stage"], as_of=receipt["as_of"], horizon_days=receipt["horizon_days"], situation="")
        row = {k: v for k, v in receipt.items() if k not in {"id", "submitted_at", "completed_at"}}
        row.update(_id=uuid4().hex, user_id=str(owner), context_id=context_id,
                   created_at=datetime.now(timezone.utc), delivery="prepared", citation_status="not_checked",
                   reused_from=receipt["id"], entries=[{**e, "explicitly_cited": False} for e in receipt["entries"]])
        async with asyncio.timeout(2):
            await self.db[USAGE].insert_one(row)
        return public_receipt(row)

    async def reflections(self, owner, *, symbol=None, page=1, page_size=20):
        query = {"user_id": str(owner)}
        if symbol:
            query["symbol"] = symbol.strip()
        fields = {k: 1 for k in ["task_id", "symbol", "stock_name", "decision_date", "horizon_days", "signal",
            "status", "exit_date", "return_pct", "alpha_pct", "reflection_status", "reflection_lesson",
            "reflection_available_date", "reflected_at", "reflection_retry_count"]}
        rows = await self.db[RECORDS].find(query, fields).sort([("decision_date", -1), ("_id", -1)]).skip((page-1)*page_size).limit(page_size).to_list(page_size)
        return {"enabled": settings.SETTLEMENT_REFLECTION_ENABLED, "total": await self.db[RECORDS].count_documents(query),
                "page": page, "page_size": page_size, "items": [public_receipt(r) for r in rows]}


async def prepare_experience(**kwargs):
    try:
        return await ExperienceService().prepare(**kwargs)
    except Exception:
        logger.warning("复盘经验组件不可用，继续原分析", exc_info=True)
        return None


async def submit_experience(receipt, owner):
    if not receipt:
        return False
    try:
        return await ExperienceService().submitted(receipt, owner)
    except Exception:
        return False


async def complete_experience(receipt, owner, content):
    if receipt:
        try:
            await ExperienceService().completed(receipt, owner, content)
        except Exception:
            logger.warning("经验回执暂不可用", exc_info=True)
