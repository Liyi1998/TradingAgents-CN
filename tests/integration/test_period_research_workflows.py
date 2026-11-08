"""Real Mongo configuration/compilation/prompt assembly; no model calls or mocks."""
import asyncio
import hashlib
from pathlib import Path

import pytest

from app.core.database import get_mongo_db_sync
from app.engine.agents.utils.agent_config import load_agent_config
from app.engine.orchestrator.agents import build_analyst_specs
from app.engine.orchestrator.workflow import seeds, seeder, store
from app.engine.orchestrator.workflow.compiler import CompileError, compile_workflow, params_from_legacy_config
from app.engine.orchestrator.workflow.loader import check_required_inputs, load_workflow_by_slug
from app.engine.orchestrator.workflow.research import (
    ACTIVE_RESEARCH_PROFILE, period_params, prepare_workflow, resolve_workflow_slug, role_for, role_prompt,
)
from app.engine.orchestrator.workflow.spec import CompileParams
from app.routers.workflows import ValidateRunPayload, validate_run


@pytest.fixture(scope="module", autouse=True)
def real_store():
    db = get_mongo_db_sync()
    assert "test" in db.name or "validation" in db.name, "never run against production"
    assert db.command("ping")["ok"] == 1
    seeder.sync_all()
    yield db
    store.invalidate_store_cache()


@pytest.mark.parametrize("horizon", ["medium", "long"])
def test_real_compilation_has_required_research_and_risk_without_short_capital(horizon):
    spec, _, plan = prepare_workflow({"analysis_horizon": horizon, "workflow_slug": "default-4stage"}, [])
    assert plan.workflow_slug == f"research-{horizon}"
    assert plan.params.selected_nodes == ("fundamentals-analyst", "financial-news-analyst")
    assert len(plan.stages) == 5
    assert plan.stages[1].rounds == (0 if horizon == "medium" else 1)
    assert plan.stages[3].rounds == 0
    assert check_required_inputs(spec, plan) == []
    assert plan.total_units() == (11 if horizon == "medium" else 13)
    for stage_id in (spec.stages[1].id, spec.stages[3].id):
        with pytest.raises(CompileError, match="非 optional"):
            prepare_workflow({"analysis_horizon": horizon,
                              "stage_overrides": {stage_id: {"enabled": False}}}, [])


@pytest.mark.parametrize("horizon", ["medium", "long"])
@pytest.mark.parametrize("slug", ["fundamentals-analyst", "market-analyst", "financial-news-analyst",
                                 "china-market-analyst", "social-media-analyst", "short-term-capital-analyst",
                                 "bull-researcher", "bear-researcher", "research-manager", "trader",
                                 "risky-analyst", "safe-analyst", "neutral-analyst", "risk-manager", "summary"])
def test_each_role_replaces_short_body_from_frozen_db_profile(horizon, slug):
    spec, _, _ = prepare_workflow({"analysis_horizon": horizon}, [])
    original = store.get_agent_spec(slug)
    original_prompt = (original or {}).get("roleDefinition", "short-summary")
    token = ACTIVE_RESEARCH_PROFILE.set(spec.research_profile)
    try:
        actual = role_prompt(slug, original_prompt)
        assert actual == spec.research_profile.roles[slug].prompt
        assert actual != original_prompt
        assert ("1–3个月" if horizon == "medium" else "6–12个月") in actual
        assert "单项缺失不终止" in actual
        if slug != "summary":
            assert load_agent_config(slug) == actual
    finally:
        ACTIVE_RESEARCH_PROFILE.reset(token)
    assert role_prompt(slug, original_prompt) == original_prompt
    assert store.get_agent_spec(slug) == original


def test_original_agent_seeds_byte_identical_and_short_plan_unmodified():
    root = Path(__file__).resolve().parents[2]
    assert hashlib.sha256((root / "config/seeds/agents.json").read_bytes()).hexdigest() == \
        "cca899e05c6c4da3c44d69bc3aa24f0b69d146b09cec93abab5933df44871de9"
    selected = ["market-analyst", "short-term-capital-analyst"]
    for enabled2, enabled3 in ((True, True), (True, False), (False, True), (False, False)):
        config = {"phase2_enabled": enabled2, "phase3_enabled": enabled3}
        original, original_hash = load_workflow_by_slug("default-4stage")
        expected = compile_workflow(original, params_from_legacy_config(config, selected), spec_hash=original_hash).snapshot()
        actual = prepare_workflow(config, selected)[2].snapshot()
        expected.pop("compiled_at")
        actual.pop("compiled_at")
        assert actual == expected
    assert role_for("trader") is None


@pytest.mark.parametrize("horizon", ["medium", "long"])
async def test_real_analyst_assembly_uses_period_tools_without_mutating_short(horizon):
    profile = prepare_workflow({"analysis_horizon": horizon}, ["market-analyst", "short-term-capital-analyst"])[0].research_profile
    before = store.get_agent_spec("market-analyst")
    token = ACTIVE_RESEARCH_PROFILE.set(profile)
    try:
        specs = await build_analyst_specs(["market-analyst", "fundamentals-analyst", "short-term-capital-analyst"], None)
        assert "intraday_quotes" not in specs["market"].declared_tool_ids
        assert "official_announcements" in specs["fundamentals"].declared_tool_ids
        assert "financial_data" in specs["fundamentals"].declared_tool_ids
        assert profile.roles["market-analyst"].prompt in specs["market"].system_prompt
        assert before["roleDefinition"] not in specs["market"].system_prompt
        if horizon == "long":
            assert "money_flow" not in specs["short_term_capital"].declared_tool_ids
    finally:
        ACTIVE_RESEARCH_PROFILE.reset(token)
    assert store.get_agent_spec("market-analyst") == before


@pytest.mark.parametrize("horizon", ["medium", "long"])
async def test_api_preview_and_execution_compile_agree(horizon):
    payload = ValidateRunPayload(workflow_slug="default-4stage", analysis_horizon=horizon,
                                 selected_nodes=["market-analyst"])
    preview = (await validate_run(payload, user={"id": "isolated-test"}))["data"]
    plan = prepare_workflow(payload.model_dump(), payload.selected_nodes)[2]
    assert preview["valid"], preview
    assert preview["plan"]["workflow_slug"] == plan.workflow_slug
    assert preview["plan"]["selected_nodes"] == list(plan.params.selected_nodes)
    assert preview["plan"]["total_units"] == plan.total_units()


def test_custom_workflow_not_silently_replaced_and_period_mismatch_rejected():
    assert resolve_workflow_slug("custom-user-workflow", "long") == "custom-user-workflow"
    short = load_workflow_by_slug("default-4stage")[0]
    with pytest.raises(CompileError, match="不匹配"):
        period_params(short, CompileParams(), "long")
    with pytest.raises(CompileError, match="短线任务"):
        prepare_workflow({"analysis_horizon": "short", "workflow_slug": "research-long"}, [])
    with pytest.raises(CompileError, match="不存在"):
        prepare_workflow({"analysis_horizon": "long"}, ["unconfigured-custom-analyst"])


async def test_concurrent_periods_are_context_isolated():
    async def read(period):
        profile = prepare_workflow({"analysis_horizon": period}, [])[0].research_profile
        token = ACTIVE_RESEARCH_PROFILE.set(profile)
        try:
            await asyncio.sleep(0)
            return load_agent_config("trader")
        finally:
            ACTIVE_RESEARCH_PROFILE.reset(token)
    medium, long = await asyncio.gather(read("medium"), read("long"))
    assert "1–3个月" in medium and "6–12个月" in long
    assert medium != long
    assert ACTIVE_RESEARCH_PROFILE.get() is None


def test_custom_db_period_prompt_survives_seed_sync_and_loaded_snapshot_is_frozen(real_store):
    spec, _, _ = prepare_workflow({"analysis_horizon": "long"}, [])
    custom = spec.model_dump(mode="json")
    custom["slug"] = "test-period-research-custom"
    custom["builtin"] = False
    custom["research_profile"]["roles"]["trader"]["prompt"] += "\n用户定制：重点审查资本配置。"
    try:
        store.save_workflow_spec(custom)
        frozen, old_hash, _ = prepare_workflow({"analysis_horizon": "long", "workflow_slug": custom["slug"]}, [])
        seeder.sync_all()
        assert store.get_workflow_spec(custom["slug"]) == custom
        custom["research_profile"]["roles"]["trader"]["prompt"] += "\n后续修改。"
        store.save_workflow_spec(custom)
        assert "后续修改" not in frozen.research_profile.roles["trader"].prompt
        assert prepare_workflow({"analysis_horizon": "long", "workflow_slug": custom["slug"]}, [])[1] != old_hash
    finally:
        real_store.workflow_specs.delete_one({"_id": custom["slug"]})
        store.invalidate_store_cache()


def test_deleted_research_workflow_not_revived_by_seeder(real_store):
    original = real_store.workflow_specs.find_one({"_id": "research-long"})
    try:
        store.delete_workflow_spec("research-long")
        seeder.sync_all()
        assert store.get_workflow_spec("research-long") is None
    finally:
        real_store.workflow_specs.replace_one({"_id": "research-long"}, original)
        store.invalidate_store_cache()


@pytest.mark.parametrize("horizon,expected", [("short", (30, 100, 15)),
                                              ("medium", (90, 200, 30)), ("long", (365, 400, 60))])
def test_news_and_sentiment_share_bounded_period_window(horizon, expected):
    from app.engine.tools.datasources.news import news_research_scope
    assert news_research_scope({"analysis_horizon": horizon}) == expected
    assert news_research_scope({"analysis_horizon": horizon}, 8)[2] == 8
    if horizon != "short":
        assert news_research_scope({"analysis_horizon": horizon}, 10000)[2] == 80


def test_original_default_workflow_seed_stays_identical_to_accepted_baseline():
    import json
    # Fixture captured from the accepted pre-change default workflow, not regenerated.
    raw = next(s for s in seeds.load_workflow_seeds() if s["slug"] == "default-4stage")
    baseline_path = Path(__file__).resolve().parents[1] / "fixtures/short_workflow_baseline.json"
    assert raw == json.loads(baseline_path.read_text())
