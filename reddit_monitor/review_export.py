"""Small, explicit review files built from the durable feed snapshot ledger."""

import csv
import json
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import db


SNAPSHOT_HEADERS = [
    "Observed At UTC", "Run Time Local", "Subreddit", "Feed", "Rank",
    "Change Since Previous Snapshot", "Post ID", "Published UTC", "Classification",
    "Topic", "Title", "Reddit Link", "HTTP Status", "Copied From Cache",
]


def _parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _record(row):
    return {
        "post_id": row["post_id"],
        "subreddit": row["subreddit"],
        "title": row["title"],
        "published_utc": row["post_time"],
        "observed_utc": row["observed_at"],
        "feed": row["feed_type"],
        "rank": row["rank"],
        "change": row["change_status"],
        "classification": (row["classification"] or "not_evaluated").upper(),
        "topic": row["topic"] or "",
        "description": row["description"] or "",
        "engagement": row["engagement"] or "UNKNOWN",
        "ranking_evidence": row["signal_evidence"] or "",
        "url": row["permalink"],
        "comments_status": "not_collected",
    }


def _eligible(row):
    return row["classification"] in ("relevant", "review")


def _age_hours(row):
    return (_parse_time(row["observed_at"]) - _parse_time(row["post_time"])).total_seconds() / 3600


def _recurring_thread(row):
    title = row["title"].lower()
    return bool(re.search(r"\b(?:monthly|weekly|bi.weekly)\b.*\b(?:thread|discussion|self.promotion)\b", title))


def _reply_priority(row):
    age = _age_hours(row)
    freshness = 10 if age <= 3 else 8 if age <= 6 else 6 if age <= 12 else 3 if age <= 24 else 1
    hot_rank = row["rank"] if row["feed_type"] == "hot" else None
    hot_bonus = 4 if hot_rank is not None and hot_rank <= 5 else 2 if hot_rank is not None else 0
    return freshness + hot_bonus + (2 if row["classification"] == "relevant" else 0) + min(float(row["score"] or 0) / 4, 2)


def _reply_shortlist(rows, limit=10, per_subreddit=4):
    by_post = {}
    for row in rows:
        if _recurring_thread(row) or _image_only_allocation_risk(row):
            continue
        old = by_post.get(row["post_id"])
        if old is None or _reply_priority(row) > _reply_priority(old):
            by_post[row["post_id"]] = row
    sorted_rows = sorted(by_post.values(), key=lambda r: (_reply_priority(r), r["post_time"]), reverse=True)
    counts = {}
    selected = []
    for row in sorted_rows:
        sub = row["subreddit"].lower()
        if counts.get(sub, 0) >= per_subreddit:
            continue
        counts[sub] = counts.get(sub, 0) + 1
        selected.append(_record(row))
        if len(selected) >= limit:
            break
    return selected


def select_reply_shortlist(conn, report_date):
    """The same ten candidates drive comment collection and the review file."""
    day_rows = list(db.snapshot_items_for_day(conn, report_date))
    latest_hot = list(db.latest_snapshot_items(conn, report_date, "hot"))
    new_rows = [r for r in day_rows if r["feed_type"] == "new" and _eligible(r)
                and r["change_status"] in ("BASELINE", "ENTERED", "RETURNED")
                and 0 <= _age_hours(r) <= 24 and not _recurring_thread(r)]
    hot_rows = [r for r in latest_hot if r["rank"] <= 10 and _eligible(r)
                and 0 <= _age_hours(r) <= 36 and not _recurring_thread(r)]
    return _reply_shortlist(hot_rows + new_rows)


def _attach_comments(conn, records):
    for record in records:
        state = db.comment_state_for_post(conn, record["post_id"])
        if not state or not state["last_success_at"]:
            record["comments_status"] = "not_collected" if not state else "unavailable"
            record["comments_fetched_at_utc"] = None
            record["comments_collected"] = []
            record["comment_count_collected"] = 0
            continue
        comments = list(db.comments_for_post(conn, record["post_id"]))
        record["comments_status"] = "sample_collected" if state["last_status"] in (200, 304) else "stale_sample"
        record["comments_fetched_at_utc"] = state["last_success_at"]
        record["comment_count_collected"] = len(comments)
        record["comments_collected"] = [{"id": c["comment_id"], "body": c["body"],
                                          "url": c["permalink"], "published_utc": c["published_at"]}
                                         for c in comments[:50]]
    return records


def _unique(rows, limit):
    seen = set()
    out = []
    for row in rows:
        if row["post_id"] not in seen:
            seen.add(row["post_id"])
            out.append(_record(row))
        if len(out) >= limit:
            break
    return out


def _copy_latest(source, latest):
    tmp = latest.with_suffix(latest.suffix + ".tmp")
    shutil.copyfile(source, tmp)
    tmp.replace(latest)


def _line(row):
    title = " ".join(row["title"].split())
    if len(title) > 130:
        title = title[:127] + "..."
    observed = _parse_time(row["observed_utc"]).astimezone().strftime("%d %b %H:%M %Z")
    published = _parse_time(row["published_utc"]).astimezone().strftime("%d %b %H:%M %Z")
    return "- **r/%s · %s #%s · %s** [%s](%s) — %s; seen %s; posted %s" % (
        row["subreddit"], row["feed"].upper(), row["rank"], row["change"],
        title, row["url"], row["topic"] or row["classification"],
        observed, published,
    )


def _section(lines, heading, rows):
    lines.extend(["## " + heading, ""])
    if rows:
        lines.extend(_line(row) for row in rows)
    else:
        lines.append("No qualifying posts in the stored snapshots.")
    lines.append("")


def _recent_history(output_dir, report_date, days=6, limit=180):
    """Compact earlier selected posts for a single-file rolling Work handoff."""
    today = date.fromisoformat(report_date)
    seen = set()
    history = []
    for offset in range(1, days + 1):
        day = (today - timedelta(days=offset)).isoformat()
        path = output_dir / ("Reddit-Work-Batch-%s.json" % day)
        if not path.is_file():
            continue
        try:
            older = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for key in ("new_posts_last_24h", "hot_top_10_latest", "content_pool"):
            for row in older.get(key, []):
                post_id = row.get("post_id")
                if not post_id or post_id in seen:
                    continue
                seen.add(post_id)
                history.append({
                    "batch_date": day, "post_id": post_id,
                    "subreddit": row.get("subreddit", ""), "title": row.get("title", ""),
                    "topic": row.get("topic", ""), "published_utc": row.get("published_utc", ""),
                    "url": row.get("url", ""),
                })
                if len(history) >= limit:
                    return history
    return history


def _image_only_allocation_risk(row):
    """A fund-allocation review needs itemised source data, not comment guesses."""
    title = row["title"].lower()
    body = row["description"].lower()
    if not any(word in title for word in ("portfolio", "allocation", "fund review", "funds review")):
        return False
    amount_tokens = re.findall(r"(?:₹|rs\.?|inr)\s*[\d,]+(?:\.\d+)?\s*(?:k|lakh|lac|cr)?", body)
    allocation_percentages = re.findall(r"\b\d+(?:\.\d+)?\s*%", body)
    return len(amount_tokens) < 2 and len(allocation_percentages) < 2


def _work_post_lines(row, include_comments=False):
    description = " ".join(row["description"].split())[:1500] or "(No usable body in feed)"
    lines = [
        "Post ID: " + row["post_id"],
        "Subreddit: r/" + row["subreddit"],
        "Title: " + row["title"],
        "Published UTC: " + row["published_utc"],
        "Observed UTC: " + row["observed_utc"],
        "Feed/rank/change: %s / %s / %s" % (row["feed"], row["rank"], row["change"]),
        "Classification/topic: %s / %s" % (row["classification"], row["topic"]),
        "Ranking evidence: " + row["ranking_evidence"],
        "Comments: %s; %d saved; fetched %s; RSS coverage not verified" % (
            row["comments_status"], row["comment_count_collected"],
            row["comments_fetched_at_utc"] or "never"),
        "Body excerpt: " + description,
        "Source completeness: " + (
            "INCOMPLETE ALLOCATION; fund-by-fund amounts are not in captured text. "
            "Reject the reply candidate. Comment descriptions cannot supply the missing source."
            if _image_only_allocation_risk(row) else "Text may still need source verification."),
        "URL: " + row["url"],
    ]
    if include_comments:
        for comment in row["comments_collected"]:
            lines.append("Comment %s | %s | %s" % (
                comment["id"], " ".join(comment["body"].split())[:1000], comment["url"]))
    lines.append("")
    return lines


def _write_work_file(path, lines):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def generate_review_exports(conn, output_dir, report_date):
    output_dir = Path(output_dir)
    day_rows = list(db.snapshot_items_for_day(conn, report_date))
    latest_hot = list(db.latest_snapshot_items(conn, report_date, "hot"))

    new_rows = [r for r in day_rows if r["feed_type"] == "new" and _eligible(r)
                and r["change_status"] in ("BASELINE", "ENTERED", "RETURNED")
                and 0 <= _age_hours(r) <= 24 and not _recurring_thread(r)]
    new_rows.sort(key=lambda r: r["post_time"], reverse=True)
    hot_rows = [r for r in latest_hot if r["rank"] <= 10 and _eligible(r)
                and 0 <= _age_hours(r) <= 168 and not _recurring_thread(r)]
    hot_rows.sort(key=lambda r: (r["subreddit"].lower(), r["rank"]))
    entered_hot = [r for r in day_rows if r["feed_type"] == "hot" and _eligible(r)
                   and r["change_status"] in ("ENTERED", "RETURNED")]
    entered_hot.sort(key=lambda r: r["observed_at"], reverse=True)

    new_posts = _unique(new_rows, 30)
    # Give the editor a compact index of every newly observed NEW-feed post in
    # this review cycle, including locally excluded posts. This uses stored
    # snapshots only: it makes no extra Reddit request and costs no scrape quota.
    latest_new_at = max((_parse_time(r["observed_at"]) for r in day_rows
                         if r["feed_type"] == "new"), default=None)
    discovery_rows = []
    if latest_new_at is not None:
        discovery_start = latest_new_at - timedelta(hours=4)
        discovery_rows = [r for r in day_rows if r["feed_type"] == "new"
                          and r["change_status"] in ("BASELINE", "ENTERED", "RETURNED")
                          and _parse_time(r["observed_at"]) >= discovery_start]
        discovery_rows.sort(key=lambda r: (r["observed_at"], r["post_time"]), reverse=True)
    new_discovery = _unique(discovery_rows, len(discovery_rows))
    hot_now = [_record(r) for r in hot_rows]
    hot_entrants = _unique(entered_hot, 20)
    reply_shortlist = select_reply_shortlist(conn, report_date)
    relevant_day = [r for r in day_rows if r["classification"] == "relevant"
                    and 0 <= _age_hours(r) <= 168 and not _recurring_thread(r)]
    relevant_day.sort(key=lambda r: (r["feed_type"] != "hot", -_reply_priority(r), r["rank"]))
    content_pool = _unique(relevant_day, 25)
    for selected in (new_posts, hot_now, hot_entrants, reply_shortlist, content_pool,
                     new_discovery):
        _attach_comments(conn, selected)
    recent_history = _recent_history(output_dir, report_date)
    run_rows = list(db.runs_for_day(conn, report_date))
    last_observed_utc = max((r["observed_at"] for r in day_rows), default=None)
    issues = [{"run_time": r["run_time"], "feed": r["feed_subreddit"],
               "status": r["coverage_status"], "http_status": r["http_status"],
               "notes": r["notes"]}
              for r in run_rows if r["coverage_status"] in ("GAP", "ERROR", "STOPPED", "SKIPPED")]
    if not run_rows:
        issues.append({"run_time": "", "feed": "GLOBAL", "status": "NO_RUN",
                       "http_status": "", "notes": "No collector run recorded for this date"})
    batch = {
        "schema_version": 1,
        "report_date_local": report_date,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "last_run_local": run_rows[-1]["run_time"] if run_rows else None,
        "last_successful_feed_response_utc": last_observed_utc,
        "interpretation": "Review lists filter out recurring threads, incomplete fund-allocation posts and posts older than 7 days. HOT rank is feed placement, not a verified vote or comment count. Comment RSS samples are collected only for shortlisted posts and may be incomplete; check the live thread before posting. BASELINE means first stored snapshot, not newly posted on Reddit. The snapshot CSV retains every observed feed item.",
        "new_posts_last_24h": new_posts,
        "new_discovery_since_last_cycle": new_discovery,
        "hot_top_10_latest": hot_now,
        "entered_hot_today": hot_entrants,
        "reply_shortlist": reply_shortlist,
        "content_pool": content_pool,
        "recent_post_history_7d": recent_history,
        "collection_issues": issues,
    }

    stem = "Reddit-Review-%s" % report_date
    brief = output_dir / (stem + ".md")
    batch_path = output_dir / ("Reddit-Work-Batch-%s.json" % report_date)
    batch_text = output_dir / ("Reddit-Work-Batch-%s.txt" % report_date)
    snapshot_csv = output_dir / ("Reddit-Feed-Snapshots-%s.csv" % report_date)
    lines = ["# Reddit review — " + report_date, "",
             "Generated: " + batch["generated_at_utc"],
             "Last collector run: " + (batch["last_run_local"] or "none"), "",
             "Last successful feed response: " + (batch["last_successful_feed_response_utc"] or "none"), "",
             "**Read this first:** HOT means feed rank, not verified votes or comment count. "
             "Comments are sampled only for shortlisted posts; coverage is not verified. BASELINE means this monitor's first stored snapshot. "
             "Review lists omit recurring threads and posts older than 7 days; the snapshot CSV retains all observations.", ""]
    _section(lines, "Newly detected in NEW within 24 hours", new_posts)
    _section(lines, "Recent relevant posts in each subreddit's latest stored HOT top 10", hot_now)
    _section(lines, "Entered or returned to HOT today", hot_entrants)
    _section(lines, "Reply review shortlist — check live comments before posting", reply_shortlist)
    lines.extend(["## Collected comment samples for reply candidates", ""])
    for row in reply_shortlist:
        lines.append("- **%s:** %s; %d comments saved; checked %s" % (
            row["post_id"], row["comments_status"], row["comment_count_collected"],
            row["comments_fetched_at_utc"] or "never"))
        for comment in row["comments_collected"][:5]:
            lines.append("  - %s ([comment](%s))" % (" ".join(comment["body"].split())[:300], comment["url"]))
    lines.append("")
    _section(lines, "Content research pool", content_pool)
    lines.extend(["## Collection issues", ""])
    if issues:
        lines.extend("- %s · %s · %s: %s" % (x["run_time"], x["feed"], x["status"], x["notes"]) for x in issues)
    else:
        lines.append("No recorded coverage or request issue today.")
    lines.append("")
    tmp = brief.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(brief)
    tmp = batch_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(batch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(batch_path)
    work_lines = [
        "REDDIT SIGNALS | " + report_date,
        "Generated UTC: " + batch["generated_at_utc"],
        "Last collector run local: " + (batch["last_run_local"] or "none"),
        "Last successful feed response UTC: " + (batch["last_successful_feed_response_utc"] or "none"),
        batch["interpretation"],
        "Editorial task: verify sources and trends independently; do not infer comment opinions from this file.",
        "",
    ]
    seen = set()
    for label, selected in (
        ("NEW IN LAST 24 HOURS", new_posts),
        ("RECENT RELEVANT POSTS IN HOT TOP 10 NOW", hot_now),
        ("ENTERED HOT TODAY", hot_entrants),
        ("ADDITIONAL CONTENT POOL", content_pool),
    ):
        work_lines.extend([label, ""])
        for row in selected:
            if row["post_id"] in seen:
                continue
            seen.add(row["post_id"])
            description = " ".join(row["description"].split())[:1500] or "(No usable body in feed)"
            work_lines.extend([
                "Post ID: " + row["post_id"],
                "Subreddit: r/" + row["subreddit"],
                "Title: " + row["title"],
                "Published UTC: " + row["published_utc"],
                "Observed UTC: " + row["observed_utc"],
                "Feed/rank/change: %s / %s / %s" % (row["feed"], row["rank"], row["change"]),
                "Classification/topic: %s / %s" % (row["classification"], row["topic"]),
                "Ranking evidence: " + row["ranking_evidence"],
                "Comments: %s; %d saved; fetched %s; RSS coverage not verified" % (
                    row["comments_status"], row["comment_count_collected"],
                    row["comments_fetched_at_utc"] or "never"),
                "Body excerpt: " + description,
                "URL: " + row["url"],
                "",
            ])
            for comment in row["comments_collected"]:
                work_lines.append("Comment %s | %s | %s" % (
                    comment["id"], " ".join(comment["body"].split())[:1000], comment["url"]))
            if row["comments_collected"]:
                work_lines.append("")
    if issues:
        work_lines.extend(["COLLECTION ISSUES", ""])
        work_lines.extend("%s %s %s: %s" % (x["run_time"], x["feed"], x["status"], x["notes"]) for x in issues)
    work_lines.extend(["", "PRIOR SIX DAYS: SELECTED POST HISTORY", "",
                       "This section is selected posts from earlier daily batches, not all subreddit posts. "
                       "Do not use it for subreddit-wide counts or prevalence claims.", ""])
    if recent_history:
        for row in recent_history:
            work_lines.append("%s | %s | r/%s | %s | %s | %s" % (
                row["batch_date"], row["post_id"], row["subreddit"], row["topic"],
                " ".join(row["title"].split())[:160], row["url"],
            ))
    else:
        work_lines.append("No earlier daily batches yet.")
    tmp = batch_text.with_suffix(".txt.tmp")
    tmp.write_text("\n".join(work_lines) + "\n", encoding="utf-8")
    tmp.replace(batch_text)

    handoff_header = [
        "REDDIT SIGNALS | " + report_date,
        "Generated UTC: " + batch["generated_at_utc"],
        "Last collector run local: " + (batch["last_run_local"] or "none"),
        "Last successful feed response UTC: " + (batch["last_successful_feed_response_utc"] or "none"),
        batch["interpretation"], "",
    ]
    reply_lines = handoff_header + [
        "REPLY REVIEW SHORTLIST", "",
        "Review the live thread and subreddit rules before a human posts. Comment samples may be incomplete or stale. "
        "Answer the OP's actual question first. Fund-allocation reviews without itemised source holdings "
        "are excluded from this shortlist. Never infer holdings from commenters or draft a clarification-only "
        "reply. Do not claim complete comment coverage.", "",
    ]
    if reply_shortlist:
        for row in reply_shortlist:
            reply_lines.extend(_work_post_lines(row, include_comments=True))
    else:
        reply_lines.extend(["No ranked reply candidates in the stored snapshots.", ""])
    reply_lines.extend(["ALL NEW-FEED DISCOVERY THIS CYCLE", "",
                        "Compact index of every newly observed NEW-feed post in the latest four-hour cycle, "
                        "including posts excluded by the local topic filter. This is an editorial screen, "
                        "not a request to draft for every post. Only the ranked shortlist above receives "
                        "bounded local comment fetching; other rows may have no comment sample. "
                        "Live comments and rules must be checked for any finalist when accessible.", ""])
    ranked_ids = {row["post_id"] for row in reply_shortlist}
    if new_discovery:
        for row in new_discovery:
            reply_lines.extend([
                "Post ID: %s | r/%s | %s | %s" % (
                    row["post_id"], row["subreddit"], row["classification"], row["title"]),
                "Published UTC: %s | Observed UTC: %s | Feed: %s/%s | Saved comments: %s (%d)" % (
                    row["published_utc"], row["observed_utc"], row["feed"], row["change"],
                    row["comments_status"], row["comment_count_collected"]),
                "Body excerpt: %s" % (
                    "(withheld for reply review: fund-by-fund allocation missing from captured source)"
                    if _image_only_allocation_risk(row)
                    else " ".join(row["description"].split())[:450] or "(No usable body in feed)"),
                "Local reply eligibility: %s" % (
                    "REJECTED — incomplete allocation; source holdings unavailable"
                    if _image_only_allocation_risk(row) else "needs editorial review"),
                "URL: %s | Detailed shortlist above: %s" % (
                    row["url"], "yes" if row["post_id"] in ranked_ids else "no"),
                "",
            ])
    else:
        reply_lines.extend(["No newly observed NEW-feed posts in this cycle.", ""])

    content_lines = handoff_header + [
        "CONTENT RESEARCH", "",
        "Propose a topic only with source post IDs, a distinct editorial lens, a concrete reason the angle adds value, "
        "and the facts that must be checked. HOT placement is not a verified vote count. "
        "The prior six-day section is selected-post history, not a denominator for prevalence claims.", "",
    ]
    content_seen = set()
    for label, selected in (
        ("NEW IN LAST 24 HOURS", new_posts),
        ("RECENT RELEVANT POSTS IN HOT TOP 10 NOW", hot_now),
        ("ENTERED HOT TODAY", hot_entrants),
        ("ADDITIONAL CONTENT POOL", content_pool),
    ):
        content_lines.extend([label, ""])
        found = False
        for row in selected:
            if row["post_id"] in content_seen:
                continue
            content_seen.add(row["post_id"])
            content_lines.extend(_work_post_lines(row))
            found = True
        if not found:
            content_lines.extend(["No additional qualifying posts.", ""])
    content_lines.extend(["PRIOR SIX DAYS: SELECTED POST HISTORY", ""])
    if recent_history:
        for row in recent_history:
            content_lines.append("%s | %s | r/%s | %s | %s | %s" % (
                row["batch_date"], row["post_id"], row["subreddit"], row["topic"],
                " ".join(row["title"].split())[:160], row["url"]))
    else:
        content_lines.append("No earlier daily batches yet.")
    content_lines.append("")
    if issues:
        for lines in (reply_lines, content_lines):
            lines.extend(["COLLECTION ISSUES", ""])
            lines.extend("%s %s %s: %s" % (x["run_time"], x["feed"], x["status"], x["notes"])
                         for x in issues)
            lines.append("")
    latest_reply_text = output_dir / "Reddit-Replies-Latest.txt"
    latest_content_text = output_dir / "Reddit-Content-Latest.txt"
    _write_work_file(latest_reply_text, reply_lines)
    _write_work_file(latest_content_text, content_lines)
    tmp = snapshot_csv.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(SNAPSHOT_HEADERS)
        for row in day_rows:
            writer.writerow([
                row["observed_at"], row["run_time"], row["subreddit"], row["feed_type"].upper(),
                row["rank"], row["change_status"], row["post_id"], row["post_time"],
                (row["classification"] or "not_evaluated").upper(), row["topic"] or "",
                row["title"], row["permalink"], row["http_status"], bool(row["copied_from_cache"]),
            ])
    tmp.replace(snapshot_csv)
    latest_brief = output_dir / "Reddit-Review-Latest.md"
    latest_batch = output_dir / "Reddit-Work-Batch-Latest.json"
    latest_batch_text = output_dir / "Reddit-Work-Batch-Latest.txt"
    latest_snapshots = output_dir / "Reddit-Feed-Snapshots-Latest.csv"
    for source, latest in ((brief, latest_brief), (batch_path, latest_batch),
                           (batch_text, latest_batch_text), (snapshot_csv, latest_snapshots)):
        _copy_latest(source, latest)
    return {
        "review_md": str(brief), "batch_json": str(batch_path), "batch_text": str(batch_text),
        "snapshots_csv": str(snapshot_csv),
        "latest_review_md": str(latest_brief), "latest_batch_json": str(latest_batch),
        "latest_batch_text": str(latest_batch_text),
        "latest_reply_text": str(latest_reply_text),
        "latest_content_text": str(latest_content_text),
        "latest_snapshots_csv": str(latest_snapshots),
    }
