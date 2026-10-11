import csv
import io
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reddit_monitor import db
from reddit_monitor.collector import Collector
from reddit_monitor.config import ConfigError, default_config, load_config, validate_config
from reddit_monitor.engagement import engagement_from_ranks
from reddit_monitor.feed_parser import canonical_permalink, extract_post_id, normalize_timestamp, parse_feed
from reddit_monitor.filter_engine import RelevanceEngine, phrase_matches
from reddit_monitor.http_client import CooldownActive, RedditHTTP
from reddit_monitor.models import FeedEntry, FetchResult, FilterResult
from reddit_monitor.process_lock import AlreadyRunning, ProcessLock
from reddit_monitor.reports import generate_reports, RELEVANT_HEADERS
from reddit_monitor.xlsx_writer import validate_xlsx

FIX = ROOT / "tests" / "fixtures"
FILTER = ROOT / "filters" / "finance.json"


def atom(entries):
    bits = ['<?xml version="1.0" encoding="UTF-8"?>', '<feed xmlns="http://www.w3.org/2005/Atom">']
    for post_id, published, title, desc in entries:
        bits.append('<entry>')
        bits.append('<id>t3_%s</id>' % post_id)
        bits.append('<title>%s</title>' % title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
        bits.append('<published>%s</published>' % published)
        bits.append('<link href="https://www.reddit.com/r/Test/comments/%s/sample/" />' % post_id)
        d = desc.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        bits.append('<content type="html">%s</content>' % d)
        bits.append('</entry>')
    bits.append('</feed>')
    return ''.join(bits).encode('utf-8')


def cfg_for(tmp, subs=None):
    return {
        "contact_email": "test@example.com",
        "user_agent": "RedditRSSMonitor-Test/1.0 (+contact:test@example.com)",
        "subreddits": subs or ["Test"],
        "output_dir": str(Path(tmp) / "out"),
        "runtime_dir": str(Path(tmp) / "runtime"),
        "filter_profile": str(FILTER),
        "enrichment_hours_local": [9, 18],
        "minimum_request_delay_seconds": 0,
        "first_run_recovery_hours": 24,
        "hot_interval_hours": 6,
        "enable_rising_feed": False,
        "top_day_interval_hours": 12,
        "top_week_interval_hours": 12,
        "cooldown_429_hours": 6,
        "cooldown_429_repeat_hours": 24,
        "cooldown_403_hours": 24,
        "feed_limit": 100,
        "request_timeout_seconds": 2,
        "server_error_retry_once": False,
        "warn_delay_below_seconds": 60,
        "collect_comments": False,
    }


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self._cooldown = None
    def cooldown_until(self):
        return self._cooldown
    def fetch(self, url, conditional=None, progress=None):
        self.calls.append((url, dict(conditional or {})))
        if not self.responses:
            raise AssertionError("Unexpected HTTP call: %s" % url)
        item = self.responses.pop(0)
        if callable(item):
            item = item(url, conditional or {})
        if item.cooldown_until:
            self._cooldown = item.cooldown_until
        return item


class MutableClock:
    def __init__(self, value):
        self.value = value
    def __call__(self):
        return self.value


class AlwaysErrorHTTP(RedditHTTP):
    def __init__(self, *args, status=429, retry_after=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.status_to_raise = status
        self.retry_after = retry_after
        self.open_calls = 0
    def _open(self, request):
        self.open_calls += 1
        headers = Message()
        if self.retry_after is not None:
            headers["Retry-After"] = str(self.retry_after)
        raise urllib.error.HTTPError(request.full_url, self.status_to_raise, "error", headers, io.BytesIO(b""))


class ParserTests(unittest.TestCase):
    def test_01_atom_parsing(self):
        rows = parse_feed((FIX / "atom_sample.xml").read_bytes(), "Test")
        self.assertEqual(2, len(rows))
        self.assertEqual("abc123", rows[0].post_id)
        self.assertIn("expense ratio", rows[0].description)

    def test_01b_atom_preserves_attached_image_link_without_fetching(self):
        xml = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <id>t3_pic123</id><title>Review allocation screenshot</title>
          <published>2026-09-23T05:00:00Z</published>
          <link href="https://www.reddit.com/r/Test/comments/pic123/review/" rel="alternate"/>
          <content type="html">&lt;img src="https://preview.redd.it/example.jpeg?width=640"/&gt;
          &lt;p&gt;Please review my funds&lt;/p&gt;</content>
        </entry></feed>"""
        rows = parse_feed(xml, "Test")
        self.assertEqual(1, len(rows))
        self.assertIn("Attached media URLs (not inspected): https://preview.redd.it/example.jpeg?width=640",
                      rows[0].description)

    def test_02_rss_parsing(self):
        rows = parse_feed((FIX / "rss_sample.xml").read_bytes(), "Test")
        self.assertEqual("ghi789", rows[0].post_id)

    def test_03_post_id_extraction(self):
        self.assertEqual("abc123", extract_post_id("t3_ABC123", ""))
        self.assertEqual("xyz9", extract_post_id("", "https://www.reddit.com/r/x/comments/XYZ9/title/"))

    def test_04_canonical_permalink(self):
        got = canonical_permalink("https://old.reddit.com/r/Test/comments/ABC123/title/?utm_source=x#frag")
        self.assertEqual("https://www.reddit.com/r/Test/comments/ABC123/title", got)

    def test_05_timestamp_conversion(self):
        self.assertEqual("2026-09-18T03:00:00Z", normalize_timestamp("2026-09-18T08:30:00+05:30"))

    def test_05b_malformed_entry_fails_conservatively(self):
        bad = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><id>t3_x</id><title>x</title><published>2026-09-18T00:00:00Z</published></entry></feed>'
        with self.assertRaises(ValueError):
            parse_feed(bad, "Test")


class FilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = RelevanceEngine.from_file(FILTER)

    def test_06_weighted_relevance(self):
        r = self.engine.classify("Should I switch my mutual fund?", "My portfolio expense ratio is high")
        self.assertEqual("relevant", r.classification)
        self.assertGreaterEqual(r.score, 5.5)

    def test_07_word_boundary_ria(self):
        self.assertFalse(phrase_matches("The criteria are strict", "RIA"))
        self.assertTrue(phrase_matches("Talked to an RIA yesterday", "RIA"))

    def test_08_word_boundary_fd(self):
        self.assertFalse(phrase_matches("I received a refund", "FD"))
        self.assertTrue(phrase_matches("Should I use an FD?", "FD"))

    def test_09_strong_phrase_classification(self):
        r = self.engine.classify("Health insurance advice", "Need health insurance for parents")
        self.assertEqual("relevant", r.classification)
        self.assertEqual("Insurance / Protection", r.topic)

    def test_10_supporting_phrase_classification(self):
        r = self.engine.classify("SIP confused", "Should I continue my SIP?")
        self.assertIn(r.classification, ("relevant", "review"))
        self.assertEqual("Mutual Funds", r.topic)

    def test_11_noise_penalty(self):
        clean = self.engine.classify("Mutual fund review", "Should I review my portfolio?")
        noisy = self.engine.classify("Mutual fund KYC pending", "KYC processing and account activation")
        self.assertLess(noisy.score, clean.score)

    def test_12_hard_exclusion(self):
        r = self.engine.classify("Mutual fund guaranteed return", "Use my referral code")
        self.assertEqual("excluded", r.classification)
        self.assertIn("Hard exclusion", r.review_reason)

    def test_13_sensitive_needs_review(self):
        r = self.engine.classify("Lost my job, retirement SIP help", "I lost my job and need help with my retirement corpus")
        self.assertEqual("review", r.classification)
        self.assertIn("Sensitive", r.review_reason)

    def test_14_tax_regulatory_needs_review(self):
        r = self.engine.classify("Schedule FA help", "Should I report foreign assets in Schedule FA for tax filing?")
        self.assertEqual("review", r.classification)
        self.assertIn("tax or regulatory", r.review_reason)

    def test_15_irrelevant_excluded(self):
        r = self.engine.classify("Best pizza in Bengaluru", "Where should I eat tonight?")
        self.assertEqual("excluded", r.classification)

    def test_15b_rent_question_not_mislabeled_as_fund_selection(self):
        r = self.engine.classify("Whats the ideal rent to income ratio?", "I might move out and pay 20000 monthly rent. I also have a 5000 SIP in an index fund.")
        self.assertEqual("relevant", r.classification)
        self.assertEqual("Debt / Housing", r.topic)

    def test_15c_freelance_income_not_mislabeled_as_fund_selection(self):
        r = self.engine.classify("Freelancers: how do you invest or save?", "I am self employed and work on a project basis. Income is irregular, so I cannot commit to a fixed SIP.")
        self.assertEqual("relevant", r.classification)
        self.assertEqual("Life / Money Context", r.topic)


class EngagementTests(unittest.TestCase):
    def test_16_engagement_new_low(self):
        self.assertEqual(("LOW", "New only"), engagement_from_ranks())

    def test_17_engagement_hot_medium_high(self):
        self.assertEqual("HIGH", engagement_from_ranks(hot_rank=4)[0])
        self.assertEqual("MEDIUM", engagement_from_ranks(hot_rank=14)[0])

    def test_18_engagement_top_medium_high(self):
        self.assertEqual("HIGH", engagement_from_ranks(top_week_rank=8)[0])
        self.assertEqual("MEDIUM", engagement_from_ranks(top_week_rank=20)[0])

    def test_19_engagement_both_high(self):
        level, evidence = engagement_from_ranks(hot_rank=20, top_week_rank=30)
        self.assertEqual("HIGH", level)
        self.assertEqual("Hot #20; Top week #30", evidence)


class DBAndCollectorTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.td.name)
        self.conn = db.connect(Path(self.td.name) / "state.sqlite3")
        db.init_db(self.conn)
        self.now = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)
        for feed_type in ("hot", "top_day", "top_week"):
            db.save_feed_state(self.conn, "Test", feed_type, None, None, "2026-09-18T06:00:00Z", 200)

    def tearDown(self):
        self.conn.close()
        self.td.cleanup()

    def _old_enough_feed(self, title="Mutual fund review", post_id="p1", new_time="2026-09-18T05:30:00Z"):
        return atom([
            (post_id, new_time, title, "Should I review my mutual fund portfolio?"),
            ("old1", "2026-09-17T20:00:00Z", "Old irrelevant", "hello world"),
        ])

    def test_20_dedupe(self):
        entry = FeedEntry("x1", "Test", "Mutual fund", "portfolio review", "https://www.reddit.com/r/Test/comments/x1/a", "2026-09-18T05:00:00Z")
        result = FilterResult("relevant", "Mutual Funds", 6, ["mutual fund"], "")
        db.upsert_post(self.conn, entry, result, "2026-09-18T06:00:00+00:00", "2026-09-18T06:00:00Z")
        db.upsert_post(self.conn, entry, result, "2026-09-18T06:01:00+00:00", "2026-09-18T06:01:00Z")
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0])

    def test_21_existing_post_engagement_update_without_duplication(self):
        body = self._old_enough_feed()
        hot = atom([("p1", "2026-09-18T05:30:00Z", "Mutual fund review", "Should I review my mutual fund portfolio?")])
        top = hot
        fake = FakeHTTP([FetchResult(200, body, {}, "u"), FetchResult(200, hot, {}, "u"), FetchResult(200, top, {}, "u"), FetchResult(200, top, {}, "u")])
        c = Collector(self.cfg, self.conn, progress=lambda x: None, http=fake, now_func=lambda: self.now)
        c.run(force_enrichment=True)
        row = db.get_post(self.conn, "p1")
        self.assertEqual("HIGH", row["engagement"])
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM posts WHERE post_id='p1'").fetchone()[0])

    def test_22_etag_sent(self):
        db.save_feed_state(self.conn, "Test", "new", '"abc"', None, "2026-09-18T03:00:00Z", 200)
        db.set_checkpoint(self.conn, "Test", "2026-09-18T03:00:00Z")
        fake = FakeHTTP([FetchResult(304, b"", {}, "u")])
        Collector(self.cfg, self.conn, progress=lambda x: None, http=fake, now_func=lambda: self.now).run()
        self.assertEqual('"abc"', fake.calls[0][1]["etag"])

    def test_23_last_modified_sent(self):
        db.save_feed_state(self.conn, "Test", "new", None, "Fri, 18 Sep 2026 03:00:00 GMT", "2026-09-18T03:00:00Z", 200)
        db.set_checkpoint(self.conn, "Test", "2026-09-18T03:00:00Z")
        fake = FakeHTTP([FetchResult(304, b"", {}, "u")])
        Collector(self.cfg, self.conn, progress=lambda x: None, http=fake, now_func=lambda: self.now).run()
        self.assertIn("Last", "Last-Modified")
        self.assertEqual("Fri, 18 Sep 2026 03:00:00 GMT", fake.calls[0][1]["last_modified"])

    def test_24_304_handling_advances_checkpoint(self):
        db.save_feed_state(self.conn, "Test", "new", '"abc"', None, "2026-09-18T03:00:00Z", 200)
        db.set_checkpoint(self.conn, "Test", "2026-09-18T03:00:00Z")
        fake = FakeHTTP([FetchResult(304, b"", {}, "u")])
        Collector(self.cfg, self.conn, progress=lambda x: None, http=fake, now_func=lambda: self.now).run()
        self.assertEqual("2026-09-18T06:00:00Z", db.get_checkpoint(self.conn, "Test"))

    def test_25_independent_subreddit_checkpoints(self):
        self.cfg = cfg_for(self.td.name, ["Test", "Other"])
        db.set_checkpoint(self.conn, "Test", "2026-09-18T03:00:00Z")
        db.set_checkpoint(self.conn, "Other", "2026-09-18T02:00:00Z")
        for feed_type in ("hot", "top_day", "top_week"):
            db.save_feed_state(self.conn, "Other", feed_type, None, None, "2026-09-18T06:00:00Z", 200)
        body1 = atom([("a1", "2026-09-18T05:00:00Z", "Mutual fund", "review"), ("a0", "2026-09-18T02:30:00Z", "old", "x")])
        body2 = atom([("b1", "2026-09-18T05:00:00Z", "Retirement", "retirement corpus help"), ("b0", "2026-09-18T01:30:00Z", "old", "x")])
        fake = FakeHTTP([FetchResult(200, body1, {}, "u"), FetchResult(200, body2, {}, "u")])
        Collector(self.cfg, self.conn, progress=lambda x: None, http=fake, now_func=lambda: self.now).run()
        self.assertEqual("2026-09-18T06:00:00Z", db.get_checkpoint(self.conn, "Test"))
        self.assertEqual("2026-09-18T06:00:00Z", db.get_checkpoint(self.conn, "Other"))

    def test_26_missed_run_catchup(self):
        db.set_checkpoint(self.conn, "Test", "2026-09-17T21:00:00Z")
        body = atom([
            ("n2", "2026-09-18T04:00:00Z", "Retirement corpus", "Should I review retirement?"),
            ("n1", "2026-09-18T00:00:00Z", "Mutual fund", "Should I switch mutual fund?"),
            ("older", "2026-09-17T20:00:00Z", "old", "x"),
        ])
        fake = FakeHTTP([FetchResult(200, body, {}, "u")])
        Collector(self.cfg, self.conn, progress=lambda x: None, http=fake, now_func=lambda: self.now).run()
        self.assertIsNotNone(db.get_post(self.conn, "n1"))
        self.assertIsNotNone(db.get_post(self.conn, "n2"))
        self.assertEqual("2026-09-18T06:00:00Z", db.get_checkpoint(self.conn, "Test"))

    def test_27_feed_coverage_sufficient(self):
        fake = FakeHTTP([FetchResult(200, self._old_enough_feed(), {"ETag": '"new"'}, "u")])
        Collector(self.cfg, self.conn, progress=lambda x: None, http=fake, now_func=lambda: self.now).run()
        row = self.conn.execute("SELECT coverage_status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual("OK", row[0])

    def test_28_feed_coverage_insufficient(self):
        body = atom([("p%03d" % i, "2026-09-18T05:50:00Z", "Mutual fund", "review") for i in range(100)])
        fake = FakeHTTP([FetchResult(200, body, {"ETag": '"gap"'}, "u")])
        Collector(self.cfg, self.conn, progress=lambda x: None, http=fake, now_func=lambda: self.now).run()
        row = self.conn.execute("SELECT coverage_status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual("GAP", row[0])

    def test_29_gap_does_not_advance_checkpoint(self):
        db.set_checkpoint(self.conn, "Test", "2026-09-18T03:00:00Z")
        body = atom([("p%03d" % i, "2026-09-18T05:50:00Z", "Mutual fund", "review") for i in range(100)])
        fake = FakeHTTP([FetchResult(200, body, {"ETag": '"gap"'}, "u")])
        Collector(self.cfg, self.conn, progress=lambda x: None, http=fake, now_func=lambda: self.now).run()
        self.assertEqual("2026-09-18T03:00:00Z", db.get_checkpoint(self.conn, "Test"))

    def test_30_gap_does_not_poison_etag_state(self):
        db.set_checkpoint(self.conn, "Test", "2026-09-18T03:00:00Z")
        db.save_feed_state(self.conn, "Test", "new", '"old"', "old-lm", "2026-09-18T03:00:00Z", 200)
        body = atom([("p%03d" % i, "2026-09-18T05:50:00Z", "Mutual fund", "review") for i in range(100)])
        fake = FakeHTTP([FetchResult(200, body, {"ETag": '"new"', "Last-Modified": "new-lm"}, "u")])
        Collector(self.cfg, self.conn, progress=lambda x: None, http=fake, now_func=lambda: self.now).run()
        state = db.get_feed_state(self.conn, "Test", "new")
        self.assertEqual('"old"', state["etag"])
        self.assertEqual("old-lm", state["last_modified"])

    def test_31_429_stops_remaining_calls(self):
        self.cfg = cfg_for(self.td.name, ["Test", "Other", "Third"])
        cooldown = "2026-09-18T12:00:00Z"
        fake = FakeHTTP([
            FetchResult(200, self._old_enough_feed(), {}, "u"),
            FetchResult(429, b"", {}, "u", stopped=True, cooldown_until=cooldown),
        ])
        Collector(self.cfg, self.conn, progress=lambda x: None, http=fake, now_func=lambda: self.now).run()
        self.assertEqual(2, len(fake.calls))

    def test_32a_5xx_at_most_one_retry_then_success(self):
        self.cfg["server_error_retry_once"] = True
        body = self._old_enough_feed()
        fake = FakeHTTP([FetchResult(500, b"", {}, "u"), FetchResult(200, body, {}, "u")])
        Collector(self.cfg, self.conn, progress=lambda x: None, http=fake, now_func=lambda: self.now).run()
        self.assertEqual(2, len(fake.calls))
        self.assertEqual("2026-09-18T06:00:00Z", db.get_checkpoint(self.conn, "Test"))

    def test_32b_collector_cooldown_makes_zero_requests(self):
        fake = FakeHTTP([])
        fake._cooldown = "2026-09-18T12:00:00Z"
        Collector(self.cfg, self.conn, progress=lambda x: None, http=fake, now_func=lambda: self.now).run()
        self.assertEqual(0, len(fake.calls))
        row = self.conn.execute("SELECT http_status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual("COOLDOWN", row[0])


class GovernorTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.td.name) / "db.sqlite3")
        db.init_db(self.conn)
        self.clock = MutableClock(datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc))

    def tearDown(self):
        self.conn.close(); self.td.cleanup()

    def test_32_429_persistent_cooldown_zero_second_request(self):
        h = AlwaysErrorHTTP(self.conn, "ua", minimum_delay_seconds=0, clock=self.clock, sleeper=lambda x: None, status=429)
        first = h.fetch("https://example.test")
        self.assertEqual(429, first.status)
        with self.assertRaises(CooldownActive):
            h.fetch("https://example.test")
        self.assertEqual(1, h.open_calls)

    def test_33_repeated_429_escalates_to_24h(self):
        h = AlwaysErrorHTTP(self.conn, "ua", minimum_delay_seconds=0, clock=self.clock, sleeper=lambda x: None, status=429)
        h.fetch("https://example.test")
        self.clock.value += timedelta(hours=7)
        h.fetch("https://example.test")
        gov = db.get_governor(self.conn)
        until = datetime.fromisoformat(gov["cooldown_until"].replace("Z", "+00:00"))
        self.assertGreaterEqual((until - self.clock.value).total_seconds(), 24 * 3600 - 1)

    def test_34_retry_after_respected_if_longer(self):
        h = AlwaysErrorHTTP(self.conn, "ua", minimum_delay_seconds=0, clock=self.clock, sleeper=lambda x: None, status=429, retry_after=8*3600)
        h.fetch("https://example.test")
        until = datetime.fromisoformat(db.get_governor(self.conn)["cooldown_until"].replace("Z", "+00:00"))
        self.assertGreaterEqual((until - self.clock.value).total_seconds(), 8*3600 - 1)

    def test_35_403_cooldown(self):
        h = AlwaysErrorHTTP(self.conn, "ua", minimum_delay_seconds=0, clock=self.clock, sleeper=lambda x: None, status=403)
        h.fetch("https://example.test")
        until = datetime.fromisoformat(db.get_governor(self.conn)["cooldown_until"].replace("Z", "+00:00"))
        self.assertGreaterEqual((until - self.clock.value).total_seconds(), 24*3600 - 1)

    def test_36_http_conditional_headers(self):
        h = RedditHTTP(self.conn, "ua", minimum_delay_seconds=0, clock=self.clock, sleeper=lambda x: None)
        headers = h._headers({"etag": '"x"', "last_modified": "Fri, 18 Sep 2026 00:00:00 GMT"})
        self.assertEqual('"x"', headers["If-None-Match"])
        self.assertIn("If-Modified-Since", headers)


class LockTests(unittest.TestCase):
    def test_37_concurrent_process_lock(self):
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "run.lock"
            code = (
                "import time; from reddit_monitor.process_lock import ProcessLock; "
                "p=r'%s'; "
                "\nwith ProcessLock(p):\n print('LOCKED', flush=True); time.sleep(2)" % str(lock)
            )
            env = dict(os.environ); env["PYTHONPATH"] = str(ROOT)
            proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            try:
                line = proc.stdout.readline().strip()
                self.assertEqual("LOCKED", line)
                with self.assertRaises(AlreadyRunning):
                    with ProcessLock(lock):
                        pass
            finally:
                proc.terminate(); proc.wait(timeout=5)
                if proc.stdout: proc.stdout.close()
                if proc.stderr: proc.stderr.close()


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.td.name) / "db.sqlite3"); db.init_db(self.conn)
        self.date = "2026-09-18"

    def tearDown(self):
        self.conn.close(); self.td.cleanup()

    def add_post(self, pid, classification="relevant", title="A&B <test>"):
        entry = FeedEntry(pid, "Test", title, "Description & details", "https://www.reddit.com/r/Test/comments/%s/title" % pid, "2026-09-18T05:00:00Z")
        result = FilterResult(classification, "Mutual Funds", 6, ["mutual fund", "A&B"], "Manual check" if classification == "review" else "")
        db.upsert_post(self.conn, entry, result, "2026-09-18T10:00:00+05:30", "2026-09-18T04:30:00Z")
        db.mark_report_day(self.conn, self.date, pid, "test")

    def test_38_xlsx_exactly_three_tabs(self):
        self.add_post("p1")
        out = generate_reports(self.conn, Path(self.td.name)/"out", self.date)
        v = validate_xlsx(out["xlsx"], ["Relevant Posts", "Needs Review", "Runs"])
        self.assertEqual(3, len(v["sheet_names"]))

    def test_39_xlsx_xml_escaping(self):
        self.add_post("p1", title='A&B <tag> "quote"')
        out = generate_reports(self.conn, Path(self.td.name)/"out", self.date)
        v = validate_xlsx(out["xlsx"], ["Relevant Posts", "Needs Review", "Runs"])
        self.assertEqual([1,0,0], v["row_counts"])

    def test_40_xlsx_hyperlink_validity(self):
        self.add_post("p1")
        out = generate_reports(self.conn, Path(self.td.name)/"out", self.date)
        v = validate_xlsx(out["xlsx"], ["Relevant Posts", "Needs Review", "Runs"])
        self.assertEqual(1, v["hyperlink_counts"][0])

    def test_41_csv_relevant_only(self):
        self.add_post("r1", "relevant"); self.add_post("q1", "review")
        out = generate_reports(self.conn, Path(self.td.name)/"out", self.date)
        with open(out["csv"], encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(2, len(rows))
        self.assertEqual("r1", rows[1][-1].split("/comments/")[1].split("/")[0])

    def test_42_jsonl_relevant_only(self):
        self.add_post("r1", "relevant"); self.add_post("q1", "review")
        out = generate_reports(self.conn, Path(self.td.name)/"out", self.date)
        lines = Path(out["jsonl"]).read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(lines))
        self.assertIn("r1", json.loads(lines[0])["Reddit Link"])

    def test_43_daily_file_update_same_path(self):
        self.add_post("r1")
        out1 = generate_reports(self.conn, Path(self.td.name)/"out", self.date)
        self.add_post("r2")
        out2 = generate_reports(self.conn, Path(self.td.name)/"out", self.date)
        self.assertEqual(out1["xlsx"], out2["xlsx"])
        self.assertEqual([2,0,0], validate_xlsx(out2["xlsx"], ["Relevant Posts", "Needs Review", "Runs"])["row_counts"])

    def test_44_no_duplicate_spreadsheet_rows(self):
        self.add_post("r1")
        db.mark_report_day(self.conn, self.date, "r1", "again")
        out = generate_reports(self.conn, Path(self.td.name)/"out", self.date)
        self.assertEqual(1, validate_xlsx(out["xlsx"], ["Relevant Posts", "Needs Review", "Runs"])["row_counts"][0])


class ConfigTests(unittest.TestCase):
    def test_45_configuration_validation(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = default_config("a@example.com", td, subreddits=["r/Test"], runtime_dir=Path(td)/"runtime", filter_profile=FILTER)
            self.assertEqual(["Test"], validate_config(cfg)["subreddits"])
            cfg["feed_limit"] = 101
            with self.assertRaises(ConfigError):
                validate_config(cfg)

    def test_46_configuration_invalid_subreddit(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ConfigError):
                default_config("a@example.com", td, subreddits=["bad/sub"], runtime_dir=Path(td)/"runtime", filter_profile=FILTER)


class SchedulerTests(unittest.TestCase):
    def test_48_launchd_plist_generation(self):
        import reddit_monitor.cli as cli
        with tempfile.TemporaryDirectory() as td:
            cfg = cfg_for(td)
            cfg["python_executable"] = "/opt/homebrew/bin/python3.13"
            config_path = Path(td) / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            launch_dir = Path(td) / "LaunchAgents"
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""
            with mock.patch.object(cli, "_launchagents_dir", return_value=launch_dir), mock.patch.object(cli, "_launchctl", return_value=Result()):
                dest = cli.install_scheduler(cfg, config_path)
            with dest.open("rb") as f:
                data = plistlib.load(f)
            self.assertEqual("com.redditrssmonitor", data["Label"])
            self.assertEqual(8, len(data["StartCalendarInterval"]))
            self.assertEqual([0,3,6,9,12,15,18,21], [x["Hour"] for x in data["StartCalendarInterval"]])
            self.assertTrue(all(x["Minute"] == 17 for x in data["StartCalendarInterval"]))
            self.assertEqual("/opt/homebrew/bin/python3.13", data["ProgramArguments"][0])


class E2ETests(unittest.TestCase):
    def test_47_end_to_end_two_runs_fake_reddit_server(self):
        phase = {"n": 1}
        request_log = []

        def body_for(path):
            if phase["n"] == 1:
                if "/new/" in path:
                    return atom([
                        ("p1", "2026-09-18T05:30:00Z", "Should I switch my mutual fund?", "My portfolio has a high expense ratio and I am confused"),
                        ("junk", "2026-09-18T05:00:00Z", "Cricket match", "great game"),
                        ("old", "2026-09-17T20:00:00Z", "Old post", "nothing"),
                    ])
                if "/hot/" in path:
                    return atom([("p1", "2026-09-18T05:30:00Z", "Should I switch my mutual fund?", "expense ratio portfolio review")])
                return atom([("p1", "2026-09-18T05:30:00Z", "Should I switch my mutual fund?", "expense ratio portfolio review")])
            if "/new/" in path:
                return atom([
                    ("p2", "2026-09-18T08:00:00Z", "Retirement corpus help", "Should I review my retirement corpus and NPS?"),
                    ("p1", "2026-09-18T05:30:00Z", "Should I switch my mutual fund?", "expense ratio portfolio review"),
                    ("old", "2026-09-17T20:00:00Z", "Old post", "nothing"),
                ])
            if "/hot/" in path:
                return atom([("p1", "2026-09-18T05:30:00Z", "Should I switch my mutual fund?", "expense ratio portfolio review"), ("p2", "2026-09-18T08:00:00Z", "Retirement corpus help", "retirement corpus")])
            return atom([("p1", "2026-09-18T05:30:00Z", "Should I switch my mutual fund?", "expense ratio portfolio review")])

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                request_log.append(self.path)
                payload = body_for(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/atom+xml")
                self.send_header("ETag", '"phase-%d"' % phase["n"])
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, fmt, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                cfg = cfg_for(td)
                conn = db.connect(Path(td)/"db.sqlite3"); db.init_db(conn)
                clock = MutableClock(datetime(2026,9,18,6,0,tzinfo=timezone.utc))
                c1 = Collector(cfg, conn, progress=lambda x: None, now_func=clock,
                               base_url_override="http://127.0.0.1:%d" % server.server_port)
                out1 = c1.run(force_enrichment=True)
                self.assertEqual(4, len(request_log))
                self.assertEqual(1, out1["counts"]["relevant"])
                self.assertEqual("HIGH", db.get_post(conn, "p1")["engagement"])
                phase["n"] = 2
                clock.value = datetime(2026,9,18,9,0,tzinfo=timezone.utc)
                c2 = Collector(cfg, conn, progress=lambda x: None, now_func=clock,
                               base_url_override="http://127.0.0.1:%d" % server.server_port)
                out2 = c2.run(force_enrichment=True)
                self.assertEqual(8, len(request_log))
                self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM posts WHERE post_id='p1'").fetchone()[0])
                self.assertIsNotNone(db.get_post(conn, "p2"))
                self.assertEqual(out1["xlsx"], out2["xlsx"])
                v = validate_xlsx(out2["xlsx"], ["Relevant Posts", "Needs Review", "Runs"])
                self.assertEqual(3, len(v["sheet_names"]))
                self.assertEqual(2, v["row_counts"][0])
                conn.close()
        finally:
            server.shutdown(); thread.join(timeout=2); server.server_close()


if __name__ == "__main__":
    unittest.main()


class StabilizationV11Tests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.td.name)
        self.conn = db.connect(Path(self.td.name) / "stabilization.sqlite3")
        db.init_db(self.conn)
        self.now = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()
        self.td.cleanup()

    def _collector(self, responses=None):
        fake = FakeHTTP(responses or [])
        return Collector(self.cfg, self.conn, progress=lambda x: None, http=fake, now_func=lambda: self.now), fake

    def test_49_top_day_engagement(self):
        level, evidence = engagement_from_ranks(top_day_rank=7)
        self.assertEqual("HIGH", level)
        self.assertEqual("Top day #7", evidence)

    def test_50_first_run_plan_contains_all_four_feed_types(self):
        c, _ = self._collector()
        plan = c._feed_plan(self.now)
        self.assertEqual([("Test", "new"), ("Test", "hot"), ("Test", "top_day"), ("Test", "top_week")], plan)

    def test_51_due_state_suppresses_rankings_until_due(self):
        for ft in ("hot", "top_day", "top_week"):
            db.save_feed_state(self.conn, "Test", ft, None, None, "2026-09-18T06:00:00Z", 200)
        c, _ = self._collector()
        self.assertEqual([("Test", "new")], c._feed_plan(self.now))

    def test_52_hot_due_after_six_hours(self):
        db.save_feed_state(self.conn, "Test", "hot", None, None, "2026-09-18T00:00:00Z", 200)
        db.save_feed_state(self.conn, "Test", "top_day", None, None, "2026-09-18T06:00:00Z", 200)
        db.save_feed_state(self.conn, "Test", "top_week", None, None, "2026-09-18T06:00:00Z", 200)
        c, _ = self._collector()
        self.assertEqual([("Test", "new"), ("Test", "hot")], c._feed_plan(self.now))

    def test_53_top_feeds_due_after_twelve_hours(self):
        db.save_feed_state(self.conn, "Test", "hot", None, None, "2026-09-18T06:00:00Z", 200)
        db.save_feed_state(self.conn, "Test", "top_day", None, None, "2026-09-17T18:00:00Z", 200)
        db.save_feed_state(self.conn, "Test", "top_week", None, None, "2026-09-17T18:00:00Z", 200)
        c, _ = self._collector()
        self.assertEqual([("Test", "new"), ("Test", "top_day"), ("Test", "top_week")], c._feed_plan(self.now))

    def test_54_first_run_new_window_is_24_hours(self):
        body = atom([
            ("inside", "2026-09-17T07:00:00Z", "Mutual fund review", "Should I review my mutual fund?"),
            ("outside", "2026-09-17T05:00:00Z", "Mutual fund review", "Should I review my mutual fund?"),
        ])
        c, _ = self._collector([FetchResult(200, body, {}, "u")])
        out = c._run_feed("Test", "new", "2026-09-18T06:00:00+00:00", "2026-09-18", self.now)
        self.assertEqual("OK", out["coverage"])
        self.assertIsNotNone(db.get_post(self.conn, "inside"))
        self.assertIsNone(db.get_post(self.conn, "outside"))
        run = self.conn.execute("SELECT scan_window_start FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual("2026-09-17T06:00:00Z", run[0])

    def test_55_quiet_feed_below_capacity_can_prove_no_truncation(self):
        body = atom([("only", "2026-09-18T05:30:00Z", "Mutual fund review", "Should I review my mutual fund?")])
        c, _ = self._collector([FetchResult(200, body, {}, "u")])
        out = c._run_feed("Test", "new", "2026-09-18T06:00:00+00:00", "2026-09-18", self.now)
        self.assertEqual("OK", out["coverage"])
        self.assertEqual("2026-09-18T06:00:00Z", db.get_checkpoint(self.conn, "Test"))

    def test_56_unexpected_304_without_prior_state_is_not_success(self):
        c, _ = self._collector([FetchResult(304, b"", {}, "u")])
        out = c._run_feed("Test", "new", "2026-09-18T06:00:00+00:00", "2026-09-18", self.now)
        self.assertEqual("ERROR", out["coverage"])
        self.assertIsNone(db.get_checkpoint(self.conn, "Test"))

    def test_57_unchanged_ranking_304_carries_active_post_into_today(self):
        entry = FeedEntry("carry", "Test", "Mutual fund review", "Should I review my mutual fund?",
                          "https://www.reddit.com/r/Test/comments/carry/title", "2026-09-17T05:00:00Z")
        result = FilterResult("relevant", "Mutual Funds", 8, ["mutual fund"], "Relevant threshold met")
        db.upsert_post(self.conn, entry, result, "2026-09-17T05:00:00+00:00", "2026-09-17T05:00:00Z",
                       top_week_rank=4, engagement="HIGH", evidence="Top week #4")
        db.save_feed_state(self.conn, "Test", "top_week", '"same"', None, "2026-09-17T18:00:00Z", 200)
        c, fake = self._collector([FetchResult(304, b"", {}, "u")])
        c._run_feed("Test", "top_week", "2026-09-18T06:00:00+00:00", "2026-09-18", self.now)
        rows = db.posts_for_report_day(self.conn, "2026-09-18", "relevant")
        self.assertEqual(1, len(rows))
        self.assertEqual("HIGH", rows[0]["report_engagement"])
        self.assertEqual('"same"', fake.calls[0][1]["etag"])

    def test_58_ranking_rediscovery_reports_today_without_level_upgrade(self):
        entry = FeedEntry("rediscover", "Test", "Mutual fund review", "Should I review my mutual fund?",
                          "https://www.reddit.com/r/Test/comments/rediscover/title", "2026-09-17T05:00:00Z")
        result = FilterResult("relevant", "Mutual Funds", 8, ["mutual fund"], "Relevant threshold met")
        db.upsert_post(self.conn, entry, result, "2026-09-17T05:00:00+00:00", "2026-09-17T05:00:00Z",
                       hot_rank=20, engagement="MEDIUM", evidence="Hot #20")
        c, _ = self._collector()
        c._process_entry(entry, "hot", 20, "2026-09-18T06:00:00+00:00", "2026-09-18", "2026-09-18T06:00:00Z")
        rows = db.posts_for_report_day(self.conn, "2026-09-18", "relevant")
        self.assertEqual(1, len(rows))
        self.assertEqual("MEDIUM", rows[0]["report_engagement"])
        self.assertEqual("2026-09-18T06:00:00+00:00", rows[0]["report_detected_run"])

    def test_59_daily_report_preserves_peak_engagement(self):
        entry = FeedEntry("peak", "Test", "Mutual fund review", "Should I review my mutual fund?",
                          "https://www.reddit.com/r/Test/comments/peak/title", "2026-09-18T05:00:00Z")
        result = FilterResult("relevant", "Mutual Funds", 8, ["mutual fund"], "Relevant threshold met")
        db.upsert_post(self.conn, entry, result, "2026-09-18T05:00:00+00:00", "2026-09-18T05:00:00Z",
                       hot_rank=2, engagement="HIGH", evidence="Hot #2")
        db.mark_report_day(self.conn, "2026-09-18", "peak", "ranking_seen_today", "2026-09-18T05:00:00+00:00", "HIGH", "Hot #2")
        db.set_post_ranks(self.conn, "peak", None, None, None, "LOW", "New only", "2026-09-18T06:00:00Z")
        rows = db.posts_for_report_day(self.conn, "2026-09-18", "relevant")
        self.assertEqual("HIGH", rows[0]["report_engagement"])
        self.assertEqual("Hot #2", rows[0]["report_signal_evidence"])

    def test_60_audit_lists_excluded_post_and_reason(self):
        entry = FeedEntry("junk-audit", "Test", "Cricket score", "Great match",
                          "https://www.reddit.com/r/Test/comments/junk-audit/title", "2026-09-18T05:00:00Z")
        c, _ = self._collector()
        c._process_entry(entry, "new", 1, "2026-09-18T06:00:00+00:00", "2026-09-18", "2026-09-18T06:00:00Z")
        out = generate_reports(self.conn, Path(self.td.name) / "out", "2026-09-18")
        with open(out["audit_csv"], encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(1, len(rows))
        self.assertEqual("EXCLUDED", rows[0]["Classification"])
        self.assertEqual("No configured topic matched", rows[0]["Classification Reason"])

    def test_61_audit_dedupes_across_feeds_and_updates_feed_evidence(self):
        entry = FeedEntry("audit-one", "Test", "Mutual fund review", "Should I review my mutual fund?",
                          "https://www.reddit.com/r/Test/comments/audit-one/title", "2026-09-18T05:00:00Z")
        c, _ = self._collector()
        c._process_entry(entry, "new", 1, "2026-09-18T06:00:00+00:00", "2026-09-18", "2026-09-18T06:00:00Z")
        c._process_entry(entry, "hot", 4, "2026-09-18T09:00:00+00:00", "2026-09-18", "2026-09-18T09:00:00Z")
        out = generate_reports(self.conn, Path(self.td.name) / "out", "2026-09-18")
        with open(out["audit_csv"], encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(1, len(rows))
        self.assertEqual("NEW; HOT", rows[0]["Feeds Seen"])
        self.assertEqual("HIGH", rows[0]["Engagement"])
        self.assertTrue(Path(out["latest_audit_csv"]).exists())

    def test_62_rebootstrap_rewinds_checkpoint_and_clears_conditional_state(self):
        db.set_checkpoint(self.conn, "Test", "2026-09-18T05:00:00Z")
        for ft in ("new", "hot", "top_day", "top_week"):
            db.save_feed_state(self.conn, "Test", ft, '"etag"', "lm", "2026-09-18T05:00:00Z", 200)

        # Seed deliberately stale ranking evidence so the reset is exercised,
        # rather than merely checking a missing post.
        entry = FeedEntry(
            "p1", "Test", "Mutual fund review", "Should I review my mutual fund?",
            "https://www.reddit.com/r/Test/comments/p1/title", "2026-09-18T04:00:00Z"
        )
        result = FilterResult("relevant", "Mutual Funds", 8, ["mutual fund"], "Relevant threshold met")
        db.upsert_post(
            self.conn, entry, result, "2026-09-18T05:00:00+00:00", "2026-09-18T05:00:00Z",
            hot_rank=2, top_day_rank=3, top_week_rank=4, engagement="HIGH",
            evidence="Hot #2; Top day #3; Top week #4"
        )

        target = db.prepare_rebootstrap(self.conn, ["Test"], self.now, hours=24)
        self.assertEqual("2026-09-17T06:00:00Z", target)
        self.assertEqual(target, db.get_checkpoint(self.conn, "Test"))
        for ft in ("new", "hot", "top_day", "top_week"):
            self.assertIsNone(db.get_feed_state(self.conn, "Test", ft))

        row = db.get_post(self.conn, "p1")
        self.assertIsNone(row["hot_rank"])
        self.assertIsNone(row["top_day_rank"])
        self.assertIsNone(row["top_week_rank"])
        self.assertEqual("LOW", row["engagement"])
        self.assertEqual("New only", row["signal_evidence"])

    def test_63_default_config_is_24h_bootstrap_with_due_intervals(self):
        cfg = default_config("a@example.com", self.td.name, subreddits=["Test"],
                             runtime_dir=Path(self.td.name) / "runtime", filter_profile=FILTER)
        self.assertEqual(24, cfg["first_run_recovery_hours"])
        self.assertEqual(6, cfg["hot_interval_hours"])
        self.assertEqual(12, cfg["top_day_interval_hours"])
        self.assertEqual(12, cfg["top_week_interval_hours"])

    def test_64_top_day_url_is_public_rss_endpoint(self):
        c, _ = self._collector()
        url = c._feed_url("Test", "top_day")
        self.assertIn("/r/Test/top/.rss?", url)
        self.assertIn("t=day", url)
        self.assertIn("limit=100", url)

    def test_65_relevant_filter_has_explicit_reason(self):
        r = RelevanceEngine.from_file(FILTER).classify("Mutual fund portfolio review", "Please review my mutual fund")
        self.assertEqual("relevant", r.classification)
        self.assertTrue(r.review_reason)


class SchemaMigrationTests(unittest.TestCase):
    def test_66_v1_database_schema_migrates_in_place(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "old.sqlite3"
            conn = sqlite3.connect(str(path))
            conn.executescript("""
                CREATE TABLE posts (
                    post_id TEXT PRIMARY KEY, permalink TEXT NOT NULL UNIQUE, subreddit TEXT NOT NULL,
                    post_time TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
                    topic TEXT NOT NULL DEFAULT '', classification TEXT NOT NULL DEFAULT 'excluded',
                    review_reason TEXT NOT NULL DEFAULT '', score REAL NOT NULL DEFAULT 0,
                    matched_terms TEXT NOT NULL DEFAULT '[]', first_detected_run TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, hot_rank INTEGER,
                    top_week_rank INTEGER, engagement TEXT NOT NULL DEFAULT 'LOW',
                    signal_evidence TEXT NOT NULL DEFAULT 'New only'
                );
                CREATE TABLE post_report_days (
                    report_date TEXT NOT NULL, post_id TEXT NOT NULL, reason TEXT NOT NULL,
                    PRIMARY KEY(report_date, post_id)
                );
            """)
            conn.commit(); conn.close()
            migrated = db.connect(path)
            try:
                db.init_db(migrated)
                post_cols = {r[1] for r in migrated.execute("PRAGMA table_info(posts)")}
                day_cols = {r[1] for r in migrated.execute("PRAGMA table_info(post_report_days)")}
                self.assertIn("top_day_rank", post_cols)
                self.assertIn("detected_run", day_cols)
                self.assertIn("peak_engagement", day_cols)
                self.assertIsNotNone(migrated.execute("SELECT name FROM sqlite_master WHERE name='post_observation_days'").fetchone())
            finally:
                migrated.close()


class DueScheduleE2ETests(unittest.TestCase):
    def test_67_due_schedule_first_run_then_3h_6h_12h(self):
        request_log = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                request_log.append(self.path)
                payload = atom([
                    ("p1", "2026-09-18T05:30:00Z", "Should I switch my mutual fund?", "My portfolio has a high expense ratio"),
                ])
                self.send_response(200)
                self.send_header("Content-Type", "application/atom+xml")
                self.send_header("ETag", '"stable"')
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, fmt, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                cfg = cfg_for(td)
                conn = db.connect(Path(td) / "due.sqlite3"); db.init_db(conn)
                clock = MutableClock(datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc))
                def run_at(hour):
                    clock.value = datetime(2026, 9, 18, hour, 0, tzinfo=timezone.utc)
                    Collector(cfg, conn, progress=lambda x: None, now_func=clock,
                              base_url_override="http://127.0.0.1:%d" % server.server_port).run()
                run_at(6)
                self.assertEqual(4, len(request_log))
                run_at(9)
                self.assertEqual(5, len(request_log))
                run_at(12)
                self.assertEqual(7, len(request_log))
                run_at(18)
                self.assertEqual(11, len(request_log))
                self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM posts WHERE post_id='p1'").fetchone()[0])
                conn.close()
        finally:
            server.shutdown(); thread.join(timeout=2); server.server_close()

class UpgradeCommandTests(unittest.TestCase):
    def test_68_upgrade_preserves_posts_and_rebootstraps_v1_state(self):
        from types import SimpleNamespace
        import reddit_monitor.cli as cli
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runtime = base / "runtime"
            output = base / "out"
            cfg = default_config("a@example.com", output, subreddits=["Test"], runtime_dir=runtime, filter_profile=FILTER)
            cfg["version"] = 1
            cfg["first_run_recovery_hours"] = 6
            cfg["user_agent"] = "RedditRSSMonitor/1.0 (+contact:a@example.com)"
            config_path = base / "config.json"
            from reddit_monitor.config import save_config
            save_config(cfg, config_path)
            conn = db.connect(runtime / "state.sqlite3"); db.init_db(conn)
            entry = FeedEntry("keepme", "Test", "Mutual fund review", "Should I review my mutual fund?",
                              "https://www.reddit.com/r/Test/comments/keepme/title", "2026-09-18T05:00:00Z")
            result = FilterResult("relevant", "Mutual Funds", 8, ["mutual fund"], "Relevant threshold met")
            db.upsert_post(conn, entry, result, "2026-09-18T05:00:00+00:00", "2026-09-18T05:00:00Z")
            db.set_checkpoint(conn, "Test", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
            db.save_feed_state(conn, "Test", "new", '"old"', "lm", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), 200)
            conn.close()
            fake_plist = base / "no-scheduler.plist"
            with mock.patch.object(cli, "_run_local_tests", return_value=0), mock.patch.object(cli, "_plist_path", return_value=fake_plist):
                with __import__("contextlib").redirect_stdout(io.StringIO()):
                    rc = cli.command_upgrade(SimpleNamespace(config=str(config_path)))
            self.assertEqual(0, rc)
            upgraded = load_config(config_path)
            self.assertEqual(2, upgraded["version"])
            upgraded_filter = Path(upgraded["filter_profile"]).resolve()
            self.assertEqual((ROOT / "filters" / "finance.json").resolve(), upgraded_filter)
            self.assertTrue(upgraded_filter.is_file())
            conn = db.connect(runtime / "state.sqlite3"); db.init_db(conn)
            try:
                self.assertIsNotNone(db.get_post(conn, "keepme"))
                cp = datetime.fromisoformat(db.get_checkpoint(conn, "Test").replace("Z", "+00:00"))
                age = datetime.now(timezone.utc) - cp
                self.assertGreaterEqual(age.total_seconds(), 23.9 * 3600)
                self.assertIsNone(db.get_feed_state(conn, "Test", "new"))
            finally:
                conn.close()


class SnapshotCheckpointTests(unittest.TestCase):
    def test_69_new_feed_checkpoint_matches_processed_response_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = cfg_for(td)
            conn = db.connect(Path(td) / "snapshot.sqlite3"); db.init_db(conn)
            db.set_checkpoint(conn, "Test", "2026-09-18T05:00:00Z")
            clock = MutableClock(datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc))

            def first_response(url, conditional):
                # Simulate the global rate guard/network time: the feed response
                # is obtained five minutes after the overall run began and
                # includes a post published during that interval.
                clock.value = datetime(2026, 9, 18, 6, 5, tzinfo=timezone.utc)
                return FetchResult(
                    200,
                    atom([("duringrun", "2026-09-18T06:03:00Z", "Should I switch my mutual fund?",
                          "My portfolio expense ratio is high")]),
                    {"ETag": '"snapshot-1"'}, url,
                )

            http = FakeHTTP([first_response, FetchResult(304, b"", {"ETag": '"snapshot-1"'}, "u")])
            collector = Collector(cfg, conn, progress=lambda x: None, http=http, now_func=clock)
            anchor = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)
            outcome = collector._run_feed("Test", "new", "2026-09-18T06:00:00+00:00", "2026-09-18", anchor)
            self.assertEqual(1, outcome["evaluated"])
            self.assertIsNotNone(db.get_post(conn, "duringrun"))
            self.assertEqual("2026-09-18T06:05:00Z", db.get_checkpoint(conn, "Test"))
            self.assertEqual('"snapshot-1"', db.get_feed_state(conn, "Test", "new")["etag"])

            # If the exact snapshot is unchanged later, a 304 is now safe:
            # every entry that was in the cached snapshot was processed.
            clock.value = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
            collector._run_feed("Test", "new", "2026-09-18T09:00:00+00:00", "2026-09-18", clock.value)
            self.assertEqual("2026-09-18T09:00:00Z", db.get_checkpoint(conn, "Test"))
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM posts WHERE post_id='duringrun'").fetchone()[0])
            conn.close()
