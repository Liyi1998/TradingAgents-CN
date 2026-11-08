"""
AKShare 财务数据 API
"""
import asyncio
import logging
from typing import Any, Dict, Optional

import pandas as pd

from app.data.sources.base.exceptions import (
    DataFormatError,
    DataNotFoundError,
    DataSourceUnavailableError,
)
from app.data.sources.base.mappers import map_network_exception
from app.data.schema.base.types import _parse_date
from app.data.schema.domains.financial_data import (
    FINANCIAL_CONTRACT, financial_date, financial_number,
)

logger = logging.getLogger(__name__)

_DOMAIN = "financial"

# 按报告期全市场批量的东财接口列名（接口稳定契约，形状已在 2026-09 实测确认）
_YJBB_KEY_COLS = {
    "symbol": "股票代码",
    "eps": "每股收益",
    "revenue": "营业总收入-营业总收入",
    "net_profit": "净利润-净利润",
    "bps": "每股净资产",
    "roe": "净资产收益率",
    "gross_margin": "销售毛利率",
    "ann_date": "最新公告日期",
}
_ZCFZ_KEY_COLS = {
    "symbol": "股票代码",
    "total_assets": "资产-总资产",
    "total_liab": "负债-总负债",
    "debt_ratio": "资产负债率",
    "total_equity": "股东权益合计",
}



def _safe_float(value) -> Optional[float]:
    """安全转 float，支持中文单位（亿/万）和百分号换算。

    示例：
    - "3.5亿"   → 3.5e8
    - "12.5%"   → 12.5（标准库百分比使用百分点）
    - "1,234.5" → 1234.5
    - None / NaN / "--" / "-" → None
    """
    if value is None or isinstance(value, bool) or type(value).__name__ == "bool_":
        return None
    multiplier = 1.0
    try:
        if isinstance(value, str):
            value = value.strip()
            if not value or value.lower() in ("nan", "null", "none", "--", "-"):
                return None
            value = value.replace(",", "")
            multiplier = 1.0
            # 优先匹配百分号（无单位前缀），其次匹配亿/万单位
            if value.endswith("%"):
                return financial_number(value[:-1])
            if value.endswith("亿"):
                multiplier = 1e8
                value = value[:-1]
            elif value.endswith("万"):
                multiplier = 1e4
                value = value[:-1]
        number = financial_number(value)
        return financial_number(number * multiplier) if number is not None else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def _extract_report_period(raw_row: Dict) -> Optional[str]:
    """从多种可能的字段中提取报告期。"""
    for key in ("REPORT_DATE", "报告期", "报告日", "报表日期", "end_date", "report_date"):
        v = raw_row.get(key)
        if v is not None and not pd.isna(v):
            value = str(v).strip()
            if len(value) == 4 and value.isdigit():
                value += "1231"
            parsed = financial_date(value)
            if parsed:
                return parsed
    return None


def _fetch_sina_table(symbol: str, statement: str) -> pd.DataFrame:
    """Public Sina endpoint used by AKShare, with a real HTTP timeout."""
    import requests

    response = requests.get("https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022",
                            params={"paperCode": symbol, "source": {"balance": "fzb", "income": "lrb", "cashflow": "llb"}[statement],
                                    "type": "0", "page": "1", "num": "20"}, timeout=(3, 6))
    response.raise_for_status()
    body = response.json()["result"]["data"]
    records = []
    for period in body.get("report_date", [])[:20]:
        date = period["date_value"]
        raw = body["report_list"][date]
        row = {item["item_title"]: item.get("item_value") for item in raw.get("data", [])}
        row.update({"报告日": date, "公告日期": raw.get("publish_date"), "币种": raw.get("rCurrency"), "provider_channel": "sina_finance"})
        records.append(row)
    return pd.DataFrame(records)


def _normalize_report_period(raw) -> Optional[str]:
    """报告期归一为 YYYY-MM-DD。

    THS 按年度摘要返回年份值（2025 / int），东财报表返回
    "2025-06-30 00:00:00"——前者归一为年报期 YYYY-12-31（否则入库后
    引擎 YoY 同期匹配断裂），后者截断时间部分。
    """
    if raw is None:
        return None
    s = str(raw).strip().split()[0].replace("-", "").replace("/", "")
    if len(s) == 4 and s.isdigit():
        s += "1231"
    return financial_date(s)



def _build_financial_row(code: str, tables: Dict[str, pd.DataFrame], report_period: str = None) -> Dict[str, Any]:
    """只合并同一报告期，不把最新摘要与不同季度的三表拼在一起。"""
    periods = [_extract_report_period(row) for table in tables.values() if table is not None
               for row in table.to_dict("records")]
    report_period = report_period or max((p for p in periods if p), default=None)

    def matched(name):
        table = tables.get(name)
        return next((row for row in table.to_dict("records") if _extract_report_period(row) == report_period), {}) if table is not None else {}

    a, b, i, c = (matched(name) for name in ("abstract", "balance", "income", "cashflow"))

    def pick(*pairs):
        for row, keys in pairs:
            for key in keys:
                value = _safe_float(row.get(key))
                if value is not None:
                    return value
        return None

    # Exact statement amounts take precedence over rounded abstract amounts.
    revenue = pick((i, ("OPERATE_INCOME", "TOTAL_OPERATE_INCOME", "营业收入", "营业总收入", "revenue", "total_revenue")), (a, ("营业收入", "营业总收入", "revenue")))
    attributed_profit = pick((i, ("PARENT_NETPROFIT", "归属于母公司所有者的净利润", "归属于母公司股东的净利润", "n_income_attr_p")))
    consolidated_profit = pick((i, ("NETPROFIT", "净利润", "net_profit", "n_income")))
    deducted_profit = pick((a, ("扣非净利润", "扣除非经常性损益后的净利润")), (i, ("DEDUCT_PARENT_NETPROFIT",)))
    net_profit = attributed_profit
    total_assets = pick((b, ("TOTAL_ASSETS", "资产总计", "总资产", "total_assets")))
    total_equity = pick((b, ("TOTAL_EQUITY", "所有者权益(或股东权益)合计", "所有者权益合计", "total_equity", "total_hldr_eqy_exc_min_int")))
    total_liab = pick((b, ("TOTAL_LIABILITIES", "负债合计", "总负债", "total_liab")))
    roe = pick((a, ("净资产收益率", "roe")))
    gross_margin = pick((a, ("毛利率", "销售毛利率", "grossprofit_margin", "gross_margin")))
    net_margin = pick((a, ("净利率", "销售净利率", "netprofit_margin", "net_margin")))
    debt_ratio = pick((a, ("资产负债率", "debt_ratio")))
    if debt_ratio is None and total_assets and total_liab is not None:
        debt_ratio = total_liab / total_assets * 100
    current_ratio = pick((a, ("流动比率", "current_ratio")))
    eps = pick((a, ("每股收益", "基本每股收益", "eps")))
    bps = pick((a, ("每股净资产", "bps")))
    operating_cashflow = pick((c, ("NETCASH_OPERATE", "经营活动产生的现金流量净额", "n_cashflow_act")))
    revenue_ttm = _safe_float(a.get("营业收入TTM") or a.get("revenue_ttm"))
    net_profit_ttm = _safe_float(a.get("净利润TTM") or a.get("net_profit_ttm"))

    record = {
        "symbol": code,
        "report_period": report_period,
        "statement_type": "indicator",
        "report_date": report_period,
        "data_source": "akshare",
        "announce_date": None,
        "currency": "CNY",
        "monetary_unit": "元",
        "available_statements": ",".join(name for name, row in zip(("abstract", "balance", "income", "cashflow"), (a, b, i, c)) if row),
        "statement_sources": ",".join(f"{name}:{row.get('provider_channel', 'unknown')}" for name, row in zip(("abstract", "balance", "income", "cashflow"), (a, b, i, c)) if row),
        "cash_and_equivalents": pick((b, ("MONETARYFUNDS", "货币资金"))),
        "accounts_receivable": pick((b, ("ACCOUNTS_RECE", "应收账款"))),
        "capital_expenditure": pick((c, ("CONSTRUCT_LONG_ASSET", "购建固定资产、无形资产和其他长期资产支付的现金"))),
        "revenue": revenue,
        "revenue_ttm": revenue_ttm,
        "net_profit": net_profit,
        "net_profit_attributable": attributed_profit,
        "net_profit_consolidated": consolidated_profit,
        "net_profit_deducted": deducted_profit,
        "net_profit_basis": "attributable" if attributed_profit is not None else "unknown",
        "net_profit_ttm": net_profit_ttm,
        "total_assets": total_assets,
        "total_equity": total_equity,
        "total_liab": total_liab,
        "roe": roe,
        "gross_margin": gross_margin,
        "net_margin": net_margin,
        "debt_ratio": debt_ratio,
        "current_ratio": current_ratio,
        "eps": eps,
        "bps": bps,
        "operating_cashflow": operating_cashflow,
    }
    matched_rows = {"abstract": a, "balance": b, "income": i, "cashflow": c}
    disclosures = {}
    for name, raw in matched_rows.items():
        if not raw:
            continue
        dates = [financial_date(raw.get(key)) for key in ("NOTICE_DATE", "公告日期", "ann_date", "announce_date")]
        known = [value for value in dates if value and report_period and value >= report_period]
        disclosures[name] = {"report_period": report_period, "announce_date": max(known, default=None),
                             "provider_channel": raw.get("provider_channel", "unknown"), "included": True}
    record["announce_date"] = max((item["announce_date"] for item in disclosures.values()
                                   if item["announce_date"]), default=None)
    record["financial_contract"] = FINANCIAL_CONTRACT
    record["report_type"] = "consolidated_ytd"
    record["source_disclosures"] = disclosures
    record["field_provenance"] = {}
    # A THS abstract without its own disclosure date cannot borrow the balance
    # sheet's date. Derive only from exact, same-period dated statement amounts.
    derivations = {}
    dated_income = bool(disclosures.get("income", {}).get("announce_date"))
    dated_abstract = bool(disclosures.get("abstract", {}).get("announce_date"))
    operating_revenue = pick((i, ("OPERATE_INCOME", "营业收入")))
    operating_cost = pick((i, ("OPERATE_COST", "营业成本")))
    income_eps = pick((i, ("BASIC_EPS", "基本每股收益")))
    if dated_income and operating_revenue and operating_revenue > 0 and operating_cost is not None and operating_cost >= 0:
        record["gross_margin"] = (operating_revenue - operating_cost) / operating_revenue * 100
        derivations["gross_margin"] = {"table": "income", "method": "derived_income_revenue_cost", "formula": "(operating_revenue-operating_cost)/operating_revenue*100", "inputs": {"operating_revenue": operating_revenue, "operating_cost": operating_cost}}
    if dated_income and income_eps is not None:
        record["eps"] = income_eps
        derivations["eps"] = {"table": "income", "method": "reported_basic_eps"}
    if not dated_abstract and disclosures.get("balance", {}).get("announce_date") and total_assets and total_assets > 0 and total_liab is not None and total_liab >= 0:
        record["debt_ratio"] = total_liab / total_assets * 100
        derivations["debt_ratio"] = {"table": "balance", "method": "derived_liabilities_over_assets"}
    debt_from_abstract = pick((a, ("资产负债率", "debt_ratio"))) is not None
    revenue_from_income = pick((i, ("OPERATE_INCOME", "TOTAL_OPERATE_INCOME", "营业收入", "营业总收入", "revenue", "total_revenue"))) is not None
    deducted_from_abstract = pick((a, ("扣非净利润", "扣除非经常性损益后的净利润"))) is not None
    fields = {"roe": "abstract", "gross_margin": "abstract", "net_profit": "income",
              "net_profit_attributable": "income", "net_profit_consolidated": "income",
              "debt_ratio": "abstract" if debt_from_abstract else "balance",
              "revenue": "income" if revenue_from_income else "abstract",
              "net_profit_deducted": "abstract" if deducted_from_abstract else "income",
              "revenue_ttm": "abstract", "net_profit_ttm": "abstract",
              "total_assets": "balance", "total_equity": "balance", "total_liab": "balance",
              "cash_and_equivalents": "balance", "accounts_receivable": "balance",
              "capital_expenditure": "cashflow", "operating_cashflow": "cashflow",
              "net_margin": "abstract", "current_ratio": "abstract", "eps": "abstract", "bps": "abstract"}
    ratios = {"roe", "gross_margin", "net_margin", "debt_ratio", "current_ratio"}
    for field, table in fields.items():
        derived = derivations.get(field, {})
        table = derived.get("table", table)
        evidence = disclosures.get(table, {})
        value = record.get(field)
        source_row = matched_rows[table]
        currencies = [str(source_row[key]).strip().upper() for key in ("币种", "rCurrency", "CURRENCY", "currency")
                      if source_row.get(key) is not None and not pd.isna(source_row[key])]
        units = [str(source_row[key]).strip() for key in ("monetary_unit", "金额单位", "单位")
                 if source_row.get(key) is not None and not pd.isna(source_row[key])]
        # These endpoints normally publish CNY/yuan (THS embeds 亿/万 in each
        # value). Explicit conflicting currency/table units must not be silently
        # relabeled; a separate, verified conversion is required for such rows.
        unit_proven = (all(unit in {"元", "人民币元", "CNY"} for unit in units)
                       and all(currency in {"CNY", "RMB", "人民币", "人民币元"} for currency in currencies))
        source_proven = evidence.get("provider_channel") not in {None, "", "unknown"}
        needs_units = field not in ratios or bool(derived)
        if needs_units and not unit_proven:
            record[field] = None
        record["field_provenance"]["total_liabilities" if field == "total_liab" else field] = {
            **evidence, "data_source": "akshare",
            "method": derived.get("method") or ("derived_liabilities_over_assets" if field == "debt_ratio" and not debt_from_abstract else "reported"),
            "basis": "attributable" if field in {"net_profit", "net_profit_attributable"} else "consolidated" if field == "net_profit_consolidated" else "reported",
            "currency": "CNY" if unit_proven else None,
            "monetary_unit": "元" if unit_proven else None,
            "status": ("missing" if value is None else
                       "unproven_unit" if needs_units and not unit_proven else
                       "unproven_source" if not source_proven else
                       "available" if evidence.get("announce_date") else "unproven_disclosure"),
        }
        if derived.get("inputs"):
            record["field_provenance"][field].update(inputs=derived["inputs"], formula=derived["formula"])
    if record["net_profit"] is None:
        record["net_profit_basis"] = "unknown"
    return record


async def fetch_financial_data(
    code: str,
    start_date: str = None,
    end_date: str = None,
) -> pd.DataFrame:
    """获取多表联合财务数据，返回 DataFrame（一行或多行）。

    日期过滤：AKShare 财务接口不支持服务端日期范围查询，拉取全量后在内存中
    按报告期列（REPORT_DATE / 报告期）过滤，减少返回调用方范围外的冗余数据。

    Raises:
        NetworkError: 网络/超时异常（可重试）
        DataFormatError: AKShare 返回结构异常（不可重试）
        DataNotFoundError: 所有财务子表均无数据（不可重试）
        DataSourceUnavailableError: 其他未知异常
    """
    try:
        import akshare as ak

        tables: Dict[str, Optional[pd.DataFrame]] = {}

        from app.data.sources.cn.akshare.api.anti_scraping import wait_rate_limit

        from app.data.sources.cn.stock_name_utils import infer_exchange

        exchange = infer_exchange(code)
        prefix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exchange, "")
        em_symbol = f"{prefix}{code}"
        sina_symbol = em_symbol.lower()
        fetch_tasks = [
            ("abstract", lambda: (wait_rate_limit(), ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期"))[1]),
            ("balance", lambda: (wait_rate_limit(), ak.stock_balance_sheet_by_report_em(symbol=em_symbol))[1]),
            ("income", lambda: (wait_rate_limit(), ak.stock_profit_sheet_by_report_em(symbol=em_symbol))[1]),
            ("cashflow", lambda: (wait_rate_limit(), ak.stock_cash_flow_sheet_by_report_em(symbol=em_symbol))[1]),
        ]

        async def fetch_table(name, fn):
            try:
                df = await asyncio.wait_for(asyncio.to_thread(fn), timeout=8)
                if df is not None and not df.empty:
                    if start_date or end_date:
                        df = _filter_df_by_report_period(df, start_date, end_date)
                    if df is not None and not df.empty:
                        df = df.copy()
                        df["provider_channel"] = "ths_abstract" if name == "abstract" else "eastmoney_statement"
                        tables[name] = df
            except Exception as e:
                # 单个子表数据格式异常（AKShare 返回结构变化或缺字段）：跳过该表，不影响其他表
                # TypeError 也按子表级异常处理：东财系报表接口（*_by_report_em）
                # 在页面结构变化/网络被拒时 akshare 内部抛 'NoneType' object is
                # not subscriptable；单表降级跳过，不影响 THS 摘要等其他子表
                logger.debug(f"AKShare获取财务数据 {name} 格式异常: {e}")
                return
        await asyncio.gather(*(fetch_table(name, fn) for name, fn in fetch_tasks))
        # 免费新浪三表回退；失败只影响当前子表，不丢弃其他已经成功的表。
        async def fallback_table(name, label):
            if name in tables:
                return
            try:
                df = await asyncio.wait_for(asyncio.to_thread(_fetch_sina_table, sina_symbol, name), timeout=10)
                df = _filter_df_by_report_period(df, start_date, end_date)
                if not df.empty:
                    tables[name] = df
            except Exception as exc:
                logger.debug("新浪财报回退 %s/%s: %s", code, name, exc)
        await asyncio.gather(*(fallback_table(name, label) for name, label in (("balance", "资产负债表"), ("income", "利润表"), ("cashflow", "现金流量表"))))

    except (asyncio.TimeoutError, ConnectionError, TimeoutError) as exc:
        # 网络异常：可重试
        raise map_network_exception(exc, "akshare", _DOMAIN)
    except (KeyError, IndexError, AttributeError, ValueError) as exc:
        # 数据格式异常：AKShare 返回结构不符合预期，不可重试
        raise DataFormatError("akshare", _DOMAIN, f"code={code}: {exc}")
    except Exception as exc:
        # 其他未知异常
        raise DataSourceUnavailableError("akshare", _DOMAIN, f"code={code}: {exc}")

    # 所有子表均无数据 → 视为业务空结果
    if not tables:
        raise DataNotFoundError("akshare", _DOMAIN, f"code={code} 所有财务子表均无数据")

    periods = sorted({_extract_report_period(row) for table in tables.values() for row in table.to_dict("records")} - {None}, reverse=True)
    if not periods:
        raise DataFormatError("akshare", _DOMAIN, f"code={code} 无可核验报告期")
    return pd.DataFrame([_build_financial_row(code, tables, period) for period in periods[:20]])


def _filter_df_by_report_period(
    df: "pd.DataFrame", start_date: str, end_date: str
) -> "pd.DataFrame":
    """按报告期列过滤 AKShare 财务 DataFrame。

    AKShare 各财务报表的报告期列名不统一（REPORT_DATE / 报告期 / 报表日期等），
    这里尝试常见列名并核验真实日期；未知报告期不进入一个已限定的窗口。
    """
    period_cols = ["REPORT_DATE", "报告期", "报表日期", "报告日", "end_date", "report_date"]
    col = None
    for c in period_cols:
        if c in df.columns:
            col = c
            break
    if col is None:
        return df.iloc[0:0].copy()

    start, end = financial_date(start_date), financial_date(end_date)
    if (start_date and not start) or (end_date and not end) or (start and end and start > end):
        raise ValueError("Invalid financial report date bounds")
    periods = df[col].map(_normalize_report_period)
    mask = periods.notna()
    if start:
        mask = mask & periods.ge(start)
    if end:
        mask = mask & periods.le(end)
    filtered = df[mask].copy()
    if not filtered.empty:
        filtered["__period_sort"] = periods.loc[filtered.index]
        filtered = filtered.sort_values(
            "__period_sort", ascending=False, na_position="last"
        ).drop(columns=["__period_sort"])
    return filtered


# ── 按报告期批量模式（东财全市场，tushare 积分不足时的批量主源）─────

def _rename_key_cols(df: pd.DataFrame, key_cols: Dict[str, str]) -> pd.DataFrame:
    """按接口稳定契约重命名关键列；源列缺失时置 None（接口改版早暴露）。"""
    renamed = pd.DataFrame(index=df.index)
    for neutral, src in key_cols.items():
        renamed[neutral] = df[src] if src in df.columns else None
    return renamed


async def fetch_financial_data_by_period(period: str, symbols=None) -> pd.DataFrame:
    """东财业绩报表 + 资产负债表按报告期合并全市场财务快照。

    stock_yjbb_em（每股收益/营收/净利/ROE/毛利率）左连 stock_zcfz_em
    （总资产/总负债/资产负债率），产一行合并记录（statement_type=
    "indicator"），列名与 _build_financial_row 输出形态一致，
    adapt_financial_data 零改动标准化。同比由引擎从 net_profit 绝对值
    按同期自算（与 tushare 路径口径一致），不取东财同比列。

    Raises:
        DataNotFoundError: 该报告期两接口均无数据（未披露/停市）
    """
    from app.data.sources.cn.akshare.api.financial_disclosures import (
        fetch_period_rows, normalize_period_rows,
    )

    results = await asyncio.gather(*(
        asyncio.to_thread(fetch_period_rows, report, period, symbols)
        for report in ("RPT_LICO_FN_CPD", "RPT_DMSK_FN_BALANCE")
    ), return_exceptions=True)
    if all(isinstance(result, Exception) for result in results):
        raise DataSourceUnavailableError("akshare", _DOMAIN, f"{period}: both financial reports unavailable") from results[0]
    available = []
    for name, result in zip(("indicator", "balance"), results):
        if isinstance(result, Exception):
            logger.warning("Financial report unavailable %s/%s: %s", period, name, type(result).__name__)
            available.append([])
        else:
            available.append(result)
    merged = normalize_period_rows(*available, period=period)
    if merged.empty:
        raise DataNotFoundError("akshare", _DOMAIN, f"{period}: no normalized financial rows")
    logger.info("AKShare disclosed financial batch %s: %d rows", period, len(merged))
    return merged


async def fetch_financial_data_batch(periods: list) -> pd.DataFrame:
    """批量入口：逐报告期拉东财全市场快照并纵向合并。

    单期无数据（极早期报告期接口未覆盖）跳过不阻塞；全部期无数据
    返回空 DataFrame（调用方按 failed 处理并保持熔断语义）。
    """
    frames = []
    for period in periods:
        try:
            df = await fetch_financial_data_by_period(period)
        except DataNotFoundError:
            logger.warning(f"AKShare 报告期 {period} 无数据，跳过")
            continue
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
