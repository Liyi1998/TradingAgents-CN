"""Field/time/source coverage independent of wall-clock cache freshness."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

DATE_FIELDS = {
    "daily_quotes": "trade_date", "daily_indicators": "trade_date",
    "intraday_quotes": "datetime", "financial_data": "report_period",
    "news": "publish_time", "money_flow": "trade_date",
    "margin_trading": "trade_date", "dragon_tiger": "trade_date",
    "block_trade": "trade_date", "market_quotes": "trade_date",
}
REQUIRED = {
    "basic_info": ("name", "industry"),
    "daily_quotes": ("open", "high", "low", "close", "volume", "amount"),
    "intraday_quotes": ("datetime", "open", "high", "low", "close", "volume", "amount", "freq"),
    "financial_data": ("revenue", "net_profit", "total_assets", "total_equity", "operating_cashflow"),
    "money_flow": ("main_net_inflow",),
    "margin_trading": ("margin_balance", "margin_buy_amount", "margin_repay_amount"),
}


def present(value: Any) -> bool:
    return value is not None and str(value).strip().lower() not in {"", "nan", "none", "n/a", "unknown"}


def evaluate(data: Any, domain: str, start: str | None, end: str | None, filters: dict | None = None) -> tuple[Any, dict]:
    filters = filters or {}
    rows = deepcopy(data if isinstance(data, list) else [data] if isinstance(data, dict) else [])
    date_field = DATE_FIELDS.get(domain)
    future = 0
    retained = []
    for row in rows:
        cutoff = str(row.get(date_field) or "")[:10] if date_field else ""
        disclosed = str(row.get("announce_date") or "")[:10]
        if end and ((cutoff and cutoff > end[:10]) or (domain == "financial_data" and disclosed and disclosed > end[:10])):
            future += 1
            continue
        if start and cutoff and cutoff < start[:10]:
            continue
        if domain == "basic_info":
            from app.data.processor.industry_contract import industry_label, industry_verified
            if not industry_label(row.get("industry")):
                row["industry"] = None
                row["industry_status"] = "invalid_security_type_not_industry"
            elif row.get("market") == "CN" and not industry_verified(row, end):
                row["unverified_industry"] = row["industry"]
                row["industry"] = None
                row["industry_status"] = "unverified_classification_or_date"
            else:
                row["industry_status"] = "verified" if row.get("market") == "CN" else "reported"
        if filters.get("asset_type") and row.get("asset_type") != filters["asset_type"]:
            continue
        if filters.get("exchange") and row.get("exchange") not in {filters["exchange"], {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(filters["exchange"])}:
            continue
        if domain == "financial_data" and row.get("market") == "CN" and present(row.get("net_profit")) and not present(row.get("net_profit_basis")):
            row["unverified_net_profit"] = row["net_profit"]
            row["net_profit"] = None
            row["net_profit_basis"] = "unknown_legacy_alias"
        retained.append(row)
    from app.data.core.requirements import verify_rows
    retained, contract_issues = verify_rows(retained, domain, filters)
    fields = filters.get("required_fields", REQUIRED.get(domain, ()))
    expected = set(filters.get("expected_dates") or [])
    checked = [row for row in retained if not expected or str(row.get(date_field) or "")[:10] in expected]
    missing = [key for key in fields if not any(present(row.get(key)) for row in checked)]
    missing_by_date = {}
    for period in {str(row.get(date_field) or "")[:10] for row in checked if date_field}:
        peers = [row for row in checked if str(row.get(date_field) or "")[:10] == period]
        holes = [key for key in fields if not any(present(row.get(key)) for row in peers)]
        if holes:
            missing_by_date[period] = holes
    dates = sorted({str(row.get(date_field) or "")[:10] for row in retained if date_field and row.get(date_field)})
    missing_dates = sorted(set(filters.get("expected_dates") or []) - set(dates))
    unit_conflicts = []
    field_conflicts = []
    if domain == "financial_data":
        for period in dates:
            peers = [row for row in retained if str(row.get("report_period") or "")[:10] == period]
            for key in fields:
                values = [(float(row[key]), row.get("data_source")) for row in peers if isinstance(row.get(key), (int, float))]
                if len({source for _, source in values}) > 1:
                    low, high = min(value for value, _ in values), max(value for value, _ in values)
                    if abs(high - low) > max(abs(low), abs(high), 1) * .02:
                        field_conflicts.append({"period": period, "field": key, "values": values})
    if domain in {"daily_quotes", "intraday_quotes", "market_quotes"}:
        for row in retained:
            if row.get("asset_type") == "index":
                row["unit_status"] = "vwap_check_not_applicable_to_index_points"
                continue
            if (row.get("price_basis") != "raw" or row.get("volume_unit") != "share"
                    or row.get("amount_unit") != "CNY"):
                # Adjusted OHLC and raw turnover are not comparable; unknown
                # units are insufficient evidence for erasing source amounts.
                row["unit_status"] = "vwap_check_unverified_price_basis_or_units"
                continue
            try:
                volume, amount = float(row.get("volume") or 0), float(row.get("amount") or 0)
                low, high = float(row.get("low") or 0), float(row.get("high") or 0)
                if volume > 0 and amount > 0 and low > 0 and high >= low:
                    implied = amount / volume
                    if not low * .98 <= implied <= high * 1.02:
                        unit_conflicts.append({"date": row.get(date_field), "source": row.get("data_source"), "implied_vwap": implied})
                        row["volume"], row["amount"] = None, None
                        row["unit_status"] = "conflict_quarantined_not_corrected"
            except (TypeError, ValueError):
                pass
    financial_missing = {}
    if domain == "financial_data" and dates and end:
        year = int(end[:4])
        latest = dates[-1]
        fiscal_years = [int(day[:4]) for day in dates if day.endswith("-12-31")]
        latest_fiscal = max(fiscal_years) if fiscal_years else year - 1
        required_periods = {f"{latest_fiscal - offset}-12-31" for offset in range(4)}
        if filters.get("full_financial_window") and start:
            required_periods.update(f"{fiscal}-12-31" for fiscal in range(int(start[:4]), latest_fiscal + 1)
                                    if start[:10] <= f"{fiscal}-12-31" <= end[:10])
        required_periods.add(latest)
        required_periods.add(f"{int(latest[:4]) - 1}{latest[4:]}")
        for period in sorted(required_periods):
            same = [row for row in retained if str(row.get("report_period"))[:10] == period]
            holes = [key for key in fields if not any(present(row.get(key)) for row in same)]
            if holes:
                financial_missing[period] = holes
    unknown_disclosure = domain == "financial_data" and any(not present(row.get("announce_date")) for row in retained)
    quality = {
        "status": "partial" if retained and (missing or missing_by_date or missing_dates or unknown_disclosure) else "success" if retained else "failed",
        "reason": "missing_fields_or_dates" if missing or missing_by_date or missing_dates else "disclosure_date_unknown" if unknown_disclosure else "no_verified_records" if not retained else None,
        "requested_range": {"start": start, "end": end},
        "data_cutoff": dates[-1] if dates else None, "missing_fields": missing,
        "missing_dates": missing_dates, "missing_fields_by_date": missing_by_date,
        "future_rows_excluded": future,
        "disclosure_date_unknown": unknown_disclosure,
        "financial_period_holes": financial_missing,
        "sources": sorted({str(row.get("data_source") or "unknown") for row in retained}),
        "fetched_at": max((str(row.get("updated_at")) for row in retained if row.get("updated_at")), default=None),
        "read_at": datetime.now(timezone.utc).isoformat(),
        "units": sorted({(str(row.get("volume_unit") or "unknown"), str(row.get("amount_unit") or "unknown")) for row in retained}),
        "unit_conflicts": unit_conflicts, "field_conflicts": field_conflicts,
        "requirement_contract": filters.get("contract"), "contract_issues": contract_issues,
        "profit_basis_unknown": any(row.get("net_profit_basis") == "unknown_legacy_alias" for row in retained),
    }
    if financial_missing and retained:
        quality.update(status="partial", reason="missing_financial_history")
    if domain == "financial_data" and filters.get("full_financial_window"):
        quality.update(observed_financial_periods=dates, observed_period_count=len(dates),
                       observed_history_start=dates[0] if dates else None,
                       history_note="仅列实际取得且通过披露校验的报告期；文档条数不等于季度数，也不证明完整多年覆盖。")
    if field_conflicts:
        quality.update(status="conflict", reason="financial_sources_disagree_do_not_merge")
    if unit_conflicts:
        quality.update(status="conflict", reason="amount_volume_price_inconsistent")
    if contract_issues:
        quality.update(status="partial" if retained else "failed", reason="requirement_contract_unverified")
    return (retained if isinstance(data, list) else retained[0] if retained else None), quality
