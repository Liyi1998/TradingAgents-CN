"""Eastmoney report rows normalized by named fields and real disclosure dates.

The public endpoints are the same ones used by AKShare's period fetchers. Named
JSON keys avoid positional column drift and preserve each contributing report's
NOTICE_DATE; request dates and UPDATE_DATE are never publication evidence.
"""
import re

import pandas as pd
import requests

from app.data.schema.domains.financial_data import (
    FINANCIAL_CONTRACT, financial_date, financial_number,
)
from app.data.sources.base.exceptions import DataFormatError, DataNotFoundError

CONTRACT = FINANCIAL_CONTRACT
URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _number(value):
    return financial_number(value)


def _date(value):
    return financial_date(value)


def period_request_params(report: str, period: str, symbols=None):
    """Keep the installed AKShare endpoint universe, with optional narrowing."""
    period = _date(period)
    if not period or period[5:] not in {"03-31", "06-30", "09-30", "12-31"}:
        raise ValueError("Expected a real quarter/year-end report period")
    date_field = {"RPT_LICO_FN_CPD": "REPORTDATE", "RPT_DMSK_FN_BALANCE": "REPORT_DATE"}.get(report)
    if date_field is None:
        raise ValueError("Unsupported financial report")
    scope = set(symbols or [])
    if symbols is not None and (not scope or len(scope) > 100 or any(not re.fullmatch(r"\d{6}", s) for s in scope)):
        raise ValueError("Financial sample must contain 1..100 valid CN symbols")
    condition = f"({date_field}='{period}')"
    if report == "RPT_DMSK_FN_BALANCE":
        # stock_zcfz_em's native A/B-share, non-OTC filter. Removing it widens
        # the default universe even if a 20-symbol sample happens to pass.
        condition = ('(SECURITY_TYPE_CODE in ("058001001","058001008"))'
                     '(TRADE_MARKET_CODE!="069001017")' + condition)
    if scope:
        condition += '(SECURITY_CODE in (' + ','.join('"' + s + '"' for s in sorted(scope)) + '))'
    return {"reportName": report, "columns": "ALL", "filter": condition,
            "sortColumns": "NOTICE_DATE,SECURITY_CODE", "sortTypes": "-1,1",
            "pageSize": 500, "pageNumber": 1}


def fetch_period_rows(report: str, period: str, symbols=None):
    """Bounded public GETs. Optional symbol scope is also enforced on responses."""
    from app.data.sources.cn.akshare.api.anti_scraping import wait_rate_limit

    params = period_request_params(report, period, symbols)
    period = _date(period)
    date_field = "REPORTDATE" if report == "RPT_LICO_FN_CPD" else "REPORT_DATE"
    scope = set(symbols or [])
    rows = []
    pages = None
    for page in range(1, 31):
        wait_rate_limit()
        params["pageNumber"] = page
        response = requests.get(URL, params=params, timeout=(4, 12))
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result")
        if page == 1 and not result and payload.get("success") is False:
            # An empty business response is not a successful/complete snapshot.
            raise DataNotFoundError("akshare", "financial_data", f"{report}/{period} has no rows")
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise DataFormatError("akshare", "financial_data", "Unexpected report response")
        actual_pages = int(result.get("pages") or 0)
        if not 1 <= actual_pages <= 30 or pages is not None and actual_pages != pages:
            raise DataFormatError("akshare", "financial_data", "Unbounded or changing report pagination")
        pages = actual_pages
        for row in result["data"]:
            if _date(row.get(date_field)) != period or scope and row.get("SECURITY_CODE") not in scope:
                raise DataFormatError("akshare", "financial_data", "Report response escaped requested scope")
            if report == "RPT_DMSK_FN_BALANCE" and (
                    row.get("SECURITY_TYPE_CODE") not in {"058001001", "058001008"}
                    or not row.get("TRADE_MARKET_CODE")
                    or row.get("TRADE_MARKET_CODE") == "069001017"):
                raise DataFormatError("akshare", "financial_data", "Balance response escaped native market scope")
        rows.extend(result["data"])
        if page == pages:
            break
    return rows


def normalize_period_rows(indicators, balances, period, *, as_of=None):
    """Keep disclosure evidence per field, including explicitly unknown dates."""
    period = _date(period)
    if not period:
        raise ValueError("Invalid financial report period")
    cutoff = _date(as_of) if as_of is not None else None
    if as_of is not None and not cutoff:
        raise ValueError("Invalid financial as-of date")

    def indexed(rows, date_field):
        selected = {}
        for raw in rows:
            symbol = str(raw.get("SECURITY_CODE") or "")
            if not re.fullmatch(r"\d{6}", symbol) or _date(raw.get(date_field)) != period:
                continue
            # Prefer a disclosed revision. Never create a disclosure from its period.
            disclosure = _date(raw.get("NOTICE_DATE"))
            if disclosure and disclosure < period:
                disclosure = None
            if cutoff and (period > cutoff or disclosure and disclosure > cutoff):
                continue
            row = dict(raw, _disclosure=disclosure)
            previous = selected.get(symbol)
            if previous is None or (disclosure or "") > (previous["_disclosure"] or ""):
                selected[symbol] = row
            elif disclosure == previous["_disclosure"]:
                fields = (("BASIC_EPS", "TOTAL_OPERATE_INCOME", "PARENT_NETPROFIT", "BPS", "WEIGHTAVG_ROE", "XSMLL")
                          if date_field == "REPORTDATE" else
                          ("TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_EQUITY", "MONETARYFUNDS", "ACCOUNTS_RECE", "DEBT_ASSET_RATIO"))
                if any(_number(row.get(key)) != _number(previous.get(key)) for key in fields):
                    raise ValueError("Conflicting financial values for one disclosure date")
        return selected

    ind = indexed(indicators, "REPORTDATE")
    bal = indexed(balances, "REPORT_DATE")
    records = []
    for symbol in sorted(set(ind) | set(bal)):
        i, b = ind.get(symbol, {}), bal.get(symbol, {})
        disclosures = {}
        for name, raw in (("indicator", i), ("balance", b)):
            if raw:
                disclosures[name] = {"report_period": period, "announce_date": raw.get("_disclosure"),
                                     "provider_channel": "eastmoney_" + name, "included": True}
        record = {"symbol": symbol, "report_period": period, "statement_type": "indicator",
                  "report_type": "consolidated_ytd", "data_source": "akshare",
                  "financial_contract": CONTRACT, "source_disclosures": disclosures,
                  "field_provenance": {}, "currency": "CNY", "monetary_unit": "元",
                  "announce_date": max((d["announce_date"] for d in disclosures.values() if d["announce_date"]), default=None),
                  "net_profit_basis": "attributable"}
        mappings = ((i, "indicator", {"eps": "BASIC_EPS", "revenue": "TOTAL_OPERATE_INCOME",
                     "net_profit": "PARENT_NETPROFIT", "net_profit_attributable": "PARENT_NETPROFIT",
                     "bps": "BPS", "roe": "WEIGHTAVG_ROE", "gross_margin": "XSMLL"}),
                    (b, "balance", {"total_assets": "TOTAL_ASSETS", "total_liab": "TOTAL_LIABILITIES",
                     "total_equity": "TOTAL_EQUITY", "cash_and_equivalents": "MONETARYFUNDS",
                     "accounts_receivable": "ACCOUNTS_RECE", "debt_ratio": "DEBT_ASSET_RATIO"}))
        for raw, name, fields in mappings:
            for field, source_field in fields.items():
                value = _number(raw.get(source_field))
                record[field] = value
                record["field_provenance"]["total_liabilities" if field == "total_liab" else field] = {
                    "data_source": "akshare", "provider_channel": "eastmoney_" + name,
                    "report_period": period, "announce_date": raw.get("_disclosure"),
                    "method": "reported", "basis": "attributable" if "net_profit" in field else "reported",
                    "status": "available" if value is not None and raw.get("_disclosure") else "unproven_disclosure" if value is not None else "missing"}
        records.append(record)
    return pd.DataFrame(records)
