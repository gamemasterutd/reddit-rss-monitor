import json
import os
import re
from pathlib import Path

APP_NAME = "RedditRSSMonitor"
CONFIG_VERSION = 2
DEFAULT_SUBREDDITS = ["personalfinanceindia", "IndiaInvestments", "MutualfundsIndia", "FIRE_Ind"]
DEFAULT_NEW_INTERVAL_HOURS = 3
DEFAULT_HOT_INTERVAL_HOURS = 6
DEFAULT_RISING_INTERVAL_HOURS = 6
DEFAULT_TOP_DAY_INTERVAL_HOURS = 12
DEFAULT_TOP_WEEK_INTERVAL_HOURS = 12
DEFAULT_MIN_DELAY_SECONDS = 120
DEFAULT_FIRST_RUN_RECOVERY_HOURS = 24
DEFAULT_429_COOLDOWN_HOURS = 6
DEFAULT_429_REPEAT_COOLDOWN_HOURS = 24
DEFAULT_403_COOLDOWN_HOURS = 24
DEFAULT_FEED_LIMIT = 100


class ConfigError(ValueError):
    pass


def app_support_dir(home=None):
    base = Path(home or Path.home())
    return base / "Library" / "Application Support" / APP_NAME


def default_config_path(home=None):
    return app_support_dir(home) / "config.json"


def project_root():
    return Path(__file__).resolve().parent.parent


def default_filter_path():
    return project_root() / "filters" / "finance.json"


def _valid_email(value):
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value or ""))


def _clean_subreddit(value):
    value = value.strip()
    if value.lower().startswith("r/"):
        value = value[2:]
    if not re.fullmatch(r"[A-Za-z0-9_]{2,50}", value):
        raise ConfigError("Invalid subreddit: %s" % value)
    return value


def normalize_subreddits(values):
    seen = set()
    out = []
    for value in values:
        item = _clean_subreddit(value)
        key = item.lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    if not out:
        raise ConfigError("At least one subreddit is required")
    return out


def _compat_defaults(cfg):
    """Add safe defaults when an older config is opened by newer code."""
    cfg.setdefault("version", 1)
    cfg.setdefault("new_interval_hours", DEFAULT_NEW_INTERVAL_HOURS)
    cfg.setdefault("hot_interval_hours", DEFAULT_HOT_INTERVAL_HOURS)
    cfg.setdefault("rising_interval_hours", DEFAULT_RISING_INTERVAL_HOURS)
    cfg.setdefault("enable_rising_feed", True)
    cfg.setdefault("top_day_interval_hours", DEFAULT_TOP_DAY_INTERVAL_HOURS)
    cfg.setdefault("top_week_interval_hours", DEFAULT_TOP_WEEK_INTERVAL_HOURS)
    cfg.setdefault("minimum_request_delay_seconds", DEFAULT_MIN_DELAY_SECONDS)
    cfg.setdefault("first_run_recovery_hours", DEFAULT_FIRST_RUN_RECOVERY_HOURS)
    cfg.setdefault("cooldown_429_hours", DEFAULT_429_COOLDOWN_HOURS)
    cfg.setdefault("cooldown_429_repeat_hours", DEFAULT_429_REPEAT_COOLDOWN_HOURS)
    cfg.setdefault("cooldown_403_hours", DEFAULT_403_COOLDOWN_HOURS)
    cfg.setdefault("feed_limit", DEFAULT_FEED_LIMIT)
    cfg.setdefault("request_timeout_seconds", 30)
    cfg.setdefault("server_error_retry_once", True)
    cfg.setdefault("warn_delay_below_seconds", 60)
    cfg.setdefault("drive_sync_folder", "")
    cfg.setdefault("drive_rclone_remote", "")
    cfg.setdefault("drive_rclone_executable", "")
    cfg.setdefault("drive_rclone_split", False)
    cfg.setdefault("drive_monthly_folder_id", "")
    cfg.setdefault("drive_direct_upload", False)
    cfg.setdefault("collect_comments", True)
    cfg.setdefault("comment_feed_limit", 50)
    cfg.setdefault("comment_max_posts_per_run", 10)
    cfg.setdefault("comment_max_requests_per_day", 48)
    cfg.setdefault("comment_refresh_hours", 6)
    cfg.setdefault("comment_new_relevant_extra_per_run", 4)
    cfg.setdefault("organized_output", True)
    cfg.setdefault("ai_enabled", False)
    cfg.setdefault("ai_provider", "gemini")
    cfg.setdefault("ai_model", "")
    cfg.setdefault("ai_key_file", "")
    cfg.setdefault("ai_reply_prompt_file", "")
    cfg.setdefault("ai_content_prompt_file", "")
    cfg.setdefault("ai_max_requests_per_day", 12)
    cfg.setdefault("ai_content_hour_ist", 21)
    return cfg


def default_config(contact_email, output_dir, subreddits=None, runtime_dir=None, filter_profile=None):
    subreddits = normalize_subreddits(subreddits or DEFAULT_SUBREDDITS)
    if not _valid_email(contact_email):
        raise ConfigError("A valid contact email is required for the User-Agent")
    output_dir = str(Path(output_dir).expanduser().resolve())
    runtime = Path(runtime_dir).expanduser().resolve() if runtime_dir else app_support_dir()
    profile = Path(filter_profile).expanduser().resolve() if filter_profile else default_filter_path()
    return {
        "version": CONFIG_VERSION,
        "contact_email": contact_email,
        "user_agent": "RedditRSSMonitor/1.1 (+contact:%s)" % contact_email,
        "subreddits": subreddits,
        "output_dir": output_dir,
        "runtime_dir": str(runtime),
        "filter_profile": str(profile),
        "new_interval_hours": DEFAULT_NEW_INTERVAL_HOURS,
        "hot_interval_hours": DEFAULT_HOT_INTERVAL_HOURS,
        "rising_interval_hours": DEFAULT_RISING_INTERVAL_HOURS,
        "enable_rising_feed": True,
        "top_day_interval_hours": DEFAULT_TOP_DAY_INTERVAL_HOURS,
        "top_week_interval_hours": DEFAULT_TOP_WEEK_INTERVAL_HOURS,
        "minimum_request_delay_seconds": DEFAULT_MIN_DELAY_SECONDS,
        "first_run_recovery_hours": DEFAULT_FIRST_RUN_RECOVERY_HOURS,
        "cooldown_429_hours": DEFAULT_429_COOLDOWN_HOURS,
        "cooldown_429_repeat_hours": DEFAULT_429_REPEAT_COOLDOWN_HOURS,
        "cooldown_403_hours": DEFAULT_403_COOLDOWN_HOURS,
        "feed_limit": DEFAULT_FEED_LIMIT,
        "request_timeout_seconds": 30,
        "server_error_retry_once": True,
        "drive_sync_folder": "",
        "drive_rclone_remote": "",
        "drive_rclone_executable": "",
        "drive_rclone_split": False,
        "drive_monthly_folder_id": "",
        "drive_direct_upload": False,
        "collect_comments": True,
        "comment_feed_limit": 50,
        "comment_max_posts_per_run": 12,
        "comment_max_requests_per_day": 48,
        "comment_new_relevant_extra_per_run": 4,
        "organized_output": True,
        "comment_refresh_hours": 6,
        "warn_delay_below_seconds": 60,
        "ai_enabled": False,
        "ai_provider": "gemini",
        "ai_model": "",
        "ai_key_file": "",
        "ai_reply_prompt_file": "",
        "ai_content_prompt_file": "",
        "ai_max_requests_per_day": 12,
        "ai_content_hour_ist": 21,
    }


def validate_config(cfg, require_paths=True):
    _compat_defaults(cfg)
    required = ["contact_email", "subreddits", "output_dir", "runtime_dir", "filter_profile"]
    missing = [k for k in required if k not in cfg]
    if missing:
        raise ConfigError("Missing config keys: %s" % ", ".join(missing))
    if not _valid_email(cfg["contact_email"]):
        raise ConfigError("contact_email is invalid")
    cfg["subreddits"] = normalize_subreddits(cfg["subreddits"])
    delay = int(cfg.get("minimum_request_delay_seconds", DEFAULT_MIN_DELAY_SECONDS))
    if delay < 1:
        raise ConfigError("minimum_request_delay_seconds must be >= 1")
    if int(cfg.get("feed_limit", DEFAULT_FEED_LIMIT)) < 1 or int(cfg.get("feed_limit", DEFAULT_FEED_LIMIT)) > 100:
        raise ConfigError("feed_limit must be between 1 and 100")
    for key in ("comment_feed_limit", "comment_max_posts_per_run", "comment_max_requests_per_day"):
        value = int(cfg[key])
        if value < 1 or (key == "comment_feed_limit" and value > 100):
            raise ConfigError("%s is outside the supported range" % key)
    if not 0 <= int(cfg["comment_new_relevant_extra_per_run"]) <= 8:
        raise ConfigError("comment_new_relevant_extra_per_run must be 0–8")
    if float(cfg["comment_refresh_hours"]) <= 0:
        raise ConfigError("comment_refresh_hours must be > 0")
    if cfg["ai_provider"] not in ("gemini", "groq", "openrouter"):
        raise ConfigError("ai_provider must be gemini, groq, or openrouter")
    if cfg.get("ai_enabled") and not (cfg.get("ai_model") or "").strip():
        raise ConfigError("Set ai_model before enabling AI review")
    if not 1 <= int(cfg["ai_max_requests_per_day"]) <= 100:
        raise ConfigError("ai_max_requests_per_day must be 1–100")
    if not 0 <= int(cfg["ai_content_hour_ist"]) <= 23:
        raise ConfigError("ai_content_hour_ist must be 0–23")
    for key, default in (
        ("new_interval_hours", DEFAULT_NEW_INTERVAL_HOURS),
        ("hot_interval_hours", DEFAULT_HOT_INTERVAL_HOURS),
        ("rising_interval_hours", DEFAULT_RISING_INTERVAL_HOURS),
        ("top_day_interval_hours", DEFAULT_TOP_DAY_INTERVAL_HOURS),
        ("top_week_interval_hours", DEFAULT_TOP_WEEK_INTERVAL_HOURS),
        ("first_run_recovery_hours", DEFAULT_FIRST_RUN_RECOVERY_HOURS),
    ):
        try:
            value = float(cfg.get(key, default))
        except (TypeError, ValueError):
            raise ConfigError("%s must be numeric" % key)
        if value <= 0:
            raise ConfigError("%s must be > 0" % key)
    if require_paths:
        profile = Path(cfg["filter_profile"]).expanduser()
        if not profile.exists():
            raise ConfigError("Filter profile not found: %s" % profile)
    return cfg


def load_config(path=None, require_paths=True):
    path = Path(path or os.environ.get("REDDIT_MONITOR_CONFIG") or default_config_path()).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError("Config not found: %s" % path)
    except json.JSONDecodeError as exc:
        raise ConfigError("Config is not valid JSON: %s" % exc)
    validate_config(data, require_paths=require_paths)
    data["config_path"] = str(path.resolve())
    return data


def save_config(cfg, path):
    validate_config(cfg, require_paths=False)
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    serializable = dict(cfg)
    serializable.pop("config_path", None)
    tmp.write_text(json.dumps(serializable, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path
