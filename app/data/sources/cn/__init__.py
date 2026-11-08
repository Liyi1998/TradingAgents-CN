"""A 股数据源入口 — Provider/Adapter 工厂 + 能力注册。"""

from typing import Optional

from app.data.sources.base.provider import BaseProvider
from app.data.sources.base.adapter import BaseAdapter


def get_cn_provider(source_name: str) -> Optional[BaseProvider]:
    if source_name in ("akshare", "ak"):
        from .akshare.provider import AKShareCNProvider
        return AKShareCNProvider()
    elif source_name in ("baostock", "bst"):
        from .baostock.provider import BaoStockCNProvider
        return BaoStockCNProvider()
    elif source_name in ("duckdb", "jince_duckdb"):
        from .duckdb.provider import JinceDuckDBCNProvider
        return JinceDuckDBCNProvider()
    elif source_name in ("tencent", "tencent_cn"):
        from .tencent.provider import TencentCNProvider
        return TencentCNProvider()
    elif source_name in ("sina", "sina_cn"):
        from .sina.provider import SinaCNProvider
        return SinaCNProvider()
    return None


def get_cn_adapter(source_name: str) -> Optional[BaseAdapter]:
    if source_name in ("akshare", "ak"):
        from .akshare.adapter import AKShareCNAdapter
        return AKShareCNAdapter()
    elif source_name in ("baostock", "bst"):
        from .baostock.adapter import BaoStockCNAdapter
        return BaoStockCNAdapter()
    elif source_name in ("duckdb", "jince_duckdb"):
        from .duckdb.adapter import JinceDuckDBCNAdapter
        return JinceDuckDBCNAdapter()
    elif source_name in ("tencent", "tencent_cn"):
        from .tencent.adapter import TencentCNAdapter
        return TencentCNAdapter()
    elif source_name in ("sina", "sina_cn"):
        from .sina.adapter import SinaCNAdapter
        return SinaCNAdapter()
    return None
