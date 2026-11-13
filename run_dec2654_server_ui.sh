#!/usr/bin/env bash
set -euo pipefail

remote_host="${CRAFTS_SSH_HOST:?Set CRAFTS_SSH_HOST to your SSH alias}"
remote_app="${CRAFTS_REMOTE_APP:?Set CRAFTS_REMOTE_APP to the remote checkout}"
input_dir="${CRAFTS_INPUT_DIR:?Set CRAFTS_INPUT_DIR to the remote input directory}"
mask_dir="${CRAFTS_MASK_DIR:?Set CRAFTS_MASK_DIR to the remote output directory}"
remote_python="${CRAFTS_REMOTE_PYTHON:-python3}"
# Escape literal quotes for the remote shell command.
for remote_key in remote_app input_dir mask_dir remote_python; do
  remote_value=${!remote_key}
  printf -v "$remote_key" '%s' "${remote_value//\'/\'\\\'\'}"
done

scan="Dec+2654_22_09"
nfile="${1:-0002}"
if [[ "$nfile" =~ ^[0-9]+$ ]]; then
  printf -v nfile "%04d" "$((10#$nfile))"
else
  echo "用法: bash run_dec2654_server_ui.sh [nfile编号，例如 2 或 0002]" >&2
  exit 2
fi

target="${scan}:${nfile}"

echo "服务器数据目录: $input_dir"
echo "正在列出可用的 _T nfile 组..."
ssh "$remote_host" \
  "cd '$remote_app' && '$remote_python' crafts_tod_visual_mask.py --input-dir '$input_dir' --mask-dir '$mask_dir' --list"

echo
echo "即将打开: $target"
echo "首次读取 19 beams 和自动 RFI mask 预计需要约 2–3 分钟。"

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
  echo "XQuartz 没有提供 DISPLAY；请打开 XQuartz 后重新运行。" >&2
  exit 1
fi
export DISPLAY="$xquartz_display"

exec ssh -Y "$remote_host" \
  "mkdir -p '$mask_dir' && cd '$remote_app' && exec env MPLBACKEND=TkAgg PYTHONUNBUFFERED=1 '$remote_python' crafts_tod_visual_mask.py '$target' --input-dir '$input_dir' --mask-dir '$mask_dir' --require-all-auto-masks --block-records 64"
