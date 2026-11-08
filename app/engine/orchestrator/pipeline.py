"""
保序流水线（替代 LangGraph graph/setup.py 的编排）

硬约束（语义与旧图等价，禁止漂移）：
- Phase 1 分析师并行（Semaphore(analyst_concurrency)，模型级 max_concurrency 封顶）
- Phase 2 公平辩论：同轮 Bull/Bear 并行 + barrier（debate_parallel=False 回退串行交替），
  各发言 rounds+1 次，总发言上限 2*(rounds+1)；公平性 = 上下文注入只读完整历史轮
  （rounds[0:current_round_index]），与旧串行逐字节一致
- Trader 恒执行（phase2 关闭时直接从分析师进入）
- Phase 3 风险辩论：同轮三方并行 + barrier（回退固定 Risky→Safe→Neutral 串行），
  总发言上限 3*(rounds+1)
- Summary 恒执行
- phase2/phase3 开关三拓扑：P2+P3 / P2 / 仅 P3(P2 关→分析师直连 Trader)
"""

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import logging

from . import registry
from .state import create_initial_state, export_legacy_state

# 身份映射（节点名 ↔ slug/event_key/显示名/报告标题）的单一权威来源 = registry：
# 旧四张模块级映射表已收敛（等价证明见 tests/engine/test_registry.py），
# agent_key 保持英文稳定标识（历史事件兼容与用户消息门禁稳定）。

logger = logging.getLogger("orchestrator.pipeline")

# 辩论轮次安全上限（与旧 ConditionalLogic.MAX_ROUNDS 一致）
MAX_ROUNDS = 10


class AnalysisQualityError(RuntimeError):
    """流水线产物未达到可交付标准。

    节点为了兼容旧流程会把 LLM 异常转换成占位文本；如果不在编排层拦截，
    这些占位文本会继续流入后续节点，最终被误存为“分析完成”。
    """


_REPORT_FAILURE_MARKERS = (
    "分析失败",
    "执行异常",
    "未能生成有效",
    "未生成报告",
    "llm 返回空响应",
    "connection error",
    "系统错误：无法生成",
    "数据获取失败，无法生成报告",
    "json解析失败，无法生成分析摘要",
)
_INVALID_REPORT_MAX_LEN = 500
_INVALID_SUMMARY_TEXTS = {
    "系统错误：无法生成分析摘要",
    "数据获取失败，无法生成报告",
    "JSON解析失败，无法生成分析摘要",
}


def _is_invalid_report_content(value: Any) -> bool:
    """确定性识别节点失败占位，不把长报告中的局部数据缺口误判为整份失败。"""
    if not isinstance(value, str) or not value.strip():
        return True
    text = value.strip()
    lowered = text.lower()
    if text.startswith(("❌", "⚠️")):
        return True
    # A fully templated failure can be thousands of characters long. Explicit
    # target-identification failures in the opening are fatal, not local data gaps.
    opening = lowered[:800].replace("`", "").replace("*", "")
    if any(marker in opening for marker in (
        "无法对目标股票进行有效分析",
        "无法唯一识别目标证券",
        "未提供有效股票代码",
        "股票代码和公司名称均为 none",
        "股票代码和公司名称均为none",
    )):
        return True
    return len(text) < _INVALID_REPORT_MAX_LEN and any(
        marker in lowered for marker in _REPORT_FAILURE_MARKERS
    )


def _require_valid_reports(
    reports: Any,
    keys: List[str],
    stage: str,
) -> None:
    """要求指定报告全部有效；失败时终止任务，禁止生成伪投资建议。"""
    report_map = reports if isinstance(reports, dict) else {}
    invalid = [key for key in keys if _is_invalid_report_content(report_map.get(key))]
    if invalid:
        from app.llm.failures import describe_failure

        details = "；".join(f"{key}：{describe_failure(report_map.get(key))['message']}" for key in invalid)
        saved = len(keys) - len(invalid)
        raise AnalysisQualityError(
            f"{stage}质量检查失败：以下报告缺失或生成失败：{', '.join(invalid)}。"
            f"原因：{details}。已有 {saved} 份报告正文保留。"
            "任务已终止，未生成投资建议；可在任务详情重试并复用已完成报告（仅同日）。"
        )


def _require_valid_text(value: Any, label: str, stage: str) -> None:
    if _is_invalid_report_content(value):
        from app.llm.failures import describe_failure

        raise AnalysisQualityError(
            f"{stage}质量检查失败：{label}缺失或生成失败。"
            f"原因：{describe_failure(value)['message']}。"
            "任务已终止，未生成投资建议；可在同日重试并复用有效分析师报告。"
        )


def _require_valid_debate_rounds(
    debate_state: Any,
    side_keys: List[str],
    expected_rounds: int,
    stage: str,
) -> None:
    """逐轮核验辩论产物，避免“一个有效长回复掩盖其他失败轮次”。"""
    rounds = (debate_state or {}).get("rounds") if isinstance(debate_state, dict) else []
    rounds = rounds if isinstance(rounds, list) else []
    invalid: List[str] = []
    reasons: List[str] = []
    for round_index in range(expected_rounds):
        round_data = rounds[round_index] if round_index < len(rounds) else {}
        round_data = round_data if isinstance(round_data, dict) else {}
        for side_key in side_keys:
            if _is_invalid_report_content(round_data.get(side_key)):
                from app.llm.failures import describe_failure

                invalid.append(f"{side_key}[{round_index + 1}]")
                reason = describe_failure(round_data.get(side_key))["message"]
                if reason not in reasons:
                    reasons.append(reason)
    if invalid:
        raise AnalysisQualityError(
            f"{stage}质量检查失败：以下辩论轮次缺失或生成失败：{', '.join(invalid)}。"
            f"原因：{'；'.join(reasons)}。"
            "任务已终止，不再执行后续轮次或生成投资建议；"
            "可在同日重试并复用有效分析师报告。"
        )


def _require_valid_summary(value: Any) -> None:
    if not isinstance(value, dict) or not value:
        raise AnalysisQualityError("总结阶段质量检查失败：未生成结构化摘要。")
    summary = str(value.get("analysis_summary") or "").strip()
    if not summary or summary in _INVALID_SUMMARY_TEXTS:
        raise AnalysisQualityError(
            "总结阶段质量检查失败：结构化摘要为失败兜底内容。"
            "任务已终止，不会保存为已完成报告。"
        )


@dataclass
class PipelineDeps:
    """pipeline 依赖（由 AnalysisRuntime 构造）"""

    analyst_client: Any  # BaseLLMClient
    debate_client: Any  # BaseLLMClient
    toolkit: Any
    bull_memory: Any = None
    bear_memory: Any = None
    trader_memory: Any = None
    invest_judge_memory: Any = None
    risk_manager_memory: Any = None
    config: Dict[str, Any] = field(default_factory=dict)
    # 模型级并发限额（providers bundle._meta 透传；None=不限）
    analyst_limit: Optional[int] = None
    analyst_limit_key: Optional[str] = None
    debate_limit: Optional[int] = None
    debate_limit_key: Optional[str] = None


def _format_analyst_node(internal_key: str) -> str:
    """internal_key → 节点名（与旧 setup.py 一致，如 'market' → 'Market Analyst'）"""
    return registry.format_analyst_node(internal_key)


def _merge_state_update(target: Dict[str, Any], update: Dict[str, Any]) -> None:
    """顺序合并节点增量（reports 字典合并、messages 追加、errors 追加，其余覆盖）"""
    if not update:
        return
    if "reports" in update and isinstance(update["reports"], dict):
        target["reports"] = {**(target.get("reports") or {}), **update["reports"]}
    if "messages" in update and isinstance(update["messages"], list):
        target.setdefault("messages", [])
        target["messages"].extend(update["messages"])
    for k, v in update.items():
        if k in ("reports", "messages"):
            continue
        target[k] = v


def _report_display_title(report_key: str, display_name: str = "") -> str:
    """report_key → 展示标题（委托 registry 单一权威表）。

    解析链：去掉 _report 后缀 → 别名索引/slug 化（bull_researcher → bull-researcher）
    → YAML 中文名（原 slug 与 +\"-analyst\" 后缀双候选——Stage 1 报告键不带后缀而
    YAML slug 带，如 social_media_report → social-media-analyst）→ display_name
    → report_key 原样。等价证明见 tests/engine/test_registry.py。
    """
    from .workflow.research import research_display_name
    period_name = research_display_name(report_key)
    return f"{period_name}报告" if period_name else registry.report_title(report_key, display_name)


async def _emit_report_ready(
    event_sink: Optional[Any],
    update: Dict[str, Any],
    *,
    agent_key: str,
    phase: str,
    before_reports: Dict[str, Any],
    display_name: str = "",
) -> None:
    """对比合并前后 reports 字典，为本次 update 中新增/内容变化的报告发射 report_ready。

    前端据此逐份即时展示报告，无需等整阶段结束。事件失败不阻断流水线。
    """
    if event_sink is None:
        return
    try:
        from app.llm.events import REPORT_CONTENT_MAX_CHARS

        reports_update = (update or {}).get("reports")
        if not isinstance(reports_update, dict) or not reports_update:
            return
        for report_key, content in reports_update.items():
            if before_reports.get(report_key) == content:
                continue  # 本次 update 未新增也未变化
            text = content if isinstance(content, str) else str(content)
            await event_sink.emit(
                "report_ready",
                agent_key=agent_key,
                phase=phase,
                report_key=report_key,
                title=_report_display_title(report_key, display_name),
                content=text[:REPORT_CONTENT_MAX_CHARS],
            )
    except Exception as e:  # noqa: BLE001 - 事件发射失败不阻断节点合并
        logger.warning(f"⚠️ [orchestrator] report_ready 发射失败: {e}")


def _merge_debate_updates(
    st: Dict[str, Any],
    updates: List[Dict[str, Any]],
    *,
    state_key: str,
    side_keys: List[str],
    has_latest_speaker: bool = False,
) -> List[str]:
    """辩论 barrier 合并（纯函数，就地修改 st）：并行各侧 update → 字段级写入 debate_state。

    - rounds[idx][side_key] 按侧写入（idx 用合并前快照 count//per_turn，避免读改写竞争）
    - count = 快照 + 成功侧数（失败侧缺席该轮，与串行失败语义一致）
    - 各 update 的非 debate_state 键（messages/reports/errors）按固定侧序合并（确定性）
    - Phase 3 latest_speaker = 固定顺序中最后一个有产出的侧（旧串行轮末等价）
    返回成功产出内容的 side_key 列表（固定顺序）。
    """
    from .state import current_round_index

    ds = st.setdefault(state_key, {})
    snapshot_count = int(ds.get("count", 0) or 0)
    snapshot_idx = current_round_index(ds, len(side_keys))
    succeeded: List[str] = []
    last_written: Optional[str] = None
    for side_key, update in zip(side_keys, updates):
        new_ds = (update or {}).get(state_key)
        content = None
        if isinstance(new_ds, dict):
            new_rounds = new_ds.get("rounds") or []
            if snapshot_idx < len(new_rounds):
                content = new_rounds[snapshot_idx].get(side_key)
        if content:
            rounds: List[Dict[str, Any]] = ds.setdefault("rounds", [])
            while snapshot_idx >= len(rounds):
                rounds.append({})
            rounds[snapshot_idx][side_key] = content
            succeeded.append(side_key)
            last_written = side_key
        _merge_state_update(st, {k: v for k, v in (update or {}).items() if k != state_key})
    ds["count"] = snapshot_count + len(succeeded)
    if has_latest_speaker and last_written is not None:
        ds["latest_speaker"] = last_written
    return succeeded


def _record_error(st: Dict[str, Any], node_name: str, error: str, start: float, *, retried: bool) -> None:
    """节点失败落 state["errors"]（不静默吞掉，供前端/回放展示）"""
    st.setdefault("errors", []).append(
        {
            "node": node_name,
            "phase": st.get("_phase", ""),
            "error": error,
            "ts": time.time(),
            "duration_ms": int((time.time() - start) * 1000),
            "retried": retried,
        }
    )


def interpolate_percent(completed: int, total: int, lo: int, hi: int) -> int:
    """completed/total 线性映射到 [lo, hi]；total<=0 返回 lo，完成时恰为 hi（尾差归末端）"""
    if total <= 0 or completed <= 0:
        return lo
    if completed >= total:
        return hi
    return lo + round(completed / total * (hi - lo))


async def run_pipeline(
    deps: PipelineDeps,
    company_name: str,
    trade_date: str,
    selected_analysts: List[str],
    *,
    task_id: Optional[str] = None,
    progress_callback: Optional[Callable[[Any], None]] = None,
    event_sink: Optional[Any] = None,
    user_id: Optional[str] = None,
    progress_range: tuple = (0, 100),
) -> Dict[str, Any]:
    """执行完整分析流水线，返回最终 state（字段形状与旧 final_state 一致）。

    编排（2026-09 工作流通用化）：加载工作流 spec（DB 权威）→ 旧 API 配置映射为
    CompileParams → compile 冻结执行计划（快照随 state 下发，在跑任务不受编辑影响）
    → executor 按 plan 遍历阶段。阶段拓扑不再写死在本函数。
    """
    config = dict(deps.config or {})
    from app.core.horizons import resolve_research_horizon
    config["analysis_horizon"] = resolve_research_horizon(config.get("analysis_horizon"), config.get("horizon"))

    # ── MCP 工具（任务级统一发现；新层 MCPManager，pipeline 结束统一关闭）──
    mcp_manager = None
    mcp_tools: List = []
    enable_mcp = bool(getattr(deps.toolkit, "enable_mcp", False))
    if enable_mcp:
        try:
            from app.llm.mcp.client import MCPManager
            from app.llm.mcp.tools import discover_mcp_tools

            mcp_manager = MCPManager()
            mcp_tools = await discover_mcp_tools(mcp_manager, task_manager=config.get("task_mcp_manager"))
            # 用户显式选择的 MCP 工具 id 过滤（analysis_service 传入）
            selected_ids = config.get("mcp_tool_ids")
            if selected_ids:
                selected_set = set(selected_ids)
                mcp_tools = [t for t in mcp_tools if t.name in selected_set or t.name.rsplit("__", 1)[-1] in selected_set]
                logger.info(f"🔧 [orchestrator] MCP 工具按选择过滤: {len(mcp_tools)}/{len(selected_set)} 个")
            logger.info(f"🔧 [orchestrator] MCP 工具发现: {len(mcp_tools)} 个")
        except Exception as e:  # noqa: BLE001 - MCP 不可用不阻断分析
            logger.warning(f"⚠️ [orchestrator] MCP 工具发现失败，跳过: {e}")
            # 失败也必须显式关闭已建立的 stdio 会话：泄漏的 anyio cancel scope
            # 被 GC 关闭时会以 CancelledError 反杀整个流水线（BaseException 穿透 except Exception）
            if mcp_manager is not None:
                try:
                    await mcp_manager.close_all()
                except Exception as close_err:  # noqa: BLE001
                    logger.warning(f"⚠️ [orchestrator] MCP 会话关闭失败: {close_err}")
            mcp_manager = None
            mcp_tools = []

    node_timings: Dict[str, float] = {}

    # 进度单通道：progress_callback 旧兼容入口统一挂到 EventSink.on_progress
    # （无 event_sink 时构造轻量 sink，仅转发进度，不落库不下发）
    if event_sink is None and progress_callback is not None:
        from app.llm.events import EventSink

        event_sink = EventSink(task_id=task_id or "", on_progress=progress_callback)

    state = create_initial_state(
        company_name, trade_date, task_id=task_id, user_id=user_id,
        account_position=config.get("account_position", "UNKNOWN"),
        analysis_horizon=config.get("analysis_horizon", "short"),
    )
    # 事件汇聚点经 state 下发（业务节点经 invoker 透传给 run_conversation，Stage 2-4 过程可观测）
    state["_event_sink"] = event_sink
    from app.data.core.interface import DataInterface
    from app.utils.stock_utils import StockUtils
    info = StockUtils.get_market_info(company_name)
    market = "US" if info.get("is_us") else "HK" if info.get("is_hk") else "CN"
    from app.data.core.analysis_context import ACTIVE_ANALYSIS_CONTEXT
    try:
        state["analysis_context"] = await DataInterface.get_instance().analysis_context(market, trade_date)
    except Exception as exc:
        logger.warning("统一交易日上下文不可用: %s", exc, exc_info=True)
        state["analysis_context"] = {"analysis_date": trade_date, "market": market, "calendar_status": "failed"}
    state["analysis_context"]["symbol"] = company_name
    state["analysis_context"]["analysis_horizon"] = state["analysis_horizon"]
    context_token = ACTIVE_ANALYSIS_CONTEXT.set(state["analysis_context"])
    from app.engine.data_preflight import ACTIVE_PREFLIGHT, PreflightSession
    preflight_session = PreflightSession()
    preflight_token = ACTIVE_PREFLIGHT.set(preflight_session)
    from .workflow.research import ACTIVE_RESEARCH_PROFILE, prepare_workflow
    research_token = ACTIVE_RESEARCH_PROFILE.set(None)

    try:
        try:
            # ── 工作流编译（延迟 import 避免 pipeline ↔ workflow.executor 模块级循环）──
            from .workflow.executor import run_compiled
            spec, spec_hash, plan = prepare_workflow(config, selected_analysts)
            ACTIVE_RESEARCH_PROFILE.set(spec.research_profile)
            # 执行计划快照：任务启动即冻结（spec_hash + params），编辑库中 spec/智能体
            # 不影响在跑任务；随 state 导出，由服务层持久化到 workflow_snapshot 字段
            state["_plan_snapshot"] = plan.snapshot()
            state["_plan_snapshot"]["analysis_horizon"] = state["analysis_horizon"]
            if spec.research_profile is not None:
                from .workflow.seeds import agent_spec_hash
                state["_plan_snapshot"]["research_profile"] = {
                    "revision": spec.research_profile.revision,
                    "hash": agent_spec_hash(spec.research_profile.model_dump(mode="json")),
                    "horizon": spec.research_profile.horizon,
                    "roles": {slug: role.name for slug, role in spec.research_profile.roles.items()},
                }
                if event_sink is not None:
                    await event_sink.emit("research_workflow_snapshot", snapshot=state["_plan_snapshot"])

            # One shared bounded quote read, independent of analyst selection or period.
            if market == "CN":
                try:
                    symbol = company_name.split(".")[0]
                    reference = await DataInterface.get_instance().price_reference(market, symbol, state["analysis_context"])
                    state["analysis_context"]["price_reference"] = reference
                    if event_sink is not None:
                        await event_sink.emit("price_reference_snapshot", snapshot=reference)
                except Exception as exc:
                    logger.warning("单股报价参考不可用，继续分析: %s", type(exc).__name__)
                    state["analysis_context"]["price_reference"] = {"status": "unavailable", "reason": type(exc).__name__}

            await run_compiled(
                plan,
                deps,
                state,
                event_sink=event_sink,
                mcp_tools=mcp_tools,
                node_timings=node_timings,
                progress_range=progress_range,
            )

            # reports 字典回填顶层 *_report 字段（支持自定义智能体，与旧逻辑一致）
            for report_key, report_content in (state.get("reports") or {}).items():
                if report_key.endswith("_report") and report_content and not state.get(report_key):
                    state[report_key] = report_content

            state.pop("_phase", None)
            state.pop("_event_sink", None)
            state.pop("_stage_inputs", None)
            state["node_timings"] = dict(node_timings)
            # 出口重建旧键形状（外部契约不变；内部 rounds 单一数据源）
            return export_legacy_state(state)
        except Exception as e:
            # partial_state 异常语义保留：携带已完成节点的部分结果
            state.pop("_phase", None)
            state.pop("_event_sink", None)
            state.pop("_stage_inputs", None)
            state["node_timings"] = dict(node_timings)
            e.partial_state = export_legacy_state(state)  # type: ignore[attr-defined]
            raise
    finally:
        ACTIVE_RESEARCH_PROFILE.reset(research_token)
        await preflight_session.close()
        ACTIVE_PREFLIGHT.reset(preflight_token)
        ACTIVE_ANALYSIS_CONTEXT.reset(context_token)
        if mcp_manager is not None:
            try:
                await mcp_manager.close_all()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"⚠️ [orchestrator] 关闭 MCP 连接失败: {e}")
