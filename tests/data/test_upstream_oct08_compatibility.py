"""Oct-08 upstream integration guards; real objects/files, no network or mocks.

These regressions cover rollout compatibility, not the separately pending
minute-window, field-completeness or MCP data-quality repairs.
"""

import ast
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SCHEDULE = ROOT / "app/data/scheduler/jobs/cn/schedule.yaml"


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate mappings rather than silently accepting the last one."""


def _unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.constructor.ConstructorError(
                "mapping", node.start_mark, f"duplicate key: {key}", key_node.start_mark
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
)


def _tree(relative_path):
    return ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))


def _named(nodes, node_type, name):
    matches = [n for n in nodes if isinstance(n, node_type) and n.name == name]
    assert len(matches) == 1, f"expected one {name}, found {len(matches)}"
    return matches[0]


def _calls(node, function_name):
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == function_name
    ]


def test_unique_key_loader_rejects_silent_schedule_overrides():
    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key"):
        yaml.load("dragon_tiger: {}\ndragon_tiger: {}\n", Loader=UniqueKeyLoader)


def test_cn_schedule_retains_free_sources_and_disabled_margin():
    config = yaml.load(SCHEDULE.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    assert config["daily_quotes"]["source"] == "duckdb"
    assert config["daily_indicators"]["source"] == "tencent"
    assert config["market_quotes"]["source"] == "tencent"
    assert config["margin_trading"]["enabled"] is False
    for domain in ("dragon_tiger", "block_trade"):
        assert config[domain]["source"] == "akshare"
        assert "daily_quotes" in config[domain]["depends_on"]


def test_disabled_margin_is_not_registered_with_real_scheduler(scheduler_engine):
    # Loading registers real APScheduler jobs, but never starts or executes them.
    scheduler_engine.load_schedule("CN", str(SCHEDULE))
    scheduler = scheduler_engine.get_scheduler()
    assert scheduler.get_job("cn_margin_trading") is None
    assert ("CN", "margin_trading") not in scheduler_engine._job_configs
    for domain in ("daily_quotes", "dragon_tiger", "block_trade"):
        assert scheduler.get_job(f"cn_{domain}") is not None


@pytest.mark.parametrize("market", ["CN", "HK", "US"])
@pytest.mark.parametrize("source", [None, "duckdb", "tushare", "akshare", "baostock", "yfinance"])
def test_daily_jobs_preserve_non_cn_and_explicit_free_source_paths(market, source):
    from app.data.scheduler.jobs.cn import CNDailyQuotesJob
    from app.data.scheduler.jobs.hk import HKDailyQuotesJob
    from app.data.scheduler.jobs.us import USDailyQuotesJob

    job = {"CN": CNDailyQuotesJob, "HK": HKDailyQuotesJob, "US": USDailyQuotesJob}[market]()
    job.preferred_source = source
    expected = market != "CN" or source not in (None, "duckdb", "tushare")
    assert job._needs_symbol_list() is expected


@pytest.mark.parametrize(
    "flag,call_names",
    [
        ("DATA_FRESHNESS_PATROL_ENABLED", ("startup_patrol", "patrol_loop")),
        ("DATA_REFRESH_CONSUMER_ENABLED", ("start_refresh_consumer",)),
    ],
)
def test_new_background_jobs_default_off_and_every_call_is_guarded(flag, call_names):
    from app.core.config import Settings

    # Inspect declared defaults, not environment-dependent effective settings.
    assert Settings.model_fields[flag].default is False
    function = _named(_tree("app/main.py").body, ast.AsyncFunctionDef, "_init_scheduler")
    guards = [
        n for n in ast.walk(function)
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Attribute)
        and isinstance(n.test.value, ast.Name)
        and n.test.value.id == "settings"
        and n.test.attr == flag
    ]
    assert len(guards) == 1
    guarded_body = ast.Module(body=guards[0].body, type_ignores=[])
    for call_name in call_names:
        all_calls = _calls(function, call_name)
        guarded_calls = _calls(guarded_body, call_name)
        assert all_calls, f"expected retained upstream entry point: {call_name}"
        assert {id(n) for n in all_calls} == {id(n) for n in guarded_calls}


def test_data_interface_retains_local_and_upstream_methods_once():
    interface = _named(
        _tree("app/data/core/interface.py").body, ast.ClassDef, "DataInterface"
    )
    for name in ("fetch_market_quotes", "reset_source_circuit", "technical_snapshot"):
        method = _named(interface.body, ast.AsyncFunctionDef, name)
        assert method.body
    refresh = _named(interface.body, ast.AsyncFunctionDef, "refresh")
    assert {"start_date", "end_date"}.issubset(
        {argument.arg for argument in refresh.args.kwonlyargs}
    )


def test_basic_info_dispatch_retains_symbol_and_market_arguments():
    router = _named(
        _tree("app/data/processor/fallback_router.py").body, ast.ClassDef, "FallbackRouter"
    )
    fetch_raw = _named(router.body, ast.AsyncFunctionDef, "_fetch_raw")
    candidates = []
    for node in ast.walk(fetch_raw):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "basic_info":
                candidates.append(value)
    assert len(candidates) == 1
    expression = candidates[0]
    assert isinstance(expression, ast.Lambda)
    call = expression.body
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Attribute)
    assert call.func.attr == "get_stock_list"
    arguments = {keyword.arg: keyword.value for keyword in call.keywords}
    for name in ("symbol", "market"):
        assert isinstance(arguments.get(name), ast.Name)
        assert arguments[name].id == name


def test_cn_duckdb_bridge_and_business_checkpoint_are_retained():
    sync = _named(
        _tree("app/data/scheduler/jobs/base/sync_job.py").body, ast.ClassDef, "BaseSyncJob"
    )
    _named(sync.body, ast.AsyncFunctionDef, "_sync_cn_duckdb_daily_quotes")
    execute = _named(sync.body, ast.AsyncFunctionDef, "execute")
    bridge_calls = [
        n for n in ast.walk(execute)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_sync_cn_duckdb_daily_quotes"
    ]
    assert len(bridge_calls) == 1
    assignments = [
        n for n in ast.walk(execute)
        if isinstance(n, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "checkpoint_date" for target in n.targets)
    ]
    assert any(
        isinstance(n.value, ast.Subscript)
        and isinstance(n.value.value, ast.Name)
        and n.value.value.id == "bulk_result"
        and isinstance(n.value.slice, ast.Constant)
        and n.value.slice.value == "checkpoint_date"
        for n in assignments
    )
