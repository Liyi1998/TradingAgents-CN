"""主事件循环桥接回归测试。"""

import asyncio
import threading

import pytest

from app.core.async_utils import await_on_main_loop, set_main_loop


@pytest.mark.asyncio
async def test_await_on_main_loop_routes_worker_loop_coroutine_to_main_loop():
    main_loop = asyncio.get_running_loop()
    main_thread_id = threading.get_ident()
    set_main_loop(main_loop)

    async def operation():
        assert asyncio.get_running_loop() is main_loop
        assert threading.get_ident() == main_thread_id
        return "ok"

    def worker():
        return asyncio.run(await_on_main_loop(operation()))

    try:
        assert await asyncio.to_thread(worker) == "ok"
    finally:
        set_main_loop(None)
