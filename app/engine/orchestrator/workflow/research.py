"""Period routing and task-scoped role snapshots. Short workflows are untouched.

Period prompts/tool lists live in workflow_specs (DB authority), not runtime string
patches to agent_specs. A copied custom workflow must explicitly carry a compatible
profile; we never silently replace a user's custom workflow with a builtin one.
"""
from contextvars import ContextVar
from typing import Optional

from app.core.analysis_horizon import normalize_analysis_horizon
from .compiler import CompileError, compile_workflow, params_from_legacy_config
from .spec import CompileParams, ResearchProfile, ResearchRole, WorkflowSpec

ACTIVE_RESEARCH_PROFILE: ContextVar[Optional[ResearchProfile]] = ContextVar(
    "active_research_profile", default=None
)
PERIOD_WORKFLOWS = {"medium": "research-medium", "long": "research-long"}


def resolve_workflow_slug(slug: str, horizon: object = None) -> str:
    period = normalize_analysis_horizon(horizon)
    if slug == "default-4stage" and period != "short":
        return PERIOD_WORKFLOWS[period]
    return slug


def role_for(slug: str) -> Optional[ResearchRole]:
    profile = ACTIVE_RESEARCH_PROFILE.get()
    if profile is None:
        return None
    role = profile.roles.get(slug)
    if role is None:
        raise CompileError(f"{profile.horizon} 研究工作流未配置角色 {slug}，禁止回退短线提示词")
    return role


def role_prompt(slug: str, original: str) -> str:
    role = role_for(slug)
    return role.prompt if role else original


def research_display_name(identifier: str) -> str:
    """Do not cache period names in the shared (short) registry."""
    profile = ACTIVE_RESEARCH_PROFILE.get()
    if profile is None:
        return ""
    from .. import registry
    for item in registry.load_identities():
        if identifier in (item.slug, item.node_name, item.event_key, *item.report_keys):
            role = profile.roles.get(item.slug)
            return role.name if role else ""
    return ""


def period_params(spec: WorkflowSpec, params: CompileParams, horizon: object) -> CompileParams:
    period = normalize_analysis_horizon(horizon)
    profile = spec.research_profile
    if period == "short":
        if profile is not None:
            raise CompileError("短线任务不能使用中长线研究工作流，请选择原短线工作流")
        return params
    if profile is None or profile.horizon != period:
        raise CompileError("工作流与分析周期不匹配；请选择对应周期研究工作流，或复制并配置该周期研究方案")
    if not spec.enabled:
        raise CompileError("所选研究工作流已禁用")
    from .. import registry
    aliases = {}
    for item in registry.load_identities():
        if item.is_analyst:
            for key in (item.slug, item.internal_key, item.name):
                aliases[key] = item.slug
    selected = tuple(dict.fromkeys((*profile.required_analysts, *(aliases.get(s, s) for s in params.selected_nodes))))
    missing = [s for s in selected if s not in aliases or s not in profile.roles]
    if missing:
        raise CompileError(f"研究分析师不存在、已删除或缺周期提示词：{', '.join(missing)}")
    return params.model_copy(update={"selected_nodes": selected})


def prepare_workflow(config: dict, selected_analysts):
    """Shared by API validation, task admission, and execution (no model calls)."""
    from .loader import check_required_inputs, default_workflow_slug, load_workflow_by_slug
    horizon = normalize_analysis_horizon(config.get("analysis_horizon"))
    slug = resolve_workflow_slug(config.get("workflow_slug") or default_workflow_slug(), horizon)
    spec, spec_hash = load_workflow_by_slug(slug)
    params = period_params(spec, params_from_legacy_config(config, selected_analysts), horizon)
    plan = compile_workflow(spec, params, spec_hash=spec_hash)
    errors = check_required_inputs(spec, plan)
    if errors:
        raise CompileError("；".join(errors))
    return spec.model_copy(deep=True), spec_hash, plan
