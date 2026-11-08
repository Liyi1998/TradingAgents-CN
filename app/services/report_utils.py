"""报告规范化工具。"""

from typing import Dict, Tuple


_CONTENT_ALIAS_GROUPS = (
    ("research_team_decision", ("investment_plan",)),
    ("final_trade_decision", ("risk_management_decision", "risk_manager_decision")),
)


def dedupe_report_aliases(reports: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """仅删除内容完全一致的已知别名，避免误删真正不同的阶段报告。"""
    cleaned = dict(reports or {})
    aliases: Dict[str, str] = {}
    for canonical, candidates in _CONTENT_ALIAS_GROUPS:
        canonical_content = cleaned.get(canonical)
        if not canonical_content:
            # 最终决策缺失时，允许以风险经理版本作为规范内容。
            for candidate in candidates:
                if cleaned.get(candidate):
                    canonical_content = cleaned[candidate]
                    cleaned[canonical] = canonical_content
                    break
        if not canonical_content:
            continue
        for candidate in candidates:
            if candidate in cleaned and cleaned[candidate].strip() == canonical_content.strip():
                cleaned.pop(candidate, None)
                aliases[candidate] = canonical
    return cleaned, aliases
