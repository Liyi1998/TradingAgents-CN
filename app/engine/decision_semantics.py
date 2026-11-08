"""Position-aware decision semantics and legacy compatibility helpers.

The analysis pipeline historically collapsed every conclusion into buy/hold/sell.
That made ``hold`` ambiguous: it could mean "keep an existing position" or
"do not open a position yet".  This module keeps those branches separate and
contains only deterministic parsing/formatting logic so it can also adapt old
reports without another LLM call or a database migration.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional


DECISION_SCHEMA_VERSION = 3

ACCOUNT_POSITIONS = frozenset({"UNKNOWN", "NONE", "HELD"})
RESEARCH_VIEWS = frozenset({"BULLISH", "NEUTRAL", "BEARISH", "UNCERTAIN"})
UNHELD_ACTIONS = frozenset({"BUY", "WATCH"})
HELD_ACTIONS = frozenset({"HOLD", "REDUCE", "SELL"})

ACTION_LABELS = {
    "BUY": "买入",
    "WATCH": "观望",
    "HOLD": "持有",
    "REDUCE": "减仓",
    "SELL": "卖出",
}
ACCOUNT_POSITION_LABELS = {
    "UNKNOWN": "未提供",
    "NONE": "未持仓",
    "HELD": "已持仓",
}
RESEARCH_VIEW_LABELS = {
    "BULLISH": "偏多",
    "NEUTRAL": "中性",
    "BEARISH": "偏空",
    "UNCERTAIN": "不确定",
}

_DECISION_REPORT_KEYS = (
    "risk_manager_decision",
    "risk_management_decision",
    "final_trade_decision",
    "trader_investment_plan",
    "investment_plan",
    "research_team_decision",
)

_UNHELD_MARKERS = re.compile(
    r"未持仓|尚未持仓|当前空仓|空仓账户|空仓者|未持有|无持仓"
)
_HELD_MARKERS = re.compile(
    r"已持仓|已有持仓|现有持仓|已有仓位|持仓账户|持有者"
)
_WATCH_TERMS = re.compile(
    r"观望|等待|暂缓|不宜(?:追涨|买入|建仓|加仓)|"
    r"不(?:要|应|得|建议)?(?:追涨|追买|买入|建仓|加仓)|"
    r"维持\s*0\s*%|不新增|不追涨|不得追涨"
)
_ENTRY_TERMS = re.compile(r"买入|建仓|试仓|介入|配置|增持")
_CONDITIONAL_TERMS = re.compile(
    r"等待|仅在|只有|如果|若|一旦|确认后|突破后|回踩后|企稳后|站稳后|触发后"
)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _clean_code(value: Any) -> str:
    return re.sub(r"[\s_\-/（）()]+", "", _as_text(value)).upper()


def normalize_account_position(value: Any) -> str:
    """Normalize an explicitly supplied account position.

    Report headings such as "for existing holders" are hypothetical branches,
    not evidence about the current user's account, so callers must pass this
    field explicitly. Unknown/invalid values remain ``UNKNOWN``.
    """

    code = _clean_code(value)
    if code in {"NONE", "FLAT", "NOPOSITION", "未持仓", "空仓", "无持仓"}:
        return "NONE"
    if code in {"HELD", "HOLDING", "POSITIONED", "已持仓", "持仓", "有持仓"}:
        return "HELD"
    return "UNKNOWN"


def normalize_research_view(value: Any) -> Optional[str]:
    code = _clean_code(value)
    if code in RESEARCH_VIEWS:
        return code
    text = _as_text(value)
    if re.search(r"偏多|看多|看涨|积极", text):
        return "BULLISH"
    if re.search(r"偏空|看空|看跌|悲观", text):
        return "BEARISH"
    if re.search(r"中性|谨慎|震荡|均衡", text):
        return "NEUTRAL"
    return None


def normalize_action_code(value: Any) -> Optional[str]:
    """Map an action token or short localized label to a canonical code."""

    code = _clean_code(value)
    if code in ACTION_LABELS:
        return code
    text = _as_text(value)
    # Order matters for composite labels such as "持有但不新增".
    if re.search(r"观望|等待|暂缓|不新增|不买|不追|WATCH", text, re.IGNORECASE):
        return "WATCH"
    if re.search(r"减仓|降低仓位|缩减仓位|REDUCE", text, re.IGNORECASE):
        return "REDUCE"
    if re.search(r"清仓|全部退出|卖出|离场|SELL", text, re.IGNORECASE):
        return "SELL"
    if re.search(r"买入|建仓|增持|BUY", text, re.IGNORECASE):
        return "BUY"
    if re.search(r"持有|保留|HOLD", text, re.IGNORECASE):
        return "HOLD"
    return None


def _focus_text(text: str, edge_chars: int = 4000) -> str:
    """Prefer executive summaries and final directives in long legacy reports."""

    if len(text) <= edge_chars * 2:
        return text
    return f"{text[:edge_chars]}\n{text[-edge_chars:]}"


def _marker_contexts(text: str, marker: re.Pattern[str], window: int = 260) -> list[str]:
    contexts: list[str] = []
    for match in marker.finditer(text):
        start = max(0, match.start() - 30)
        contexts.append(text[start : match.end() + window])
    return contexts


def _infer_unheld_action(source_text: str, raw_action: Optional[str]) -> str:
    contexts = _marker_contexts(_focus_text(source_text), _UNHELD_MARKERS)
    for context in contexts:
        if _WATCH_TERMS.search(context):
            return "WATCH"
        if _ENTRY_TERMS.search(context):
            if _CONDITIONAL_TERMS.search(context):
                return "WATCH"
            return "BUY"
    # Legacy HOLD/SELL never means that a flat account should transact.
    return "BUY" if raw_action == "BUY" else "WATCH"


def _is_conditional_before_action(context: str, action_match: re.Match[str]) -> bool:
    prefix = context[max(0, action_match.start() - 45) : action_match.start()]
    return bool(_CONDITIONAL_TERMS.search(prefix) or re.search(r"跌破|失守|触及", prefix))


def _infer_held_action(source_text: str, raw_action: Optional[str]) -> str:
    contexts = _marker_contexts(_focus_text(source_text), _HELD_MARKERS)
    for context in contexts:
        sell_match = re.search(r"立即(?:清仓|退出|卖出)|建议(?:清仓|卖出)|全部退出|全部卖出", context)
        if sell_match and not _is_conditional_before_action(context, sell_match):
            return "SELL"
        reduce_match = re.search(r"减仓|降至\s*\d|降低仓位|缩减仓位|压缩仓位", context)
        if reduce_match and not _is_conditional_before_action(context, reduce_match):
            return "REDUCE"
        if re.search(r"继续持有|谨慎持有|可持有|保留(?:底仓|仓位)", context):
            return "HOLD"
    if raw_action in HELD_ACTIONS:
        return raw_action
    if raw_action == "SELL":
        return "SELL"
    if raw_action == "REDUCE":
        return "REDUCE"
    return "HOLD"


def _infer_research_view(source_text: str, raw_action: Optional[str]) -> str:
    focus = _focus_text(source_text)
    rating = re.search(
        r"(?:研究观点|研究结论|分析倾向|最终投资评级|最终评级|总体评级|投资评级)"
        r"[^\n。；]{0,35}?(偏多|看多|看涨|买入|增持|中性|谨慎|持有|观望|偏空|看空|看跌|卖出)",
        focus,
    )
    if rating:
        value = rating.group(1)
        if value in {"偏多", "看多", "看涨", "买入", "增持"}:
            return "BULLISH"
        if value in {"偏空", "看空", "看跌", "卖出"}:
            return "BEARISH"
        return "NEUTRAL"
    if raw_action == "BUY":
        return "BULLISH"
    if raw_action in {"SELL", "REDUCE"}:
        return "BEARISH"
    if raw_action in {"HOLD", "WATCH"}:
        return "NEUTRAL"
    return "UNCERTAIN"


def _coerce_position_limit(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    if not 0 <= number <= 100:
        return None
    return round(number, 2)


def _extract_position_limit(source_text: str) -> Optional[float]:
    focus = _focus_text(source_text)
    patterns = (
        r"(?:仓位上限|最高(?:总)?仓位|最高总仓|总仓上限)[^\d%]{0,24}(\d+(?:\.\d+)?)\s*%",
        r"(?:总仓|仓位)[^\n。；]{0,30}?(?:最高|不超过|不得超过|控制在|上限为|降至)\s*(\d+(?:\.\d+)?)\s*%",
        r"(?:提高至|控制在|降至)\s*(?:最高)?\s*(\d+(?:\.\d+)?)\s*%",
    )
    values: list[float] = []
    for pattern in patterns:
        for raw in re.findall(pattern, focus):
            value = _coerce_position_limit(raw)
            if value is not None:
                values.append(value)
    return max(values) if values else None


def _normalize_exit_conditions(value: Any) -> list[str]:
    if value is None:
        return []
    items: Iterable[Any]
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = re.split(r"[\n；;]+", _as_text(value))
    result: list[str] = []
    for item in items:
        text = re.sub(r"^\s*(?:[>#*\-]+\s*|\d+[.、)]\s+)", "", _as_text(item)).strip()
        text = " ".join(text.split())[:600]
        if text and text not in result:
            result.append(text)
        if len(result) >= 20:
            break
    return result


def _extract_exit_conditions(source_text: str) -> list[str]:
    clauses = re.split(r"[\n。；;]+", source_text or "")
    candidates = []
    for clause in clauses:
        clean = re.sub(r"^[\s>#*\-\d.、]+", "", clause).strip()
        if not clean:
            continue
        if re.search(r"止损|退出|清仓|离场|减仓|跌破|失守", clean):
            candidates.append(clean)
    candidates.sort(key=lambda text: (0 if re.search(r"EXIT[-_]", text, re.I) else 1))
    return _normalize_exit_conditions(candidates)


def collect_decision_source(payload: Mapping[str, Any] | None) -> str:
    """Collect the most decision-relevant text from a result/report document."""

    if not isinstance(payload, Mapping):
        return ""
    parts: list[str] = []
    decision = payload.get("decision")
    if isinstance(decision, Mapping):
        for key in ("reasoning", "reason", "recommendation", "legacy_action"):
            value = _as_text(decision.get(key)).strip()
            if value:
                parts.append(value)

    recommendation = _as_text(payload.get("recommendation")).strip()
    if recommendation:
        parts.append(recommendation)

    reports = payload.get("reports")
    if isinstance(reports, Mapping):
        for key in _DECISION_REPORT_KEYS:
            value = _as_text(reports.get(key)).strip()
            if value:
                return value

    state = payload.get("state")
    if isinstance(state, Mapping):
        for key in ("final_trade_decision", "trader_investment_plan", "investment_plan"):
            value = _as_text(state.get(key)).strip()
            if value:
                parts.append(value)

    structured = payload.get("structured_summary")
    if isinstance(structured, Mapping):
        value = _as_text(structured.get("investment_recommendation")).strip()
        if value:
            parts.append(value)
    return "\n\n".join(parts)


def normalize_decision(
    decision: Mapping[str, Any] | None,
    source_text: str = "",
    *,
    account_position: Any = None,
) -> dict[str, Any]:
    """Return a v3 evidence-bound decision while preserving legacy aliases.

    For an unknown account, ``primary_action`` deliberately follows the
    no-position branch. This makes legacy HOLD display as WATCH while keeping
    ``held_action=HOLD`` for users who already own the stock.
    """

    raw = dict(decision) if isinstance(decision, Mapping) else {}
    # ``account_position`` alone is transport context, not proof that the
    # producer supplied the split decision schema.  This matters when an older
    # model echoes only that field and the actions still need deterministic
    # legacy inference.
    has_v2_fields = any(
        key in raw
        for key in (
            "research_view",
            "unheld_action",
            "held_action",
            "position_limit_pct",
            "exit_conditions",
            "primary_action",
        )
    )
    raw_action_value = raw.get("legacy_action", raw.get("action"))
    raw_action = normalize_action_code(raw_action_value)

    explicit_account = account_position if account_position is not None else raw.get("account_position")
    normalized_account = normalize_account_position(explicit_account)

    research_view = normalize_research_view(raw.get("research_view"))
    if research_view is None:
        research_view = _infer_research_view(source_text, raw_action)

    unheld_action = normalize_action_code(
        raw.get("unheld_action", raw.get("no_position_action"))
    )
    if unheld_action not in UNHELD_ACTIONS:
        unheld_action = _infer_unheld_action(source_text, raw_action)

    held_action = normalize_action_code(
        raw.get("held_action", raw.get("holding_action"))
    )
    if held_action not in HELD_ACTIONS:
        held_action = _infer_held_action(source_text, raw_action)

    from app.engine.decision_evidence import position_facts, validate_numeric_facts

    if re.search(r"(?:当前|未持仓|空仓)[^。\\n]{0,18}(?:不买|WATCH|不批准.*新增|暂停新增)|当前不提前试仓", source_text):
        unheld_action = "WATCH"
    positions = position_facts({**raw, "unheld_action": unheld_action,
                                "account_position": normalized_account}, source_text)
    if unheld_action == "WATCH":
        positions["current_entry_limit_pct"] = 0.0
    numeric_facts = validate_numeric_facts(raw, source_text)
    from app.engine.research_contract import qualitative_confidence
    confidence_label = raw.get("confidence_label")
    if not isinstance(confidence_label, str) or not confidence_label.strip():
        confidence_label = qualitative_confidence(source_text)

    exit_conditions = _normalize_exit_conditions(raw.get("exit_conditions"))
    if not exit_conditions:
        exit_conditions = _extract_exit_conditions(source_text)

    primary_action = held_action if normalized_account == "HELD" else unheld_action
    legacy_action = _as_text(raw_action_value).strip() or None

    result = {
        **raw,
        "schema_version": DECISION_SCHEMA_VERSION,
        "account_position": normalized_account,
        "research_view": research_view,
        "unheld_action": unheld_action,
        "held_action": held_action,
        **positions,
        **numeric_facts,
        "confidence_label": confidence_label,
        "exit_conditions": exit_conditions,
        "primary_action": primary_action,
        "primary_action_basis": (
            "ACCOUNT_POSITION" if normalized_account != "UNKNOWN" else "UNHELD_DEFAULT"
        ),
        "action": ACTION_LABELS[primary_action],
        "derived_from_legacy": bool(raw.get("derived_from_legacy", not has_v2_fields)),
        "semantics_source": (
            raw.get("semantics_source")
            or ("STRUCTURED" if has_v2_fields else "LEGACY_DETERMINISTIC")
        ),
    }
    if legacy_action:
        result["legacy_action"] = legacy_action
    return result


DECISION_PRESENTATION_FIELDS = (
    "schema_version", "account_position", "research_view", "unheld_action",
    "held_action", "position_limit_pct", "total_position_limit_pct",
    "current_entry_limit_pct", "conditional_strategy", "position_evidence",
    "exit_conditions", "primary_action", "numeric_evidence",
    "decision_source_sha256",
)


def synchronize_summary(summary: Mapping[str, Any] | None, decision: Mapping[str, Any]) -> dict[str, Any]:
    """Synchronize actions, not erase a distinct model research assessment.

    Terminal scalars are execution/decision facts. Summary references and model
    self-assessments remain separately labelled research, never order authority.
    Repeated API/export adaptation must be idempotent and must not write history.
    """
    from copy import deepcopy
    from app.engine.decision_evidence import number

    normalized = normalize_decision(decision)
    result = deepcopy(dict(summary)) if isinstance(summary, Mapping) else {}
    result["decision_semantics"] = {
        key: normalized.get(key) for key in DECISION_PRESENTATION_FIELDS
    }
    result["final_signal"] = normalized["primary_action"]
    confidence = number(result.get("model_confidence"), 0, 100)
    metadata = result.get("assessment_metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    confidence_source = (metadata.get("confidence_source") or "summary_model_self_assessment") if confidence is not None else "unknown"
    if confidence is None and normalized.get("confidence") is not None:
        confidence = normalized["confidence"] * 100
        confidence_source = "explicit_terminal_self_assessment"
    result["model_confidence"] = confidence
    result["confidence_label"] = result.get("confidence_label") or normalized.get("confidence_label")
    risk_raw = result.get("risk_assessment")
    risk = dict(risk_raw) if isinstance(risk_raw, Mapping) else {}
    score = number(risk.get("score"), 0, 10)
    if score is None and normalized.get("risk_score") is not None:
        score = normalized["risk_score"] * 10
    risk["score"] = score
    if risk.get("level") not in ("High", "Medium", "Low", "高", "中等", "低", "中高", "中低"):
        risk["level"] = normalized.get("risk_level") or "Unknown"
    risk.setdefault("description", "风险未充分评估；未量化不代表低风险")
    result["risk_assessment"] = risk
    indicators_raw = result.get("key_indicators")
    indicators = dict(indicators_raw) if isinstance(indicators_raw, Mapping) else {}
    # A conditional textual technical target must not be replaced by an absent
    # fair-value/executable scalar. Its display is explicitly research-only.
    if indicators.get("target_price") is None or indicators.get("target_price") in ("", "N/A"):
        indicators["target_price"] = normalized.get("target_price") if normalized.get("target_price") is not None else "N/A"
    result["key_indicators"] = indicators
    result["assessment_metadata"] = {
        "confidence_source": confidence_source,
        "confidence_kind": "model_self_assessment_not_win_probability",
        "risk_kind": "model_judgement_not_measured_loss_probability",
        "price_kind": "research_reference_not_order",
        "account_action_source": "terminal_verdict",
    }
    return result


def format_decision_recommendation(decision: Mapping[str, Any] | None) -> str:
    normalized = normalize_decision(decision)
    parts = [
        f"账户持仓：{ACCOUNT_POSITION_LABELS[normalized['account_position']]}",
        f"研究观点：{RESEARCH_VIEW_LABELS[normalized['research_view']]}",
        (
            "未持仓："
            f"{ACTION_LABELS[normalized['unheld_action']]}（{normalized['unheld_action']}）"
        ),
        (
            "已持仓："
            f"{ACTION_LABELS[normalized['held_action']]}（{normalized['held_action']}）"
        ),
    ]
    limit = normalized.get("position_limit_pct")
    if limit is not None:
        parts.append(f"当前总仓位上限：{limit:g}%")
    if normalized.get("current_entry_limit_pct") is not None:
        parts.append(f"当前新增上限：{normalized['current_entry_limit_pct']:g}%")
    if normalized.get("conditional_strategy"):
        parts.append("后续条件策略须另获授权，不属于当前额度")
    conditions = normalized.get("exit_conditions") or []
    if conditions:
        parts.append(f"首要退出条件：{conditions[0]}")
    return "；".join(parts)


def merge_decision_key_points(
    existing: Iterable[Any] | None,
    decision: Mapping[str, Any] | None,
) -> list[str]:
    """Prepend position-aware points and discard legacy single-action headlines."""

    normalized = normalize_decision(decision)
    points = [
        f"未持仓：{ACTION_LABELS[normalized['unheld_action']]}（{normalized['unheld_action']}）",
        f"已持仓：{ACTION_LABELS[normalized['held_action']]}（{normalized['held_action']}）",
    ]
    if normalized.get("position_limit_pct") is not None:
        points.append(f"仓位上限：{normalized['position_limit_pct']:g}%")
    if normalized.get("exit_conditions"):
        points.append(f"退出条件：{normalized['exit_conditions'][0]}")

    existing_items: Iterable[Any]
    if isinstance(existing, (str, bytes)):
        existing_items = [existing]
    else:
        existing_items = existing or []

    for item in existing_items:
        text = " ".join(_as_text(item).split())[:180]
        legacy_headline = re.match(
            r"^(?:操作建议|操作|行动|投资建议|最终(?:投资)?评级|建议)[:：]?",
            text,
        ) and re.search(
            r"买入|观望|持有|减仓|卖出|BUY|WATCH|HOLD|REDUCE|SELL",
            text,
        )
        if not text or legacy_headline:
            continue
        if text not in points:
            points.append(text)
        if len(points) >= 5:
            break
    return points[:5]


def render_decision_markdown(stock_symbol: str, decision: Mapping[str, Any] | None) -> str:
    normalized = normalize_decision(decision)

    def _format_ratio(value: Any) -> str:
        try:
            return f"{float(value):.1%}" if value is not None else "N/A"
        except (TypeError, ValueError):
            return "N/A"

    lines = [
        f"# {stock_symbol} 最终投资决策",
        "",
        "## 分账户裁决",
        "",
        f"- **账户持仓状态**: {ACCOUNT_POSITION_LABELS[normalized['account_position']]}",
        f"- **研究观点**: {RESEARCH_VIEW_LABELS[normalized['research_view']]}（{normalized['research_view']}）",
        f"- **未持仓动作**: {ACTION_LABELS[normalized['unheld_action']]}（{normalized['unheld_action']}）",
        f"- **已持仓动作**: {ACTION_LABELS[normalized['held_action']]}（{normalized['held_action']}）",
        (
            "- **仓位上限**: "
            + (
                f"{normalized['position_limit_pct']:g}%"
                if normalized.get("position_limit_pct") is not None
                else "未明确"
            )
        ),
        "",
        "## 退出条件",
        "",
    ]
    if normalized.get("current_entry_limit_pct") is not None:
        lines.insert(16, f"- **当前新增上限**: {normalized['current_entry_limit_pct']:g}%")
    for strategy in normalized.get("conditional_strategy") or []:
        lines.append(f"- **后续条件策略（非当前额度）**: {strategy.get('condition', '未明确')}")
    conditions = normalized.get("exit_conditions") or []
    lines.extend([f"- {condition}" for condition in conditions] or ["- 未明确"])
    lines.extend(
        [
            "",
            "## 参考指标",
            "",
            f"- **置信度**: {_format_ratio(normalized.get('confidence'))}",
            f"- **风险评分**: {_format_ratio(normalized.get('risk_score'))}",
            f"- **目标价位**: {normalized.get('target_price') if normalized.get('target_price') is not None else 'N/A'}",
            "",
            "## 分析推理",
            "",
            _as_text(normalized.get("reasoning") or normalized.get("reason") or "暂无分析推理"),
            "",
        ]
    )
    return "\n".join(lines)
