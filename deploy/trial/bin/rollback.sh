#!/bin/sh
# Stop only tradingagents-smc-tpo-trial. Never prunes and never removes volumes
# unless --remove-trial-volumes is passed, and then only prefixed volumes.
set -eu
. "$(dirname "$0")/common.sh"
root=$(trial_root)
assert_project
secret=$(trial_secret_file)
[ -f "$secret" ] || secret=/dev/null
cd "$root"

remove=0
if [ "${1:-}" = "--remove-trial-volumes" ]; then
    remove=1
    printf '将删除且只删除名称以 %s_ 开头的卷。输入 delete-trial-volumes 确认: ' "$TRIAL_PROJECT"
    read -r answer
    [ "$answer" = "delete-trial-volumes" ] || die "未确认，未删除任何卷" 12
fi

docker compose --project-name "$TRIAL_PROJECT" -f deploy/trial/compose.yml stop
docker compose --project-name "$TRIAL_PROJECT" -f deploy/trial/compose.yml down --remove-orphans

if [ "$remove" -eq 1 ]; then
    docker volume ls --format '{{.Name}}' | while read -r name; do
        case "$name" in
            ${TRIAL_PROJECT}_*)
                docker volume rm "$name"
                ;;
            *)
                printf '保留卷: %s\n' "$name"
                ;;
        esac
    done
fi
printf '已停止 %s。生产 project、18080/18081 和原有卷未操作。\n' "$TRIAL_PROJECT"
