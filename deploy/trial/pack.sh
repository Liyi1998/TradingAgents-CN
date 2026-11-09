#!/bin/sh
# Build the copy-out trial bundle. Does not touch a Mac or a Docker daemon.
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
commit=$(git -C "$root" rev-parse HEAD)
short=$(git -C "$root" rev-parse --short=12 HEAD)
if [ -f "$root/deploy/trial/IMAGE_TAG" ]; then
    image_tag=$(tr -d '[:space:]' < "$root/deploy/trial/IMAGE_TAG")
else
    image_tag=$commit
fi
stage=$(mktemp -d)
dest="$stage/tradingagents-smc-tpo-trial"
mkdir -p "$dest"
git -C "$root" archive HEAD | tar -x -C "$dest"
if [ ! -f "$root/frontend/dist/index.html" ]; then
    echo "frontend/dist 缺失，先在云端完成 npm run build" >&2
    exit 1
fi
mkdir -p "$dest/frontend/dist"
cp -a "$root/frontend/dist/." "$dest/frontend/dist/"
printf '%s\n' "$image_tag" > "$dest/deploy/trial/IMAGE_TAG"
printf '%s\n' "$commit" > "$dest/SOURCE_COMMIT"
find "$dest" -name .env -o -name '.env.*' -o -name trial.env | while read -r path; do
    rm -f "$path"
done
# Drop host node_modules if a dirty archive ever contains them.
rm -rf "$dest/frontend/node_modules" "$dest/.git"
out_dir=${1:-/opt/cursor/artifacts}
mkdir -p "$out_dir"
archive="$out_dir/tradingagents-smc-tpo-trial-${short}.tar.gz"
tar -C "$stage" -czf "$archive" tradingagents-smc-tpo-trial
sha=$(sha256sum "$archive" | awk '{print $1}')
printf '%s  %s\n' "$sha" "$(basename "$archive")" > "$out_dir/tradingagents-smc-tpo-trial-${short}.sha256"
cp "$dest/deploy/trial/CHECKLIST.md" "$out_dir/SMC_TPO_TRIAL_CHECKLIST.md"
printf 'archive %s\nsha256 %s\ncommit %s\n' "$archive" "$sha" "$commit"
rm -rf "$stage"
