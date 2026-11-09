"""Standalone local daily-bar bridge. No Jince imports, jobs, network or writes.

Only the explicitly supplied existing database's main.dat_day table is read.
The three GET contracts match TradingAgents' existing DuckDB HTTP provider.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, timedelta
import logging
import math
from pathlib import Path
import re
from threading import BoundedSemaphore, Timer
from time import monotonic

import duckdb
from fastapi import FastAPI, HTTPException, Query, Request
from starlette.middleware.trustedhost import TrustedHostMiddleware

logger = logging.getLogger(__name__)
READ_CONFIG = {
    'threads': '2', 'memory_limit': '512MB', 'temp_directory': '',
    'max_temp_directory_size': '0B',
    # The wheel's built-in ICU is required for the explicit Shanghai timezone.
    # Autoinstall stays disabled; table queries run with external access off.
    'autoinstall_known_extensions': 'false', 'allow_community_extensions': 'false',
    'allow_persistent_secrets': 'false',
}
SYMBOL = re.compile(r'^[0-9]{6}\.(SH|SZ|BJ)$')
DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
FIELDS = ('open', 'high', 'low', 'close', 'vol', 'amount')


def iso_date(value: str) -> date:
    if not DATE.fullmatch(value):
        raise HTTPException(422, 'Use an ISO YYYY-MM-DD date')
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, 'Invalid calendar date') from exc


def date_range(start: str, end: str) -> tuple[date, date]:
    first, last = iso_date(start), iso_date(end)
    if first > last or (last - first).days > 3660:
        raise HTTPException(422, 'Date range must be ordered and at most 3660 days')
    return first, last


def only_parameters(request: Request, allowed: set[str]) -> None:
    if set(request.query_params) - allowed or any(
        len(request.query_params.getlist(key)) != 1 for key in request.query_params
    ):
        raise HTTPException(422, 'Unsupported or repeated query parameter')


class DeadlineConnection:
    """Check between queries as well as interrupting a currently running query."""
    def __init__(self, connection, deadline):
        self.connection = connection
        self.deadline = deadline

    def check(self):
        if monotonic() >= self.deadline:
            raise HTTPException(503, 'Read-only query deadline exceeded')

    def execute(self, *args):
        self.check()
        result = self.connection.execute(*args)
        self.check()
        return result


@contextmanager
def open_read_only(database: Path, query_timeout: float = 30.0):
    """Missing/locked/unreadable databases fail closed; never retry as a writer."""
    connection = None
    timer = None
    deadline = monotonic() + query_timeout
    try:
        if not database.is_file():
            raise OSError('Daily database is unavailable')
        connection = duckdb.connect(str(database), read_only=True, config=READ_CONFIG)
        # DuckDB rejects setting temp_directory and disabling external access
        # in the same connect config. Disable it before any table is queried.
        connection.execute('SET enable_external_access = false')
        # Set after connect: built-in ICU loads during connection initialization.
        connection.execute("SET TimeZone = 'Asia/Shanghai'")
        bounded = DeadlineConnection(connection, deadline)
        bounded.check()
        timer = Timer(max(0, deadline - monotonic()), connection.interrupt)
        timer.daemon = True
        timer.start()
        # Reject a view with the same name; never execute user-defined view SQL.
        if bounded.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE schema_name='main' "
            "AND table_name='dat_day' AND NOT internal"
        ).fetchone()[0] != 1:
            raise OSError('Required daily table is unavailable')
        yield bounded
        bounded.check()
    except (duckdb.Error, OSError) as exc:
        logger.warning('Read-only daily database unavailable (%s)', type(exc).__name__)
        raise HTTPException(503, 'Read-only daily data unavailable; retry later') from exc
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        if connection is not None:
            connection.close()


def finite(value):
    if value is None:
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def normalize_volume(row: dict) -> tuple[float | None, str]:
    """Infer share/lot convention conservatively; ambiguous values remain null.

    Historical rows have mixed units. amount is CNY; the ratio to OHLC mean
    separates shares (~1) from lots (~100). This is evidence, not a unit tag.
    """
    volume, amount = finite(row['vol']), finite(row['amount'])
    prices = [finite(row[key]) for key in ('open', 'high', 'low', 'close')]
    if volume == 0 and amount == 0:
        return 0.0, 'zero_turnover'
    if volume is None or amount is None or volume <= 0 or amount <= 0 or any(
        value is None or value <= 0 for value in prices
    ):
        return None, 'unknown'
    ratio = amount / (volume * (sum(prices) / 4))
    if 0.25 <= ratio <= 4:
        return volume / 100, 'inferred_share_to_lot'
    if 25 <= ratio <= 400:
        return volume, 'inferred_lot'
    return None, 'unknown'


# Fixed column names only. Input values are always bound SQL parameters.
# Collapse equivalent same-civil-day rows BEFORE LAG and cursor pagination.
_CONFLICT = ' OR '.join(
    f"(count({key}) OVER w NOT IN (0, count(*) OVER w) OR "
    f"max({key}) OVER w - min({key}) OVER w > "
    f"greatest(1e-8, max(abs({key})) OVER w * 1e-9))" for key in FIELDS
)
BAR_SQL = """
WITH raw AS (
 SELECT code, CAST(trade_time AS DATE) AS trade_date, trade_time,
        open, high, low, close, vol, amount
 FROM main.dat_day
 WHERE CAST(trade_time AS DATE) BETWEEN ? AND ?
   AND regexp_full_match(code, '[0-9]{6}\\.(SH|SZ|BJ)')
   AND (? = '' OR code = ?) AND code >= ?
), ranked AS (
 SELECT *, row_number() OVER (PARTITION BY code, trade_date ORDER BY trade_time) AS rn,
        count(*) OVER w AS duplicate_count,
        (count(*) OVER w > 1 AND (""" + _CONFLICT + """)) AS duplicate_conflict
 FROM raw WINDOW w AS (PARTITION BY code, trade_date)
), daily AS (
 SELECT * FROM ranked WHERE rn = 1
), priced AS (
 SELECT *, lag(close) OVER w AS pre_close,
           lag(duplicate_conflict) OVER w AS prior_conflict
 FROM daily WINDOW w AS (PARTITION BY code ORDER BY trade_date)
)
SELECT code, trade_date, open, high, low, close, vol, amount, pre_close,
       duplicate_count, duplicate_conflict, prior_conflict
FROM priced
WHERE trade_date >= ? AND (code > ? OR (code = ? AND trade_date > ?))
ORDER BY code, trade_date LIMIT ?
"""


def read_bars(connection, first: date, last: date, limit: int,
              stock: str = '', cursor_code: str = '', cursor_date: date | None = None):
    result = connection.execute(BAR_SQL, [
        max(first, date.min + timedelta(days=10)) - timedelta(days=10),
        last, stock, stock, cursor_code,
        first, cursor_code, cursor_code, cursor_date or date.min, limit + 1,
    ])
    columns = [item[0] for item in result.description]
    raw = [dict(zip(columns, values)) for values in result.fetchall()]
    more = len(raw) > limit
    records = []
    for row in raw[:limit]:
        if row['duplicate_conflict'] or row['prior_conflict']:
            raise HTTPException(503, 'Conflicting duplicate daily bars; source repair required')
        volume, status = normalize_volume(row)
        record = {key: finite(row[key]) for key in FIELDS if key != 'vol'}
        previous, close = finite(row['pre_close']), finite(row['close'])
        change = finite(close - previous) if close is not None and previous is not None else None
        record.update(code=row['code'], trade_date=row['trade_date'].isoformat(),
                      vol=volume, volume_unit_status=status, pre_close=previous,
                      change=change, pct_chg=finite(change / previous * 100)
                      if change is not None and previous and previous > 0 else None,
                      duplicate_count=row['duplicate_count'])
        records.append(record)
    return records, more


def create_app(database: Path, query_timeout: float = 30.0) -> FastAPI:
    database = Path(database).absolute()
    if not 0 < query_timeout <= 60:
        raise ValueError('query_timeout must be in (0, 60] seconds')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=['127.0.0.1', 'localhost', 'host.docker.internal', 'testserver'])
    gate = BoundedSemaphore(1)

    @contextmanager
    def reader():
        if not gate.acquire(blocking=False):
            raise HTTPException(503, 'Read-only bridge busy; retry later')
        try:
            with open_read_only(database, query_timeout) as connection:
                yield connection
        finally:
            gate.release()

    @app.get('/api/duckdb/coverage')
    def coverage(request: Request):
        only_parameters(request, set())
        with reader() as connection:
            first, last, count = connection.execute(
                'SELECT min(CAST(trade_time AS DATE)), max(CAST(trade_time AS DATE)), '
                'count(*) FROM main.dat_day'
            ).fetchone()
        return {'status': 'success', 'read_only': True,
                'latest_trade_date': last.isoformat() if last else None,
                'tables': [{'interval': 'D', 'status': 'success',
                            'min_date': first.isoformat() if first else None,
                            'max_date': last.isoformat() if last else None,
                            'raw_rows': count}],
                'volume_unit': 'lot', 'volume_unit_basis': 'amount_price_inference'}

    @app.get('/api/duckdb/daily_quotes')
    def daily_quotes(request: Request, start_date: str, end_date: str,
                     limit: int = Query(20000, ge=1, le=50000),
                     cursor_code: str = '', cursor_date: str = ''):
        only_parameters(request, {'start_date', 'end_date', 'limit', 'cursor_code', 'cursor_date'})
        first, last = date_range(start_date, end_date)
        after_date = None
        if bool(cursor_code) != bool(cursor_date):
            raise HTTPException(422, 'Cursor code and date must be supplied together')
        if cursor_code:
            if not SYMBOL.fullmatch(cursor_code):
                raise HTTPException(422, 'Invalid cursor symbol')
            after_date = iso_date(cursor_date)
            if not first <= after_date <= last:
                raise HTTPException(422, 'Cursor date outside request range')
        with reader() as connection:
            records, more = read_bars(connection, first, last, limit,
                                      cursor_code=cursor_code, cursor_date=after_date)
        return {'status': 'success', 'records': records, 'count': len(records),
                'volume_unit': 'lot', 'volume_unit_basis': 'amount_price_inference',
                'has_more': more,
                'next_cursor': {'code': records[-1]['code'], 'trade_date': records[-1]['trade_date']}
                if more and records else None}

    @app.get('/api/backtest/kline_data')
    def kline_data(request: Request, stock: str, start: str, end: str):
        only_parameters(request, {'stock', 'start', 'end'})
        if not SYMBOL.fullmatch(stock):
            raise HTTPException(422, 'Use a six-digit symbol with .SH, .SZ or .BJ')
        first, last = date_range(start, end)
        with reader() as connection:
            records, more = read_bars(connection, first, last, 10000, stock=stock)
        if more:
            raise HTTPException(422, 'Too many daily bars')
        return {'status': 'success', 'stock': stock, 'volume_unit': 'lot',
                'volume_unit_basis': 'amount_price_inference',
                'candles': [{'time': row['trade_date'], **{key: row[key]
                             for key in ('open', 'high', 'low', 'close')}} for row in records],
                'volumes': [{'time': row['trade_date'], 'value': row['vol'],
                             'unit_status': row['volume_unit_status']} for row in records]}

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--port', type=int, default=18080)
    args = parser.parse_args()
    if not args.database.is_absolute() or not 1024 <= args.port <= 65535:
        parser.error('Use an absolute database path and unprivileged port')
    import uvicorn
    uvicorn.run(create_app(args.database), host='127.0.0.1', port=args.port,
                workers=1, access_log=False, proxy_headers=False, limit_concurrency=8)


if __name__ == '__main__':
    main()
