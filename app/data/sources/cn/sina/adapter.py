"""新浪财经资金流到标准 Schema 的转换。"""

from typing import Any, List

import pandas as pd

from app.data.schema.base.types import _safe_float
from app.data.schema.domains.money_flow import MoneyFlowSchema
from app.data.sources.base.adapter import BaseAdapter


class SinaCNAdapter(BaseAdapter):
    def __init__(self, provider=None):
        super().__init__(provider=provider, market="CN", source_name="sina")

    def adapt_money_flow(self, raw: Any) -> List[MoneyFlowSchema]:
        import numpy as np

        from app.data.schema.base.types import _parse_date
        from app.data.schema.domains.money_flow import normalize_money_flow_symbol
        from app.data.sources.cn.stock_name_utils import infer_exchange

        frame = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        records: List[MoneyFlowSchema] = []
        for _, row in frame.iterrows():
            try:
                symbol = normalize_money_flow_symbol(row.get("symbol"))
            except ValueError:
                continue

            def number(key):
                value = row.get(key)
                return None if isinstance(value, (bool, np.bool_)) else _safe_float(value)

            # netamount 为全部大单口径净额，r0_net 为超大单净额。
            # 新浪不提供与东财完全一致的中/小单分拆，保留为 None。
            records.append(
                MoneyFlowSchema(
                    symbol=symbol,
                    market="CN",
                    data_source="sina",
                    exchange=infer_exchange(symbol),
                    trade_date=_parse_date(row.get("opendate")),
                    main_net_inflow=number("netamount"),
                    main_net_inflow_pct=None,  # ratioamount 单位尚未独立核实
                    huge_net_inflow=number("r0_net"),
                    flow_basis="sina_large_order_v1", flow_source="sina",
                    amount_unit="CNY", flow_pct_unit=None,
                    flow_api="MoneyFlow.ssl_qsfx_zjlrqs",
                    flow_date_verified=True, flow_date_basis="source_row_date",
                    reported_flow_ratio=number("ratioamount"),
                    reported_flow_ratio_unit="unverified",
                )
            )
        return records
