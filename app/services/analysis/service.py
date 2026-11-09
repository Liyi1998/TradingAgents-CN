"""
股票分析服务
整合了原 simple_analysis_service.py 和 analysis_service.py 的功能
"""
# data-access-exempt: 应用层集合（analysis_tasks/reports/users/notifications 等）直查属架构豁免；业务数据读取已收敛至 DataInterface

import atexit
import logging
from typing import Dict, Any, List, Optional
import concurrent.futures


from app.models.user import PyObjectId
from bson import ObjectId
from app.core.database import get_mongo_db, get_redis_client
from app.services.queue_service import QueueService
from app.services.usage_statistics_service import UsageStatisticsService
from app.services.progress.tracker import RedisProgressTracker
from app.services.memory_state_manager import get_memory_state_manager
from app.services.websocket_manager import get_websocket_manager
from app.core.config import settings

# 设置日志
logger = logging.getLogger("app.services.analysis_service")

# config_service imported from app.services.config_service (facade singleton)

from app.services.analysis.helpers import (
    _get_stock_info_async,
    _is_placeholder_stock_name,
)
from app.services.analysis.tasks import TasksMixin
from app.services.analysis.reports import ReportsMixin
from app.services.analysis.prefetch import PrefetchMixin
from app.services.analysis.executor import ExecutorMixin


class AnalysisService(TasksMixin, ReportsMixin, PrefetchMixin, ExecutorMixin):
    def __init__(self):
        # 初始化组件
        self._trading_graph_cache = {}
        self.memory_manager = get_memory_state_manager()
        self._progress_trackers: Dict[str, RedisProgressTracker] = {}
        # 有界 LRU 缓存（防止高频查询任务下内存无限增长）
        from app.core.lru_cache import BoundedLRUCache

        self._stock_name_cache = BoundedLRUCache(maxsize=512, name="stock_name_cache")
        # 线程池上限可配置（settings.ANALYSIS_THREAD_POOL_SIZE）
        pool_workers = getattr(settings, "ANALYSIS_THREAD_POOL_SIZE", 3)
        self._thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=pool_workers)
        atexit.register(self._shutdown_pool)
        logger.info(f"🔧 [服务初始化] 线程池最大并发数: {pool_workers}")

        # 队列和统计服务
        try:
            redis_client = get_redis_client()
            self.queue_service = QueueService(redis_client)
            self.usage_service = UsageStatisticsService()
        except Exception as e:
            logger.warning(f"⚠️ 队列或统计服务初始化失败: {e}")

        # 设置 WebSocket 管理器
        try:
            self.memory_manager.set_websocket_manager(get_websocket_manager())
        except ImportError:
            logger.warning("⚠️ WebSocket 管理器不可用")

        logger.info(f"🔧 [服务初始化] AnalysisService 实例ID: {id(self)}")


    def _shutdown_pool(self, wait: bool = False):
        """关闭线程池。可在 lifespan 关闭时显式调用（wait=True）或由 atexit 触发。"""
        try:
            if self._thread_pool:
                self._thread_pool.shutdown(wait=wait)
        except Exception as e:
            logger.debug(f"线程池关闭失败（atexit 阶段可能日志已关闭）: {e}")

    # -------------------------------------------------------------------------
    # Private Methods
    # -------------------------------------------------------------------------


    async def _resolve_stock_name(self, code: Optional[str]) -> str:
        """解析股票名称（带缓存，异步直查 basic_info）"""
        if not code:
            return ""
        cached = self._stock_name_cache.get(code)
        if cached and not _is_placeholder_stock_name(cached, code):
            return cached
        name = None
        try:
            if _get_stock_info_async:
                info = await _get_stock_info_async(code)
                if isinstance(info, dict):
                    name = info.get("name")
        except Exception as e:
            logger.debug(f"解析股票名称失败: {e}")
        if not name:
            name = f"股票{code}"
        # 占位名不能进入长期缓存；预拉取 basic_info 后必须允许再次解析真实名称。
        if not _is_placeholder_stock_name(name, code):
            self._stock_name_cache.set(code, name)
        return name


    def _enrich_stock_names(self, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """为任务列表补齐股票名称(就地更新)

        仅查询内存缓存，不触发同步 DB/IO 操作，避免阻塞事件循环。
        未命中缓存时使用 stock_code 作为兜底名称。
        """
        try:
            for t in tasks:
                code = t.get("stock_code") or t.get("stock_symbol")
                name = t.get("stock_name")
                if not name and code:
                    cached = self._stock_name_cache.get(code)
                    t["stock_name"] = cached if cached else f"股票{code}"
        except Exception as e:
            logger.warning(f"⚠️ 补齐股票名称时出现异常: {e}")
        return tasks


    async def _convert_user_id_async(self, user_id: str) -> PyObjectId:
        """异步将字符串用户ID转换为PyObjectId，避免在事件循环中执行同步数据库查询"""
        try:
            if user_id == "admin":
                try:
                    db = get_mongo_db()
                    admin_doc = await db.users.find_one({"username": "admin"}, {"_id": 1})
                    if admin_doc:
                        return PyObjectId(admin_doc["_id"])
                except Exception as e:
                    logger.warning(f"⚠️ 异步查询 admin 用户失败: {e}")
                raise ValueError("无法确定 admin 用户的 ObjectId")
            return PyObjectId(ObjectId(user_id))
        except Exception as e:
            logger.warning(f"⚠️ 用户ID转换失败: user_id={user_id}, error={e}")
            raise ValueError(f"无效的用户ID: {user_id}") from e


    def _serialize_for_response(self, value: Any) -> Any:
        """递归转换 Mongo 特定类型为可序列化格式"""
        if isinstance(value, ObjectId):
            return str(value)
        if isinstance(value, list):
            return [self._serialize_for_response(v) for v in value]
        if isinstance(value, dict):
            return {k: self._serialize_for_response(v) for k, v in value.items()}
        return value


    def _get_sync_mongo_db(self):
        """获取同步 MongoDB 数据库连接（使用全局统一管理）"""
        from app.core.database import get_mongo_db_sync

        return get_mongo_db_sync()



analysis_service: Optional[AnalysisService] = None


def get_analysis_service() -> AnalysisService:
    """获取分析服务实例"""
    global analysis_service
    if analysis_service is None:
        analysis_service = AnalysisService()
    return analysis_service
