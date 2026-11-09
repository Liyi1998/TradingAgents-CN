"""Deterministic data-quality appendix from actual facade reads, not LLM confidence."""
import json

def render_data_quality(context: dict, coverage: list[dict]) -> str:
    lines = ["# 数据质量与取数覆盖", "", "以下元信息来自实际数据接口；不表示报告正确率、胜率或模型置信度。",
             f"分析时点：{context.get('as_of', '未知')}；最近实际交易日：{context.get('last_actual_trade_date', '未知')}。",
             "按行情发生日、新闻发布时间、财报公告日期检查分析日覆盖；缓存更新时间不是发布时点，不能因取数晚几秒就排除当日资料。未验证逐秒历史可得性时如实披露。",
             "", "| 数据域 | 状态 | 缓存时效 | 数据截止 | 来源 | 缺失/冲突原因 |", "|---|---|---|---|---|---|"]
    for item in coverage:
        quality = item.get("quality") or {}
        gaps = list(quality.get("missing_fields") or [])
        if quality.get("missing_dates"):
            gaps.append("缺日期:" + ",".join(quality["missing_dates"]))
        if quality.get("financial_period_holes"):
            gaps.append("财务历史缺口:" + ",".join(quality["financial_period_holes"]))
        if quality.get("unit_conflicts"):
            gaps.append("成交量/额单位冲突（读取时隔离）")
        if quality.get("field_conflicts"):
            gaps.append("同报告期多源口径冲突")
        if quality.get("contract_issues"):
            gaps.append("取数契约:" + ",".join(quality["contract_issues"]))
        if quality.get("optional_field_status"):
            gaps.append("额外字段未证实:" + json.dumps(quality["optional_field_status"], ensure_ascii=False))
        if quality.get("fallback_from"):
            gaps.append("备用链路:" + quality["fallback_from"])
        if not gaps:
            gaps = [quality.get("reason") or "无已识别缺口"]
        cells = [item.get("domain"), quality.get("status"), quality.get("cache_freshness") or "未知", quality.get("data_cutoff") or "未知",
                 ",".join(quality.get("sources") or []) or "未知", ";".join(gaps)]
        lines.append("| " + " | ".join(str(cell).replace("|", "/").replace("\n", " ") for cell in cells) + " |")
    if not coverage:
        lines.append("| 未采集 | 未知 | 未知 | 未知 | 未知 | 不得推断数据完整 |")
    if context.get("builtin_fallback_statuses"):
        lines += ["", "MCP失败后实际执行的内置备用数据（仅部分标准数据，不证明整个外部查询已替代）：",
                  json.dumps(context["builtin_fallback_statuses"], ensure_ascii=False)]
    if context.get("role_data_checks"):
        from app.engine.data_preflight import render_role_checks
        lines += ["", render_role_checks(context)]
    if context.get("news_classification_coverage"):
        lines += ["", "## 新闻文本分类覆盖（不是社交情绪或置信度）", "",
                  json.dumps(context["news_classification_coverage"], ensure_ascii=False)]
    lines += ["", "## 不可观测边界", ""]
    lines += ["单项数据不可用不阻止整体报告；系统保留真实缺项，不能将受影响的合计、评分或条件判断当作已验证事实。", ""]
    lines += ["- " + value for value in context.get("capability_limits") or ["未验证外部实时事实；缺失不能替换为默认中性或零。"]]
    lines += ["", "## 覆盖元信息", "", "```json", json.dumps(coverage, ensure_ascii=False, indent=2, default=str), "```"]
    return "\n".join(lines)
