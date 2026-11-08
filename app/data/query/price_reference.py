"""Separate observed spot prices from completed bars and trading proposals."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.data.core.requirements import finite_number

TZ = ZoneInfo('Asia/Shanghai')


def verified_spot(row: dict, symbol: str, as_of: str, now: datetime) -> dict | None:
    """A cache write time is never proof of a quote's observation time."""
    price = row.get('last_price')
    # Older ingestion writers could leave a previous last_price alongside a
    # newer close/time. Such a mixed record is not a verified quote.
    if row.get('close') is not None and (not finite_number(row['close'])
            or not finite_number(price) or abs(row['close'] - price) > .0001):
        return None
    if (row.get('symbol') != symbol or row.get('market') != 'CN'
            or row.get('asset_type') != 'stock' or row.get('data_source') != 'tencent'
            or row.get('price_basis') != 'raw' or row.get('currency') != 'CNY'
            or row.get('quote_time_basis') != 'source_quote_timestamp'
            or not finite_number(price) or price <= 0):
        return None
    try:
        observed = datetime.fromisoformat(row['quote_observed_at'])
        if observed.tzinfo is None:
            return None
        observed = observed.astimezone(TZ)
        day = datetime.fromisoformat(as_of[:10]).date()
    except (ValueError, TypeError, KeyError):
        return None
    now = now.astimezone(TZ)
    if observed.date() != day or row.get('trade_date') != day.isoformat() or observed > now:
        return None
    # Tencent also updates the quote snapshot after the closing session. Its
    # timestamp describes the source snapshot, NOT an exchange execution time.
    if observed.time() < time(9, 15):
        return None
    freshness_clock = now.replace(hour=11, minute=30, second=0, microsecond=0) if time(11, 30) < now.time() < time(13) else now
    if day == now.date() and now.time() < time(15) and (freshness_clock - observed).total_seconds() > 900:
        return None
    if day == now.date() and now.time() >= time(15) and observed.time() < time(14, 55):
        return None
    return {'price': float(price), 'observed_at': observed.isoformat(), 'trade_date': day.isoformat(),
            'data_source': row['data_source'], 'currency': 'CNY', 'price_basis': 'raw',
            'kind': 'spot_snapshot', 'session': 'post_close' if observed.time() >= time(15) else 'intraday',
            'label': '盘后报价快照（非官方收盘确认）' if observed.time() >= time(15) else '已验证报价快照（非成交保证）'}


async def collect_price_reference(di, market: str, symbol: str, context: dict) -> dict:
    """At most one bounded single-stock refresh; never request a market-wide batch."""
    import asyncio
    now = datetime.now(TZ)
    as_of = str(context.get('as_of') or context.get('analysis_date') or '')[:10]
    result = {'status': 'unavailable', 'spot': None, 'historical_close': None,
              'analysis_date': as_of, 'checked_at': now.isoformat(),
              'limitations': ['报价快照与完整日线独立；不混入历史均线，不自动授权买卖。']}
    if market != 'CN' or len(symbol) != 6 or not symbol.isdigit():
        result['reason'] = 'market_or_symbol_not_supported'
        return result
    try:
        day = datetime.fromisoformat(as_of).date()
    except ValueError:
        result['reason'] = 'analysis_date_unverified'
        return result
    if day > now.date():
        result['reason'] = 'future_analysis_date'
        return result
    filters = {'asset_type': 'stock', 'required_fields': ['last_price'],
               'contract': 'verified_spot_quote', 'quote_symbol': symbol,
               'quote_as_of': as_of}
    try:
        async with asyncio.timeout(10):
            if day == now.date():
                quote = await di.read_ready(market, 'market_quotes', symbol, timeout=8,
                    start_date=as_of, end_date=as_of, filters=filters)
            else:
                quote = await di.read(market, 'market_quotes', symbol,
                    start_date=as_of, end_date=as_of, filters=filters)
            rows = quote.get('data') or []
            rows = rows if isinstance(rows, list) else [rows]
            for row in rows:
                snapshot = verified_spot(row, symbol, as_of, datetime.now(TZ))
                if snapshot:
                    result['spot'] = snapshot
                    break
            result['quote_quality'] = quote.get('quality') or {}
    except Exception as exc:
        result['quote_error'] = type(exc).__name__
    # Keep the already cached, completed daily reference even if spot is unavailable.
    completed = context.get('last_completed_trade_date')
    if completed and completed <= as_of:
        try:
            async with asyncio.timeout(3):
                daily = await di.read(market, 'daily_quotes', symbol,
                    start_date=(day - timedelta(days=30)).isoformat(), end_date=completed,
                    filters={'required_fields': ['close'], 'expected_dates': [],
                             'contract': 'verified_raw_daily_quotes'})
                rows = daily.get('data') or []
                rows = rows if isinstance(rows, list) else [rows]
                valid = [r for r in rows if r.get('symbol') == symbol and r.get('price_basis') == 'raw'
                         and r.get('data_source') and str(r.get('trade_date') or '') <= completed
                         and finite_number(r.get('close')) and r['close'] > 0]
                if valid:
                    row = max(valid, key=lambda r: r['trade_date'])
                    result['historical_close'] = {'price': float(row['close']), 'trade_date': row['trade_date'],
                        'data_source': row['data_source'], 'currency': 'CNY', 'price_basis': 'raw',
                        'kind': 'completed_daily_close', 'label': '已完成日线收盘（非实时报价）'}
        except Exception as exc:
            result['daily_error'] = type(exc).__name__
    result['status'] = 'spot_available' if result['spot'] else 'historical_only' if result['historical_close'] else 'unavailable'
    if not result['spot']:
        result['limitations'].append('未取得分析日已验证报价；不将上一交易日收盘称为当日实时价格。')
    return result
