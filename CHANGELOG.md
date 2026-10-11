# Changelog

## Unreleased - 2026-09-24

- Each relevant or Needs Review post from any feed under 24 hours old enters the first-comment-sample queue, regardless of top-ten rank. Nonempty successful samples are not routinely fetched again; empty or failed samples can retry after six hours when eligible. Defaults cap requests at 12 per run and 48 per day.
- Added six-hour RISING collection. BEST is omitted because its home ranking is personalized.
- One `Reddit-Stream-YYYY-MM.xlsx` workbook now holds the month’s reply candidates, daily content evidence, comments, request reasons, run history, placements, and all posts.
- Added a single-folder Google Drive connection that updates the same monthly workbook after each run. AI drafting remains separate and disabled by default.
- Added a Latest Replies tab for the newest feed run and a richer status command for checking the Mac schedule without Codex.
- Preserved longer captured post text in the workbook and added explicit pending comment labels.

## 1.1.0 - 2026-09-18

Stability-focused collector release.

- First-run discovery window increased to 24 hours.
- First run now checks independent NEW, HOT, TOP DAY, and TOP WEEK feeds for every configured subreddit.
- Replaced clock-hour enrichment with persisted due-state: NEW every scheduled run, HOT every 6 hours, TOP DAY every 12 hours, TOP WEEK every 12 hours.
- Missed/sleep-delayed launchd runs now catch up ranking refreshes based on the last successful feed state rather than the wall-clock hour at execution.
- Ranking rediscovery can surface an older post in today's report even when its engagement label does not increase.
- Today's report preserves the strongest engagement evidence observed during that day while SQLite still tracks current ranks.
- Added `Reddit-All-Posts-YYYY-MM-DD.csv` and `Reddit-All-Posts-Latest.csv` audit outputs with Relevant / Needs Review / Excluded classification, reason, score, matched terms, ranking evidence, and feeds seen.
- Daily XLSX remains exactly three tabs: Relevant Posts, Needs Review, Runs.
- Improved live progress to distinguish feed entries returned from posts actually evaluated in the NEW scan window.
- Coverage logic now distinguishes a quiet feed below configured capacity from a capacity-truncated feed that cannot reach the required checkpoint.
- NEW checkpoints now advance to each feed response snapshot time, not the overall run start, preventing later feeds in a deliberately slow sequential run from caching an ETag for posts that were not processed.
- Unexpected 304 responses without prior successful conditional state are treated as errors and cannot advance discovery state.
- Added TOP DAY evidence to engagement classification.
- Expanded the default India personal-finance filter profile while retaining weighted scoring, word boundaries, noise penalties, hard exclusions, and Needs Review routing.
- Added `6_UPGRADE_EXISTING_INSTALL.command` with database/config backup, in-place schema migration, v1 checkpoint rewind for a one-time 24-hour bootstrap, and scheduler repointing when installed.
- Added v1 database migration for TOP DAY ranks, daily peak engagement, and daily all-post observation records.
- Automated suite expanded to 72 local tests, including due-state scheduling, audit output, 304 carry-over, daily peak engagement, schema migration, and upgrade preservation.

## 1.0.2 - 2026-09-18

- Hardened macOS Python discovery: interpreters must pass required standard-library XML/Expat, SQLite, HTTP, ZIP, plist and related imports before selection.
- Automatically skips broken Homebrew Python installations and tries other compatible Python 3.9+ interpreters.
- `0_REMOVE_PREVIOUS_BUILDS.command` is shell-only so recovery does not depend on Python working.

## 1.0.1 - 2026-09-18

- Finder-launched `.command` windows remain open until Return is pressed.
- Wrappers show a clear title and detected Python path.

## 1.0.0 - 2026-09-18

- Clean-room initial release using Python standard library, public Reddit RSS/Atom, SQLite, and internal OOXML writing.
