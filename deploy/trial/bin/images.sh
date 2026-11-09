#!/bin/sh
# Read the pinned linux/arm64 image manifest. Sourced by the load and runtime scripts.
manifest_file() {
    root=$1
    printf '%s\n' "$root/deploy/trial/prebuilt-images.txt"
}

manifest_field() {
    file=$1
    key=$2
    awk -v k="$key" '$1 == k { print $2; exit }' "$file"
}

file_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{ print $1 }'
        return
    fi
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{ print $1 }'
        return
    fi
    die "没有 sha256sum 或 shasum，不能校验镜像包" 12
}

verify_prebuilt_images() {
    file=$1
    [ -f "$file" ] || die "缺少预构建清单 $file" 12
    found=0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            image\ *)
                found=$((found + 1))
                tag=$(printf '%s\n' "$line" | awk '{ print $2 }')
                arch=$(printf '%s\n' "$line" | awk '{ print $3 }')
                expected=$(printf '%s\n' "$line" | awk '{ for (i = 5; i <= NF; i++) printf "%s%s", $i, (i == NF ? "" : " ") }')
                if ! docker image inspect "$tag" >/dev/null 2>&1; then
                    die "镜像不存在: $tag。这里不构建，也不拉取替代镜像。" 14
                fi
                actual_arch=$(docker image inspect --format '{{.Architecture}}' "$tag")
                [ "$actual_arch" = "$arch" ] || die "镜像 $tag 架构是 $actual_arch，清单要求 $arch" 14
                actual=$(docker image inspect --format '{{range .RootFS.Layers}}{{.}} {{end}}' "$tag")
                expected_norm=$(printf '%s\n' "$expected" | awk '{$1=$1; print}')
                actual_norm=$(printf '%s\n' "$actual" | awk '{$1=$1; print}')
                [ "$actual_norm" = "$expected_norm" ] || die "镜像 $tag 的层摘要与固定清单不一致" 14
                ;;
        esac
    done < "$file"
    [ "$found" -eq 5 ] || die "预构建清单应有 5 个镜像，读到 $found" 12
}
