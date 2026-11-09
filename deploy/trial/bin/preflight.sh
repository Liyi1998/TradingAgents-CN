#!/bin/sh
# Resource and safety preflight. Prints measurements. Does not start or delete anything.
set -eu
. "$(dirname "$0")/common.sh"

root=$(trial_root)
assert_project
assert_not_production_tree "$root"
assert_compose_text "$root"

port="${TRIAL_HTTP_PORT:-$TRIAL_HTTP_PORT_DEFAULT}"
assert_port "$port"

disk_kib=$(disk_free_kib)
disk_gib=$(awk -v k="$disk_kib" 'BEGIN {printf "%.1f", k/1024/1024}')
swap_mib=$(swap_used_mib)
swap_mib=${swap_mib:-0}
mem_total=$(docker_mem_total_mib || printf '0')
mem_used=$(docker_used_mib || printf '0')
mem_avail=$((mem_total - mem_used))
arch=$(uname -m)
docker_arch=$(docker info --format '{{.Architecture}}' 2>/dev/null || printf 'unknown')

printf '试运行预检\n'
printf '  目录: %s\n' "$root"
printf '  项目: %s\n' "$TRIAL_PROJECT"
printf '  架构: 主机 %s，Docker %s（不固定平台，按本机架构构建）\n' "$arch" "$docker_arch"
printf '  磁盘可用: %s GiB\n' "$disk_gib"
printf '  swap 已用: %s MiB\n' "$swap_mib"
printf '  Docker 内存总量: %s MiB，容器已用约: %s MiB，估算剩余: %s MiB\n' "$mem_total" "$mem_used" "$mem_avail"
printf '  计划入口: 127.0.0.1:%s\n' "$port"
printf '  运行预算: 合计不超过 2.0 GiB / 2 CPU（mongo 512m，redis 128m，backend 1024m，frontend+nginx 128m）\n'

build_block=0
start_block=0
if [ "$disk_kib" -lt $((12 * 1024 * 1024)) ]; then
    build_block=1
    printf '  阻塞: 构建需要至少 12 GiB 可用磁盘，当前不够。不要清理生产镜像。\n'
fi
if [ "$disk_kib" -lt $((6 * 1024 * 1024)) ]; then
    start_block=1
    printf '  阻塞: 启动也需要至少 6 GiB 可用磁盘。\n'
fi
if [ "$swap_mib" -gt 1536 ]; then
    build_block=1
    printf '  阻塞: swap 已用超过 1536 MiB。当前机器已在换页，禁止再做镜像构建。不要为了腾出内存停止生产容器。\n'
fi
if [ "$mem_total" -lt 6144 ]; then
    build_block=1
    printf '  阻塞: Docker 内存总量低于 6 GiB，构建峰值不安全。\n'
fi
if [ "$mem_avail" -lt 4096 ]; then
    build_block=1
    printf '  阻塞: Docker 估算剩余内存低于 4096 MiB，构建峰值会挤占生产。\n'
fi
if [ "$mem_avail" -lt 2600 ]; then
    start_block=1
    printf '  阻塞: Docker 估算剩余内存低于 2600 MiB，即使镜像已存在也不启动试运行。\n'
fi

if [ "$build_block" -eq 0 ]; then
    printf '  构建: 允许。必须单线程构建，不要并行批分析或下载模型。\n'
else
    printf '  构建: 拒绝。\n'
fi
if [ "$start_block" -eq 0 ]; then
    printf '  启动: 仅当镜像已经存在时允许。\n'
else
    printf '  启动: 拒绝。\n'
fi

if [ "$build_block" -ne 0 ]; then
    exit 10
fi
exit 0
