import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reddit_monitor import db
from reddit_monitor.collector import Collector
from reddit_monitor.comment_collector import select_comment_candidates
from reddit_monitor.config import default_config
from reddit_monitor.engagement import engagement_from_ranks
from reddit_monitor.models import FeedEntry, FilterResult
from reddit_monitor.reports import generate_reports
from reddit_monitor.xlsx_writer import validate_xlsx


class RisingWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.conn = db.connect(root / 'state.sqlite3')
        db.init_db(self.conn)
        self.cfg = default_config('test@example.com', root / 'output', subreddits=['Test'],
                                  runtime_dir=root / 'runtime', filter_profile=Path(__file__).parents[1] / 'filters' / 'finance.json')
        self.now = datetime.now(timezone.utc)
        self.run = self.now.astimezone().isoformat(timespec='seconds')
        self.day = self.now.astimezone().date().isoformat()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def post(self, pid, age_hours, classification, title='Should I review this mutual fund?'):
        stamp = (self.now - timedelta(hours=age_hours)).isoformat().replace('+00:00', 'Z')
        entry = FeedEntry(pid, 'Test', title, 'Mutual fund question',
                          f'https://www.reddit.com/r/Test/comments/{pid}/post', stamp)
        db.upsert_post(self.conn, entry, FilterResult(classification, 'Mutual Funds', 8), self.run, stamp)
        return entry

    def test_finance_filter_catches_missed_decisions_without_account_noise(self):
        from reddit_monitor.filter_engine import RelevanceEngine
        engine = RelevanceEngine.from_file(Path(__file__).parents[1] / 'filters' / 'finance.json')
        for title in ('What do you guys think about Liquid Funds?',
                      'Help me decide - should I buy a second home',
                      'Starting SIP as a Student',
                      'My Parents have money sitting in a savings account'):
            self.assertIn(engine.classify(title, '').classification, ('relevant', 'review'), title)
        self.assertEqual('excluded', engine.classify('Can I open HDFC Salary Platinum Account?', '').classification)
        self.assertEqual('excluded', engine.classify('Should I hold GOOG stock?', '').classification)

    def test_rising_is_collected_and_ranked(self):
        collector = Collector(self.cfg, self.conn, progress=lambda _: None, now_func=lambda: self.now)
        self.assertIn(('Test', 'rising'), collector._feed_plan(self.now))
        self.assertIn('/r/Test/rising/.rss?', collector._feed_url('Test', 'rising'))
        entry = self.post('rising1', 9, 'relevant')
        collector._process_entry(entry, 'rising', 7, self.run, self.day, self.now.isoformat())
        row = db.get_post(self.conn, 'rising1')
        self.assertEqual(7, row['rising_rank'])
        self.assertEqual(('HIGH', 'Rising #7'), engagement_from_ranks(rising_rank=7))

    def test_fresh_relevant_and_review_posts_precede_ranked_research(self):
        recent = self.post('recent1', 20, 'relevant')
        review = self.post('review1', 2, 'review')
        rising = self.post('rising2', 48, 'relevant')
        hot = self.post('hot1', 96, 'relevant')
        excluded = self.post('excluded1', 2, 'excluded')
        for feed, entries in [('new', [recent, review, excluded]), ('rising', [rising]), ('hot', [hot])]:
            db.record_feed_snapshot(self.conn, self.run, self.now.isoformat(), 'Test', feed, entries)
        candidates = select_comment_candidates(self.conn, self.cfg, self.day)
        self.assertEqual(['recent1', 'review1', 'rising2', 'hot1'], [row['post_id'] for row, _ in candidates])
        self.assertIn('Review under 24h', candidates[1][1])
        self.assertIn('RISING', candidates[2][1])
        db.save_comment_fetch(self.conn, self.day, 'recent1', self.now.isoformat(), 200, 'sample',
                              [{'comment_id': 'c1', 'permalink': recent.permalink + '/c1', 'body': 'Useful comment',
                                'published_at': self.now.isoformat()}])
        self.assertNotIn('recent1', [row['post_id'] for row, _ in select_comment_candidates(self.conn, self.cfg, self.day)])

    def test_dropped_ranking_post_is_not_fetched_as_active(self):
        old = self.post('cooled1', 96, 'relevant')
        db.record_feed_snapshot(self.conn, self.run, self.now.isoformat(), 'Test', 'hot', [old])
        later = (self.now + timedelta(seconds=1)).astimezone().isoformat(timespec='seconds')
        db.record_feed_snapshot(self.conn, later, (self.now + timedelta(seconds=1)).isoformat(), 'Test', 'hot', [])
        self.assertNotIn('cooled1', [row['post_id'] for row, _ in select_comment_candidates(self.conn, self.cfg, self.day)])

    def test_latest_reply_tab_includes_review_and_gates_image(self):
        entry = self.post('image1', 1, 'review', 'Review my fund allocation')
        entry = FeedEntry(entry.post_id, entry.subreddit, entry.title,
                          'Please review. Attached media URLs (not inspected): https://preview.redd.it/x.png',
                          entry.permalink, entry.published)
        db.upsert_post(self.conn, entry, FilterResult('review', 'Mutual Funds', 8), self.run, self.now.isoformat())
        db.record_feed_snapshot(self.conn, self.run, self.now.isoformat(), 'Test', 'new', [entry])
        db.mark_observation_day(self.conn, self.day, entry.post_id, self.run, 'new')
        db.insert_run(self.conn, self.run, 'new/Test', '', self.now.isoformat(), 200, 1, 0, 1, 0, 'OK')
        reports = generate_reports(self.conn, self.cfg['output_dir'], self.day, organized=True, cfg=self.cfg)
        summary = validate_xlsx(reports['review_workbook'])
        self.assertEqual(1, summary['row_counts'][1])
        self.assertEqual(1, summary['row_counts'][2])
        import zipfile
        with zipfile.ZipFile(reports['review_workbook']) as z:
            latest = z.read('xl/worksheets/sheet2.xml').decode()
        self.assertIn('HOLD: source image unreadable', latest)
        self.assertIn('REVIEW', latest)


if __name__ == '__main__':
    unittest.main()
