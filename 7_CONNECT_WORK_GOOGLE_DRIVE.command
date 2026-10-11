#!/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/scripts/common.sh"
trap pause_on_exit EXIT
command_title "Connect a Google Drive synced folder"
PY="$(find_python || true)"
if [ -z "$PY" ]; then
  python_not_found_help
  exit 1
fi
echo "In Finder, open a folder in Google Drive for desktop signed into your Google account."
echo "In Finder, select the folder, press Option-Command-C to copy its pathname, then paste it here."
echo
read -r -p "Synced Google Drive folder: " DRIVE_FOLDER
DRIVE_FOLDER="${DRIVE_FOLDER%\"}"
DRIVE_FOLDER="${DRIVE_FOLDER#\"}"
cd "$DIR"
"$PY" -m reddit_monitor.cli connect-drive --folder "$DRIVE_FOLDER"
