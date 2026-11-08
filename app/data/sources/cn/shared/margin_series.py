"""Bounded free exchange margin detail, shared full-market day snapshots."""
from __future__ import annotations

import asyncio
import math
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN

import pandas as pd

from app.data.storage.cache.memory_cache import TTLCache
from app.data.storage.mongo.repositories.trade_calendar_repo import TradeCalendarRepo

_CACHE = TTLCache(default_ttl=1800)
_LOCKS = {}


def validate_szse_json_amount_units(metadata: dict) -> None:
    """The JSON comparison scale is accepted only from explicit unit labels."""
    columns = metadata.get("cols") or {}
    for key, name in (("jrrzmr", "融资买入额"), ("jrrzye", "融资余额")):
        label = re.sub(r"\s+", "", re.sub(r"<[^>]*>", "", str(columns.get(key, ""))))
        if label not in {f"{name}(亿元)", f"{name}（亿元）"}:
            raise ValueError("SZSE dated JSON monetary units unverified")


def validate_szse_display_amount(raw_yuan, display_yi_yuan) -> None:
    """SZSE JSON truncates to two decimals in 亿元; XLSX retains raw yuan.

    Validate that the exact export amount belongs to that display interval, not
    a guessed percentage tolerance. This does not alter the exported amount.
    """
    displayed = str(display_yi_yuan).replace(",", "").strip()
    if not re.fullmatch(r"\d+\.\d{2}", displayed):
        raise ValueError("SZSE JSON amount display precision unverified")
    try:
        raw = Decimal(str(raw_yuan).replace(",", "").strip())
        expected = Decimal(displayed)
        valid = raw.is_finite() and raw >= 0 and (raw / Decimal("100000000")).quantize(Decimal("0.01"), rounding=ROUND_DOWN) == expected
    except InvalidOperation:
        valid = False
    if not valid:
        raise ValueError("SZSE XLSX/dated JSON amount mismatch")


def derive_margin_repayments(frame: pd.DataFrame, open_dates: list[str]) -> pd.DataFrame:
    """Independent, labelled balance-identity estimate; never fill reported fields."""
    result = frame.copy()
    result["margin_repay_amount_derived"] = None
    result["margin_repay_derivation_status"] = "insufficient_inputs"
    result["margin_repay_derivation"] = None
    previous = {day: open_dates[index - 1] for index, day in enumerate(open_dates) if index}
    for index, row in result.iterrows():
        day = row.get("trade_date")
        prior_day = previous.get(day)
        code = row.get("证券代码", row.get("标的证券代码"))
        rows = result[result["trade_date"] == prior_day]
        key = "证券代码" if "证券代码" in result else "标的证券代码"
        if key in rows:
            rows = rows[rows[key] == code]
        current = result[(result["trade_date"] == day) & (result[key] == code)]
        if len(rows) != 1 or len(current) != 1:
            result.at[index, "margin_repay_derivation_status"] = "missing_previous_session_or_duplicate_conflict"
            continue
        prior = rows.iloc[0]
        if (row.get("exchange_source") != "SZSE" or prior.get("exchange_source") != "SZSE"
                or row.get("monetary_unit") != "元" or prior.get("monetary_unit") != "元"
                or row.get("source_response_date") != day or prior.get("source_response_date") != prior_day):
            result.at[index, "margin_repay_derivation_status"] = "date_source_or_unit_unverified"
            continue
        amounts = [prior.get("融资余额"), row.get("融资买入额"), row.get("融资余额")]
        try:
            valid = all(math.isfinite(float(value)) and float(value) >= 0 for value in amounts)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            continue
        value = round(float(amounts[0]) + float(amounts[1]) - float(amounts[2]), 2)
        if value < 0:
            result.at[index, "margin_repay_derivation_status"] = "negative_identity_or_revision_conflict"
            continue
        reported = row.get("融资偿还额")
        if reported is not None and pd.notna(reported) and abs(float(reported) - value) > 0.02:
            result.at[index, "margin_repay_derivation_status"] = "reported_or_revision_conflict"
            continue
        result.at[index, "margin_repay_amount_derived"] = value
        result.at[index, "margin_repay_derivation_status"] = "derived_not_reported"
        result.at[index, "margin_repay_derivation"] = {"formula": "previous_balance + current_buy - current_balance",
            "previous_trade_date": prior_day, "trade_date": day, "data_source": "SZSE", "monetary_unit": "元",
            "previous_balance": float(amounts[0]), "current_buy": float(amounts[1]), "current_balance": float(amounts[2]),
            "limitations": "balance_identity_estimate; adjustments_or_revisions_may_affect_value; not_official_repayment"}
    return result


def _exchange_day(exchange: str, day: str) -> pd.DataFrame:
    """Public exchange endpoints with socket, size and total-download budgets."""
    import io
    import json
    import time
    import requests
    compact = day.replace("-", "")
    if exchange == "SSE":
        url = "https://query.sse.com.cn/marketdata/tradedata/queryMargin.do"
        params = {"tabType": "mxtype", "detailsDate": compact, "stockCode": "",
                  "isPagination": "true", "pageHelp.pageSize": "5000", "pageHelp.pageNo": "1"}
        referer = "https://www.sse.com.cn/"
    else:
        url = "https://www.szse.cn/api/report/ShowReport"
        params = {"SHOWTYPE": "xlsx", "CATALOGID": "1837_xxpl", "txtDate": day,
                  "TABKEY": "tab2", "tab2PAGENO": "1"}
        referer = "https://www.szse.cn/disclosure/margin/margin/index.html"
    deadline, payload = time.monotonic() + 8, bytearray()
    with requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0", "Referer": referer},
                      timeout=(3, 5), stream=True, allow_redirects=False) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("unexpected exchange response")
        for chunk in response.iter_content(65536):
            payload.extend(chunk)
            if len(payload) > 8 * 1024 * 1024 or time.monotonic() > deadline:
                raise ValueError("margin export exceeds bounded download budget")
    if exchange == "SZSE":
        from app.data.sources.cn.shared.margin_eligibility import official_bytes
        metadata_payload = json.loads(official_bytes("https://www.szse.cn/api/report/ShowReport/data",
            params={"SHOWTYPE": "JSON", "CATALOGID": "1837_xxpl", "txtDate": day, "TABKEY": "tab2", "tab2PAGENO": "1"}))
        tabs = [tab for tab in metadata_payload if (tab.get("metadata") or {}).get("tabkey") == "tab2"]
        if len(tabs) != 1 or str(tabs[0]["metadata"].get("subname", "")).strip() != day:
            raise ValueError("SZSE margin response date unverified")
        validate_szse_json_amount_units(tabs[0]["metadata"])
        frame = pd.read_excel(io.BytesIO(payload), dtype={"证券代码": str})
        if not {"融资买入额(元)", "融资余额(元)"}.issubset(frame.columns):
            raise ValueError("SZSE monetary units unverified")
        frame = frame.rename(columns={"融资买入额(元)": "融资买入额", "融资余额(元)": "融资余额",
            "融券卖出量(股/份)": "融券卖出量", "融券余量(股/份)": "融券余量",
            "融券余额(元)": "融券余额", "融资融券余额(元)": "融资融券余额"})
        if "证券代码" not in frame or "融资余额" not in frame:
            raise ValueError("unverified SZSE margin columns")
        if len(frame) != int(tabs[0]["metadata"].get("recordcount", -1)):
            raise ValueError("incomplete SZSE full-day margin snapshot")
        # Bind the XLSX to the dated JSON result using all available first-page
        # rows. JSON truncates amounts to 0.01 亿元, not exact raw amounts.
        json_rows = tabs[0].get("data") or []
        if not json_rows:
            raise ValueError("SZSE dated comparison rows unavailable")
        for item in json_rows:
            match = frame[frame["证券代码"].astype(str).str.zfill(6) == str(item.get("zqdm"))]
            if len(match) != 1:
                raise ValueError("SZSE XLSX/dated JSON security mismatch")
            for raw_key, column in (("jrrzmr", "融资买入额"), ("jrrzye", "融资余额")):
                validate_szse_display_amount(match.iloc[0][column], item.get(raw_key, ""))
        frame["date_evidence_status"] = "dated_json_and_xlsx_truncated_display_crosscheck"
    else:
        result = json.loads(payload)
        rows = result.get("result") or []
        total = (result.get("pageHelp") or {}).get("total")
        if total is None or len(rows) < int(total):
            raise ValueError("incomplete SSE full-day margin snapshot")
        if any(str(row.get("opDate")) != compact for row in rows):
            raise ValueError("SSE margin date mismatch")
        frame = pd.DataFrame(rows).rename(columns={"stockCode": "标的证券代码", "securityAbbr": "标的证券简称",
            "rzye": "融资余额", "rzmre": "融资买入额", "rzche": "融资偿还额",
            "rqyl": "融券余量", "rqmcl": "融券卖出量", "rqchl": "融券偿还量"})
        if not frame.empty and "标的证券代码" not in frame:
            raise ValueError("unverified SSE margin columns")
        frame["date_evidence_status"] = "official_response_date_exact"
    for column in ("融资余额", "融资买入额", "融资偿还额", "融券余额", "融券卖出量", "融券余量", "融资融券余额"):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column].astype(str).str.replace(",", "", regex=False), errors="coerce")
    frame["trade_date"] = day
    frame["source_response_date"] = day
    frame["exchange_source"] = exchange
    frame["monetary_unit"] = "元"
    # Carry the actual exchange contract through the adapter. Never certify
    # legacy numeric-only records merely because their source says AKShare.
    unit_evidence = {
        "amount_unit": "CNY", "native_amount_unit": "元", "exchange": exchange,
        "method": "dated_xlsx_explicit_yuan_headers" if exchange == "SZSE" else "official_margin_detail_column_contract",
        "source_url": "https://www.szse.cn/disclosure/margin/margin/index.html" if exchange == "SZSE" else "https://www.sse.com.cn/market/othersdata/margin/detail/index.shtml",
    }
    frame["unit_evidence"] = [dict(unit_evidence) for _ in range(len(frame))]
    return frame


async def fetch_margin_range(symbol: str, start: str, end: str) -> pd.DataFrame:
    from app.data.sources.cn.akshare.api.margin_trading import _filter_symbol

    code = symbol.split(".")[0].zfill(6)
    exchange = "SSE" if code.startswith(("6", "9")) else "SZSE" if code.startswith(("0", "2", "3")) else None
    if exchange is None:
        raise NotImplementedError("该交易所的免费两融明细通道尚未接入")
    anchor = date.fromisoformat(end[:10])
    rows = await TradeCalendarRepo().get_range("SSE", "CN", (anchor - timedelta(days=60)).isoformat(), end[:10])
    open_dates = sorted({row["cal_date"] for row in rows if str(row.get("is_open")).lower() in {"true", "1"}})
    dates = [day for day in open_dates if day >= start[:10]][-5:]
    if not dates:
        raise ValueError("缺少实际交易日历，不能用工作日替代两融查询日期")

    loop_key = id(asyncio.get_running_loop())
    deadline = asyncio.get_running_loop().time() + 30
    semaphore = _LOCKS.setdefault(("concurrency", loop_key), asyncio.Semaphore(2))

    async def one(day):
        key = f"{exchange}:{day}"
        lock = _LOCKS.setdefault((key, loop_key), asyncio.Lock())
        async with lock:
            frame = _CACHE.get(key)
            if frame is None:
                async with semaphore:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError("shared margin range budget exhausted")
                    from app.data.sources.cn.akshare.api.anti_scraping import wait_rate_limit
                    def call():
                        wait_rate_limit()
                        return _exchange_day(exchange, day)
                    frame = await asyncio.wait_for(asyncio.to_thread(call), timeout=min(18, remaining))
                    if frame is not None:
                        frame = frame.copy()
                        frame["trade_date"] = day
                        _CACHE.set(key, frame)
            return _filter_symbol(frame, code) if frame is not None else None

    requested_dates = list(dates)
    if exchange == "SZSE" and open_dates.index(dates[0]) > 0:
        requested_dates.append(open_dates[open_dates.index(dates[0]) - 1])
    results = await asyncio.gather(*(one(day) for day in requested_dates), return_exceptions=True)
    frames = [frame for frame in results if isinstance(frame, pd.DataFrame) and not frame.empty]
    if not frames:
        errors = [item for item in results if isinstance(item, Exception)]
        if errors:
            raise errors[0]
        # Absence from detail is not proof of non-eligibility.
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    if exchange == "SZSE":
        combined = derive_margin_repayments(combined, open_dates)
    return combined[combined["trade_date"].isin(dates)].reset_index(drop=True)

async def fetch_margin_eligibility(symbol: str, as_of: str) -> dict:
    """Verified response dates and scoped historical events; never infer from detail absence."""
    code = symbol.split(".")[0].zfill(6)
    result = {"status": "unknown", "eligible": None, "as_of": as_of,
              "reason": "no_verified_asof_eligibility_list"}
    exchange = "SZSE" if code.startswith(("0", "2", "3")) else "SSE" if code.startswith(("6", "9")) else None
    if exchange is None:
        return result
    key = f"eligibility:{exchange}:{code}:{as_of}"
    cached = _CACHE.get(key)
    if cached is None:
        from app.data.sources.cn.shared.margin_eligibility import fetch_sse_eligibility, fetch_szse_eligibility
        def request():
            return fetch_szse_eligibility(code, as_of) if exchange == "SZSE" else fetch_sse_eligibility(code, as_of)
        loop_key = id(asyncio.get_running_loop())
        lock = _LOCKS.setdefault((key, loop_key), asyncio.Lock())
        async with lock:
            cached = _CACHE.get(key)
            if cached is None:
                try:
                    cached = await asyncio.wait_for(asyncio.to_thread(request), timeout=35 if exchange == "SSE" else 9)
                    _CACHE.set(key, cached, ttl=1800 if cached.get("verified") else 60)
                except Exception as exc:
                    cached = {"verified": False, "error_type": type(exc).__name__}
                    _CACHE.set(key, cached, ttl=60)
    if cached.get("verified"):
        eligible = cached["eligible"]
        result.update(cached)
        result.update(status="success" if eligible else "not_applicable",
                      eligible=eligible)
    else:
        result.update({key: value for key, value in cached.items() if key not in {"status", "eligible"}})
        result.setdefault("reason", "eligibility_source_unavailable")
    return result
