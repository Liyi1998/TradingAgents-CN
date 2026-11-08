"""Owner-scoped explicit experiments; no router database access."""
from fastapi import APIRouter, Depends, HTTPException

from app.llm.jev_decisions import ExperimentError, RunInput, StrategyInput
from app.routers.auth_db import get_current_user
from app.services.jev_decision_service import get_experiments, preview_experiment, run_experiment
from app.services.jev_final_action_service import get_final_action

router = APIRouter(prefix="/api/jev-decisions", tags=["Jev Decisions"])


async def _execute(operation, *args):
    try:
        return {"success": True, "data": await operation(*args)}
    except ExperimentError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(503, "动作试验暂不可用；原分析报告不受影响") from None


@router.get("/{analysis_id}")
async def history(analysis_id: str, user: dict = Depends(get_current_user)):
    return await _execute(get_experiments, analysis_id, str(user["id"]))


@router.post("/{analysis_id}/preview")
async def preview(analysis_id: str, value: StrategyInput, user: dict = Depends(get_current_user)):
    return await _execute(preview_experiment, analysis_id, str(user["id"]), value)


@router.get("/{analysis_id}/final-action")
async def final_action(analysis_id: str, user: dict = Depends(get_current_user)):
    result = await get_final_action(analysis_id, str(user["id"]))
    if result is None:
        raise HTTPException(404, "暂无可访问的自动动作建议；不会补跑旧报告")
    return {"success": True, "data": result}


@router.post("/{analysis_id}/{preview_id}/run")
async def run(analysis_id: str, preview_id: str, value: RunInput, user: dict = Depends(get_current_user)):
    return await _execute(run_experiment, analysis_id, str(user["id"]), preview_id, value.confirm_external)
