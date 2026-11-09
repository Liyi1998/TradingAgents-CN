#!/bin/sh
# Load one pinned docker save archive. Never builds, pulls, prunes, or deletes images.
set -eu
. "$(dirname "$0")/common.sh"
. "$(dirname "$0")/images.sh"

root=$(trial_root)
assert_project
[ "$#" -eq 1 ] || die "用法: bin/load-prebuilt.sh <镜像tar>" 12
archive=$1
[ -f "$archive" ] || die "找不到镜像包: $archive" 12

"$(dirname "$0")/preflight-load.sh"
if [ "${TRIAL_RUNTIME_APPROVED:-}" != "1" ]; then
    die "加载预检通过也不等于可以加载。审查资源数字后才设置 TRIAL_RUNTIME_APPROVED=1。这个开关不改变门槛。" 12
fi

manifest=$(manifest_file "$root")
expected=$(manifest_field "$manifest" archive_sha256)
actual=$(file_sha256 "$archive")
[ "$actual" = "$expected" ] || die "镜像包摘要不一致。期望 $expected，实际 $actual。没有加载。" 12

printf '摘要一致，开始 docker load。不拉取，不构建。\n'
docker load -i "$archive"
verify_prebuilt_images "$manifest"
printf '五个 linux/arm64 镜像已加载，层摘要与清单一致。\n'
printf '下一步仍要单独跑 bin/preflight-runtime.sh。通过后才考虑 bin/start.sh。\n'
