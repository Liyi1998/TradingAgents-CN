"""Conservative extraction of executable facts, not financial ratios or scenarios."""
from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from typing import Any, Mapping

_DENIED = re.compile(r"否决|撤销|不批准|未获|拒绝|不采纳|不成立|不予|不得沿用|不采用")
_CONDITIONAL = re.compile(r"只有|未来|后续|条件.*满足|另获授权|再申请|重新审批|确认后|假设|算例|示范|才可")
_POSITION = re.compile(r"仓位|敞口|资产权重|总上限|(?:新增|首笔|试仓|建仓)(?:上限|额度)")
_ENTRY = re.compile(r"新增|首笔|试仓|建仓")
_UNKNOWN_TOTAL = re.compile(r"(?:总仓位|总持仓|存量仓位|现有总仓位|过渡期资产权重)[^。\n]{0,100}(?:暂不能|未知|未明确|不明确|未能量化|须按|暂无|未给出|未提供)")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_KEYS = ("total_position_limit_pct", "current_entry_limit_pct")
_FLAT = re.compile(r"未持仓|空仓|(?:账户|本账户)\s*NONE\b", re.I)
_HELD = re.compile(r"已有持仓|已持仓|已有仓位|存量(?:仓位|策略)|(?:账户|本账户)\s*HELD\b", re.I)
_CURRENT = re.compile(r"当前|本账户|本标的")
_CAP = re.compile(r"上限|不超过|不得超过|以内|降至|控制在|最多|最高|总资产|敞口|额度")
_TOTAL = re.compile(r"总仓(?:位)?|总持仓|存量仓位|现有总仓位|过渡期资产权重|总上限")
_CANCELLED = re.compile(r"取消|撤销|删除|作废|废止|否决|不采纳|不采用|不得沿用")
_REJECTED_TEMPLATE = re.compile(
    r"(?:并非|不是|不属于)[^。；]{0,24}(?:既有规则|已生效|有效额度|已授权)"
    r"|(?:模板|比例|额度|方案)[^。；]{0,16}(?:不生效|已取消|已撤销|不成立)"
)


def _rejected_future(fact: str, clause: str) -> bool:
    """A critique of a template is not a conditional authorization.

    Do not confuse 'not a current order' with withdrawal of a future plan.
    A comma-split percentage must retain the negation in its template critique.
    """
    return bool(_REJECTED_TEMPLATE.search(fact) or (
        re.search(r"质疑|模板|默认比例", clause) and _REJECTED_TEMPLATE.search(clause)
    ))


def _revoked_future_limits(source: str) -> set[float]:
    """Link explicit percentage withdrawals, not every percentage in a report."""
    revoked = set()
    for fact, clause in _position_fragments(source):
        if not _CANCELLED.search(clause):
            continue
        # A new plan after a comma is independent of a withdrawn old template.
        if _CONDITIONAL.search(fact) and not _CANCELLED.search(fact):
            continue
        if not (_POSITION.search(fact) or _ENTRY.search(fact)):
            continue
        for match in _PERCENT.finditer(fact):
            before, after = fact[:match.start()], fact[match.end():]
            if re.search(r"风险预算|计划风险|计划亏损|损失", before[-12:]) or re.match(r"\s*(?:计划)?(?:亏损|损失|风险)", after):
                continue
            revoked.add(float(match.group(1)))
    return revoked


def _declared_future_allowed(item: Mapping[str, Any], revoked: set[float]) -> bool:
    status = str(item.get("status") or "").strip()
    if re.fullmatch(r"(?:已)?(?:取消|撤销|删除|作废|否决)|cancelled|canceled|revoked|rejected", status, re.I):
        return False
    condition = item.get("condition", item.get("conditions", ""))
    text = "；".join(str(value) for value in condition) if isinstance(condition, list) else str(condition)
    if _rejected_future(text, text):
        return False
    limit = number(item.get("limit_pct"))
    return limit not in revoked


def _position_fragments(source: str):
    """Keep the fact cells separate from a table's other-account commentary."""
    for clause in clauses(source):
        if clause.startswith("|"):
            cells = [cell.strip() for cell in clause.strip("|").split("|")]
            yield " ".join(cells[:2]), clause
        else:
            # A nearby entry percentage must not relabel a subsequent total cap.
            for part in re.split(r"[，、]|,(?!\d{3}(?:\D|$))", clause):
                if part.strip():
                    yield part.strip(), clause


def _source_account_scope(source: str) -> str | None:
    plain = re.sub(r"[*_`#]", "", source)
    match = re.search(r"(?:本账户|账户(?:持仓)?状态)[^。\n；]{0,20}?(NONE\b|HELD\b|未持仓|空仓|已持仓)", plain, re.I)
    if match:
        return "none" if match[1].upper() in {"NONE", "未持仓", "空仓"} else "held"
    return None


def _position_scope(fact: str, source_account: str | None = None) -> str:
    if _CONDITIONAL.search(fact):
        return "future"
    if _HELD.search(fact):
        return "held"
    if _FLAT.search(fact):
        return "none"
    # A pronoun without an explicit actor is not evidence that the current
    # account is the one whose cap is unknown (e.g. a preceding HELD contrast).
    if re.match(r"其(?:当前)?|该(?:账户|分支|情景)", fact):
        return "unspecified"
    return (source_account or "current") if _CURRENT.search(fact) else "unspecified"


def _relevant_scopes(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    account = str(raw.get("account_position") or "UNKNOWN").upper()
    if account in {"NONE", "HELD"}:
        return ("current", "none" if account == "NONE" else "held", "unspecified")
    # Legacy total caps can describe the holding policy even while the default
    # display is WATCH. Do not turn that into an empty-account liquidation cap.
    if key == "total_position_limit_pct":
        return ("current", "held", "unspecified", "none")
    return ("current", "none", "unspecified")


def number(value: Any, lo: float = 0, hi: float = 100) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace("%", "").strip())
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and lo <= result <= hi else None


def clauses(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[。；;\n]+", re.sub(r"[*_\x60]", "", text or "")) if part.strip()]


def position_facts(raw: Mapping[str, Any], source: str) -> dict[str, Any]:
    """Extract only explicit current caps; future/exit plans remain conditional."""
    if not source:
        total = number(raw.get("total_position_limit_pct", raw.get("position_limit_pct")))
        return {
            "position_limit_pct": total, "total_position_limit_pct": total,
            "current_entry_limit_pct": number(raw.get("current_entry_limit_pct")),
            "conditional_strategy": list(raw.get("conditional_strategy") or []),
            "position_evidence": dict(raw.get("position_evidence") or {}),
        }
    candidates: dict[str, dict[str, list[tuple[float, str]]]] = {key: {} for key in _KEYS}
    unknown_scopes: dict[str, set[str]] = {key: set() for key in _KEYS}
    declared = raw.get("conditional_strategy")
    revoked = _revoked_future_limits(source)
    future = deepcopy([item for item in declared if isinstance(item, Mapping)
                       and _declared_future_allowed(item, revoked)]) if isinstance(declared, list) else []
    source_account = _source_account_scope(source)
    for fact, clause in _position_fragments(source):
        if not _POSITION.search(fact):
            continue
        scope = _position_scope(fact, source_account)
        if _UNKNOWN_TOTAL.search(fact):
            unknown_scopes["total_position_limit_pct"].add(scope)
        # An explicit zero is a prohibition, not an approved nonzero entry.
        if (_DENIED.search(fact) or _CANCELLED.search(fact)) and not re.search(r"(?<![\d.])0\s*%", fact):
            continue
        # A debt ratio, risk budget or share-of-initial-holdings is not NAV exposure.
        if re.search(r"负债率|股数|初始股|静态仓位算例|风险预算", fact) and not re.search(r"仓位上限|剩余仓位上限|总上限", fact):
            continue
        values = list(_PERCENT.finditer(fact))
        if not values:
            continue
        if _rejected_future(fact, clause):
            continue
        if scope == "future" or re.search(r"最终资产权重|D[1-9].*(?:为0|退出)", fact):
            if float(values[0].group(1)) in revoked or _CANCELLED.search(fact):
                continue
            future.append({"kind": "conditional", "limit_pct": float(values[0].group(1)), "condition": clause})
            continue
        if _ENTRY.search(fact) and raw.get("unheld_action") == "WATCH" and float(values[0].group(1)) > 0:
            if float(values[0].group(1)) in revoked or _CANCELLED.search(fact):
                continue
            future.append({"kind": "conditional", "limit_pct": float(values[0].group(1)), "condition": "非当前授权；" + clause})
            continue
        for match in values:
            before = fact[max(0, match.start() - 30):match.start()]
            after = fact[match.end():match.end() + 20]
            if re.search(r"风险预算|计划风险|损失|静态止损", before) or re.search(r"^[^\d%]{0,12}(?:风险预算|计划风险|静态止损)", after):
                continue
            vicinity = fact[max(0, match.start() - 45):match.end() + 10]
            if not _POSITION.search(vicinity) or not _CAP.search(vicinity):
                continue
            keys = ["current_entry_limit_pct"] if _ENTRY.search(before) else ["total_position_limit_pct"]
            if _ENTRY.search(before) and _TOTAL.search(before):
                keys.append("total_position_limit_pct")
            value = number(match.group(1))
            if value is not None:
                for key in keys:
                    candidates[key].setdefault(scope, []).append((value, clause))
    evidence: dict[str, Any] = {}
    result: dict[str, Any] = {}
    for key in _KEYS:
        scopes = _relevant_scopes(raw, key)
        # Specific current/actor facts outrank an unspecified narrative note.
        specific = scopes[:-1] if scopes[-1] == "unspecified" else scopes[:2]
        selected = [scope for scope in specific if candidates[key].get(scope) or scope in unknown_scopes[key]]
        if str(raw.get("account_position") or "UNKNOWN").upper() not in {"NONE", "HELD"}:
            # With no selected account, a labeled CURRENT policy is not the
            # same actor as a contrasting HELD policy. Preserve legacy fallback
            # order, but never merge their caps/unknowns into one current fact.
            selected = selected[:1]
        if not selected:
            selected = [scope for scope in scopes if candidates[key].get(scope) or scope in unknown_scopes[key]][:1]
        items = [item for scope in selected for item in candidates[key].get(scope, [])]
        values = {value for value, _ in items}
        value = next(iter(values)) if len(values) == 1 else None
        if any(scope in unknown_scopes[key] for scope in selected):
            value = None
        result[key] = value
        evidence[key] = {
            "status": "explicit" if value is not None else "unknown",
            "clauses": list(dict.fromkeys(text for _, text in items)),
            "scopes": selected,
            "unknown_scopes": sorted(unknown_scopes[key]),
        }
    unique_future = {json.dumps(item, sort_keys=True, ensure_ascii=False, default=str): item for item in future}
    result.update(position_limit_pct=result["total_position_limit_pct"], conditional_strategy=list(unique_future.values()), position_evidence=evidence)
    return result


def validate_numeric_facts(raw: Mapping[str, Any], source: str) -> dict[str, Any]:
    """Never substitute defaults for measured confidence, risk or target prices."""
    limits = {"confidence": 1, "risk_score": 1, "target_price": float("inf")}
    labels = {"confidence": r"置信度|信心", "risk_score": r"风险评分|风险分数", "target_price": r"目标价|目标价格"}
    result: dict[str, Any] = {}
    statuses: dict[str, str] = {}
    for key, hi in limits.items():
        value = number(raw.get(key), 0, hi)
        supported = not source
        for clause in clauses(source):
            if not re.search(labels[key], clause):
                continue
            if re.search(r"未给出|未明确|不发布|不设置|非目标|不是目标|默认|假设|参考价格|历史收盘|阻力参考", clause):
                continue
            if _DENIED.search(clause):
                continue
            for token in re.finditer(r"(\d+(?:\.\d+)?)\s*(%?)", clause):
                candidate = float(token.group(1))
                if key != "target_price" and token.group(2):
                    candidate /= 100
                if value is not None and abs(candidate - value) < 1e-7:
                    supported = True
        result[key] = value if supported else None
        statuses[key] = "reported" if supported and value is not None else "unknown"
    result["numeric_evidence"] = statuses
    result["decision_source_sha256"] = hashlib.sha256(source.encode()).hexdigest() if source else raw.get("decision_source_sha256")
    return result
