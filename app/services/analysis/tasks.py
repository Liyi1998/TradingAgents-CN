"""
股票分析服务
整合了原 simple_analysis_service.py 和 analysis_service.py 的功能
"""
# data-access-exempt: 应用层集合（analysis_tasks/reports/users/notifications 等）直查属架构豁免；业务数据读取已收敛至 DataInterface

import asyncio
import uuid
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from app.engine.decision_semantics import (
    collect_decision_source,
    format_decision_recommendation,
    merge_decision_key_points,
    normalize_decision,
    synchronize_summary,
)
from app.data.core.interface import DataInterface

from app.models.analysis import (
    AnalysisParameters,
    AnalysisTask,
    AnalysisBatch,
    AnalysisStatus,
    BatchStatus,
    SingleAnalysisRequest,
    BatchAnalysisRequest,
)
from app.core.database import get_mongo_db
from app.services.progress.tracker import get_progress_by_id
from app.services.config_provider import provider as config_provider
from app.services.memory_state_manager import get_memory_state_manager, TaskStatus
from app.services.analysis_horizon import horizon_from_snapshot
from app.utils.timezone import now_utc

# 设置日志
logger = logging.getLogger("app.services.analysis_service")

# config_service imported from app.services.config_service (facade singleton)

from app.services.analysis.helpers import (
    _extract_result_risk_and_confidence,
    _TASK_STATUS_MESSAGES,
)
from app.services.analysis.providers import (
    resolve_analysis_models_sync,
)


class TasksMixin:
    async def create_analysis_task(self, user_id: str, request: SingleAnalysisRequest) -> Dict[str, Any]:
        """创建分析任务（立即返回，不执行分析）"""
        try:
            task_id = str(uuid.uuid4())
            stock_code = request.get_symbol()
            if not stock_code:
                raise ValueError("股票代码不能为空")

            # 旧前端可能仍提交已禁用的 qwen 默认值。任务入库前统一解析为
            # 当前启用、具备密钥且角色适配的模型，避免预取完成后才失败。
            if not request.parameters:
                request.parameters = AnalysisParameters()
            if request.parameters.analysis_horizon != "short":
                from app.engine.orchestrator.workflow.research import prepare_workflow
                selected = (request.parameters.selected_nodes if request.parameters.selected_nodes is not None
                            else request.parameters.selected_analysts)
                _, _, research_plan = await asyncio.to_thread(
                    prepare_workflow,
                    {**request.parameters.model_dump(), "workflow_slug": request.workflow_slug}, selected,
                )
                request.workflow_slug = research_plan.workflow_slug
                request.parameters.selected_nodes = list(research_plan.params.selected_nodes)
            analyst_model, debate_model = await asyncio.to_thread(
                resolve_analysis_models_sync,
                request.parameters.analyst_model,
                request.parameters.debate_model,
            )
            request.parameters.analyst_model = analyst_model
            request.parameters.debate_model = debate_model

            logger.info(f"📝 创建分析任务: {task_id} - {stock_code}")

            # 任务级 workflow_slug 并入 parameters 快照（P5-c：任务创建即可追溯所用工作流）
            parameters = request.parameters.model_dump() if request.parameters else {}
            if request.workflow_slug:
                parameters["workflow_slug"] = request.workflow_slug

            # 股票名称解析一次、两处复用（内存态 + DB 文档），避免重复查询
            stock_name = await self._resolve_stock_name(stock_code)

            # 在内存中创建任务状态
            await self.memory_manager.create_task(
                task_id=task_id,
                user_id=user_id,
                stock_code=stock_code,
                parameters=parameters,
                stock_name=stock_name,
            )

            # 写入MongoDB
            code = stock_code
            name = stock_name
            try:
                db = get_mongo_db()
                await db.analysis_tasks.update_one(
                    {"task_id": task_id},
                    {
                        "$setOnInsert": {
                            "task_id": task_id,
                            "user_id": user_id,
                            "stock_code": code,
                            "stock_symbol": code,
                            "stock_name": name,
                            "status": "pending",
                            "progress": 0,
                            "created_at": now_utc(),
                            "parameters": parameters,
                            "analysis_horizon": request.parameters.analysis_horizon,
                            "resume_source_task_id": request._resume_source_task_id,
                            "reused_report_keys": list(request._resume_reports),
                        }
                    },
                    upsert=True,
                )
            except Exception as e:
                logger.error(f"❌ 创建任务时写入MongoDB失败: {e}", exc_info=True)

            return {
                "task_id": task_id,
                "status": "pending",
                "analysis_horizon": request.parameters.analysis_horizon,
                "message": "任务已创建，等待执行",
            }

        except Exception as e:
            logger.error(f"❌ 创建分析任务失败: {e}", exc_info=True)
            raise


    async def submit_single_analysis(self, user_id: str, request: SingleAnalysisRequest) -> Dict[str, Any]:
        """
        提交单股分析任务 (兼容旧 AnalysisService 接口)
        注意：这个方法现在只是 create_analysis_task 的别名，
        实际执行需要在调用处通过 BackgroundTasks 或其他方式触发 execute_analysis_background
        """
        return await self.create_analysis_task(user_id, request)


    async def submit_batch_analysis(self, user_id: str, request: BatchAnalysisRequest) -> Dict[str, Any]:
        """提交批量分析任务 (保留原功能)"""
        try:
            batch_id = str(uuid.uuid4())
            converted_user_id = await self._convert_user_id_async(user_id)

            # 读取配置
            effective_settings = await config_provider.get_effective_system_settings()
            settings_payload = effective_settings.get("config", effective_settings)
            params = request.parameters or AnalysisParameters()

            # 模型默认值：系统设置优先，未设置时按"第一个启用的模型"推荐，不写死具体模型 ID
            if not getattr(params, "analyst_model", None) or not getattr(params, "debate_model", None):
                from app.services.model_capability_service import (
                    get_model_capability_service,
                )

                rec_analyst, rec_debate = get_model_capability_service().recommend_models()
                if not getattr(params, "analyst_model", None):
                    params.analyst_model = settings_payload.get("analyst_model") or rec_analyst
                if not getattr(params, "debate_model", None):
                    params.debate_model = settings_payload.get("debate_model") or rec_debate

            params.analyst_model, params.debate_model = await asyncio.to_thread(
                resolve_analysis_models_sync,
                params.analyst_model,
                params.debate_model,
            )

            stock_symbols = request.get_symbols()

            batch = AnalysisBatch(
                batch_id=batch_id,
                user_id=converted_user_id,
                title=request.title,
                description=request.description,
                total_tasks=len(stock_symbols),
                parameters=params,
                status=BatchStatus.PENDING,
            )

            tasks = []
            for symbol in stock_symbols:
                task_id = str(uuid.uuid4())
                task = AnalysisTask(
                    task_id=task_id,
                    batch_id=batch_id,
                    user_id=converted_user_id,
                    symbol=symbol,
                    stock_code=symbol,
                    parameters=batch.parameters,
                    status=AnalysisStatus.PENDING,
                )
                tasks.append(task)

            db = get_mongo_db()
            await db.analysis_batches.insert_one(batch.dict(by_alias=True))
            await db.analysis_tasks.insert_many([task.dict(by_alias=True) for task in tasks])

            for task in tasks:
                queue_params = task.parameters.dict() if task.parameters else {}
                queue_params.update(
                    {
                        "task_id": task.task_id,
                        "symbol": task.symbol,
                        "stock_code": task.symbol,
                        "user_id": str(task.user_id),
                        "batch_id": task.batch_id,
                        "created_at": task.created_at.isoformat() if task.created_at else None,
                    }
                )
                await self.queue_service.enqueue_task(
                    user_id=str(converted_user_id),
                    symbol=task.symbol,
                    params=queue_params,
                    batch_id=task.batch_id,
                )

            return {
                "batch_id": batch_id,
                "total_tasks": len(tasks),
                "status": BatchStatus.PENDING,
                "message": f"已提交{len(tasks)}个分析任务到队列",
            }
        except Exception as e:
            logger.error(f"提交批量分析任务失败: {e}", exc_info=True)
            raise


    async def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        try:
            await self._update_task_status(task_id, AnalysisStatus.CANCELLED, 0)
            await self.queue_service.cancel_task(task_id)
            return True
        except Exception as e:
            logger.error(f"取消任务失败: {task_id} - {e}", exc_info=True)
            return False

    # -------------------------------------------------------------------------
    # Internal Execution Logic (from simple_analysis_service.py)
    # -------------------------------------------------------------------------


    async def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务状态 (包含详细进度)"""
        global_memory_manager = get_memory_state_manager()
        result = await global_memory_manager.get_task_dict(task_id)
        if result:
            result["analysis_horizon"] = horizon_from_snapshot(result)
            # 状态响应（/status 轮询、WS 鉴权、cancel/events 校验）只消费状态字段，
            # 全量结果由 /result 端点按需读取，这里剥离避免大对象随轮询外带
            result.pop("result_data", None)
            redis_progress = get_progress_by_id(task_id)
            if redis_progress:
                result.update(
                    {
                        "progress": redis_progress.get("progress_percentage", result.get("progress", 0)),
                        "message": redis_progress.get("last_message", result.get("message", "")),
                        "steps": redis_progress.get("steps", []),
                    }
                )
        return result


    async def list_user_tasks(
        self,
        user_id: str,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """获取用户任务列表 (数据库 + 内存状态合并)"""
        # 兼容性处理：processing/running 统一查询
        if status in ("processing", "running"):
            status = "running"

        # 构建查询条件
        query = {"user_id": user_id}
        if status == "running":
            query["status"] = {"$in": ["running", "pending", "processing"]}
        elif status:
            query["status"] = status

        try:
            db = get_mongo_db()
            # 按创建时间倒序；列表页不消费 result（约占单文档 99%）与
            # workflow_snapshot，用 exclusion projection 避免 5s 轮询拉全量报告
            cursor = (
                db.analysis_tasks.find(query, {"result": 0, "workflow_snapshot": 0})
                .sort("created_at", -1)
                .skip(offset)
                .limit(limit)
            )
            db_tasks = await cursor.to_list(length=limit)

            # 批量获取内存中的实时状态（一次加锁，替代逐条查询）
            task_ids = [t.get("task_id") for t in db_tasks if t.get("task_id")]
            memory_map = self.memory_manager.batch_get_task_dicts(task_ids)

            results = []
            for task in db_tasks:
                if "_id" in task:
                    task["_id"] = str(task["_id"])

                task_id = task.get("task_id")
                if task_id and task_id in memory_map:
                    memory_task = memory_map[task_id]
                    task["status"] = memory_task.get("status", task.get("status"))
                    task["progress"] = memory_task.get("progress", task.get("progress"))
                    task["message"] = memory_task.get("message", task.get("message"))
                    task["current_step"] = memory_task.get("current_step", task.get("current_step"))

                task["analysis_horizon"] = horizon_from_snapshot(task)
                results.append(task)

            # 如果数据库返回为空，可能是因为所有数据都在内存中（极少见情况，例如DB写入失败但内存成功）
            # 或者如果是刚启动，DB 为空也是正常的。
            # 这里我们只返回 DB 的结果，因为 create_analysis_task 保证了先写 DB。

            enriched = self._enrich_stock_names(results)
            return self._serialize_for_response(enriched)

        except Exception as e:
            logger.error(f"❌ 获取用户任务列表失败 (DB): {e}", exc_info=True)
            # 降级：如果 DB 失败，尝试返回内存中的数据
            status_enum = None
            if status:
                try:
                    status_enum = TaskStatus(status)
                except ValueError:
                    pass

            tasks = await self.memory_manager.list_user_tasks(
                user_id=user_id, status=status_enum, limit=limit, offset=offset
            )
            for task in tasks:
                task["analysis_horizon"] = horizon_from_snapshot(task)
            enriched = self._enrich_stock_names(tasks)
            return self._serialize_for_response(enriched)


    async def query_user_tasks(
        self,
        user_id: str,
        status: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        symbol: Optional[str] = None,
        market_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """查询用户任务列表（支持复杂筛选与分页）"""
        # 兼容性处理
        if status == "processing":
            status = "running"

        # 构建查询条件
        query = {"user_id": user_id}

        if status:
            if status == "running":
                # 前端"进行中"包括 processing, running, pending
                query["status"] = {"$in": ["running", "pending", "processing"]}
            else:
                query["status"] = status

        if symbol:
            # 同时匹配 symbol 和 stock_code
            query["$or"] = [
                {"symbol": symbol},
                {"stock_code": symbol},
                {"stock_symbol": symbol},
            ]

        if market_type:
            query["parameters.market_type"] = market_type

        # 时间范围查询
        date_query = {}
        if start_date:
            try:
                # 假设传入的是 YYYY-MM-DD
                s_date = datetime.strptime(start_date, "%Y-%m-%d")
                date_query["$gte"] = s_date
            except Exception as e:
                logger.debug(f"日期解析失败: start_date={start_date}: {e}")
                pass
        if end_date:
            try:
                e_date = datetime.strptime(end_date, "%Y-%m-%d")
                # 结束日期加一天，包含当天
                e_date = e_date.replace(hour=23, minute=59, second=59, microsecond=999999)
                date_query["$lte"] = e_date
            except Exception as e:
                logger.debug(f"日期解析失败: end_date={end_date}: {e}")
                pass

        if date_query:
            query["created_at"] = date_query

        try:
            db = get_mongo_db()

            # 获取总数
            total = await db.analysis_tasks.count_documents(query)

            # 分页查询；历史页同样排除 result/workflow_snapshot 大字段（见 list_user_tasks）
            skip = (page - 1) * page_size
            cursor = (
                db.analysis_tasks.find(query, {"result": 0, "workflow_snapshot": 0})
                .sort("created_at", -1)
                .skip(skip)
                .limit(page_size)
            )
            db_tasks = await cursor.to_list(length=page_size)

            # 批量获取内存中的实时状态（一次加锁，替代逐条查询）
            task_ids = [t.get("task_id") for t in db_tasks if t.get("task_id")]
            memory_map = self.memory_manager.batch_get_task_dicts(task_ids)

            results = []
            for task in db_tasks:
                if "_id" in task:
                    task["_id"] = str(task["_id"])

                task_id = task.get("task_id")
                if task_id and task_id in memory_map:
                    memory_task = memory_map[task_id]
                    task["status"] = memory_task.get("status", task.get("status"))
                    task["progress"] = memory_task.get("progress", task.get("progress"))
                    task["message"] = memory_task.get("message", task.get("message"))
                    task["current_step"] = memory_task.get("current_step", task.get("current_step"))

                task["analysis_horizon"] = horizon_from_snapshot(task)
                results.append(task)

            enriched_tasks = self._enrich_stock_names(results)

            return self._serialize_for_response(
                {
                    "tasks": enriched_tasks,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                }
            )

        except Exception as e:
            logger.error(f"❌ 查询用户任务列表失败 (DB): {e}", exc_info=True)
            # 降级处理：使用 list_user_tasks 获取并手动过滤（不太精确但可用）
            all_tasks = await self.list_user_tasks(user_id, status, limit=1000)  # 获取最近1000条

            # 手动过滤
            filtered = []
            for t in all_tasks:
                if symbol:
                    s = t.get("symbol") or t.get("stock_code") or t.get("stock_symbol")
                    if s != symbol:
                        continue
                if market_type and t.get("parameters", {}).get("market_type") != market_type:
                    continue
                filtered.append(t)

            # 手动分页
            start = (page - 1) * page_size
            paginated = filtered[start : start + page_size]

            return self._serialize_for_response(
                {
                    "tasks": paginated,
                    "total": len(filtered),
                    "page": page,
                    "page_size": page_size,
                }
            )


    async def _update_task_status(
        self,
        task_id: str,
        status: AnalysisStatus,
        progress: int,
        error_message: str = None,
    ):
        """更新任务状态到MongoDB"""
        try:
            db = get_mongo_db()
            update_data = {
                "status": status,
                "progress": progress,
                "updated_at": now_utc(),
            }
            if status == AnalysisStatus.PROCESSING and progress == 10:
                update_data["started_at"] = now_utc()
            elif status == AnalysisStatus.COMPLETED:
                update_data["completed_at"] = now_utc()
            elif status == AnalysisStatus.FAILED:
                update_data["last_error"] = error_message
                update_data["error_message"] = error_message
                update_data["message"] = "分析失败"
                update_data["completed_at"] = now_utc()
            update_op: Dict[str, Any] = {"$set": update_data}
            if status == AnalysisStatus.PROCESSING:
                # 同一任务重试时不能继续向前端暴露上次失败原因。
                update_op["$unset"] = {"last_error": "", "error_message": ""}
            await db.analysis_tasks.update_one({"task_id": task_id}, update_op)
        except Exception as e:
            logger.error(f"❌ 更新任务状态失败: {task_id} - {e}", exc_info=True)


    async def get_task_with_status_fallback(
        self,
        task_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        获取任务状态，依次尝试：内存 → analysis_tasks 集合 → analysis_reports 集合。

        Args:
            task_id: 任务 ID
            user_id: 可选用户 ID。非 None 时仅返回该用户拥有的任务（admin 路径传 None 跳过校验）。

        Returns:
            包含 "source" 字段标记数据来源的字典（mongodb_tasks / mongodb_reports），
            或 None 表示所有来源均未找到。
        """
        status_info, _ = await self._get_status_with_record(task_id, user_id)
        return status_info


    async def _get_status_with_record(
        self,
        task_id: str,
        user_id: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """get_task_with_status_fallback 的核心实现。

        额外返回 analysis_tasks 命中时的原始记录（第二返回值），供
        get_task_overview 复用，避免对同一 filter 重复 find_one。
        内存 / reports 兜底命中时原始记录为空 dict。
        """
        # 1) 先走现有的 get_task_status（内存 + Redis 进度）
        result = await self.get_task_status(task_id)
        if result:
            # 内存命中后仍要校验所有权：admin (user_id=None) 直通；普通用户需匹配
            if user_id is None:
                return result, {}
            owner = result.get("user_id")
            if owner is None or owner == user_id:
                return result, {}
            logger.warning(f"⚠️ 用户 {user_id} 越权访问任务状态 task_id={task_id} (owner={owner})")
            return None, {}

        # 2) 从 analysis_tasks 集合查找（带 user_id 过滤）
        # 老数据兼容：早期记录可能不含 user_id 字段，用 $or 兜底放行无主任务
        try:
            db = get_mongo_db()
            filter_doc: Dict[str, Any] = {"task_id": task_id}
            if user_id is not None:
                filter_doc["$or"] = [
                    {"user_id": user_id},
                    {"user_id": {"$exists": False}},
                    {"user_id": None},
                ]
            task_result = await db.analysis_tasks.find_one(filter_doc)
        except Exception as e:
            logger.warning(f"⚠️ get_task_with_status_fallback 查询 analysis_tasks 失败: {e}")
            task_result = None

        if task_result:
            status = task_result.get("status", "pending")
            progress = task_result.get("progress", 0)
            start_time = task_result.get("started_at") or task_result.get("created_at")
            end_time = task_result.get("completed_at")
            elapsed_time = 0.0
            # 仅对进行中的任务计算已耗时长；已完成/失败/取消任务用 start/end 区间
            if start_time and status in {"pending", "running"}:
                end_ref = end_time or now_utc()
                elapsed_time = (end_ref - start_time).total_seconds()
            elif start_time and end_time:
                elapsed_time = (end_time - start_time).total_seconds()

            message = _TASK_STATUS_MESSAGES.get(status, "任务进行中…")

            status_dict = {
                "task_id": task_id,
                "analysis_horizon": horizon_from_snapshot(task_result),
                "status": status,
                "progress": progress,
                "message": message,
                "current_step": status,
                "start_time": start_time,
                "end_time": end_time,
                "elapsed_time": elapsed_time,
                "remaining_time": 0,
                "estimated_total_time": 0,
                "symbol": task_result.get("symbol") or task_result.get("stock_code"),
                "stock_code": task_result.get("symbol")
                or task_result.get("stock_code"),
                "stock_symbol": task_result.get("symbol")
                or task_result.get("stock_code"),
                "error_message": task_result.get("error_message")
                or task_result.get("last_error"),
                "source": "mongodb_tasks",
            }
            return status_dict, task_result

        # 3) 尝试通过 analysis_id 兜底查找 analysis_reports（同样带 user_id 过滤）
        report = await self._find_report_by_task_id(task_id, user_id)
        if report:
            start_time = report.get("created_at")
            end_time = report.get("updated_at")
            elapsed_time = 0
            if start_time and end_time:
                elapsed_time = (end_time - start_time).total_seconds()

            report_dict = {
                "task_id": task_id,
                "analysis_horizon": horizon_from_snapshot(report),
                "status": "completed",
                "progress": 100,
                "message": "分析完成（从历史记录恢复）",
                "current_step": "completed",
                "start_time": start_time,
                "end_time": end_time,
                "elapsed_time": elapsed_time,
                "remaining_time": 0,
                "estimated_total_time": elapsed_time,
                "stock_code": report.get("stock_symbol"),
                "stock_symbol": report.get("stock_symbol"),
                "source": "mongodb_reports",
            }
            return report_dict, {}

        return None, {}


    async def get_task_overview(
        self,
        task_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """获取任务概览档案（供 /tasks/{id}/overview 聚合端点）。

        权限语义与 get_task_with_status_fallback 一致（user_id=None 管理员直通），
        在其基础上补全 analysis_tasks 档案字段（parameters/market_type/时间戳）。
        parameters 已随 $setOnInsert 落库；旧数据（落库改造前）无该字段，
        故缺失时仍从内存任务兜底。

        Returns:
            任务概览 dict；不存在或无权访问时返回 None。
        """
        status_info, record = await self._get_status_with_record(task_id, user_id)
        if not status_info:
            return None

        # mongodb_tasks 命中时直接复用 fallback 查得的原始记录，不重复查询；
        # 仅内存命中时补查一次档案字段（reports 兜底命中时 analysis_tasks 必无记录，跳过空查）
        if not record and status_info.get("source") != "mongodb_reports":
            try:
                db = get_mongo_db()
                filter_doc: Dict[str, Any] = {"task_id": task_id}
                if user_id is not None:
                    # 老数据兼容：与 get_task_with_status_fallback 保持一致
                    filter_doc["$or"] = [
                        {"user_id": user_id},
                        {"user_id": {"$exists": False}},
                        {"user_id": None},
                    ]
                record = await db.analysis_tasks.find_one(filter_doc) or {}
            except Exception as e:
                logger.warning(f"⚠️ get_task_overview 查询 analysis_tasks 失败: {e}")
                record = {}

        parameters = record.get("parameters")
        if not isinstance(parameters, dict):
            mem_task = await self.memory_manager.get_task_dict(task_id)
            parameters = (mem_task or {}).get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}
        analysis_horizon = horizon_from_snapshot(record or status_info)
        parameters = {**parameters, "analysis_horizon": analysis_horizon}
        market_type = parameters.get("market_type") or record.get("market_type") or "A股"

        return {
            "task_id": task_id,
            "status": record.get("status") or status_info.get("status"),
            "symbol": record.get("symbol")
            or record.get("stock_code")
            or status_info.get("symbol")
            or status_info.get("stock_symbol"),
            "market_type": market_type,
            "analysis_horizon": analysis_horizon,
            "parameters": parameters,
            "created_at": record.get("created_at"),
            "started_at": record.get("started_at") or status_info.get("start_time"),
            "completed_at": record.get("completed_at") or status_info.get("end_time"),
            "error_message": record.get("error_message")
            or record.get("last_error")
            or status_info.get("error_message"),
        }


    async def get_task_result_data(
        self,
        task_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        获取任务的完整分析结果数据。

        查询顺序：analysis_reports（按 task_id / analysis_id）→ analysis_tasks.result。
        不走内存：完成态内存仅存摘要（无 reports 全量），所有权校验由
        DB 查询的 user_id 过滤承担。

        Args:
            task_id: 任务 ID
            user_id: 可选用户 ID，非 None 时仅返回该用户拥有的任务结果（admin 传 None）。

        Returns:
            结果字典（含 "source" 标记），或 None。
        """
        # 1) 从 analysis_reports 获取
        mongo_result = await self._find_report_by_task_id(task_id, user_id)
        if mongo_result:
            decision = normalize_decision(
                mongo_result.get("decision"),
                collect_decision_source(mongo_result),
            )
            result_data = {
                "analysis_id": mongo_result.get("analysis_id"),
                "task_id": task_id,
                "analysis_horizon": horizon_from_snapshot(mongo_result),
                "report_archive_version": mongo_result.get("report_archive_version"),
                "stock_symbol": mongo_result.get("stock_symbol"),
                "stock_code": mongo_result.get("stock_symbol"),
                "analysis_date": mongo_result.get("analysis_date"),
                "summary": mongo_result.get("summary", ""),
                "recommendation": format_decision_recommendation(decision),
                "confidence_score": _extract_result_risk_and_confidence(decision, mongo_result.get("structured_summary"))[0],
                "risk_level": _extract_result_risk_and_confidence(decision, mongo_result.get("structured_summary"))[1],
                "key_points": merge_decision_key_points(
                    mongo_result.get("key_points", []), decision
                ),
                "execution_time": mongo_result.get("execution_time", 0),
                "tokens_used": mongo_result.get("tokens_used", 0),
                "analysts": mongo_result.get("analysts", []),
                "reports": mongo_result.get("reports", {}),
                "created_at": mongo_result.get("created_at"),
                "updated_at": mongo_result.get("updated_at"),
                "status": mongo_result.get("status", "completed"),
                "decision": decision,
                "structured_summary": synchronize_summary(mongo_result.get("structured_summary"), decision),
                "source": "mongodb",
            }
            return result_data

        # 2) 从 analysis_tasks.result 兜底（同样带 user_id 过滤 + 老数据兼容）
        try:
            db = get_mongo_db()
            fallback_filter: Dict[str, Any] = {"task_id": task_id}
            if user_id is not None:
                fallback_filter["$or"] = [
                    {"user_id": user_id},
                    {"user_id": {"$exists": False}},
                    {"user_id": None},
                ]
            tasks_doc = await db.analysis_tasks.find_one(
                fallback_filter,
                {
                    "result": 1,
                    "analysis_horizon": 1,
                    "parameters.analysis_horizon": 1,
                    "symbol": 1,
                    "stock_code": 1,
                    "created_at": 1,
                    "completed_at": 1,
                },
            )
            if tasks_doc and tasks_doc.get("result"):
                r = tasks_doc["result"] or {}
                symbol = (
                    tasks_doc.get("symbol")
                    or tasks_doc.get("stock_code")
                    or r.get("stock_symbol")
                    or r.get("stock_code")
                )
                decision = normalize_decision(
                    r.get("decision"),
                    collect_decision_source(r),
                )
                return {
                    "analysis_id": r.get("analysis_id"),
                    "task_id": task_id,
                    "analysis_horizon": horizon_from_snapshot(tasks_doc),
                    "report_archive_version": r.get("report_archive_version"),
                    "stock_symbol": symbol,
                    "stock_code": symbol,
                    "analysis_date": r.get("analysis_date"),
                    "summary": r.get("summary", ""),
                    "recommendation": format_decision_recommendation(decision),
                    "confidence_score": _extract_result_risk_and_confidence(decision, r.get("structured_summary"))[0],
                    "risk_level": _extract_result_risk_and_confidence(decision, r.get("structured_summary"))[1],
                    "key_points": merge_decision_key_points(
                        r.get("key_points", []), decision
                    ),
                    "execution_time": r.get("execution_time", 0),
                    "tokens_used": r.get("tokens_used", 0),
                    "analysts": r.get("analysts", []),
                    "reports": r.get("reports", {}),
                    "state": r.get("state", {}),
                    "detailed_analysis": r.get("detailed_analysis", {}),
                    "created_at": tasks_doc.get("created_at"),
                    "updated_at": tasks_doc.get("completed_at"),
                    "status": r.get("status", "completed"),
                    "decision": decision,
                    "structured_summary": synchronize_summary(r.get("structured_summary"), decision),
                    "source": "analysis_tasks",
                }
        except Exception as e:
            logger.warning(f"⚠️ get_task_result_data 从 analysis_tasks.result 兜底失败: {e}")

        return None


    async def mark_task_failed(self, task_id: str, error_message: str = "用户手动标记为失败") -> bool:
        """
        将任务标记为失败（内存 + MongoDB 同步更新）。
        """
        try:
            # 更新内存状态
            await self.memory_manager.update_task_status(
                task_id=task_id,
                status=TaskStatus.FAILED,
                message="手动标记为失败",
                error_message=error_message,
            )

            # 更新 MongoDB
            db = get_mongo_db()
            result = await db.analysis_tasks.update_one(
                {"task_id": task_id},
                {
                    "$set": {
                        "status": "failed",
                        "last_error": error_message,
                        "completed_at": now_utc(),
                        "updated_at": now_utc(),
                    }
                },
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"❌ mark_task_failed 失败: {e}", exc_info=True)
            return False


    async def validate_task_ownership(self, task_id: str, user_id: str) -> bool:
        """
        验证任务是否属于指定用户。

        查询顺序：内存 → MongoDB（兼容 Redis 已过期的完成/失败任务）。
        """
        # 1) 内存中查找
        task = await self.memory_manager.get_task_dict(task_id)
        if task and task.get("user_id") == user_id:
            return True

        # 2) MongoDB 中查找
        try:
            db = get_mongo_db()
            task_doc = await db.analysis_tasks.find_one({"task_id": task_id}, {"user_id": 1})
            return task_doc is not None and task_doc.get("user_id") == user_id
        except Exception as e:
            logger.error(f"❌ validate_task_ownership 查询失败: {e}", exc_info=True)
            return False


    async def delete_task_by_id(self, task_id: str, user_id: Optional[str] = None) -> bool:
        """
        从内存和数据库中删除任务记录。

        支持通过 user_id 进行所有权验证。当 Redis 中找不到任务时，
        会回退到 MongoDB 进行验证（适用于已完成/失败等过期任务）。
        """
        try:
            # 构造过滤条件：将所有权校验合并到删除操作中，减少数据库往返
            filter_doc: Dict[str, Any] = {"task_id": task_id}
            if user_id is not None:
                filter_doc["user_id"] = user_id

            db = get_mongo_db()
            result = await db.analysis_tasks.delete_one(filter_doc)

            if result.deleted_count > 0:
                await self.memory_manager.remove_task(task_id)
                return True

            # 删除失败时，区分"非本人任务"和"任务不存在"
            if user_id is not None:
                existing = await db.analysis_tasks.find_one({"task_id": task_id}, {"user_id": 1})
                if existing and existing.get("user_id") != user_id:
                    logger.warning(f"⚠️ 用户 {user_id} 尝试删除非本人任务: {task_id}")
                    return False

            # 任务不在 MongoDB 中（可能仅在内存中），仍需清理内存状态
            await self.memory_manager.remove_task(task_id)
            return False
        except Exception as e:
            logger.error(f"❌ delete_task_by_id 失败: {e}", exc_info=True)
            return False


    async def get_analysis_stats(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        market_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        获取分析报告统计信息（总数、按日期、按市场分布）。
        """
        try:
            db = get_mongo_db()
            query: Dict[str, Any] = {}
            if start_date or end_date:
                date_query: Dict[str, Any] = {}
                if start_date:
                    date_query["$gte"] = start_date
                if end_date:
                    date_query["$lte"] = end_date
                query["created_at"] = date_query
            if market_type:
                query["market_type"] = market_type

            total = await db.analysis_reports.count_documents(query)
            completed = await db.analysis_reports.count_documents({**query, "status": "completed"})
            failed = await db.analysis_reports.count_documents({**query, "status": "failed"})

            # 按日期统计
            pipeline_date = [
                {"$match": query},
                {
                    "$group": {
                        "_id": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": "$created_at",
                            }
                        },
                        "count": {"$sum": 1},
                    }
                },
                {"$sort": {"_id": -1}},
                {"$limit": 30},
            ]
            by_date = []
            async for doc in db.analysis_reports.aggregate(pipeline_date):
                by_date.append({"date": doc["_id"], "count": doc["count"]})

            # 按市场统计
            pipeline_market = [
                {"$match": query},
                {"$group": {"_id": "$market_type", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
            ]
            by_market = []
            async for doc in db.analysis_reports.aggregate(pipeline_market):
                by_market.append({"market": doc["_id"] or "未知", "count": doc["count"]})

            return {
                "total_analyses": total,
                "successful_analyses": completed,
                "failed_analyses": failed,
                "avg_duration": 0,
                "total_tokens": 0,
                "total_cost": 0,
                "popular_stocks": [],
                "analysis_by_date": by_date,
                "analysis_by_market": by_market,
            }
        except Exception as e:
            logger.error(f"❌ get_analysis_stats 失败: {e}", exc_info=True)
            raise


    async def search_stock_basic_info(
        self,
        query: str,
        market: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        在 stock_basic_info 集合中搜索股票（支持名称/代码/符号模糊匹配）。
        """
        try:

            di = DataInterface.get_instance()
            docs = await di.search_basic_info("CN", query, fields=["symbol", "name"], limit=limit)

            results = []
            for doc in docs:
                if market and doc.get("market") != market:
                    continue
                results.append(
                    {
                        "symbol": doc.get("symbol", ""),
                        "name": doc.get("name", ""),
                        "market": doc.get("market", "A股"),
                        "type": "stock",
                    }
                )
            return results
        except Exception as e:
            logger.error(f"❌ search_stock_basic_info 失败: {e}", exc_info=True)
            raise


    async def get_stock_info_with_quote(
        self, symbol: str, market: str = "A股"
    ) -> Optional[Dict[str, Any]]:
        """
        获取股票基础信息 + 实时行情（用于分析前预览）。

        按数据源优先级查询 stock_basic_info，并补充 market_quotes。
        """
        try:
            from app.data.core.symbols import normalize_symbol
            from app.core.async_utils import await_on_main_loop
            market_key = {"A股": "CN", "港股": "HK", "美股": "US"}.get(market, market)
            code6 = normalize_symbol(symbol, market_key)
            di = DataInterface.get_instance()
            basic = await await_on_main_loop(di.read(market_key, "basic_info", symbol=code6))
            b = basic.get("data")
            if not b:
                return None
            q = await await_on_main_loop(di.read_latest(market_key, "market_quotes", code6))

            return {
                "symbol": b.get("symbol", code6),
                "name": b.get("name", ""),
                "market": b.get("market", market),
                "industry": b.get("industry", ""),
                "sector": b.get("sector", ""),
                "market_cap": b.get("total_mv"),
                "price": (q or {}).get("close") or b.get("close"),
                "change_percent": (q or {}).get("pct_chg"),
                "pe_ratio": b.get("pe"),
                "pb_ratio": b.get("pb"),
                "dividend_yield": b.get("dividend_yield"),
                "volume": (q or {}).get("volume"),
            }
        except Exception as e:
            logger.error(f"❌ get_stock_info_with_quote 失败: {e}", exc_info=True)
            raise
