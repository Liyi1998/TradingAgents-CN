from dataclasses import dataclass
from typing import Optional

from app.data.schema.base.common_fields import CommonFields


@dataclass
class AdjFactorsSchema(CommonFields):
    """复权因子。"""

    trade_date: Optional[str] = None
    adj_factor: Optional[float] = None       # 复权因子
    fore_adj_factor: Optional[float] = None  # 前复权因子
    back_adj_factor: Optional[float] = None  # 后复权因子
    # CN v1: adj_factor/fore_adj_factor multiply raw prices. Raw provider
    # divisors/event values are retained separately and never consumed as-is.
    raw_factor: Optional[float] = None
    adjustment_basis: Optional[str] = None
    adjustment_contract: Optional[str] = None
    adjustment_method: Optional[str] = None
    adjustment_series_id: Optional[str] = None
    record_type: Optional[str] = None        # event, not an exact daily observation
    coverage_end: Optional[str] = None      # checked extraction horizon

    def to_db_doc(self) -> dict:
        doc = super().to_db_doc()
        if self.market == "CN" and self.adjustment_contract:
            # The natural key intentionally excludes source. A replacement
            # contract must clear absent auxiliary values rather than retain
            # an earlier provider's hfq/fore/raw values under the new label.
            for field in ("adj_factor", "fore_adj_factor", "back_adj_factor",
                          "raw_factor", "adjustment_basis", "adjustment_contract",
                          "adjustment_method", "adjustment_series_id",
                          "record_type", "coverage_end"):
                doc[field] = doc.get(field)
        return doc
