#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"
root=$(trial_root)
secret=$(trial_secret_file)
set -a
# shellcheck disable=SC1090
. "$secret"
set +a
cd "$root"
docker compose --project-name "$TRIAL_PROJECT" --env-file "$secret" -f deploy/trial/compose.yml ps
port="${TRIAL_HTTP_PORT:-18082}"
printf 'health '
curl -fsS "http://127.0.0.1:${port}/health" || printf 'unreachable\n'
printf '\n'
docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' \
    | awk 'NR==1 || /tradingagents-smc-tpo-trial/'
