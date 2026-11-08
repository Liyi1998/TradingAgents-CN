from dataclasses import dataclass
import math
from numbers import Real
from typing import Dict, Optional

from app.data.schema.base.common_fields import CommonFields


INDICATOR_COVERAGE_FIELDS = (
    "pe_ttm", "pb", "volume_ratio", "dividend_yield", "dividend_yield_ttm",
)
INDICATOR_VALUE_FIELDS = INDICATOR_COVERAGE_FIELDS + (
    "pe_dynamic", "pe_reported", "ps_ttm", "turnover_rate", "turnover_rate_f",
    "total_mv", "circ_mv", "shares_outstanding", "float_shares",
    "southbound_holding", "southbound_holding_ratio",
)


def finite_indicator(value) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def indicator_quality(values: Dict, provenance: Dict, unsupported=()) -> Dict:
    """Field coverage is evidence, not a count of successfully stored documents."""
    statuses = {}
    for field in INDICATOR_COVERAGE_FIELDS:
        evidence = provenance.get(field) or {}
        if not finite_indicator(values.get(field)):
            statuses[field] = "unsupported" if field in unsupported else "missing"
        elif not evidence.get("date_verified"):
            statuses[field] = "unverified_date"
        elif field == "pe_ttm" and evidence.get("basis") != "ttm":
            statuses[field] = "unverified_basis"
        elif field == "dividend_yield_ttm" and evidence.get("basis") != "ttm":
            statuses[field] = "unverified_basis"
        elif field == "dividend_yield" and evidence.get("basis") != "annual":
            statuses[field] = "unverified_basis"
        else:
            statuses[field] = "available"
    available = [field for field, status in statuses.items() if status == "available"]
    return {
        "status": "complete" if len(available) == len(statuses) else "partial" if available else "insufficient",
        "field_status": statuses,
        "available_fields": available,
        "missing_fields": [field for field, status in statuses.items() if status in {"missing", "unsupported"}],
        "unverified_fields": [field for field, status in statuses.items() if status.startswith("unverified_")],
    }


@dataclass
class DailyIndicatorsSchema(CommonFields):
    """每日指标 — PE/PB/市值等。"""

    trade_date: Optional[str] = None
    pe_ttm: Optional[float] = None
    pe_dynamic: Optional[float] = None  # 动态 PE 不等于 TTM
    pe_reported: Optional[float] = None  # 来源未证实口径的 PE，不参与 TTM 分位
    pb: Optional[float] = None
    ps_ttm: Optional[float] = None
    turnover_rate: Optional[float] = None
    turnover_rate_f: Optional[float] = None  # 自由流通换手率
    total_mv: Optional[float] = None         # 总市值
    circ_mv: Optional[float] = None          # 流通市值
    volume_ratio: Optional[float] = None     # 量比
    dividend_yield: Optional[float] = None   # 股息率 %
    dividend_yield_ttm: Optional[float] = None  # TTM 股息率
    shares_outstanding: Optional[float] = None  # 总股本
    float_shares: Optional[float] = None        # 流通股本
    southbound_holding: Optional[float] = None       # 南向持股 (HK)
    southbound_holding_ratio: Optional[float] = None  # 南向持股占比 (HK)
    field_provenance: Optional[Dict] = None  # 每字段的来源、真实日期与指标口径
    data_quality: Optional[Dict] = None
