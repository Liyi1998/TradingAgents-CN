"""AKShare CN Adapter — 原始数据 → 标准 Schema。

AKShare 返回的数据单位已是标准单位（股/元），主要做字段名映射。
"""

import logging
import re
from typing import Any, Dict, List, Optional

import pandas as pd

from app.data.sources.base.adapter import BaseAdapter
from app.data.schema.base.types import _safe_float, _safe_str, _parse_date
from app.data.schema.domains.basic_info import StockBasicInfoSchema
from app.data.schema.domains.daily_quotes import DailyQuotesSchema
from app.data.schema.domains.daily_indicators import DailyIndicatorsSchema
from app.data.schema.domains.adj_factors import AdjFactorsSchema
from app.data.schema.domains.financial_data import FinancialDataSchema
from app.data.schema.domains.stock_news import StockNewsSchema
from app.data.schema.domains.market_quotes import MarketQuotesSchema
from app.data.schema.domains.money_flow import MoneyFlowSchema
from app.data.schema.domains.margin_trading import MarginTradingSchema
from app.data.schema.domains.dragon_tiger import DragonTigerSchema
from app.data.schema.domains.block_trade import BlockTradeSchema
from app.data.schema.domains.intraday_quotes import IntradayQuotesSchema
from app.data.schema.domains.trade_calendar import TradeCalendarSchema
from app.data.schema.domains.corporate_actions import CorporateActionsSchema

logger = logging.getLogger(__name__)


from app.data.sources.cn.stock_name_utils import infer_exchange as _infer_exchange  # noqa: E402 (intentional late import)


# AKShare 市值字段单位映射表（按接口名声明，是 AKShare 接口的稳定契约）。
# schema 统一以"元"为单位存储市值，adapter 出口按映射表归一化。
#   - stock_zh_valuation_baidu（百度股市通）→ 亿元（数值 × 1e8 转元）
#   - stock_zh_a_spot_em（东方财富实时）→ 元（数值本身已是元）
#   - stock_zh_a_st_em / 其它 spot 类 → 元
# 未声明接口默认按"元"处理（不换算）；调用方若发现新接口单位异常，应在此显式登记。
_AKSHARE_MV_UNIT_BY_INTERFACE: Dict[str, str] = {
    "stock_zh_valuation_baidu": "亿元",
    "stock_zh_a_spot_em": "元",
    "stock_zh_a_st_em": "元",
}


def _convert_mv(value: Optional[float], unit: str) -> Optional[float]:
    """按声明单位把市值归一化为元。"""
    if value is None:
        return None
    if unit == "亿元":
        return value * 1e8
    if unit == "万元":
        return value * 1e4
    return value  # "元" 或未知单位 → 原值


class AKShareCNAdapter(BaseAdapter):
    """AKShare A 股数据标准化适配器。"""

    def __init__(self, provider=None):
        super().__init__(provider=provider, market="CN", source_name="akshare")

    def adapt_basic_info(self, raw: Any) -> List[StockBasicInfoSchema]:
        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if df.empty:
            return []
        results = []
        for _, row in df.iterrows():
            get = row.get
            symbol = str(get("symbol", "") or get("code", "")).zfill(6)
            exchange = _infer_exchange(symbol)
            results.append(StockBasicInfoSchema(
                symbol=symbol,
                market="CN",
                data_source="akshare",
                name=get("name", ""),
                full_symbol=f"{symbol}.{exchange}" if exchange else symbol,
                exchange=exchange,
                industry=get("industry"),
                industry_source=get("industry_source"),
                industry_classification=get("industry_classification"),
                industry_as_of=_parse_date(get("industry_as_of")),
                industry_contract=get("industry_contract"),
                list_date=_parse_date(get("list_date")),
                currency="CNY",
            ))
        return results

    def adapt_trade_calendar(self, raw: Any) -> List[TradeCalendarSchema]:
        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if df.empty:
            return []
        results = []
        for _, row in df.iterrows():
            cal_date = _parse_date(row.get("cal_date") or row.get("trade_date"))
            if not cal_date:
                continue
            results.append(
                TradeCalendarSchema(
                    symbol="__calendar__",
                    market="CN",
                    data_source="akshare",
                    exchange=str(row.get("exchange") or "SSE"),
                    cal_date=cal_date,
                    is_open=bool(row.get("is_open")),
                    pretrade_date=_parse_date(row.get("pretrade_date")),
                )
            )
        return results

    def adapt_daily_quotes(self, raw: Any) -> List[DailyQuotesSchema]:
        import math
        import numpy as np

        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if df.empty:
            return []
        # Persist the exact sub-source and conversion through existing common
        # schema fields. Never infer a unit just because a value looks plausible.
        conversions = {
            "stock_zh_a_hist": "akshare:stock_zh_a_hist/eastmoney;volume:lot100->share*100;amount:CNY->CNY",
            "stock_zh_a_daily": "akshare:stock_zh_a_daily/sina;volume:share->share;amount:CNY->CNY",
        }
        results = []
        for _, row in df.iterrows():
            get = row.get

            def first(*names):
                for name in names:
                    value = get(name)
                    if value is None or (isinstance(value, str) and not value.strip()):
                        continue
                    try:
                        if pd.isna(value):
                            continue
                    except (TypeError, ValueError):
                        continue
                    return value
                return None

            def number(*names):
                value = first(*names)
                if isinstance(value, (bool, np.bool_)):
                    return None
                parsed = _safe_float(value)
                return parsed if parsed is not None and math.isfinite(parsed) else None

            raw_symbol = str(first("symbol", "code", "股票代码") or "").strip()
            if not raw_symbol:
                # 东财/新浪行情接口的 df 不含代码列；缺失时不得静默写成
                # "".zfill(6)="000000" 入库（会污染主键、导致回读为空）
                logger.warning("AKShare daily_quotes 行缺少 symbol/code 列，跳过该行")
                continue
            symbol = raw_symbol.zfill(6)
            trade_date = _parse_date(first("trade_date", "日期", "date"))
            if not trade_date:
                continue

            source_api = first("quote_api")
            source = first("quote_source")
            verified = first("quote_units_verified")
            units_proven = (isinstance(verified, (bool, np.bool_)) and bool(verified)
                            and first("amount_unit") == "CNY"
                            and ((source_api == "stock_zh_a_hist" and source == "eastmoney"
                                  and first("volume_unit") == "lot100")
                                 or (source_api == "stock_zh_a_daily" and source == "sina"
                                     and first("volume_unit") == "share")))
            conversion = conversions.get(source_api) if units_proven else None
            # Idempotent when a previously normalized standard row is read back.
            # The exact persisted contract is required; share/CNY alone is not evidence.
            normalized = (source_api is None and first("data_source") == "akshare"
                          and first("volume_unit") == "share" and first("amount_unit") == "CNY"
                          and first("unit_conversion") in conversions.values())
            if normalized:
                conversion = first("unit_conversion")
            volume = number("volume", "vol", "成交量")
            if units_proven and source_api == "stock_zh_a_hist" and volume is not None:
                volume *= 100.0
                if not math.isfinite(volume):
                    volume = None
            amount = number("amount", "成交额")  # Sina turnover is a ratio, not money.
            turnover = number("turnover_rate", "换手率")
            if turnover is None and units_proven and source_api == "stock_zh_a_daily":
                turnover = number("turnover")
                if turnover is not None:
                    turnover *= 100.0
                    if not math.isfinite(turnover):
                        turnover = None
            close = number("close", "收盘")
            pre_close = number("pre_close")
            change = number("change", "涨跌额")
            pct_chg = number("pct_chg", "change_percent", "涨跌幅")
            if change is None and close is not None and pre_close is not None:
                change = round(close - pre_close, 4)
            if pct_chg is None and close is not None and pre_close is not None and pre_close != 0:
                pct_chg = round((close - pre_close) / pre_close * 100, 4)

            results.append(DailyQuotesSchema(
                symbol=symbol,
                market="CN",
                data_source="akshare",
                trade_date=trade_date,
                period=first("quote_period", "period") or "daily",
                price_basis=get("price_basis"),
                adjustment_as_of=_parse_date(get("adjustment_as_of")),
                adjustment_contract=get("adjustment_contract"),
                volume_unit="share" if units_proven or normalized else None,
                amount_unit="CNY" if units_proven or normalized else None,
                unit_conversion=conversion,
                open=number("open", "开盘"),
                high=number("high", "最高"),
                low=number("low", "最低"),
                close=close,
                pre_close=pre_close,
                change=change,
                pct_chg=pct_chg,
                volume=volume,
                amount=amount,
                turnover_rate=turnover,
            ))
        return results

    def adapt_daily_indicators(self, raw: Any) -> List[DailyIndicatorsSchema]:
        import numpy as np

        from app.data.schema.domains.daily_indicators import finite_indicator, indicator_quality

        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if df.empty:
            return []
        # AKShare daily_indicators 的数据来自百度股市通（stock_zh_valuation_baidu），
        # 该接口返回的市值数值单位为"亿元"；按接口契约显式声明，不再用数值阈值猜测。
        # 详见 _AKSHARE_MV_UNIT_BY_INTERFACE。
        mv_unit = _AKSHARE_MV_UNIT_BY_INTERFACE.get(
            "stock_zh_valuation_baidu", "元"
        )
        results = []
        for _, row in df.iterrows():
            get = row.get
            symbol = str(get("symbol", "") or get("code", "") or get("代码", "")).zfill(6)
            trade_date = _parse_date(
                get("trade_date") or get("日期") or get("date")
            )
            if not trade_date:
                continue

            row_mv_unit = str(get("_mv_unit") or mv_unit)
            def number(*names):
                for name in names:
                    raw_value = get(name)
                    if isinstance(raw_value, (bool, np.bool_)):
                        continue
                    value = _safe_float(raw_value)
                    if finite_indicator(value):
                        return value
                return None

            total_mv = _convert_mv(number("total_mv", "总市值"), row_mv_unit)
            circ_mv = _convert_mv(number("circ_mv", "流通市值"), row_mv_unit)
            total_mv = total_mv if finite_indicator(total_mv) else None
            circ_mv = circ_mv if finite_indicator(circ_mv) else None

            values = {
                "pe_ttm": number("pe_ttm"),
                "pe_dynamic": number("pe_dynamic", "市盈率-动态"),
                "pe_reported": number("pe"),
                "pb": number("pb", "市净率"),
                "ps_ttm": number("ps_ttm"),
                "turnover_rate": number("turnover_rate", "换手率"),
                "total_mv": total_mv, "circ_mv": circ_mv,
                "volume_ratio": number("volume_ratio", "量比"),
            }
            method = str(get("_indicator_method") or "unverified_input")
            date_verified = (method == "baidu_valuation_history"
                             and str(get("_date_verified")).lower() == "true")
            provenance = {}
            for field, value in values.items():
                if not finite_indicator(value):
                    continue
                basis = ("ttm" if field == "pe_ttm" and method == "baidu_valuation_history"
                         else "dynamic" if field == "pe_dynamic"
                         else "unknown" if field in {"pe_ttm", "pe_reported"}
                         else "vendor_pb" if field == "pb" else "vendor_reported")
                provenance[field] = {
                    "data_source": "akshare", "as_of": trade_date, "method": method,
                    "date_verified": date_verified, "basis": basis,
                }
                if field == "turnover_rate" and method == "baidu_valuation_history":
                    quote_method = get("_quote_method")
                    provenance[field]["method"] = quote_method if quote_method in {
                        "eastmoney_daily_history", "sina_daily_history"} else "unverified_quote_history"
                    provenance[field]["date_verified"] = quote_method in {
                        "eastmoney_daily_history", "sina_daily_history"}
            unsupported = {"dividend_yield", "dividend_yield_ttm"}
            dividend_evidence = get("_dividend_ttm_provenance")
            dividend_diagnostic = get("_dividend_ttm_diagnostic")
            if isinstance(dividend_diagnostic, dict):
                unsupported.discard("dividend_yield_ttm")
            if (isinstance(dividend_evidence, dict)
                    and dividend_evidence.get("method") == "derived_cash_dividend_ex_date_12m"
                    and dividend_evidence.get("basis") == "ttm"
                    and dividend_evidence.get("date_verified") is True
                    and dividend_evidence.get("as_of") == trade_date
                    and dividend_evidence.get("denominator_date") == trade_date
                    and dividend_evidence.get("denominator_basis") == "unadjusted"):
                values["dividend_yield_ttm"] = number("dividend_yield_ttm")
                provenance["dividend_yield_ttm"] = dict(dividend_evidence)
                unsupported.discard("dividend_yield_ttm")
            if method == "baidu_valuation_history":
                unsupported.add("volume_ratio")
            quality = indicator_quality(values, provenance, unsupported)
            if isinstance(dividend_diagnostic, dict):
                quality["dividend_ttm"] = dividend_diagnostic
            results.append(DailyIndicatorsSchema(
                symbol=symbol,
                market="CN",
                data_source="akshare",
                trade_date=trade_date,
                **values, field_provenance=provenance, data_quality=quality,
            ))
        return results

    def adapt_adj_factors(self, raw: Any) -> List[AdjFactorsSchema]:
        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if df.empty:
            return []
        from app.data.processor.adjustment_contract import adjustment_schemas

        return adjustment_schemas(df, source="akshare")

    def adapt_financial_data(self, raw: Any) -> List[FinancialDataSchema]:
        from app.data.schema.domains.financial_data import financial_date, financial_number

        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if df.empty:
            return []
        results = []
        for _, row in df.iterrows():
            get = row.get
            symbol = str(get("symbol", "") or get("code", "")).zfill(6)
            report_period = financial_date(
                get("report_period") or get("report_date") or get("end_date")
            )
            results.append(FinancialDataSchema(
                symbol=symbol,
                market="CN",
                data_source="akshare",
                report_period=report_period,
                statement_type=_safe_str(get("statement_type")) or "indicator",
                report_type=_safe_str(get("report_type")),
                announce_date=financial_date(get("announce_date")) or financial_date(get("ann_date")),
                financial_contract=_safe_str(get("financial_contract")),
                source_disclosures=get("source_disclosures") if isinstance(get("source_disclosures"), dict) else None,
                field_provenance=get("field_provenance") if isinstance(get("field_provenance"), dict) else None,
                available_statements=_safe_str(get("available_statements")),
                statement_sources=_safe_str(get("statement_sources")),
                currency=_safe_str(get("currency")),
                monetary_unit=_safe_str(get("monetary_unit")),
                revenue=financial_number(get("revenue")),
                net_profit=financial_number(get("net_profit")),
                net_profit_attributable=financial_number(get("net_profit_attributable")),
                net_profit_consolidated=financial_number(get("net_profit_consolidated")),
                net_profit_deducted=financial_number(get("net_profit_deducted")),
                net_profit_basis=_safe_str(get("net_profit_basis")),
                total_liabilities=financial_number(get("total_liab")) if financial_number(get("total_liab")) is not None else financial_number(get("total_liabilities")),
                cash_and_equivalents=financial_number(get("cash_and_equivalents")),
                accounts_receivable=financial_number(get("accounts_receivable")),
                capital_expenditure=financial_number(get("capital_expenditure")),
                total_assets=financial_number(get("total_assets")),
                total_equity=financial_number(get("total_equity")),
                roe=financial_number(get("roe")),
                gross_margin=financial_number(get("gross_margin")),
                net_margin=financial_number(get("net_margin")),
                debt_ratio=financial_number(get("debt_ratio")),
                current_ratio=financial_number(get("current_ratio")),
                eps=financial_number(get("eps")),
                bps=financial_number(get("bps")),
                operating_cashflow=financial_number(get("operating_cashflow")),
            ))
        return results

    def adapt_news(self, raw: Any) -> List[StockNewsSchema]:
        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if df.empty:
            return []
        results = []
        for _, row in df.iterrows():
            get = row.get
            title = get("title", "") or get("标题", "")
            publish_time = get("datetime", "") or get("publish_time", "") or get("发布时间", "")
            symbol = str(get("symbol", ""))
            content_hash = StockNewsSchema.compute_hash(title, str(publish_time), symbol) if title else None
            results.append(StockNewsSchema(
                symbol=symbol,
                market="CN",
                data_source="akshare",
                title=title,
                content=get("content") or get("内容"),
                content_hash=content_hash,
                source=get("source") or get("来源", ""),
                publish_time=publish_time,
                url=get("url") or get("链接"),
                category=_safe_str(get("category") or get("type")),
                source_kind=_safe_str(get("source_kind")) or "aggregator",
                content_status=_safe_str(get("content_status")),
                filing_metadata=get("filing_metadata") if isinstance(get("filing_metadata"), dict) else None,
                pdf_metadata=get("pdf_metadata") if isinstance(get("pdf_metadata"), dict) else None,
                discovery_metadata=get("discovery_metadata") if isinstance(get("discovery_metadata"), dict) else None,
                type=get("type") or get("category"),
                summary=get("summary"),
            ))
        return results

    def adapt_market_quotes(self, raw: Any) -> List[MarketQuotesSchema]:
        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if df.empty:
            return []
        results = []
        for _, row in df.iterrows():
            get = row.get
            symbol = str(get("symbol", "") or get("code", "") or get("股票代码", "")).zfill(6)
            close = _safe_float(get("price") or get("最新价") or get("close"))
            pre_close = _safe_float(get("昨收") or get("pre_close"))
            pct_chg = _safe_float(get("涨跌幅") or get("pct_chg") or get("change_percent"))
            volume = _safe_float(get("volume") or get("成交量"))
            amount = _safe_float(get("amount") or get("成交额") or get("turnover"))
            results.append(MarketQuotesSchema(
                symbol=symbol,
                market="CN",
                data_source="akshare",
                open=_safe_float(get("开盘") or get("open")),
                high=_safe_float(get("最高") or get("high")),
                low=_safe_float(get("最低") or get("low")),
                close=close,
                pre_close=pre_close,
                pct_chg=pct_chg,
                volume=volume,
                amount=amount,
                turnover_rate=_safe_float(get("换手率") or get("turnover_rate")),
                last_price=close,
                last_volume=volume,
            ))
        return results

    def adapt_intraday_quotes(self, raw: Any) -> List[IntradayQuotesSchema]:
        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if df.empty:
            return []
        results = []
        for _, row in df.iterrows():
            get = row.get
            def first(*keys):
                for key in keys:
                    value = get(key)
                    if value is not None and not pd.isna(value) and value != "":
                        return value
                return None
            code = first("股票代码", "code", "symbol")
            if code is None or not re.fullmatch(r"\d{6}", str(code).split(".", 1)[0].zfill(6)):
                continue
            symbol = str(code).split(".", 1)[0].zfill(6)
            dt = str(get("时间", "") or get("datetime", "") or get("day", ""))
            results.append(IntradayQuotesSchema(
                symbol=symbol,
                market="CN",
                data_source="akshare",
                datetime=dt,
                freq=str(get("freq", "30min")),
                open=_safe_float(first("开盘", "open")),
                close=_safe_float(first("收盘", "close")),
                high=_safe_float(first("最高", "high")),
                low=_safe_float(first("最低", "low")),
                volume=_safe_float(first("成交量", "volume")),
                amount=_safe_float(first("成交额", "amount")),
                ohlc_kind=first("ohlc_kind"), price_basis=first("price_basis"),
                volume_unit=first("volume_unit"), amount_unit=first("amount_unit"),
                unit_conversion=first("unit_conversion"),
                pct_chg=_safe_float(get("涨跌幅", "") or get("change")),
            ))
        return results

    def adapt_money_flow(self, raw: Any) -> List[MoneyFlowSchema]:
        import math
        import numpy as np

        from app.data.schema.domains.money_flow import normalize_money_flow_symbol

        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if df.empty:
            return []
        results = []
        for _, row in df.iterrows():
            get = row.get
            def first(*names):
                for name in names:
                    value = get(name)
                    if value is not None and not pd.isna(value) and value != "":
                        return value
                return None

            def number(*names):
                value = first(*names)
                if isinstance(value, (bool, np.bool_)):
                    return None
                result = _safe_float(value)
                return result if result is not None and math.isfinite(result) else None

            try:
                symbol = normalize_money_flow_symbol(first("股票代码", "代码", "symbol"))
            except ValueError:
                continue
            td = _parse_date(first("日期", "trade_date"))
            is_rank = any(str(c).startswith("今日") for c in df.columns)
            verified = get("flow_date_verified")
            if verified is None or pd.isna(verified):
                verified = not is_rank
            verified = isinstance(verified, (bool, np.bool_)) and bool(verified)
            results.append(MoneyFlowSchema(
                symbol=symbol,
                market="CN",
                data_source="akshare",
                trade_date=td,
                exchange=_infer_exchange(symbol),
                main_net_inflow=number("主力净流入-净额", "今日主力净流入-净额", "main_net_inflow"),
                main_net_inflow_pct=number("主力净流入-净占比", "今日主力净流入-净占比", "main_net_inflow_pct", "main_net_pct"),
                huge_net_inflow=number("超大单净流入-净额", "今日超大单净流入-净额", "huge_net_inflow"),
                large_net_inflow=number("大单净流入-净额", "今日大单净流入-净额", "large_net_inflow"),
                medium_net_inflow=number("中单净流入-净额", "今日中单净流入-净额", "medium_net_inflow"),
                small_net_inflow=number("小单净流入-净额", "今日小单净流入-净额", "small_net_inflow"),
                flow_basis="eastmoney_order_size_v1",
                flow_source="eastmoney", amount_unit="CNY", flow_pct_unit="percent",
                flow_api=_safe_str(first("flow_api")) or ("stock_individual_fund_flow_rank" if is_rank else "stock_individual_fund_flow"),
                flow_date_verified=verified,
                flow_date_basis="source_row_date" if verified else "capture_date",
            ))
        return results

    def adapt_margin_trading(self, raw: Any) -> List[MarginTradingSchema]:
        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if df.empty:
            return []
        results = []
        for _, row in df.iterrows():
            get = row.get
            symbol = str(
                get("标的证券代码", "")
                or get("证券代码", "")
                or get("股票代码", "")
                or get("symbol", "")
            ).zfill(6)
            td = _parse_date(
                get("信用交易日期", "") or get("日期", "") or get("trade_date", "")
            )
            unit_evidence = get("unit_evidence")
            exchange = _safe_str(get("exchange_source"))
            response_date = _parse_date(get("source_response_date"))
            date_status = _safe_str(get("date_evidence_status"))
            native_unit = _safe_str(get("monetary_unit"))
            from app.data.core.requirements import margin_units_verified
            verified = margin_units_verified({
                "exchange": exchange, "data_source": "akshare", "trade_date": td,
                "source_response_date": response_date, "date_evidence_status": date_status,
                "amount_unit": "CNY" if native_unit == "元" else None,
                "monetary_unit": native_unit,
                "unit_evidence": unit_evidence,
            })
            results.append(MarginTradingSchema(
                symbol=symbol,
                market="CN",
                data_source="akshare",
                trade_date=td,
                exchange=exchange,
                amount_unit="CNY" if verified else None,
                unit_conversion="native_yuan_no_rescale" if verified else None,
                source_response_date=response_date,
                date_evidence_status=date_status,
                unit_evidence=unit_evidence if isinstance(unit_evidence, dict) else None,
                monetary_unit=native_unit,
                margin_balance=_safe_float(get("融资余额") if pd.notna(get("融资余额")) else get("rzye")),
                short_balance=_safe_float(get("融券余额") if pd.notna(get("融券余额")) else get("rqye")),
                margin_buy_amount=_safe_float(get("融资买入额") if pd.notna(get("融资买入额")) else get("融资买入金额") if pd.notna(get("融资买入金额")) else get("rz_buy")),
                margin_repay_amount=_safe_float(get("融资偿还额") if pd.notna(get("融资偿还额")) else get("融资偿还金额") if pd.notna(get("融资偿还金额")) else get("rzche")),
                margin_repay_amount_derived=_safe_float(get("margin_repay_amount_derived")),
                margin_repay_derivation_status=_safe_str(get("margin_repay_derivation_status")),
                margin_repay_derivation=get("margin_repay_derivation") if isinstance(get("margin_repay_derivation"), dict) else None,
                short_sell_volume=_safe_float(get("融券卖出量", "") or get("rq_sell")),
                total_balance=_safe_float(get("融资融券余额") if pd.notna(get("融资融券余额")) else get("rzrqye")),
            ))
        return results

    def adapt_dragon_tiger(self, raw: Any) -> List[DragonTigerSchema]:
        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if df.empty:
            return []
        results = []
        for _, row in df.iterrows():
            get = row.get
            raw_symbol = _safe_str(
                get("代码", "") or get("股票代码", "") or get("symbol", "")
            )
            td = _parse_date(
                get("上榜日", "") or get("日期", "") or get("trade_date", "")
            )
            if not raw_symbol or not td:
                logger.warning("AKShare dragon_tiger 行缺少代码或上榜日，跳过该行")
                continue
            results.append(DragonTigerSchema(
                symbol=raw_symbol.zfill(6),
                market="CN",
                data_source="akshare",
                trade_date=td,
                name=_safe_str(get("名称", "") or get("name", "")),
                close=_safe_float(get("收盘价", "") or get("close")),
                pct_chg=_safe_float(get("涨跌幅", "") or get("change_pct")),
                # stock_lhb_detail_em 的唯一业务维度是“上榜原因”；“解读”
                # 只是机构参与摘要，不能优先用于唯一键。direction 是本地
                # v1.4 历史字段，仅作为迁移兼容输入。
                reason=_safe_str(
                    get("上榜原因", "")
                    or get("解读", "")
                    or get("reason", "")
                    or get("direction", "")
                )
                or "",
                buy_amount=_safe_float(
                    get("龙虎榜买入额", "") or get("买入额", "") or get("buy")
                ),
                sell_amount=_safe_float(
                    get("龙虎榜卖出额", "") or get("卖出额", "") or get("sell")
                ),
                net_amount=_safe_float(
                    get("龙虎榜净买额", "") or get("净额", "") or get("net")
                ),
            ))
        return results

    def adapt_block_trade(self, raw: Any) -> List[BlockTradeSchema]:
        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if df.empty:
            return []
        results = []
        # 东方财富可能返回多笔成交要素完全相同、但确实分别披露的交易。
        # API 的“序号”会随查询区间变化，不能直接作为稳定主键；改为在相同
        # 业务要素内生成从 1 开始的出现序号，跨不同查询窗口仍保持稳定。
        occurrence_counts: Dict[tuple, int] = {}
        for _, row in df.iterrows():
            get = row.get
            raw_symbol = _safe_str(
                get("证券代码", "") or get("代码", "") or get("symbol", "")
            )
            td = _parse_date(
                get("交易日期", "") or get("成交日期", "") or get("trade_date", "")
            )
            if not raw_symbol or not td:
                logger.warning("AKShare block_trade 行缺少证券代码或交易日期，跳过该行")
                continue
            symbol = raw_symbol.zfill(6)
            price = _safe_float(get("成交价", "") or get("price"))
            volume = _safe_float(get("成交量", "") or get("volume"))
            amount = _safe_float(get("成交额", "") or get("amount"))
            buyer = _safe_str(get("买方营业部", "") or get("buyer", "")) or ""
            seller = _safe_str(get("卖方营业部", "") or get("seller", "")) or ""
            occurrence_key = (
                symbol, td, buyer, seller, price, volume, amount,
            )
            trade_seq = occurrence_counts.get(occurrence_key, 0) + 1
            occurrence_counts[occurrence_key] = trade_seq
            results.append(BlockTradeSchema(
                symbol=symbol,
                market="CN",
                data_source="akshare",
                trade_date=td,
                name=_safe_str(
                    get("证券简称", "") or get("证券名称", "")
                    or get("名称", "") or get("name", "")
                ),
                price=price,
                # stock_dzjy_mrmx 当前返回值已经是“股/元”，不再二次乘 10000。
                volume=volume,
                amount=amount,
                buyer=buyer,
                seller=seller,
                trade_seq=trade_seq,
            ))
        return results

    def adapt_corporate_actions(self, raw: Any) -> List[CorporateActionsSchema]:
        """标准化巨潮/东方财富分红送配数据；比例字段均按“每10股”换算。"""
        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if df.empty:
            return []
        results: List[CorporateActionsSchema] = []
        for _, row in df.iterrows():
            get = row.get
            symbol = str(get("symbol", "") or get("代码", "")).zfill(6)
            ex_date = _parse_date(get("除权日") or get("除权除息日"))
            if not ex_date:
                continue
            announce_date = _parse_date(
                get("实施方案公告日期") or get("预案公告日") or get("最新公告日期")
            )
            record_date = _parse_date(get("股权登记日"))
            pay_date = _parse_date(get("派息日"))
            cash_per_ten = _safe_float(
                get("派息比例") or get("现金分红-现金分红比例")
            )
            bonus_per_ten = _safe_float(
                get("送股比例") or get("送转股份-送股比例")
            ) or 0.0
            transfer_per_ten = _safe_float(
                get("转增比例") or get("送转股份-转股比例")
            ) or 0.0

            if cash_per_ten and cash_per_ten > 0:
                results.append(CorporateActionsSchema(
                    symbol=symbol, market="CN", data_source="akshare",
                    ex_date=ex_date, record_date=record_date, pay_date=pay_date,
                    action_type="cash_dividend", amount=cash_per_ten / 10,
                    currency="CNY", announce_date=announce_date,
                ))
            issued = bonus_per_ten + transfer_per_ten
            if issued > 0:
                results.append(CorporateActionsSchema(
                    symbol=symbol, market="CN", data_source="akshare",
                    ex_date=ex_date, record_date=record_date,
                    action_type="bonus_issue", ratio_from=10.0,
                    ratio_to=10.0 + issued, announce_date=announce_date,
                ))
        return results
