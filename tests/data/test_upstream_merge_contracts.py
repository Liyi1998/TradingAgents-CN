"""Cross-boundary merge regressions; real database I/O and no model calls."""
import pytest
from pydantic import ValidationError

from app.core.horizons import horizon_days_of
from app.data.core.interface import DataInterface
from app.data.core.symbols import normalize_data_symbol
from app.data.storage.mongo.collections import get_collection_name
from app.models.analysis import AnalysisParameters


@pytest.mark.parametrize("canonical,alias,days", [
    ("short", "short", 7), ("medium", "mid", 60), ("long", "long", 252),
])
def test_period_roundtrip_has_one_unambiguous_contract(canonical, alias, days):
    original = AnalysisParameters(analysis_horizon=canonical)
    restored = AnalysisParameters.model_validate(original.model_dump())
    assert restored.analysis_horizon == canonical
    assert restored.horizon == alias
    assert horizon_days_of(restored.horizon) == days
    assert AnalysisParameters(horizon=alias).analysis_horizon == canonical


def test_mismatched_periods_fail_before_creating_task():
    with pytest.raises(ValidationError, match="周期不一致"):
        AnalysisParameters(analysis_horizon="long", horizon="short")


def test_exchange_cannot_be_lost_for_index_identity():
    assert normalize_data_symbol("000001.SH", "CN", "index_quotes") == "000001.SH"
    assert normalize_data_symbol("sh000001", "CN", "index_quotes") == "000001.SH"
    assert normalize_data_symbol("000001.SZ", "CN", "daily_quotes") == "000001"
    with pytest.raises(ValueError, match="交易所"):
        normalize_data_symbol("000001", "CN", "index_quotes")


@pytest.mark.requires_db
async def test_index_facade_keeps_exchange_across_range_latest_and_batch(real_mongo_db):
    from isolated_database import require_isolated_database
    require_isolated_database()
    db = real_mongo_db
    index_coll = db[get_collection_name("index_quotes", "CN")]
    stock_coll = db[get_collection_name("daily_quotes", "CN")]
    symbols = ["900921.SH", "900921"]
    try:
        await index_coll.insert_one({"symbol": symbols[0], "trade_date": "2026-10-30", "close": 3200, "data_source": "test"})
        await stock_coll.insert_one({"symbol": symbols[1], "trade_date": "2026-10-30", "period": "daily", "close": 12, "data_source": "test"})
        di = DataInterface.get_instance()
        ranged = await di.read("CN", "index_quotes", symbol="900921.SH", start_date="20261030", end_date="20261030")
        latest = await di.read_latest("CN", "index_quotes", "900921.SH")
        batch = await di.read_latest_batch("CN", "index_quotes", ["900921.SH"])
        assert ranged["data"][0]["close"] == latest["close"] == batch["900921.SH"]["close"] == 3200
        stock = await di.read("CN", "daily_quotes", symbol="900921.SZ", end_date="20261030")
        assert stock["data"][0]["close"] == 12
    finally:
        for coll in (index_coll, stock_coll):
            await coll.delete_many({"symbol": {"$in": symbols}})


def test_legacy_service_exports_share_new_implementation():
    from app.services.analysis import AnalysisService, resolve_analysis_models_sync
    from app.services.analysis_service import AnalysisService as Legacy, resolve_analysis_models_sync as old_resolver
    assert Legacy is AnalysisService
    assert old_resolver is resolve_analysis_models_sync


def test_disabled_knowledge_preload_does_not_need_model_configuration():
    from app.core.config import settings
    from app.engine.tools.datasources.knowledge import get_knowledge_base_brief
    previous = settings.KB_RETRIEVAL_ENABLED
    try:
        settings.KB_RETRIEVAL_ENABLED = False
        assert "未调用向量模型" in get_knowledge_base_brief("000001", current_date="2026-10-30")
    finally:
        settings.KB_RETRIEVAL_ENABLED = previous


def test_all_registered_preloads_accept_their_injected_keyword_arguments():
    import inspect
    from app.engine.tools.datasources.registry import DATASOURCE_REGISTRY, resolve_real_fn

    # 实际函数签名与注册参数必须匹配，防重构后 companion 证据在预加载时静默丢失。
    for spec in DATASOURCE_REGISTRY:
        inspect.signature(resolve_real_fn(spec.fn)).bind(**dict.fromkeys(spec.inject_args, "test"))


@pytest.mark.requires_db
async def test_mcp_ingestion_facade_persists_normalized_news_once(real_mongo_db):
    import json
    from app.services.mcp_data_cache import persist_mcp_result

    raw = [{"type": "text", "text": json.dumps({"data": [{
        "columns": ["标题", "摘要", "发布时间", "来源", "跳转链接"],
        "items": [["合并验收新闻", "公开测试摘要", "2026-10-30 10:00:00", "测试源", "https://example.com/merge-test"]],
    }]}, ensure_ascii=False)}]
    args = {"query": "900921 的新闻"}
    di = DataInterface.get_instance()
    await di.persist_mcp_result("mx_finance_search_news", args, raw)
    await persist_mcp_result("mx_finance_search_news", args, raw)
    rows = await di.read("CN", "news", symbol="900921", end_date="2026-10-30")
    assert len(rows["data"]) == 1
    assert rows["data"][0]["title"] == "合并验收新闻"
    assert rows["data"][0]["data_source"] == "mx_mcp"
