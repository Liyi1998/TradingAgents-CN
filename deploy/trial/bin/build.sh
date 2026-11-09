#!/bin/sh
# Sequential native build. Refuses when preflight blocks. No prune.
set -eu
. "$(dirname "$0")/common.sh"

root=$(trial_root)
assert_project
secret=$(trial_secret_file)
[ -f "$secret" ] || die "先运行 bin/install.sh。口令文件应在 $(trial_secret_file)" 12
set -a
# shellcheck disable=SC1090
. "$secret"
set +a
export TRIAL_IMAGE_TAG="${TRIAL_IMAGE_TAG:-unknown}"

"$(dirname "$0")/preflight.sh"

printf '开始单线程构建。只构建本 project 的镜像。\n'
cd "$root"
COMPOSE_PARALLEL_LIMIT=1 docker compose \
    --project-name "$TRIAL_PROJECT" \
    --env-file "$secret" \
    -f deploy/trial/compose.yml \
    build --progress=plain backend
COMPOSE_PARALLEL_LIMIT=1 docker compose \
    --project-name "$TRIAL_PROJECT" \
    --env-file "$secret" \
    -f deploy/trial/compose.yml \
    build --progress=plain frontend
printf '构建完成。镜像名带项目前缀 tradingagents-smc-tpo-trial-。\n'
