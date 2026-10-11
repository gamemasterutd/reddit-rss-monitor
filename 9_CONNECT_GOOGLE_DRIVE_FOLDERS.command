#!/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/scripts/common.sh"
trap pause_on_exit EXIT
command_title "Connect one monthly Google Drive source folder"
RCLONE="$(command -v rclone || true)"
if [ -z "$RCLONE" ]; then
  echo "rclone is not installed. Install it from rclone.org, then run rclone config once."
  exit 1
fi
PY="$(find_python || true)"
if [ -z "$PY" ]; then
  python_not_found_help
  exit 1
fi
DEFAULT_CONFIG="$HOME/.config/rclone/rclone.conf"
if [ -f "$DIR/.runtime/work-drive-rclone.conf" ]; then
  DEFAULT_CONFIG="$DIR/.runtime/work-drive-rclone.conf"
fi
read -r -p "rclone Google Drive remote name: " REMOTE
read -r -p "rclone config file [$DEFAULT_CONFIG]: " AUTH_FILE
AUTH_FILE="${AUTH_FILE:-$DEFAULT_CONFIG}"
if [ ! -f "$AUTH_FILE" ]; then
  echo "The rclone config file was not found: $AUTH_FILE"
  exit 1
fi
read -r -p "Source Drive folder ID (the part after /folders/): " FOLDER_ID
if [[ ! "$REMOTE" =~ ^[A-Za-z0-9_-]+$ || ! "$FOLDER_ID" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "Remote name or folder ID is missing or invalid."
  exit 1
fi
cd "$DIR"
"$PY" -m reddit_monitor.cli connect-rclone-monthly \
  --remote "$REMOTE" --binary "$RCLONE" --rclone-config "$AUTH_FILE" \
  --folder-id "$FOLDER_ID"
