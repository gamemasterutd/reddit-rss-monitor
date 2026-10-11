# Architecture

## Runtime modules

- `config.py` — configuration defaults, compatibility defaults, validation
- `http_client.py` — sequential HTTP, conditional headers, persistent global request governor/cooldowns
- `comment_collector.py` — selective per-thread comment RSS parsing and bounded collection through the same governor
- `feed_parser.py` — Atom/RSS parsing and canonical Reddit identity
- `filter_engine.py` — configurable weighted relevance scoring
- `db.py` — SQLite schema, migrations, checkpoints, feed state, posts, daily observations, run records
- `collector.py` — feed due-state, 24-hour bootstrap, coverage proof, dedupe, ranking enrichment
- `engagement.py` — HOT/RISING/TOP DAY/TOP WEEK evidence to HIGH/MEDIUM/LOW
- `xlsx_writer.py` — minimal standard-library OOXML workbook writer
- `reports.py` — daily audits and the monthly review-workbook handoff
- `review_export.py` — concise human review, Work batch, and timestamped feed snapshot CSV
- `review_workbook.py` — one monthly XLSX with latest replies, comment coverage, content evidence, placements, and run history
- `drive_sync.py` — optional one-file staging via Google Drive for desktop or direct upload via rclone
- `process_lock.py` — non-overlap lock
- `cli.py` — run/status/scheduler/upgrade/uninstall commands
- `setup_wizard.py` — dependency-free macOS setup and self-test

## Collection schedule

`launchd` wakes the monitor every three hours at minute 17. The collector then decides which feeds are due from persisted successful feed timestamps:

- NEW: every run, per subreddit
- HOT: every 6 hours, per subreddit
- RISING: every 6 hours, per subreddit
- TOP DAY: every 12 hours, per subreddit
- TOP WEEK: every 12 hours, per subreddit

A fresh database has no ranking feed state, so the first run checks all five feed types for every subreddit. NEW scans the previous 24 hours by default. Ranking feeds are not tied to a particular clock hour; if the Mac sleeps through a run, the next invocation sees the stale persisted state and performs the missed refresh. BEST is a personalized home ranking and is not collected as a comparable subreddit feed.

## Data-flow invariants

1. NEW discovery checkpoints are independent per subreddit.
2. ETag/Last-Modified state is independent per subreddit and feed type.
3. A capacity-truncated NEW feed that cannot reach the required checkpoint cannot advance that checkpoint or overwrite successful validators.
4. A NEW response below configured feed capacity is not automatically a gap merely because its oldest post is newer than the scan start; it may simply be a quiet community.
5. Post identity is unique by Reddit post ID with canonical permalink uniqueness as a second database constraint.
6. NEW/HOT/RISING/TOP DAY/TOP WEEK appearances update the same post record.
7. Daily report membership is separate from lifetime post identity. A post first seen yesterday can appear today if Reddit still surfaces it in a ranking feed.
8. The daily report retains the strongest engagement evidence observed that day; the post table tracks the latest ranking state.
9. All evaluated posts, including excluded ones, are recorded in daily audits and the monthly workbook's `All Posts` tab. The monthly workbook is the human handoff; internal daily files remain for recovery and diagnostics.
10. A 403/429 response stops the remaining run immediately.
11. Global cooldown is persisted so later manual/scheduled runs make zero Reddit requests while active.
12. Every successful NEW checkpoint is tied to that feed response's own snapshot time, so posts arriving while an earlier rate-guarded request is waiting cannot be hidden behind a later conditional 304.
13. Reports are rebuilt atomically from SQLite, so repeated feed appearances do not create duplicate output rows.
14. Each successful 200/304 feed refresh records a separate ranked snapshot with the local run time and UTC observation time. An unchanged 304 retains its prior visible order without claiming new posts.
15. Every relevant or review-classified post under 24 hours old enters the comment queue, including posts outside the top ten. Older active ranking posts use remaining capacity. A saved nonempty comment sample is not routinely fetched again.
16. One `Reddit-Stream-YYYY-MM.xlsx` file is rebuilt from SQLite and uploaded to the same Drive filename after each completed run. AI drafts remain separate from the scraper source.

## First-run coverage

The first NEW scan requires a 24-hour window. With `limit=100`, coverage is considered proven when either:

- the oldest returned item reaches or predates the required scan start; or
- Reddit returns fewer than the configured maximum, so there is no evidence that feed capacity truncated older entries.

If the feed is at capacity and still does not reach the required start, the run records `GAP`, stores visible posts, and refuses to advance the checkpoint or save the new conditional validators.

## Engagement

No numeric upvote/comment count is fabricated. Evidence comes only from ranking feeds:

- HIGH: top position in a ranking feed, or appearance in two or more ranking feeds
- MEDIUM: lower-ranked appearance in HOT/RISING/TOP DAY/TOP WEEK
- LOW: NEW-only

Evidence is written as text such as `Hot #4; Rising #2; Top day #8`. Ranking position is not a vote or comment count.
