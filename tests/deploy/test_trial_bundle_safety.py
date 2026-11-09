"""Static guards for the isolated Mac trial bundle."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TRIAL = ROOT / "deploy" / "trial"
COMPOSE = (TRIAL / "compose.yml").read_text(encoding="utf-8")
SCRIPTS = list((TRIAL / "bin").glob("*.sh")) + [TRIAL / "bin" / "common.sh"]
FORBIDDEN = (
    "tradingagents-cn-hub_tradingagents-network",
    "tradingagents-cn-hub_tradingagents_mongodb_data",
    "tradingagents-cn-hub_tradingagents_redis_data",
)


def test_compose_names_ports_and_memory_budget():
    data = yaml.safe_load(COMPOSE)
    assert data["name"] == "tradingagents-smc-tpo-trial"
    assert "\ncontainer_name:" not in COMPOSE and not COMPOSE.startswith("container_name:")
    assert "external:" not in COMPOSE
    for name in FORBIDDEN:
        assert name not in COMPOSE
    assert '127.0.0.1:${TRIAL_HTTP_PORT:-18082}:80' in COMPOSE
    assert "27018" not in COMPOSE and "6380" not in COMPOSE
    limits = [512, 128, 1024, 64, 64]
    assert sum(limits) <= 2048
    assert data["networks"]["trial"]["name"] == "tradingagents-smc-tpo-trial_net"
    assert data["volumes"]["trial_mongodb"]["name"].startswith("tradingagents-smc-tpo-trial_")
    assert data["services"]["backend"]["environment"]["BACKGROUND_JOBS_ENABLED"] == "false"
    assert data["services"]["backend"]["environment"]["TUSHARE_ENABLED"] == "false"
    assert data["services"]["backend"]["environment"]["QUOTES_INGEST_ENABLED"] == "false"


def test_scripts_cannot_prune_or_stop_production():
    text = "\n".join(path.read_text(encoding="utf-8") for path in SCRIPTS)
    for banned in ("docker system prune", "docker volume prune", "docker compose down -v", "--volumes"):
        assert banned not in text, banned
    assert "tradingagents-smc-tpo-trial" in text
    assert "18080" in text and "18081" in text
    rollback = (TRIAL / "bin" / "rollback.sh").read_text(encoding="utf-8")
    assert "--remove-orphans" in rollback
    assert "docker volume rm" in rollback
    assert "${TRIAL_PROJECT}_*" in rollback


def test_background_jobs_default_on_and_trial_scheduler_gate():
    config = (ROOT / "app" / "core" / "config.py").read_text(encoding="utf-8")
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "BACKGROUND_JOBS_ENABLED: bool = Field(default=True)" in config
    assert "if not settings.BACKGROUND_JOBS_ENABLED:" in main


def test_swap_parser_reads_macos_sysctl():
    import subprocess

    script = """
. deploy/trial/bin/common.sh
swap_used_mib_from_text 'total = 4096.00M  used = 3594.00M  free = 100.00M'
"""
    result = subprocess.run(["sh", "-c", script], cwd=ROOT, check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "3594"
