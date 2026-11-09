"""Owner-scoped daily research API; reads never start work."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from app.models.research_tracking import CreateTrack, SwitchBaselineRequest, TrackSettings, UpdateRequest
from app.routers.auth_db import get_current_user
from app.services.research_tracking import ResearchTrackingService, TrackingError

router = APIRouter(prefix="/api/research-tracking", tags=["Research Tracking"])


async def execute(operation, *args, **kwargs):
    try:
        return {"success": True, "data": await operation(*args, **kwargs)}
    except TrackingError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("/tracks")
async def list_tracks(user: dict = Depends(get_current_user)):
    return await execute(ResearchTrackingService().list_tracks, str(user["id"]))


@router.get("/stocks/{symbol}")
async def board(
    symbol: str,
    version_id: str | None = Query(None, max_length=128),
    baseline_analysis_id: str | None = Query(None, max_length=128),
    user: dict = Depends(get_current_user),
):
    return await execute(ResearchTrackingService().board, symbol, str(user["id"]), version_id, baseline_analysis_id)


@router.post("/stocks/{symbol}")
async def create(symbol: str, value: CreateTrack, user: dict = Depends(get_current_user)):
    return await execute(ResearchTrackingService().create, symbol, str(user["id"]), value.analysis_id)


@router.get("/stocks/{symbol}/market-chart")
async def market_chart(symbol: str, force_refresh: bool = Query(False), user: dict = Depends(get_current_user)):
    """Free single-stock data refresh; never creates a research/model job."""
    from app.services.unified_stock_service import UnifiedStockService

    if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise HTTPException(400, "仅支持六位 A 股代码")
    try:
        return {
            "success": True,
            "data": await UnifiedStockService().get_cn_market_chart(symbol, force_refresh=force_refresh),
        }
    except (ValueError, TimeoutError):
        raise HTTPException(503, "日线获取暂未完成；研究版本保持原样，请稍后刷新行情") from None


@router.put("/{track_id}/settings")
async def settings(track_id: str, value: TrackSettings, user: dict = Depends(get_current_user)):
    return await execute(ResearchTrackingService().settings, track_id, str(user["id"]), value)


@router.get("/{track_id}/intraday")
async def intraday(track_id: str, user: dict = Depends(get_current_user)):
    """Read-only strategy evaluation; may fetch free bars, never calls a model."""
    from app.services.intraday_research import IntradayResearchService

    return await execute(IntradayResearchService().observe, track_id, str(user["id"]))


def get_structure_service():
    from app.services.structure_auxiliary import StructureAuxiliaryService

    return StructureAuxiliaryService()


@router.get("/{track_id}/structure-auxiliary")
async def structure_auxiliary(
    track_id: str, user: dict = Depends(get_current_user), service=Depends(get_structure_service)
):
    """Opt-in TPO/SMC sidecar. Does not change reports, actions, or Wyckoff."""
    return await execute(service.observe, track_id, str(user["id"]))


@router.post("/{track_id}/updates")
async def update(
    track_id: str, value: UpdateRequest, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user)
):
    service = ResearchTrackingService()
    result = await execute(
        service.enqueue,
        track_id,
        str(user["id"]),
        retry_failed=value.retry_failed,
        manual_extra_calls=value.manual_extra_calls,
        expected_baseline_analysis_id=value.expected_baseline_analysis_id,
    )
    background_tasks.add_task(service.execute, result["data"]["id"])
    return result


@router.post("/{track_id}/baseline")
async def switch_baseline(track_id: str, value: SwitchBaselineRequest, user: dict = Depends(get_current_user)):
    return await execute(
        ResearchTrackingService().switch_baseline,
        track_id,
        str(user["id"]),
        value.analysis_id,
        value.expected_baseline_analysis_id,
    )
