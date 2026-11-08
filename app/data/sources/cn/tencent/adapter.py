"""腾讯财经 A 股行情到标准 Schema 的转换。"""

from typing import Any, List

import pandas as pd

from app.data.schema.base.types import _safe_float
from app.data.schema.domains.basic_info import StockBasicInfoSchema
from app.data.schema.domains.daily_indicators import DailyIndicatorsSchema
from app.data.schema.domains.market_quotes import MarketQuotesSchema
from app.data.schema.domains.intraday_quotes import IntradayQuotesSchema
from app.data.sources.base.adapter import BaseAdapter
from app.data.sources.cn.stock_name_utils import infer_exchange


class TencentCNAdapter(BaseAdapter):
    def __init__(self, provider=None):
        super().__init__(provider=provider, market="CN", source_name="tencent")

    def adapt_basic_info(self, raw: Any) -> List[StockBasicInfoSchema]:
        frame = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        records: List[StockBasicInfoSchema] = []
        for _, row in frame.iterrows():
            symbol = str(row.get("symbol", "")).zfill(6)
            exchange = infer_exchange(symbol)
            records.append(
                StockBasicInfoSchema(
                    symbol=symbol,
                    market="CN",
                    data_source="tencent",
                    name=str(row.get("name") or ""),
                    full_symbol=f"{symbol}.{exchange}" if exchange else symbol,
                    exchange=exchange,
                    currency="CNY",
                )
            )
        return records

    def adapt_market_quotes(self, raw: Any) -> List[MarketQuotesSchema]:
        frame = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        records: List[MarketQuotesSchema] = []
        for _, row in frame.iterrows():
            get = row.get
            price = _safe_float(get("price"))
            volume = _safe_float(get("volume"))
            update_time = str(get("update_time") or "")
            from datetime import datetime
            from zoneinfo import ZoneInfo
            try:
                observed = datetime.strptime(update_time, "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo("Asia/Shanghai")).isoformat()
            except ValueError:
                observed = None
            trade_date = (
                f"{update_time[:4]}-{update_time[4:6]}-{update_time[6:8]}"
                if len(update_time) >= 8 and update_time[:8].isdigit()
                else None
            )
            records.append(
                MarketQuotesSchema(
                    symbol=str(get("symbol", "")).zfill(6),
                    exchange=str(get("exchange") or ""), asset_type=str(get("asset_type") or "stock"),
                    volume_unit="share", amount_unit="CNY", unit_conversion="tencent:lot*100,CNY_10k*10000",
                    market="CN",
                    data_source="tencent",
                    open=_safe_float(get("open")),
                    high=_safe_float(get("high")),
                    low=_safe_float(get("low")),
                    close=price,
                    pre_close=_safe_float(get("pre_close")),
                    pct_chg=_safe_float(get("pct_chg")),
                    volume=volume,
                    amount=_safe_float(get("amount")),
                    turnover_rate=_safe_float(get("turnover_rate")),
                    last_price=price,
                    last_volume=volume,
                    last_updated=update_time,
                    trade_date=trade_date,
                    quote_source_type="realtime",
                    quote_observed_at=observed,
                    quote_time_basis="source_quote_timestamp" if observed else None,
                    price_basis="raw", currency="CNY",
                )
            )
        return records

    def adapt_daily_indicators(self, raw: Any) -> List[DailyIndicatorsSchema]:
        import numpy as np

        from app.data.schema.domains.daily_indicators import finite_indicator, indicator_quality
        from datetime import date

        frame = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        records: List[DailyIndicatorsSchema] = []
        for _, row in frame.iterrows():
            get = row.get
            update_time = str(get("update_time") or "")
            trade_date = (
                f"{update_time[:4]}-{update_time[4:6]}-{update_time[6:8]}"
                if len(update_time) >= 8 and update_time[:8].isdigit()
                else None
            )
            if not trade_date:
                continue
            try:
                trade_date = date.fromisoformat(trade_date).isoformat()
            except ValueError:
                continue
            def number(field):
                raw_value = get(field)
                if isinstance(raw_value, (bool, np.bool_)):
                    return None
                value = _safe_float(raw_value)
                return value if finite_indicator(value) else None

            values = {field: number(field) for field in (
                "pe_reported", "pb", "turnover_rate", "total_mv", "circ_mv")}
            # Compatibility for saved raw frames: do not certify their old alias.
            if values["pe_reported"] is None:
                values["pe_reported"] = number("pe_ttm")
            provenance = {
                field: {"data_source": "tencent", "as_of": trade_date,
                        "method": "tencent_quote_timestamp", "date_verified": True,
                        "basis": "unknown" if field == "pe_reported" else "vendor_pb" if field == "pb" else "vendor_reported"}
                for field, value in values.items() if finite_indicator(value)
            }
            quality = indicator_quality(values, provenance, {
                "pe_ttm", "volume_ratio", "dividend_yield", "dividend_yield_ttm"})
            records.append(
                DailyIndicatorsSchema(
                    symbol=str(get("symbol", "")).zfill(6),
                    market="CN",
                    data_source="tencent",
                    trade_date=trade_date,
                    **values, field_provenance=provenance, data_quality=quality,
                )
            )
        return records

    def adapt_intraday_quotes(self, raw: Any) -> List[IntradayQuotesSchema]:
        frame = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        records: List[IntradayQuotesSchema] = []
        for _, row in frame.iterrows():
            records.append(IntradayQuotesSchema(
                symbol=str(row.get("symbol", "")).zfill(6),
                market="CN", data_source="tencent",
                datetime=str(row.get("datetime") or ""),
                freq=str(row.get("freq") or "1min"),
                open=_safe_float(row.get("open")), high=_safe_float(row.get("high")),
                low=_safe_float(row.get("low")), close=_safe_float(row.get("close")),
                volume=_safe_float(row.get("volume")), amount=_safe_float(row.get("amount")),
                ohlc_kind=row.get("ohlc_kind"), price_basis=row.get("price_basis"),
                volume_unit=row.get("volume_unit"), amount_unit=row.get("amount_unit"),
                unit_conversion=row.get("unit_conversion"),
            ))
        return records
