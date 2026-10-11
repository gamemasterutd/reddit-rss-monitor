#!/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/scripts/common.sh"
trap pause_on_exit EXIT
command_title "Remove Previous Builds"

# This cleanup intentionally does not require Python. It exists specifically so
# a damaged Python installation cannot prevent recovery from an earlier build.
PLIST="$HOME/Library/LaunchAgents/com.redditrssmonitor.plist"
RUNTIME="$HOME/Library/Application Support/RedditRSSMonitor"

if [ -f "$PLIST" ]; then
  if [ -x /bin/launchctl ]; then
    /bin/launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
    /bin/launchctl unload -w "$PLIST" >/dev/null 2>&1 || true
  fi
  rm -f "$PLIST"
  echo "Removed prior launchd scheduler."
else
  echo "No prior launchd scheduler found."
fi

if [ -d "$RUNTIME" ]; then
  rm -rf "$RUNTIME"
  echo "Removed prior local runtime state."
else
  echo "No prior runtime state found."
fi

echo "Generated Reddit-Signals reports and Python installations were not touched."
