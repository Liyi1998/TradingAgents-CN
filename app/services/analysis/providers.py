"""
股票分析服务
整合了原 simple_analysis_service.py 和 analysis_service.py 的功能
"""
# data-access-exempt: 应用层集合（analysis_tasks/reports/users/notifications 等）直查属架构豁免；业务数据读取已收敛至 DataInterface

import logging
from typing import Dict, Any, List, Optional

from app.engine.default_config import DEFAULT_CONFIG

from app.core.database import get_mongo_db_sync
from app.services.config_service import config_service

# 设置日志
logger = logging.getLogger("app.services.analysis_service")

# config_service imported from app.services.config_service (facade singleton)

async def get_provider_by_model_name(model_name: str) -> str:
    """
    根据模型名称从数据库配置中查找对应的供应商（异步版本）
    """
    try:
        # 从配置服务获取系统配置
        system_config = await config_service.get_system_config()
        if not system_config or not system_config.llm_configs:
            logger.warning("⚠️ 系统配置为空，使用默认供应商映射")
            return _get_default_provider_by_model(model_name)

        # 在LLM配置中查找匹配的模型
        for llm_config in system_config.llm_configs:
            if llm_config.model_name == model_name:
                provider = (
                    llm_config.provider.value if hasattr(llm_config.provider, "value") else str(llm_config.provider)
                )
                logger.info(f"✅ 从数据库找到模型 {model_name} 的供应商: {provider}")
                return provider

        # 如果数据库中没有找到，使用默认映射
        logger.warning(f"⚠️ 数据库中未找到模型 {model_name}，使用默认映射")
        return _get_default_provider_by_model(model_name)

    except Exception as e:
        logger.error(f"❌ 查找模型供应商失败: {e}", exc_info=True)
        return _get_default_provider_by_model(model_name)


def get_provider_by_model_name_sync(model_name: str) -> str:
    """
    根据模型名称从数据库配置中查找对应的供应商（同步版本）
    """
    provider_info = get_provider_and_url_by_model_sync(model_name)
    return provider_info["provider"]


# 模型能力元数据缓存（仅缓存 provider + backend_url，绝不缓存明文 API Key）。
# API Key 通过独立路径 _resolve_api_key_sync() 实时从 DB 读取，避免明文密钥长期驻留内存。
from app.core.lru_cache import BoundedLRUCache  # noqa: E402 (intentional late import)

_model_metadata_cache: BoundedLRUCache = BoundedLRUCache(maxsize=32, ttl=300, name="model_metadata_cache")
_MODEL_API_KEY_PLACEHOLDER = "__resolved_at_runtime__"


def _resolve_api_key_sync(db, provider: str, model_api_key: Optional[str]) -> Optional[str]:
    """实时从 MongoDB 解析 API Key（模型配置优先，厂家配置兜底）。

    返回明文 API Key 或 None。不缓存——密钥每次调用都从 DB 实时读取。
    """
    if model_api_key and model_api_key.strip() and model_api_key != "your-api-key":
        return model_api_key.strip()
    provider_doc = db.llm_providers.find_one({"name": provider})
    if provider_doc and provider_doc.get("api_key"):
        provider_api_key = provider_doc["api_key"]
        if provider_api_key and provider_api_key.strip() and provider_api_key != "your-api-key":
            return provider_api_key.strip()
    return None


def _role_matches(config: Dict[str, Any], role: str) -> bool:
    """判断模型配置是否适合指定分析角色。"""
    roles = config.get("suitable_roles") or ["both"]
    normalized = {
        str(getattr(item, "value", item)).strip().lower() for item in roles if item
    }
    return not normalized or role in normalized or "both" in normalized


def _select_configured_model(
    requested_model: Optional[str],
    configured_default: Optional[str],
    llm_configs: List[Dict[str, Any]],
    role: str,
    db,
) -> str:
    """从已启用且具备凭据的模型中选择一个角色适配模型。

    浏览器可能保留旧版本页面里的 qwen 默认值，因此不能仅判断字段是否非空；
    请求模型必须仍处于启用状态且有可用密钥，否则自动采用系统默认/角色模型。
    """
    usable: List[Dict[str, Any]] = []
    for item in llm_configs:
        if not item.get("enabled", True) or not item.get("model_name"):
            continue
        provider = str(item.get("provider") or "").strip()
        provider_doc = db.llm_providers.find_one({"name": provider}) if provider else None
        if provider_doc and provider_doc.get("is_active") is False:
            continue
        if not _resolve_api_key_sync(db, provider, item.get("api_key")):
            continue
        usable.append(item)

    by_name = {str(item["model_name"]): item for item in usable}
    for preferred in (requested_model, configured_default):
        if preferred and preferred in by_name and _role_matches(by_name[preferred], role):
            return str(preferred)

    role_candidates = [item for item in usable if _role_matches(item, role)]
    if not role_candidates:
        raise ValueError(
            f"没有已启用且具备 API 密钥的{('分析' if role == 'analyst' else '辩论')}模型，"
            "请在“配置管理 → 大模型配置”中启用并配置模型。"
        )

    # 精确角色优先于 both，其次采用后台配置的优先级和能力等级。
    role_candidates.sort(
        key=lambda item: (
            role in {
                str(getattr(value, "value", value)).strip().lower()
                for value in (item.get("suitable_roles") or [])
            },
            int(item.get("priority") or 0),
            int(item.get("capability_level") or 0),
        ),
        reverse=True,
    )
    return str(role_candidates[0]["model_name"])


def resolve_analysis_models_sync(
    analyst_model: Optional[str], debate_model: Optional[str]
) -> tuple[str, str]:
    """把请求中的模型解析为当前数据库里真正可用的模型。"""
    db = get_mongo_db_sync()
    doc = db.system_configs.find_one({"is_active": True}, sort=[("version", -1)])
    if not doc:
        raise ValueError("系统模型配置不存在，请先在配置管理中添加模型。")

    raw_settings = doc.get("system_settings") or {}
    settings = raw_settings.get("config", raw_settings)
    if not isinstance(settings, dict):
        settings = {}
    llm_configs = doc.get("llm_configs") or []

    resolved_analyst = _select_configured_model(
        analyst_model,
        settings.get("analyst_model"),
        llm_configs,
        "analyst",
        db,
    )
    resolved_debate = _select_configured_model(
        debate_model,
        settings.get("debate_model"),
        llm_configs,
        "debate",
        db,
    )
    if (resolved_analyst, resolved_debate) != (analyst_model, debate_model):
        logger.warning(
            "模型请求已自动修正: analyst=%s→%s, debate=%s→%s",
            analyst_model,
            resolved_analyst,
            debate_model,
            resolved_debate,
        )
    return resolved_analyst, resolved_debate


def get_provider_and_url_by_model_sync(model_name: str) -> dict:
    """
    根据模型名称从数据库配置中查找对应的供应商和 API URL（同步版本，带元数据缓存）

    安全策略：
    - 缓存仅保存 provider + backend_url（不可敏感元数据）
    - api_key 通过占位符返回，调用方需通过 _resolve_api_key_sync 实时解析
    """
    cached = _model_metadata_cache.get(model_name)
    if cached:
        api_key = _resolve_api_key_sync(get_mongo_db_sync(), cached["provider"], None)
        return {
            "provider": cached["provider"],
            "backend_url": cached["backend_url"],
            "api_key": api_key,
        }

    try:
        db = get_mongo_db_sync()
        # 查询最新的活跃配置
        configs_collection = db.system_configs
        doc = configs_collection.find_one({"is_active": True}, sort=[("version", -1)])

        if doc and "llm_configs" in doc:
            llm_configs = doc["llm_configs"]

            for config_dict in llm_configs:
                if (
                    config_dict.get("model_name") == model_name
                    and config_dict.get("enabled", True)
                ):
                    provider = config_dict.get("provider")
                    api_base = config_dict.get("api_base")
                    model_api_key = config_dict.get("api_key")

                    providers_collection = db.llm_providers
                    provider_doc = providers_collection.find_one({"name": provider})

                    # 实时解析 API Key（不缓存）
                    api_key = _resolve_api_key_sync(db, provider, model_api_key)

                    if not api_key:
                        logger.warning(f"⚠️ [同步查询] 未找到 {provider} 的 API Key，请在 Web UI 配置管理中添加")

                    # 确定 backend_url
                    backend_url = None
                    if api_base:
                        backend_url = api_base
                        logger.info(f"✅ [同步查询] 模型 {model_name} 使用自定义 API: {api_base}")
                    elif provider_doc and provider_doc.get("default_base_url"):
                        backend_url = provider_doc["default_base_url"]
                        logger.info(f"✅ [同步查询] 模型 {model_name} 使用厂家默认 API: {backend_url}")
                    else:
                        backend_url = _get_default_backend_url(provider)
                        logger.warning(f"⚠️ [同步查询] 厂家 {provider} 没有配置 default_base_url，使用硬编码默认值")

                    # 仅缓存 provider + backend_url 元数据
                    _model_metadata_cache.set(
                        model_name,
                        {
                            "provider": provider,
                            "backend_url": backend_url,
                        },
                    )
                    return {
                        "provider": provider,
                        "backend_url": backend_url,
                        "api_key": api_key,
                    }
        logger.warning(f"⚠️ [同步查询] 数据库中未找到模型 {model_name}，使用默认映射")
        provider = _get_default_provider_by_model(model_name)

        # 尝试从厂家配置中获取 default_base_url 和 API Key
        try:
            providers_collection = db.llm_providers
            provider_doc = providers_collection.find_one({"name": provider})

            backend_url = _get_default_backend_url(provider)
            if provider_doc:
                if provider_doc.get("default_base_url"):
                    backend_url = provider_doc["default_base_url"]
                    logger.info(f"✅ [同步查询] 使用厂家 {provider} 的 default_base_url: {backend_url}")

            api_key = _resolve_api_key_sync(db, provider, None)
            if not api_key:
                logger.warning(f"⚠️ [同步查询] 厂家 {provider} 无 API Key，请在 Web UI 配置管理中添加")

            _model_metadata_cache.set(
                model_name,
                {
                    "provider": provider,
                    "backend_url": backend_url,
                },
            )
            return {
                "provider": provider,
                "backend_url": backend_url,
                "api_key": api_key,
            }
        except Exception as e:
            logger.warning(f"⚠️ [同步查询] 无法查询厂家配置: {e}")

        # 最后回退到硬编码的默认 URL
        result = {
            "provider": provider,
            "backend_url": _get_default_backend_url(provider),
            "api_key": None,
        }
        _model_metadata_cache.set(
            model_name,
            {
                "provider": provider,
                "backend_url": result["backend_url"],
            },
        )
        return result
    except Exception as e:
        logger.error(f"❌ [同步查询] 查找模型供应商失败: {e}", exc_info=True)
        provider = _get_default_provider_by_model(model_name)
        fallback_result = {
            "provider": provider,
            "backend_url": _get_default_backend_url(provider),
            "api_key": None,
        }
        _model_metadata_cache.set(
            model_name,
            {
                "provider": provider,
                "backend_url": fallback_result["backend_url"],
            },
        )
        return fallback_result


def _get_default_backend_url(provider: str) -> str:
    """根据供应商名称返回默认的 backend_url"""
    default_urls = {
        "google": "https://generativelanguage.googleapis.com/v1beta",
        "dashscope": "https://dashscope.aliyuncs.com/api/v1",
        "openai": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com",
        "anthropic": "https://api.anthropic.com",
        "openrouter": "https://openrouter.ai/api/v1",
        "qianfan": "https://qianfan.baidubce.com/v2",
        "302ai": "https://api.302.ai/v1",
    }

    url = default_urls.get(provider, "https://dashscope.aliyuncs.com/compatible-mode/v1")
    return url


def _get_default_provider_by_model(model_name: str) -> str:
    """根据模型名称返回默认的供应商映射"""
    model_provider_map = {
        "qwen-turbo": "dashscope",
        "qwen-plus": "dashscope",
        "qwen-max": "dashscope",
        "qwen-plus-latest": "dashscope",
        "qwen-max-longcontext": "dashscope",
        "gpt-3.5-turbo": "openai",
        "gpt-4": "openai",
        "gpt-4-turbo": "openai",
        "gpt-4o": "openai",
        "gpt-4o-mini": "openai",
        "gemini-pro": "google",
        "gemini-2.0-flash": "google",
        "gemini-2.0-flash-thinking-exp": "google",
        "deepseek-chat": "deepseek",
        "deepseek-coder": "deepseek",
        "deepseek-v4-flash": "deepseek",
        "deepseek-v4-pro": "deepseek",
        "deepseek-reasoner": "deepseek",
        "glm-4": "zhipu",
        "glm-3-turbo": "zhipu",
        "chatglm3-6b": "zhipu",
    }
    provider = model_provider_map.get(model_name, "dashscope")
    return provider


# DeepSeek 旧模型弃用提醒（模块级常量，避免重复定义）
_DEPRECATED_MODELS = {
    "deepseek-chat": ("deepseek-v4-flash", "2026/07/24"),
    "deepseek-reasoner": ("deepseek-v4-pro", "2026/07/24"),
}


def create_analysis_config(
    selected_analysts: list,
    analyst_model: str,
    debate_model: str,
    llm_provider: str,
    market_type: str = "A股",
    analyst_model_config: dict = None,
    debate_model_config: dict = None,
) -> dict:
    """创建分析配置"""

    # 统一复制默认配置
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = llm_provider
    config["debate_llm"] = debate_model
    config["analyst_llm"] = analyst_model

    for model_name in [analyst_model, debate_model]:
        if model_name in _DEPRECATED_MODELS:
            replacement, date = _DEPRECATED_MODELS[model_name]
            logger.warning(f"[Deprecation] 模型 '{model_name}' 将于 {date} 弃用，请迁移至 '{replacement}'")

    # 轮次由阶段配置决定
    config["max_debate_rounds"] = 1
    config["max_risk_discuss_rounds"] = 1
    config["memory_enabled"] = True
    config["online_tools"] = True

    try:
        analyst_provider_info = get_provider_and_url_by_model_sync(analyst_model)
        debate_provider_info = get_provider_and_url_by_model_sync(debate_model)

        config["backend_url"] = analyst_provider_info["backend_url"]
        config["analyst_api_key"] = analyst_provider_info.get("api_key")
        config["debate_api_key"] = debate_provider_info.get("api_key")

        # 始终设置 per-model provider，让 create_llm() 精确路由
        config["analyst_provider"] = analyst_provider_info["provider"]
        config["debate_provider"] = debate_provider_info["provider"]
        config["analyst_backend_url"] = analyst_provider_info["backend_url"]
        config["debate_backend_url"] = debate_provider_info["backend_url"]
    except Exception as e:
        logger.warning(f"⚠️  无法从数据库获取 backend_url 和 API Key: {e}")
        config["backend_url"] = _get_default_backend_url(llm_provider)
        # 回退：使用传入的 llm_provider 作为两个模型的 provider
        config["analyst_provider"] = llm_provider
        config["debate_provider"] = llm_provider
        config["analyst_backend_url"] = config["backend_url"]
        config["debate_backend_url"] = config["backend_url"]

    config["selected_analysts"] = selected_analysts
    config["debug"] = False

    if analyst_model_config:
        config["analyst_model_config"] = analyst_model_config
    if debate_model_config:
        config["debate_model_config"] = debate_model_config

    # 阶段配置默认值（交易员始终执行）
    config.setdefault("phase2_enabled", False)
    config.setdefault("phase2_debate_rounds", 1)
    config.setdefault("phase3_enabled", False)
    config.setdefault("phase3_debate_rounds", 1)
    config.setdefault("phase4_enabled", True)
    config.setdefault("phase4_debate_rounds", 1)
    config.setdefault("max_debate_rounds", 1)
    config.setdefault("max_risk_discuss_rounds", 1)

    return config


# -----------------------------------------------------------------------------
# AnalysisService Class
# -----------------------------------------------------------------------------
