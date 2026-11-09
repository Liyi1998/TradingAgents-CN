"""Real Mongo configuration routing to local TCP failure; no model generation."""

import socket
import uuid

from app.core.database import close_database, get_mongo_db, init_database
from app.models.config import LLMConfig, SystemConfig
from app.services.config.llm_service import LLMService


async def test_model_and_provider_checks_inherit_saved_timeout(mongodb_available):
    import os

    previous = os.environ.get("LLM_PRIVATE_HOST_ALLOWLIST")
    os.environ["LLM_PRIVATE_HOST_ALLOWLIST"] = "127.0.0.1"
    await init_database()
    db = get_mongo_db()
    assert "test" in db.name or "validation" in db.name, "test-owned database required"
    provider_id = config_id = None
    try:
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            base = f"http://127.0.0.1:{reserved.getsockname()[1]}/v1"
            name = "custom_probe_" + uuid.uuid4().hex
            provider_id = (await db.llm_providers.insert_one({
                "name": name, "display_name": "Local TCP check", "is_active": True,
                "api_key": "local-probe-noncredential", "default_base_url": base,
            })).inserted_id
            config = LLMConfig(provider=name, model_name="local-probe", timeout=1, enabled=True)
            stored = SystemConfig(config_name=name, config_type="system", llm_configs=[config])
            document = stored.model_dump(by_alias=True, exclude={"id"})
            document.update(is_active=True, version=1000000)
            config_id = (await db.system_configs.insert_one(document)).inserted_id
            service = LLMService()
            model_result = await service.test_llm_config(config)
            provider_result = await service.test_provider_api(str(provider_id))
            for result in [model_result, provider_result]:
                assert not result["success"]
                assert result["details"]["timeout_seconds"] == 1
                assert result["details"]["kind"] in {"connection", "connection_timeout"}
                assert "noncredential" not in result["message"]
    finally:
        if provider_id is not None:
            await db.llm_providers.delete_one({"_id": provider_id})
        if config_id is not None:
            await db.system_configs.delete_one({"_id": config_id})
        await close_database()
        if previous is None:
            os.environ.pop("LLM_PRIVATE_HOST_ALLOWLIST", None)
        else:
            os.environ["LLM_PRIVATE_HOST_ALLOWLIST"] = previous
