"""Register the opted-in research consumer on the application's live scheduler."""

import logging

from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings
from app.services.research_tracking import run_due_updates

logger = logging.getLogger(__name__)
JOB_ID = "short_research_tracking"


def register_research_tracking_jobs(scheduler):
    from app.services.intraday_research import run_intraday_tracking
    scheduler.add_job(run_intraday_tracking, IntervalTrigger(minutes=1, timezone=settings.TIMEZONE),
        id="short_research_intraday", name="个股盘中条件跟踪（不调用模型）",
        max_instances=1, misfire_grace_time=60, coalesce=True, replace_existing=True)
    # Registration never runs research. The callback retains the 19:00 gate,
    # per-stock opt-in, daily deduplication and existing model-call budgets.
    scheduler.add_job(
        run_due_updates,
        IntervalTrigger(minutes=10, timezone=settings.TIMEZONE),
        id=JOB_ID,
        name="个股每日研究（仅已启用跟踪）",
        max_instances=1,
        misfire_grace_time=600,
        coalesce=True,
        replace_existing=True,
    )
    logger.info("个股每日研究任务已注册：每10分钟检查，仅19:00后处理已开启的档案")
