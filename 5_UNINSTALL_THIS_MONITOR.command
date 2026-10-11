#!/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/scripts/common.sh"
trap pause_on_exit EXIT
command_title "Uninstall"
PY="$(find_python || true)"
if [ -z "$PY" ]; then
  python_not_found_help
  echo "The launchd plist can be removed manually from ~/Library/LaunchAgents/com.redditrssmonitor.plist."
  exit 1
fi
echo "Using Python: $PY"
echo
cd "$DIR"
"$PY" -m reddit_monitor.cli uninstall
