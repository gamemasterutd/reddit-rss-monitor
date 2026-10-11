import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import DEFAULT_SUBREDDITS, ConfigError, default_config, default_config_path, save_config, validate_config

REQUIRED_STDLIB = [
    "argparse", "configparser", "contextlib", "csv", "dataclasses", "datetime", "email.utils",
    "fcntl", "html", "html.parser", "http.client", "importlib", "json", "os", "pathlib", "plistlib",
    "re", "shutil", "sqlite3", "subprocess", "tempfile", "time", "typing", "unicodedata", "unittest",
    "urllib.error", "urllib.parse", "urllib.request", "xml.etree.ElementTree", "xml.parsers.expat", "zipfile"
]


def _prompt(label, default=None):
    suffix = " [%s]" % default if default else ""
    value = input(label + suffix + ": ").strip()
    return value or (default or "")


def _check_stdlib():
    missing = []
    for name in REQUIRED_STDLIB:
        try:
            importlib.import_module(name)
        except Exception:
            missing.append(name)
    if missing:
        raise RuntimeError("Missing required Python standard-library modules: %s" % ", ".join(missing))


def main():
    if sys.version_info < (3, 9):
        print("SETUP FAILED: Python 3.9+ is required.")
        return 1
    root = Path(__file__).resolve().parent.parent
    _check_stdlib()
    print("Reddit RSS Monitor setup")
    print("Python: %s (%s)" % (sys.executable, sys.version.split()[0]))
    email = _prompt("Contact email for the Reddit User-Agent")
    sub_default = ",".join(DEFAULT_SUBREDDITS)
    sub_raw = _prompt("Subreddits (comma-separated, r/ optional)", sub_default)
    subreddits = [x.strip() for x in sub_raw.split(",") if x.strip()]
    out_default = str(root / "output")
    output_dir = _prompt("Output directory", out_default)
    runtime = default_config_path().parent
    cfg = default_config(email, output_dir, subreddits=subreddits, runtime_dir=runtime, filter_profile=root / "filters" / "finance.json")
    cfg["python_executable"] = sys.executable
    validate_config(cfg)

    with tempfile.TemporaryDirectory(prefix="reddit-rss-monitor-setup-") as td:
        staged = Path(td) / "config.json"
        staged.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print("Running full local self-test suite...")
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=str(root),
            check=False,
        )
        if result.returncode != 0:
            print("SETUP FAILED: self-tests did not pass. No configuration was installed.")
            return 1

    final = default_config_path()
    output_path = Path(output_dir).expanduser()
    runtime_existed = runtime.exists()
    output_existed = output_path.exists()
    db_path = runtime / "state.sqlite3"
    db_existed = db_path.exists()
    backup = None
    if final.exists():
        backup = final.with_suffix(".json.backup")
        backup.write_bytes(final.read_bytes())
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        output_path.mkdir(parents=True, exist_ok=True)
        save_config(cfg, final)
        # Initialize database without any Reddit request.
        from .db import connect, init_db
        conn = connect(db_path)
        init_db(conn)
        conn.close()
    except Exception:
        try:
            if final.exists():
                final.unlink()
            if backup and backup.exists():
                backup.replace(final)
            if not db_existed and db_path.exists():
                db_path.unlink()
            if not runtime_existed and runtime.exists():
                import shutil
                shutil.rmtree(runtime, ignore_errors=True)
            if not output_existed and output_path.exists():
                try:
                    output_path.rmdir()
                except OSError:
                    pass
        finally:
            print("SETUP FAILED: installation was rolled back.")
        raise
    if backup and backup.exists():
        backup.unlink()
    print("SETUP COMPLETE")
    print("Config: %s" % final)
    print("Reports: %s" % Path(output_dir).expanduser())
    print("Next: double-click 2_RUN_NOW.command for a manual collection test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
