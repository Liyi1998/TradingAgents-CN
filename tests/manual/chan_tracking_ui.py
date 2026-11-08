"""Opt-in isolated UI: real auth/routes/DI/Mongo; labeled synthetic research."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import os

from fastapi import FastAPI

assert os.environ.get("MONGODB_PORT") == "27229"
assert os.environ.get("MONGODB_DATABASE") == "chan_ui_test_20261104"
from app.core import database
from app.middleware.csrf import CSRFMiddleware
from app.routers import auth_db, research_tracking, health, notifications, websocket_notifications
from app.core.service_lifecycle import ServiceLifecycle
from app.models.user import UserCreate
from app.data.storage.mongo.collections import get_collection_name
from app.data.query.research_tracking import domain_block, price_bars, chan_context
from app.engine.chan_structure import calculate
from app.engine.research_tracking import validate_update
from app.services.research_tracking import ResearchTrackingService, TRACKS, VERSIONS
from tests.data.test_wyckoff_tracking import baseline, technical
from tests.data.test_research_tracking import report, review


@asynccontextmanager
async def lifespan(app):
    app.state.service_lifecycle = ServiceLifecycle()
    await database.db_manager.init_mongodb()
    await database.db_manager.init_redis()
    database.mongo_db = database.db_manager.mongo_db
    database.mongo_client = database.db_manager.mongo_client
    database.redis_client = database.db_manager.redis_client
    db = database.mongo_db
    auth_db.user_service.set_database(db)
    user = await auth_db.user_service.get_user_by_username("tracking-ui")
    if user is None:
        user = await auth_db.user_service.create_user(
            UserCreate(username="tracking-ui", email="tracking-ui@example.test", password="tracking-ui-test-only")
        )
    owner = str(user.id)
    service = ResearchTrackingService(db)
    source = json.loads(Path("runtime/chan-validation-20261104/real-data.json").read_text())
    if not await db[get_collection_name("trade_calendar", "CN")].count_documents({}):
        await db[get_collection_name("trade_calendar", "CN")].insert_many(source["calendar"]["data"])
    for symbol in ["000503", "002241", "603232"]:
        coll = db[get_collection_name("daily_quotes", "CN")]
        if not await coll.count_documents({"symbol": symbol}):
            await coll.insert_many(source[symbol]["data"])
        if await db[TRACKS].find_one({"user_id": owner, "symbol": symbol}):
            continue
        old = report(owner, symbol)
        old.update(
            analysis_date="2026-10-26",
            created_at=datetime(2026, 10, 26, 10, tzinfo=timezone.utc),
            reports=baseline()["reports"],
        )
        if symbol == "000503":
            old["reports"]["market_report"] = "隔离验收：没有可提取的阶段。"
        await db.analysis_reports.insert_one(old)
        track = await service.create(symbol, owner, old["analysis_id"])
        for day in ["2026-11-02", "2026-11-03"]:
            if day == "2026-11-03" and symbol in ["000503", "603232"]:
                new = report(owner, symbol)
                new.update(
                    analysis_date=day,
                    created_at=datetime.fromisoformat(day + "T10:00:00+00:00"),
                    reports={
                        "market_report": "**威科夫方法校验：阶段结构未通过校验，不能作为已确认阶段。** 隔离验收样例。"
                    },
                )
                await db.analysis_reports.insert_one(new)
                track = await service.switch_baseline(
                    track["id"], owner, new["analysis_id"], track["baseline_analysis_id"]
                )
            block = domain_block("daily_quotes", source[symbol], day, symbol)
            bars, price = price_bars(block, day)
            context = chan_context(bars, day, source["calendar"]["data"])
            snap = dict(
                as_of=day,
                bars=bars,
                price=price,
                chan_context=context,
                next_sessions=["2026-11-04", "2026-11-05", "2026-11-06"],
                domains={"daily_quotes": block},
            )
            if day == "2026-11-03":
                snap["chan"] = calculate(symbol, bars, context, day)
            raw = technical(day, "distribution", "candidate")
            raw["evidence_ids"] = raw["wyckoff"]["evidence_ids"] = [price["evidence_id"]]
            output = validate_update(raw, snap, technical=True)
            if day == "2026-11-03" or symbol == "000503":
                output.update(
                    wyckoff=None,
                    wyckoff_error="隔离验收：阶段未通过校验，其他技术研究已保存：3 validation errors for WyckoffAssessment",
                )
            final = review()
            final["content"] = "## 隔离界面验收样例\n此处仅检查页面展示，不是新的股票研究。"
            final["strategy"] = None
            vid = symbol + "-" + day
            await db[VERSIONS].insert_one(
                dict(
                    _id=vid,
                    track_id=track["id"],
                    user_id=owner,
                    baseline_analysis_id=track["baseline_analysis_id"],
                    as_of=day,
                    created_at=datetime.fromisoformat(day + "T11:05:00+00:00"),
                    status="completed",
                    errors=[],
                    changed_modules=["technical"],
                    snapshot=snap,
                    modules={"technical": {**output, "as_of": day, "inherited": False}},
                    review=final,
                )
            )
            await db[TRACKS].update_one({"_id": track["id"]}, {"$set": {"latest_version_id": vid}})
    app.state.service_lifecycle.mark_ready()
    yield
    app.state.service_lifecycle.mark_stopping()
    await database.db_manager.close_connections()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CSRFMiddleware)
app.include_router(auth_db.router)
app.include_router(research_tracking.router)
app.include_router(health.router)
app.include_router(notifications.router)
app.include_router(websocket_notifications.router)
