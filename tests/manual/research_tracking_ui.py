"""Opt-in isolated UI acceptance server. Requires the task's test DB ports."""
import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

from fastapi import FastAPI

assert os.environ.get("MONGODB_PORT") == "27227", "Use the isolated validation Mongo only"
from app.core import database
from app.middleware.csrf import CSRFMiddleware
from app.routers import auth_db, research_tracking, stocks
from app.models.user import UserCreate
from app.services.research_tracking import ResearchTrackingService, VERSIONS, TRACKS, RUNS
from tests.data.test_research_tracking import report, review, snapshot


@asynccontextmanager
async def lifespan(app):
    await database.db_manager.init_mongodb()
    await database.db_manager.init_redis()
    database.mongo_db = database.db_manager.mongo_db
    database.mongo_client = database.db_manager.mongo_client
    database.redis_client = database.db_manager.redis_client
    db = database.mongo_db
    auth_db.user_service.set_database(db)
    user = await auth_db.user_service.get_user_by_username("tracking-ui")
    if user is None:
        user = await auth_db.user_service.create_user(UserCreate(username="tracking-ui", email="tracking-ui@example.test", password="tracking-ui-test-only"))
    owner = str(user.id)
    service = ResearchTrackingService(db)
    for symbol in ["002241", "603232"]:
        if not await db.analysis_reports.find_one({"user_id": owner, "stock_symbol": symbol}):
            await db.analysis_reports.insert_one(report(owner, symbol))
    track = await service.create("002241", owner)
    if not await db[VERSIONS].find_one({"track_id": track["id"]}):
        snap = snapshot()
        snap["bars"] = [{"date": "2026-10-"+str(day), "open": 10+(day % 3)*.1, "close": 10.1+(day % 4)*.08,
            "low": 9.8, "high": 10.6, "volume": 100000+day*1000, "data_source": "acceptance_sample"} for day in [19,20,21,22,23,26,27]]
        snap["price"] = snap["bars"][-1]
        for d in snap["domains"].values(): d["latest_date"] = "2026-10-27"
        snap["domains"]["money_flow"].update(status="partial", limitations=["验收样例：五日窗口缺少一天，不计算完整资金评分"])
        snap["domains"]["financial_data"]["latest_date"] = "2026-08-20"
        now = datetime(2026,10,27,10,30,tzinfo=timezone.utc)
        modules = {"technical": {"content": "## 技术研究\n本段为验收合成文字。保留参考区间和放量确认条件。", "as_of": "2026-10-27", "inherited": False, "changes": [], "limitations": []},
            "fundamentals": {"content": "## 基本面研究\n本段来自较早研究，新版未将其标成当日披露。", "as_of": "2026-10-26", "inherited": True, "changes": [], "limitations": ["财务披露日仍为8月20日"]}}
        older = {"_id": uuid4().hex, "track_id": track["id"], "user_id": owner, "as_of": "2026-10-26", "created_at": now-timedelta(days=1),
            "status": "partial", "errors": ["合成验收：综合模块未完成"], "snapshot": deepcopy(snap), "modules": deepcopy(modules), "review": None, "changed_modules": ["technical"]}
        older["snapshot"]["as_of"] = "2026-10-26"
        older["snapshot"]["bars"] = [b for b in older["snapshot"]["bars"] if b["date"] <= "2026-10-26"]
        older["snapshot"]["price"] = older["snapshot"]["bars"][-1]
        older["modules"]["technical"]["as_of"] = "2026-10-26"
        await db[VERSIONS].insert_one(older)
        current = {**older, "_id": uuid4().hex, "as_of": "2026-10-27", "created_at": now, "status": "completed", "errors": [], "review": review(), "snapshot": snap, "modules": modules}
        await db[VERSIONS].insert_one(current)
        await db[TRACKS].update_one({"_id": track["id"]}, {"$set": {"latest_version_id": current["_id"]}})
    yield
    await database.db_manager.close_connections()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CSRFMiddleware)
app.include_router(auth_db.router)
app.include_router(research_tracking.router)
app.include_router(stocks.router)
