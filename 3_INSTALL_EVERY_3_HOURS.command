#!/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/scripts/common.sh"
trap pause_on_exit EXIT
command_title "Install Every 3 Hours"
PY="$(find_python || true)"
if [ -z "$PY" ]; then
  python_not_found_help
  exit 1
fi
echo "Using Python: $PY"
echo
cd "$DIR"
"$PY" -m reddit_monitor.cli install-scheduler
