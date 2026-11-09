"""Repository inheritance contracts using real isolated MongoDB I/O.

No simulated bulk-write implementation: pipeline updates and matched counts are
verified by MongoDB itself. Snapshot provenance validation stays enabled.
"""


import pytest


from app.data.storage.mongo.collections import get_collection_name
from app.data.storage.mongo.repositories.base import MongoBaseRepo
from app.data.storage.mongo.repositories.daily_quotes_repo import DailyQuotesRepo
from app.data.storage.mongo.repositories.index_quotes_repo import IndexQuotesRepo
from app.data.storage.mongo.repositories.money_flow_repo import MoneyFlowRepo

from app.data.storage.mongo.repositories.adj_factors_repo import AdjFactorsRepo
from app.data.storage.mongo.repositories.daily_indicators_repo import DailyIndicatorsRepo
from app.data.storage.mongo.repositories.factor_scores_repo import FactorScoresRepo
from app.data.storage.mongo.repositories.margin_trading_repo import MarginTradingRepo
from app.data.storage.mongo.repositories.pre_post_market_repo import PrePostMarketRepo
from app.data.storage.mongo.repositories.southbound_holding_repo import SouthboundHoldingRepo



# ============================================================
# 模拟库 bulk_write 补齐 — UpdateOne 逐条走真实 update_one 路径
# ============================================================

@pytest.fixture
async def repo_db(real_mongo_db):
    from isolated_database import require_isolated_database
    require_isolated_database()
    query = {"symbol": {"$in": ["900901", "900902", "900903", "900904", "900905", "900906"]}}
    for domain in ("daily_quotes", "index_quotes", "money_flow"):
        await real_mongo_db[get_collection_name(domain, "CN")].delete_many(query)
    yield real_mongo_db
    for domain in ("daily_quotes", "index_quotes", "money_flow"):
        await real_mongo_db[get_collection_name(domain, "CN")].delete_many(query)



# ============================================================
# upsert 覆盖语义 — 同自然键不同 data_source 只留一份
# ============================================================

# (domain, repo_cls, 自然键除 symbol/trade_date 外的额外字段)
OVERWRITE_CASES = [
    ("daily_quotes", DailyQuotesRepo, {"period": "daily"}),
    ("index_quotes", IndexQuotesRepo, {}),
    ("money_flow", MoneyFlowRepo, {}),
]


@pytest.mark.parametrize("domain,repo_cls,key_extra", OVERWRITE_CASES)
async def test_upsert_single_version_overwrite(
    repo_db, domain, repo_cls, key_extra
):
    """同自然键两批写入（data_source 不同）只留一份，后写覆盖先写。"""
    symbol = "900901"
    natural_key = {"symbol": symbol, "trade_date": "2026-01-05", **key_extra}
    repo = repo_cls()

    n1 = await repo.upsert_many(
        [{**natural_key, "close": 10.0, "main_net_inflow": 1.0, "data_source": "tushare"}], "CN"
    )
    assert n1 == 1
    n2 = await repo.upsert_many(
        [{**natural_key, "close": 11.0, "main_net_inflow": 2.0, "data_source": "akshare"}], "CN"
    )
    assert n2 == 1  # 覆盖不新增

    coll = repo_db[get_collection_name(domain, "CN")]
    assert await coll.count_documents({"symbol": symbol}) == 1
    doc = await coll.find_one({"symbol": symbol})
    assert doc["data_source"] == "akshare"  # 备用源覆盖同自然键（键不含 data_source）
    assert doc["close"] == 11.0


# ============================================================
# 缺唯一键字段的记录跳过
# ============================================================


@pytest.mark.parametrize("domain,repo_cls,key_extra", OVERWRITE_CASES)
async def test_upsert_skips_records_missing_trade_date(
    repo_db, domain, repo_cls, key_extra
):
    """缺 trade_date 的记录跳过，不写库、不计入返回值。"""
    repo = repo_cls()
    records = [
        {"symbol": "900901", "trade_date": "2026-01-05", **key_extra,
         "data_source": "t", "main_net_inflow": 1.0},
        {"symbol": "900902", "data_source": "t", "main_net_inflow": 1.0},  # 缺 trade_date → 跳过
        {"symbol": "900903", "trade_date": "2026-01-06", **key_extra,
         "data_source": "t", "main_net_inflow": 1.0},
    ]
    n = await repo.upsert_many(records, "CN")
    assert n == 2

    coll = repo_db[get_collection_name(domain, "CN")]
    assert await coll.count_documents({"symbol": {"$in": ["900901", "900902", "900903"]}}) == 2
    assert await coll.count_documents({"symbol": "900902"}) == 0


async def test_upsert_skips_daily_quotes_missing_period(repo_db):
    """Missing natural-key period is rejected before writing."""
    repo = DailyQuotesRepo()
    records = [
        {"symbol": "900901", "trade_date": "2026-01-05", "period": "daily"},
        {"symbol": "900901", "trade_date": "2026-01-06"},  # 缺 period → 跳过
    ]
    assert await repo.upsert_many(records, "CN") == 1


# ============================================================
# range_sort_direction 显式契约（模拟层游标不排序，锁类属性声明）
# ============================================================

# 全量锁定使用基类区间方法的域（防排序方向静默漂移）
DIRECTION_CONTRACT = [
    ("daily_quotes", DailyQuotesRepo, 1),        # 升序：K线取尾部最新、周期聚合
    ("index_quotes", IndexQuotesRepo, 1),        # 升序：基准走势按时间正序
    ("adj_factors", AdjFactorsRepo, 1),          # 升序：前复权序列计算
    ("daily_indicators", DailyIndicatorsRepo, -1),   # 降序：最新优先
    ("money_flow", MoneyFlowRepo, -1),           # 降序：资金流展示最新优先
    ("margin_trading", MarginTradingRepo, -1),   # 降序：两融展示最新优先
    ("southbound_holding", SouthboundHoldingRepo, -1),  # 降序：持仓变动展示
    ("pre_post_market", PrePostMarketRepo, -1),  # 降序：盘前盘后展示
    ("factor_scores", FactorScoresRepo, -1),     # 降序：因子快照最新优先
]


@pytest.mark.parametrize("domain,repo_cls,direction", DIRECTION_CONTRACT)
def test_range_sort_direction_declared(domain, repo_cls, direction):
    assert repo_cls.domain == domain
    assert repo_cls.range_sort_direction == direction


def test_base_default_direction_is_ascending():
    assert MongoBaseRepo.range_sort_direction == 1


# ============================================================
# get_by_symbol_and_range 过滤与 extra_query 透传（模拟层）
# ============================================================


async def test_range_filters_and_extra_query(repo_db):
    coll = repo_db[get_collection_name("daily_quotes", "CN")]
    await coll.insert_many([
        {"symbol": "900901", "trade_date": "2026-01-05", "period": "daily"},
        {"symbol": "900901", "trade_date": "2026-01-06", "period": "daily"},
        {"symbol": "900901", "trade_date": "2026-01-07", "period": "week"},
        {"symbol": "900901", "trade_date": "2026-02-01", "period": "daily"},  # 区间外
        {"symbol": "900904", "trade_date": "2026-01-06", "period": "daily"},  # 其它标的
    ])
    repo = DailyQuotesRepo()

    rows = await repo.get_by_symbol_and_range(
        "900901", "CN", "2026-01-01", "2026-01-31", period="daily"
    )
    assert sorted(r["trade_date"] for r in rows) == ["2026-01-05", "2026-01-06"]

    rows_all = await repo.get_by_symbol_and_range(
        "900901", "CN", "2026-01-01", "2026-01-31"
    )
    assert sorted(r["trade_date"] for r in rows_all) == [
        "2026-01-05", "2026-01-06", "2026-01-07",
    ]


# ============================================================
# get_latest_date 命中与 miss（模拟层 find_one 支持排序）
# ============================================================


@pytest.mark.parametrize("repo_cls", [DailyQuotesRepo, MoneyFlowRepo])
async def test_get_latest_date_hit_and_miss(repo_db, repo_cls):
    repo = repo_cls()
    await repo.upsert_many([
        {"symbol": "900901", "trade_date": "2026-01-05", "period": "daily",
         "data_source": "t", "main_net_inflow": 1.0},
        {"symbol": "900901", "trade_date": "2026-01-08", "period": "daily",
         "data_source": "t", "main_net_inflow": 1.0},
        {"symbol": "900901", "trade_date": "2026-01-06", "period": "daily",
         "data_source": "t", "main_net_inflow": 1.0},
    ], "CN")
    # 命中：取最大 trade_date（乱序写入）
    assert await repo.get_latest_date("900901", "CN") == "2026-01-08"
    # miss：无数据返回 None
    assert await repo.get_latest_date("999999", "CN") is None


# ============================================================
# 排序方向行为断言（真实 Mongo 行序，requires_db → integration 层）
# ============================================================

_RP_SYMBOLS = ("900905", "900906")
_RP_DATES = ("2026-01-07", "2026-01-05", "2026-01-06")  # 乱序写入


@pytest.mark.requires_db
class TestRangeOrderOnRealMongo:
    @pytest.fixture(autouse=True)
    async def _cleanup_rp_seeds(self, real_mongo_db):
        yield
        for domain in ("daily_quotes", "money_flow"):
            await real_mongo_db[get_collection_name(domain, "CN")].delete_many(
                {"symbol": {"$in": list(_RP_SYMBOLS)}}
            )

    async def test_daily_quotes_ascending(self, real_mongo_db):
        repo = DailyQuotesRepo()
        await repo.upsert_many([
            {"symbol": "900905", "trade_date": d, "period": "daily",
             "close": 1.0, "data_source": "t", "main_net_inflow": 1.0}
            for d in _RP_DATES
        ], "CN")
        rows = await repo.get_by_symbol_and_range(
            "900905", "CN", "2026-01-01", "2026-01-31"
        )
        assert [r["trade_date"] for r in rows] == [
            "2026-01-05", "2026-01-06", "2026-01-07",
        ]

    async def test_money_flow_descending(self, real_mongo_db):
        repo = MoneyFlowRepo()
        await repo.upsert_many([
            {"symbol": "900906", "trade_date": d,
             "main_net_inflow": 1.0, "data_source": "t", "main_net_inflow": 1.0}
            for d in _RP_DATES
        ], "CN")
        rows = await repo.get_by_symbol_and_range(
            "900906", "CN", "2026-01-01", "2026-01-31"
        )
        assert [r["trade_date"] for r in rows] == [
            "2026-01-07", "2026-01-06", "2026-01-05",
        ]
