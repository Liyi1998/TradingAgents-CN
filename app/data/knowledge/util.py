"""知识库工具函数：publish_ts 解析与 symbol 归一。"""

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

# symbol 归一化的唯一权威在 app/data/core/symbols.py；此处转发保持既有导入路径
from app.data.core.symbols import normalize_symbol as _normalize

# 时间键用数值（epoch 秒）而非字符串：规避 Chroma where 区间比较的字符串版本差异
_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%Y%m%d",
)


def market_timezone(market=None):
    return ZoneInfo({"CN": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "US": "America/New_York"}.get(market, "UTC"))


def parse_publish_ts(value: Optional[str], market=None) -> Optional[float]:
    """发布时间字符串 → epoch 秒。

    带偏移时间保留真实时点；无偏移按市场时区解释（未传市场兼容旧 UTC）。
    无法解析返回 None。
    """
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = None
        for fmt in _TIME_FORMATS:
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=market_timezone(market))
    return dt.timestamp()


def end_of_day_ts(date_str: str, market=None) -> Optional[float]:
    """日期串（YYYY-MM-DD）→ 当日 23:59:59 的 epoch 秒（as_of 截断点）。"""
    ts = parse_publish_ts(date_str, market)
    if ts is None:
        return None
    dt = datetime.fromtimestamp(ts, tz=market_timezone(market))
    dt = dt.replace(hour=23, minute=59, second=59, microsecond=0)
    return dt.timestamp()


def normalize_symbol(symbol: str, market: str) -> str:  # noqa: F811 — 转发权威实现
    """股票代码归一（转发 app.data.core.symbols.normalize_symbol）。"""
    return _normalize(symbol, market)
