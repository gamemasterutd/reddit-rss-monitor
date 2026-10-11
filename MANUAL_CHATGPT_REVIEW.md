# Manual ChatGPT review of the monthly Reddit stream

Open the current `Reddit-Stream-YYYY-MM.xlsx` in the work Drive folder. It is the scraper's source workbook. `Latest Replies` contains all under-24-hour reply candidates, with `LATEST RUN` tagging newly discovered posts; `Reply Candidates` keeps the month's reply candidates; `Content by Day` keeps daily research material. `Comments` has individual saved comments, and `Comment Requests`, `Runs`, and `Feed Placements` explain what was collected. Match all evidence by **Post ID**. The saved Reddit RSS comments are a sample, and feed ranks are not vote counts.

Keep scheduled AI drafting paused while testing quality. A team member decides whether and where to post. The scraper never publishes to Reddit.

## Prompt 1 — reply review

Attach or select the latest monthly workbook in this chat, then paste:

> You are reviewing the attached Reddit stream for possible *human-written* Reddit replies. Read `Start Here`, `Latest Replies`, `Reply Candidates`, `Comments`, and `Comment Requests`. State the workbook's update time and latest run before drafting. Evaluate every row in `Latest Replies`, prioritising `LATEST RUN` rows, then check `Reply Candidates` only for context or a missed active row. Deduplicate by Post ID. Join comment rows by exact Post ID. Do not mix facts, fund names, family details, or comments from two posts.
>
> For each candidate, first write a private one-sentence test: “OP asks ___.” Compare every possible reply with that test. If the post's real question depends on an image or attachment not readable in the workbook, mark `HOLD — source image needed`; do not infer its contents from comments. If the saved comment sample is missing or stale, mark the gap and use the live thread only if you can actually open it. Never claim that you read all comments when you read a sample.
>
> Qualification is separate from drafting. A post can be a good content signal and a poor reply opportunity. Confirm that OP is asking for a decision or discussion; an article-style or promotional post with a question-shaped title may be a poor reply target. But **do not skip merely because someone already mentioned the same idea**. A concise synthesis, example, or practical next step can still help. Skip when the actual question cannot be answered responsibly, the context is missing, the thread is unsuitable for a brand account, or a specific point cannot be supported. For `REVIEW` classification, explain the concern and ask for a team decision before posting.
>
> For qualified posts, draft a reply that answers OP's literal question in its **first sentence**. Follow with one concrete reason, example, or decision test. Aim for 40–90 words unless the thread genuinely needs less or more. Match the language and energy of the thread. Sound like a thoughtful Indian Reddit participant: direct, specific, occasionally informal where natural. Do not force “bhai,” slang, empathy openers, personal anecdotes you did not live, a product pitch, a generic checklist, or a boilerplate disclosure. Do not pretend to be an independent consumer when the team will post from a team account. A human will check subreddit rules, account identity and any required disclosure before posting.
>
> Never invent a current tax, legal, interest-rate, fund, or market fact. If a material current fact matters, verify it with a dated primary source and link it, or put it under `Facts to verify` instead of asserting it. No personalized fund buy/sell advice or guaranteed returns.
>
> Return one table with: Run IST, Post ID, subreddit, post title and direct link, OP's actual question, comment coverage, what useful point or synthesis the reply adds, decision (`DRAFT`, `HOLD`, or `SKIP`), draft, why it answers OP, facts/source image to verify, and team status. If a draft's first sentence would make sense on a different thread, rewrite it. Check each number, scheme name, and claimed fact against that same Post ID before showing the table. Do not post anything.

A useful reply is allowed to be simple. For “Should I build an app?”, start with “I wouldn't build an app yet” or another direct answer, then explain the smallest existing tool or test that addresses OP's real concern. Do not pivot to an unrelated portfolio review.

## Prompt 2 — daily content ideas and script

Use the same workbook and paste:

> Review `Content by Day` for the latest 7 complete days and `Comments` by exact Post ID. Include `REVIEW` posts as possible research signals, with their uncertainty visible. Read `Runs` so you know whether each subreddit/feed was covered; do not treat the selected workbook rows as every Reddit post. De-duplicate repeated observations by Post ID, then cluster related posts by the **underlying money decision**, not just shared keywords.
>
> Produce 0–4 ideas only if they pass these gates, each scored 0–2 with evidence: observed demand, size of the financial consequence, checkable evidence, distinct insight, and a clear hook/format. Reject an idea if checkable evidence or distinct insight is zero. For each selected idea, show source post IDs and direct links, the OPs' actual questions, dates and subreddits, what the saved comments support or dispute, and what remains unknown. Explain why this cluster deserves content **now**, what your editorial point of view adds, and why it is different from a generic “how to choose funds” post. Consider lenses such as missing variable, counterpoint, decision test, worked example, misconception, behaviour trap, and verified market-trend intersection.
>
> If you use a live Indian market or product trend, browse dated **primary** sources, cite the exact source and date, and separate that finding from Reddit evidence. If you cannot verify a live trend, do not invent one. For claims like “26 of 40 questions,” show the complete counted denominator, time window and classification rule; otherwise use precise qualitative wording.
>
> For each selected idea propose a title, a three-second hook, the one-sentence thesis, a worked example or exact math if useful, a fair counterpoint, the best format (video, text, chart, carousel, or another form), and a 35–60 second script only if video really suits the idea. Choose English, Hindi or natural Hinglish based on the audience and post evidence. Use familiar words, not textbook translations or forced slang. A provocative hook must be defensible; no fabricated “scam” or loss claim. Make the script teach one useful decision, not merely restate the Reddit post. Include a human fact-check list and source links.
>
 Compare every idea with the existing `Content Idea Ledger` before adding it; reuse a topic only when the thesis or evidence is materially different. If writing to the ledger, update its `Ideas` tab and append an `Audit` row with run IST, workbook update time, date range, post IDs scanned, number of distinct posts, shortlisted clusters, selected ideas, rejected ideas and reasons, external sources used, and coverage limitations. Do not create a separate content document.

## Why these prompts differ from the old agents

- **Lead Intelligence** had a useful relevance / engagement / recency / safety / intent / value checklist. Its old per-run search and Sheets workflow is unnecessary because the local monitor already collects and deduplicates the stream. Its example replies often answered the decision directly, but were frequently too long; some sample links were listing pages rather than direct post links, so they are style references, not validated evidence.
- **Hot Signals** and **Hot Signals — Draft 2** correctly separated reply value from content value and treated missing metrics and partial comment access honestly. The long Heat Score formulas implied precision the RSS workbook cannot support. Feed placement is retained as an observed signal, with unknown votes and total comments left unknown.
- **Content Intelligence** emphasized immediate clarity, one useful insight, a strong hook, and avoiding repeated angles. Its single-image default does not fit this pipeline's flexible format choice, so a video script is proposed only when the idea warrants one.
