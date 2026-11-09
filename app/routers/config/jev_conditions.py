"""Admin-only controls; never makes model calls or exposes credentials."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException

from app.routers.auth_db import require_admin
from app.services.jev_condition_config_service import (
    ConditionSettingsUpdate, SettingsConflict, SettingsRejected,
    get_condition_settings, update_condition_settings,
)

router = APIRouter(tags=["Config"])


async def _run(operation, *args):
    try:
        data = await asyncio.to_thread(operation, *args)
        return {"success": True, "data": data, "message": ""}
    except SettingsConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except SettingsRejected as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        # No raw filesystem/validation errors: they may contain credential material.
        raise HTTPException(503, "Jev 配置暂不可用，请重试或联系管理员检查配置权限与格式") from None


@router.get("/jev-conditions")
async def read_settings(_admin=Depends(require_admin)):
    return await _run(get_condition_settings)


@router.put("/jev-conditions")
async def save_settings(update: ConditionSettingsUpdate, _admin=Depends(require_admin)):
    return await _run(update_condition_settings, update)
