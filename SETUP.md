# Set up Reddit RSS Monitor

> **Before installing:** Reddit has [announced the end of RSS support on 13 November 2026](https://www.reddit.com/r/modnews/comments/1wubgvt/continuing_our_infrastructure_updates_whats/). This release has no replacement collector. Live access can fail before that date; installing the package does not establish permission or guarantee collection. You can run the fixture tests without contacting Reddit.

This guide is for a teammate who has never used this project. The monitor reads public Reddit RSS feeds and saves the posts in files you can review. It also saves a bounded *sample* of comments for fresh relevant posts and active ranked posts.

The scraper runs on your Mac. It does not require a Reddit account or an AI key. Google Drive and AI review are optional. Once installed, its Mac schedule runs without keeping Codex open. The Mac must be awake and logged in for collection and Drive upload. ChatGPT review can be done manually from the monthly workbook. A separate AI schedule is optional and should be enabled only after draft quality is checked.

## 1. Get the folder

Download the project ZIP from its GitHub page and unzip it, or clone its GitHub repository. Keep the project in a normal folder in your home directory, such as `~/reddit-rss-monitor`. Avoid Downloads if macOS blocks background access to it.

Open Terminal and enter the project folder:

```bash
cd ~/reddit-rss-monitor
```

Check that Python is available:

```bash
python3 --version
```

Python 3.9 or newer is needed. The scraper itself uses only Python's standard library. No `pip install` is needed.

## 2. Answer three setup questions

```bash
bash 1_SETUP_MAC.command
```

The setup asks for:

1. A contact email for the public RSS request's User-Agent. Use an address you control.
2. The subreddits to follow, separated by commas. Type names such as `personalfinanceindia,MutualfundsIndia`; `r/` is optional.
3. Where to save reports. Press Return to use this project's `output` folder, or enter another folder.

The setup checks your Python installation, runs local tests, and creates a private config and database. It does not fetch Reddit yet. You can later change subreddit names, timing, and filters in `~/Library/Application Support/RedditRSSMonitor/config.json`. The example settings are in `config.example.json`; the filter rules are in `filters/finance.json`.

## 3. Run once and check the result

```bash
bash 2_RUN_NOW.command
```

The first run checks the last 24 hours of NEW and takes snapshots of HOT, RISING, TOP DAY, and TOP WEEK. With four subreddits and the default two-minute gap between requests, allow at least 40 minutes plus time for comment feeds. Leave the Mac awake.

Open **`output/Reddit-Stream-YYYY-MM.xlsx`** for the current month. It is the only visible review file. It is rebuilt after each completed run and keeps this month’s date-wise rows from the saved database:

1. **Latest Replies:** all relevant and Needs Review posts still under 24 hours old. `LATEST RUN` marks posts newly found in the last feed run; `Reply Review State` shows whether comments or an image are missing.
2. **Reply Candidates:** the full month of candidate posts and their comment/source status.
3. **Content by Day:** relevant and Needs Review posts observed each day, with saved comments where available.
4. **Comments and Comment Requests:** saved comment samples and why each comment request was made.
5. **Runs and Feed Placements:** feed checks and each post's rank and entry/return status.
6. **All Posts and Method:** excluded posts too, plus label and request-limit explanations.

The `.data` folder inside `output` is hidden in Finder. It keeps dated machine exports and legacy text handoffs; the project does not delete your earlier reports. To rebuild the workbook from saved data without fetching Reddit, run `python3 -m reddit_monitor.cli rebuild-review` in the project folder.

A post can appear in several feeds, but it keeps one post ID. `BASELINE` means the first snapshot the monitor saw, not a brand-new Reddit post. HOT/RISING/TOP position is a rank, not an upvote count. BEST is personalized and not used as a neutral subreddit signal. Saved comment RSS can be incomplete. Fund-allocation posts whose holdings are missing from the captured text stay visible and are marked “do not draft” until the image is reviewed. The monitor may retain a public image link from RSS, but it does not read the image. Check the live thread before using a reply.

## 4. Turn on the automatic Mac schedule

After a successful manual run:

```bash
bash 3_INSTALL_EVERY_3_HOURS.command
bash 4_STATUS.command
```

The one Mac schedule starts at **00:17, 03:17, 06:17, 09:17, 12:17, 15:17, 18:17, and 21:17** in the Mac's local time. NEW is due every three hours; HOT and RISING every six; TOP DAY and TOP WEEK every twelve. The collector saves its own checkpoints, so it does not start over each time. If the Mac sleeps, it catches up when it next runs, but it cannot collect while the Mac is off.

To check status, double-click `4_STATUS.command` in Finder, or run `python3 -m reddit_monitor.cli status` from the project folder. Check the Runs tab for actual source success; the workbook's rebuild timestamp is a separate event.

To stop the schedule and any scheduled run while preserving configuration and reports, run these commands on your Mac:

```bash
launchctl disable "gui/$(id -u)/com.redditrssmonitor"
launchctl bootout "gui/$(id -u)/com.redditrssmonitor"
```

To resume later, enable it and reinstall the scheduler from the project folder:

```bash
launchctl enable "gui/$(id -u)/com.redditrssmonitor"
bash 3_INSTALL_EVERY_3_HOURS.command
```

`5_UNINSTALL_THIS_MONITOR.command` is a full uninstall: it removes scheduler, private configuration, database and runtime state, and asks whether reports should also be deleted. Use it only when you intend to remove the installation.

## 5. Choose where files go

**Local only:** do nothing else. The output folder you chose in step 2 is enough.

**A folder synced by Google Drive for desktop:** use `7_CONNECT_WORK_GOOGLE_DRIVE.command` and select a synced folder. This option needs Google Drive for desktop already installed and signed in.

**Direct Google Drive upload without the Drive desktop app:** install `rclone` from its official source, make a Google Drive remote with `rclone config`, then run `9_CONNECT_GOOGLE_DRIVE_FOLDERS.command`. That command asks for the remote name, the private rclone config file, and one Drive folder ID for the monthly source workbook. You can find a folder ID after `/folders/` in its Drive URL. The command uploads one `Reddit-Stream-YYYY-MM.xlsx` file and updates that same monthly filename after each run. Keep the OAuth config private. A Google Cloud OAuth client may be needed for a reliable long-running connection.

Drive upload is optional. If it fails, local reports still work and the error appears in the run log. A cloud AI task sees only the last successfully uploaded version, so it must check the timestamp in the file.

## 6. Optional AI review

For optional provider keys and local draft generation, follow [LLM_SETUP.md](LLM_SETUP.md). Start with a manual test, choose a model supported by your account and supply your own instructions if the finance examples do not fit your subject.

The scraper does not automatically publish or write Reddit replies. Give an AI the current `Reddit-Stream-YYYY-MM.xlsx`. Read **Latest Replies + Comments** for draft replies or **Content by Day + Comments** for ideas. Check **Source Check**, **Comment Status**, and **Reply Review State**. `LATEST RUN` marks newly discovered posts. Copy the prompts in `MANUAL_CHATGPT_REVIEW.md` for a careful first pass. Keep AI draft output in a separate review sheet; never overwrite the scraper source workbook.

Ask it to answer the poster's actual question first, show the post link, what comments were actually available, why its reply or content idea is useful, what facts remain unchecked, and the source timestamp. A missing comment sample means *unknown coverage*, not that nobody said the point. A team member should check the live post and subreddit rules before posting. Do not automate posting from this project.

## The safety rules built into the scraper

- Reddit requests are sequential, with a default gap of at least 120 seconds.
- Comment RSS is limited to 12 requests per run and 48 per local day by default. Every relevant or Needs Review post under 24 hours old takes priority, even outside the top ten. Older ranked posts use spare slots. Nonempty samples are not routinely refreshed; empty or failed samples may be retried after six hours. Posts left by caps remain pending while eligible.
- A 429 rate-limit response stops the run and starts a saved cooldown. A 403 stops the run and starts a longer cooldown. An active cooldown means zero new Reddit requests.
- The monitor does not rotate IPs, solve CAPTCHAs, use hidden endpoints, or disguise browser traffic.
- A failed feed does not silently advance the collection checkpoint.
- It never invents vote or total-comment counts from RSS ranks.

Reddit can change its feeds or policies. This software's MIT license covers the code; it does not grant rights to Reddit data or override Reddit's rules. Read `GUARDRAILS.md` and `SECURITY.md` before sharing data or using it in a brand account.

## Before putting this on GitHub

The included `.gitignore` keeps `output/`, `.runtime/`, local Work setup notes, tokens, and database files out of a new repository. Review `git status` before every commit. Never publish OAuth tokens, Reddit post exports containing personal details, or your private configuration. The code is MIT licensed in `LICENSE`.
