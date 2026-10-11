import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reddit_monitor import db
from reddit_monitor.drive_sync import sync_review_files, upload_review_file_rclone, upload_split_files_rclone
from reddit_monitor.models import FeedEntry, FilterResult
from reddit_monitor.reports import generate_reports


class ReviewExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conn = db.connect(self.root / "state.sqlite3")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _entry(self, post_id, title):
        return FeedEntry(
            post_id, "Test", title, "Full question about mutual funds and fees",
            "https://www.reddit.com/r/Test/comments/%s/question" % post_id,
            "2026-09-23T05:00:00Z",
        )

    def _save(self, entry):
        db.upsert_post(
            self.conn, entry, FilterResult("relevant", "Mutual Funds", 7, ["mutual fund"]),
            "2026-09-23T12:00:00+05:30", "2026-09-23T06:30:00Z",
        )

    def test_feed_snapshots_preserve_rank_changes_and_304(self):
        first = self._entry("first", "First mutual fund question")
        second = self._entry("second", "Second mutual fund question")
        self._save(first)
        self._save(second)
        db.record_feed_snapshot(self.conn, "2026-09-23T12:00:00+05:30", "2026-09-23T06:30:00Z",
                                "Test", "hot", [first])
        db.record_feed_snapshot(self.conn, "2026-09-23T15:00:00+05:30", "2026-09-23T09:30:00Z",
                                "Test", "hot", [second, first])
        db.record_feed_snapshot(self.conn, "2026-09-23T18:00:00+05:30", "2026-09-23T12:30:00Z",
                                "Test", "hot", [], http_status=304)
        rows = db.snapshot_items_for_day(self.conn, "2026-09-23")
        self.assertEqual(["BASELINE", "ENTERED", "STAYED", "STAYED", "STAYED"],
                         [r["change_status"] for r in rows])
        self.assertEqual(["second", "first"],
                         [r["post_id"] for r in db.latest_snapshot_items(self.conn, "2026-09-23", "hot")])

    def test_review_batch_and_drive_staging(self):
        entry = self._entry("first", "Mutual fund fee question")
        self._save(entry)
        db.record_feed_snapshot(self.conn, "2026-09-23T12:00:00+05:30", "2026-09-23T06:30:00Z",
                                "Test", "new", [entry])
        db.record_feed_snapshot(self.conn, "2026-09-23T12:01:00+05:30", "2026-09-23T06:31:00Z",
                                "Test", "hot", [entry])
        reports = generate_reports(self.conn, self.root / "out", "2026-09-23")
        batch = json.loads(Path(reports["batch_json"]).read_text(encoding="utf-8"))
        self.assertEqual("first", batch["new_posts_last_24h"][0]["post_id"])
        self.assertEqual("not_collected", batch["reply_shortlist"][0]["comments_status"])
        self.assertIn("Full question", Path(reports["batch_text"]).read_text(encoding="utf-8"))
        self.assertIn("REPLY REVIEW SHORTLIST", Path(reports["latest_reply_text"]).read_text(encoding="utf-8"))
        self.assertIn("CONTENT RESEARCH", Path(reports["latest_content_text"]).read_text(encoding="utf-8"))
        drive = self.root / "drive"
        drive.mkdir()
        first = sync_review_files(reports, drive)
        second = sync_review_files(reports, drive)
        self.assertEqual(["Reddit-Work-Batch-Latest.txt"], first["copied"])
        self.assertEqual(0, len(second["copied"]))

        binary = self.root / "rclone"
        binary.write_text("", encoding="utf-8")
        binary.chmod(0o755)
        with patch("reddit_monitor.drive_sync.subprocess.run") as run:
            run.return_value.returncode = 0
            uploaded = upload_review_file_rclone(reports, "reddit", binary)
        self.assertEqual("reddit:Reddit RSS Monitor/Reddit-Work-Batch-Latest.txt",
                         uploaded["destination"])
        self.assertEqual("copyto", run.call_args.args[0][1])

        auth_file = self.root / "rclone.conf"
        auth_file.write_text("[reddit]\ntype = drive\n", encoding="utf-8")
        auth_file.chmod(0o600)
        with patch("reddit_monitor.drive_sync.subprocess.run") as run:
            run.return_value.returncode = 0
            uploaded = upload_split_files_rclone(
                reports, "reddit", binary, auth_file,
                {"latest_reply_text": "reply123", "latest_content_text": "content123"})
        self.assertEqual(2, len(uploaded))
        self.assertEqual(2, run.call_count)
        self.assertIn("reply123", run.call_args_list[0].args[0])
        self.assertIn("content123", run.call_args_list[1].args[0])

    def test_reply_handoff_indexes_new_posts_even_if_local_filter_excludes_them(self):
        candidate = self._entry("candidate", "Mutual fund fee question")
        missed = self._entry("missed", "How should I compare these options?")
        self._save(candidate)
        db.upsert_post(
            self.conn, missed, FilterResult("excluded", "Other", 0, []),
            "2026-09-23T12:00:00+05:30", "2026-09-23T06:30:00Z",
        )
        db.record_feed_snapshot(self.conn, "2026-09-23T12:00:00+05:30", "2026-09-23T06:30:00Z",
                                "Test", "new", [candidate, missed])
        reports = generate_reports(self.conn, self.root / "out", "2026-09-23")
        batch = json.loads(Path(reports["batch_json"]).read_text(encoding="utf-8"))
        self.assertEqual({"candidate", "missed"},
                         {r["post_id"] for r in batch["new_discovery_since_last_cycle"]})
        reply_text = Path(reports["latest_reply_text"]).read_text(encoding="utf-8")
        self.assertIn("ALL NEW-FEED DISCOVERY THIS CYCLE", reply_text)
        self.assertIn("Post ID: missed", reply_text)
        self.assertIn("Saved comments: not_collected", reply_text)
        self.assertEqual(["candidate"], [r["post_id"] for r in batch["reply_shortlist"]])

    def test_incomplete_allocation_is_rejected_but_kept_in_discovery(self):
        image_only = FeedEntry(
            "imageonly", "Test", "Please review this fund allocations",
            "Please review my ₹45k SIP for retirement. Details are in the image.",
            "https://www.reddit.com/r/Test/comments/imageonly/review", "2026-09-23T05:00:00Z",
        )
        itemised = FeedEntry(
            "itemised", "Test", "Portfolio allocation review",
            "Fund A ₹10,000, Fund B ₹5,000; should I change the split?",
            "https://www.reddit.com/r/Test/comments/itemised/review", "2026-09-23T05:00:00Z",
        )
        percentage_split = FeedEntry(
            "percentages", "Test", "Portfolio allocation review",
            "Fund A 60%, Fund B 40%; should I change the split?",
            "https://www.reddit.com/r/Test/comments/percentages/review", "2026-09-23T05:00:00Z",
        )
        for entry in (image_only, itemised, percentage_split):
            self._save(entry)
        db.record_feed_snapshot(self.conn, "2026-09-23T12:00:00+05:30", "2026-09-23T06:30:00Z",
                                "Test", "new", [image_only, itemised, percentage_split])
        reports = generate_reports(self.conn, self.root / "out", "2026-09-23")
        batch = json.loads(Path(reports["batch_json"]).read_text(encoding="utf-8"))
        self.assertEqual({"itemised", "percentages"},
                         {r["post_id"] for r in batch["reply_shortlist"]})
        self.assertEqual({"imageonly", "itemised", "percentages"},
                         {r["post_id"] for r in batch["new_discovery_since_last_cycle"]})
        reply_text = Path(reports["latest_reply_text"]).read_text(encoding="utf-8")
        self.assertIn("REJECTED — incomplete allocation; source holdings unavailable", reply_text)

    def test_review_shortlists_exclude_recurring_threads_but_keep_audit(self):
        recurring = self._entry("monthly", "Monthly mutual fund discussion thread")
        question = self._entry("question", "Should I switch my mutual fund because of fees?")
        for entry in (recurring, question):
            self._save(entry)
        db.record_feed_snapshot(self.conn, "2026-09-23T12:00:00+05:30", "2026-09-23T06:30:00Z",
                                "Test", "new", [recurring, question])
        db.record_feed_snapshot(self.conn, "2026-09-23T12:01:00+05:30", "2026-09-23T06:31:00Z",
                                "Test", "hot", [recurring, question])
        reports = generate_reports(self.conn, self.root / "out", "2026-09-23")
        batch = json.loads(Path(reports["batch_json"]).read_text(encoding="utf-8"))
        for key in ("new_posts_last_24h", "hot_top_10_latest", "reply_shortlist", "content_pool"):
            self.assertEqual(["question"], [row["post_id"] for row in batch[key]])
        self.assertIn("monthly", Path(reports["snapshots_csv"]).read_text(encoding="utf-8-sig"))

    def test_work_batch_carries_prior_selected_posts_in_same_file(self):
        out = self.root / "out"
        out.mkdir()
        (out / "Reddit-Work-Batch-2026-09-22.json").write_text(json.dumps({
            "new_posts_last_24h": [{"post_id": "older", "subreddit": "Test",
                                    "title": "Prior fee question", "topic": "Mutual Funds",
                                    "published_utc": "2026-09-22T05:00:00Z",
                                    "url": "https://www.reddit.com/r/Test/comments/older/question"}],
            "hot_top_10_latest": [], "content_pool": [],
        }), encoding="utf-8")
        entry = self._entry("today", "Today's mutual fund question")
        self._save(entry)
        db.record_feed_snapshot(self.conn, "2026-09-23T12:00:00+05:30", "2026-09-23T06:30:00Z",
                                "Test", "new", [entry])
        reports = generate_reports(self.conn, out, "2026-09-23")
        text = Path(reports["batch_text"]).read_text(encoding="utf-8")
        self.assertIn("PRIOR SIX DAYS: SELECTED POST HISTORY", text)
        self.assertIn("2026-09-22 | older | r/Test", text)


if __name__ == "__main__":
    unittest.main()
