"""Real adapters/facade/storage; no model calls and no mocked provider results."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.data.query.price_reference import verified_spot
from app.data.sources.cn.tencent.adapter import TencentCNAdapter

TZ = ZoneInfo('Asia/Shanghai')


def test_report_read_preserves_price_evidence_without_authorizing_entry():
    from copy import deepcopy
    from app.services.reports_service import _extract_structured_fields
    reference = {'status': 'spot_available', 'spot': {'price': 49.33,
        'trade_date': '2026-10-14', 'currency': 'CNY', 'data_source': 'tencent'}}
    doc = {'structured_summary': {'price_reference': reference},
           'decision': {'action': 'WATCH', 'current_entry_limit_pct': 0}}
    original = deepcopy(doc)
    result = _extract_structured_fields(doc)
    assert result['structured_summary']['price_reference'] == reference
    assert result['decision']['action'] == '观望'
    assert result['decision']['current_entry_limit_pct'] == 0
    assert doc == original


def quote(stamp='20261014144753', price=49.29):
    return TencentCNAdapter().adapt_market_quotes(pd.DataFrame([{
        'symbol': '300124', 'exchange': 'SZ', 'asset_type': 'stock',
        'price': price, 'update_time': stamp, 'volume': 1000, 'amount': 49290,
    }]))[0].to_db_doc()


def test_source_timestamp_price_currency_survive_adapter():
    row = quote()
    result = verified_spot(row, '300124', '2026-10-14', datetime(2026, 10, 14, 14, 48, tzinfo=TZ))
    assert result['price'] == 49.29
    assert result['observed_at'] == '2026-10-14T14:47:53+08:00'
    assert result['kind'] == 'spot_snapshot'
    assert result['price_basis'] == 'raw'


@pytest.mark.parametrize('changes', [
    {'last_price': 0}, {'last_price': float('nan')}, {'last_price': True},
    {'data_source': 'akshare'}, {'currency': 'USD'}, {'price_basis': 'qfq'},
    {'asset_type': 'index'}, {'symbol': '600206'}, {'quote_time_basis': None},
    {'quote_observed_at': '2026-10-14T14:47:53'},
    {'quote_observed_at': '2026-10-14T14:49:00+08:00'},
    {'quote_observed_at': '2026-10-13T14:47:53+08:00'},
    {'trade_date': '2026-10-13'}, {'quote_observed_at': '2026-10-14T14:00:00+08:00'},
])
def test_invalid_or_stale_evidence_is_not_certified(changes):
    row = {**quote(), **changes}
    assert verified_spot(row, '300124', '2026-10-14', datetime(2026, 10, 14, 14, 48, tzinfo=TZ)) is None


def test_postclose_quote_is_not_a_completed_daily_bar():
    row = quote('20261014150000')
    result = verified_spot(row, '300124', '2026-10-14', datetime(2026, 10, 14, 16, tzinfo=TZ))
    assert result['kind'] == 'spot_snapshot'
    assert verified_spot(quote(), '300124', '2026-10-14', datetime(2026, 10, 14, 16, tzinfo=TZ)) is None


def test_source_updates_after_close_and_lunch_quote_remain_distinct_from_trade_time():
    result = verified_spot(quote('20261014161409'), '300124', '2026-10-14', datetime(2026, 10, 14, 16, 15, tzinfo=TZ))
    assert result['session'] == 'post_close'
    assert '非官方收盘确认' in result['label']
    assert verified_spot(quote('20261014113000'), '300124', '2026-10-14', datetime(2026, 10, 14, 12, tzinfo=TZ))


def test_malformed_timestamp_does_not_gain_acquisition_time_as_evidence():
    row = quote('20261014')
    assert 'quote_observed_at' not in row
    assert verified_spot(row, '300124', '2026-10-14', datetime(2026, 10, 14, 16, tzinfo=TZ)) is None


@pytest.mark.asyncio
async def test_real_repository_replaces_entire_snapshot_without_mixing_timestamps(real_mongo_db):
    from app.data.storage.mongo.repositories.market_quotes_repo import MarketQuotesRepo
    from app.data.storage.mongo.collections import get_collection_name
    assert 'validation' in real_mongo_db.name or 'test' in real_mongo_db.name
    coll = real_mongo_db[get_collection_name('market_quotes', 'CN')]
    symbol = '987641'
    assert await coll.count_documents({'symbol': symbol}) == 0
    repo = MarketQuotesRepo()
    try:
        await repo.upsert_many([{**quote(), 'symbol': symbol}], 'CN')
        await repo.upsert_many([{**quote('20261013150000', 50.89), 'symbol': symbol}], 'CN')
        row = await repo.get_by_symbol(symbol, 'CN')
        assert row['last_price'] == 50.89 and row['last_updated'] == '20261013150000'
        assert row['quote_observed_at'] == '2026-10-13T15:00:00+08:00'
        await repo.upsert_many([{'symbol': symbol, 'market': 'CN', 'data_source': 'akshare', 'last_price': 51}], 'CN')
        row = await repo.get_by_symbol(symbol, 'CN')
        assert 'quote_observed_at' not in row and 'quote_time_basis' not in row
    finally:
        await coll.delete_many({'symbol': symbol})


@pytest.mark.asyncio
async def test_real_facade_keeps_historical_price_and_does_not_fetch_future_live_quote(real_mongo_db):
    from app.data.core.interface import DataInterface
    from app.data.storage.mongo.collections import get_collection_name
    from app.data.query.price_reference import collect_price_reference
    assert 'validation' in real_mongo_db.name or 'test' in real_mongo_db.name
    symbol = '987642'
    coll = real_mongo_db[get_collection_name('daily_quotes', 'CN')]
    assert await coll.count_documents({'symbol': symbol}) == 0
    await coll.insert_one({'symbol': symbol, 'market': 'CN', 'data_source': 'baostock',
        'period': 'daily', 'trade_date': '2026-10-13', 'close': 50.89,
        'open': 50, 'high': 51.34, 'low': 49.51, 'volume': 1000, 'amount': 50890,
        'price_basis': 'raw', 'volume_unit': 'share', 'amount_unit': 'CNY',
        'unit_conversion': 'baostock:identity', 'updated_at': '2026-10-13T15:01:00+08:00'})
    DataInterface.reset_instance()
    try:
        result = await collect_price_reference(DataInterface.get_instance(), 'CN', symbol,
            {'as_of': '2026-10-13', 'last_completed_trade_date': '2026-10-13'})
        assert result['spot'] is None
        assert result['historical_close']['price'] == 50.89
        assert result['historical_close']['trade_date'] == '2026-10-13'
        assert result['status'] == 'historical_only'
    finally:
        await coll.delete_many({'symbol': symbol})
        DataInterface.reset_instance()
