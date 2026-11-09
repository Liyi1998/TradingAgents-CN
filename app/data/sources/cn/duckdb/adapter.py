"""金策智算 DuckDB 原始行情到标准 Schema 的转换。"""

from typing import Any, List

import pandas as pd

from app.data.schema.base.types import _parse_date, _safe_float
from app.data.schema.domains.daily_quotes import DailyQuotesSchema
from app.data.sources.base.adapter import BaseAdapter


class JinceDuckDBCNAdapter(BaseAdapter):
    def __init__(self, provider=None):
        super().__init__(provider=provider, market="CN", source_name="duckdb")

    def adapt_daily_quotes(self, raw: Any) -> List[DailyQuotesSchema]:
        frame = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if frame.empty:
            return []
        frame = frame.copy()
        symbol_col = "symbol" if "symbol" in frame.columns else "code"
        frame["_symbol"] = (
            frame[symbol_col]
            .astype(str)
            .str.upper()
            .str.replace(r"\.(SH|SZ|BJ)$", "", regex=True)
            .str.replace(r"^(SH|SZ|BJ)", "", regex=True)
            .str.zfill(6)
        )
        frame["trade_date"] = frame["trade_date"].astype(str).str[:10]
        frame = frame.sort_values(["_symbol", "trade_date"]).reset_index(drop=True)
        if "pre_close" not in frame.columns:
            frame["pre_close"] = frame.groupby("_symbol", sort=False)["close"].shift(1)
        if "change" not in frame.columns:
            frame["change"] = frame["close"] - frame["pre_close"]
        if "pct_chg" not in frame.columns:
            frame["pct_chg"] = frame["change"] / frame["pre_close"] * 100
        records: List[DailyQuotesSchema] = []
        for _, row in frame.iterrows():
            get = row.get
            trade_date = _parse_date(get("trade_date"))
            if not trade_date:
                continue
            records.append(
                DailyQuotesSchema(
                    symbol=str(get("_symbol", "")).zfill(6),
                    market="CN",
                    data_source="duckdb",
                    trade_date=trade_date,
                    period="daily",
                    open=_safe_float(get("open")),
                    high=_safe_float(get("high")),
                    low=_safe_float(get("low")),
                    close=_safe_float(get("close")),
                    pre_close=_safe_float(get("pre_close")),
                    change=_safe_float(get("change")),
                    pct_chg=_safe_float(get("pct_chg")),
                    volume=_safe_float(get("volume")),
                    amount=_safe_float(get("amount")),
                )
            )
        return records
