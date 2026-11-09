"""Actual facade/Mongo trace integration, opt-in isolated DB only; no mocks."""
import asyncio
import os

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("MONGODB_DATABASE") != "tradingagents_v21_validation_20261008",
    reason="Requires the dedicated V2.1 real Mongo/Redis validation environment",
)


@pytest.mark.asyncio
async def test_real_facade_trace_is_shared_and_task_cutoff_is_respected():
    import app.core.database as database
    from app.core.config import settings
    from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
    from app.data.core.interface import DataInterface
    from app.data.storage.mongo.client import reset_client
    from app.engine.data_preflight import ACTIVE_PREFLIGHT, PreflightSession, invoke_shared, summarize_result
    from app.engine.tools.datasources.registry import DatasourceSpec

    from isolated_database import require_isolated_database

    require_isolated_database()
    reset_client()
    await database.init_database()
    DataInterface.reset_instance()
    di = DataInterface.get_instance()
    context = {"market": "CN", "as_of": "2026-10-07"}
    session = PreflightSession()
    context_token = ACTIVE_ANALYSIS_CONTEXT.set(context)
    session_token = ACTIVE_PREFLIGHT.set(session)
    spec = DatasourceSpec("actual_calendar", "Actual calendar", ["trade_calendar"], ["CN"], di.read, {}, "Actual Mongo read")
    args = {"market": "CN", "domain": "trade_calendar", "start_date": "2026-09-22", "end_date": "2026-10-07"}
    try:
        first, second = await asyncio.gather(invoke_shared(spec, args), invoke_shared(spec, args))
        assert first is second and len(session.calls) == 1
        assert first.result["data"] and len(first.coverage) == 1
        assert first.coverage[0]["market"] == "CN"
        assert first.coverage[0]["domain"] == "trade_calendar"
        assert first.coverage[0]["start_date"] == "2026-09-22"
        assert first.coverage[0]["end_date"] == "2026-10-07"
        assert all("2026-09-22" <= row["cal_date"] <= "2026-10-07" for row in first.result["data"])
        status, _ = summarize_result({"status": "success", "data": first.result["data"]},
                                    ["trade_calendar"], context, coverage=first.coverage, args=args)
        assert status == "available"
        # A genuinely empty read remains failed/unknown coverage, never borrowed success.
        empty = await invoke_shared(spec, {**args, "start_date": "2000-01-01", "end_date": "2000-01-02"})
        assert not empty.result["data"]
        assert empty.coverage[0]["quality"]["status"] == "failed"
        assert len(session.calls) == 2
    finally:
        await session.close()
        ACTIVE_PREFLIGHT.reset(session_token)
        ACTIVE_ANALYSIS_CONTEXT.reset(context_token)
        await database.close_database()
        DataInterface.reset_instance()
        reset_client()
