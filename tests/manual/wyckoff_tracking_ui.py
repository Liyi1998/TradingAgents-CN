"""Opt-in UI QA with real auth/routes/Mongo and clearly labeled research fixtures."""
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, date, timezone
import os

from fastapi import FastAPI

assert os.environ.get('MONGODB_PORT') == '27227'
assert os.environ.get('MONGODB_DATABASE') == 'wyckoff_ui_20261103'
from app.core import database
from app.middleware.csrf import CSRFMiddleware
from app.routers import auth_db, research_tracking
from app.models.user import UserCreate
from app.services.research_tracking import ResearchTrackingService, TRACKS, VERSIONS
from app.engine.research_tracking import validate_update
from tests.data.test_wyckoff_tracking import baseline, price_snapshot, technical
from tests.data.test_research_tracking import report, review


@asynccontextmanager
async def lifespan(app):
    await database.db_manager.init_mongodb()
    await database.db_manager.init_redis()
    database.mongo_db = database.db_manager.mongo_db
    database.mongo_client = database.db_manager.mongo_client
    database.redis_client = database.db_manager.redis_client
    db = database.mongo_db
    auth_db.user_service.set_database(db)
    user = await auth_db.user_service.get_user_by_username('tracking-ui')
    if user is None:
        user = await auth_db.user_service.create_user(UserCreate(username='tracking-ui', email='tracking-ui@example.test', password='tracking-ui-test-only'))
    owner = str(user.id); service = ResearchTrackingService(db)
    for symbol in ['002241', '603232', '000503']:
        if not await db.analysis_reports.find_one({'user_id': owner, 'stock_symbol': symbol}):
            doc = report(owner, symbol); doc.update(reports=baseline()['reports'], created_at=baseline()['created_at'])
            await db.analysis_reports.insert_one(doc)
        track = await service.create(symbol, owner)
        if symbol == '603232' or await db[VERSIONS].find_one({'track_id': track['id']}):
            continue
        for day, phase, status in [('2026-10-27','testing','candidate'),('2026-10-28','markup','supported'),('2026-10-29','markup','supported')]:
            snap = price_snapshot(day)
            for i, bar in enumerate(snap['bars']):
                bar.update(open=round(9.5+i*.04,2),close=round(9.52+i*.04+(.16 if i%3 else -.1),2),
                    high=round(9.82+i*.04,2),low=round(9.32+i*.04,2),volume=10000+i*300+(i%3)*2000)
            raw = technical(day, phase, status)
            if symbol == '000503':
                raw = technical(day, 'uncertain', 'uncertain')
                raw['wyckoff'].update(structure_start=None, limitations=['隔离样例：量价证据有分歧，当前阶段不确定'])
            output = validate_update(raw, snap, technical=True)
            if day == '2026-10-29' and symbol == '002241':
                output.update(wyckoff=None, wyckoff_error='隔离样例：新阶段证据未通过校验')
            final = review(); final['strategy']['as_of'] = day; final['strategy']['valid_until'] = '2026-10-30'
            vid = symbol+'-'+day
            await db[VERSIONS].insert_one({'_id':vid,'track_id':track['id'],'user_id':owner,
                'baseline_analysis_id':track['baseline_analysis_id'],'as_of':day,
                'created_at':datetime.fromisoformat(day+'T11:05:00+00:00'), 'status':'completed','errors':[],
                'changed_modules':['technical'],'snapshot':snap,
                'modules':{'technical':{**output,'as_of':day,'inherited':False}},'review':final})
            await db[TRACKS].update_one({'_id':track['id']},{'$set':{'latest_version_id':vid}})
    # Warm the real calendar through the normal DI path, never replace API routes.
    from app.data.core.interface import DataInterface
    from app.data.core.analysis_context import build_context
    await build_context(DataInterface.get_instance(), 'CN', date.today().isoformat())
    yield
    await database.db_manager.close_connections()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CSRFMiddleware)
app.include_router(auth_db.router)
app.include_router(research_tracking.router)
