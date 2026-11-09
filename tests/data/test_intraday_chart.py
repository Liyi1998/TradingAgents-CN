"""Calendar/session and candle transition contracts, without provider mocks."""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.data.query.intraday_chart import intraday_observation, market_clock
from tests.data.test_quote_tracking_regression import quote
from app.data.query.quote_snapshot import quote_view

CN = ZoneInfo('Asia/Shanghai')
CALENDAR = [{'cal_date': '2026-10-' + str(day), 'is_open': day not in (24, 25)} for day in range(23, 31)]


def clock(stamp):
    return datetime.fromisoformat(stamp).replace(tzinfo=CN)


@pytest.mark.parametrize('stamp,phase,next_time', [
    ('2026-10-27T09:20', 'pre_open', '2026-10-27T09:30'),
    ('2026-10-27T09:30', 'trading', '2026-10-27T09:30:30'),
    ('2026-10-27T11:30', 'lunch', '2026-10-27T13:00'),
    ('2026-10-27T13:00', 'trading', '2026-10-27T13:00:30'),
    ('2026-10-27T15:00', 'settling', '2026-10-27T15:01'),
    ('2026-10-27T16:00', 'closed', '2026-10-28T09:30'),
    ('2026-10-24T10:00', 'closed', '2026-10-26T09:30'),
])
def test_verified_sessions_schedule_only_appropriate_market_refreshes(stamp, phase, next_time):
    session = market_clock(CALENDAR, clock(stamp))
    assert session['phase'] == phase
    assert datetime.fromisoformat(session['next_poll_at']) == clock(next_time)
    assert market_clock(CALENDAR, clock(stamp).astimezone(ZoneInfo('UTC'))) == session


def test_holiday_and_calendar_holes_never_substitute_weekdays():
    rows = deepcopy(CALENDAR)
    rows[4]['is_open'] = False # explicit closed Tuesday, not a weekday heuristic
    assert market_clock(rows, clock('2026-10-27T10:00'))['phase'] == 'closed'
    assert market_clock([], clock('2026-10-27T10:00'))['next_poll_at'] is None
    rows = [r for r in CALENDAR if r['cal_date'] != '2026-10-25']
    assert market_clock(rows, clock('2026-10-24T10:00'))['next_poll_at'] is None


def view(now):
    row = quote(); row.update(volume_unit='share', amount_unit='CNY')
    return quote_view('000503', {}, row, {}, now)


def test_live_ohlcv_comes_from_one_raw_quote_and_is_not_persisted_into_daily_rows():
    now = clock('2026-10-27T13:52')
    bars = [{'date': '2026-10-26', 'close': 7.58}]
    original = deepcopy(bars)
    live, status = intraday_observation('000503', view(now), bars, market_clock(CALENDAR, now), now)
    assert (live['open'], live['high'], live['low'], live['close'], live['volume']) == (7.57, 7.79, 7.09, 7.1, 27677600)
    assert live['observed_at'] == '2026-10-27T13:51:33+08:00'
    assert live['date'] == '2026-10-27' and status == 'intraday'
    assert bars == original


@pytest.mark.parametrize('patch', [{'trade_date':'2026-10-26'}, {'quote_kind':'historical_daily'}, {'volume_unit':'lot'},
    {'volume':None}, {'volume':-1}, {'price':float('nan')}, {'low':8}, {'symbol':'600000'},
    {'quote_observed_at':'2026-10-27T14:00:00+08:00'}, {'quote_observed_at':'2026-10-27T09:25:00+08:00'}])
def test_invalid_or_old_snapshots_do_not_become_today_candles(patch):
    now = clock('2026-10-27T13:52')
    q = view(now); q.update(patch)
    assert intraday_observation('000503', q, [], market_clock(CALENDAR, now), now) == (None, 'unavailable')


def test_delay_zero_volume_and_close_confirmation_are_separate_states():
    now = clock('2026-10-27T13:55')
    q = view(now);q['volume'] = 0
    live, status = intraday_observation('000503', q, [], market_clock(CALENDAR, now), now)
    assert live['volume'] == 0 and live['stale'] and status == 'delayed'
    now = clock('2026-10-27T15:03')
    assert intraday_observation('000503', q, [], market_clock(CALENDAR, now), now)[1] == 'delayed'
    q['quote_observed_at'] = '2026-10-27T15:01:00+08:00'
    live, status = intraday_observation('000503', q, [], market_clock(CALENDAR, now), now)
    assert status == live['status'] == 'awaiting_daily'
    # An arrived completed bar replaces the ephemeral snapshot, no duplicate.
    assert intraday_observation('000503', q, [{'date':'2026-10-27'}], market_clock(CALENDAR, now), now) == (None, 'completed')
