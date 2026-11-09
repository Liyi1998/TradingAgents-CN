#!/bin/sh
# Shared guards for the isolated SMC/TPO trial. POSIX sh, including macOS /bin/sh.
set -eu

TRIAL_PROJECT=tradingagents-smc-tpo-trial
TRIAL_HTTP_PORT_DEFAULT=18082
FORBIDDEN_PORTS="8080 18080 18081 27017 6379"
FORBIDDEN_NAMES="
tradingagents-cn-hub
tradingagents-cn-hub_tradingagents-network
tradingagents-cn-hub_tradingagents_mongodb_data
tradingagents-cn-hub_tradingagents_redis_data
tradingagents-mongodb
tradingagents-redis
tradingagents-backend
tradingagents-frontend
tradingagents-nginx
"

trial_root() {
    here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
    cd "$here/../../.." && pwd
}

trial_secret_file() {
    printf '%s\n' "${TRIAL_SECRET_FILE:-$HOME/.config/tradingagents-smc-tpo-trial/trial.env}"
}

die() {
    printf 'trial: %s\n' "$1" >&2
    exit "${2:-12}"
}

assert_project() {
    [ "$TRIAL_PROJECT" = "tradingagents-smc-tpo-trial" ] || die "project name was changed" 12
}

assert_not_production_tree() {
    root=$1
    case "$root" in
        */Projects/TradingAgents-CN|*/Projects/TradingAgents-CN/*)
            if [ "${TRIAL_ALLOW_INSIDE_GIT:-}" != "1" ]; then
                die "目的目录在现有源码树内。复制会让 git status 出现未跟踪文件。默认拒绝。确认后才可设 TRIAL_ALLOW_INSIDE_GIT=1，脚本仍不会执行任何 git 写操作。" 12
            fi
            ;;
    esac
    if [ -d "$root/.git" ]; then
        die "试运行目录不能是一个 git 仓库，避免误操作现有源码。" 12
    fi
}

assert_compose_text() {
    root=$1
    file="$root/deploy/trial/compose.yml"
    [ -f "$file" ] || die "缺少 compose.yml" 12
    for name in $FORBIDDEN_NAMES; do
        [ -n "$name" ] || continue
        if grep -F "$name" "$file" >/dev/null 2>&1; then
            die "compose 含有禁止引用的生产名称: $name" 12
        fi
    done
    if grep -E '^[[:space:]]*container_name:|^[[:space:]]*external:[[:space:]]*true|^[[:space:]]*docker[[:space:]]+system[[:space:]]+prune' "$file" >/dev/null 2>&1; then
        die "compose 含有 container_name、external 卷或清理命令" 12
    fi
    if ! grep -F '127.0.0.1:${TRIAL_HTTP_PORT:-18082}:80' "$file" >/dev/null 2>&1; then
        die "对外端口必须只绑定 127.0.0.1" 12
    fi
}

assert_port() {
    port=$1
    for blocked in $FORBIDDEN_PORTS; do
        [ "$port" != "$blocked" ] || die "端口 $port 被禁止。18080/18081 属于现有桥接服务，8080 是生产入口。" 11
    done
    if command -v lsof >/dev/null 2>&1; then
        if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            die "127.0.0.1:$port 已被占用。不要停止现有进程；可改 TRIAL_HTTP_PORT=18083 后重新预检。" 11
        fi
    fi
}

disk_free_kib() {
    df -k . | awk 'NR==2 {print $4}'
}

swap_used_mib_from_text() {
    printf '%s\n' "$1" | awk '
        {
            if (match($0, /used = [0-9.]+/)) {
                s = substr($0, RSTART, RLENGTH)
                split(s, a, " ")
                printf "%d\n", a[3]
            }
        }
    '
}

swap_used_mib() {
    if ! command -v sysctl >/dev/null 2>&1; then
        printf '0\n'
        return
    fi
    raw=$(sysctl -n vm.swapusage 2>/dev/null || true)
    swap_used_mib_from_text "$raw"
}

docker_mem_total_mib() {
    docker info --format '{{.MemTotal}}' 2>/dev/null | awk '{printf "%d\n", $1/1024/1024}'
}

docker_used_mib() {
    docker stats --no-stream --format '{{.MemUsage}}' 2>/dev/null | awk '
        function to_mib(v, u) {
            if (u ~ /GiB/) return v * 1024
            if (u ~ /MiB/) return v
            if (u ~ /KiB/) return v / 1024
            return v / 1024 / 1024
        }
        {
            split($1, a, "")
            value=$1
            gsub(/[^0-9.]/, "", value)
            unit=$1
            gsub(/[0-9.]/, "", unit)
            sum += to_mib(value + 0, unit)
        }
        END {printf "%d\n", sum}
    '
}
