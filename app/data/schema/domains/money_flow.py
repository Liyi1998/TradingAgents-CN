from dataclasses import dataclass
from typing import Optional

from app.data.schema.base.common_fields import CommonFields


@dataclass
class MoneyFlowSchema(CommonFields):
    """资金流向 — 个股主力/散户资金净流入。"""

    trade_date: Optional[str] = None
    main_net_inflow: Optional[float] = None        # 主力净流入（元）
    main_net_inflow_pct: Optional[float] = None     # 主力净流入占比 %（仅 AKShare 提供，Tushare 无此字段为 None）
    huge_net_inflow: Optional[float] = None         # 超大单净流入（元）
    large_net_inflow: Optional[float] = None        # 大单净流入（元）
    medium_net_inflow: Optional[float] = None       # 中单净流入（元）
    small_net_inflow: Optional[float] = None        # 小单净流入（元）
    # 来源相同不等于统计口径相同；消费者必须按以下契约分组，不能拼接。
    flow_basis: Optional[str] = None
    flow_source: Optional[str] = None
    flow_api: Optional[str] = None
    flow_pct_unit: Optional[str] = None
    flow_date_verified: Optional[bool] = None
    flow_date_basis: Optional[str] = None
    reported_flow_ratio: Optional[float] = None
    reported_flow_ratio_unit: Optional[str] = None

    def to_db_doc(self):
        """资金字段是单版本快照；显式空值阻止换源后复用旧口径字段。"""
        doc = super().to_db_doc()
        for name in MONEY_FLOW_SNAPSHOT_FIELDS:
            doc.setdefault(name, None)
        return doc


MONEY_FLOW_VALUE_FIELDS = (
    "main_net_inflow", "huge_net_inflow", "large_net_inflow",
    "medium_net_inflow", "small_net_inflow",
)
MONEY_FLOW_SNAPSHOT_FIELDS = MONEY_FLOW_VALUE_FIELDS + (
    "main_net_inflow_pct", "flow_basis", "flow_source", "flow_api",
    "amount_unit", "flow_pct_unit", "flow_date_verified", "flow_date_basis",
    "reported_flow_ratio", "reported_flow_ratio_unit",
    "asset_type", "exchange", "volume_unit", "unit_conversion",
)


def normalize_money_flow_symbol(value) -> str:
    """Return a real six-digit CN identity, never a fabricated 000000 key."""
    if value is None or isinstance(value, bool):
        raise ValueError("missing/invalid money-flow symbol")
    text = str(value).strip().upper()
    if text.endswith((".SH", ".SZ", ".BJ")):
        text = text[:-3]
    if not text.isascii() or not text.isdigit() or not 1 <= len(text) <= 6:
        raise ValueError("invalid money-flow symbol")
    symbol = text.zfill(6)
    if symbol == "000000":
        raise ValueError("invalid money-flow symbol 000000")
    return symbol
