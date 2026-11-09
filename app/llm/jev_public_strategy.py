"""Source-bound public strategy projection; never accepts summary as evidence.

Only the existing report allowlist feeds this module.  A private line is safe to
omit for completeness purposes only when it is a standalone personal fact.
Mixed clauses, unknown dependencies and lossy extraction remain unknown.
"""
from __future__ import annotations

import json
import re
from typing import Callable

from app.engine.decision_semantics import normalize_decision

_FIELDS = {
    "action": r"买入|观望|等待|不新增|持有|减仓|卖出|清仓|WATCH|BUY|HOLD|REDUCE|SELL",
    "total_limit": r"总仓位|仓位上限|总上限|资产权重|敞口",
    "entry_limit": r"(?:新增|首笔|试仓|建仓).{0,15}(?:上限|额度|最多|不超过|\d\s*%)",
    "authorization": r"授权|审批|批准|许可",
    "veto": r"否决|禁止|不得|不批准|不允许|不执行",
    "exit": r"退出|止损|失效|重新核验|复评|重评|重新评估|跌破|失守|清仓",
    "conditions": r"若|如果|仅在|只有|一旦|确认后|满足|有效期|交易日|期限|未来|后续",
}
_DEPENDENCY = re.compile(
    r"(?:根据|取决于|结合|相对|按|以).{0,20}(?:实际(?:持仓|仓位|成本|净资产|余额|股数|持有)|个人|用户|账户|成本价|持仓)|"
    r"成本(?:价)?[上下]|回本|实际(?:浮亏|浮盈)|上述(?:账户|持仓|成本)|"
    r"(?:授权|审批|批准).{0,12}(?:未知|不明|未明确|待确认)|"
    r"(?<![意观])(?:见|参见|详见|遵循|沿用)\s*(?:前文|上文|另文|附件|前述|上述|其他报告|账户|风险管理报告)|"
    r"(?:内容|报告|文本|材料).{0,8}(?:省略|截断|未完成|被截取)|输出中断|篇幅限制|\[.*省略"
)
_UNCLEAR_AUTHORIZATION = re.compile(r"(?:授权|审批|批准)(?:状态|条件)?.{0,8}(?:未知|不明|未明确|待确认|另行讨论)")
_PURE_PRIVATE = re.compile(
    r"(?:(?:当前|现有|已有|实际)?(?:持仓|持有)(?:数量)?\s*[:：为]?\s*[\d,.]+\s*股|"
    r"(?:当前|现有|已有|实际)(?:仓位|持仓比例|持有比例)\s*[:：为]?\s*[\d.]+%|"
    r"(?:持仓成本|平均成本|成本价|成本|买入价)\s*[:：为]?\s*[\d,.]+\s*(?:元)?|"
    r"账户(?:净资产|总资产|余额|金额)\s*[:：为]?\s*[\d,.]+\s*(?:元|万元|亿元)?|"
    r"账户(?:持仓)?状态\s*[:：为]?\s*(?:已持仓|未持仓|空仓|未知|未提供)|"
    r"账户持仓\s*[:：为]?\s*(?:已持仓|未持仓|空仓|未知|未提供))"
)
_STATE_LABEL = re.compile(r"(?:本)?账户(?:持仓)?状态")
_STATE_VALUE = re.compile(r"(?:NONE|HELD|已持仓|未持仓|空仓|未知|未提供)(?:\s*[:：（(]\s*(?:已持仓|未持仓|空仓)[）)]?)?", re.I)
_PUBLIC_POLICY = re.compile(
    r"策略|动作|上限|额度|敞口|退出|止损|止盈|保护|复评|重评|禁止|不新增|不买|观望|WATCH|HOLD|REDUCE|SELL|"
    r"假设|情景|政策|映射|未来|后续|空仓措施|研究|条件|授权|风险预算|集中度|成本前|几何盈亏比", re.I,
)


def independent_state_metadata(line: str) -> tuple[str, bool]:
    """Only split independent state cells, never mixed account/risk clauses.

    Delimited executive metadata may retain public prices/actions/caps while
    dropping the standalone state value. A table's private-state row is omitted
    only if its commentary is a factual explanation, not an execution rule.
    """
    plain = re.sub(r"[*_`#]", "", line).strip().lstrip("- ")
    if plain.startswith("|"):
        cells = [cell.strip() for cell in plain.strip("|").split("|")]
        if len(cells) >= 2 and _STATE_LABEL.fullmatch(cells[0]) and _STATE_VALUE.fullmatch(cells[1]):
            note = " ".join(cells[2:])
            if not note or re.fullmatch(
                r"采用系统明确事实，不猜测账户已有股份|系统明确未持仓，无股份可减或可卖", note,
            ):
                return "", True
        return line, False
    if "｜" in plain:
        parts = [part.strip() for part in plain.split("｜")]
        retained = []
        omitted = False
        for part in parts:
            match = re.fullmatch(r"账户(?:持仓)?状态\s*[:：]\s*(.*)", part)
            if match and all(_STATE_VALUE.fullmatch(value.strip()) for value in re.split(r"[，,]", match[1])):
                omitted = True
            else:
                retained.append(part)
        if omitted:
            return "｜".join(retained), True
    return line, False


def generalize_branch(line: str) -> str:
    """Preserve hypothetical action headings, not actual account state."""
    plain = re.sub(r"[*_`#]", "", line).strip().lstrip("- ")
    branch = re.fullmatch(
        r"(未持仓|已持仓)(?:动作|分支|策略)\s*[:：]\s*"
        r"(买入|观望|持有|减仓|卖出)(?:\s*[（(](BUY|WATCH|HOLD|REDUCE|SELL)[）)])?\s*[。；;]?",
        plain,
    )
    if branch:
        prefix = "新增策略动作" if branch[1] == "未持仓" else "存量策略动作"
        return f"{prefix}：{branch[2]}" + (f"（{branch[3]}）" if branch[3] else "")
    if not _PUBLIC_POLICY.search(line):
        return line
    # These are policy actors/conditions, not observations about actual shares.
    # Numeric personal facts are checked BEFORE this function by the sanitizer.
    for pattern, replacement in (
        (r"(?:本账户|当前账户)\s*(?:[:：]\s*)?(?:NONE|HELD)\b", "当前策略"),
        (r"(?:本|当前)?(?:NONE|HELD)账户", "当前策略"),
        (r"未持仓(?=动作|分支|策略|账户|情景)", "新增策略"),
        (r"已持仓(?=动作|分支|策略)", "存量策略"),
        (r"(?:假设)?(?:已持仓|已有持仓|已有仓位)(?:账户|情景|分支)?", "存量策略分支"),
        (r"(?<!当前)持仓(?=分支|措施)", "存量策略"),
        (r"基础持仓", "基础策略敞口"),
        (r"未来实际建仓", "未来获准建仓"),
        (r"当前空仓", "当前新增策略分支"),
        (r"空仓(?=动作|措施|账户|WATCH|分支|情景)", "新增策略"),
        (r"分账户", "分支"),
        (r"(?:本账户|当前账户)", "当前策略"),
        (r"账户规则", "策略规则"),
    ):
        line = re.sub(pattern, replacement, line)
    # Public future formulas use symbolic variables; actual cost values remain
    # prohibited. An unscoped "止损按成本价下方5%" is deliberately NOT rewritten.
    if re.search(r"未来|假设|若有真实入场价", line) and re.search(r"保护|止损|卖出|退出|成本前|盈亏比", line):
        line = line.replace("实际加权成本", "届时加权入场变量").replace("实际入场价", "届时入场变量")
        line = line.replace("成本下方", "届时入场变量下方").replace("成本价未知", "入场变量未知")
        line = line.replace("成本未知", "入场变量未知").replace("当前策略无成本", "当前策略无已核验入场变量")
    return line


def _standalone_personal_fact(line: str) -> bool:
    plain = re.sub(r"[*_`#]", "", line).strip().lstrip("- ")
    parts = [p.strip() for p in re.split(r"[；;，,](?!\d{3}(?:\D|$))", plain.rstrip("。")) if p.strip()]
    return bool(parts) and all(_PURE_PRIVATE.fullmatch(part) for part in parts)


def project_public_strategy(
    source: str,
    section: str,
    sanitizer: Callable[[str, int], tuple[str, bool, int]],
) -> dict:
    """Project safe evidence from the complete final/plan text, locally only.

    Each keyword hit cites retained source lines. A scan without a hit denotes
    material coverage, NOT semantic absence; the reviewer must read the complete
    source independently. Summary assertions and default WATCH are not evidence.
    The caller must invalidate this assessment if the outbound text is cut.
    """
    lines = source.splitlines()
    retained: list[tuple[int, str]] = []
    unsafe_omission = False
    private_fact_lines = 0
    for index, line in enumerate(lines, 1):
        safe, shortened, removed = sanitizer(line, max(len(line.encode("utf-8")) + 1024, 2048))
        if removed:
            remainder, state_omitted = independent_state_metadata(line)
            state_only_omission = state_omitted and not remainder
            if state_omitted and remainder:
                remaining_safe, remaining_shortened, remaining_removed = sanitizer(
                    remainder, max(len(remainder.encode("utf-8")) + 1024, 2048),
                )
                state_only_omission = (not remaining_shortened and not remaining_removed
                                       and remaining_safe == safe)
            if _standalone_personal_fact(line) or state_only_omission:
                private_fact_lines += 1
            else:
                unsafe_omission = True
        if safe:
            retained.append((index, safe))
        unsafe_omission |= shortened
    safe_text = "\n".join(line for _, line in retained)
    dependent = bool(_DEPENDENCY.search(safe_text) or _UNCLEAR_AUTHORIZATION.search(safe_text))
    # A private state can disambiguate the current branch LOCALLY. Its value is
    # not transmitted or used to invent a missing cap/action.
    state = re.search(r"账户(?:持仓)?状态[^。\n；]{0,20}?(NONE\b|HELD\b|未持仓|空仓|已持仓)", re.sub(r"[*_`#]", "", source), re.I)
    account = "UNKNOWN" if state is None else ("NONE" if state[1].upper() in {"NONE", "未持仓", "空仓"} else "HELD")
    authoritative = normalize_decision({}, source_text=source, account_position=account)
    public = normalize_decision({}, source_text=safe_text, account_position="UNKNOWN")
    fields = {}
    for field, pattern in _FIELDS.items():
        evidence = [{"line": index, "text": line} for index, line in retained if re.search(pattern, line, re.I)]
        status = "source_keyword_evidence" if evidence else "complete_source_no_keyword_evidence"
        if unsafe_omission or dependent:
            status = "unknown"
        fields[field] = {"status": status, "evidence": evidence if status != "unknown" else []}
    for field, key in (("total_limit", "total_position_limit_pct"), ("entry_limit", "current_entry_limit_pct")):
        value = public.get(key)
        # Do not present deterministic WATCH=0 or an ambiguous cap as a quoted fact.
        explicit = public.get("position_evidence", {}).get(key, {}).get("status") == "explicit"
        if fields[field]["status"] == "source_keyword_evidence" and (not explicit or value != authoritative.get(key)):
            fields[field]["status"] = "unknown"
            fields[field]["evidence"] = []
        if fields[field]["status"] == "source_keyword_evidence":
            fields[field]["value_pct"] = value
    # Inferred/default action is not evidence: send only exact action quotations.
    return {
        "version": "public-strategy-v2",
        "source_section": section,
        "source_complete": bool(source.strip()),
        "private_fact_lines_omitted": private_fact_lines,
        "unsafe_omission": unsafe_omission,
        "private_dependency": dependent,
        "fields": fields,
    }


def projection_text(projection: dict) -> str:
    # Full safe source is already sent above. Repeating every quoted line can
    # exceed the approved cap and incorrectly make a complete report unknown.
    return "\n\n[本地公共策略词法索引；行号指向原报告，引文已在上述完整脱敏文本中；非新增策略或语义验证]\n" + json.dumps(
        {"source_section": projection["source_section"],
         "fields": {name: {**field, "evidence": [item["line"] for item in field["evidence"]]} for name, field in projection["fields"].items()}},
        ensure_ascii=False, separators=(",", ":"),
    )


def projection_guards(projection: dict) -> dict[str, str]:
    """Only the specific proven-complete check may bypass a redacted flag."""
    fields = projection.get("fields") or {}
    required = {
        "missing_risk_conditions": ("exit", "conditions", "authorization", "veto"),
        "decision_conflict": tuple(_FIELDS),
    }
    result = {}
    for check, names in required.items():
        if not projection.get("source_complete") or not projection.get("sent_complete"):
            result[check] = "公共策略投影或完整来源未完整发送"
        elif any(fields.get(name, {}).get("status") not in {"source_keyword_evidence", "complete_source_no_keyword_evidence"} for name in names):
            result[check] = "相关策略字段含未知授权、私有依赖、混合删行或无法验证的条件"
        elif fields.get("action", {}).get("status") != "source_keyword_evidence":
            result[check] = "最终来源未明确行动方向，不能用默认动作替代"
    return result
