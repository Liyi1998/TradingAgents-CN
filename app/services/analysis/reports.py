"""
股票分析服务
整合了原 simple_analysis_service.py 和 analysis_service.py 的功能
"""
# data-access-exempt: 应用层集合（analysis_tasks/reports/users/notifications 等）直查属架构豁免；业务数据读取已收敛至 DataInterface

import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Any, Optional

from app.engine.decision_semantics import (
    collect_decision_source,
    format_decision_recommendation,
    merge_decision_key_points,
    normalize_decision,
    render_decision_markdown,
)
from app.utils.runtime_paths import get_analysis_results_dir

from app.core.database import get_mongo_db
from app.services.analysis_horizon import horizon_from_snapshot, require_matching_horizon, task_report_directory
from app.core.config import settings
from app.utils.timezone import now_utc, now_config_tz, format_date_short, format_iso

# 设置日志
logger = logging.getLogger("app.services.analysis_service")

# config_service imported from app.services.config_service (facade singleton)

from app.services.analysis.helpers import (
    _extract_result_risk_and_confidence,
)


class ReportsMixin:
    def _extract_reports_from_state(self, state: dict) -> dict:
        """从 LangGraph 状态中提取所有报告（5 层提取策略）。"""
        reports = {}
        if not isinstance(state, dict):
            return reports

        # 1. 动态发现所有 *_report 字段和已知非 _report 后缀的报告字段
        known_non_report_keys = [
            "trader_investment_plan",
            "investment_plan",
            "final_trade_decision",
        ]

        report_keys_found = [k for k in state.keys() if k.endswith("_report") or k in known_non_report_keys]
        logger.info(f"[报告提取] state中发现的报告键: {report_keys_found}")

        for key in state.keys():
            if key.endswith("_report") or key in known_non_report_keys:
                content = state[key]
                if content:
                    if isinstance(content, str):
                        reports[key] = content
                    elif hasattr(content, "content") and isinstance(content.content, str):
                        reports[key] = content.content
                    else:
                        try:
                            reports[key] = str(content)
                        except Exception as e:
                            logger.warning(f"[报告提取] 无法提取报告 {key}: 类型={type(content)}, 错误: {e}")

        logger.info(f"[报告提取] 根级报告: {list(reports.keys())}")

        # 2. 提取 investment_debate_state (多空博弈)
        if "investment_debate_state" in state and isinstance(state["investment_debate_state"], dict):
            inv_state = state["investment_debate_state"]
            for state_key, report_key in {
                "bull_history": "bull_researcher",
                "bear_history": "bear_researcher",
                "judge_decision": "research_team_decision",
            }.items():
                if state_key in inv_state and inv_state[state_key]:
                    reports[report_key] = inv_state[state_key]

        # 3. 提取 risk_debate_state (风险管理)
        if "risk_debate_state" in state and isinstance(state["risk_debate_state"], dict):
            risk_state = state["risk_debate_state"]
            for state_key, report_key in {
                "risky_history": "risky_analyst",
                "safe_history": "safe_analyst",
                "neutral_history": "neutral_analyst",
                "judge_decision": "risk_management_decision",
            }.items():
                if state_key in risk_state and risk_state[state_key]:
                    reports[report_key] = risk_state[state_key]

        # 4. 从 reports 字典中提取 (动态添加的智能体)
        if "reports" in state and isinstance(state["reports"], dict):
            dynamic_reports = state["reports"]
            logger.info(f"[报告提取] 从 reports 字典发现 {len(dynamic_reports)} 个: {list(dynamic_reports.keys())}")
            for key, content in dynamic_reports.items():
                if key not in reports and content:
                    reports[key] = content if isinstance(content, str) else str(content)

        # 5. 从 messages 列表中提取 (最终兜底)
        if "messages" in state and isinstance(state["messages"], list):
            from app.llm.core.types import Message, Role

            messages_reports_count = 0
            for msg in reversed(state["messages"]):
                # 新层 Message 无 name 字段；命名报告消息（若上游附加）作兜底提取
                msg_name = getattr(msg, "name", "")
                if (
                    isinstance(msg, Message)
                    and msg.role == Role.ASSISTANT
                    and msg_name
                    and msg_name.endswith("_report")
                ):
                    report_key = msg.name
                    if report_key not in reports:
                        content = msg.content
                        if content and isinstance(content, str):
                            reports[report_key] = content
                            messages_reports_count += 1
            if messages_reports_count > 0:
                logger.info(f"[报告提取] 从消息历史中恢复了 {messages_reports_count} 个报告")

        return reports


    async def _save_analysis_results_complete(
        self,
        task_id: str,
        result: Dict[str, Any],
        user_id: Optional[str] = None,
    ):
        """完整的分析结果保存"""
        result["task_id"] = task_id
        stock_symbol = result.get("stock_symbol") or result.get(
            "stock_code", "UNKNOWN"
        )
        # 本地 Markdown 归档是辅助输出，失败时其内部会记录警告；MongoDB 是
        # 前端报告的权威来源，写入失败必须向上抛出，任务不得标记为完成。
        await self._save_modular_reports_to_data_dir(result, stock_symbol)
        await self._save_analysis_result_web_style(task_id, result, user_id)


    async def _save_modular_reports_to_data_dir(self, result: Dict[str, Any], stock_symbol: str) -> Dict[str, str]:
        """保存分模块报告到data目录 - 完全采用web目录的文件结构"""
        try:
            # 使用统一的路径获取方式
            runtime_base = settings.RUNTIME_BASE_DIR
            results_dir = get_analysis_results_dir(runtime_base)

            analysis_date_raw = result.get("analysis_date", now_config_tz())

            # 确保 analysis_date 是字符串格式
            if isinstance(analysis_date_raw, datetime):
                analysis_date_str = analysis_date_raw.strftime("%Y-%m-%d")
            elif isinstance(analysis_date_raw, str):
                # 如果已经是字符串，检查格式
                try:
                    # 尝试解析日期字符串，确保格式正确
                    datetime.strptime(analysis_date_raw, "%Y-%m-%d")
                    analysis_date_str = analysis_date_raw
                except ValueError:
                    # 如果格式不正确，使用当前日期
                    analysis_date_str = format_date_short(now_config_tz())
            else:
                # 其他类型，使用当前日期
                analysis_date_str = format_date_short(now_config_tz())

            reports_dir = task_report_directory(
                results_dir, stock_symbol, analysis_date_str,
                horizon_from_snapshot(result), result["task_id"],
            )
            stock_dir = reports_dir.parent
            await asyncio.to_thread(reports_dir.mkdir, parents=True, exist_ok=True)

            # 创建message_tool.log文件 - 与web目录保持一致（线程池中执行避免阻塞事件循环）
            log_file = stock_dir / "message_tool.log"
            await asyncio.to_thread(log_file.touch, exist_ok=True)

            # 获取已提取的报告
            reports = result.get("reports", {})
            saved_files = {}

            # 🔥 报告标题映射（registry 单一权威表）：分析师段 + 非分析师固定报告键。
            # 非分析师标题从硬编码中文统一切 YAML 配置名（与 router 层 report_titles 一致）
            from app.engine.orchestrator.registry import (
                analyst_report_display_names,
                names_by_slug,
                slug_for_report_key,
            )

            known_report_titles = analyst_report_display_names()
            try:
                slug_names = names_by_slug()
                for fixed_key in (
                    "investment_plan",
                    "trader_investment_plan",
                    "bull_researcher",
                    "bear_researcher",
                    "research_team_decision",
                    "risky_analyst",
                    "safe_analyst",
                    "neutral_analyst",
                    "risk_management_decision",
                    "risk_manager_decision",
                ):
                    slug = slug_for_report_key(fixed_key)
                    name = slug_names.get(slug) if slug else None
                    if name:
                        known_report_titles[fixed_key] = f"{name}报告"
            except Exception as e:  # noqa: BLE001 - 标题解析失败走 key 兜底，不阻断落盘
                logger.warning(f"⚠️ 无法加载报告标题: {e}")

            # 🔥 动态保存所有报告（包括新添加的智能体报告）
            for report_key, report_content in reports.items():
                try:
                    if report_content:
                        # 生成文件名：使用 report_key 作为文件名
                        filename = f"{report_key}.md"
                        # 获取友好标题，如果没有则使用 key 的格式化版本
                        title = known_report_titles.get(report_key, report_key.replace("_", " ").title() + "报告")

                        file_path = reports_dir / filename
                        await asyncio.to_thread(file_path.write_text, report_content, encoding="utf-8")

                        saved_files[report_key] = str(file_path)
                        logger.info(f"✅ 保存模块报告: {file_path} ({title})")
                except Exception as e:
                    logger.warning(f"⚠️ 保存模块 {report_key} 失败: {e}")

            # 保存最终决策报告 - 完全按照web目录的方式
            decision = result.get("decision", {})
            if decision:
                decision_content = render_decision_markdown(stock_symbol, decision)

                decision_file = reports_dir / "final_trade_decision.md"
                await asyncio.to_thread(decision_file.write_text, decision_content, encoding="utf-8")
                saved_files["final_trade_decision"] = str(decision_file)

            # 保存分析元数据文件 - 完全按照web目录的方式
            metadata = {
                "stock_symbol": stock_symbol,
                "analysis_date": analysis_date_str,
                "task_id": result["task_id"],
                "analysis_horizon": horizon_from_snapshot(result),
                "timestamp": format_iso(now_config_tz()),
                "analysts": result.get("analysts", []),
                "status": "completed",
                "reports_count": len(saved_files),
                "report_types": list(saved_files.keys()),
            }

            metadata_file = reports_dir.parent / "analysis_metadata.json"
            await asyncio.to_thread(
                metadata_file.write_text,
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            return saved_files
        except Exception as e:
            logger.error(f"❌ 保存分模块报告失败: {e}", exc_info=True)
            return {}


    async def _save_analysis_result_web_style(
        self,
        task_id: str,
        result: Dict[str, Any],
        user_id: Optional[str] = None,
    ):
        """保存分析结果 (Web Style)"""
        try:
            db = get_mongo_db()
            stock_symbol = result.get("stock_symbol") or result.get("stock_code", "UNKNOWN")
            timestamp = now_utc()
            task_snapshot = await db.analysis_tasks.find_one(
                {"task_id": task_id}, {"analysis_horizon": 1, "parameters.analysis_horizon": 1}
            )
            analysis_horizon = horizon_from_snapshot(result)
            if task_snapshot:
                analysis_horizon = require_matching_horizon(task_snapshot, result)
            analysis_id = result.get("analysis_id") or (
                f"{stock_symbol}_{timestamp.strftime('%Y%m%d_%H%M%S')}_{analysis_horizon}_{task_id}"
            )
            result["analysis_id"] = analysis_id
            result["analysis_horizon"] = analysis_horizon

            # 处理 reports，确保为字符串内容，避免空值
            raw_reports = result.get("reports") or {}
            cleaned_reports: Dict[str, str] = {}
            if isinstance(raw_reports, dict):
                for key, value in raw_reports.items():
                    if value is None:
                        continue
                    if isinstance(value, str):
                        content = value.strip()
                    else:
                        # 对非字符串内容进行 JSON 序列化，保持可读
                        content = json.dumps(value, ensure_ascii=False, indent=2)
                    if content:
                        cleaned_reports[key] = content

            from app.services.report_utils import dedupe_report_aliases

            cleaned_reports, report_aliases = dedupe_report_aliases(cleaned_reports)

            decision = normalize_decision(
                result.get("decision"),
                collect_decision_source(
                    {
                        "decision": result.get("decision"),
                        "recommendation": result.get("recommendation"),
                        "reports": cleaned_reports,
                        "structured_summary": result.get("structured_summary"),
                    }
                ),
            )

            # 关键字段兜底
            analysis_date = result.get("analysis_date") or timestamp.strftime("%Y-%m-%d")
            summary = result.get("summary", "")
            recommendation = format_decision_recommendation(decision)
            confidence_score, risk_level = _extract_result_risk_and_confidence(decision, result.get("structured_summary"))
            key_points = merge_decision_key_points(
                result.get("key_points") or [], decision
            )
            analysts = result.get("analysts") or result.get("selected_analysts") or []
            model_info = result.get("model_info") or result.get("llm_model") or "Unknown"
            tokens_used = result.get("tokens_used") or result.get("token_usage", {}).get("total_tokens", 0)
            # token 用量回填：从 token_usage 集合按任务聚合（per-call 记录的权威汇总）
            token_usage_detail = {}
            try:
                usage_rows = await db.token_usage.aggregate(
                    [
                        {"$match": {"task_id": task_id}},
                        {
                            "$group": {
                                "_id": None,
                                "input_tokens": {"$sum": {"$ifNull": ["$input_tokens", 0]}},
                                "output_tokens": {"$sum": {"$ifNull": ["$output_tokens", 0]}},
                                "cache_read_tokens": {"$sum": {"$ifNull": ["$cache_read_input_tokens", 0]}},
                                "cache_creation_tokens": {"$sum": {"$ifNull": ["$cache_creation_input_tokens", 0]}},
                                "cost": {"$sum": {"$ifNull": ["$cost", 0.0]}},
                            }
                        },
                    ]
                ).to_list(length=1)
                if usage_rows:
                    row = usage_rows[0]
                    token_usage_detail = {
                        "input_tokens": row.get("input_tokens", 0),
                        "output_tokens": row.get("output_tokens", 0),
                        "cache_read_tokens": row.get("cache_read_tokens", 0),
                        "cache_creation_tokens": row.get("cache_creation_tokens", 0),
                        "cost": row.get("cost", 0.0),
                    }
                    tokens_used = token_usage_detail["input_tokens"] + token_usage_detail["output_tokens"]
            except Exception as usage_err:  # noqa: BLE001 - 统计回填失败不阻断保存
                logger.warning(f"⚠️ token 用量回填失败 task={task_id}: {usage_err}")
            execution_time = result.get("execution_time", 0)
            structured_summary = result.get("structured_summary") or {}
            market_type = result.get("market_type") or result.get("parameters", {}).get("market_type") or "A股"

            # 执行计划快照提取：任务文档存 workflow_snapshot（冻结的 spec 版本 + 编译参数，
            # 供回放/审计「这个任务当时跑的是什么拓扑」）；同时从对外 state 剥离（内部执行元数据）
            workflow_snapshot = None
            state_obj = result.get("state")
            if isinstance(state_obj, dict):
                snap = state_obj.pop("_plan_snapshot", None)
                if isinstance(snap, dict) and snap:
                    workflow_snapshot = snap

            # 报告列表按 user_id 隔离。早期仅把 user_id 写入任务，报告文档
            # 没有归属字段，导致已完成报告被列表接口全部过滤掉。
            owner_id = str(user_id or result.get("user_id") or "").strip()
            if not owner_id:
                task_owner = await db.analysis_tasks.find_one(
                    {"task_id": task_id}, {"user_id": 1}
                )
                if task_owner and task_owner.get("user_id") is not None:
                    owner_id = str(task_owner["user_id"])

            document = {
                "analysis_id": analysis_id,
                "analysis_horizon": analysis_horizon,
                "horizon": result.get("horizon"),
                "evaluation_horizon_days": result.get("evaluation_horizon_days"),
                "report_archive_version": result.get("report_archive_version", 2),
                "stock_symbol": stock_symbol,
                "stock_name": result.get("stock_name")
                or await self._resolve_stock_name(stock_symbol),
                "analysis_date": analysis_date,
                "market_type": market_type,
                "status": result.get("status", "completed"),
                "decision": decision,
                "structured_summary": structured_summary,  # 🔥 显式保存结构化总结到DB
                "task_id": task_id,
                "user_id": owner_id or None,
                "created_at": timestamp,
                "updated_at": timestamp,
                "summary": summary,
                "recommendation": recommendation,
                "reports": cleaned_reports,
                "report_aliases": report_aliases,
                "confidence_score": confidence_score,
                "risk_level": risk_level,
                "key_points": key_points,
                "analysts": analysts,
                "model_info": model_info,
                "tokens_used": tokens_used,
                "token_usage_detail": token_usage_detail,
                "execution_time": execution_time,
                "source": result.get("source", "analysis_service"),
            }
            if workflow_snapshot is not None:
                document["workflow_snapshot"] = workflow_snapshot

            # Re-derive from the exact canonical persisted source; never trust a
            # stale upstream partition or rewrite the original report/decision.
            from app.engine.strategy_partition import partition_strategy
            strategy_source = cleaned_reports.get("final_trade_decision") or cleaned_reports.get("trader_investment_plan") or ""
            document["strategy_partition"] = partition_strategy(strategy_source)

            # 写入报告集合
            insert_result = await db.analysis_reports.insert_one(document)

            # 任务集合只存瘦身引用拷贝（排除 reports 等大对象，全量以
            # analysis_reports 为准）：result 保留 analysis_id 等索引键，供
            # _find_report_by_task_id / reports_service 反查关联报告；
            # 携带 report_id 便于前端关联
            document_for_task = {
                k: v for k, v in document.items() if k not in ("reports", "state")
            }
            document_for_task["_id"] = insert_result.inserted_id
            await db.analysis_tasks.update_one({"task_id": task_id}, {"$set": {"result": document_for_task}})
            # Shadow-only observer runs after persistence; never await a model here.
            try:
                from app.services.jev_final_action_service import schedule_jev_after_report
                schedule_jev_after_report(db, insert_result.inserted_id)
            except Exception:
                logger.warning("条件核验旁路未能排队，原分析结果不受影响")
        except Exception as e:
            logger.error(f"❌ 保存DB结果失败: {e}", exc_info=True)
            raise

    # -------------------------------------------------------------------------
    # Router-facing Methods (added for analysis.py DB abstraction)
    # -------------------------------------------------------------------------


    async def _find_report_by_task_id(
        self,
        task_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        从 analysis_reports 中根据 task_id 查找报告。
        兼容旧数据：如果直接查不到，通过 analysis_tasks.result.analysis_id 兜底。

        Args:
            task_id: 任务 ID
            user_id: 可选用户 ID，用于所有权过滤。非 None 时仅返回该用户的报告。
        """
        try:
            db = get_mongo_db()
            filter_doc: Dict[str, Any] = {"task_id": task_id}
            if user_id is not None:
                # 老数据兼容：早期记录可能不含 user_id 字段
                filter_doc["$or"] = [
                    {"user_id": user_id},
                    {"user_id": {"$exists": False}},
                    {"user_id": None},
                ]
            report = await db.analysis_reports.find_one(filter_doc)
            if report:
                return report

            # 兼容旧数据：analysis_reports 旧记录可能不含 user_id，回退到 analysis_tasks 查 analysis_id
            tasks_filter: Dict[str, Any] = {"task_id": task_id}
            if user_id is not None:
                tasks_filter["$or"] = [
                    {"user_id": user_id},
                    {"user_id": {"$exists": False}},
                    {"user_id": None},
                ]
            tasks_doc = await db.analysis_tasks.find_one(tasks_filter, {
                "result.analysis_id": 1, "analysis_horizon": 1, "parameters.analysis_horizon": 1,
            })
            if tasks_doc:
                analysis_id = tasks_doc.get("result", {}).get("analysis_id")
                if analysis_id:
                    report = await db.analysis_reports.find_one({"analysis_id": analysis_id})
                    if report:
                        require_matching_horizon(tasks_doc, report)
                    return report
        except Exception as e:
            logger.warning(f"⚠️ _find_report_by_task_id 失败: {e}")
        return None
