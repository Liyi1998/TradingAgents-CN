"""Real Mongo, persistent Chroma and local embedding; no chat/model generation."""
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.config import settings
from app.services.experience_service import ExperienceService, experience_text

pytestmark = pytest.mark.requires_db


@pytest.fixture
async def experiences(real_mongo_db):
    from app.data.knowledge.config import get_embedding_config, set_embedding_config_resolver
    from app.llm.providers import resolve_embedding_config
    from app.engine.agents.utils.outcome_memory import collection
    config = os.environ.get("TEST_EMBEDDING_CONFIG_FILE")
    if not config:
        pytest.skip("requires an authenticated real local embedding service")
    cfg = json.loads(Path(config).read_text())
    await real_mongo_db.system_configs.insert_one({"is_active": True, "llm_configs": [{
        "provider": "local_embedding", "model_name": cfg["model"], "model_type": "embedding",
        "enabled": True, "embedding_dimension": 384, "api_base": f"http://127.0.0.1:{cfg['port']}/v1", "api_key": cfg["token"],
    }]})
    set_embedding_config_resolver(resolve_embedding_config)
    assert get_embedding_config(force_refresh=True)
    assert "test" in str(settings.resolve_runtime_path(settings.SETTLEMENT_MEMORY_DIR))
    owner = "experience-" + uuid4().hex
    enabled = settings.SETTLEMENT_REFLECTION_ENABLED
    settings.SETTLEMENT_REFLECTION_ENABLED = True
    try:
        yield ExperienceService(real_mongo_db), owner
    finally:
        collection().delete(where={"user_id": owner})
        settings.SETTLEMENT_REFLECTION_ENABLED = enabled


async def lesson(service, owner, **overrides):
    from app.engine.agents.utils.outcome_memory import save_lesson
    row = {"_id": uuid4().hex, "user_id": owner, "task_id": uuid4().hex,
           "symbol": "999991", "decision_date": "2026-10-20", "horizon_days": 7,
           "status": "settled", "signal": "Buy", "reflection_status": "done",
           "reflection_available_date": "2026-11-01", "reflection_lesson": "突破后需要核验量能及回踩，不能因相似历史而认定当前上涨。"} | overrides
    await service.db.decision_settlements.insert_one(row)
    save_lesson(row, "放量突破后缩量回踩的短线研究", row["reflection_lesson"], row["reflection_available_date"])
    return row


async def prepare(service, owner_id, **overrides):
    return await service.prepare(**({"owner": owner_id, "context_id": uuid4().hex, "kind": "analysis", "stage": "trader",
        "as_of": "2026-11-02", "horizon_days": 7, "situation": "放量突破后回踩"} | overrides))


async def test_durable_body_input_and_explicit_citation(experiences):
    service, owner = experiences
    source = await lesson(service, owner)
    receipt = await prepare(service, owner)
    assert receipt["status"] == "ready" and receipt["entries"][0]["id"] == source["_id"]
    assert source["reflection_lesson"] in experience_text(receipt)
    assert await service.submitted(receipt, owner)
    await service.completed(receipt, owner, f"检查回踩【复盘经验 {source['_id']}】")
    saved = (await service.usage(owner, receipt["context_id"]))[0]
    assert saved["delivery"] == "completed" and saved["entries"][0]["explicitly_cited"]
    await service.db.decision_settlements.update_one({"_id": source["_id"]}, {"$set": {"reflection_lesson": "later text"}})
    assert (await service.usage(owner, receipt["context_id"]))[0]["entries"][0]["lesson"] == source["reflection_lesson"]
    assert not await service.usage("another-user", receipt["context_id"])
    assert await service.by_id("another-user", receipt["id"]) is None


async def test_owner_horizon_date_and_authoritative_status(experiences):
    service, owner = experiences
    source = await lesson(service, owner)
    for change in [{"owner": "no-such-user"}, {"horizon_days": 60}, {"as_of": "2026-10-31"}]:
        receipt = await prepare(service, owner, **change)
        assert receipt["status"] == "no_matches" and not receipt["entries"]
    await service.db.decision_settlements.update_one({"_id": source["_id"]}, {"$set": {"reflection_status": "failed"}})
    assert (await prepare(service, owner))["status"] == "no_matches"


async def test_daily_retry_keeps_original_input_and_no_implicit_citation(experiences):
    service, owner = experiences
    await lesson(service, owner)
    receipt = await prepare(service, owner, kind="daily_research", stage="review", context_id="tracking:first")
    retry = await service.reuse(receipt, owner, "tracking:retry")
    assert retry["entries"] == receipt["entries"] and retry["reused_from"] == receipt["id"]
    await service.submitted(retry, owner)
    await service.completed(retry, owner, "突破后回踩，但没有明确的经验引用标识。")
    saved = (await service.usage(owner, "tracking:retry"))[0]
    assert saved["citation_status"] == "checked" and not saved["entries"][0]["explicitly_cited"]
    assert (await service.usage(owner, "tracking:first"))[0]["delivery"] == "prepared"
    settings.SETTLEMENT_REFLECTION_ENABLED = False
    disabled = await service.reuse(receipt, owner, "tracking:disabled-retry")
    assert disabled["status"] == "disabled" and not disabled["entries"]
    assert experience_text(disabled) == ""


async def test_reflections_owner_scoped_include_pending_and_body(experiences):
    service, owner = experiences
    row = await lesson(service, owner)
    result = await service.reflections(owner)
    assert result["total"] == 1 and result["items"][0]["reflection_lesson"] == row["reflection_lesson"]
    assert "user_id" not in result["items"][0]
    assert not (await service.reflections("another-user"))["items"]
    assert not (await service.reflections(owner, symbol="000001"))["items"]


async def test_disabled_and_invalid_date_do_not_interrupt(experiences):
    service, owner = experiences
    settings.SETTLEMENT_REFLECTION_ENABLED = False
    receipt = await prepare(service, owner)
    assert receipt["status"] == "disabled" and experience_text(receipt) == ""
    settings.SETTLEMENT_REFLECTION_ENABLED = True
    receipt = await prepare(service, owner, as_of="invalid")
    assert receipt["status"] == "unavailable" and not receipt["entries"]


async def test_duplicate_lesson_text_keeps_correct_source_identity(experiences):
    service, owner = experiences
    first = await lesson(service, owner)
    second = await lesson(service, owner)
    receipt = await prepare(service, owner)
    assert {e["id"] for e in receipt["entries"]} == {first["_id"], second["_id"]}


async def test_daily_review_packet_preserves_current_evidence_and_retry_receipt(experiences):
    from app.services.research_tracking import ResearchTrackingService
    service, owner = experiences
    await lesson(service, owner)
    tracking = ResearchTrackingService(service.db)
    track = {"_id": "track", "user_id": owner, "symbol": "002241"}
    packet = {"modules": {"technical": {"content": "放量突破后回踩"}}, "original_plan": "完整原策略不能被替代"}
    receipt, text, enriched = await tracking.prepare_review_experience(track, {"_id": "run"}, "step", packet, {"as_of": "2026-11-02"}, None, 128000)
    assert receipt["kind"] == "daily_research" and text
    payload = json.loads(enriched)
    assert payload["modules"] == packet["modules"] and payload["original_plan"] == packet["original_plan"]
    assert payload["historical_outcome_lessons"] == text and "不是本次行情" in text
    previous = await service.db.research_update_steps.find_one({"_id": "step"})
    await lesson(service, owner, reflection_lesson="后续才写入的另一条经验")
    retry, _, _ = await tracking.prepare_review_experience(track, {"_id": "retry"}, "step", packet, {"as_of": "2026-11-02"}, previous, 128000)
    assert retry["entries"] == receipt["entries"]
    # A lack of context space drops only optional experience, not report facts.
    dropped, addition, _ = await tracking.prepare_review_experience(track, {"_id": "limited"}, "small", packet, {"as_of": "2026-11-02"}, None, 18000)
    assert not addition and not dropped["entries"] and dropped["status"] == "unavailable"
