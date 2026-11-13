#!/usr/bin/env bash
set -euo pipefail

remote_host="${CRAFTS_SSH_HOST:?Set CRAFTS_SSH_HOST to your SSH alias}"
remote_app="${CRAFTS_REMOTE_APP:?Set CRAFTS_REMOTE_APP to the remote checkout}"
input_root="${CRAFTS_INPUT_DIR:?Set CRAFTS_INPUT_DIR to the remote input directory}"
mask_root="${CRAFTS_MASK_DIR:?Set CRAFTS_MASK_DIR to the remote output directory}"
remote_python="${CRAFTS_REMOTE_PYTHON:-python3}"
# Escape literal quotes for the remote shell command.
for remote_key in remote_app input_root mask_root remote_python; do
  remote_value=${!remote_key}
  printf -v "$remote_key" '%s' "${remote_value//\'/\'\\\'\'}"
done

target="${1:-Dec+2654_22_09:0002}"
case "$target" in
  *[!A-Za-z0-9+_.:-]*)
    echo "Invalid target: $target" >&2
    exit 2
    ;;
esac

open -a XQuartz
xquartz_display=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  xquartz_display="$(launchctl getenv DISPLAY)"
  if [[ -n "$xquartz_display" ]]; then
    break
  fi
  sleep 1
done
if [[ -z "$xquartz_display" ]]; then
  echo "XQuartz did not publish DISPLAY; open XQuartz and retry." >&2
  exit 1
fi
export DISPLAY="$xquartz_display"

exec ssh -Y "$remote_host" \
  "mkdir -p '$mask_root' && cd '$remote_app' && exec env MPLBACKEND=TkAgg PYTHONUNBUFFERED=1 '$remote_python' crafts_tod_visual_mask.py '$target' --input-dir '$input_root' --mask-dir '$mask_root' --require-all-auto-masks --block-records 64"
