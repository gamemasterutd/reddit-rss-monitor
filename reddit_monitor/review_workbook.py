"""One human-facing workbook over all locally stored Reddit observations."""

import json
from datetime import datetime, timezone
from pathlib import Path

from .review_export import _image_only_allocation_risk
from .xlsx_writer import validate_xlsx, write_xlsx


FILE_NAME = "Reddit-Stream-%s.xlsx"
SHEET_NAMES = ("Start Here", "Latest Replies", "Reply Candidates", "Content by Day", "Comments",
               "Comment Requests", "Runs", "Feed Placements", "All Posts", "Method")


def _short(value, maximum=32000):
    return str(value or "")[:maximum]


def _count_comments(conn):
    return {r["post_id"]: r["n"] for r in conn.execute(
        "SELECT post_id,COUNT(*) n FROM post_comments GROUP BY post_id")}


def _sample_comments(conn):
    result = {}
    for row in conn.execute("SELECT post_id,body FROM post_comments ORDER BY published_at DESC,comment_id DESC"):
        bucket = result.setdefault(row["post_id"], [])
        if len(bucket) < 3:
            bucket.append(_short(row["body"], 500))
    return {key: "\n\n".join(value) for key, value in result.items()}


def _latest_fetch(conn):
    result = {}
    for row in conn.execute("SELECT * FROM comment_fetches ORDER BY id DESC"):
        result.setdefault(row["post_id"], row)
    return result


def _run_coverage(run):
    if run["coverage_status"] == "N/A" and not run["feed_subreddit"].startswith("new/"):
        return "Ranking snapshot collected" if run["http_status"] in ("200", "304") else "Ranking snapshot failed"
    if run["coverage_status"] == "OK" and run["feed_subreddit"].startswith("new/"):
        return "NEW checkpoint covered"
    return run["coverage_status"]


def _comment_review_status(state, fetch, count, classification, image_hold):
    if state and state["last_success_at"]:
        latest_failed = bool(fetch and fetch["status"] not in (200, 304)
                             and fetch["attempted_at"] > state["last_success_at"])
        if latest_failed:
            comment_status = "Saved sample; latest fetch failed"
        elif count:
            comment_status = "Sample saved"
        else:
            comment_status = "Empty RSS sample"
    else:
        comment_status = "Fetch failed / retry pending" if state else "Awaiting comment fetch"
    if image_hold:
        review_state = "HOLD: source image unreadable"
    elif classification == "review":
        review_state = "HUMAN REVIEW: relevance uncertain"
    elif comment_status == "Sample saved":
        review_state = "READY: verify live thread"
    elif comment_status == "Empty RSS sample":
        review_state = "HUMAN REVIEW: no saved comments"
    elif comment_status == "Saved sample; latest fetch failed":
        review_state = "HUMAN REVIEW: comments may be stale"
    else:
        review_state = "WAITING: comments"
    return comment_status, review_state


def generate_review_workbook(conn, output_dir, cfg, report_date=None):
    """Rebuild the stable workbook after each run; SQLite remains the full archive."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    month = (report_date or now.astimezone().date().isoformat())[:7]
    month_like = month + "%"
    posts = list(conn.execute(
        """SELECT p.* FROM posts p WHERE EXISTS (
             SELECT 1 FROM post_observation_days o
             WHERE o.post_id=p.post_id AND o.report_date LIKE ?)
           ORDER BY p.first_seen_at DESC,p.post_id""", (month_like,)))
    comment_counts = _count_comments(conn)
    samples = _sample_comments(conn)
    states = {r["post_id"]: r for r in conn.execute("SELECT * FROM comment_state")}
    fetches = _latest_fetch(conn)
    latest_run = conn.execute("SELECT MAX(run_time) FROM runs WHERE run_time LIKE ?", (month_like,)).fetchone()[0] or "none"
    latest_placements = {}
    for placement in conn.execute(
        """SELECT i.post_id,s.feed_type,i.rank,i.change_status,s.observed_at
           FROM feed_snapshots s JOIN feed_snapshot_items i ON i.snapshot_id=s.id
           WHERE s.run_time LIKE ? AND s.feed_type IN ('new','hot','rising','top_day','top_week')
           ORDER BY s.id DESC,i.rank""", (month_like,)):
        latest_placements.setdefault(placement["post_id"], {}).setdefault(placement["feed_type"], placement)

    start = [
        ["Open first", "Latest Replies", "All relevant and review-classified posts still under 24 hours old, with LATEST RUN marking newly discovered posts."],
        ["Reply archive", "Reply Candidates", "All candidate posts this month, including comment and source status."],
        ["Daily content input", "Content by Day", "Every relevant post observed on each day, with saved comments where available."],
        ["Full evidence", "Comments / Comment Requests / Runs / Feed Placements / All Posts", "These tabs show what was collected, when and why."],
        ["Month", month, "One source workbook for this calendar month (local run dates)."],
        ["Updated UTC", now.isoformat(timespec="seconds"), "This file is rebuilt after each completed run."],
        ["Latest run local", latest_run, "See Runs for each feed and its actual coverage status."],
        ["Distinct posts", len(posts), "Includes posts filtered out of the reply and content tabs."],
        ["Posts with saved comments", sum(1 for p in posts if p["post_id"] in comment_counts), "Saved RSS sample; not the total Reddit comment count."],
        ["Votes / total comments", "Not verified", "Feed placement is not a vote count."],
    ]

    reply = []
    for p in posts:
        if p["classification"] not in ("relevant", "review"):
            continue
        try:
            age_hours = (now - datetime.fromisoformat(p["post_time"].replace("Z", "+00:00"))).total_seconds() / 3600
        except (ValueError, TypeError):
            age_hours = 9999
        if age_hours < 0:
            continue
        state, fetch = states.get(p["post_id"]), fetches.get(p["post_id"])
        saved_count = comment_counts.get(p["post_id"], 0)
        image_hold = _image_only_allocation_risk(p)
        comment_status, review_state = _comment_review_status(
            state, fetch, saved_count, p["classification"], image_hold)
        by_feed = latest_placements.get(p["post_id"], {})
        placement = " | ".join("%s #%s · %s · %s UTC" % (by_feed[feed]["feed_type"].upper().replace("_", " "),
                             by_feed[feed]["rank"], by_feed[feed]["change_status"],
                             by_feed[feed]["observed_at"][:16])
                             for feed in ("new", "rising", "hot", "top_day", "top_week") if feed in by_feed)
        reply.append([
            p["first_detected_run"][:10], p["first_detected_run"], p["post_id"], p["subreddit"], p["title"],
            _short(p["description"]), p["permalink"], p["post_time"], p["engagement"], p["signal_evidence"],
            placement or "No tracked placement",
            "Source image/allocation unavailable: do not draft" if image_hold else "Verify live OP before posting",
            "Under 24h" if age_hours < 24 else "Past 24h",
            "LATEST RUN" if p["first_detected_run"] == latest_run else "Earlier run",
            comment_status,
            saved_count, state["last_success_at"] if state else "Not collected",
            fetch["selection_reason"] if fetch and fetch["selection_reason"] else ("Fresh post; waiting for request slot" if age_hours < 24 else "No saved comment request"),
            samples.get(p["post_id"], "No saved comment sample"), "RSS comment coverage not verified",
            p["classification"].upper(), p["review_reason"],
            review_state,
        ])

    latest = [row for row in reply if row[12] == "Under 24h"]
    start.append(["Active reply window", len(latest),
                  "Open Latest Replies for all under-24h candidates; LATEST RUN marks newly added posts."])
    start.append(["Fresh candidates awaiting comments",
                  sum(1 for row in reply if row[12] == "Under 24h" and row[14] != "Sample saved"),
                  "These can be delayed by request caps, cooldown, or a failed feed. Check Reply Candidates."])

    content = []
    for r in conn.execute(
        """SELECT o.report_date,o.first_observed_run,o.last_observed_run,o.feeds_seen,p.*
           FROM post_observation_days o JOIN posts p ON p.post_id=o.post_id
           WHERE p.classification IN ('relevant','review') AND o.report_date LIKE ?
           ORDER BY o.report_date DESC,o.first_observed_run DESC,p.post_id""", (month_like,)):
        state = states.get(r["post_id"])
        try:
            feeds = ", ".join(json.loads(r["feeds_seen"] or "[]"))
        except (TypeError, ValueError):
            feeds = _short(r["feeds_seen"])
        content.append([
            r["report_date"], r["first_observed_run"], r["last_observed_run"], r["post_id"],
            r["subreddit"], r["title"], _short(r["description"]), r["permalink"], r["post_time"],
            r["topic"], r["engagement"], r["signal_evidence"], feeds,
            comment_counts.get(r["post_id"], 0), state["last_success_at"] if state else "Not collected",
            samples.get(r["post_id"], "No saved comment sample"), "RSS comment coverage not verified",
            r["classification"].upper(), r["review_reason"],
        ])

    comments = [[r["observed_at"][:10], r["observed_at"], r["post_id"], r["subreddit"], r["title"],
                 r["comment_id"], r["published_at"], r["permalink"], _short(r["body"], 3000)]
                for r in conn.execute(
                    """SELECT c.*,p.subreddit,p.title FROM post_comments c JOIN posts p ON p.post_id=c.post_id
                       WHERE EXISTS (SELECT 1 FROM post_observation_days o WHERE o.post_id=c.post_id AND o.report_date LIKE ?)
                       ORDER BY c.observed_at DESC,c.post_id,c.published_at DESC""", (month_like,))]
    requests = [[r["report_date"], r["attempted_at"], r["post_id"], r["subreddit"], r["title"],
                 r["permalink"], r["selection_reason"] or "Earlier shortlist; reason not stored",
                 r["status"], r["note"]] for r in conn.execute(
                    """SELECT f.*,p.subreddit,p.title,p.permalink FROM comment_fetches f
                       JOIN posts p ON p.post_id=f.post_id
                       WHERE f.report_date LIKE ? ORDER BY f.id DESC""", (month_like,))]
    runs = [[r["run_time"][:10], r["run_time"], r["feed_subreddit"], r["http_status"],
             r["entries_seen"], r["relevant"], r["needs_review"], r["excluded"],
             _run_coverage(r), r["scan_window_start"], r["scan_window_end"], r["notes"]]
            for r in conn.execute("SELECT * FROM runs WHERE run_time LIKE ? ORDER BY id DESC", (month_like,))]
    placements = [[r["run_time"][:10], r["run_time"], r["observed_at"], r["subreddit"], r["feed_type"],
                   r["rank"], r["change_status"], r["post_id"], r["title"], r["permalink"],
                   r["http_status"], "Yes" if r["copied_from_cache"] else "No"]
                  for r in conn.execute(
                      """SELECT s.run_time,s.observed_at,s.subreddit,s.feed_type,s.http_status,s.copied_from_cache,
                                i.rank,i.change_status,i.post_id,i.title,i.permalink
                         FROM feed_snapshots s JOIN feed_snapshot_items i ON i.snapshot_id=s.id
                         WHERE s.run_time LIKE ? ORDER BY s.id DESC,i.rank""", (month_like,))]
    all_posts = [[p["first_detected_run"][:10], p["first_detected_run"], p["last_seen_at"], p["post_id"],
                  p["subreddit"], p["classification"].upper(), p["title"], _short(p["description"]),
                  p["permalink"], p["post_time"], p["topic"], p["review_reason"], p["score"],
                  p["matched_terms"], p["engagement"], p["signal_evidence"],
                  comment_counts.get(p["post_id"], 0)] for p in posts]
    method = [
        ["NEW coverage", "OK means the fetched NEW feed covers the stored checkpoint; GAP means it cannot be proved."],
        ["Ranking snapshots", "HOT, RISING and TOP have no NEW checkpoint. Successful HTTP 200/304 is labelled Ranking snapshot collected."],
        ["BEST feed", "BEST is a personalised home ranking, so it is not used as a comparable subreddit signal."],
        ["Comment priority", "Every relevant or review post under 24h first; then rising (under 72h), HOT/TOP DAY top 20 and TOP WEEK top 10 (under 7d). Fresh posts are oldest first."],
        ["Request caps", "At most %s comment requests/run and %s/day. Others remain pending while under 24h." %
         (cfg.get("comment_max_posts_per_run", 12), cfg.get("comment_max_requests_per_day", 48))],
        ["Request gap", "%s seconds minimum between all Reddit requests; HTTP 429/403 triggers persistent cooldown." %
         cfg.get("minimum_request_delay_seconds", 120)],
        ["Comment completeness", "RSS saves a bounded public sample, never a verified complete comment thread. Empty samples may be retried once the post ranks."],
        ["Engagement", "HOT/RISING/TOP rank is placement only. Votes and total comment count are not verified."],
        ["Missing images", "A portfolio or allocation question with unseen crucial image data is flagged; do not draft advice from commenters' guesses."],
        ["Storage", "This monthly workbook is the scraper source file. SQLite and hidden .data preserve machine records."],
    ]
    reply_headers = ["First Seen Date", "First Seen Run", "Post ID", "Subreddit", "Title", "Captured OP Text", "Post Link", "Published UTC", "Engagement Label", "Feed Evidence", "Last Observed Feed Placement", "Source Check", "Reply Window", "Latest Run Tag", "Comment Status", "Saved Comments", "Comments Fetched UTC", "Comment Selection Basis", "Comment Excerpts", "Coverage Limit", "Classification", "Filter Reason", "Reply Review State"]
    sheets = [
        {"name": "Start Here", "headers": ["Item", "Value", "Meaning"], "rows": start},
        {"name": "Latest Replies", "headers": reply_headers, "rows": latest, "hyperlink_col": 7},
        {"name": "Reply Candidates", "headers": reply_headers, "rows": reply, "hyperlink_col": 7},
        {"name": "Content by Day", "headers": ["Date", "First Seen Run", "Last Seen Run", "Post ID", "Subreddit", "Title", "Captured OP Text", "Post Link", "Published UTC", "Topic", "Engagement Label", "Feed Evidence", "Feeds Seen", "Saved Comments", "Comments Fetched UTC", "Comment Excerpts", "Coverage Limit", "Classification", "Filter Reason"], "rows": content, "hyperlink_col": 8},
        {"name": "Comments", "headers": ["Saved Date", "Saved UTC", "Post ID", "Subreddit", "Post Title", "Comment ID", "Published UTC", "Comment Link", "Comment Text"], "rows": comments, "hyperlink_col": 8},
        {"name": "Comment Requests", "headers": ["Date", "Attempt UTC", "Post ID", "Subreddit", "Post Title", "Post Link", "Selection Basis", "HTTP Status", "Result"], "rows": requests, "hyperlink_col": 6},
        {"name": "Runs", "headers": ["Date", "Run Time Local", "Feed / Subreddit", "HTTP Status", "Entries Seen", "Relevant", "Needs Review", "Excluded", "Coverage Explained", "Scan Start UTC", "Scan End UTC", "Notes"], "rows": runs},
        {"name": "Feed Placements", "headers": ["Date", "Run Time Local", "Observed UTC", "Subreddit", "Feed", "Rank", "Change", "Post ID", "Title", "Post Link", "HTTP Status", "Copied From Cache"], "rows": placements, "hyperlink_col": 10},
        {"name": "All Posts", "headers": ["First Seen Date", "First Seen Run", "Last Seen UTC", "Post ID", "Subreddit", "Current Classification", "Title", "Captured OP Text", "Post Link", "Published UTC", "Topic", "Review Reason", "Filter Score", "Matched Terms", "Engagement Label", "Feed Evidence", "Saved Comments"], "rows": all_posts, "hyperlink_col": 9},
        {"name": "Method", "headers": ["Term", "Meaning"], "rows": method},
    ]
    path = write_xlsx(output_dir / (FILE_NAME % month), sheets)
    validate_xlsx(path, SHEET_NAMES)
    return path
