#!/usr/bin/env bash
set -euo pipefail

remote_host="${CRAFTS_SSH_HOST:?Set CRAFTS_SSH_HOST to your SSH alias}"
remote_app="${CRAFTS_REMOTE_APP:?Set CRAFTS_REMOTE_APP to the remote checkout}"
input_dir="${CRAFTS_INPUT_DIR:?Set CRAFTS_INPUT_DIR to the remote input directory}"
mask_dir="${CRAFTS_MASK_DIR:?Set CRAFTS_MASK_DIR to the remote output directory}"
remote_python="${CRAFTS_REMOTE_PYTHON:-python3}"
runtime_dir="${CRAFTS_RUNTIME_DIR:?Set CRAFTS_RUNTIME_DIR to a remote runtime directory}"
# Escape literal quotes for the remote shell command.
for remote_key in remote_app input_dir mask_dir remote_python runtime_dir; do
  remote_value=${!remote_key}
  printf -v "$remote_key" '%s' "${remote_value//\'/\'\\\'\'}"
done

scan="Dec+2654_22_09"
nfile="${1:-0002}"
port="${2:-8992}"

if [[ "$nfile" =~ ^[0-9]+$ ]]; then printf -v nfile "%04d" "$((10#$nfile))"; else echo "用法: ./run_dec2654_native_ui.sh [nfile] [本地端口]" >&2; exit 2; fi
if [[ ! "$port" =~ ^[0-9]+$ ]] || ((port < 1024 || port > 65535)); then echo "端口必须是 1024--65535 的整数。" >&2; exit 2; fi

target="${scan}:${nfile}"
base_url="http://127.0.0.1:${port}/"
url="${base_url}?v=native-web-v1"
remote_pidfile="${runtime_dir}/native_${port}.pid"

if curl -fsS --max-time 1 "$base_url" >/dev/null 2>&1; then echo "本地端口 ${port} 已被占用，请换端口。" >&2; exit 1; fi
if [[ "${CRAFTS_WEB_UI_DRY_RUN:-0}" == "1" ]]; then echo "参数检查通过：${target} -> ${url}"; exit 0; fi
if ! ssh "$remote_host" "'$remote_python' -c \"import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', $port)); s.close()\""; then echo "服务器端口 ${port} 已被占用，请换端口。" >&2; exit 1; fi

echo "正在启动原生浏览器 UI：${target}"
echo "网页会立即打开，并在右侧实时显示 19-beam 载入进度。结束时在本终端按 Ctrl-C。"
ssh -tt -o ExitOnForwardFailure=yes -o LogLevel=QUIET -o ServerAliveInterval=30 -o ServerAliveCountMax=6 -L "${port}:127.0.0.1:${port}" "$remote_host" \
  "mkdir -p '$mask_dir' '$runtime_dir/tmp' && cd '$remote_app' && echo \$\$ > '$remote_pidfile' && exec env TMPDIR='$runtime_dir/tmp' PYTHONUNBUFFERED=1 '$remote_python' crafts_tod_native_web.py '$target' --input-dir '$input_dir' --mask-dir '$mask_dir' --require-all-auto-masks --block-records 64 --port '$port'" &
ssh_pid=$!

cleanup(){ trap - EXIT INT TERM HUP; if kill -0 "$ssh_pid" 2>/dev/null; then kill -HUP "$ssh_pid" 2>/dev/null || true; wait "$ssh_pid" 2>/dev/null || true; fi; ssh -o BatchMode=yes -o ConnectTimeout=10 "$remote_host" "if test -f '$remote_pidfile'; then pid=\$(cat '$remote_pidfile'); cmd=\$(ps -p \"\$pid\" -o args= 2>/dev/null || true); case \"\$cmd\" in *crafts_tod_native_web.py*'--port $port'*) kill \"\$pid\" 2>/dev/null || true; rm -f '$remote_pidfile';; '') rm -f '$remote_pidfile';; *) echo '保留不匹配的远端进程:' \"\$cmd\" >&2;; esac; fi" >/dev/null || true; }
trap cleanup EXIT INT TERM HUP

ready=0
for ((attempt=1;attempt<=30;attempt++)); do if ! kill -0 "$ssh_pid" 2>/dev/null; then wait "$ssh_pid"; fi; if curl -fsS --max-time 1 "$base_url" >/dev/null 2>&1; then ready=1; break; fi; sleep 1; done
if ((ready==0)); then echo "30 秒内未检测到 UI，请查看服务器输出。" >&2; exit 1; fi
open "$url"
echo "原生 UI 已打开；载入期间页面可正常响应。"
wait "$ssh_pid"
