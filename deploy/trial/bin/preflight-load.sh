#!/bin/sh
# Resource check before docker load. Does not load, build, pull, or delete anything.
# Build thresholds stay in preflight.sh and are not consulted here.
set -eu
. "$(dirname "$0")/common.sh"

root=$(trial_root)
assert_project
assert_not_production_tree "$root"
assert_compose_text "$root"

if ! command -v docker >/dev/null 2>&1; then
    die "未找到 docker，拒绝加载镜像" 13
fi

# Cold docker load of the pinned 1221839360-byte archive grew reclaimable page
# cache by 2375 MiB and dockerd RSS by about 9 MiB. The gate keeps that cache
# plus a 1024 MiB cushion, equal to the backend mem_limit.
LOAD_MEM_AVAIL_MIB=3400
# Archive plus image store is about 2387 MiB. Add 4 GiB and round up to 8 GiB.
LOAD_DISK_GIB=8
# One quarter of the stated 16 GiB Mac. This is not the 1536 MiB build swap gate.
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

printf '试运行加载预检\n'
printf '  这条路径只检查 docker load，不构建。\n'
printf '  构建门槛没有改：swap 1536 MiB、Docker 剩余 4096 MiB、磁盘 12 GiB 仍由 bin/preflight.sh 执行。\n'
printf '  磁盘可用: %s GiB，加载门槛 %s GiB\n' "$disk_gib" "$LOAD_DISK_GIB"
printf '  swap 已用: %s MiB，加载门槛不超过 %s MiB\n' "$swap_mib" "$SWAP_MAX_MIB"
printf '  Docker 内存总量: %s MiB，容器已用约: %s MiB，估算剩余: %s MiB，加载门槛 %s MiB\n' \
    "$mem_total" "$mem_used" "$mem_avail" "$LOAD_MEM_AVAIL_MIB"

if [ "$mem_total" -eq 0 ]; then
    die "读不到 Docker 内存总量，拒绝加载" 10
fi

block=0
if [ "$disk_kib" -lt $((LOAD_DISK_GIB * 1024 * 1024)) ]; then
    block=1
    printf '  阻塞: 可用磁盘低于 %s GiB。不要清理生产镜像。\n' "$LOAD_DISK_GIB"
fi
if [ "$swap_mib" -gt "$SWAP_MAX_MIB" ]; then
    block=1
    printf '  阻塞: swap 已用超过 %s MiB。不要为了加载停止生产容器。\n' "$SWAP_MAX_MIB"
fi
if [ "$mem_avail" -lt "$LOAD_MEM_AVAIL_MIB" ]; then
    block=1
    printf '  阻塞: Docker 估算剩余低于 %s MiB，页缓存峰值会挤占生产。\n' "$LOAD_MEM_AVAIL_MIB"
fi
if [ "$block" -ne 0 ]; then
    printf '  加载: 拒绝。\n'
    exit 10
fi
printf '  加载: 资源数字通过。bin/load-prebuilt.sh 仍要校验压缩包，并且在审查授权前不会加载。\n'
exit 0
