"""Stage or upload the single ChatGPT Work handoff file.

Google Drive for desktop or rclone handles Google sign-in. This module never
stores credentials or claims that a staged local copy has already synced.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path


def _digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            result.update(block)
    return result.hexdigest()


def sync_review_files(reports, folder):
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise FileNotFoundError("Configured Google Drive folder is unavailable: %s" % folder)
    keys = ("latest_batch_text",)
    copied = []
    skipped = []
    for key in keys:
        source = Path(reports[key])
        if not source.is_file():
            raise FileNotFoundError("Review export is missing: %s" % source)
        dest = folder / source.name
        if dest.is_file() and source.stat().st_size == dest.stat().st_size and _digest(source) == _digest(dest):
            skipped.append(dest.name)
            continue
        tmp = folder / ("." + source.name + ".uploading")
        try:
            shutil.copyfile(source, tmp)
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                tmp.unlink()
        copied.append(dest.name)
    return {"folder": str(folder), "copied": copied, "unchanged": skipped}


def upload_review_file_rclone(reports, remote, executable):
    """Upload one stable filename; OAuth stays in rclone's own configuration."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", remote or ""):
        raise ValueError("rclone remote name must contain only letters, numbers, _ or -")
    binary = Path(executable).expanduser()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise FileNotFoundError("rclone executable is unavailable: %s" % binary)
    source = Path(reports["latest_batch_text"])
    if not source.is_file():
        raise FileNotFoundError("Work batch is missing: %s" % source)
    dest = "%s:Reddit RSS Monitor/%s" % (remote, source.name)
    result = subprocess.run([str(binary), "copyto", str(source), dest],
                            capture_output=True, text=True, timeout=180, check=False)
    if result.returncode:
        raise RuntimeError("rclone upload failed: %s" % (result.stderr.strip() or "exit %s" % result.returncode))
    return {"destination": dest, "file": source.name}


def upload_split_files_rclone(reports, remote, executable, config_path, folder_ids):
    """Update one stable file in each of the two exact work Drive folders."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", remote or ""):
        raise ValueError("rclone remote name must contain only letters, numbers, _ or -")
    binary = Path(executable).expanduser()
    config_path = Path(config_path).expanduser()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise FileNotFoundError("rclone executable is unavailable: %s" % binary)
    if not config_path.is_file():
        raise FileNotFoundError("Work Drive authorization is unavailable: %s" % config_path)
    if config_path.stat().st_mode & 0o077:
        raise PermissionError("Work Drive authorization file must be private (mode 0600)")
    uploaded = []
    for key in ("latest_reply_text", "latest_content_text"):
        folder_id = folder_ids[key]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", folder_id):
            raise ValueError("Invalid Drive folder ID for %s" % key)
        source = Path(reports[key])
        if not source.is_file():
            raise FileNotFoundError("Work handoff file is missing: %s" % source)
        destination = "%s:%s" % (remote, source.name)
        command = [str(binary), "--config", str(config_path), "copyto", str(source), destination,
                   "--drive-root-folder-id", folder_id, "--retries", "1", "--low-level-retries", "2"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
        if result.returncode:
            raise RuntimeError("Work Drive upload failed for %s: %s" %
                               (source.name, result.stderr.strip() or "exit %s" % result.returncode))
        uploaded.append({"file": source.name, "folder_id": folder_id,
                         "folder_url": "https://drive.google.com/drive/folders/%s" % folder_id})
    return uploaded


def upload_monthly_workbook_rclone(reports, remote, executable, config_path, folder_id):
    """Update one month-named source workbook in the chosen Drive folder."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", remote or ""):
        raise ValueError("Invalid rclone remote name")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", folder_id or ""):
        raise ValueError("Invalid Drive folder ID")
    binary = Path(executable).expanduser()
    config_path = Path(config_path).expanduser()
    source = Path(reports["review_workbook"])
    if not source.is_file() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise FileNotFoundError("Monthly workbook or rclone executable unavailable")
    if not config_path.is_file() or config_path.stat().st_mode & 0o077:
        raise PermissionError("Private Work Drive authorization unavailable")
    destination = "%s:%s" % (remote, source.name)
    command = [str(binary), "--config", str(config_path), "copyto", str(source), destination,
               "--drive-root-folder-id", folder_id, "--retries", "1", "--low-level-retries", "2"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    if result.returncode:
        raise RuntimeError("Monthly Drive upload failed: %s" % (result.stderr.strip() or result.returncode))
    return {"file": source.name, "folder_id": folder_id,
            "folder_url": "https://drive.google.com/drive/folders/%s" % folder_id}

def latest_review_paths(output_dir):
    output_dir = Path(output_dir).expanduser()
    if (output_dir / ".data" / "Reddit-Work-Batch-Latest.json").is_file():
        output_dir = output_dir / ".data"
    latest_json = output_dir / "Reddit-Work-Batch-Latest.json"
    batch = json.loads(latest_json.read_text(encoding="utf-8"))
    date = batch["report_date_local"]
    return {
        "review_md": str(output_dir / ("Reddit-Review-%s.md" % date)),
        "batch_text": str(output_dir / ("Reddit-Work-Batch-%s.txt" % date)),
        "batch_json": str(output_dir / ("Reddit-Work-Batch-%s.json" % date)),
        "snapshots_csv": str(output_dir / ("Reddit-Feed-Snapshots-%s.csv" % date)),
        "latest_review_md": str(output_dir / "Reddit-Review-Latest.md"),
        "latest_batch_text": str(output_dir / "Reddit-Work-Batch-Latest.txt"),
        "latest_reply_text": str(output_dir / "Reddit-Replies-Latest.txt"),
        "latest_content_text": str(output_dir / "Reddit-Content-Latest.txt"),
        "latest_batch_json": str(latest_json),
        "latest_snapshots_csv": str(output_dir / "Reddit-Feed-Snapshots-Latest.csv"),
    }
