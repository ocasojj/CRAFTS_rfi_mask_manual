#!/usr/bin/env bash
set -euo pipefail
launcher_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${CRAFTS_PYTHON:-python3}" -B "$launcher_dir/rfi_calibration_launcher/launch.py" "$@"
