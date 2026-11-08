"""Conservative, explicitly derived trailing cash yield from public Sina tables.

This is NOT a vendor's annual/TTM dividend indicator. The numerator is cash per
share with implemented ex-dates in (as_of - 1 calendar year, as_of]. We decline
to rebase dividends across bonus/rights events rather than infer share ratios.
"""

import asyncio
from dataclasses import dataclass
from datetime import date
from io import StringIO
import math
import re
from typing import Dict, List, Optional

import pandas as pd


_DIVIDEND_COLUMNS = (
    ("分红", "公告日期", "公告日期"),
    ("分红", "分红方案(每10股)", "送股(股)"),
    ("分红", "分红方案(每10股)", "转增(股)"),
    ("分红", "分红方案(每10股)", "派息(税前)(元)"),
    ("分红", "进度", "进度"),
    ("分红", "除权除息日", "除权除息日"),
)
_RIGHTS_COLUMNS = (
    ("配股", "公告日期"),
    ("配股", "配股方案(每10股配股股数)"),
    ("配股", "除权日"),
)


@dataclass(frozen=True)
class CashDividendHistory:
    dividends: List[Dict]
    rights: List[Dict]
    source: str = "sina_share_bonus_full_history"
    schema_verified: bool = False


def _source_date(value) -> Optional[date]:
    text = str(value).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _source_number(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def parse_cash_dividend_tables(tables: List[pd.DataFrame]) -> CashDividendHistory:
    """Match verified labels (including per-ten/pre-tax units), never positions."""
    dividends = [frame for frame in tables if set(_DIVIDEND_COLUMNS).issubset(frame.columns)]
    rights = [frame for frame in tables if set(_RIGHTS_COLUMNS).issubset(frame.columns)]
    if len(dividends) != 1 or len(rights) != 1:
        raise ValueError("Dividend/rights full-history table schema or units are unverified")

    def rows(frame, columns, names):
        if len(frame) == 1 and str(frame.iloc[0, 0]).strip() == "暂时没有数据！":
            return []
        return [dict(zip(names, values)) for values in frame[list(columns)].itertuples(index=False, name=None)]

    return CashDividendHistory(
        dividends=rows(dividends[0], _DIVIDEND_COLUMNS,
                       ("announce_date", "bonus_per_ten", "transfer_per_ten", "cash_per_ten", "status", "ex_date")),
        rights=rows(rights[0], _RIGHTS_COLUMNS, ("announce_date", "rights_per_ten", "ex_date")),
        schema_verified=True,
    )


async def fetch_cash_dividend_history(symbol: str) -> CashDividendHistory:
    """One bounded free GET; no detail requests, retries, or database side effects."""
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("A six-digit CN symbol is required")

    def fetch():
        import requests

        from app.data.sources.cn.akshare.api.anti_scraping import wait_rate_limit

        wait_rate_limit()
        response = requests.get(
            f"https://vip.stock.finance.sina.com.cn/corp/go.php/vISSUE_ShareBonus/stockid/{symbol}.phtml",
            timeout=(5, 20),
        )
        response.raise_for_status()
        return parse_cash_dividend_tables(pd.read_html(StringIO(response.text)))

    return await asyncio.to_thread(fetch)


def derive_cash_dividend_yield(history: CashDividendHistory, *, as_of: str,
                               close: float, close_date: str, close_basis: str,
                               close_source: str = "eastmoney_daily_history") -> Dict:
    """Return missing plus a reason unless the entire calculation is evidenced."""
    day = _source_date(as_of)
    result = {"value": None, "status": "unavailable", "reason": "unverified_price"}
    price = _source_number(close)
    if (day is None or _source_date(close_date) != day or close_basis != "unadjusted" or not price
            or close_source not in {"eastmoney_daily_history", "sina_daily_history"}):
        return result
    if not history.schema_verified:
        return {**result, "reason": "unverified_history_schema"}
    # Feb 29 rolls back to Feb 28; this is a calendar-year window, not 365 rows.
    try:
        cutoff = day.replace(year=day.year - 1)
    except ValueError:
        if day.year <= 1:
            return {**result, "reason": "invalid_window"}
        cutoff = day.replace(year=day.year - 1, day=28)
    seen = {}
    boundary = False
    for row in history.dividends:
        ex_date = _source_date(row.get("ex_date"))
        status = str(row.get("status", "")).strip()
        if status != "实施":
            # Only explicit non-implemented proposals may be excluded safely.
            if status not in {"预案", "股东大会预案", "董事会预案", "股东大会通过", "取消", "不分配"}:
                return {**result, "reason": "unknown_implementation_status"}
            continue
        if ex_date is None:
            return {**result, "reason": "undated_implemented_event"}
        if ex_date <= cutoff:
            boundary = True
            continue
        if ex_date > day:
            continue
        announced = _source_date(row.get("announce_date"))
        cash = _source_number(row.get("cash_per_ten"))
        bonus = _source_number(row.get("bonus_per_ten"))
        transfer = _source_number(row.get("transfer_per_ten"))
        if announced is None or announced > ex_date or cash is None or bonus is None or transfer is None:
            return {**result, "reason": "incomplete_implemented_event"}
        if bonus or transfer:
            return {**result, "reason": "share_basis_changed"}
        event = (cash, bonus, transfer, announced)
        if ex_date in seen and seen[ex_date] != event:
            return {**result, "reason": "conflicting_same_day_event"}
        seen[ex_date] = event
    if not boundary:
        return {**result, "reason": "history_does_not_cover_window"}
    for row in history.rights:
        ex_date = _source_date(row.get("ex_date"))
        if ex_date is None:
            # An unimplemented/undated rights offer cannot prove a stable basis.
            return {**result, "reason": "undated_rights_event"}
        if cutoff < ex_date <= day:
            return {**result, "reason": "share_basis_changed"}

    cash_per_share = sum(event[0] / 10.0 for event in seen.values())
    value = cash_per_share / price * 100.0
    if not math.isfinite(value):
        return {**result, "reason": "nonfinite_yield"}
    evidence = {
        "data_source": "akshare", "event_source": history.source,
        "method": "derived_cash_dividend_ex_date_12m", "basis": "ttm",
        "date_verified": True, "as_of": day.isoformat(),
        "window_start_exclusive": cutoff.isoformat(), "window_end_inclusive": day.isoformat(),
        "cash_per_share": cash_per_share, "cash_currency": "CNY", "cash_basis": "pre_tax",
        "event_count": len(seen), "event_ex_dates": [item.isoformat() for item in sorted(seen)],
        "denominator_close": price, "denominator_date": close_date,
        "denominator_basis": close_basis, "denominator_source": close_source,
        "coverage_method": "full_history_tables_with_prior_event_boundary",
        "share_basis": "no_bonus_transfer_or_rights_in_window",
    }
    return {"value": value, "status": "available", "reason": None, "provenance": evidence}
