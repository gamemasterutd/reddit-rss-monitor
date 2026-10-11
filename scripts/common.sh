#!/bin/bash
set -u

# Emit candidate Python interpreters in preference order. The optional
# RRM_PYTHON_CANDIDATES override is used by release tests and advanced users.
python_candidates() {
  if [ -n "${RRM_PYTHON_CANDIDATES:-}" ]; then
    local old_ifs="$IFS"
    IFS=':'
    for p in $RRM_PYTHON_CANDIDATES; do
      [ -n "$p" ] && printf '%s\n' "$p"
    done
    IFS="$old_ifs"
    return 0
  fi

  local versions="3.15 3.14 3.13 3.12 3.11 3.10 3.9"
  local v p

  # Homebrew on Apple Silicon and Intel Macs.
  for v in $versions; do
    printf '%s\n' "/opt/homebrew/bin/python$v"
    printf '%s\n' "/opt/homebrew/opt/python@$v/bin/python$v"
    printf '%s\n' "/usr/local/bin/python$v"
    printf '%s\n' "/usr/local/opt/python@$v/bin/python$v"
  done
  printf '%s\n' "/opt/homebrew/bin/python3"
  printf '%s\n' "/usr/local/bin/python3"

  # python.org framework installs.
  for v in $versions; do
    printf '%s\n' "/Library/Frameworks/Python.framework/Versions/$v/bin/python$v"
    printf '%s\n' "/Library/Frameworks/Python.framework/Versions/$v/bin/python3"
  done

  # Common user-managed locations, then Apple's/Xcode's system Python.
  printf '%s\n' "$HOME/.pyenv/shims/python3"
  printf '%s\n' "$HOME/.local/bin/python3"
  printf '%s\n' "/usr/bin/python3"

  # Finally, anything already on PATH.
  for v in $versions; do
    p="$(command -v "python$v" 2>/dev/null || true)"
    [ -n "$p" ] && printf '%s\n' "$p"
  done
  p="$(command -v python3 2>/dev/null || true)"
  [ -n "$p" ] && printf '%s\n' "$p"
}

python_version_ok() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,9) else 1)' >/dev/null 2>&1
}

python_stdlib_ok() {
  # This is intentionally broader than a version check. It catches damaged or
  # incompatible Python installations (including pyexpat/libexpat linkage
  # failures) before the monitor imports any of its own modules.
  "$1" -c '
import sys
import argparse
import configparser
import contextlib
import csv
import dataclasses
import datetime
import email.utils
import fcntl
import html
import html.parser
import http.client
import importlib
import json
import os
import pathlib
import plistlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import typing
import unicodedata
import unittest
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import xml.parsers.expat
import zipfile
assert sys.version_info >= (3, 9)
ET.fromstring("<root><item>ok</item></root>")
conn = sqlite3.connect(":memory:")
assert conn.execute("select 1").fetchone()[0] == 1
conn.close()
' >/dev/null 2>&1
}

find_python() {
  local p seen="\n"
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    # Deduplicate literal paths without requiring realpath (not portable to all Macs).
    case "$seen" in
      *"\n$p\n"*) continue ;;
    esac
    seen="${seen}${p}\n"
    [ -x "$p" ] || continue
    if ! python_version_ok "$p"; then
      continue
    fi
    if python_stdlib_ok "$p"; then
      printf '%s\n' "$p"
      return 0
    fi
    printf 'Skipping unusable Python: %s (required standard-library self-test failed).\n' "$p" >&2
  done < <(python_candidates)
  return 1
}

python_not_found_help() {
  cat <<'EOF_HELP'
No compatible Python 3.9+ installation passed the required standard-library self-test.

The monitor requires a healthy Python with XML/Expat, SQLite, HTTP, ZIP and plist support.
No Python packages are required or installed by this project.

If you saw an error mentioning pyexpat, libexpat, or
XML_SetAllocTrackerActivationThreshold, that is an interpreter/OS compatibility
problem rather than a Reddit RSS Monitor dependency problem. This launcher will
automatically use another compatible Python installation if one is available.
EOF_HELP
}

command_title() {
  printf '\n============================================================\n'
  printf 'Reddit RSS Monitor - %s\n' "$1"
  printf '============================================================\n\n'
}

pause_on_exit() {
  local code="$?"
  trap - EXIT
  printf '\n'
  if [ "$code" -eq 0 ]; then
    printf 'Command finished successfully.\n'
  else
    printf 'Command stopped with exit status %s.\n' "$code"
    printf 'Read the message above for the exact error.\n'
  fi
  if [ -t 0 ]; then
    printf '\nPress Return to close this window...'
    IFS= read -r _ || true
  fi
  exit "$code"
}
