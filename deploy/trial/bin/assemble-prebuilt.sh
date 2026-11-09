#!/bin/sh
# Join the split image archive and check its pinned sha256. Does not load or start.
set -eu
. "$(dirname "$0")/common.sh"
. "$(dirname "$0")/images.sh"

root=$(trial_root)
assert_project
[ "$#" -eq 2 ] || die "用法: bin/assemble-prebuilt.sh <分卷目录> <输出tar>" 12
parts_dir=$1
output=$2
[ -d "$parts_dir" ] || die "找不到分卷目录: $parts_dir" 12

manifest=$(manifest_file "$root")
expected=$(manifest_field "$manifest" archive_sha256)
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
: > "$tmp"
found=0
for part in "$parts_dir"/image.tar.part-*; do
    [ -f "$part" ] || die "分卷目录里没有 image.tar.part-*" 12
    found=$((found + 1))
    cat "$part" >> "$tmp"
done
[ "$found" -ge 2 ] || die "分卷数量不对" 12
actual=$(file_sha256 "$tmp")
[ "$actual" = "$expected" ] || die "拼合后的镜像包摘要不一致。期望 $expected，实际 $actual。" 12
mv "$tmp" "$output"
trap - EXIT
printf '已拼合 %s 个分卷。摘要一致。还没有把镜像载入 Docker。\n' "$found"
printf '审查授权之后才运行: TRIAL_RUNTIME_APPROVED=1 bin/load-prebuilt.sh %s\n' "$output"
