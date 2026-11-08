"""真实 Mongo 游标及无网络日期门槛；向量闭环另由真实服务验收。"""
from datetime import datetime, timezone
import uuid

from app.data.knowledge.util import end_of_day_ts, parse_publish_ts


def test_market_dates_do_not_admit_next_morning_news():
    cutoff = end_of_day_ts('2026-11-02', 'CN')
    assert parse_publish_ts('2026-11-02T23:59:59+08:00', 'CN') == cutoff
    assert parse_publish_ts('2026-11-03T00:00:00+08:00', 'CN') > cutoff
    assert parse_publish_ts('2026-11-02 20:00:00', 'CN') == parse_publish_ts('2026-11-02T12:00:00Z', 'CN')
    assert end_of_day_ts('2026-11-02', 'US') == datetime(2026, 11, 3, 4, 59, 59, tzinfo=timezone.utc).timestamp()


def test_invalid_dates_fail_closed_without_embedding_request():
    from app.data.knowledge.retrieval import search
    assert search('CN', '002241', '利润', 'invalid') == []
    assert search('CN', '002241', '利润', '2026-11-02', start_date='invalid') == []


async def test_index_cursor_keeps_all_equal_timestamps(real_mongo_db):
    from app.data.storage.mongo.repositories.news_repo import NewsRepo
    from app.data.storage.mongo.collections import get_collection_name
    coll = real_mongo_db[get_collection_name('news', 'CN')]
    stamp = '9998-' + uuid.uuid4().hex
    rows = [{'symbol': '999992', 'title': str(i), 'updated_at': stamp,
             'content_hash': stamp + str(i)} for i in range(205)]
    inserted = await coll.insert_many(rows)
    try:
        first = await NewsRepo().get_index_page('CN', stamp, 200)
        assert len(first) == 200
        second = await NewsRepo().get_index_page('CN', {'updated_at': stamp, 'id': str(first[-1]['_id'])}, 200)
        assert len(second) == 5
        assert {d['_id'] for d in first + second} == set(inserted.inserted_ids)
    finally:
        await coll.delete_many({'_id': {'$in': inserted.inserted_ids}})
