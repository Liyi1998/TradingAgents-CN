"""Shared market-local, close-and-ready date semantics for readers and patrols."""

from datetime import date, datetime, timedelta
from typing import Dict, Optional

# 各域收盘后数据可用的等待窗（市场本地时间）：
# 收盘后源端整理需要时间，巡检判定「应有数据」的时点 = 收盘 + 该窗口。
# 缺省 90 分钟，行情类源通常 30~60 分钟内可用，留裕量避免巡检与 cron 抢跑。
_DOMAIN_READY_AFTER_CLOSE: Dict[str, int] = {
    "CN": {"default": 90, "daily_indicators": 120, "adj_factors": 150,
           "money_flow": 90, "financial_data": 360},
    "HK": {"default": 90},
    "US": {"default": 90, "daily_indicators": 120, "adj_factors": 120},
}

# 市场收盘时间（本地）与自然日容忍度：
# daily 类域按交易日判定；basic_info/calendar/财务等自然日域按天判定。
_MARKET_CLOSE_LOCAL = {"CN": 15, "HK": 16, "US": 16}

def get_ready_after_close(market: str, domain: str) -> int:
    cfg = _DOMAIN_READY_AFTER_CLOSE.get(market, {})
    return cfg.get(domain, cfg.get("default", 90))


async def get_expected_trading_date(market: str, domain: str) -> Optional[date]:
    """计算「此刻源端应已具备数据」的最新交易日。

    从今天往回找第一个满足「交易日且已过收盘+就绪窗」的日期。
    今天是交易日但还没到就绪时点 → 昨个交易日；否则 → 今天。
    找不到日历（None）时返回 None，调用方按自然日逻辑兜底。
    """
    from app.data.core.market import get_market_timezone
    from app.data.core.domain import DataDomain

    if domain not in {d.value for d in DataDomain}:
        return None

    tz = get_market_timezone(market)
    now_local = datetime.now(tz)
    ready_minutes = get_ready_after_close(market, domain)
    close_hour = _MARKET_CLOSE_LOCAL.get(market, 16)

    # 就绪时点（本地）：收盘 + 就绪窗；超过它当日数据才「应有」
    ready_dt = now_local.replace(
        hour=close_hour, minute=0, second=0, microsecond=0
    ) + timedelta(minutes=ready_minutes)

    from app.data.storage.mongo.repositories.trade_calendar_repo import TradeCalendarRepo
    from app.data.schema.base.markets import MARKET_META, MarketType

    meta = MARKET_META.get(MarketType(market))
    exchange = meta.exchanges[0] if meta and meta.exchanges else ""
    repo = TradeCalendarRepo()

    candidate = now_local.date()
    # 最多回看 40 天，覆盖最长节假日连休 + 巡检长期停摆
    for _ in range(40):
        is_open = await repo.is_trading_day(candidate.isoformat(), exchange, market)
        if is_open:
            if candidate == now_local.date() and now_local < ready_dt:
                # 今日数据尚未就绪，继续回看上一交易日
                candidate = candidate - timedelta(days=1)
                continue
            return candidate
        candidate = candidate - timedelta(days=1)
    return None
