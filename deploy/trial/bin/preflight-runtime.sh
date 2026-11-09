#!/bin/sh
# Runtime check for an already loaded trial. Does not build, pull, load, or start.
# Build thresholds stay in preflight.sh and are not consulted here.
set -eu
. "$(dirname "$0")/common.sh"
. "$(dirname "$0")/images.sh"

root=$(trial_root)
assert_project
assert_not_production_tree "$root"
assert_compose_text "$root"
port="${TRIAL_HTTP_PORT:-$TRIAL_HTTP_PORT_DEFAULT}"
assert_port "$port"

if ! command -v docker >/dev/null 2>&1; then
    die "未找到 docker，拒绝启动" 13
fi

# Compose limits sum to 1792 MiB. Keep another 1024 MiB, one backend limit, unused.
START_MEM_AVAIL_MIB=2816
START_DISK_GIB=6
SWAP_MAX_MIB=4096

disk_kib=$(disk_free_kib)
disk_gib=$(awk -v k="$disk_kib" 'BEGIN {printf "%.1f", k/1024/1024}')
swap_mib=$(swap_used_mib)
swap_mib=${swap_mib:-0}
mem_total=$(docker_mem_total_mib || true)
mem_used=$(docker_used_mib || true)
mem_total=${mem_total:-0}
mem_used=${mem_used:-0}
mem_avail=$((mem_total - mem_used))

printf '试运行启动预检\n'
printf '  这条路径只检查已经加载的镜像，不构建，不拉取。\n'
printf '  构建门槛没有改：swap 1536 MiB、Docker 剩余 4096 MiB、磁盘 12 GiB 仍由 bin/preflight.sh 执行。\n'
printf '  磁盘可用: %s GiB，启动门槛 %s GiB\n' "$disk_gib" "$START_DISK_GIB"
printf '  swap 已用: %s MiB，启动门槛不超过 %s MiB\n' "$swap_mib" "$SWAP_MAX_MIB"
printf '  Docker 内存总量: %s MiB，容器已用约: %s MiB，估算剩余: %s MiB，启动门槛 %s MiB\n' \
    "$mem_total" "$mem_used" "$mem_avail" "$START_MEM_AVAIL_MIB"
printf '  计划入口: 127.0.0.1:%s\n' "$port"
printf '  容器上限合计: 1792 MiB / 1.85 CPU\n'

if [ "$mem_total" -eq 0 ]; then
    die "读不到 Docker 内存总量，拒绝启动" 10
fi

block=0
if [ "$disk_kib" -lt $((START_DISK_GIB * 1024 * 1024)) ]; then
    block=1
    printf '  阻塞: 可用磁盘低于 %s GiB。\n' "$START_DISK_GIB"
fi
if [ "$swap_mib" -gt "$SWAP_MAX_MIB" ]; then
    block=1
    printf '  阻塞: swap 已用超过 %s MiB。不要为了启动停止生产容器。\n' "$SWAP_MAX_MIB"
fi
if [ "$mem_avail" -lt "$START_MEM_AVAIL_MIB" ]; then
    block=1
    printf '  阻塞: Docker 估算剩余低于 %s MiB。\n' "$START_MEM_AVAIL_MIB"
fi
if [ "$block" -ne 0 ]; then
    printf '  启动: 拒绝。\n'
    exit 10
fi

verify_prebuilt_images "$(manifest_file "$root")"
printf '  启动: 资源和镜像摘要通过。bin/start.sh 在审查授权前不会启动。\n'
exit 0
