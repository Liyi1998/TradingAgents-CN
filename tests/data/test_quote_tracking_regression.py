"""Replay the 000503 mixed-observation regression and real Mongo replacement."""
from datetime import datetime
from zoneinfo import ZoneInfo
from uuid import uuid4

import pytest

from app.data.core.coverage import evaluate
from app.data.query.price_reference import verified_spot
from app.data.query.quote_snapshot import quote_view
from app.data.query.research_tracking import evidence_filters
from app.services.quotes_ingestion_service import QuotesIngestionService
from app.services.research_tracking import submission_failure
from app.models.research_tracking import DailyReview

NOW = datetime(2026, 10, 27, 13, 52, tzinfo=ZoneInfo('Asia/Shanghai'))


def quote():
    # Public provider observation captured on 2026-10-27, no account information.
    return dict(symbol='000503', market='CN', asset_type='stock', data_source='tencent',
        price_basis='raw', currency='CNY', close=7.10, last_price=7.10, pre_close=7.58,
        open=7.57, high=7.79, low=7.09, pct_chg=-6.33, volume=27677600,
        quote_time_basis='source_quote_timestamp', quote_observed_at='2026-10-27T13:51:33+08:00', trade_date='2026-10-27')


def daily(day='2026-10-23'):
    return dict(symbol='000503', trade_date=day, close=7.64, open=7.61, high=7.71, low=7.14,
        volume=45496232, amount=338792503, period='daily', price_basis='raw', volume_unit='share',
        amount_unit='CNY', data_source='akshare',
        unit_conversion='akshare:stock_zh_a_daily/sina;volume:share->share;amount:CNY->CNY')


def test_quote_does_not_mix_older_daily_fields_or_alias_price():
    q = quote()
    view = quote_view('000503', {'name': '国新健康', 'turnover_rate': 99}, q, daily(), NOW)
    assert (view['price'], view['open'], view['high'], view['volume']) == (7.10, 7.57, 7.79, 27677600)
    assert view['trade_date'] == '2026-10-27' and view['data_source'] == 'tencent'
    assert view['amount'] is None and view['turnover_rate'] is None
    q['last_price'] = 7.58
    assert verified_spot(q, '000503', '2026-10-27', NOW) is None
    view = quote_view('000503', {}, q, daily(), NOW)
    assert view['price'] == 7.64 and view['trade_date'] == '2026-10-23'
    assert view['quote_kind'] == 'historical_daily' and view['quote_observed_at'] is None


def test_zero_values_and_unknown_quote_are_not_replaced_by_old_fields():
    q = quote(); q.update(volume=0, amount=0, pct_chg=0, turnover_rate=0)
    view = quote_view('000503', {}, q, daily(), NOW)
    assert all(view[k] == 0 for k in ('volume', 'amount', 'change_percent', 'turnover_rate'))
    assert quote_view('000503', {}, {}, {}, NOW)['price'] is None
    assert quote_view('000503', {}, {}, daily('2026-10-28'), NOW)['price'] is None


def test_cached_friday_is_not_enough_for_monday_tracking_even_if_read_today():
    # Exchange calendar fixture (National Day closure excluded).
    days = ['2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24',
        '2026-09-28', '2026-09-29', '2026-09-30', '2026-10-08', '2026-10-09', '2026-10-12',
        '2026-10-13', '2026-10-14', '2026-10-15', '2026-10-16', '2026-10-19', '2026-10-20',
        '2026-10-21', '2026-10-22', '2026-10-23', '2026-10-26']
    filters = evidence_filters('000503', 'daily_quotes', '2026-03-01', days[-1], days)
    rows = [daily(day) for day in days[:-1]]
    for row in rows:
        row['updated_at'] = NOW
    _, quality = evaluate(rows, 'daily_quotes', '2026-03-01', days[-1], filters)
    assert quality['status'] != 'success' and quality['missing_dates'] == ['2026-10-26']
    _, quality = evaluate(rows + [daily(days[-1])], 'daily_quotes', '2026-03-01', days[-1], filters)
    assert quality['status'] == 'success'
    for domain, contract in [('daily_indicators', 'verified_dated_indicators'), ('money_flow', 'verified_same_basis_flow'), ('margin_trading', 'verified_exchange_margin')]:
        assert evidence_filters('000503', domain, '2026-10-12', days[-1], days)['contract'] == contract


@pytest.mark.asyncio
async def test_ingestion_replaces_snapshot_and_proof_in_real_mongo(real_mongo_db):
    name = 'quote_repair_' + uuid4().hex
    collection = real_mongo_db[name]
    service = QuotesIngestionService(collection_name=name)
    try:
        await collection.insert_one({**quote(), 'last_price': 7.58, 'turnover_rate': 99, 'old_only': True})
        await service._bulk_upsert({'000503': quote()}, '2026-10-26', 'akshare')
        actual = await collection.find_one({'symbol': '000503'})
        assert actual['last_price'] == actual['close'] == 7.1
        assert actual['trade_date'] == '2026-10-27' and actual['data_source'] == 'tencent'
        assert actual['quote_observed_at'] == quote()['quote_observed_at']
        assert 'turnover_rate' not in actual and 'old_only' not in actual
        await service.upsert_market_quote('000503', daily())
        actual = await collection.find_one({'symbol': '000503'})
        assert actual['last_price'] == actual['close'] == 7.64
        assert 'quote_observed_at' not in actual and 'quote_time_basis' not in actual
        assert verified_spot(actual, '000503', '2026-10-27', NOW) is None
    finally:
        await real_mongo_db.drop_collection(name)


def test_rejected_submission_retains_field_reason_without_echoing_input():
    try:
        DailyReview.model_validate({'content': 'PRIVATE_ACCOUNT_VALUE'})
    except ValueError as exc:
        message = submission_failure(exc)
        assert 'content' in message and 'missing' in message
        assert 'PRIVATE_ACCOUNT_VALUE' not in message
