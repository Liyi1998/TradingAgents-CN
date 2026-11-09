from dataclasses import dataclass
from typing import Optional

from app.data.schema.base.common_fields import CommonFields


@dataclass
class IntradayQuotesSchema(CommonFields):
    """分钟线行情 — 盘中分时 OHLCV。"""

    datetime: Optional[str] = None       # 交易时间（YYYY-MM-DD HH:MM:SS）
    freq: Optional[str] = None           # 频率（1/5/15/30/60 min）
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None       # 成交量（股）
    amount: Optional[float] = None       # 成交额（元）
    pct_chg: Optional[float] = None      # 涨跌幅 %
    ohlc_kind: Optional[str] = None     # native_bar, not sampled last prices
    price_basis: Optional[str] = None

    def to_db_doc(self):
        doc = super().to_db_doc()
        if self.ohlc_kind == "native_bar":
            # Replacing a different provider's same-time bar must not retain its
            # old turnover under the new provider identity.
            doc["amount"] = self.amount
            doc["amount_unit"] = self.amount_unit
        return doc
