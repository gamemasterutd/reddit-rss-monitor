import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reddit_monitor import db
from reddit_monitor.comment_collector import select_comment_candidates
from reddit_monitor.models import FeedEntry, FilterResult
from reddit_monitor.reports import generate_reports
from reddit_monitor.review_workbook import SHEET_NAMES
from reddit_monitor.xlsx_writer import validate_xlsx


class UnifiedReviewTests(unittest.TestCase):
    def test_one_visible_workbook_preserves_machine_exports_and_explains_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / "output"
            out.mkdir()
            (out / "Reddit-old-note.txt").write_text("keep this")
            conn = db.connect(root / "state.sqlite3")
            db.init_db(conn)
            now = datetime.now(timezone.utc)
            stamp = now.isoformat().replace("+00:00", "Z")
            run = now.astimezone().isoformat(timespec="seconds")
            item = FeedEntry("abc123", "Test", "Should I keep this mutual fund?",
                             "I invest ₹5000 a month", "https://www.reddit.com/r/Test/comments/abc123/post", stamp)
            db.upsert_post(conn, item, FilterResult("relevant", "Funds", 7), run, stamp)
            db.record_feed_snapshot(conn, run, stamp, "Test", "new", [item])
            db.mark_observation_day(conn, now.astimezone().date().isoformat(), "abc123", run, "new")
            prior = now - timedelta(days=40)
            prior_stamp = prior.isoformat().replace("+00:00", "Z")
            prior_run = prior.astimezone().isoformat(timespec="seconds")
            older = FeedEntry("older", "Test", "Older mutual fund post",
                              "Prior month source", "https://www.reddit.com/r/Test/comments/older/post", prior_stamp)
            db.upsert_post(conn, older, FilterResult("relevant", "Funds", 7), prior_run, prior_stamp)
            db.mark_observation_day(conn, prior.astimezone().date().isoformat(), "older", prior_run, "new")
            db.insert_run(conn, run, "hot/Test", "", stamp, 200, 1, 1, 0, 0, "N/A")
            db.save_comment_fetch(conn, now.astimezone().date().isoformat(), "abc123", stamp, 200,
                                  "RSS sample", [], selection_reason="New relevant NEW-feed post")
            reports = generate_reports(conn, out, now.astimezone().date().isoformat(),
                                       organized=True, cfg={"minimum_request_delay_seconds": 120})
            review = Path(reports["review_workbook"])
            self.assertEqual(["Reddit-Stream-%s.xlsx" % now.astimezone().strftime("%Y-%m")],
                             sorted(p.name for p in out.glob("Reddit-*")))
            self.assertTrue((out / ".data" / "Reddit-old-note.txt").is_file())
            self.assertTrue(Path(reports["latest_batch_json"]).is_file())
            summary = validate_xlsx(review)
            self.assertEqual(list(SHEET_NAMES), summary["sheet_names"])
            self.assertEqual(1, summary["row_counts"][1])  # latest run
            self.assertEqual(1, summary["row_counts"][2])  # monthly reply archive
            self.assertEqual(1, summary["row_counts"][8])  # all posts
            with zipfile.ZipFile(review) as archive:
                xml = archive.read("xl/worksheets/sheet7.xml").decode()
                self.assertIn("Ranking snapshot collected", xml)
                latest = archive.read("xl/worksheets/sheet2.xml").decode()
                self.assertIn("LATEST RUN", latest)
                self.assertIn("Empty RSS sample", latest)
                self.assertIn("HUMAN REVIEW: no saved comments", latest)
                requests = archive.read("xl/worksheets/sheet6.xml").decode()
                self.assertIn("New relevant NEW-feed post", requests)
            conn.close()

    def test_every_fresh_relevant_new_or_hot_post_gets_one_comment_slot(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = db.connect(Path(temp) / "state.sqlite3")
            db.init_db(conn)
            now = datetime.now(timezone.utc)
            stamp = now.isoformat().replace("+00:00", "Z")
            run = now.astimezone().isoformat(timespec="seconds")
            new_posts, hot_posts = [], []
            for i in range(15):
                entry = FeedEntry("post%d" % i, "Test", "Should I keep this mutual fund %d?" % i,
                                  "I invest ₹5000 a month", "https://www.reddit.com/r/Test/comments/post%d/post" % i, stamp)
                db.upsert_post(conn, entry, FilterResult("relevant", "Funds", 7), run, stamp)
                (new_posts if i < 12 else hot_posts).append(entry)
            db.record_feed_snapshot(conn, run, stamp, "Test", "new", new_posts)
            db.record_feed_snapshot(conn, run, stamp, "Test", "hot", hot_posts)
            chosen = select_comment_candidates(conn, {}, now.astimezone().date().isoformat())
            self.assertEqual(15, len(chosen))
            self.assertEqual(15, len({row["post_id"] for row, _ in chosen}))
            self.assertTrue(any(row["post_id"] == "post14" for row, _ in chosen))
            db.save_comment_fetch(conn, now.astimezone().date().isoformat(), "post14", stamp,
                                  200, "RSS sample", [])
            self.assertEqual(14, len(select_comment_candidates(conn, {}, now.astimezone().date().isoformat())))
            older_attempt = (now - timedelta(hours=7)).isoformat().replace("+00:00", "Z")
            db.save_comment_fetch(conn, now.astimezone().date().isoformat(), "post14", older_attempt,
                                  200, "RSS sample", [])
            selected = select_comment_candidates(conn, {}, now.astimezone().date().isoformat())
            self.assertIn("post14", [row["post_id"] for row, _ in selected])
            conn.close()


if __name__ == "__main__":
    unittest.main()
