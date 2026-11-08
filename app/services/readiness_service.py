"""Bounded business readiness, separate from process-only liveness."""

import asyncio
from typing import Optional

from app.core.database import db_manager
from app.core.service_lifecycle import ServiceLifecycle

RESOURCE_CHECK_TIMEOUT = 2.0


async def check_readiness(lifecycle: Optional[ServiceLifecycle]) -> dict:
    phase = lifecycle.phase if lifecycle is not None else "not_started"
    result = {"ready": False, "phase": phase, "dependencies": {}}
    if phase != "ready":
        return result

    async def check_dependency(name: str) -> str:
        try:
            health = await asyncio.wait_for(
                db_manager.health_check(check_mongodb=name == "mongodb", check_redis=name == "redis"),
                timeout=RESOURCE_CHECK_TIMEOUT,
            )
            return health[name]["status"]
        except asyncio.TimeoutError:
            return "timeout"
        except Exception:
            return "unavailable"

    names = ("mongodb", "redis")
    statuses = await asyncio.gather(*(check_dependency(name) for name in names))
    result["dependencies"] = dict(zip(names, statuses))
    # Lifecycle may have entered cleanup while the bounded pings were pending.
    result["phase"] = lifecycle.phase
    result["ready"] = lifecycle.phase == "ready" and all(status == "healthy" for status in statuses)
    return result
