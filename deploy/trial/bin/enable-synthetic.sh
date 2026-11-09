#!/bin/sh
# Turn on the labeled synthetic fixture directory and seed 900001-900006.
# These symbols are test cards, not holdings.
set -eu
. "$(dirname "$0")/common.sh"
root=$(trial_root)
secret=$(trial_secret_file)
[ -f "$secret" ] || die "缺少口令文件" 12
set -a
# shellcheck disable=SC1090
. "$secret"
set +a
cd "$root"
docker compose \
    --project-name "$TRIAL_PROJECT" \
    --env-file "$secret" \
    -f deploy/trial/compose.yml \
    -f deploy/trial/compose.synthetic.yml \
    up -d --no-build backend
printf '等待后端健康检查通过后写入合成测试档。\n'
i=0
until docker compose --project-name "$TRIAL_PROJECT" -f deploy/trial/compose.yml exec -T backend \
    curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -lt 40 ] || die "后端未在预期时间内就绪" 10
    sleep 3
done
docker compose --project-name "$TRIAL_PROJECT" -f deploy/trial/compose.yml exec -T backend \
    python /opt/trial-seed/seed_synthetic.py
printf '合成模式已开启。页面股票名都带「合成测试」，不是真实持仓。\n'
printf '结构辅助仍默认关闭，需要在页面里单独打开。\n'
