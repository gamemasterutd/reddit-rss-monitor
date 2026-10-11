#!/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/scripts/common.sh"
trap pause_on_exit EXIT
command_title "Setup"
PY="$(find_python || true)"
if [ -z "$PY" ]; then
  echo "SETUP FAILED: no compatible Python interpreter was found."
  python_not_found_help
  echo "No packages were installed and no system changes were made."
  exit 1
fi
echo "Using Python: $PY"
echo
cd "$DIR"
"$PY" -m reddit_monitor.setup_wizard
