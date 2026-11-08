"""
股票分析服务
整合了原 simple_analysis_service.py 和 analysis_service.py 的功能
"""

import logging
from typing import Dict, Any, List, Optional

from app.engine.decision_semantics import (
    synchronize_summary,
)
from app.data.core.interface import DataInterface


# 设置日志
logger = logging.getLogger("app.services.analysis_service")

# config_service imported from app.services.config_service (facade singleton)

# 股票基础信息获取（用于补充显示名称）
try:
    _di = DataInterface.get_instance()

    async def _get_stock_info_async(stock_code: str):
        """获取股票基础信息（异步直查）。

        旧实现经 run_async 桥接：在主线程事件循环内调用会抛 RuntimeError
        被吞掉，任务创建时永远拿不到真名、只剩占位名；调用方均在 async
        上下文，这里改为直接 await。
        """
        from app.core.async_utils import await_on_main_loop
        try:
            result = await await_on_main_loop(_di.read("CN", "basic_info", symbol=stock_code))
            return result.get("data") if result else None
        except Exception as e:
            logger.debug(f"获取股票信息失败: {e}")
            return None
except Exception as e:
    logger.debug(f"DataInterface 初始化失败，_get_stock_info_async 不可用: {e}")
    _get_stock_info_async = None

# -----------------------------------------------------------------------------
# Helper Functions (from simple_analysis_service.py)
# -----------------------------------------------------------------------------

# 历史任务状态 → 面向用户的消息（避免 "任务completed中..." 这类拼接错误）
_TASK_STATUS_MESSAGES: Dict[str, str] = {
    "pending": "任务排队中…",
    "queued": "任务排队中…",
    "running": "任务分析中…",
    "completed": "任务已完成",
    "failed": "任务执行失败",
    "cancelled": "任务已取消",
    "canceled": "任务已取消",
}

_RISK_LEVEL_MAP = {"High": "高", "Medium": "中等", "Low": "低"}


def _extract_result_risk_and_confidence(
    decision: Any, structured_summary: Any
) -> tuple[Optional[float], str]:
    """统一分析结果的置信度和风险等级字段。

    Stage 4 的最终裁决使用 ``decision.confidence`` / ``risk_score``，总结节点
    使用 ``model_confidence``（0-100）和英文风险等级。此前只读取不存在的
    ``decision.confidence_score`` / ``decision.risk_level``，导致数据库固定保存
    0 和“中等”。结构化总结优先，最终裁决作为可靠回退。
    """
    decision = decision if isinstance(decision, dict) else {}
    structured_summary = (
        structured_summary if isinstance(structured_summary, dict) else {}
    )

    raw_confidence = structured_summary.get("model_confidence")
    if decision.get("schema_version", 0) >= 3:
        structured_summary = synchronize_summary(structured_summary, decision)
        raw_confidence = structured_summary.get("model_confidence")
    if raw_confidence is not None:
        from app.engine.decision_evidence import number
        value = number(raw_confidence, 0, 100)
        raw_confidence = value / 100 if value is not None else None
    if raw_confidence is None and decision.get("schema_version", 0) < 3:
        raw_confidence = decision.get("confidence_score")
    if raw_confidence is None:
        raw_confidence = decision.get("confidence")
    try:
        from app.engine.decision_evidence import number
        confidence = number(raw_confidence, 0, 100)
        if confidence is not None and confidence > 1:
            confidence /= 100.0
        confidence = min(1.0, max(0.0, confidence)) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None

    risk = structured_summary.get("risk_assessment")
    raw_level = risk.get("level") if isinstance(risk, dict) else None
    risk_level = _RISK_LEVEL_MAP.get(raw_level, raw_level) if raw_level else None
    if not risk_level:
        risk_level = decision.get("risk_level")
    if not risk_level:
        try:
            from app.engine.decision_evidence import number
            risk_score = number(decision.get("risk_score"), 0, 1)
            if risk_score is None:
                raise ValueError("unquantified_risk")
            risk_level = "高" if risk_score >= 0.7 else "低" if risk_score <= 0.3 else "中等"
        except (TypeError, ValueError):
            risk_level = "未知"

    return confidence, str(risk_level)


def _derive_key_points(
    structured_summary: Any,
    recommendation: str = "",
    risk_level: str = "",
) -> List[str]:
    """仅从已生成的结构化结论提取前端关键要点，不补造分析数值。"""
    summary = structured_summary if isinstance(structured_summary, dict) else {}
    candidates: List[str] = []
    if recommendation and str(recommendation).strip():
        candidates.append(f"操作建议：{str(recommendation).strip()}")

    indicators = summary.get("key_indicators") or {}
    if isinstance(indicators, dict):
        labels = {
            "entry_price": "入场参考",
            "target_price": "目标位",
            "stop_loss": "止损位",
            "support_level": "支撑位",
            "resistance_level": "阻力位",
        }
        for key, label in labels.items():
            value = str(indicators.get(key) or "").strip()
            if value and value.upper() not in {"N/A", "NA", "NONE", "未知"}:
                candidates.append(f"{label}：{value}")

    risk = summary.get("risk_assessment") or {}
    if isinstance(risk, dict):
        description = str(risk.get("description") or "").strip()
        if description:
            prefix = f"风险（{risk_level}）" if risk_level else "主要风险"
            candidates.append(f"{prefix}：{description}")

    result: List[str] = []
    for item in candidates:
        compact = " ".join(item.split())[:180]
        if compact and compact not in result:
            result.append(compact)
        if len(result) >= 5:
            break
    return result


def _is_placeholder_stock_name(name: Any, code: str) -> bool:
    text = str(name or "").strip()
    return not text or text in {code, f"股票{code}", "Unknown", "UNKNOWN"}
