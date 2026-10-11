import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from reddit_monitor import db
from reddit_monitor.comment_collector import collect_comments, parse_comment_feed
from reddit_monitor.models import FeedEntry, FetchResult, FilterResult
from reddit_monitor.review_export import generate_review_exports


def comment_atom(post_id="abc123", comment_id="def456"):
    return ('''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>t3_abc123</id><title>Post</title>
<link href="https://www.reddit.com/r/Test/comments/abc123/sample/" /></entry>
<entry><id>t1_%s</id><published>2026-09-23T11:00:00Z</published>
<link href="https://www.reddit.com/r/Test/comments/%s/sample/%s/" />
<content type="html">&lt;div class="md"&gt;&lt;p&gt;Consider the exit load &amp;amp; tax.&lt;/p&gt;&lt;/div&gt;</content>
</entry></feed>''' % (comment_id, post_id, comment_id)).encode("utf-8")


class FakeHTTP:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def fetch(self, url, conditional=None, progress=None):
        self.calls.append((url, conditional))
        return self.response


class CommentCollectionTests(unittest.TestCase):
    def test_parser_skips_post_entry_and_extracts_comment_body(self):
        rows = parse_comment_feed(comment_atom(), "abc123")
        self.assertEqual(1, len(rows))
        self.assertEqual("def456", rows[0]["comment_id"])
        self.assertEqual("Consider the exit load & tax.", rows[0]["body"])

    def test_parser_excludes_automated_moderation_comment(self):
        feed = comment_atom().replace(b"<entry><id>t1_", b"<entry><author><name>AutoModerator</name></author><id>t1_")
        self.assertEqual([], parse_comment_feed(feed, "abc123"))

    def test_bounded_collection_persists_and_exports_comments(self):
        with tempfile.TemporaryDirectory() as root:
            conn = db.connect(Path(root) / "state.sqlite3")
            db.init_db(conn)
            now = datetime.now(timezone.utc)
            observed = now.isoformat().replace("+00:00", "Z")
            run_time = now.astimezone().isoformat(timespec="seconds")
            report_date = now.astimezone().date().isoformat()
            entry = FeedEntry("abc123", "Test", "Should I switch mutual funds?",
                              "Investment choice", "https://www.reddit.com/r/Test/comments/abc123/sample",
                              observed)
            db.upsert_post(conn, entry, FilterResult("relevant", "fund selection", 10),
                           run_time, observed)
            db.record_feed_snapshot(conn, run_time, observed, "Test", "new", [entry])
            http = FakeHTTP(FetchResult(200, comment_atom(), {"ETag": "abc"}, "unused"))
            cfg = {"comment_max_posts_per_run": 10, "comment_max_requests_per_day": 30,
                   "comment_refresh_hours": 6, "comment_feed_limit": 50}
            result = collect_comments(conn, http, cfg, report_date, progress=lambda _: None)
            self.assertEqual(1, result["requested"])
            self.assertEqual(1, len(db.comments_for_post(conn, "abc123")))
            self.assertEqual(1, db.comment_fetch_count_for_day(conn, report_date))
            self.assertEqual(0, collect_comments(conn, http, cfg, report_date,
                                                 progress=lambda _: None)["requested"])
            exported = generate_review_exports(conn, Path(root), report_date)
            work = Path(exported["latest_batch_text"]).read_text(encoding="utf-8")
            self.assertIn("Consider the exit load & tax.", work)
            self.assertIn("RSS coverage not verified", work)
            conn.close()


if __name__ == "__main__":
    unittest.main()
