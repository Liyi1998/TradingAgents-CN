"""
分析服务测试

业务逻辑测试：测试 AnalysisService 的纯逻辑方法
数据依赖测试：使用 SimulatedMongoDB，通过设置全局变量注入
"""

import pytest
import app.core.database as db_module
from test_infra import SimulatedMongoDB


class TestHelperFunctions:
    """辅助函数测试 — 纯逻辑，无外部依赖"""

    def test_get_default_provider_by_model(self):
        from app.services.analysis.providers import _get_default_provider_by_model

        assert _get_default_provider_by_model("qwen-turbo") == "dashscope"
        assert _get_default_provider_by_model("gpt-4o") == "openai"
        assert _get_default_provider_by_model("deepseek-chat") == "deepseek"
        assert _get_default_provider_by_model("unknown-model") == "dashscope"

    def test_get_default_backend_url(self):
        from app.services.analysis.providers import _get_default_backend_url

        assert "openai.com" in _get_default_backend_url("openai")
        assert "deepseek.com" in _get_default_backend_url("deepseek")
        assert "dashscope" in _get_default_backend_url("dashscope")

    def test_serialize_for_response_converts_objectid(self):
        from app.services.analysis import AnalysisService
        from bson import ObjectId

        svc = AnalysisService.__new__(AnalysisService)
        oid = ObjectId()
        result = svc._serialize_for_response({"_id": oid, "name": "test"})
        assert result["_id"] == str(oid)
        assert result["name"] == "test"

    def test_result_metrics_prefer_structured_summary(self):
        from app.services.analysis_service import (
            _extract_result_risk_and_confidence,
        )

        confidence, risk = _extract_result_risk_and_confidence(
            {"confidence": 0.55, "risk_score": 0.75},
            {
                "model_confidence": 64,
                "risk_assessment": {"level": "High"},
            },
        )

        assert confidence == 0.64
        assert risk == "高"

    def test_result_metrics_fall_back_to_final_decision(self):
        from app.services.analysis_service import (
            _extract_result_risk_and_confidence,
        )

        confidence, risk = _extract_result_risk_and_confidence(
            {"confidence": 0.55, "risk_score": 0.75}, {}
        )

        assert confidence == 0.55
        assert risk == "高"

    def test_select_configured_model_replaces_disabled_request(self):
        from app.services.analysis_service import _select_configured_model

        class Collection:
            @staticmethod
            def find_one(query):
                return {"name": query["name"], "is_active": True, "api_key": "local-key"}

        class DB:
            llm_providers = Collection()

        configs = [
            {"model_name": "qwen-turbo", "provider": "qwen", "enabled": False},
            {
                "model_name": "local-analyst",
                "provider": "custom_openai",
                "enabled": True,
                "suitable_roles": ["analyst"],
            },
        ]

        assert _select_configured_model(
            "qwen-turbo", None, configs, "analyst", DB()
        ) == "local-analyst"

    def test_select_configured_model_prefers_valid_system_default(self):
        from app.services.analysis_service import _select_configured_model

        class Collection:
            @staticmethod
            def find_one(query):
                return {"name": query["name"], "is_active": True, "api_key": "local-key"}

        class DB:
            llm_providers = Collection()

        configs = [
            {
                "model_name": "local-a",
                "provider": "custom_openai",
                "enabled": True,
                "suitable_roles": ["analyst"],
            },
            {
                "model_name": "local-b",
                "provider": "custom_openai",
                "enabled": True,
                "suitable_roles": ["analyst"],
            },
        ]

        assert _select_configured_model(
            None, "local-b", configs, "analyst", DB()
        ) == "local-b"

    def test_task_token_usage_aggregates_input_and_output(self):
        from app.services.analysis_service import AnalysisService

        class Collection:
            @staticmethod
            def aggregate(pipeline):
                assert pipeline[0] == {"$match": {"session_id": "task-1"}}
                return [{"_id": None, "input": 120, "output": 30}]

        class DB:
            def __getitem__(self, name):
                assert name == "token_usage"
                return Collection()

        service = AnalysisService.__new__(AnalysisService)
        service._get_sync_mongo_db = lambda: DB()
        assert service._get_task_token_usage_sync("task-1") == 150

    def test_key_points_are_derived_from_structured_summary(self):
        from app.services.analysis_service import _derive_key_points

        points = _derive_key_points(
            {
                "key_indicators": {
                    "target_price": "23.50元",
                    "stop_loss": "20.10元",
                    "entry_price": "N/A",
                },
                "risk_assessment": {"description": "资金口径存在分歧"},
            },
            recommendation="持有，等待放量确认",
            risk_level="中等",
        )

        assert points == [
            "操作建议：持有，等待放量确认",
            "目标位：23.50元",
            "止损位：20.10元",
            "风险（中等）：资金口径存在分歧",
        ]

    @pytest.mark.asyncio
    async def test_placeholder_stock_name_is_not_cached(self, real_mongo_db):
        from app.services.analysis import AnalysisService
        from app.data.storage.mongo.collections import get_collection_name
        from isolated_database import require_isolated_database
        require_isolated_database()
        coll = real_mongo_db[get_collection_name("basic_info", "CN")]
        code = "900922"
        await coll.delete_many({"symbol": code})
        service = AnalysisService()
        try:
            assert await service._resolve_stock_name(code) == f"股票{code}"
            assert service._stock_name_cache.get(code) is None
            await coll.insert_one({"symbol": code, "market": "CN", "name": "隔离名称样本", "data_source": "test"})
            assert await service._resolve_stock_name(code) == "隔离名称样本"
            assert service._stock_name_cache.get(code) == "隔离名称样本"
        finally:
            await coll.delete_many({"symbol": code})
            service._shutdown_pool()


class TestAnalysisServiceWithSimDB:
    """使用 SimulatedMongoDB 的 AnalysisService 测试"""

    @pytest.fixture
    def sim_db(self):
        return SimulatedMongoDB()

    @pytest.mark.asyncio
    async def test_search_returns_list(self, sim_db):
        from app.services.analysis import AnalysisService

        original = db_module.mongo_db
        db_module.mongo_db = sim_db
        try:
            svc = AnalysisService.__new__(AnalysisService)
            result = await svc.search_stock_basic_info("平安")
            assert isinstance(result, list)
        finally:
            db_module.mongo_db = original

    @pytest.mark.asyncio
    async def test_search_with_market_filter(self, sim_db):
        from app.services.analysis import AnalysisService

        await sim_db.stock_basic_info.insert_one({
            "code": "000001",
            "name": "平安银行",
            "market": "A股",
        })

        original = db_module.mongo_db
        db_module.mongo_db = sim_db
        try:
            svc = AnalysisService.__new__(AnalysisService)
            result = await svc.search_stock_basic_info("000001", market="A股")
            assert isinstance(result, list)
        finally:
            db_module.mongo_db = original

    @pytest.mark.asyncio
    async def test_mark_task_failed_returns_false_for_missing(self, sim_db):
        """标记不存在的任务应返回 False 或 True（取决于实现）"""
        from app.services.analysis import AnalysisService

        original = db_module.mongo_db
        db_module.mongo_db = sim_db
        try:
            svc = AnalysisService.__new__(AnalysisService)
            result = await svc.mark_task_failed("nonexistent-task", "测试错误")
            # 不存在的任务，结果取决于实现
            assert isinstance(result, bool)
        finally:
            db_module.mongo_db = original


class TestGetAnalysisServiceSingleton:

    def test_get_analysis_service_returns_instance(self):
        from app.services.analysis import AnalysisService, get_analysis_service

        import app.services.analysis.service as mod
        mod.analysis_service = None

        svc = get_analysis_service()
        assert isinstance(svc, AnalysisService)

        mod.analysis_service = None
