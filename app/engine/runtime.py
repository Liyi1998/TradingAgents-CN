# TradingAgents/engine/runtime.py
# 分析运行时入口（原 graph/trading_graph.py 瘦身迁移，graph/ 目录退役）
# 手写编排（orchestrator/pipeline）— LangGraph/langchain 已完全移除

import asyncio
import json
import os
import time
from typing import Any, Dict

from app.engine.agents.utils.agent_utils import Toolkit
from app.engine.agents.utils.memory import FinancialSituationMemory
from app.engine.default_config import DEFAULT_CONFIG
from app.engine.decision_semantics import normalize_decision
from app.engine.orchestrator.pipeline import PipelineDeps, run_pipeline
from app.engine.orchestrator.state import side_history_labeled
from app.engine.orchestrator.workflow.inputs import resolve_field
from app.engine.orchestrator.workflow.plan import MEMORY_SLOT_FIELDS
import logging
from app.utils.runtime_paths import get_eval_results_dir

logger = logging.getLogger("agents")

# 终端契约缺省时的信号文本回退链（P4-b 参数化前的历史硬编码行为，逐项一致）
_DEFAULT_SIGNAL_FALLBACK = (
    "final_trade_decision",
    "investment_plan",
    "risk_debate_state.judge_decision",
    "trader_investment_plan",
)


def _extract_final_signal(final_state: Dict[str, Any], terminal: Dict[str, Any]) -> Any:
    """终端契约驱动的最终信号文本探取（P4-b：点路径链参数化）。

    探取顺序 = spec 声明的 terminal.signal_fallback（点路径，各级结构化
    字段缺失时按序下探，§4.8）；未声明链时 = decision_field 优先 + 内置
    默认链兜底；快照无终端契约（旧任务）时直接用内置默认链（与历史
    硬编码行为一致）。取首个非空值，全空返回 ""。
    """
    decision_field = (terminal or {}).get("decision_field") or ""
    paths = (terminal or {}).get("signal_fallback") or (
        (decision_field, *_DEFAULT_SIGNAL_FALLBACK) if decision_field else _DEFAULT_SIGNAL_FALLBACK
    )
    for path in paths:
        value = resolve_field(final_state, path)
        if value:
            return value
    return ""


def _classify_node(node_name: str) -> str:
    """将节点名称分类为类别标识（节点命名与旧图一致）"""
    if "Risky" in node_name or "Safe" in node_name or "Neutral" in node_name or "Risk Judge" in node_name:
        return "risk"
    if "Analyst" in node_name:
        return "analyst"
    if "Researcher" in node_name or "Research Manager" in node_name:
        return "research"
    if "Trader" in node_name:
        return "trader"
    return "other"


class AnalysisRuntime:
    """分析运行时：客户端解析、memory 装配、流水线执行与后处理（信号提取/反思）。"""

    def __init__(
        self,
        selected_analysts=None,
        debug=False,
        config: Dict[str, Any] = None,
    ):
        self.debug = debug
        from app.core.horizons import resolve_research_horizon
        self.config = dict(config or DEFAULT_CONFIG)
        self.config["analysis_horizon"] = resolve_research_horizon(self.config.get("analysis_horizon"), self.config.get("horizon"))
        if not selected_analysts:
            raise ValueError("selected_analysts 不能为空，请先配置阶段1分析师。")

        self.selected_analysts = list(selected_analysts)

        # 如果外部已注入 MCP 开关
        if self.config.get("mcp_tool_loader") and not self.config.get("enable_mcp", False):
            self.config["enable_mcp"] = True
            logger.info("🔧 [AnalysisRuntime] 检测到 MCP 配置，已自动启用 MCP 工具")

        # Create necessary directories
        cache_root = self.config.get("data_cache_dir") or str(get_eval_results_dir().parent / "cache" / "dataflows")
        os.makedirs(cache_root, exist_ok=True)

        self.toolkit = Toolkit(config=self.config)

        # Initialize memories（如果启用）
        memory_enabled = self.config.get("memory_enabled", True)
        if memory_enabled:
            from app.engine.analysis_horizon import horizon_memory_name
            horizon = self.config["analysis_horizon"]
            self.bull_memory = FinancialSituationMemory(horizon_memory_name("bull_memory", horizon), self.config)
            self.bear_memory = FinancialSituationMemory(horizon_memory_name("bear_memory", horizon), self.config)
            self.trader_memory = FinancialSituationMemory(horizon_memory_name("trader_memory", horizon), self.config)
            self.invest_judge_memory = FinancialSituationMemory(horizon_memory_name("invest_judge_memory", horizon), self.config)
            self.risk_manager_memory = FinancialSituationMemory(horizon_memory_name("risk_manager_memory", horizon), self.config)
        else:
            self.bull_memory = None
            self.bear_memory = None
            self.trader_memory = None
            self.invest_judge_memory = None
            self.risk_manager_memory = None

        # State tracking
        self.curr_state = None
        self.ticker = None
        self.log_states_dict = {}

    # ── 客户端解析 ────────────────────────────────────────────────────

    async def _resolve_clients(self) -> Dict[str, Any]:
        """数据库模型配置优先（providers.get_engine_clients），
        任务级 config 显式指定 provider/model/api_key 时覆盖。"""
        from app.llm.providers import get_engine_clients, resolve_task_override_bundle

        clients = await get_engine_clients()

        async def _override(role: str):
            model = self.config.get(f"{role}_llm")
            if not model or model == "default":
                return
            provider = self.config.get(f"{role}_provider") or self.config.get("llm_provider", "openai")
            api_key = self.config.get(f"{role}_api_key") or self.config.get("api_key")
            base_url = self.config.get(f"{role}_backend_url") or self.config.get("backend_url")
            # 经 providers 解析：从数据库同模型配置继承 max_tokens 等每模型参数，
            # 避免覆盖路径丢失 DB 参数回落 .env 默认 4096（推理模型截断空响应）
            bundle = await resolve_task_override_bundle(
                model, provider=provider, api_key=api_key or None, base_url=base_url or None
            )
            if bundle is None:
                return  # 无凭据时沿用数据库配置
            clients[role] = bundle
            logger.info(f"[LLM初始化] {role}: {getattr(bundle, 'protocol', 'unknown')}:{model} (任务配置覆盖)")

        await _override("analyst")
        await _override("debate")
        return clients

    async def _preflight_clients(self, clients: Dict[str, Any]) -> None:
        """在启动数十个节点前，对本次实际使用的模型做最小真实调用。

        过去网关离线时，各节点会把连接异常降级成占位文本，任务最终仍可能被
        标记为完成。预检失败必须在流水线开始前终止，并把可读原因交给任务状态。
        """
        if not self.config.get("llm_preflight_enabled", True):
            return

        from app.llm.core.types import Message, Role
        from app.llm.connection_check import (
            CHECK_MAX_TOKENS, check_failure, check_identity, client_check_timeout,
            require_check_text,
        )

        checked = set()
        checks = []
        # Validate both roles before scheduling any billable request.
        for role in ("analyst", "debate"):
            if clients.get(role) is None:
                raise RuntimeError(f"模型网关连接检查失败：未配置 {role} 模型客户端。")
        for role in ("analyst", "debate"):
            client = clients.get(role)

            primary = getattr(client, "primary", client)
            # 同端点、模型、凭据及请求参数只探测一次；不切换备用模型。
            identity = check_identity(client)
            if identity in checked:
                continue
            checked.add(identity)

            model = getattr(client, "model", None) or getattr(primary, "model", "未知模型")
            timeout = client_check_timeout(client, self.config.get("llm_preflight_timeout"))

            async def _check(check_role=role, check_client=client, check_model=model, check_budget=timeout):
                try:
                    response = await asyncio.wait_for(
                        check_client.chat(
                            [Message(role=Role.USER, content="只回复 OK")],
                            system="模型连通性检查",
                            tools=[],
                            max_tokens=CHECK_MAX_TOKENS,
                        ),
                        timeout=check_budget,
                    )
                    require_check_text(response.text())
                except Exception as exc:
                    failure = check_failure(exc, check_budget)
                    raise RuntimeError(
                        f"模型连接检查未通过（{check_role}: {check_model}）：{failure['message']}。"
                        "本次未进入分析流程，请处理后再重试。"
                    ) from exc

            checks.append(asyncio.create_task(_check()))

        # 两个角色通常使用不同模型；并行预检将额外等待时间限制为较慢的一次调用。
        if checks:
            try:
                await asyncio.gather(*checks)
            except Exception:
                for task in checks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*checks, return_exceptions=True)
                raise

    # ── 主入口 ────────────────────────────────────────────────────────

    async def propagate(
        self,
        company_name,
        trade_date,
        progress_callback=None,
        task_id=None,
        event_sink=None,
        user_id=None,
        progress_range=(0, 100),
    ):
        """Run the analysis pipeline for a company on a specific date.

        progress_callback 为旧兼容入口：pipeline 内部经 EventSink.on_progress
        单通道转发（进度机制已收敛，ProgressManager 已删除）。
        progress_range: 计数式进度映射区间 (lo, hi)，服务层用于给 pipeline
        阶段留出前后缀（前置准备/结果处理）。
        """
        logger.debug(
            f"🔍 [RUNTIME DEBUG] propagate: company='{company_name}', trade_date='{trade_date}', task_id='{task_id}'"
        )
        self.ticker = company_name
        self.config["user_id"] = str(user_id or "")

        total_start_time = time.time()
        self._current_task_id = task_id

        clients = await self._resolve_clients()
        if event_sink is not None:
            await event_sink.emit("status", text="正在检查模型网关连接...")
        await self._preflight_clients(clients)
        self._last_debate_client = clients["debate"]

        def _limit_fields(prefix: str, bundle) -> Dict[str, Any]:
            """从 bundle 提取模型级并发限额（_meta.limit_key + max_concurrency）"""
            meta = getattr(bundle, "_meta", None) or {}
            return {
                f"{prefix}_limit": getattr(bundle, "max_concurrency", None),
                f"{prefix}_limit_key": meta.get("limit_key"),
            }

        limit_fields = {**_limit_fields("analyst", clients["analyst"]), **_limit_fields("debate", clients["debate"])}
        deps = PipelineDeps(
            analyst_client=clients["analyst"],
            debate_client=clients["debate"],
            toolkit=self.toolkit,
            bull_memory=self.bull_memory,
            bear_memory=self.bear_memory,
            trader_memory=self.trader_memory,
            invest_judge_memory=self.invest_judge_memory,
            risk_manager_memory=self.risk_manager_memory,
            config=self.config,
            **limit_fields,
        )
        final_state = await run_pipeline(
            deps,
            company_name,
            trade_date,
            self.selected_analysts,
            task_id=task_id,
            progress_callback=progress_callback,
            event_sink=event_sink,
            user_id=user_id,
            progress_range=progress_range,
        )

        if final_state is None:
            logger.error("final_state 为 None，分析流程未产生任何输出", exc_info=False)
            final_state = {}

        total_elapsed = time.time() - total_start_time
        node_timings = final_state.get("node_timings") or {}
        self._print_timing_summary(node_timings, total_elapsed)

        performance_data = self._build_performance_data(node_timings, total_elapsed)
        final_state["performance_metrics"] = performance_data

        self.curr_state = final_state

        model_info = ""
        try:
            debate_client = getattr(self, "_last_debate_client", None)
            if debate_client is not None:
                model_info = f"{type(debate_client).__name__}:{getattr(debate_client, 'model', '')}"
        except Exception as e:
            logger.debug(f"获取模型信息失败: {e}")
            model_info = "Unknown"

        # 信号文本按终端契约点路径链探取（P4-b 参数化；terminal 段随执行计划
        # 快照冻结，自定义工作流声明自己的 decision_field/回退链）
        terminal = (final_state.get("_plan_snapshot") or {}).get("terminal") or {}
        final_signal = _extract_final_signal(final_state, terminal)
        # 信号提取优先级翻转（P3 §4.8）：submit_report 的结构化决策参数为一手
        # 来源（终端 > trader），文本关键词/LLM 解析仅在 fallback_text 提交时启用
        signal_fields = final_state.get("final_decision_signal") or final_state.get("trader_decision_signal")
        if signal_fields and signal_fields.get("action"):
            decision = normalize_decision({
                "action": signal_fields["action"],
                "target_price": signal_fields.get("target_price"),
                "confidence": signal_fields.get("confidence"),
                "risk_score": signal_fields.get("risk_score"),
                "risk_level": signal_fields.get("risk_level"),
                "confidence_label": signal_fields.get("confidence_label"),
                "reasoning": signal_fields.get("reasoning") or "基于结构化提交的投资决策",
                "submission_source": "structured",
                **(signal_fields.get("decision_semantics") or {}),
            }, final_signal, account_position=self.config.get("account_position", "UNKNOWN"))
            if event_sink is not None:
                await event_sink.emit("status", text="最终决策生成完毕")
        elif final_signal:
            # fallback：SignalProcessor 文本解析（signal 阶段含一次完整 LLM 调用，
            # 提前下发状态提示消除尾部静默空窗）
            if event_sink is not None:
                await event_sink.emit("status", text="正在生成最终交易信号...")
            decision = await self.process_signal(
                final_signal,
                company_name,
                account_position=self.config.get("account_position", "UNKNOWN"),
            )
            if event_sink is not None:
                await event_sink.emit("status", text="最终决策生成完毕")
        else:
            decision = normalize_decision({
                "action": "观望",
                "account_position": self.config.get("account_position", "UNKNOWN"),
                "research_view": "UNCERTAIN",
                "unheld_action": "WATCH",
                "held_action": "HOLD",
                "target_price": None,
                "confidence": None,
                "risk_score": None,
                "risk_level": "未知",
                "reasoning": "未开启深度决策阶段，未生成最终决策",
                "reason": "未开启深度决策阶段，未生成最终决策",
            }, account_position=self.config.get("account_position", "UNKNOWN"))
        decision["model_info"] = model_info
        decision["analysis_horizon"] = self.config["analysis_horizon"]

        # All presentation paths consume the terminal verdict, not a second
        # numeric interpretation by the summary model.
        from app.engine.decision_semantics import synchronize_summary
        if final_state.get("structured_summary"):
            final_state["structured_summary"] = synchronize_summary(
                final_state["structured_summary"], decision
            )
        final_state["authoritative_decision"] = decision

        # Jev is an optional text reviewer, never a replacement for the trading model.
        from app.llm.jev_review import attach_review

        context = final_state.get("analysis_context") or {}
        coverage = list(context.pop("coverage_snapshots", {}).values())
        context.pop("news_snapshots", None)
        fallback_cache = context.pop("mcp_fallback_snapshots", {})
        context["builtin_fallback_statuses"] = [{"tool_id": item.get("tool_id"), "status": item.get("status")}
                                               for item in fallback_cache.values()]
        final_state["data_coverage"] = coverage
        if isinstance(final_state.get("structured_summary"), dict):
            final_state["structured_summary"]["data_coverage"] = coverage
            if context.get("price_reference") is not None:
                final_state["structured_summary"]["price_reference"] = context["price_reference"]
        from app.engine.data_quality_report import render_data_quality
        final_state.setdefault("reports", {})["data_quality_report"] = render_data_quality(context, coverage)
        await attach_review(final_state, company_name, trade_date)
        self._log_state(trade_date, final_state)

        return final_state, decision

    def propagate_sync(self, company_name, trade_date, **kwargs):
        """同步入口（工作线程/CLI）：事件循环内运行完整流水线"""
        return asyncio.run(self.propagate(company_name, trade_date, **kwargs))

    # ── 性能统计 ─────────────────────────────────────────────────────

    def _build_performance_data(self, node_timings: Dict[str, float], total_elapsed: float) -> Dict[str, Any]:
        analyst_nodes, research_nodes, trader_nodes, risk_nodes, other_nodes = {}, {}, {}, {}, {}
        for node_name, elapsed in node_timings.items():
            cat = _classify_node(node_name)
            target = {
                "analyst": analyst_nodes,
                "research": research_nodes,
                "trader": trader_nodes,
                "risk": risk_nodes,
            }.get(cat, other_nodes)
            target[node_name] = elapsed

        slowest = max(node_timings.items(), key=lambda x: x[1]) if node_timings else (None, 0)
        fastest = min(node_timings.items(), key=lambda x: x[1]) if node_timings else (None, 0)
        avg = sum(node_timings.values()) / len(node_timings) if node_timings else 0

        def _cat(nodes: Dict[str, float]) -> Dict[str, Any]:
            return {
                "nodes": {k: round(v, 2) for k, v in nodes.items()},
                "total": round(sum(nodes.values()), 2),
                "percentage": round(sum(nodes.values()) / total_elapsed * 100, 1) if total_elapsed > 0 else 0,
            }

        return {
            "total_time": round(total_elapsed, 2),
            "total_time_minutes": round(total_elapsed / 60, 2),
            "node_count": len(node_timings),
            "average_node_time": round(avg, 2),
            "slowest_node": {"name": slowest[0], "time": round(slowest[1], 2)} if slowest[0] else None,
            "fastest_node": {"name": fastest[0], "time": round(fastest[1], 2)} if fastest[0] else None,
            "node_timings": {k: round(v, 2) for k, v in node_timings.items()},
            "category_timings": {
                "analyst_team": _cat(analyst_nodes),
                "research_team": _cat(research_nodes),
                "trader_team": _cat(trader_nodes),
                "risk_management_team": _cat(risk_nodes),
                "other": _cat(other_nodes),
            },
            "llm_config": {
                "provider": self.config.get("llm_provider", "unknown"),
                "debate_model": self.config.get("debate_llm", "unknown"),
                "analyst_model": self.config.get("analyst_llm", "unknown"),
            },
        }

    def _print_timing_summary(self, node_timings: Dict[str, float], total_elapsed: float):
        logger.info("=" * 80)
        logger.info("⏱️  分析性能统计报告")
        logger.info("=" * 80)
        for node_name, elapsed in sorted(node_timings.items(), key=lambda x: x[1], reverse=True):
            pct = elapsed / total_elapsed * 100 if total_elapsed > 0 else 0
            logger.info(f"  • {node_name:40s} {elapsed:8.2f}秒  ({pct:5.1f}%)")
        logger.info("=" * 80)
        logger.info(f"🎯 总执行时间: {total_elapsed:.2f}秒 ({total_elapsed / 60:.2f}分钟)")
        logger.info(f"📈 节点总数: {len(node_timings)}")

    def _log_state(self, trade_date, final_state):
        """Log the final state to a JSON file."""
        inv_state = final_state.get("investment_debate_state") or {}
        risk_state = final_state.get("risk_debate_state") or {}

        def _safe(d, key, default=""):
            return d.get(key, default) if isinstance(d, dict) else default

        all_reports = {key: final_state.get(key, "") for key in final_state.keys() if key.endswith("_report")}

        self.log_states_dict[str(trade_date)] = {
            "company_of_interest": final_state.get("company_of_interest", ""),
            "trade_date": final_state.get("trade_date", ""),
            "analysis_horizon": final_state.get("analysis_horizon", "short"),
            **all_reports,
            "investment_debate_state": {
                "bull_history": _safe(inv_state, "bull_history"),
                "bear_history": _safe(inv_state, "bear_history"),
                "history": _safe(inv_state, "history"),
                "current_response": _safe(inv_state, "current_response"),
                "judge_decision": _safe(inv_state, "judge_decision"),
            },
            "trader_investment_decision": final_state.get("trader_investment_plan", ""),
            "risk_debate_state": {
                "risky_history": _safe(risk_state, "risky_history"),
                "safe_history": _safe(risk_state, "safe_history"),
                "neutral_history": _safe(risk_state, "neutral_history"),
                "history": _safe(risk_state, "history"),
                "judge_decision": _safe(risk_state, "judge_decision"),
            },
            "investment_plan": final_state.get("investment_plan", ""),
            "final_trade_decision": final_state.get("final_trade_decision", ""),
        }

        base_dir = get_eval_results_dir()
        directory = base_dir / (self.ticker or "unknown") / "TradingAgentsStrategy_logs"
        directory.mkdir(parents=True, exist_ok=True)

        log_file = directory / "full_states_log.json"
        tmp_file = log_file.with_suffix(log_file.suffix + ".tmp")
        with tmp_file.open("w") as f:
            json.dump(self.log_states_dict, f, indent=4, ensure_ascii=False)
        os.replace(tmp_file, log_file)

    async def reflect_and_remember(self, returns_losses):
        """Reflect on decisions and update memory based on returns.

        反思输入按执行计划快照的记忆反思声明驱动（P4-b 声明式输入适配）：
        每条声明 = 一个绑定记忆槽的节点 + 它自己的历史产出（发言史/裁决/
        报告），不再读固定辩论键——自定义工作流（任意 N 方辩论组）与内置
        工作流同一路径。无声明（记忆全关 / 旧任务快照无该段）时不产生反思。
        """
        from app.engine.agents.postprocess.reflector import Reflector

        if not self.curr_state:
            return
        reflections = (self.curr_state.get("_plan_snapshot") or {}).get("memory_reflections") or []
        reflections = [r for r in reflections if isinstance(r, dict)]
        if not reflections:
            return
        if not hasattr(self, "_reflector"):
            # 反思需要 LLM 客户端：用数据库配置解析
            try:
                clients = await self._resolve_clients()
                reflector = Reflector(
                    clients["debate"],
                    task_id=self.curr_state.get("task_id") or "",
                    user_id=self.curr_state.get("user_id") or "",
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"⚠️ 反思客户端初始化失败: {e}")
                return
            self._reflector = reflector

        for entry in reflections:
            memory = getattr(self, MEMORY_SLOT_FIELDS.get(entry.get("slot", ""), ""), None)
            report = self._reflection_input(self.curr_state, entry)
            if not report or memory is None:
                continue
            wrote = await self._reflector.reflect_component(
                entry.get("component_key") or entry.get("slot", ""),
                report,
                self.curr_state,
                returns_losses,
                memory,
            )
            if wrote:
                logger.info(f"🧠 [反思] {entry.get('component_key')} 经验已写入 {entry.get('slot')} 记忆库")

    @staticmethod
    def _reflection_input(state: Dict[str, Any], entry: Dict[str, Any]) -> str:
        """单条反思声明 → 输入文本（与节点运行期产出形态对齐）。

        debater：辩论 state 内该侧累积发言史（argument 格式；内置侧与
        旧 reflector 读取的 *_side_history 派生结果逐字一致）；
        judge：辩论 state 的裁决结论；single：主报告键（黑板优先、顶层回退）。
        """
        kind = entry.get("kind")
        if kind == "debater":
            ds = state.get(entry.get("state_key") or "") or {}
            side_key = entry.get("side_key") or ""
            return side_history_labeled(ds, side_key, entry.get("label") or side_key)
        if kind == "judge":
            ds = state.get(entry.get("state_key") or "") or {}
            return ds.get("judge_decision") or ""
        if kind == "single":
            report_key = entry.get("report_key") or ""
            board = state.get("reports") if isinstance(state.get("reports"), dict) else {}
            return board.get(report_key) or state.get(report_key) or ""
        return ""

    def reflect_and_remember_sync(self, returns_losses):
        from app.core.async_utils import run_async

        run_async(self.reflect_and_remember(returns_losses))

    async def process_signal(
        self,
        full_signal,
        stock_symbol=None,
        account_position="UNKNOWN",
    ):
        """Process a signal to extract the core decision."""
        from app.engine.agents.postprocess.signal_processor import SignalProcessor

        if not hasattr(self, "_signal_processor"):
            try:
                clients = await self._resolve_clients()
                self._signal_processor = SignalProcessor(
                    clients["debate"],
                    task_id=(self.curr_state or {}).get("task_id") or "",
                    user_id=(self.curr_state or {}).get("user_id") or "",
                )
            except Exception as e:  # noqa: BLE001
                logger.error(f"❌ 信号处理客户端初始化失败: {e}", exc_info=True)
                return normalize_decision({
                    "action": "观望",
                    "account_position": account_position,
                    "research_view": "UNCERTAIN",
                    "unheld_action": "WATCH",
                    "held_action": "HOLD",
                    "target_price": None,
                    "confidence": None,
                    "risk_score": None,
                    "reasoning": "信号处理初始化失败",
                }, account_position=account_position)
        return await self._signal_processor.process_signal(
            full_signal,
            stock_symbol,
            account_position=account_position,
            analysis_horizon=self.config.get("analysis_horizon", "short"),
        )

    def process_signal_sync(
        self,
        full_signal,
        stock_symbol=None,
        account_position="UNKNOWN",
    ):
        from app.core.async_utils import run_async

        return run_async(
            self.process_signal(
                full_signal,
                stock_symbol,
                account_position=account_position,
            )
        )


# 旧名兼容：外部历史引用平滑迁移
TradingAgentsGraph = AnalysisRuntime
