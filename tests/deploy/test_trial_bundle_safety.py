"""Static guards for the isolated Mac trial bundle."""
import subprocess
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


def test_build_thresholds_stay_in_place():
    preflight = (TRIAL / "bin" / "preflight.sh").read_text(encoding="utf-8")
    assert 'if [ "$swap_mib" -gt 1536 ]; then' in preflight
    assert 'if [ "$mem_total" -lt 6144 ]; then' in preflight
    assert 'if [ "$mem_avail" -lt 4096 ]; then' in preflight
    assert 'if [ "$mem_avail" -lt 2600 ]; then' in preflight
    assert "12 * 1024 * 1024" in preflight
    assert "6 * 1024 * 1024" in preflight
    assert "docker compose" not in preflight
    assert "docker load" not in preflight


def test_prebuilt_path_does_not_build_or_pull():
    load = (TRIAL / "bin" / "preflight-load.sh").read_text(encoding="utf-8")
    runtime = (TRIAL / "bin" / "preflight-runtime.sh").read_text(encoding="utf-8")
    loader = (TRIAL / "bin" / "load-prebuilt.sh").read_text(encoding="utf-8")
    start = (TRIAL / "bin" / "start.sh").read_text(encoding="utf-8")
    for text in (load, runtime, loader, start):
        assert "docker pull" not in text
        assert "docker compose build" not in text
        assert "ignore-warnings" not in text
        assert "docker system prune" not in text
        assert "docker builder prune" not in text
    assert "LOAD_MEM_AVAIL_MIB=3400" in load
    assert "LOAD_DISK_GIB=8" in load
    assert "SWAP_MAX_MIB=4096" in load
    assert "START_MEM_AVAIL_MIB=2816" in runtime
    assert "START_DISK_GIB=6" in runtime
    assert "SWAP_MAX_MIB=4096" in runtime
    assert '"$(dirname "$0")/preflight.sh"' not in runtime
    assert "docker load" in loader
    assert "TRIAL_RUNTIME_APPROVED" in loader
    assert "--no-build" in start
    assert "--pull never" in start
    assert "preflight-runtime.sh" in start
    assert "TRIAL_RUNTIME_APPROVED" in start
    manifest = (TRIAL / "prebuilt-images.txt").read_text(encoding="utf-8")
    assert "archive_sha256 66d74ca1ab39966fb67c19d5881d5295d1b1ce5463c5ec1561a43e4eee919879" in manifest
    assert "platform linux/arm64" in manifest
    assert manifest.count("\nimage ") == 5
    assert " arm64 " in manifest
    assert "amd64" not in manifest
    assert (TRIAL / "IMAGE_TAG").read_text(encoding="utf-8").strip() == "374207d29025aa07336b6484d2a8e5c139f8b1bc"
    parts = (TRIAL / "prebuilt-image-parts.sha256").read_text(encoding="utf-8").strip().splitlines()
    assert len(parts) == 13
    assert parts[0].endswith("image.tar.part-00")
    assert parts[-1].endswith("image.tar.part-12")
    assemble = (TRIAL / "bin" / "assemble-prebuilt.sh").read_text(encoding="utf-8")
    assert "docker load" not in assemble
    assert "docker pull" not in assemble
    for path in list((TRIAL / "bin").glob("*.sh")):
        parsed = subprocess.run(["sh", "-n", str(path)], check=False)
        assert parsed.returncode == 0, path


def test_swap_parser_reads_macos_sysctl():
    import subprocess

    script = """
. deploy/trial/bin/common.sh
swap_used_mib_from_text 'total = 4096.00M  used = 3594.00M  free = 100.00M'
"""
    result = subprocess.run(["sh", "-c", script], cwd=ROOT, check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "3594"
