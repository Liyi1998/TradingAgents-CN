"""Bounded, dated valuation observations, not fair-value or return forecasts."""
from __future__ import annotations

from app.data.core.requirements import finite_number


def summarize_valuation_history(rows: list[dict], dates: list[str], sessions: int = 120) -> dict:
    """Use one field/source/basis and actual sessions; never count holiday rows."""
    dates = sorted(set(dates))[-sessions:]
    result = {"requested_sessions": sessions, "actual_trade_dates": dates,
              "window_start": dates[0] if dates else None,
              "window_end": dates[-1] if dates else None, "fields": {},
              "limitations": "仅为同口径实际交易日样本分位，不是合理估值、目标价、同业排名或收益预测。"}
    for field in ("pb", "pe_ttm"):
        groups: dict[tuple, dict] = {}
        conflicts: set[tuple] = set()
        for row in rows:
            day, value = str(row.get("trade_date", ""))[:10], row.get(field)
            proof = (row.get("field_provenance") or {}).get(field) or {}
            if (day not in dates or not finite_number(value) or proof.get("date_verified") is not True
                    or proof.get("as_of") != day or not proof.get("data_source") or not proof.get("method")
                    or not proof.get("basis") or (field == "pe_ttm" and proof["basis"] != "ttm")):
                continue
            key = (proof["data_source"], proof["basis"], proof["method"])
            values = groups.setdefault(key, {})
            if day in values and values[day] != value:
                conflicts.add((key, day))
            values[day] = value
        candidates = [(key, {day: val for day, val in values.items() if (key, day) not in conflicts})
                      for key, values in groups.items()]
        key, values = max(candidates, key=lambda item: (len(item[1]), item[0]), default=((None, None, None), {}))
        latest = values.get(dates[-1]) if dates else None
        missing = [day for day in dates if day not in values]
        complete = len(dates) == sessions and not missing
        positive = bool(values) and all(value > 0 for value in values.values())
        status = "available" if complete and positive else "not_applicable_nonpositive" if complete else "insufficient_history"
        result["fields"][field] = {
            "status": status, "observation_count": len(values), "required_count": sessions,
            "missing_dates": missing, "latest_value": latest, "data_source": key[0],
            "basis": key[1], "method": key[2],
            "sample_percentile_pct": (100 * sum(value <= latest for value in values.values()) / len(values)
                                      if complete and positive and latest is not None else None),
            "percentile_definition": f"count(value <= latest) / {sessions} actual sessions; includes latest",
            "reason": None if status == "available" else "nonpositive_values_are_not_valuation_cheapness" if complete else "requires_complete_same_basis_actual_sessions",
        }
    return result


def circulating_value_evidence(rows: list[dict], as_of: str) -> dict:
    """A dated public market-cap observation, never a holding/account amount."""
    candidates = []
    for row in rows:
        proof = (row.get("field_provenance") or {}).get("circ_mv") or {}
        value = row.get("circ_mv")
        if (row.get("trade_date") == as_of and finite_number(value) and value > 0
                and proof.get("as_of") == as_of and proof.get("date_verified") is True
                and proof.get("data_source") and proof.get("method")
                and proof.get("basis") not in {None, "", "unknown"}):
            candidates.append((value, proof))
    if not candidates or len({value for value, _ in candidates}) != 1:
        return {"status": "unavailable", "value": None, "amount_unit": "CNY", "as_of": as_of,
                "reason": "missing_or_conflicting_dated_circulating_market_value"}
    value, proof = candidates[0]
    return {"status": "available", "value": value, "amount_unit": "CNY", "as_of": as_of,
            "data_source": proof["data_source"], "method": proof["method"], "basis": proof["basis"],
            "limitations": "流通市值可用于公开金额比例；不能识别真实主力、逐笔大单、账户持仓或隐藏杠杆。"}
