"""orchestrator/pipeline 保序与状态形状等价测试（去 LangGraph 迁移验收）

- 节点命名与旧 LangGraph 图一致（进度映射依赖）
- state 初始字段与旧 AgentState 完全一致（analysis_service 5 层提取依赖）
- 辩论轮次上限与旧 ConditionalLogic.MAX_ROUNDS 一致
- 增量合并语义（reports 合并 / messages 追加 / 其余覆盖）
"""

import pytest

from app.engine.orchestrator.pipeline import (
    AnalysisQualityError,
    MAX_ROUNDS,
    PipelineDeps,
    _format_analyst_node,
    _is_invalid_report_content,
    _merge_state_update,
    _require_valid_debate_rounds,
    _require_valid_reports,
    _require_valid_summary,
)
from app.engine.orchestrator.state import create_initial_state


class TestNodeNaming:
    def test_internal_key_to_node_name(self):
        assert _format_analyst_node("market") == "Market Analyst"
        assert _format_analyst_node("news") == "News Analyst"
        assert _format_analyst_node("social_media") == "Social_Media Analyst"

    def test_max_rounds_matches_legacy(self):
        assert MAX_ROUNDS == 10


class TestInitialStateShape:
    @pytest.mark.parametrize("symbol", [None, "", "  ", "None", "null", "undefined"])
    def test_missing_symbol_is_rejected_before_agent_execution(self, symbol):
        with pytest.raises(ValueError, match="股票代码缺失"):
            create_initial_state(symbol, "2026-09-22")

    def test_core_fields(self):
        state = create_initial_state("000001", "2024-12-31")
        assert state["company_of_interest"] == "000001"
        assert state["trade_date"] == "2024-12-31"
        assert isinstance(state["messages"], list) and len(state["messages"]) == 1
        assert state["investment_debate_state"]["count"] == 0
        assert state["risk_debate_state"]["count"] == 0
        # 轮次经派生视图计算（count//2、count//3，与旧 current_round_index 语义一致）
        from app.engine.orchestrator.state import current_round_index
        assert current_round_index(state["investment_debate_state"], 2) == 0
        assert current_round_index(state["risk_debate_state"], 3) == 0
        assert state["risk_debate_state"]["latest_speaker"] == ""
        assert "trader_investment_plan" in state
        assert "investment_plan" in state
        assert "final_trade_decision" in state

    def test_dynamic_report_fields(self):
        state = create_initial_state("000001", "2024-12-31")
        # DynamicAnalystFactory 动态初始化 *_report 字段
        report_keys = [k for k in state if k.endswith("_report")]
        assert report_keys, "应至少存在一个 *_report 初始字段"


class TestMergeStateUpdate:
    def test_reports_merged_and_messages_appended(self):
        target = {"reports": {"a_report": "x"}, "messages": [1], "k": "old"}
        _merge_state_update(target, {"reports": {"b_report": "y"}, "messages": [2], "k": "new"})
        assert target["reports"] == {"a_report": "x", "b_report": "y"}
        assert target["messages"] == [1, 2]
        assert target["k"] == "new"

    def test_empty_update_noop(self):
        target = {"k": "v"}
        _merge_state_update(target, {})
        assert target == {"k": "v"}


class TestPipelineDeps:
    def test_defaults(self):
        deps = PipelineDeps(analyst_client=None, debate_client=None, toolkit=None)
        assert deps.bull_memory is None
        assert deps.config == {}


class TestReportQualityGate:
    def test_long_target_failure_template_is_invalid(self):
        report = (
            "# 财经新闻分析报告.md\n\n## 结论\n"
            "**数据获取失败：无法对目标股票进行有效分析。**\n"
            "用户提供的股票代码和公司名称均为 `None`，无法唯一识别目标证券。\n"
            + "## 数据缺失说明\n暂不能形成结论。\n" * 100
        )
        assert len(report) > 500
        assert _is_invalid_report_content(report)

    def test_failure_placeholder_is_invalid(self):
        assert _is_invalid_report_content("❌ 分析失败：Connection error.")
        assert _is_invalid_report_content("⚠️ 交易员未能生成有效投资计划（LLM 返回空响应）。")

    def test_long_report_with_local_data_gap_is_valid(self):
        report = (
            "# 完整分析报告\n\n"
            + "行情、估值、风险与关键价格区间均已完成核验。" * 45
            + "\n某个次要数据源数据获取失败，已明确披露，不影响其余章节。"
        )
        assert len(report) > 500
        assert not _is_invalid_report_content(report)

    def test_missing_selected_report_aborts_pipeline(self):
        with pytest.raises(AnalysisQualityError, match="market_report"):
            _require_valid_reports(
                {"financial_news_report": "# 财经新闻\n" + "有效内容" * 200},
                ["market_report", "financial_news_report"],
                "基础分析阶段",
            )

    def test_default_failure_summary_is_rejected(self):
        with pytest.raises(AnalysisQualityError, match="失败兜底"):
            _require_valid_summary({"analysis_summary": "系统错误：无法生成分析摘要"})

    def test_substantive_summary_is_accepted(self):
        _require_valid_summary({"analysis_summary": "已根据行情、基本面和风险因素完成综合判断。"})

    def test_one_failed_debate_round_cannot_be_hidden_by_long_valid_round(self):
        state = {
            "rounds": [
                {"bull": "完整看涨论证" * 200, "bear": "完整看跌论证" * 200},
                {"bull": "完整反驳" * 200, "bear": "⚠️ 看跌研究员本轮未能生成有效内容"},
            ]
        }
        with pytest.raises(AnalysisQualityError, match=r"bear\[2\]"):
            _require_valid_debate_rounds(
                state, ["bull", "bear"], expected_rounds=2, stage="多空辩论阶段"
            )

    def test_round_barrier_only_requires_rounds_already_run(self):
        state = {"rounds": [{"bull": "完整看涨论证", "bear": "完整看跌论证"}]}
        _require_valid_debate_rounds(
            state, ["bull", "bear"], expected_rounds=1, stage="多空辩论阶段"
        )

    def test_quota_cause_survives_round_merge_without_raw_gateway_body(self):
        from app.engine.orchestrator.pipeline import _merge_debate_updates
        from app.engine.orchestrator.state import append_round
        from app.llm.failures import describe_failure

        state = {"risk_debate_state": {"rounds": [], "count": 0}, "reports": {}}
        updates = []
        for side in ("risky", "safe", "neutral"):
            failure = describe_failure(
                "Error code: 429 usage_limit_reached private-upstream-body"
            )
            content = f"⚠️ 节点执行异常。原因：{failure['message']}"
            debate = {"rounds": [], "count": 0}
            append_round(debate, side, content, 3)
            updates.append({"risk_debate_state": debate})
        _merge_debate_updates(
            state, updates, state_key="risk_debate_state",
            side_keys=["risky", "safe", "neutral"],
        )
        with pytest.raises(AnalysisQualityError) as caught:
            _require_valid_debate_rounds(
                state["risk_debate_state"], ["risky", "safe", "neutral"],
                expected_rounds=1, stage="风险辩论阶段",
            )
        message = str(caught.value)
        assert "risky[1], safe[1], neutral[1]" in message
        assert message.count("quota_exhausted") == 1
        assert "不再执行后续轮次" in message
        assert "private-upstream-body" not in message

    @pytest.mark.parametrize("stage_key", ["investment_debate_state", "risk_debate_state"])
    def test_each_debate_loop_checks_quality_before_next_iteration(self, stage_key):
        import ast
        import inspect

        from app.engine.orchestrator.workflow.executor import run_compiled

        from app.engine.orchestrator.workflow.seeds import load_workflow_seeds
        stages = [stage for spec in load_workflow_seeds() for stage in spec["stages"]]
        assert any(stage.get("state_key") == stage_key for stage in stages)
        tree = ast.parse(inspect.getsource(run_compiled))
        loops = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.For)
            and isinstance(node.target, ast.Name) and node.target.id == "_round"
            and "stage.rounds" in ast.unparse(node.iter)
        ]
        assert len(loops) == 1
        # The check is outside the parallel/serial branch but inside the round loop.
        calls = [
            node.value for node in loops[0].body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_require_valid_debate_rounds"
        ]
        assert len(calls) == 1
        assert ast.unparse(calls[0].args[2]) == "_round + 1"


@pytest.mark.ai
class TestPipelineOrderingRealLLM:
    """真实 LLM 端到端保序验收（事件序列断言）

    无凭据时 skip。验证：分析师串行 → Bull/Bear 交替均等 → Trader 恒执行
    → Risky→Safe→Neutral 固定循环 → Summary 恒执行。
    """

    def test_event_order(self):
        pytest.importorskip("app.llm.providers")
        import os

        # ai 标记的测试：真实 LLM 端到端。无凭据（容器外跑/无 .env key）时
        # get_engine_clients 只会拿到占位客户端，全部节点 401 失败——
        # 这种环境直接 skip，不算回归。
        if not (os.getenv("TUSHARE_TOKEN") is not None or _has_engine_creds()):
            pytest.skip("无 LLM 凭据（ai 层测试需真实 API key）")
        from app.llm.providers import get_engine_clients

        import asyncio

        from app.engine.orchestrator.pipeline import run_pipeline

        async def _run():
            clients = await get_engine_clients()
            deps = PipelineDeps(
                analyst_client=clients["analyst"],
                debate_client=clients["debate"],
                toolkit=None,
                config={"phase2_enabled": True, "phase3_enabled": True,
                        "phase2_debate_rounds": 1, "phase3_debate_rounds": 1},
            )
            return await run_pipeline(
                deps, "000001", "2024-12-31",
                # 市场技术 + 短线资金 = required 槽（裁掉任一会被编译期拒绝）
                selected_analysts=["market", "short_term_capital"],
            )

        state = asyncio.run(_run())
        inv = state["investment_debate_state"]
        # 总发言 2*(rounds+1)=4，round_index=count//2
        assert inv["count"] == 4
        assert inv["current_round_index"] == 2
        risk = state["risk_debate_state"]
        # 总发言 3*(rounds+1)=6
        assert risk["count"] == 6
        assert risk["current_round_index"] == 2
        assert state["trader_investment_plan"]
        assert "node_timings" in state


def _has_engine_creds() -> bool:
    """宿主侧检查 DB 模型配置是否带可用 api_key（无 DB 连接时按无凭据处理）。"""
    try:
        from app.services.config.system_service import SystemService

        import asyncio

        async def _peek():
            cfgs = await SystemService.get_llm_configs()
            return [c for c in (cfgs or []) if c.get("api_key")]

        return bool(asyncio.run(_peek()))
    except Exception:
        return False
