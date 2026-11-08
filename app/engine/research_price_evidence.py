"""Read-only target provenance and narrow historical OHLC claim checks."""
import re

from app.engine.report_chart_levels import plain


def historical_price_claims(content, bars):
    """Check explicit dated price fields, not narrative guesses or projections.

    No nearest-date substitution: a real price on another day is still not
    evidence for the claimed day. Unrecognised weekly derivations stay unknown.
    """
    by_date = {b.get("date") or b.get("trade_date"): b for b in bars
               if not b.get("status") and b.get("price_basis", "raw") == "raw"}
    result, seen = [], set()
    for line_no, raw in enumerate(content.splitlines(), 1):
        line = plain(raw)
        if not re.search(r"目标|阻力", line) or not re.search(r"\d{4}-\d{2}-\d{2}", line):
            continue
        match = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*元[（(]?", line)
        if not match or re.search(r"\d\s*[-–~至]\s*\d", line[:match.end()]):
            continue
        tail = line[match.end():]
        dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", tail))
        if len(dates) != 1:
            continue
        day = dates.pop()
        reference = tail[tail.index(day)+len(day):]
        field = "close" if re.search(r"收盘(?:价|高点)", reference) else "high" if re.search(r"最高|(?:年度|历史|阶段|前期|盘中|波段).*高点|上影线顶点|峰值", reference) else None
        if not field:
            continue
        value = float(match[1])
        identity = (day, field, value)
        if identity in seen:
            continue
        seen.add(identity)
        bar = by_date.get(day, {})
        actual = bar.get(field)
        status = "missing_data" if type(actual) not in (int, float) else "matched" if abs(value-actual) < .011 else "mismatch"
        result.append({"source_line": line_no, "source_quote": raw, "price": value,
                       "date": day, "field": field, "actual": actual, "status": status,
                       "data_source": bar.get("data_source"), "evidence_id": bar.get("evidence_id")})
    return result


def with_price_conflicts(levels, content, bars):
    conflicts = [r["price"] for r in historical_price_claims(content, bars) if r["status"] == "mismatch"]
    return [{**level, "historical_claim_conflict": any(level["lower"] <= value <= level["upper"] for value in conflicts)}
            for level in levels]


def target_comparison(current, previous, *, current_date, previous_date, known_at, content, bars):
    old_prices = {(p["lower"], p["upper"]) for p in previous if p.get("kind") in {"target", "resistance"}}
    findings = historical_price_claims(content, bars)
    current_targets = [p for p in current if p.get("kind") in {"target", "resistance"}]
    rows = []
    for point in current_targets:
        related = [f for f in findings if point["lower"] <= f["price"] <= point["upper"]]
        rows.append({"label": point["label"], "lower": point["lower"], "upper": point["upper"],
                     "condition": point.get("condition", ""), "source_quote": point.get("source_quote", ""),
                     "change": "retained" if (point["lower"], point["upper"]) in old_prices else "added" if previous else "baseline",
                     "source_date": current_date, "known_at": str(known_at) if known_at else None,
                     "evidence": related,
                     "status": "historical_claim_conflict" if any(f["status"] == "mismatch" for f in related) else "conditional_reference"})
    return {"source_date": current_date, "previous_date": previous_date, "known_at": str(known_at) if known_at else None,
            "targets": rows, "previous_targets": [p for p in previous if p.get("kind") in {"target", "resistance"}],
            "notice": "新增目标不等于已经突破原目标。价格来自原文；历史价格核对仅验证列出的日期与字段，不证明目标会到达。报告生成前的盘中行情不会获得事后新增目标的提醒。"}
