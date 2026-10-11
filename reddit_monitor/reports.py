import csv
import json
import shutil
from pathlib import Path

from .db import posts_for_report_day, posts_observed_on_day, runs_for_day
from .review_export import generate_review_exports
from .review_workbook import generate_review_workbook, FILE_NAME as REVIEW_FILE_NAME
from .xlsx_writer import write_xlsx, validate_xlsx

RELEVANT_HEADERS = [
    "Detected Run", "Subreddit", "Post Time", "Title", "Post Description", "Topic",
    "Engagement", "Signal Evidence", "Matched Terms", "Reddit Link"
]
REVIEW_HEADERS = [
    "Detected Run", "Subreddit", "Post Time", "Title", "Description", "Potential Topic",
    "Review Reason", "Engagement", "Reddit Link"
]
RUN_HEADERS = [
    "Run Time", "Feed/Subreddit", "Scan Window Start", "Scan Window End", "HTTP Status",
    "Entries Seen", "Relevant", "Needs Review", "Excluded", "Coverage Status", "Notes"
]
AUDIT_HEADERS = [
    "First Observed Run", "Last Observed Run", "Subreddit", "Post Time", "Title", "Post Description",
    "Classification", "Topic", "Classification Reason", "Internal Relevance Score", "Matched Terms",
    "Engagement", "Signal Evidence", "Feeds Seen", "Reddit Link"
]
SHEET_NAMES = ["Relevant Posts", "Needs Review", "Runs"]


def _terms(row):
    try:
        values = json.loads(row["matched_terms"] or "[]")
    except json.JSONDecodeError:
        values = []
    return "; ".join(values)


def _feeds(row):
    try:
        values = json.loads(row["feeds_seen"] or "[]")
    except json.JSONDecodeError:
        values = []
    labels = {"new": "NEW", "hot": "HOT", "rising": "RISING", "top_day": "TOP DAY", "top_week": "TOP WEEK"}
    return "; ".join(labels.get(v, v) for v in values)


def _relevant_rows(rows):
    return [[
        r["report_detected_run"], r["subreddit"], r["post_time"], r["title"], r["description"], r["topic"],
        r["report_engagement"], r["report_signal_evidence"], _terms(r), r["permalink"]
    ] for r in rows]


def _review_rows(rows):
    return [[
        r["report_detected_run"], r["subreddit"], r["post_time"], r["title"], r["description"], r["topic"],
        r["review_reason"], r["report_engagement"], r["permalink"]
    ] for r in rows]


def _run_rows(rows):
    return [[
        r["run_time"], r["feed_subreddit"], r["scan_window_start"] or "", r["scan_window_end"] or "",
        r["http_status"], r["entries_seen"], r["relevant"], r["needs_review"], r["excluded"],
        r["coverage_status"], r["notes"]
    ] for r in rows]


def _audit_rows(rows):
    return [[
        r["first_observed_run"], r["last_observed_run"], r["subreddit"], r["post_time"], r["title"],
        r["description"], r["classification"].upper(), r["topic"], r["review_reason"],
        ("%.2f" % float(r["score"])), _terms(r), r["engagement"], r["signal_evidence"], _feeds(r), r["permalink"]
    ] for r in rows]


def _atomic_csv(path, headers, rows):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    tmp.replace(path)


def generate_reports(conn, output_dir, report_date, organized=False, cfg=None):
    user_dir = Path(output_dir).expanduser()
    user_dir.mkdir(parents=True, exist_ok=True)
    output_dir = user_dir
    if organized:
        output_dir = user_dir / ".data"
        output_dir.mkdir(parents=True, exist_ok=True)
        # Preserve older generated files, but put them out of the human review path.
        current_monthly = REVIEW_FILE_NAME % report_date[:7]
        for old in user_dir.glob("Reddit-*"):
            if old.is_file() and old.name != current_monthly:
                old.replace(output_dir / old.name)
    relevant_db = posts_for_report_day(conn, report_date, "relevant")
    review_db = posts_for_report_day(conn, report_date, "review")
    runs_db = runs_for_day(conn, report_date)
    audit_db = posts_observed_on_day(conn, report_date)
    relevant = _relevant_rows(relevant_db)
    review = _review_rows(review_db)
    runs = _run_rows(runs_db)
    audit = _audit_rows(audit_db)

    stem = "Reddit-Signals-%s" % report_date
    xlsx = output_dir / (stem + ".xlsx")
    csv_path = output_dir / (stem + ".csv")
    jsonl_path = output_dir / (stem + ".jsonl")
    audit_path = output_dir / ("Reddit-All-Posts-%s.csv" % report_date)

    write_xlsx(xlsx, [
        {"name": SHEET_NAMES[0], "headers": RELEVANT_HEADERS, "rows": relevant, "hyperlink_col": 10},
        {"name": SHEET_NAMES[1], "headers": REVIEW_HEADERS, "rows": review, "hyperlink_col": 9},
        {"name": SHEET_NAMES[2], "headers": RUN_HEADERS, "rows": runs},
    ])
    validate_xlsx(xlsx, SHEET_NAMES)

    _atomic_csv(csv_path, RELEVANT_HEADERS, relevant)
    _atomic_csv(audit_path, AUDIT_HEADERS, audit)

    jsonl_tmp = jsonl_path.with_suffix(jsonl_path.suffix + ".tmp")
    with jsonl_tmp.open("w", encoding="utf-8") as f:
        for row in relevant:
            obj = dict(zip(RELEVANT_HEADERS, row))
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    jsonl_tmp.replace(jsonl_path)

    latest_xlsx = output_dir / "Reddit-Signals-Latest.xlsx"
    latest_jsonl = output_dir / "Reddit-Signals-Latest.jsonl"
    latest_audit = output_dir / "Reddit-All-Posts-Latest.csv"
    for source, dest in ((xlsx, latest_xlsx), (jsonl_path, latest_jsonl), (audit_path, latest_audit)):
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        shutil.copyfile(source, tmp)
        tmp.replace(dest)

    review_files = generate_review_exports(conn, output_dir, report_date)
    review_workbook = (generate_review_workbook(conn, user_dir, cfg or {}, report_date) if organized else None)

    return {
        "review_workbook": str(review_workbook) if review_workbook else "",
        "xlsx": str(xlsx), "csv": str(csv_path), "jsonl": str(jsonl_path), "audit_csv": str(audit_path),
        "latest_xlsx": str(latest_xlsx), "latest_jsonl": str(latest_jsonl), "latest_audit_csv": str(latest_audit),
        "counts": {"relevant": len(relevant), "review": len(review), "runs": len(runs), "observed": len(audit)},
        **review_files,
    }
