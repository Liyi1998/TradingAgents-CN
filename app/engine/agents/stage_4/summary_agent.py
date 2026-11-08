import json
import logging
import re
from copy import deepcopy
from app.engine.orchestrator.invoker import run_node_turn
from app.engine.orchestrator.workflow.research import role_prompt

logger = logging.getLogger(__name__)


# System prompt 常量收敛至 prompts/parts.py（唯一来源，注入安全约束见该文件注释）
from app.engine.prompts.parts import SUMMARY_SYSTEM_PROMPT as SYSTEM_PROMPT  # noqa: E402


# 结构完整性兜底默认值
_DEFAULT_STRUCTURED_DATA = {
    "key_indicators": {
        "entry_price": "N/A",
        "target_price": "N/A",
        "stop_loss": "N/A",
        "support_level": "N/A",
        "resistance_level": "N/A",
    },
    "model_confidence": None,
    "risk_assessment": {"level": "Unknown", "score": None, "description": "生成失败，未量化风险"},
    "analysis_summary": "系统错误：无法生成分析摘要",
    "investment_recommendation": "暂无建议",
    "decision_semantics": {
        "account_position": "UNKNOWN",
        "research_view": "UNCERTAIN",
        "unheld_action": "WATCH",
        "held_action": "HOLD",
        "position_limit_pct": None,
        "exit_conditions": [],
    },
    "analysis_reference": [],
    "final_signal": "WATCH",
}

# 输入体检：占位/失败报告的确定性识别（不依赖 LLM 判断）
_PLACEHOLDER_PREFIX = "⚠️"
_FAILURE_KEYWORDS = ("数据获取失败", "获取数据失败", "工具调用失败")
# 短于该长度且含失败关键字 → 整体失败说明；长报告仅个别小节失败不算无效
_INVALID_REPORT_MAX_LEN = 200
_MISSING_PLACEHOLDER = "（该项数据缺失）"


def _is_invalid_report(text: str) -> bool:
    """判断报告是否为占位/整体失败说明。

    - `⚠️` 开头：上游节点降级时写入的占位文本（如"分析师未生成有效报告（LLM 返回空响应）"）
    - 含失败关键字且长度低于阈值：整体即为失败说明，而非正常报告中个别小节失败
    """
    if not text or not isinstance(text, str):
        return True
    if text.startswith(_PLACEHOLDER_PREFIX):
        return True
    if len(text) < _INVALID_REPORT_MAX_LEN and any(k in text for k in _FAILURE_KEYWORDS):
        return True
    return False


def _ensure_required_fields(data: dict) -> dict:
    """补齐 LLM 返回 JSON 中缺失的必需字段，已有字段保留 LLM 值。"""
    for key, default_val in _DEFAULT_STRUCTURED_DATA.items():
        if key not in data:
            data[key] = deepcopy(default_val)
    return data


def _truncate(text: str, limit: int) -> str:
    """原样返回完整文本（不截断）。

    历史上此处按 limit 截断（报告 500/决策 1500 字符），会丢弃报告后半段的
    关键结论与数据，导致结构化总结基于残缺输入生成。limit 参数保留仅为
    兼容调用签名，不再生效。
    """
    if not text or not isinstance(text, str):
        return ""
    return text


def _has_substantive_report(value: object) -> bool:
    """判断报告是否包含足够正文，避免把一句工具错误当成已覆盖数据。"""
    return isinstance(value, str) and len(value.strip()) >= 200


def _repair_false_coverage_claims(data: dict, coverage: dict) -> dict:
    """Report existence never proves field completeness; preserve limitations."""
    data["report_presence"] = dict(coverage)
    return data


def _build_user_message(
    company_name: str,
    account_position: str,
    trader_plan: str,
    final_decision: str,
    market_report: str,
    news_report: str,
    fundamentals_report: str,
    sentiment_report: str,
    risk_debate_history: str,
    other_reports: dict,
    missing_labels: list[str] | None = None,
) -> str:
    """构建用户消息：可控内容用 XML 边界符包裹，便于 LLM 区分指令与数据。

    占位/失败的输入已在调用前替换为 _MISSING_PLACEHOLDER，missing_labels
    记录缺失项名称，作为元信息随消息传递（避免 LLM 自行判定输入有效性）。
    """
    parts = [
        f"请为以下公司生成结构化总结数据：{company_name}\n",
        f"<account_position>{account_position}</account_position>\n"
        "该账户状态由系统提供，必须原样使用；UNKNOWN 时不得猜测。",
    ]
    if missing_labels:
        parts.append(
            f"注：以下输入数据缺失：{'、'.join(missing_labels)}。"
            '请基于现有有效数据正常生成总结，缺失项对应字段填 "N/A"，'
            "并在 risk_assessment.description 中简要注明数据缺失。"
        )
    coverage = {
        "market": _has_substantive_report(market_report),
        "news": _has_substantive_report(news_report),
        "fundamentals": _has_substantive_report(fundamentals_report),
        "sentiment": _has_substantive_report(sentiment_report),
    }
    parts.append(
        "<data_coverage>"
        + json.dumps(coverage, ensure_ascii=False)
        + "</data_coverage>\n"
        "此清单仅表示报告正文存在，不代表字段齐全。必须保留字段缺失、时效及来源局限。"
    )
    parts.append(f"<trader_plan>{_truncate(trader_plan, 1500)}</trader_plan>")
    parts.append(f"<final_decision>{_truncate(final_decision, 1500)}</final_decision>")
    parts.append(f"<market_report>{_truncate(market_report, 500)}</market_report>")
    parts.append(f"<news_report>{_truncate(news_report, 500)}</news_report>")
    parts.append(f"<fundamentals_report>{_truncate(fundamentals_report, 500)}</fundamentals_report>")
    parts.append(f"<sentiment_report>{_truncate(sentiment_report, 500)}</sentiment_report>")
    parts.append(f"<risk_debate>{_truncate(risk_debate_history, 1500)}</risk_debate>")
    if other_reports:
        extra_lines = []
        for k, v in other_reports.items():
            extra_lines.append(f"- {k}: {_truncate(v, 300)}")
        parts.append(f"<other_reports>{chr(10).join(extra_lines)}</other_reports>")
    return "\n".join(parts)


def create_summary_agent(llm):
    """
    创建结构化总结智能体，负责生成前端展示所需的 JSON 数据
    """

    async def summary_node(state):
        logger.info("📊 [Summary Agent] 开始生成结构化总结数据...")

        # 1. 收集上下文（stage.inputs 声明化消费，P3 §4.6：输入可见性 =
        #    声明集；analyst_reports 槽枚举本工作流约定的分析师报告清单）
        company_name = state.get("company_of_interest", "Unknown")
        from app.engine.decision_semantics import normalize_account_position

        account_position = normalize_account_position(
            state.get("account_position", "UNKNOWN")
        )

        stage_inputs = state.get("_stage_inputs") or {}
        all_reports = stage_inputs.get("analyst_reports") or {}

        # 核心报告（兼容旧代码）
        market_report = all_reports.get("market_report", "")
        news_report = all_reports.get("financial_news_report") or all_reports.get("news_report", "")
        fundamentals_report = all_reports.get("fundamentals_report", "")
        sentiment_report = all_reports.get("social_media_report") or all_reports.get("sentiment_report", "")

        # 交易计划与最终决策（field 槽）
        trader_plan = stage_inputs.get("trader_plan") or ""
        final_decision = stage_inputs.get("final_decision") or ""

        # 辩论历史（槽绑定 debate_state 整体；canonical 无 history 存储，
        # 经派生视图读取；旧形状透传）
        from app.engine.orchestrator.state import risk_history

        risk_debate_history = risk_history(stage_inputs.get("risk_debate_history"))

        # 其他报告（槽内清单剔除核心 4 个避免重复）
        other_reports = {
            k: v
            for k, v in all_reports.items()
            if k
            not in (
                "market_report",
                "news_report",
                "financial_news_report",
                "fundamentals_report",
                "sentiment_report",
                "social_media_report",
            )
        }

        # 输入体检（代码层确定性判断，不依赖 LLM）：
        # 占位/整体失败的报告替换为缺失占位，避免 LLM 见到失败字样后
        # 按"真实性检查"规则把整个总结判死（历史回归：多空辩论 8k+ 字符
        # 完整，仅因市场报告为空响应占位 + 新闻小节失败即整体输出失败）。
        def _sanitize(label: str, text: str) -> str:
            if _is_invalid_report(text):
                missing_labels.append(label)
                return _MISSING_PLACEHOLDER
            return text

        missing_labels: list[str] = []
        trader_plan_s = _sanitize("交易计划", trader_plan)
        final_decision_s = _sanitize("最终决策", final_decision)
        market_report_s = _sanitize("市场报告", market_report)
        news_report_s = _sanitize("新闻报告", news_report)
        fundamentals_report_s = _sanitize("基本面报告", fundamentals_report)
        sentiment_report_s = _sanitize("情绪报告", sentiment_report)
        risk_debate_s = _sanitize("风险辩论", risk_debate_history or "")

        other_reports_s: dict = {}
        for k, v in other_reports.items():
            other_reports_s[k] = _sanitize(k, v)

        # 全部非空输入均无效 → 不调 LLM，直接返回诚实失败结构。
        # 输入全为空（如上游未产出任何报告）时不短路，仍交给 LLM 按
        # SYSTEM_PROMPT 的真实性检查处理，保持既有空态行为不变。
        non_empty_inputs = [
            t
            for t in (
                trader_plan,
                final_decision,
                market_report,
                news_report,
                fundamentals_report,
                sentiment_report,
                risk_debate_history or "",
                *other_reports.values(),
            )
            if t
        ]
        if non_empty_inputs and all(_is_invalid_report(t) for t in non_empty_inputs):
            logger.warning(f"⚠️ [Summary Agent] 全部输入无效（{missing_labels}），跳过 LLM 调用，直接返回失败结构")
            return {
                "structured_summary": {
                    **_DEFAULT_STRUCTURED_DATA,
                    "analysis_summary": "数据获取失败，无法生成报告",
                    "investment_recommendation": "无建议",
                    "risk_assessment": {
                        "level": "High",
                        "score": None,
                        "description": "数据获取失败，无法生成报告",
                    },
                    "decision_semantics": {
                        **_DEFAULT_STRUCTURED_DATA["decision_semantics"],
                        "account_position": account_position,
                    },
                    # 降级透明化：兜底包打标，前端/报告显示"降级结果"徽标
                    # （结算登记侧已有占位包过滤，本标记只影响展示不影响统计）
                    "degraded": True,
                }
            }

        # 2. 构建 HumanMessage：可控内容用 XML 边界符包裹，便于 LLM 区分指令与数据
        coverage = {
            "market": _has_substantive_report(market_report),
            "news": _has_substantive_report(news_report),
            "fundamentals": _has_substantive_report(fundamentals_report),
            "sentiment": _has_substantive_report(sentiment_report),
        }
        user_prompt = _build_user_message(
            company_name=company_name,
            account_position=account_position,
            trader_plan=trader_plan_s,
            final_decision=final_decision_s,
            market_report=market_report_s,
            news_report=news_report_s,
            fundamentals_report=fundamentals_report_s,
            sentiment_report=sentiment_report_s,
            risk_debate_history=risk_debate_s,
            other_reports=other_reports_s,
            missing_labels=missing_labels,
        )

        # 3. 调用 LLM（submit_report 提交协议：structured_summary 的 8 字段
        #    schema 前置到工具参数，字段级校验由工具层强制；模型未调用工具 →
        #    fallback_text 降级，正文 JSON 解析降为 fallback 分支）
        try:
            submission = await run_node_turn(
                llm,
                [],
                user_prompt,
                system=role_prompt("summary", SYSTEM_PROMPT),
                node_type="summarizer",
                state=state,
                task_id=state.get("task_id") or "",
                agent_key="summary",
                phase="summary",
                user_id=state.get("user_id") or "",
                event_sink=state.get("_event_sink"),
            )

            if submission.fields:
                structured_data = dict(submission.fields)
                structured_data.setdefault("analysis_reference", [])
            else:
                content = submission.content.strip()
                if content.startswith("```json"):
                    content = content[7:]
                if content.endswith("```"):
                    content = content[:-3]
                structured_data = json.loads(content.strip())

            # Preserve whether split actions were actually submitted. Defaults must
            # not masquerade as model-provided WATCH/HOLD and overwrite the signal.
            submitted_semantics = structured_data.get("decision_semantics")
            submitted_semantics = submitted_semantics if isinstance(submitted_semantics, dict) else {}

            # 结构完整性校验：LLM 可能返回合法 JSON 但缺少必需字段
            required_keys = {
                "key_indicators", "model_confidence", "risk_assessment",
                "analysis_summary", "investment_recommendation",
                "decision_semantics", "final_signal",
            }
            missing = required_keys - set(structured_data.keys())
            if missing:
                logger.warning(
                    f"⚠️ [Summary Agent] LLM 返回 JSON 缺少必需字段: {missing}，"
                    "使用兜底值补齐"
                )
                structured_data = _ensure_required_fields(structured_data)

            structured_data = _repair_false_coverage_claims(
                structured_data, coverage
            )
            logger.info(f"✅ [Summary Agent] 成功生成结构化数据（{submission.source}）: {list(structured_data.keys())}")
            # 成功路径显式打标（含 fallback_text 解析与缺字段补齐），保证下游恒有该键
            structured_data["degraded"] = False

            # 账户状态是系统事实，LLM 只能抽取分支动作，不能从假设性标题猜测。
            from app.engine.decision_semantics import normalize_decision

            terminal_fields = state.get("final_decision_signal") or state.get("trader_decision_signal") or {}
            normalized_semantics = normalize_decision(
                {**terminal_fields, **(terminal_fields.get("decision_semantics") or {})}
                if terminal_fields else {"action": structured_data.get("final_signal"), **submitted_semantics},
                final_decision,
                account_position=account_position,
            )
            from app.engine.research_contract import qualitative_confidence
            structured_data.setdefault("confidence_label", qualitative_confidence(final_decision))
            from app.engine.decision_semantics import synchronize_summary
            structured_data = synchronize_summary(structured_data, normalized_semantics)

            logger.info(f"✅ [Summary Agent] 成功生成结构化数据: {list(structured_data.keys())}")
            
        except json.JSONDecodeError as e:
            logger.error(f"❌ [Summary Agent] JSON 解析失败: {e}", exc_info=True)
            logger.debug("总结响应无法解析；不记录完整报告正文")
            # 回退默认值
            structured_data = {
                "key_indicators": {
                    "entry_price": "N/A",
                    "target_price": "N/A",
                    "stop_loss": "N/A",
                    "support_level": "N/A",
                    "resistance_level": "N/A",
                },
                "model_confidence": None,
                "risk_assessment": {"level": "Unknown", "score": None, "description": "解析失败，评分未知"},
                "analysis_summary": "JSON解析失败，无法生成分析摘要",
                "investment_recommendation": "暂无建议",
                "decision_semantics": {
                    "account_position": account_position,
                    "research_view": "UNCERTAIN",
                    "unheld_action": "WATCH",
                    "held_action": "HOLD",
                    "position_limit_pct": None,
                    "exit_conditions": [],
                },
                "analysis_reference": [],
                "final_signal": "WATCH",
                "degraded": True,
            }
        except Exception as e:
            logger.error(f"❌ [Summary Agent] 生成失败: {e}", exc_info=True)
            # 即使失败也要返回空字典，防止图执行中断
            structured_data = {
                "key_indicators": {
                    "entry_price": "N/A",
                    "target_price": "N/A",
                    "stop_loss": "N/A",
                    "support_level": "N/A",
                    "resistance_level": "N/A",
                },
                "model_confidence": None,
                "risk_assessment": {"level": "Unknown", "score": None, "description": "生成失败，评分未知"},
                "analysis_summary": "系统错误：无法生成分析摘要",
                "investment_recommendation": "暂无建议",
                "decision_semantics": {
                    "account_position": account_position,
                    "research_view": "UNCERTAIN",
                    "unheld_action": "WATCH",
                    "held_action": "HOLD",
                    "position_limit_pct": None,
                    "exit_conditions": [],
                },
                "analysis_reference": [],
                "final_signal": "WATCH",
                "degraded": True,
            }

        from app.core.analysis_horizon import normalize_analysis_horizon
        structured_data["analysis_horizon"] = normalize_analysis_horizon(state.get("analysis_horizon"))
        return {"structured_summary": structured_data}

    return summary_node
