#!/bin/sh
# Start only this project, without building. Refuses a tight memory margin.
set -eu
. "$(dirname "$0")/common.sh"

root=$(trial_root)
assert_project
secret=$(trial_secret_file)
[ -f "$secret" ] || die "先运行 bin/install.sh" 12
set -a
# shellcheck disable=SC1090
. "$secret"
set +a

# preflight exits 10 when build is blocked but start may still be allowed.
set +e
"$(dirname "$0")/preflight.sh"
code=$?
set -e
if [ "$code" -eq 11 ] || [ "$code" -eq 12 ]; then
    exit "$code"
fi
if [ "$code" -eq 10 ]; then
    mem_total=$(docker_mem_total_mib || printf '0')
    mem_used=$(docker_used_mib || printf '0')
    mem_avail=$((mem_total - mem_used))
    if [ "$mem_avail" -lt 2600 ]; then
        die "剩余内存不足以启动。不要停止生产容器。" 10
    fi
    printf '构建仍被拒绝。若镜像已在本机，则只启动、不构建。\n'
fi

tag="${TRIAL_IMAGE_TAG:-unknown}"
docker image inspect "tradingagents-smc-tpo-trial-backend:${tag}" >/dev/null 2>&1 \
    || die "后端镜像不存在。资源预检拒绝构建时，不能在这台机器上现编镜像。" 10
docker image inspect "tradingagents-smc-tpo-trial-frontend:${tag}" >/dev/null 2>&1 \
    || die "前端镜像不存在。资源预检拒绝构建时，不能在这台机器上现编镜像。" 10

cd "$root"
docker compose \
    --project-name "$TRIAL_PROJECT" \
    --env-file "$secret" \
    -f deploy/trial/compose.yml \
    up -d --no-build
printf '已启动 %s。入口 http://127.0.0.1:%s\n' "$TRIAL_PROJECT" "${TRIAL_HTTP_PORT:-18082}"
printf '口令文件: %s\n' "$secret"
