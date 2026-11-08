"""
AKShare 反爬虫与限流工具

复用 app/utils/anti_scraping.py 中的 AntiScrapingSession 和 ThreadSafeRateLimiter。
"""

import logging
import threading

logger = logging.getLogger(__name__)

_session = None
_rate_limiter = None
_init_lock = threading.Lock()


def get_anti_scraping_session():
    """获取反爬虫会话单例"""
    global _session
    if _session is None:
        with _init_lock:
            if _session is None:
                try:
                    from app.data.sources.cn.eastmoney.direct import AntiScrapingSession
                    _session = AntiScrapingSession()
                except ImportError:
                    logger.debug("AntiScrapingSession 不可用，使用标准请求")
    return _session


def get_rate_limiter():
    """获取限流器单例"""
    global _rate_limiter
    if _rate_limiter is None:
        with _init_lock:
            if _rate_limiter is None:
                try:
                    from app.data.sources.cn.eastmoney.direct import (
                        ThreadSafeRateLimiter,
                    )

                    # 东财系接口对批量并发敏感；与路由层一致，
                    # 进程内也严格串行，避免 AKShare 内部裸 requests
                    # 绕过全局 polite interval。
                    _rate_limiter = ThreadSafeRateLimiter(
                        min_interval=1.0, burst=1
                    )
                except ImportError:
                    _rate_limiter = None
    return _rate_limiter


def wait_rate_limit():
    """执行限流等待"""
    limiter = get_rate_limiter()
    if limiter:
        limiter.acquire()
