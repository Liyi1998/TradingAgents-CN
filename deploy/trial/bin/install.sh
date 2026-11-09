#!/bin/sh
# Create the 0600 secret file. Does not build, start, or print secrets.
set -eu
. "$(dirname "$0")/common.sh"

root=$(trial_root)
assert_project
assert_not_production_tree "$root"
if [ -z "${TRIAL_IMAGE_TAG:-}" ] && [ -f "$root/deploy/trial/IMAGE_TAG" ]; then
    TRIAL_IMAGE_TAG=$(tr -d '[:space:]' < "$root/deploy/trial/IMAGE_TAG")
    export TRIAL_IMAGE_TAG
fi
secret=$(trial_secret_file)
mkdir -p "$(dirname "$secret")"
if [ -f "$secret" ]; then
    chmod 600 "$secret"
    printf '口令文件已存在，未改写: %s\n' "$secret"
else
    umask 077
    mongo_pw=$(od -An -N18 -tx1 /dev/urandom | tr -d ' \n')
    redis_pw=$(od -An -N18 -tx1 /dev/urandom | tr -d ' \n')
    jwt=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
    csrf=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
    admin_pw=$(od -An -N18 -tx1 /dev/urandom | tr -d ' \n')
    cat > "$secret" <<EOF
TRIAL_HTTP_PORT=${TRIAL_HTTP_PORT:-18082}
TRIAL_IMAGE_TAG=${TRIAL_IMAGE_TAG:-unknown}
MONGO_ROOT_USERNAME=trial
MONGODB_PASSWORD=${mongo_pw}
REDIS_PASSWORD=${redis_pw}
JWT_SECRET=${jwt}
CSRF_SECRET=${csrf}
INITIAL_ADMIN_USERNAME=trial-operator
INITIAL_ADMIN_EMAIL=trial-operator@example.com
INITIAL_ADMIN_PASSWORD=${admin_pw}
STRUCTURE_AUXILIARY_SYNTHETIC_DIR=
EOF
    chmod 600 "$secret"
    printf '已生成本机口令文件（权限 600），内容不在此显示: %s\n' "$secret"
fi
printf '下一步: bin/preflight.sh\n'
printf '预检拒绝构建时不要加内存、不要停止生产容器。预构建镜像走 bin/preflight-load.sh，不替代构建门槛。\n'
