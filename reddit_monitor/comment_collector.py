"""Bounded, sequential collection of public per-thread comment feeds."""

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlencode, urlparse

from . import db
from .feed_parser import ATOM, canonical_permalink, html_to_text, normalize_timestamp
from .http_client import CooldownActive
from .review_export import _image_only_allocation_risk


class _MarkdownBody(HTMLParser):
    """Prefer Reddit's comment body over surrounding feed HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if self.depth and tag not in ("br", "hr", "img", "input", "meta", "link", "wbr"):
            self.depth += 1
        elif tag == "div" and "md" in dict(attrs).get("class", "").split():
            self.depth = 1

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def parse_comment_feed(xml_bytes, post_id):
    root = ET.fromstring(xml_bytes)
    if root.tag != ATOM + "feed":
        raise ValueError("Expected an Atom comment feed")
    comments = []
    seen = set()
    for entry in root.findall(ATOM + "entry"):
        raw_id = entry.findtext(ATOM + "id") or ""
        link = next((el.attrib.get("href", "") for el in entry.findall(ATOM + "link")
                     if el.attrib.get("rel", "alternate") in ("alternate", "")), "")
        canonical = canonical_permalink(link)
        match = re.search(r"/comments/([A-Za-z0-9]+)/[^/]+/([A-Za-z0-9]+)$", urlparse(canonical).path)
        id_match = re.search(r"t1_([A-Za-z0-9]+)", raw_id)
        if not match or match.group(1).lower() != post_id.lower():
            continue
        comment_id = (id_match.group(1) if id_match else match.group(2)).lower()
        if comment_id in seen:
            continue
        seen.add(comment_id)
        content = entry.findtext(ATOM + "content") or entry.findtext(ATOM + "summary") or ""
        extractor = _MarkdownBody()
        extractor.feed(content)
        body = " ".join(" ".join(extractor.parts).split()) or html_to_text(content)
        if not body:
            continue
        author = entry.findtext(ATOM + "author/" + ATOM + "name") or ""
        if author.lower() == "automoderator" or "i am a bot, and this action was performed automatically" in body.lower():
            continue
        body = re.sub(r"(?<!\w)u/[A-Za-z0-9_-]+", "[user]", body)
        published = entry.findtext(ATOM + "published") or entry.findtext(ATOM + "updated") or ""
        comments.append({
            "comment_id": comment_id,
            "permalink": canonical,
            "body": body[:3000],
            "published_at": normalize_timestamp(published) if published else None,
        })
    return comments


def _iso_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def select_comment_candidates(conn, cfg, report_date):
    """Prioritise one public comment sample for fresh finance posts, then active rankings.

    Relevant and review-classified posts from any feed receive a slot while under
    24 hours old. Older ranked posts are content candidates when they are still
    rising or near the top of HOT/TOP. Saved nonempty samples are not refetched.
    A zero-comment sample may be retried after six hours if the post has since
    appeared in an active ranking feed. Global pacing and run/day caps remain in force.
    """
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        """SELECT s.run_time,s.observed_at,s.subreddit,s.feed_type,i.post_id,i.rank,
                  i.change_status,p.post_time,p.classification,p.description,p.title,
                  p.permalink,p.first_detected_run
           FROM feed_snapshots s JOIN feed_snapshot_items i ON i.snapshot_id=s.id
           JOIN posts p ON p.post_id=i.post_id
           WHERE s.feed_type IN ('new','hot','rising','top_day','top_week')
             AND p.classification IN ('relevant','review')
             AND (s.feed_type='new' OR s.id=(
                 SELECT MAX(s2.id) FROM feed_snapshots s2
                 WHERE s2.subreddit=s.subreddit AND s2.feed_type=s.feed_type
                   AND s2.http_status=200))
           ORDER BY s.id DESC,i.rank""").fetchall()
    by_post = {}
    active_ranked = {}
    for row in rows:
        pid = row["post_id"]
        if row["feed_type"] in ("hot", "rising", "top_day", "top_week"):
            active_ranked.setdefault(pid, row)
        try:
            age = (now - datetime.fromisoformat(row["post_time"].replace("Z", "+00:00"))).total_seconds()
        except (TypeError, ValueError):
            continue
        if age < 0:
            continue
        feed, rank = row["feed_type"], row["rank"]
        if age < 24 * 3600:
            tier = 0
        elif feed == "rising" and age < 72 * 3600:
            tier = 1
        elif feed in ("hot", "top_day") and rank <= 20 and age < 7 * 86400:
            tier = 2
        elif feed == "top_week" and rank <= 10 and age < 7 * 86400:
            tier = 3
        else:
            continue
        saved = by_post.get(pid)
        if saved is None or (tier, rank) < (saved[0], saved[1]):
            by_post[pid] = (tier, rank, row)
    selected = []
    for pid, (tier, rank, row) in by_post.items():
        state = db.comment_state_for_post(conn, pid)
        if state and state["last_success_at"]:
            count = conn.execute("SELECT COUNT(*) FROM post_comments WHERE post_id=?", (pid,)).fetchone()[0]
            if count:
                continue
            if tier == 0 and pid not in active_ranked:
                continue
            last = datetime.fromisoformat(state["last_success_at"].replace("Z", "+00:00"))
            if (now - last).total_seconds() < 6 * 3600:
                continue
            if tier == 0:
                tier = 1  # Still timely, but first samples of fresh posts come first.
                row = active_ranked[pid]
                rank = row["rank"]
        reason = "%s %s post; %s rank %s; %s" % (
            row["classification"].capitalize(),
            "under 24h" if tier == 0 else "active ranking",
            row["feed_type"].upper().replace("_", " "), rank,
            "retry after empty sample" if state and state["last_success_at"] else "first comment sample",
        )
        if _image_only_allocation_risk(row):
            reason += "; source image needed before any reply"
        selected.append((tier, row["post_time"], rank, row["first_detected_run"], pid, row, reason))
    # Fresh posts are served oldest first so they do not age out. Ranked posts
    # are served by tier and position only after the fresh queue.
    selected.sort(key=lambda x: (x[0], x[1] if x[0] == 0 else "", x[2], x[3], x[4]))
    return [(row, reason) for _, _, _, _, _, row, reason in selected]


def collect_comments(conn, http, cfg, report_date, progress=print, max_posts=None, force=False):
    """Fetch selected comment RSS through the shared delay and cooldown governor."""
    candidates = select_comment_candidates(conn, cfg, report_date)
    per_run = min(int(max_posts or cfg.get("comment_max_posts_per_run", 12)), len(candidates))
    per_day = int(cfg.get("comment_max_requests_per_day", 48))
    refresh_hours = float(cfg.get("comment_refresh_hours", 6))
    fetched = 0
    stopped = False
    for row, reason in candidates:
        if fetched >= per_run or db.comment_fetch_count_for_day(conn, report_date) >= per_day:
            break
        state = db.comment_state_for_post(conn, row["post_id"])
        if state and state["last_attempt_at"] and not force:
            last = datetime.fromisoformat(state["last_attempt_at"].replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - last).total_seconds() < refresh_hours * 3600:
                continue
        url = "https://www.reddit.com/r/%s/comments/%s/.rss?%s" % (
            row["subreddit"], row["post_id"], urlencode({"limit": int(cfg.get("comment_feed_limit", 50))}),
        )
        conditional = {"etag": state["etag"], "last_modified": state["last_modified"]} if state else {}
        progress("Comments: checking %s in r/%s..." % (row["post_id"], row["subreddit"]))
        try:
            result = http.fetch(url, conditional=conditional, progress=progress)
        except CooldownActive as exc:
            progress("Comments paused by global cooldown until %s." % exc.until)
            stopped = True
            break
        attempted_at = _iso_now()
        fetched += 1
        if result.stopped:
            db.save_comment_fetch(conn, report_date, row["post_id"], attempted_at, result.status,
                                  "Global cooldown until %s" % result.cooldown_until, selection_reason=reason)
            progress("Comments stopped after HTTP %s." % result.status)
            stopped = True
            break
        if result.status == 304 and state and state["last_success_at"]:
            db.save_comment_fetch(conn, report_date, row["post_id"], attempted_at, 304, "Not modified", selection_reason=reason)
            progress("  Unchanged; saved comment sample retained.")
            continue
        if result.status == 200:
            try:
                comments = parse_comment_feed(result.body, row["post_id"])
            except (ET.ParseError, ValueError) as exc:
                db.save_comment_fetch(conn, report_date, row["post_id"], attempted_at, 422,
                                      "Parse error: %s" % exc, selection_reason=reason)
                progress("  Comment feed could not be parsed: %s" % exc)
                continue
            etag = result.headers.get("ETag") or result.headers.get("Etag") or result.headers.get("etag")
            modified = result.headers.get("Last-Modified") or result.headers.get("last-modified")
            db.save_comment_fetch(conn, report_date, row["post_id"], attempted_at, 200,
                                  "RSS sample; completeness not verified", comments, etag, modified, selection_reason=reason)
            progress("  %d comments saved (RSS coverage not verified)." % len(comments))
        else:
            db.save_comment_fetch(conn, report_date, row["post_id"], attempted_at, result.status,
                                  result.error or "HTTP response did not produce a usable comment feed", selection_reason=reason)
            progress("  Comments unavailable (HTTP %s)." % result.status)
    return {"requested": fetched, "stopped": stopped, "candidates": len(candidates)}
