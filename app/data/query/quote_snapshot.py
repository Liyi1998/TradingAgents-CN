"""Keep every displayed quote field tied to one source observation."""
from datetime import date, datetime

from app.data.core.requirements import finite_number
from app.data.query.price_reference import verified_spot
from app.data.storage.mongo.repositories.daily_quotes_repo import trusted_raw_quote


def ingestion_snapshot(symbol, record, *, source=None, trade_date=None, updated_at=None):
    """Build a replacement from this observation only; never inherit old proof."""
    row = {k: v for k, v in record.items() if k != '_id'}
    row['symbol'] = symbol
    row['data_source'] = row.get('data_source') or source
    row['trade_date'] = row.get('trade_date') or trade_date
    if updated_at is not None:
        row['updated_at'] = updated_at
    # Canonical providers emit both aliases. Legacy close-only snapshots must
    # clear last_price/time instead of retaining fields from another fetch.
    if 'last_price' not in row:
        row['last_price'] = row.get('close')
    if 'last_volume' not in row:
        row['last_volume'] = row.get('volume')
    return row


def quote_view(symbol: str, basic: dict, quote: dict, daily: dict, now: datetime) -> dict:
    day = str(quote.get('trade_date') or '')
    spot = verified_spot(quote, symbol, day, now) if day else None
    try:
        daily_day = date.fromisoformat(str(daily.get('trade_date') or ''))
    except ValueError:
        daily_day = None
    usable_daily = (daily.get('symbol') == symbol and trusted_raw_quote(daily)
                    and daily.get('period') == 'daily' and daily_day is not None
                    and finite_number(daily.get('close')) and daily['close'] > 0
                    and daily_day <= now.date())
    if spot and (not usable_daily or day >= str(daily.get('trade_date') or '')):
        row, price, kind = quote, spot['price'], 'spot_snapshot'
    elif usable_daily:
        row, price, kind = daily, daily['close'], 'historical_daily'
    else:
        row, price, kind = {}, None, 'unavailable'
    def number(key):
        value = row.get(key)
        return value if finite_number(value) else None
    prev, pct = number('pre_close'), number('pct_chg')
    if pct is None and price is not None and prev is not None and prev > 0:
        pct = round((price / prev - 1) * 100, 2)
    high, low = number('high'), number('low')
    amplitude = round((high-low)/prev*100, 2) if high is not None and low is not None and prev is not None and prev > 0 else None
    return {'symbol': symbol, 'name': basic.get('name'), 'market': basic.get('market') or 'CN',
        'price': price, 'change_percent': pct, 'prev_close': prev,
        **{key: number(key) for key in ('amount', 'volume', 'open', 'high', 'low', 'turnover_rate')},
        'amplitude': amplitude, 'trade_date': row.get('trade_date'),
        'quote_observed_at': row.get('quote_observed_at') if kind == 'spot_snapshot' else None,
        'quote_kind': kind, 'updated_at': row.get('updated_at'), 'data_source': row.get('data_source'),
        'price_basis': 'raw', 'currency': 'CNY', 'volume_unit': row.get('volume_unit'), 'amount_unit': row.get('amount_unit')}
