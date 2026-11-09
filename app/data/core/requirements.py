"""Declarative, analysis-time contracts; never infer evidence from role names.

The cache is an optimization, not the success criterion. These contracts also
travel to the fallback router, so a partial HTTP success cannot stop fallback.
"""
from __future__ import annotations

import math
from numbers import Real


def finite_number(value) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def margin_units_verified(row: dict) -> bool:
    """Evidence must survive provider → adapter → storage → consumer intact."""
    evidence = row.get("unit_evidence")
    contract = {
        "SSE": ("official_response_date_exact", "official_margin_detail_column_contract", "https://www.sse.com.cn/market/othersdata/margin/detail/index.shtml"),
        "SZSE": ("dated_json_and_xlsx_truncated_display_crosscheck", "dated_xlsx_explicit_yuan_headers", "https://www.szse.cn/disclosure/margin/margin/index.html"),
    }.get(row.get("exchange"))
    source = row.get("data_source")
    if source == "tushare" and row.get("exchange") in {"SSE", "SZSE"}:
        contract = ("source_row_date", "tushare_margin_detail_yuan_column_contract", "https://tushare.pro/document/2?doc_id=59")
    return bool(
        contract and source in {"akshare", "tushare"} and row.get("amount_unit") == "CNY"
        and row.get("monetary_unit") == "元"
        and row.get("trade_date") and row.get("source_response_date") == row.get("trade_date")
        and row.get("date_evidence_status") == contract[0]
        and isinstance(evidence, dict) and evidence.get("amount_unit") == "CNY"
        and evidence.get("native_amount_unit") == "元" and evidence.get("exchange") == row.get("exchange")
        and evidence.get("method") == contract[1] and evidence.get("source_url") == contract[2]
    )


def analysis_filters(market: str, domain: str, symbol: str | None,
                     start: str | None, end: str | None, filters: dict | None,
                     context: dict | None) -> dict:
    """Requirements apply only to an explicit single-stock analysis context."""
    result = dict(filters or {})
    if (market != "CN" or not symbol or not str(symbol).isdigit()
            or not context or context.get("market") != market):
        return result
    days = [day for day in context.get("recent_trade_dates", [])
            if (not start or day >= start[:10]) and (not end or day <= end[:10])]
    if domain == "money_flow":
        result.setdefault("required_fields", ["main_net_inflow"])
        result.setdefault("expected_dates", days[-5:])
        result.setdefault("contract", "verified_same_basis_flow")
        result["calendar_verified"] = context.get("calendar_status") == "success" and len(days) >= 5
    elif domain == "daily_quotes":
        result.setdefault("required_fields", ["open", "high", "low", "close", "volume", "amount"])
        result.setdefault("expected_dates", days[-5:])
        result.setdefault("contract", "verified_raw_daily_quotes")
        result["calendar_verified"] = context.get("calendar_status") == "success" and bool(days)
    elif domain == "daily_indicators":
        result.setdefault("required_fields", ["pe_ttm", "pb"])
        result.setdefault("expected_dates", days[-1:])
        result.setdefault("contract", "verified_dated_indicators")
        result["calendar_verified"] = context.get("calendar_status") == "success" and bool(days)
    elif domain == "margin_trading":
        result.setdefault("required_fields", ["margin_balance", "margin_buy_amount", "margin_repay_amount"])
        result.setdefault("expected_dates", days[-5:])
        result.setdefault("contract", "verified_exchange_margin")
        result["calendar_verified"] = context.get("calendar_status") == "success" and bool(days)
    return result


def verify_rows(rows: list[dict], domain: str, filters: dict) -> tuple[list[dict], list[str]]:
    """Quarantine unproved values, select one flow definition, retain diagnostics."""
    contract = filters.get("contract")
    if not contract:
        return rows, []
    issues = []
    if not filters.get("calendar_verified", True):
        issues.append("actual_session_calendar_unverified")
    expected = set(filters.get("expected_dates") or [])
    fields = filters.get("required_fields") or []
    checked = [row for row in rows if not expected or str(row.get("trade_date", ""))[:10] in expected]
    for row in checked:
        for key in fields:
            if not finite_number(row.get(key)):
                row[key] = None
    if contract == "verified_spot_quote":
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from app.data.query.price_reference import verified_spot
        valid = [row for row in rows if verified_spot(row, filters.get("quote_symbol", ""),
                 filters.get("quote_as_of", ""), datetime.now(ZoneInfo("Asia/Shanghai")))]
        if len(valid) != len(rows) or not valid:
            issues.append("quote_price_source_or_observation_time_unverified")
        return valid, issues
    elif contract == "verified_same_basis_flow":
        definitions = {("akshare", "eastmoney_order_size_v1"), ("sina", "sina_large_order_v1")}
        groups: dict[tuple, list] = {}
        for row in rows:
            key = (row.get("data_source"), row.get("flow_basis"))
            if (key not in definitions or row.get("amount_unit") != "CNY"
                    or row.get("flow_date_verified") is not True
                    or row.get("flow_date_basis") != "source_row_date"):
                continue
            groups.setdefault(key, []).append(row)
        candidates = []
        for key, group in groups.items():
            per_day: dict[str, dict] = {}
            conflicting = set()
            for row in group:
                day = str(row.get("trade_date", ""))[:10]
                if day in per_day and any(row.get(field) != per_day[day].get(field) for field in fields):
                    conflicting.add(day)
                per_day[day] = row
            usable = [row for day, row in per_day.items() if day not in conflicting]
            covered = {str(row.get("trade_date", ""))[:10] for row in usable
                       if all(finite_number(row.get(field)) for field in fields)}
            candidates.append((len(expected & covered) if expected else len(covered), key, usable))
        if not candidates:
            issues.append("flow_date_units_or_definition_unverified")
            return [], issues
        # No cross-source addition: one complete definition, otherwise one best
        # incomplete definition. Eastmoney wins only a coverage tie.
        _, _, rows = max(candidates, key=lambda item: (item[0], item[1][0] == "akshare"))
    elif contract == "verified_dated_indicators":
        for row in checked:
            for key in fields:
                evidence = (row.get("field_provenance") or {}).get(key) or {}
                if (evidence.get("date_verified") is not True
                        or str(evidence.get("as_of", ""))[:10] != str(row.get("trade_date", ""))[:10]
                        or not evidence.get("data_source") or not evidence.get("method")
                        or (key == "pe_ttm" and evidence.get("basis") != "ttm")):
                    row[key] = None
    elif contract == "verified_raw_daily_quotes":
        from app.data.storage.mongo.repositories.daily_quotes_repo import trusted_raw_quote
        for row in checked:
            if not trusted_raw_quote(row):
                for key in fields:
                    row[key] = None
    elif contract == "verified_exchange_margin":
        amount_fields = ("margin_balance", "short_balance", "margin_buy_amount", "margin_repay_amount",
                         "total_balance", "margin_repay_amount_derived")
        # The output may include older observations outside the five required
        # sessions; those values also need their own date/unit proof.
        for row in rows:
            verified = margin_units_verified(row)
            row["margin_amount_evidence_status"] = "verified" if verified else "unverified"
            if not verified:
                issues.append("margin_amount_units_or_response_date_unverified")
                row["unverified_amounts"] = {key: row.get(key) for key in amount_fields if row.get(key) is not None}
                for key in amount_fields:
                    row[key] = None
                row["amount_unit"] = None
            for key in amount_fields:
                value = row.get(key)
                if value is not None and (not finite_number(value) or value < 0):
                    issues.append("invalid_margin_amount_unverified")
                    row.setdefault("unverified_amounts", {})[key] = row[key]
                    row[key] = None
    else:
        issues.append("unknown_requirement_contract")
    return rows, list(dict.fromkeys(issues))


def relative_volume_5d(rows: list[dict], expected_dates: list[str]) -> dict:
    """Current daily volume / preceding five sessions, NOT vendor volume_ratio."""
    from app.data.storage.mongo.repositories.daily_quotes_repo import trusted_raw_quote
    result = {"status": "unavailable", "name": "relative_volume_5d", "value": None,
              "definition": "current_daily_share_volume / mean(previous_5_actual_sessions)",
              "is_vendor_volume_ratio": False, "trade_dates": list(expected_dates),
              "reason": "requires_six_verified_actual_sessions"}
    if len(expected_dates) != 6 or len(set(expected_dates)) != 6:
        return result
    groups: dict[str, dict] = {}
    conflicts = set()
    for row in rows:
        day = str(row.get("trade_date", ""))[:10]
        if day not in expected_dates or not trusted_raw_quote(row) or not finite_number(row.get("volume")):
            continue
        source = str(row.get("data_source"))
        group = groups.setdefault(source, {})
        if day in group and row["volume"] != group[day]:
            conflicts.add((source, day))
        group[day] = row["volume"]
    for source, group in groups.items():
        if any(day not in group or (source, day) in conflicts for day in expected_dates):
            continue
        mean = sum(group[day] for day in expected_dates[:-1]) / 5
        if mean <= 0 or group[expected_dates[-1]] < 0:
            continue
        return {**result, "status": "available", "value": group[expected_dates[-1]] / mean,
                "data_source": source, "volume_unit": "share", "reason": None}
    return result


def five_session_flow(rows: list[dict], quality: dict, expected_dates: list[str]) -> dict:
    """A five-session sum exists only for one verified, complete definition."""
    result = {"status": "unavailable", "five_session_net_flow": None, "amount_unit": "CNY",
              "trade_dates": list(expected_dates), "flow_basis": None,
              "reason": "incomplete_or_unverified_five_session_window"}
    if quality.get("status") != "success" or len(expected_dates) != 5:
        return result
    window = {str(row.get("trade_date", ""))[:10]: row for row in rows
              if str(row.get("trade_date", ""))[:10] in expected_dates}
    definitions = {(row.get("data_source"), row.get("flow_basis")) for row in window.values()}
    if (set(window) != set(expected_dates) or len(definitions) != 1
            or any(not finite_number(row.get("main_net_inflow")) for row in window.values())):
        return result
    source, basis = next(iter(definitions))
    return {**result, "status": "available", "five_session_net_flow": sum(row["main_net_inflow"] for row in window.values()),
            "data_source": source, "flow_basis": basis, "reason": None,
            "is_eastmoney_main_flow": basis == "eastmoney_order_size_v1"}
