#!/bin/sh
# Start only this project from images that are already loaded.
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

"$(dirname "$0")/preflight-runtime.sh"
if [ "${TRIAL_RUNTIME_APPROVED:-}" != "1" ]; then
    die "启动预检通过也不等于可以启动。审查资源数字后才设置 TRIAL_RUNTIME_APPROVED=1。这个开关不改变门槛，也不会去构建。" 12
fi

cd "$root"
docker compose \
    --project-name "$TRIAL_PROJECT" \
    --env-file "$secret" \
    -f deploy/trial/compose.yml \
    up -d --no-build --pull never
printf '已启动 %s。入口 http://127.0.0.1:%s\n' "$TRIAL_PROJECT" "${TRIAL_HTTP_PORT:-18082}"
printf '口令文件: %s\n' "$secret"
