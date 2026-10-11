from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from . import db
from .engagement import engagement_from_ranks
from .feed_parser import parse_feed
from .comment_collector import collect_comments
from .filter_engine import RelevanceEngine
from .http_client import CooldownActive, RedditHTTP
from .reports import generate_reports


def _parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _utc_iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _level_value(level):
    return {"LOW": 1, "MEDIUM": 2, "HIGH": 3}.get(level, 0)


class Collector:
    RANKING_FEEDS = ("hot", "rising", "top_day", "top_week")

    def __init__(self, cfg, conn, progress=print, http=None, now_func=None, base_url_override=None):
        self.cfg = cfg
        self.conn = conn
        self.progress = progress or (lambda msg: None)
        self.engine = RelevanceEngine.from_file(cfg["filter_profile"])
        self.now_func = now_func or (lambda: datetime.now(timezone.utc))
        self.base_url_override = base_url_override
        self.http = http or RedditHTTP(
            conn,
            cfg["user_agent"],
            minimum_delay_seconds=cfg.get("minimum_request_delay_seconds", 120),
            timeout=cfg.get("request_timeout_seconds", 30),
            cooldown_429_hours=cfg.get("cooldown_429_hours", 6),
            cooldown_429_repeat_hours=cfg.get("cooldown_429_repeat_hours", 24),
            cooldown_403_hours=cfg.get("cooldown_403_hours", 24),
        )

    def _feed_url(self, subreddit, feed_type):
        root = (self.base_url_override or "https://www.reddit.com").rstrip("/")
        base = "%s/r/%s" % (root, subreddit)
        limit = int(self.cfg.get("feed_limit", 100))
        if feed_type == "new":
            return "%s/new/.rss?%s" % (base, urlencode({"limit": limit}))
        if feed_type == "hot":
            return "%s/hot/.rss?%s" % (base, urlencode({"limit": limit}))
        if feed_type == "rising":
            return "%s/rising/.rss?%s" % (base, urlencode({"limit": limit}))
        if feed_type == "top_day":
            return "%s/top/.rss?%s" % (base, urlencode({"t": "day", "limit": limit}))
        if feed_type == "top_week":
            return "%s/top/.rss?%s" % (base, urlencode({"t": "week", "limit": limit}))
        raise ValueError("Unknown feed type")

    def _conditional(self, subreddit, feed_type):
        row = db.get_feed_state(self.conn, subreddit, feed_type)
        if not row:
            return {}
        return {"etag": row["etag"], "last_modified": row["last_modified"]}

    def _ranking_values(self, row):
        return row["hot_rank"], row["top_day_rank"], row["top_week_rank"], row["rising_rank"]

    def _recompute_engagement(self, subreddit, report_date, now_iso, run_time):
        for row in db.posts_for_subreddit(self.conn, subreddit):
            new_level, evidence = engagement_from_ranks(*self._ranking_values(row))
            old_level = row["engagement"]
            if new_level != old_level or evidence != row["signal_evidence"]:
                db.set_post_ranks(
                    self.conn, row["post_id"], row["hot_rank"], row["top_day_rank"],
                    row["top_week_rank"], new_level, evidence, now_iso, rising_rank=row["rising_rank"],
                )
                if row["classification"] in ("relevant", "review") and _level_value(new_level) > _level_value(old_level):
                    db.mark_report_day(self.conn, report_date, row["post_id"], "engagement_upgrade", run_time, new_level, evidence)

    def _process_entry(self, entry, feed_type, rank, run_time, report_date, now_iso):
        result = self.engine.classify(entry.title, entry.description)
        existing = db.get_post(self.conn, entry.post_id)
        hot_rank = existing["hot_rank"] if existing else None
        rising_rank = existing["rising_rank"] if existing else None
        top_day_rank = existing["top_day_rank"] if existing else None
        top_week_rank = existing["top_week_rank"] if existing else None
        if feed_type == "hot":
            hot_rank = rank
        elif feed_type == "rising":
            rising_rank = rank
        elif feed_type == "top_day":
            top_day_rank = rank
        elif feed_type == "top_week":
            top_week_rank = rank
        level, evidence = engagement_from_ranks(hot_rank, top_day_rank, top_week_rank, rising_rank)
        created, previous = db.upsert_post(
            self.conn, entry, result, run_time, now_iso,
            hot_rank=hot_rank, top_day_rank=top_day_rank, top_week_rank=top_week_rank, rising_rank=rising_rank,
            engagement=level, evidence=evidence,
        )
        db.mark_observation_day(self.conn, report_date, entry.post_id, run_time, feed_type)

        if result.classification in ("relevant", "review"):
            # Any ranking appearance today is a current research signal, even if
            # the post was first discovered on a previous day and its engagement
            # level did not increase.
            if feed_type in self.RANKING_FEEDS:
                db.mark_report_day(self.conn, report_date, entry.post_id, "ranking_seen_today", run_time, level, evidence)
            elif created:
                db.mark_report_day(self.conn, report_date, entry.post_id, "first_detection", run_time, level, evidence)
            elif previous and previous["classification"] != result.classification:
                db.mark_report_day(self.conn, report_date, entry.post_id, "classification_change", run_time, level, evidence)
            elif previous and _level_value(level) > _level_value(previous["engagement"]):
                db.mark_report_day(self.conn, report_date, entry.post_id, "engagement_upgrade", run_time, level, evidence)
            else:
                # A qualifying NEW post in today's incremental scan should be in
                # today's output even if it had already been observed elsewhere.
                db.mark_report_day(self.conn, report_date, entry.post_id, "new_seen_today", run_time, level, evidence)
        return result

    def _fetch_with_server_retry(self, url, conditional):
        result = self.http.fetch(url, conditional=conditional, progress=self.progress)
        if 500 <= result.status <= 599 and self.cfg.get("server_error_retry_once", True):
            self.progress("Server error %d; one conservative retry will be attempted after the global rate guard." % result.status)
            result = self.http.fetch(url, conditional=conditional, progress=self.progress)
        return result

    def _new_coverage(self, entries, required_start_dt):
        if not entries:
            return "GAP", "NEW feed was unexpectedly empty; coverage cannot be proven"
        oldest = min(_parse_iso(e.published) for e in entries)
        if oldest <= required_start_dt:
            return "OK", ""
        limit = int(self.cfg.get("feed_limit", 100))
        if len(entries) >= limit:
            return "GAP", "oldest feed item does not reach required checkpoint and feed is at configured capacity"
        # When fewer than the requested maximum are returned, capacity did not
        # truncate the feed. There may simply have been no older posts.
        return "OK", "Feed returned fewer than capacity; no truncation evidence"

    def _run_feed(self, subreddit, feed_type, run_time, report_date, scan_end):
        url = self._feed_url(subreddit, feed_type)
        conditional = self._conditional(subreddit, feed_type)
        if feed_type == "new":
            checkpoint = db.get_checkpoint(self.conn, subreddit)
            if checkpoint:
                scan_start = checkpoint
            else:
                scan_start = _utc_iso(scan_end - timedelta(hours=float(self.cfg.get("first_run_recovery_hours", 24))))
        else:
            scan_start = ""

        try:
            result = self._fetch_with_server_retry(url, conditional)
        except CooldownActive as exc:
            return {"stop": True, "cooldown": exc.until, "requested": False}

        # Use a per-feed snapshot time captured after the HTTP operation. A full
        # run is intentionally slow because requests are sequential and rate
        # guarded; later feeds can therefore contain posts published after the
        # overall run began. The checkpoint and validators must describe the
        # same snapshot we actually processed, otherwise a later 304 could mask
        # an item that was present in the cached response but excluded by an
        # earlier run-wide cutoff.
        snapshot_end = self.now_func()
        if snapshot_end.tzinfo is None:
            snapshot_end = snapshot_end.replace(tzinfo=timezone.utc)
        snapshot_end = snapshot_end.astimezone(timezone.utc)
        if snapshot_end < scan_end:
            # A custom/test clock must never move the checkpoint backwards.
            snapshot_end = scan_end

        status = result.status
        db.update_feed_status_only(self.conn, subreddit, feed_type, status)
        label = "%s/%s" % (feed_type, subreddit)
        scan_end_iso = _utc_iso(snapshot_end)

        if result.stopped:
            db.insert_run(
                self.conn, run_time, label, scan_start, scan_end_iso, status, 0, 0, 0, 0,
                "STOPPED", "Persistent cooldown until %s" % result.cooldown_until,
            )
            return {"stop": True, "cooldown": result.cooldown_until, "requested": True, "status": status}

        if status == 304:
            state = db.get_feed_state(self.conn, subreddit, feed_type)
            if not state or not state["last_success_at"]:
                db.insert_run(
                    self.conn, run_time, label, scan_start, scan_end_iso, status, 0, 0, 0, 0,
                    "ERROR", "Unexpected 304 without a prior successful conditional-feed state; checkpoint not advanced",
                )
                return {"stop": False, "requested": True, "status": status, "entries": 0, "evaluated": 0,
                        "ignored": 0, "relevant": 0, "review": 0, "excluded": 0, "coverage": "ERROR"}
            if feed_type == "new":
                db.set_checkpoint(self.conn, subreddit, scan_end_iso)
            else:
                db.mark_unchanged_ranking_feed_for_day(self.conn, subreddit, feed_type, report_date, run_time)
            db.record_feed_snapshot(
                self.conn, run_time, scan_end_iso, subreddit, feed_type, [], http_status=304,
            )
            db.save_feed_state(
                self.conn, subreddit, feed_type, state["etag"], state["last_modified"], scan_end_iso, status,
            )
            db.insert_run(
                self.conn, run_time, label, scan_start, scan_end_iso, status, 0, 0, 0, 0,
                "OK", "Not modified; prior successful feed state remains current",
            )
            return {"stop": False, "requested": True, "status": status, "entries": 0, "evaluated": 0,
                    "ignored": 0, "relevant": 0, "review": 0, "excluded": 0, "coverage": "OK"}

        if status != 200:
            note = result.error or "HTTP response did not produce a usable feed"
            db.insert_run(self.conn, run_time, label, scan_start, scan_end_iso, status, 0, 0, 0, 0, "ERROR", note)
            return {"stop": False, "requested": True, "status": status, "entries": 0, "evaluated": 0,
                    "ignored": 0, "relevant": 0, "review": 0, "excluded": 0, "coverage": "ERROR"}

        try:
            entries = parse_feed(result.body, subreddit)
        except Exception as exc:
            db.insert_run(
                self.conn, run_time, label, scan_start, scan_end_iso, status, 0, 0, 0, 0,
                "ERROR", "Feed XML parse failed: %s" % exc,
            )
            return {"stop": False, "requested": True, "status": status, "entries": 0, "evaluated": 0,
                    "ignored": 0, "relevant": 0, "review": 0, "excluded": 0, "coverage": "ERROR"}

        coverage = "N/A"
        coverage_note = ""
        required_start_dt = _parse_iso(scan_start) if scan_start else None
        if feed_type == "new":
            coverage, coverage_note = self._new_coverage(entries, required_start_dt)

        counts = {"relevant": 0, "review": 0, "excluded": 0}
        evaluated = 0
        ignored = 0

        if feed_type in self.RANKING_FEEDS:
            db.clear_rank_for_subreddit(self.conn, subreddit, feed_type)

        for rank, entry in enumerate(entries, 1):
            if feed_type == "new" and required_start_dt is not None:
                published_dt = _parse_iso(entry.published)
                if not (required_start_dt <= published_dt <= snapshot_end):
                    ignored += 1
                    continue
            result_filter = self._process_entry(entry, feed_type, rank, run_time, report_date, scan_end_iso)
            evaluated += 1
            counts[result_filter.classification] += 1

        if feed_type in self.RANKING_FEEDS:
            self._recompute_engagement(subreddit, report_date, scan_end_iso, run_time)

        db.record_feed_snapshot(
            self.conn, run_time, scan_end_iso, subreddit, feed_type, entries, http_status=200,
        )

        etag = result.headers.get("ETag") or result.headers.get("Etag") or result.headers.get("etag")
        last_modified = result.headers.get("Last-Modified") or result.headers.get("last-modified")
        notes = coverage_note
        if feed_type == "new" and coverage == "GAP":
            issue = "%s: %s; required start %s" % (subreddit, coverage_note, scan_start)
            db.update_governor(self.conn, last_coverage_issue="%s at %s" % (issue, run_time))
            notes = issue + "; checkpoint and conditional cache validators were NOT advanced"
        else:
            db.save_feed_state(self.conn, subreddit, feed_type, etag, last_modified, scan_end_iso, status)
            if feed_type == "new":
                db.set_checkpoint(self.conn, subreddit, scan_end_iso)

        if feed_type == "new":
            extra = "evaluated=%d; older/already-covered=%d" % (evaluated, ignored)
            notes = (notes + "; " + extra).strip("; ")

        db.insert_run(
            self.conn, run_time, label, scan_start, scan_end_iso, status, len(entries),
            counts["relevant"], counts["review"], counts["excluded"], coverage, notes,
        )
        return {
            "stop": False, "requested": True, "status": status, "entries": len(entries),
            "evaluated": evaluated, "ignored": ignored,
            "relevant": counts["relevant"], "review": counts["review"], "excluded": counts["excluded"],
            "coverage": coverage,
        }

    def _feed_plan(self, now, force_enrichment=False):
        feeds = [(s, "new") for s in self.cfg["subreddits"]]
        intervals = (
            ("hot", float(self.cfg.get("hot_interval_hours", 6))),
            ("rising", float(self.cfg.get("rising_interval_hours", 6))),
            ("top_day", float(self.cfg.get("top_day_interval_hours", 12))),
            ("top_week", float(self.cfg.get("top_week_interval_hours", 12))),
        )
        for feed_type, hours in intervals:
            if feed_type == "rising" and not self.cfg.get("enable_rising_feed", True):
                continue
            for subreddit in self.cfg["subreddits"]:
                if force_enrichment or db.feed_due(self.conn, subreddit, feed_type, now, hours):
                    feeds.append((subreddit, feed_type))
        return feeds

    def run_comments_only(self, max_posts=None, force=False):
        now = self.now_func().astimezone()
        report_date = now.date().isoformat()
        if self.cfg.get("collect_comments", True):
            stats = collect_comments(self.conn, self.http, self.cfg, report_date,
                                     progress=self.progress, max_posts=max_posts, force=force)
            self.progress("Comment review: %d of %d fresh comment candidates requested." %
                          (stats["requested"], stats["candidates"]))
        return generate_reports(self.conn, self.cfg["output_dir"], report_date,
                                organized=self.cfg.get("organized_output", False), cfg=self.cfg)

    def run(self, force_enrichment=False):
        now = self.now_func()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        local_now = now.astimezone()
        run_time = local_now.isoformat(timespec="seconds")
        report_date = local_now.date().isoformat()
        cooldown = self.http.cooldown_until()
        if cooldown:
            self.progress("Cooldown active until %s. Zero Reddit requests will be made." % cooldown)
            db.insert_run(
                self.conn, run_time, "GLOBAL", "", _utc_iso(now), "COOLDOWN", 0, 0, 0, 0,
                "SKIPPED", "Persistent request cooldown",
            )
            return generate_reports(self.conn, self.cfg["output_dir"], report_date,
                                organized=self.cfg.get("organized_output", False), cfg=self.cfg)

        first_run = all(db.get_checkpoint(self.conn, s) is None for s in self.cfg["subreddits"])
        if first_run:
            self.progress(
                "First-run baseline: NEW recovery window=%sh plus HOT, RISING, TOP DAY and TOP WEEK for every subreddit."
                % self.cfg.get("first_run_recovery_hours", 24)
            )
        feeds = self._feed_plan(now, force_enrichment=force_enrichment)
        counts = {name: sum(1 for _, ft in feeds if ft == name) for name in ("new", "hot", "rising", "top_day", "top_week")}
        self.progress(
            "Request plan: %d NEW, %d HOT, %d RISING, %d TOP DAY, %d TOP WEEK (sequential, %ss minimum spacing)."
            % (counts["new"], counts["hot"], counts["rising"], counts["top_day"], counts["top_week"],
               self.cfg.get("minimum_request_delay_seconds", 120))
        )

        total = len(feeds)
        stopped = False
        for idx, (subreddit, feed_type) in enumerate(feeds, 1):
            self.progress("[%d/%d] Checking %s: %s..." % (idx, total, feed_type.replace("_", " "), subreddit))
            outcome = self._run_feed(subreddit, feed_type, run_time, report_date, now)
            if outcome.get("requested") and "entries" in outcome:
                if feed_type == "new":
                    self.progress("  %d feed entries returned" % outcome["entries"])
                    self.progress(
                        "  %d evaluated | %d relevant | %d review | %d excluded"
                        % (outcome["evaluated"], outcome["relevant"], outcome["review"], outcome["excluded"])
                    )
                    if outcome["ignored"]:
                        self.progress("  %d older/already-covered feed entries ignored" % outcome["ignored"])
                    self.progress("  Coverage: %s" % outcome["coverage"])
                else:
                    self.progress(
                        "  %d ranking entries | %d relevant | %d review | %d excluded"
                        % (outcome["evaluated"], outcome["relevant"], outcome["review"], outcome["excluded"])
                    )
            if outcome.get("stop"):
                self.progress("Run stopped safely. Remaining feeds will not be requested.")
                stopped = True
                break
        if not stopped and self.cfg.get("collect_comments", True):
            stats = collect_comments(self.conn, self.http, self.cfg, report_date, progress=self.progress)
            self.progress("Comment review: %d of %d fresh comment candidates requested." %
                          (stats["requested"], stats["candidates"]))
        return generate_reports(self.conn, self.cfg["output_dir"], report_date,
                                organized=self.cfg.get("organized_output", False), cfg=self.cfg)
