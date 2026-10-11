import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utcnow():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _columns(conn, table):
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}


def _add_column_if_missing(conn, table, column, definition):
    if column not in _columns(conn, table):
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, definition))


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS governor (
            id INTEGER PRIMARY KEY CHECK (id=1),
            last_request_at TEXT,
            cooldown_until TEXT,
            consecutive_429 INTEGER NOT NULL DEFAULT 0,
            last_http_status INTEGER,
            last_coverage_issue TEXT
        );
        INSERT OR IGNORE INTO governor(id) VALUES(1);

        CREATE TABLE IF NOT EXISTS checkpoints (
            subreddit TEXT PRIMARY KEY COLLATE NOCASE,
            last_new_success TEXT
        );

        CREATE TABLE IF NOT EXISTS feed_state (
            subreddit TEXT NOT NULL COLLATE NOCASE,
            feed_type TEXT NOT NULL,
            etag TEXT,
            last_modified TEXT,
            last_success_at TEXT,
            last_status INTEGER,
            PRIMARY KEY(subreddit, feed_type)
        );

        CREATE TABLE IF NOT EXISTS posts (
            post_id TEXT PRIMARY KEY,
            permalink TEXT NOT NULL UNIQUE,
            subreddit TEXT NOT NULL COLLATE NOCASE,
            post_time TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            topic TEXT NOT NULL DEFAULT '',
            classification TEXT NOT NULL DEFAULT 'excluded',
            review_reason TEXT NOT NULL DEFAULT '',
            score REAL NOT NULL DEFAULT 0,
            matched_terms TEXT NOT NULL DEFAULT '[]',
            first_detected_run TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            hot_rank INTEGER,
            rising_rank INTEGER,
            top_day_rank INTEGER,
            top_week_rank INTEGER,
            engagement TEXT NOT NULL DEFAULT 'LOW',
            signal_evidence TEXT NOT NULL DEFAULT 'New only'
        );

        CREATE TABLE IF NOT EXISTS post_report_days (
            report_date TEXT NOT NULL,
            post_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            detected_run TEXT,
            peak_engagement TEXT,
            peak_signal_evidence TEXT,
            PRIMARY KEY(report_date, post_id),
            FOREIGN KEY(post_id) REFERENCES posts(post_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS post_observation_days (
            report_date TEXT NOT NULL,
            post_id TEXT NOT NULL,
            first_observed_run TEXT NOT NULL,
            last_observed_run TEXT NOT NULL,
            feeds_seen TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY(report_date, post_id),
            FOREIGN KEY(post_id) REFERENCES posts(post_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS feed_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            subreddit TEXT NOT NULL COLLATE NOCASE,
            feed_type TEXT NOT NULL,
            http_status INTEGER NOT NULL,
            copied_from_cache INTEGER NOT NULL DEFAULT 0,
            UNIQUE(run_time, subreddit, feed_type)
        );
        CREATE TABLE IF NOT EXISTS feed_snapshot_items (
            snapshot_id INTEGER NOT NULL,
            post_id TEXT NOT NULL,
            rank INTEGER NOT NULL,
            change_status TEXT NOT NULL,
            title TEXT NOT NULL,
            post_time TEXT NOT NULL,
            permalink TEXT NOT NULL,
            PRIMARY KEY(snapshot_id, post_id),
            FOREIGN KEY(snapshot_id) REFERENCES feed_snapshots(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time TEXT NOT NULL,
            feed_subreddit TEXT NOT NULL,
            scan_window_start TEXT,
            scan_window_end TEXT,
            http_status TEXT NOT NULL,
            entries_seen INTEGER NOT NULL DEFAULT 0,
            relevant INTEGER NOT NULL DEFAULT 0,
            needs_review INTEGER NOT NULL DEFAULT 0,
            excluded INTEGER NOT NULL DEFAULT 0,
            coverage_status TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_runs_time ON runs(run_time);
        CREATE INDEX IF NOT EXISTS idx_posts_subreddit_time ON posts(subreddit, post_time);
        CREATE INDEX IF NOT EXISTS idx_report_day ON post_report_days(report_date);
        CREATE INDEX IF NOT EXISTS idx_observation_day ON post_observation_days(report_date);
        CREATE INDEX IF NOT EXISTS idx_snapshots_feed ON feed_snapshots(subreddit, feed_type, run_time);
        CREATE INDEX IF NOT EXISTS idx_snapshot_items_post ON feed_snapshot_items(post_id);
        CREATE TABLE IF NOT EXISTS comment_state (
            post_id TEXT PRIMARY KEY,
            last_attempt_at TEXT,
            last_success_at TEXT,
            last_status INTEGER,
            etag TEXT,
            last_modified TEXT,
            note TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(post_id) REFERENCES posts(post_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS post_comments (
            comment_id TEXT PRIMARY KEY,
            post_id TEXT NOT NULL,
            permalink TEXT NOT NULL,
            body TEXT NOT NULL,
            published_at TEXT,
            observed_at TEXT NOT NULL,
            FOREIGN KEY(post_id) REFERENCES posts(post_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_post_comments_post ON post_comments(post_id);
        CREATE TABLE IF NOT EXISTS comment_fetches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date TEXT NOT NULL,
            post_id TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            status INTEGER NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(post_id) REFERENCES posts(post_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_comment_fetches_day ON comment_fetches(report_date);
        """
    )
    # v1 -> v2 in-place migrations. SQLite lacks ADD COLUMN IF NOT EXISTS.
    _add_column_if_missing(conn, "posts", "top_day_rank", "INTEGER")
    _add_column_if_missing(conn, "posts", "rising_rank", "INTEGER")
    _add_column_if_missing(conn, "post_report_days", "detected_run", "TEXT")
    _add_column_if_missing(conn, "post_report_days", "peak_engagement", "TEXT")
    _add_column_if_missing(conn, "post_report_days", "peak_signal_evidence", "TEXT")
    _add_column_if_missing(conn, "comment_fetches", "selection_reason", "TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS post_observation_days (
            report_date TEXT NOT NULL,
            post_id TEXT NOT NULL,
            first_observed_run TEXT NOT NULL,
            last_observed_run TEXT NOT NULL,
            feeds_seen TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY(report_date, post_id),
            FOREIGN KEY(post_id) REFERENCES posts(post_id) ON DELETE CASCADE
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_observation_day ON post_observation_days(report_date)")
    conn.commit()


def comment_state_for_post(conn, post_id):
    return conn.execute("SELECT * FROM comment_state WHERE post_id=?", (post_id,)).fetchone()


def comments_for_post(conn, post_id):
    return conn.execute(
        "SELECT * FROM post_comments WHERE post_id=? ORDER BY published_at DESC, comment_id DESC",
        (post_id,),
    ).fetchall()


def comment_fetch_count_for_day(conn, report_date):
    return conn.execute("SELECT COUNT(*) FROM comment_fetches WHERE report_date=?", (report_date,)).fetchone()[0]


def save_comment_fetch(conn, report_date, post_id, attempted_at, status, note, comments=None,
                       etag=None, last_modified=None, selection_reason=""):
    """A successful 200 replaces the visible comment sample; failures preserve it."""
    with conn:
        conn.execute(
            "INSERT INTO comment_fetches(report_date,post_id,attempted_at,status,note,selection_reason) VALUES(?,?,?,?,?,?)",
            (report_date, post_id, attempted_at, status, note, selection_reason),
        )
        previous = comment_state_for_post(conn, post_id)
        last_success = (attempted_at if status == 200 or
                        (status == 304 and previous and previous["last_success_at"])
                        else (previous["last_success_at"] if previous else None))
        conn.execute(
            """INSERT INTO comment_state(post_id,last_attempt_at,last_success_at,last_status,etag,last_modified,note)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET
               last_attempt_at=excluded.last_attempt_at,
               last_success_at=excluded.last_success_at,
               last_status=excluded.last_status,
               etag=excluded.etag,
               last_modified=excluded.last_modified,
               note=excluded.note""",
            (post_id, attempted_at, last_success, status,
             etag if status == 200 else (previous["etag"] if previous else None),
             last_modified if status == 200 else (previous["last_modified"] if previous else None), note),
        )
        if status == 200 and comments is not None:
            conn.execute("DELETE FROM post_comments WHERE post_id=?", (post_id,))
            for comment in comments:
                conn.execute(
                    """INSERT INTO post_comments(comment_id,post_id,permalink,body,published_at,observed_at)
                       VALUES(?,?,?,?,?,?)""",
                    (comment["comment_id"], post_id, comment["permalink"], comment["body"],
                     comment["published_at"], attempted_at),
                )


def get_governor(conn):
    return conn.execute("SELECT * FROM governor WHERE id=1").fetchone()


def update_governor(conn, **values):
    if not values:
        return
    allowed = {"last_request_at", "cooldown_until", "consecutive_429", "last_http_status", "last_coverage_issue"}
    bad = set(values) - allowed
    if bad:
        raise ValueError("Invalid governor fields: %s" % bad)
    sql = "UPDATE governor SET " + ", ".join("%s=?" % k for k in values) + " WHERE id=1"
    conn.execute(sql, list(values.values()))
    conn.commit()


def get_checkpoint(conn, subreddit):
    row = conn.execute("SELECT last_new_success FROM checkpoints WHERE subreddit=?", (subreddit,)).fetchone()
    return row[0] if row else None


def set_checkpoint(conn, subreddit, timestamp):
    conn.execute(
        "INSERT INTO checkpoints(subreddit,last_new_success) VALUES(?,?) "
        "ON CONFLICT(subreddit) DO UPDATE SET last_new_success=excluded.last_new_success",
        (subreddit, timestamp),
    )
    conn.commit()


def get_feed_state(conn, subreddit, feed_type):
    return conn.execute(
        "SELECT * FROM feed_state WHERE subreddit=? AND feed_type=?",
        (subreddit, feed_type),
    ).fetchone()


def save_feed_state(conn, subreddit, feed_type, etag, last_modified, success_at, status):
    conn.execute(
        """INSERT INTO feed_state(subreddit,feed_type,etag,last_modified,last_success_at,last_status)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(subreddit,feed_type) DO UPDATE SET
             etag=excluded.etag,
             last_modified=excluded.last_modified,
             last_success_at=excluded.last_success_at,
             last_status=excluded.last_status""",
        (subreddit, feed_type, etag, last_modified, success_at, status),
    )
    conn.commit()


def update_feed_status_only(conn, subreddit, feed_type, status):
    conn.execute(
        """INSERT INTO feed_state(subreddit,feed_type,last_status) VALUES(?,?,?)
           ON CONFLICT(subreddit,feed_type) DO UPDATE SET last_status=excluded.last_status""",
        (subreddit, feed_type, status),
    )
    conn.commit()


def feed_due(conn, subreddit, feed_type, now, interval_hours):
    row = get_feed_state(conn, subreddit, feed_type)
    if not row or not row["last_success_at"]:
        return True
    last = _parse_iso(row["last_success_at"])
    if last is None:
        return True
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - last.astimezone(timezone.utc)).total_seconds() >= float(interval_hours) * 3600


def get_post(conn, post_id):
    return conn.execute("SELECT * FROM posts WHERE post_id=?", (post_id,)).fetchone()


def upsert_post(conn, entry, result, detected_run, now, hot_rank=None, top_day_rank=None,
                top_week_rank=None, engagement="LOW", evidence="New only", rising_rank=None):
    existing = get_post(conn, entry.post_id)
    if existing is None:
        conn.execute(
            """INSERT INTO posts(
                post_id,permalink,subreddit,post_time,title,description,topic,classification,review_reason,
                score,matched_terms,first_detected_run,first_seen_at,last_seen_at,hot_rank,top_day_rank,
                top_week_rank,rising_rank,engagement,signal_evidence
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                entry.post_id, entry.permalink, entry.subreddit, entry.published, entry.title, entry.description,
                result.topic, result.classification, result.review_reason, result.score,
                json.dumps(result.matched_terms, ensure_ascii=False), detected_run, now, now,
                hot_rank, top_day_rank, top_week_rank, rising_rank, engagement, evidence,
            ),
        )
        conn.commit()
        return True, None
    conn.execute(
        """UPDATE posts SET
            permalink=?, title=?, description=?, topic=?, classification=?, review_reason=?, score=?, matched_terms=?,
            last_seen_at=?, hot_rank=?, top_day_rank=?, top_week_rank=?, rising_rank=?, engagement=?, signal_evidence=?
           WHERE post_id=?""",
        (
            entry.permalink, entry.title, entry.description, result.topic, result.classification, result.review_reason,
            result.score, json.dumps(result.matched_terms, ensure_ascii=False), now,
            hot_rank if hot_rank is not None else existing["hot_rank"],
            top_day_rank if top_day_rank is not None else existing["top_day_rank"],
            top_week_rank if top_week_rank is not None else existing["top_week_rank"],
            rising_rank if rising_rank is not None else existing["rising_rank"],
            engagement, evidence, entry.post_id,
        ),
    )
    conn.commit()
    return False, existing


def set_post_ranks(conn, post_id, hot_rank, top_day_rank, top_week_rank, engagement, evidence, now, rising_rank=None):
    conn.execute(
        "UPDATE posts SET hot_rank=?, top_day_rank=?, top_week_rank=?, rising_rank=?, engagement=?, signal_evidence=?, last_seen_at=? WHERE post_id=?",
        (hot_rank, top_day_rank, top_week_rank, rising_rank, engagement, evidence, now, post_id),
    )
    conn.commit()


def _engagement_value(level):
    return {"LOW": 1, "MEDIUM": 2, "HIGH": 3}.get(level or "", 0)


def mark_report_day(conn, report_date, post_id, reason, detected_run=None, engagement=None, evidence=None):
    row = conn.execute(
        "SELECT peak_engagement, peak_signal_evidence FROM post_report_days WHERE report_date=? AND post_id=?",
        (report_date, post_id),
    ).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO post_report_days(
                report_date,post_id,reason,detected_run,peak_engagement,peak_signal_evidence
            ) VALUES(?,?,?,?,?,?)""",
            (report_date, post_id, reason, detected_run, engagement, evidence),
        )
    elif _engagement_value(engagement) > _engagement_value(row["peak_engagement"]):
        conn.execute(
            "UPDATE post_report_days SET peak_engagement=?, peak_signal_evidence=? WHERE report_date=? AND post_id=?",
            (engagement, evidence, report_date, post_id),
        )
    elif _engagement_value(engagement) == _engagement_value(row["peak_engagement"]) and evidence and evidence != row["peak_signal_evidence"]:
        # Same level but richer ranking evidence (for example HOT plus TOP DAY).
        old = row["peak_signal_evidence"] or ""
        if len(evidence) > len(old):
            conn.execute(
                "UPDATE post_report_days SET peak_signal_evidence=? WHERE report_date=? AND post_id=?",
                (evidence, report_date, post_id),
            )
    conn.commit()


def mark_observation_day(conn, report_date, post_id, run_time, feed_type):
    row = conn.execute(
        "SELECT feeds_seen FROM post_observation_days WHERE report_date=? AND post_id=?",
        (report_date, post_id),
    ).fetchone()
    if row:
        try:
            feeds = json.loads(row["feeds_seen"] or "[]")
        except json.JSONDecodeError:
            feeds = []
        if feed_type not in feeds:
            feeds.append(feed_type)
        conn.execute(
            "UPDATE post_observation_days SET last_observed_run=?, feeds_seen=? WHERE report_date=? AND post_id=?",
            (run_time, json.dumps(feeds), report_date, post_id),
        )
    else:
        conn.execute(
            "INSERT INTO post_observation_days(report_date,post_id,first_observed_run,last_observed_run,feeds_seen) VALUES(?,?,?,?,?)",
            (report_date, post_id, run_time, run_time, json.dumps([feed_type])),
        )
    conn.commit()


def record_feed_snapshot(conn, run_time, observed_at, subreddit, feed_type, entries, http_status=200):
    """Store the visible order of one feed at one time, including its changes."""
    previous = conn.execute(
        """SELECT id FROM feed_snapshots WHERE subreddit=? AND feed_type=?
           ORDER BY id DESC LIMIT 1""", (subreddit, feed_type),
    ).fetchone()
    previous_ids = set()
    if previous:
        previous_ids = {row[0] for row in conn.execute(
            "SELECT post_id FROM feed_snapshot_items WHERE snapshot_id=?", (previous["id"],)
        )}
    if http_status == 304:
        if not previous:
            return None
        items = conn.execute(
            """SELECT post_id, rank, title, post_time, permalink FROM feed_snapshot_items
               WHERE snapshot_id=? ORDER BY rank""", (previous["id"],)
        ).fetchall()
    else:
        items = entries
    with conn:
        cur = conn.execute(
            """INSERT INTO feed_snapshots(run_time, observed_at, subreddit, feed_type, http_status, copied_from_cache)
               VALUES(?,?,?,?,?,?)""",
            (run_time, observed_at, subreddit, feed_type, http_status, int(http_status == 304)),
        )
        snapshot_id = cur.lastrowid
        for rank, item in enumerate(items, 1):
            post_id = item["post_id"] if http_status == 304 else item.post_id
            if http_status == 304 or post_id in previous_ids:
                change = "STAYED"
            elif not previous:
                change = "BASELINE"
            else:
                ever_seen = conn.execute(
                    """SELECT 1 FROM feed_snapshot_items i JOIN feed_snapshots s ON s.id=i.snapshot_id
                       WHERE s.subreddit=? AND s.feed_type=? AND i.post_id=? LIMIT 1""",
                    (subreddit, feed_type, post_id),
                ).fetchone()
                change = "RETURNED" if ever_seen else "ENTERED"
            conn.execute(
                """INSERT INTO feed_snapshot_items(snapshot_id,post_id,rank,change_status,title,post_time,permalink)
                   VALUES(?,?,?,?,?,?,?)""",
                (snapshot_id, post_id, rank, change,
                 item["title"] if http_status == 304 else item.title,
                 item["post_time"] if http_status == 304 else item.published,
                 item["permalink"] if http_status == 304 else item.permalink),
            )
    return snapshot_id


def snapshot_items_for_day(conn, report_date):
    return conn.execute(
        """SELECT s.run_time, s.observed_at, s.subreddit, s.feed_type, s.http_status,
                  s.copied_from_cache, i.post_id, i.rank, i.change_status, i.title,
                  i.post_time, i.permalink, p.classification, p.topic, p.description,
                  p.score, p.engagement, p.signal_evidence
           FROM feed_snapshots s JOIN feed_snapshot_items i ON i.snapshot_id=s.id
           LEFT JOIN posts p ON p.post_id=i.post_id
           WHERE substr(s.run_time,1,10)=?
           ORDER BY s.id, i.rank""", (report_date,),
    ).fetchall()


def latest_snapshot_items(conn, report_date, feed_type):
    """The last known snapshot for each subreddit on or before this local day."""
    return conn.execute(
        """SELECT s.run_time, s.observed_at, s.subreddit, s.feed_type, s.http_status,
                  s.copied_from_cache, i.post_id, i.rank, i.change_status, i.title,
                  i.post_time, i.permalink, p.classification, p.topic, p.description,
                  p.score, p.engagement, p.signal_evidence
           FROM feed_snapshots s JOIN feed_snapshot_items i ON i.snapshot_id=s.id
           LEFT JOIN posts p ON p.post_id=i.post_id
           WHERE s.feed_type=? AND substr(s.run_time,1,10)<=?
             AND s.id=(SELECT s2.id FROM feed_snapshots s2
                       WHERE s2.subreddit=s.subreddit AND s2.feed_type=s.feed_type
                         AND substr(s2.run_time,1,10)<=?
                       ORDER BY s2.id DESC LIMIT 1)
           ORDER BY s.subreddit, i.rank""", (feed_type, report_date, report_date),
    ).fetchall()


def mark_unchanged_ranking_feed_for_day(conn, subreddit, feed_type, report_date, run_time):
    column = {"hot": "hot_rank", "rising": "rising_rank", "top_day": "top_day_rank", "top_week": "top_week_rank"}.get(feed_type)
    if not column:
        return 0
    rows = conn.execute(
        "SELECT post_id, classification FROM posts WHERE subreddit=? AND %s IS NOT NULL" % column,
        (subreddit,),
    ).fetchall()
    for row in rows:
        mark_observation_day(conn, report_date, row["post_id"], run_time, feed_type)
        if row["classification"] in ("relevant", "review"):
            post = get_post(conn, row["post_id"])
            mark_report_day(conn, report_date, row["post_id"], "ranking_unchanged_today", run_time,
                            post["engagement"], post["signal_evidence"])
    return len(rows)


def insert_run(conn, run_time, feed_subreddit, scan_window_start, scan_window_end, http_status,
               entries_seen, relevant, needs_review, excluded, coverage_status, notes=""):
    conn.execute(
        """INSERT INTO runs(run_time,feed_subreddit,scan_window_start,scan_window_end,http_status,
        entries_seen,relevant,needs_review,excluded,coverage_status,notes)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (run_time, feed_subreddit, scan_window_start, scan_window_end, str(http_status), entries_seen,
         relevant, needs_review, excluded, coverage_status, notes),
    )
    conn.commit()


def posts_for_report_day(conn, report_date, classification):
    return conn.execute(
        """SELECT p.*, COALESCE(d.detected_run,p.first_detected_run) AS report_detected_run,
                  COALESCE(d.peak_engagement,p.engagement) AS report_engagement,
                  COALESCE(d.peak_signal_evidence,p.signal_evidence) AS report_signal_evidence
           FROM posts p JOIN post_report_days d ON d.post_id=p.post_id
           WHERE d.report_date=? AND p.classification=?
           ORDER BY report_detected_run ASC, p.post_time ASC""",
        (report_date, classification),
    ).fetchall()


def posts_observed_on_day(conn, report_date):
    return conn.execute(
        """SELECT p.*, o.first_observed_run, o.last_observed_run, o.feeds_seen
           FROM posts p JOIN post_observation_days o ON o.post_id=p.post_id
           WHERE o.report_date=?
           ORDER BY o.first_observed_run ASC, p.post_time ASC""",
        (report_date,),
    ).fetchall()


def runs_for_day(conn, report_date):
    prefix = report_date + "%"
    return conn.execute("SELECT * FROM runs WHERE run_time LIKE ? ORDER BY id", (prefix,)).fetchall()


def last_run_info(conn):
    last_success = conn.execute(
        "SELECT MAX(run_time) FROM runs WHERE http_status IN ('200','304') AND coverage_status != 'GAP'"
    ).fetchone()[0]
    last_run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return last_success, last_run


def clear_rank_for_subreddit(conn, subreddit, feed_type):
    column = {"hot": "hot_rank", "rising": "rising_rank", "top_day": "top_day_rank", "top_week": "top_week_rank"}.get(feed_type)
    if not column:
        raise ValueError("Unsupported rank feed: %s" % feed_type)
    conn.execute("UPDATE posts SET %s=NULL WHERE subreddit=?" % column, (subreddit,))
    conn.commit()


def posts_for_subreddit(conn, subreddit):
    return conn.execute("SELECT * FROM posts WHERE subreddit=?", (subreddit,)).fetchall()


def prepare_rebootstrap(conn, subreddits, now, hours=24):
    """Rewind discovery conservatively and clear validators for a one-time v1 -> v2 bootstrap."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    target = now.astimezone(timezone.utc) - timedelta(hours=float(hours))
    target_iso = target.isoformat().replace("+00:00", "Z")
    for subreddit in subreddits:
        current = get_checkpoint(conn, subreddit)
        if current is None or (_parse_iso(current) and _parse_iso(current) > target):
            set_checkpoint(conn, subreddit, target_iso)
        # Clear conditional validators so a rewound NEW checkpoint can never be
        # incorrectly satisfied by a 304 based on the newer v1 scan state.
        conn.execute(
            "DELETE FROM feed_state WHERE subreddit=? AND feed_type IN ('new','hot','rising','top_day','top_week')",
            (subreddit,),
        )
        # Old ranking evidence is not carried into the new bootstrap. If the
        # bootstrap is interrupted before rankings refresh, stale v1 ranks must
        # not masquerade as current engagement.
        conn.execute(
            "UPDATE posts SET hot_rank=NULL, rising_rank=NULL, top_day_rank=NULL, top_week_rank=NULL, engagement='LOW', signal_evidence='New only' WHERE subreddit=?",
            (subreddit,),
        )
    conn.commit()
    return target_iso
