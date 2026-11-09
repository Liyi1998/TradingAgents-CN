#!/bin/sh
# Leave synthetic fixtures and use the normal single-stock minute source.
# Does not delete the labeled synthetic tracks and does not enable auxiliary.
set -eu
. "$(dirname "$0")/common.sh"
root=$(trial_root)
secret=$(trial_secret_file)
[ -f "$secret" ] || die "缺少口令文件" 12
# Keep the secret file unchanged. The base compose sets the fixture dir empty.
cd "$root"
docker compose \
    --project-name "$TRIAL_PROJECT" \
    --env-file "$secret" \
    -f deploy/trial/compose.yml \
    up -d --no-build --force-recreate backend
printf '已切回真实单股分钟源。900001–900006 仍只是合成测试档，不要当成持仓。\n'
printf '对真实股票新建研究档后，才在该档上打开结构辅助。\n'
