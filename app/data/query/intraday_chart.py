"""Ephemeral chart observations; never persisted as completed daily evidence."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.data.core.analysis_context import _calendar_days
from app.data.core.requirements import finite_number

CN = ZoneInfo('Asia/Shanghai')


def market_clock(calendar, now):
    local = now.astimezone(CN)
    days = _calendar_days(calendar)
    today, clock = local.date().isoformat(), local.time()
    opened = days.get(today)
    phase = 'unknown' if opened is None else 'closed'
    next_at = None
    if opened:
        if clock < time(9, 30):
            phase, next_at = 'pre_open', local.replace(hour=9, minute=30, second=0, microsecond=0)
        elif time(11, 30) <= clock < time(13):
            phase, next_at = 'lunch', local.replace(hour=13, minute=0, second=0, microsecond=0)
        elif clock < time(15):
            phase, next_at = 'trading', local + timedelta(seconds=30)
        elif clock < time(16):
            phase, next_at = 'settling', local + timedelta(seconds=60)
    if phase == 'closed':
        for offset in range(1, 31):
            day = (local.date()+timedelta(days=offset)).isoformat()
            if days.get(day) is None:
                break
            if days[day]:
                next_at = datetime.combine(local.date()+timedelta(days=offset), time(9, 30), CN)
                break
    return {'date': today, 'phase': phase, 'is_open': opened,
            'next_poll_at': next_at.isoformat() if next_at else None}


def intraday_observation(symbol, quote, completed_bars, session, now):
    """One source's entire OHLCV; an old close cannot become today's candle."""
    today = session['date']
    if any(bar['date'] == today for bar in completed_bars):
        return None, 'completed'
    if session.get('is_open') is not True or session['phase'] == 'pre_open':
        return None, 'not_trading'
    if (quote.get('symbol') != symbol or quote.get('quote_kind') != 'spot_snapshot'
            or quote.get('trade_date') != today or quote.get('price_basis') != 'raw'
            or quote.get('currency') != 'CNY' or quote.get('volume_unit') != 'share'
            or not quote.get('data_source')):
        return None, 'unavailable'
    try:
        observed = datetime.fromisoformat(quote['quote_observed_at'])
        if observed.tzinfo is None:
            return None, 'unavailable'
        observed = observed.astimezone(CN)
    except (ValueError, TypeError, KeyError):
        return None, 'unavailable'
    local = now.astimezone(CN)
    if observed.date().isoformat() != today or observed > local or observed.time() < time(9, 30):
        return None, 'unavailable'
    if any(not finite_number(quote.get(k)) or quote[k] <= 0 for k in ('open', 'high', 'low', 'price')):
        return None, 'unavailable'
    if not quote['low'] <= min(quote['open'], quote['price']) <= max(quote['open'], quote['price']) <= quote['high']:
        return None, 'unavailable'
    volume = quote.get('volume')
    if not finite_number(volume) or volume < 0:
        return None, 'unavailable'
    # During a break, freshness is measured against the last session boundary.
    # A 14:00 quote must not look current just because the exchange has closed.
    expected = local
    if session['phase'] == 'lunch':
        expected = local.replace(hour=11, minute=30, second=0, microsecond=0)
    elif local.time() >= time(15):
        expected = local.replace(hour=15, minute=0, second=0, microsecond=0)
    stale = (expected-observed).total_seconds() > 120
    status = 'intraday' if local.time() < time(15) else 'awaiting_daily'
    return {'date': today, 'open': quote['open'], 'high': quote['high'], 'low': quote['low'],
        'close': quote['price'], 'volume': volume, 'data_source': quote['data_source'],
        'observed_at': observed.isoformat(), 'status': status, 'stale': stale,
        'price_basis': 'raw', 'currency': 'CNY'}, 'delayed' if stale else status
