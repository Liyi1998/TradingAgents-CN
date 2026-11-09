"""Exercise real application startup and its actual APScheduler callback.

Run against isolated Mongo/Redis with startup network consumers disabled. No
model substitutes or fake scheduler: the registered research coroutine really
executes, with no opted-in tracks, and must not create research/model work.
"""

import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.worker.research_tracking_jobs import JOB_ID, register_research_tracking_jobs
from app.services.research_tracking import run_due_updates


async def test_registration_is_idempotent_and_preserves_existing_jobs():
    from app.worker.screening_jobs import JOB_ID as SCREENING_ID, register_screening_jobs

    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    scheduler.start(paused=True)
    try:
        register_screening_jobs(scheduler)
        register_research_tracking_jobs(scheduler)
        register_research_tracking_jobs(scheduler)
        assert {job.id for job in scheduler.get_jobs()} == {JOB_ID, SCREENING_ID, "short_research_intraday"}
        job = scheduler.get_job(JOB_ID)
        assert job.func is run_due_updates
        assert job.trigger.interval.total_seconds() == 600
        assert str(job.trigger.timezone) == "Asia/Shanghai"
        assert job.max_instances == 1 and job.coalesce
        assert job.misfire_grace_time == 600
        intraday = scheduler.get_job("short_research_intraday")
        assert intraday.trigger.interval.total_seconds() == 60
        assert intraday.max_instances == 1 and intraday.coalesce
        assert scheduler.state == 2
    finally:
        scheduler.shutdown(wait=False)
        await asyncio.sleep(0)


async def test_live_startup_registers_and_executes_research_callback(real_mongo_db):
    from app.core.config import settings
    from app.data.scheduler.engine import SchedulerEngine
    from app.data.scheduler.monitors import SchedulerMonitor
    from app.main import _init_scheduler
    from app.services import scheduler_service

    assert "test" in real_mongo_db.name
    assert not any((settings.QUOTES_INGEST_ENABLED, settings.FREE_DATA_AUTO_BACKFILL_ENABLED,
                    settings.DATA_FRESHNESS_PATROL_ENABLED, settings.DATA_REFRESH_CONSUMER_ENABLED))
    assert await real_mongo_db.research_tracks.count_documents({"settings.auto_enabled": True}) == 0
    protected = ["research_update_runs", "research_versions", "research_update_steps",
                 "research_update_budgets", "token_usage", "analysis_tasks"]
    before = {name: await real_mongo_db[name].count_documents({}) for name in protected}
    old_engine = SchedulerEngine._instance
    old_scheduler = scheduler_service._scheduler_instance
    old_service = scheduler_service._scheduler_service
    SchedulerEngine._instance = None
    scheduler_service._scheduler_service = None
    scheduler = None
    try:
        scheduler = await _init_scheduler(logging.getLogger(__name__))
        scheduler.pause()
        service = scheduler_service.get_scheduler_service()
        assert service.scheduler is scheduler
        job = scheduler.get_job(JOB_ID)
        assert job is not None and job.func is run_due_updates
        details = await service.get_job(JOB_ID)
        assert details["next_run_time"] is not None
        assert not scheduler.get_job("basics_sync_service")

        # Execute only the real research callback. Other data-domain cron jobs
        # belong to startup wiring, not this focused, no-network acceptance.
        for other in scheduler.get_jobs():
            if other.id != JOB_ID:
                scheduler.remove_job(other.id)
        result = asyncio.get_running_loop().create_future()

        def completed(event):
            if event.job_id == JOB_ID and not result.done():
                result.set_result(event)

        scheduler.add_listener(completed, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        scheduler.modify_job(JOB_ID, next_run_time=datetime.now(timezone.utc))
        scheduler.resume()
        event = await asyncio.wait_for(result, timeout=10)
        scheduler.pause()
        assert event.code == EVENT_JOB_EXECUTED and event.exception is None
        assert {name: await real_mongo_db[name].count_documents({}) for name in protected} == before
    finally:
        if scheduler and scheduler.running:
            scheduler.shutdown(wait=False)
            await asyncio.sleep(0)
        SchedulerMonitor().stop()
        SchedulerEngine._instance = old_engine
        scheduler_service._scheduler_instance = old_scheduler
        scheduler_service._scheduler_service = old_service
