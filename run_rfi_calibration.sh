#!/usr/bin/env bash
set -euo pipefail
launcher_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$HOME/miniforge3/envs/astro/bin/python" -B "$launcher_dir/rfi_calibration_launcher/launch.py" "$@"
