"""Reject common private files and obvious tokens without printing their contents."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATTERNS = [
    re.compile(r"(?:AIza|AQ\.)[A-Za-z0-9_-]{25,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-(?:or-v1-)?[A-Za-z0-9_-]{25,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r'"refresh_token"\s*:\s*"[^"\s]{12,}"'),
    re.compile(r"https://(?:docs|drive)\.google\.com/(?:spreadsheets/d|file/d|drive/folders)/[A-Za-z0-9_-]{20,}"),
]
PRIVATE_DIRS = {".runtime", "output"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv"}
PRIVATE_SUFFIXES = {".sqlite3", ".db", ".xlsx", ".csv", ".jsonl", ".log", ".conf", ".pem", ".key"}
violations = []
scanned = 0
for path in ROOT.rglob("*"):
    relative = path.relative_to(ROOT)
    if not path.is_file() or any(part in SKIP_DIRS for part in relative.parts):
        continue
    if path.is_symlink():
        violations.append((str(relative), "symbolic link needs manual review"))
        continue
    if (any(part in PRIVATE_DIRS for part in relative.parts)
            or path.suffix in PRIVATE_SUFFIXES
            or path.name in {"config.json", ".env"}
            or path.name.startswith((".env.", "WORK_", "HANDOFF"))):
        violations.append((str(relative), "private/generated file"))
        continue
    text = path.read_text(encoding="utf-8")
    scanned += 1
    if any(pattern.search(text) for pattern in TOKEN_PATTERNS):
        violations.append((str(relative), "possible token or account-specific Drive link"))
if violations:
    for path, reason in violations:
        print("BLOCKED:", path, reason)
    raise SystemExit(1)
print("Public-file check passed for %d text files; manual diff review is still required." % scanned)
