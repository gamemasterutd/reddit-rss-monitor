import argparse
import fcntl
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .collector import Collector
from .config import (
    CONFIG_VERSION, ConfigError, default_config_path, default_filter_path,
    load_config, save_config, validate_config,
)
from . import db
from .db import connect, get_governor, init_db, last_run_info
from .logging_utils import append_log, tail_lines
from .process_lock import AlreadyRunning, ProcessLock
from .drive_sync import latest_review_paths, sync_review_files, upload_review_file_rclone, upload_split_files_rclone, upload_monthly_workbook_rclone
from .google_drive_direct import DriveUploader, connect_drive
from .ai_pipeline import run_ai
from .reports import generate_reports

LABEL = "com.redditrssmonitor"
PLIST_NAME = LABEL + ".plist"


def runtime_paths(cfg):
    runtime = Path(cfg["runtime_dir"]).expanduser()
    return {
        "runtime": runtime,
        "db": runtime / "state.sqlite3",
        "lock": runtime / "run.lock",
        "log": runtime / "monitor.log",
        "launch_out": runtime / "launchd.out.log",
        "launch_err": runtime / "launchd.err.log",
    }


def _load(path):
    return load_config(path)


def command_run(args):
    cfg = _load(args.config)
    paths = runtime_paths(cfg)
    paths["runtime"].mkdir(parents=True, exist_ok=True)
    conn = connect(paths["db"])
    init_db(conn)

    def progress(message):
        print(message, flush=True)
        append_log(paths["log"], message)

    if int(cfg.get("minimum_request_delay_seconds", 120)) < int(cfg.get("warn_delay_below_seconds", 60)):
        progress("WARNING: minimum request delay is configured aggressively at %ss." % cfg["minimum_request_delay_seconds"])
    try:
        with ProcessLock(paths["lock"]):
            progress("Run started")
            collector = Collector(cfg, conn, progress=progress)
            reports = (collector.run_comments_only(max_posts=args.max_comment_posts, force=args.force_comments)
                       if args.comments_only else collector.run(force_enrichment=args.enrich))
            progress("Run finished. Relevant=%d Review=%d Observed=%d Runs=%d" % (
                reports["counts"]["relevant"], reports["counts"]["review"],
                reports["counts"]["observed"], reports["counts"]["runs"],
            ))
            if reports.get("review_workbook"):
                progress("Open this one review file: %s" % reports["review_workbook"])
            else:
                progress("Workbook: %s" % reports["xlsx"])
                progress("Daily review: %s" % reports["review_md"])
                progress("Work batch: %s" % reports["batch_text"])
                progress("All-posts audit: %s" % reports["audit_csv"])
            if cfg.get("drive_sync_folder"):
                try:
                    synced = sync_review_files(reports, cfg["drive_sync_folder"])
                    progress("Staged %d Work batch file in work Google Drive folder: %s" %
                             (len(synced["copied"]), synced["folder"]))
                except Exception as exc:
                    progress("DRIVE SYNC WARNING: %s" % exc)
            if cfg.get("drive_rclone_remote"):
                try:
                    if cfg.get("drive_monthly_folder_id"):
                        uploaded = upload_monthly_workbook_rclone(
                            reports, cfg["drive_rclone_remote"], cfg["drive_rclone_executable"],
                            cfg["drive_rclone_config"], cfg["drive_monthly_folder_id"])
                        progress("Updated monthly source workbook in work Google Drive: %s" % uploaded["file"])
                    elif cfg.get("drive_rclone_split"):
                        uploaded = upload_split_files_rclone(
                            reports, cfg["drive_rclone_remote"], cfg["drive_rclone_executable"],
                            cfg["drive_rclone_config"], cfg["drive_rclone_folder_ids"])
                        progress("Updated %d work Google Drive handoff files" % len(uploaded))
                    else:
                        uploaded = upload_review_file_rclone(reports, cfg["drive_rclone_remote"],
                                                             cfg["drive_rclone_executable"])
                        progress("Uploaded Work batch to work Google Drive: %s" % uploaded["destination"])
                except Exception as exc:
                    progress("DRIVE UPLOAD WARNING: %s" % exc)
            if cfg.get("drive_direct_upload"):
                try:
                    uploaded = DriveUploader(paths["runtime"]).upload(reports["latest_batch_text"])
                    progress("Updated one Work Google Drive file: %s" % uploaded["url"])
                except Exception as exc:
                    progress("DRIVE UPLOAD WARNING: %s" % exc)
            if cfg.get("ai_enabled"):
                try:
                    ai_result = run_ai(cfg)
                    progress("AI review: %s" % json.dumps(ai_result, ensure_ascii=False))
                except Exception as exc:
                    progress("AI REVIEW WARNING: %s" % exc)
            return 0
    except AlreadyRunning as exc:
        progress(str(exc) + ". This process will exit without making any Reddit request.")
        return 0
    except KeyboardInterrupt:
        progress("Run interrupted by user; saved checkpoints remain conservative.")
        return 130
    except Exception as exc:
        progress("ERROR: %s" % exc)
        return 1
    finally:
        conn.close()


def _launchagents_dir():
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path():
    return _launchagents_dir() / PLIST_NAME


def _launchctl(*args):
    try:
        return subprocess.run(["/bin/launchctl"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    except OSError:
        return None


def install_scheduler(cfg, config_path):
    paths = runtime_paths(cfg)
    paths["runtime"].mkdir(parents=True, exist_ok=True)
    python_exe = cfg.get("python_executable") or sys.executable
    root = Path(__file__).resolve().parent.parent
    plist = {
        "Label": LABEL,
        "ProgramArguments": [python_exe, "-m", "reddit_monitor.cli", "run", "--config", str(Path(config_path).expanduser().resolve())],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PYTHONPATH": str(root)},
        "StartCalendarInterval": [{"Hour": hour, "Minute": 17} for hour in range(0, 24, 3)],
        "RunAtLoad": False,
        "StandardOutPath": str(paths["launch_out"]),
        "StandardErrorPath": str(paths["launch_err"]),
        "ProcessType": "Background",
        "LowPriorityIO": True,
    }
    dest = _plist_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    with tmp.open("wb") as f:
        plistlib.dump(plist, f, sort_keys=True)
    os.chmod(tmp, 0o644)
    _launchctl("bootout", "gui/%d" % os.getuid(), str(dest))
    tmp.replace(dest)
    result = _launchctl("bootstrap", "gui/%d" % os.getuid(), str(dest))
    if result is None or result.returncode != 0:
        fallback = _launchctl("load", "-w", str(dest))
        if fallback is None or fallback.returncode != 0:
            msg = (result.stderr if result else "launchctl unavailable") + " " + (fallback.stderr if fallback else "")
            raise RuntimeError("Could not install launchd job: %s" % msg.strip())
    return dest


def remove_scheduler():
    dest = _plist_path()
    if dest.exists():
        _launchctl("bootout", "gui/%d" % os.getuid(), str(dest))
        _launchctl("unload", "-w", str(dest))
        try:
            dest.unlink()
        except FileNotFoundError:
            pass
    return dest


def command_install_scheduler(args):
    cfg = _load(args.config)
    dest = install_scheduler(cfg, args.config or cfg["config_path"])
    print("Scheduler installed: %s" % dest)
    print("Runs at 00:17, 03:17, 06:17, 09:17, 12:17, 15:17, 18:17, 21:17 local Mac time.")
    print("NEW is checked every run. HOT/TOP feeds are checked only when their persisted refresh interval is due.")
    return 0


def command_connect_drive(args):
    cfg = _load(args.config)
    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        raise ConfigError("Select an existing folder inside Google Drive for desktop: %s" % folder)
    cfg["drive_sync_folder"] = str(folder)
    cfg["drive_rclone_remote"] = ""
    cfg["drive_rclone_executable"] = ""
    save_config(cfg, cfg["config_path"])
    print("Review files will be staged in: %s" % folder)
    try:
        reports = latest_review_paths(cfg["output_dir"])
        result = sync_review_files(reports, folder)
        print("Staged %d Work batch file now." % len(result["copied"]))
    except FileNotFoundError:
        print("No new review export exists yet. The next completed monitor run will stage it.")
    print("Verify that Reddit-Work-Batch-Latest.txt appears in drive.google.com under your work account.")
    return 0


def command_connect_rclone_drive(args):
    cfg = _load(args.config)
    binary = shutil.which(args.binary)
    if not binary:
        raise ConfigError("rclone is not installed or not on PATH: %s" % args.binary)
    listed = subprocess.run([binary, "listremotes"], capture_output=True, text=True, timeout=30, check=False)
    if listed.returncode:
        raise ConfigError("Could not read rclone remotes: %s" % listed.stderr.strip())
    if args.remote + ":" not in listed.stdout.splitlines():
        raise ConfigError("rclone remote is not configured: %s" % args.remote)
    reports = latest_review_paths(cfg["output_dir"])
    uploaded = upload_review_file_rclone(reports, args.remote, binary)
    cfg["drive_rclone_remote"] = args.remote
    cfg["drive_rclone_executable"] = binary
    cfg["drive_sync_folder"] = ""
    save_config(cfg, cfg["config_path"])
    print("Uploaded %s to %s" % (uploaded["file"], uploaded["destination"]))
    print("Future completed monitor runs will update this same Drive file.")
    return 0


def command_connect_rclone_split(args):
    cfg = _load(args.config)
    paths = runtime_paths(cfg)
    binary = shutil.which(args.binary)
    if not binary:
        raise ConfigError("rclone is unavailable: %s" % args.binary)
    config_path = Path(args.rclone_config).expanduser().resolve()
    listed = subprocess.run([binary, "--config", str(config_path), "listremotes"],
                            capture_output=True, text=True, timeout=30, check=False)
    if listed.returncode or args.remote + ":" not in listed.stdout.splitlines():
        raise ConfigError("Work Google Drive authorization is not configured in %s" % config_path)
    folder_ids = {"latest_reply_text": args.replies_folder_id,
                  "latest_content_text": args.content_folder_id}
    with ProcessLock(paths["lock"]):
        reports = latest_review_paths(cfg["output_dir"])
        uploaded = upload_split_files_rclone(reports, args.remote, binary, config_path, folder_ids)
        cfg["drive_rclone_remote"] = args.remote
        cfg["drive_rclone_executable"] = binary
        cfg["drive_rclone_config"] = str(config_path)
        cfg["drive_rclone_folder_ids"] = folder_ids
        cfg["drive_rclone_split"] = True
        cfg["drive_direct_upload"] = False
        cfg["drive_sync_folder"] = ""
        save_config(cfg, cfg["config_path"])
    for item in uploaded:
        print("Updated %s in %s" % (item["file"], item["folder_url"]))
    print("Future completed monitor runs will update these two files.")
    return 0


def command_connect_rclone_monthly(args):
    cfg = _load(args.config)
    paths = runtime_paths(cfg)
    binary = shutil.which(args.binary)
    if not binary:
        raise ConfigError("rclone is unavailable: %s" % args.binary)
    config_path = Path(args.rclone_config).expanduser().resolve()
    listed = subprocess.run([binary, "--config", str(config_path), "listremotes"],
                            capture_output=True, text=True, timeout=30, check=False)
    if listed.returncode or args.remote + ":" not in listed.stdout.splitlines():
        raise ConfigError("Google Drive authorization is not configured")
    with ProcessLock(paths["lock"]):
        conn = connect(paths["db"])
        try:
            init_db(conn)
            report_date = datetime.now().astimezone().date().isoformat()
            reports = generate_reports(conn, cfg["output_dir"], report_date, organized=True, cfg=cfg)
        finally:
            conn.close()
        uploaded = upload_monthly_workbook_rclone(reports, args.remote, binary, config_path, args.folder_id)
        cfg["drive_rclone_remote"] = args.remote
        cfg["drive_rclone_executable"] = binary
        cfg["drive_rclone_config"] = str(config_path)
        cfg["drive_monthly_folder_id"] = args.folder_id
        cfg["drive_rclone_split"] = False
        cfg["drive_direct_upload"] = False
        cfg["drive_sync_folder"] = ""
        save_config(cfg, cfg["config_path"])
    print("Updated %s in %s" % (uploaded["file"], uploaded["folder_url"]))
    print("Future completed runs update that month's source workbook.")
    return 0

def command_connect_google_drive_direct(args):
    cfg = _load(args.config)
    paths = runtime_paths(cfg)
    with ProcessLock(paths["lock"]):
        uploader = connect_drive(args.client_json, paths["runtime"])
        reports = latest_review_paths(cfg["output_dir"])
        uploaded = uploader.upload(reports["latest_batch_text"])
        cfg["drive_direct_upload"] = True
        cfg["drive_rclone_remote"] = ""
        cfg["drive_rclone_executable"] = ""
        cfg["drive_sync_folder"] = ""
        save_config(cfg, cfg["config_path"])
    print("Uploaded the one Work batch file: %s" % uploaded["url"])
    print("Future completed monitor runs will update this same file directly.")
    return 0


def _next_schedule(now=None):
    now = now or datetime.now().astimezone()
    for offset_days in (0, 1):
        day = (now + timedelta(days=offset_days)).date()
        for hour in range(0, 24, 3):
            candidate = datetime.combine(day, datetime.min.time(), tzinfo=now.tzinfo).replace(hour=hour, minute=17)
            if candidate > now:
                return candidate
    return None


def _latest_feed_success(conn, feed_type):
    row = conn.execute("SELECT MAX(last_success_at) FROM feed_state WHERE feed_type=?", (feed_type,)).fetchone()
    return row[0] if row and row[0] else "none"


def command_status(args):
    try:
        cfg = _load(args.config)
    except ConfigError as exc:
        print("Scheduler: %s" % ("installed" if _plist_path().exists() else "not installed"))
        print("Config: %s" % exc)
        return 0
    paths = runtime_paths(cfg)
    conn = connect(paths["db"])
    init_db(conn)
    success, last_run = last_run_info(conn)
    gov = get_governor(conn)
    cooldown = gov["cooldown_until"] or "none"
    scheduled = _plist_path().exists()
    loaded = _launchctl("list", LABEL) if scheduled else None
    print("Scheduler: %s" % ("loaded in macOS" if loaded and loaded.returncode == 0 else
                              "installed but not loaded" if scheduled else "not installed"))
    try:
        with paths["lock"].open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                active = True
            else:
                active = False
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        active = None
    print("Run now: %s" % ("active" if active else "idle" if active is False else "unknown"))
    completed = next((line[1:].split("]", 1)[0] for line in reversed(tail_lines(paths["log"], 5000))
                      if "Run finished." in line and line.startswith("[")), "none")
    print("Last completed run: %s" % completed)
    print("Last successful feed run started: %s" % (success or "none"))
    next_time = _next_schedule()
    print("Next scheduled time: %s" % (next_time.isoformat(timespec="minutes") if next_time else "unknown"))
    print("Cooldown: %s" % cooldown)
    if cfg.get("drive_monthly_folder_id") and cfg.get("drive_rclone_remote"):
        drive_status = "connected (one monthly source workbook)"
    elif cfg.get("drive_rclone_split") and cfg.get("drive_rclone_remote"):
        drive_status = "connected (Replies + Content Ideas)"
    elif cfg.get("drive_direct_upload"):
        drive_status = "connected (legacy one-file uploader)"
    elif cfg.get("drive_rclone_remote") or cfg.get("drive_sync_folder"):
        drive_status = "connected (legacy one-file route)"
    else:
        drive_status = "not connected"
    print("Work Google Drive upload: %s" % drive_status)
    history = tail_lines(paths["log"], 5000)
    last_upload = next((line[1:].split("]", 1)[0] for line in reversed(history)
                        if "Updated monthly source workbook in work Google Drive:" in line
                        and line.startswith("[")), "none")
    print("Last successful monthly upload: %s" % last_upload)
    comment_samples = conn.execute(
        "SELECT COUNT(DISTINCT post_id) FROM comment_fetches WHERE report_date=? AND status=200",
        (datetime.now().astimezone().date().isoformat(),),
    ).fetchone()[0]
    print("Comment feeds collected today: %d" % comment_samples)
    print("Last HTTP status: %s" % (gov["last_http_status"] if gov["last_http_status"] is not None else "none"))
    print("Last coverage issue: %s" % (gov["last_coverage_issue"] or "none"))
    print("Last HOT refresh: %s" % _latest_feed_success(conn, "hot"))
    print("Last RISING refresh: %s" % _latest_feed_success(conn, "rising"))
    print("Last TOP DAY refresh: %s" % _latest_feed_success(conn, "top_day"))
    print("Last TOP WEEK refresh: %s" % _latest_feed_success(conn, "top_week"))
    print("Last 10 log lines:")
    lines = tail_lines(paths["log"], 10)
    if lines:
        for line in lines:
            print("  " + line)
    else:
        print("  (no log yet)")
    conn.close()
    return 0


def _ask_yes_no(prompt, default=False):
    suffix = " [Y/n] " if default else " [y/N] "
    value = input(prompt + suffix).strip().lower()
    if not value:
        return default
    return value in {"y", "yes"}


def command_uninstall(args):
    cfg = None
    try:
        cfg = _load(args.config)
    except ConfigError:
        pass
    output_dir = Path(cfg["output_dir"]).expanduser() if cfg else None
    runtime = Path(cfg["runtime_dir"]).expanduser() if cfg else default_config_path().parent
    remove_scheduler()
    delete_reports = False
    if output_dir and output_dir.exists() and not args.keep_reports:
        delete_reports = _ask_yes_no("Delete generated Reddit reports in %s?" % output_dir, default=False)
    if runtime.exists():
        shutil.rmtree(runtime)
    if delete_reports and output_dir:
        for pattern in ("Reddit-Signals-*", "Reddit-All-Posts-*"):
            for p in output_dir.glob(pattern):
                if p.is_file():
                    p.unlink()
    print("Uninstalled scheduler, database, logs, config, and runtime state. Python was not changed.")
    if output_dir and not delete_reports:
        print("Generated reports were kept.")
    return 0


def command_remove_previous(args):
    remove_scheduler()
    runtime = default_config_path().parent
    if runtime.exists():
        shutil.rmtree(runtime)
    print("Removed prior RedditRSSMonitor scheduler and local runtime state only.")
    print("Generated report folders and Python installations were not touched.")
    return 0


def command_validate_config(args):
    cfg = _load(args.config)
    print("Config valid: %s" % cfg["config_path"])
    delay = int(cfg.get("minimum_request_delay_seconds", 120))
    if delay < int(cfg.get("warn_delay_below_seconds", 60)):
        print("WARNING: request delay %ss is more aggressive than recommended." % delay)
    return 0


def command_rebuild_review(args):
    """Refresh the one visible workbook from saved data, without Reddit requests."""
    cfg = _load(args.config)
    paths = runtime_paths(cfg)
    paths["runtime"].mkdir(parents=True, exist_ok=True)
    conn = connect(paths["db"])
    try:
        init_db(conn)
        with ProcessLock(paths["lock"]):
            report_date = datetime.now().astimezone().date().isoformat()
            reports = generate_reports(conn, cfg["output_dir"], report_date,
                                       organized=True, cfg=cfg)
        print("Open this review workbook: %s" % reports["review_workbook"])
        return 0
    except AlreadyRunning:
        print("Monitor is running. Rebuild after that run finishes.")
        return 0
    finally:
        conn.close()


def command_ai_run(args):
    cfg = _load(args.config)
    paths = runtime_paths(cfg)
    paths["runtime"].mkdir(parents=True, exist_ok=True)
    try:
        with ProcessLock(paths["lock"]):
            result = run_ai(cfg, kind=args.kind)
    except AlreadyRunning:
        print("Collector is running; AI review will wait for the next scheduled run.")
        return 0
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] != "stale_input" else 2


def _run_local_tests(root):
    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=str(root), check=False,
    ).returncode


def command_upgrade(args):
    config_path = Path(args.config or default_config_path()).expanduser()
    # Old configs may point to a filter file in an older extracted package, so
    # path existence must not block the upgrade process.
    cfg = load_config(config_path, require_paths=False)
    old_version = int(cfg.get("version", 1))
    root = Path(__file__).resolve().parent.parent
    print("Existing installation found.")
    print("Current config version: %s" % old_version)
    print("Target config version: %s" % CONFIG_VERSION)
    print("Running local self-tests before touching installed state...")
    if _run_local_tests(root) != 0:
        raise RuntimeError("Upgrade aborted: local self-tests failed; installed state was not changed")

    paths = runtime_paths(cfg)
    paths["runtime"].mkdir(parents=True, exist_ok=True)
    scheduler_path = _plist_path()
    scheduler_was_installed = scheduler_path.exists()
    scheduler_backup = scheduler_path.read_bytes() if scheduler_was_installed else None
    config_backup = config_path.with_suffix(".json.upgrade-backup")
    db_backup = paths["db"].with_suffix(".sqlite3.upgrade-backup")
    if config_path.exists():
        shutil.copy2(config_path, config_backup)

    try:
        with ProcessLock(paths["lock"]):
            conn = connect(paths["db"])
            try:
                init_db(conn)
                if paths["db"].exists():
                    backup_conn = connect(db_backup)
                    try:
                        conn.backup(backup_conn)
                    finally:
                        backup_conn.close()
                if old_version < CONFIG_VERSION:
                    target = db.prepare_rebootstrap(
                        conn, cfg["subreddits"], datetime.now(timezone.utc), hours=24,
                    )
                    print("Discovery checkpoint conservatively rewound for one-time 24-hour bootstrap: %s" % target)
                cfg["version"] = CONFIG_VERSION
                cfg["filter_profile"] = str(default_filter_path().resolve())
                cfg["python_executable"] = sys.executable
                cfg["user_agent"] = "RedditRSSMonitor/1.1 (+contact:%s)" % cfg["contact_email"]
                cfg.setdefault("hot_interval_hours", 6)
                cfg.setdefault("top_day_interval_hours", 12)
                cfg.setdefault("top_week_interval_hours", 12)
                cfg["first_run_recovery_hours"] = max(24, float(cfg.get("first_run_recovery_hours", 24)))
                validate_config(cfg)
                save_config(cfg, config_path)
            finally:
                conn.close()

            if scheduler_was_installed:
                install_scheduler(cfg, config_path)
                print("Scheduler updated to this package.")
    except Exception:
        if config_backup.exists():
            shutil.copy2(config_backup, config_path)
        if db_backup.exists():
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(paths["db"]) + suffix)
                if p.exists():
                    p.unlink()
            shutil.copy2(db_backup, paths["db"])
        if scheduler_was_installed and scheduler_backup is not None:
            scheduler_path.parent.mkdir(parents=True, exist_ok=True)
            scheduler_path.write_bytes(scheduler_backup)
            os.chmod(scheduler_path, 0o644)
            _launchctl("bootout", "gui/%d" % os.getuid(), str(scheduler_path))
            restored = _launchctl("bootstrap", "gui/%d" % os.getuid(), str(scheduler_path))
            if restored is None or restored.returncode != 0:
                _launchctl("load", "-w", str(scheduler_path))
        raise
    finally:
        if config_backup.exists():
            config_backup.unlink()
        if db_backup.exists():
            db_backup.unlink()

    print("UPGRADE COMPLETE")
    print("Configuration, posts, and reports were preserved.")
    print("The next Run Now will perform the v2 bootstrap/ranking refresh where due.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="reddit-rss-monitor")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run")
    p.add_argument("--config", default=None)
    p.add_argument("--enrich", action="store_true", help="force HOT, TOP DAY and TOP WEEK this invocation")
    p.add_argument("--comments-only", action="store_true", help="collect comments for due shortlisted posts without rescanning post feeds")
    p.add_argument("--max-comment-posts", type=int, default=None, help="maximum comment threads in a comments-only run")
    p.add_argument("--force-comments", action="store_true", help="refresh shortlisted comment feeds even within the normal interval")
    p.set_defaults(func=command_run)

    p = sub.add_parser("rebuild-review")
    p.add_argument("--config", default=None)
    p.set_defaults(func=command_rebuild_review)

    p = sub.add_parser("ai-run")
    p.add_argument("--config", default=None)
    p.add_argument("--kind", choices=("replies", "content", "both"), default="both")
    p.set_defaults(func=command_ai_run)

    p = sub.add_parser("status")
    p.add_argument("--config", default=None)
    p.set_defaults(func=command_status)

    p = sub.add_parser("install-scheduler")
    p.add_argument("--config", default=None)
    p.set_defaults(func=command_install_scheduler)

    p = sub.add_parser("connect-drive")
    p.add_argument("--config", default=None)
    p.add_argument("--folder", required=True, help="existing folder shown in Finder by Google Drive for desktop")
    p.set_defaults(func=command_connect_drive)

    p = sub.add_parser("connect-rclone-drive")
    p.add_argument("--config", default=None)
    p.add_argument("--remote", required=True, help="rclone Google Drive remote authenticated as the work account")
    p.add_argument("--binary", default="rclone", help="rclone executable or absolute path")
    p.set_defaults(func=command_connect_rclone_drive)

    p = sub.add_parser("connect-rclone-split")
    p.add_argument("--config", default=None)
    p.add_argument("--remote", default="reddit")
    p.add_argument("--binary", default="rclone")
    p.add_argument("--rclone-config", required=True)
    p.add_argument("--replies-folder-id", required=True)
    p.add_argument("--content-folder-id", required=True)
    p.set_defaults(func=command_connect_rclone_split)

    p = sub.add_parser("connect-rclone-monthly")
    p.add_argument("--config", default=None)
    p.add_argument("--remote", required=True)
    p.add_argument("--binary", default="rclone")
    p.add_argument("--rclone-config", required=True)
    p.add_argument("--folder-id", required=True)
    p.set_defaults(func=command_connect_rclone_monthly)

    p = sub.add_parser("connect-google-drive-direct")
    p.add_argument("--config", default=None)
    p.add_argument("--client-json", required=True, help="Google OAuth Desktop client credentials JSON")
    p.set_defaults(func=command_connect_google_drive_direct)

    p = sub.add_parser("uninstall")
    p.add_argument("--config", default=None)
    p.add_argument("--keep-reports", action="store_true")
    p.set_defaults(func=command_uninstall)

    p = sub.add_parser("remove-previous-builds")
    p.set_defaults(func=command_remove_previous)

    p = sub.add_parser("validate-config")
    p.add_argument("--config", default=None)
    p.set_defaults(func=command_validate_config)

    p = sub.add_parser("upgrade")
    p.add_argument("--config", default=None)
    p.set_defaults(func=command_upgrade)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print("CONFIG ERROR: %s" % exc, file=sys.stderr)
        return 2
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
