# Guardrails

## Collection boundary

Production collection is limited to public Reddit RSS/Atom listings. The application does not use Reddit login, Reddit API credentials, browser automation, undocumented JSON endpoints, GraphQL, cookies, CAPTCHA solving, proxy rotation, IP rotation, or identity switching.

## Request governor

One global governor is persisted in SQLite. Every request checks persistent cooldown and the timestamp of the previous Reddit request. Requests are sequential. The default minimum start-to-start spacing is 120 seconds.

Per-thread comment RSS requests share this governor. Every relevant or Needs Review post under 24 hours old enters the comment queue, regardless of feed rank. Fresh posts take priority over older RISING (under 72 hours), HOT/TOP DAY top-20, and TOP WEEK top-10 posts (under seven days). Nonempty successful samples are not routinely refreshed; an empty sample can be retried after six hours if the post later ranks. Defaults cap attempts at 12 per run and 48 per local day. Failed attempts can retry after six hours while still eligible. A 403/429 stops collection and uses the persisted cooldown. The monthly workbook shows missing and failed comment samples.

## Feed frequency

The scheduler invokes the program every three hours, but ranking feeds are requested only when due from their persisted last-success timestamps:

- NEW every invocation
- HOT every 6 hours
- RISING every 6 hours
- TOP DAY every 12 hours
- TOP WEEK every 12 hours

This due-state model also catches up after sleep without adding extra normal-day polling.

## 429 behavior

On HTTP 429 the current run stops immediately. Remaining feeds are not requested. `Retry-After` is honored when longer than the configured cooldown. The first 429 defaults to six hours; a consecutive 429 defaults to 24 hours. Runs during cooldown make zero Reddit requests.

## 403 behavior

HTTP 403 stops the run immediately and creates a persistent 24-hour cooldown.

## 5xx behavior

A server error gets at most one retry. The retry still passes through the same global request governor. If it fails again, successful checkpoint/cache-validator state is left unchanged.

## Conditional requests and 304

ETag and Last-Modified are saved only after successful, coverage-safe feed handling. A 304 is accepted only if there is a prior successful state for that feed. An unexpected 304 without such state is an error and cannot advance a checkpoint.

For ranking feeds, a valid 304 means the previously stored ranking snapshot remains current; already-known posts in that ranking feed can therefore be surfaced in the current day's report without another body download.

## Coverage gaps

For NEW feeds, the required scan start is the previous successful checkpoint or the first-run 24-hour recovery window.

A response is a confirmed gap when the feed is at configured capacity and its oldest returned post still does not reach the required start. In that case:

- visible in-window posts are deduplicated and stored
- the run is marked `GAP`
- the NEW checkpoint does not move
- newly returned ETag/Last-Modified values are not saved

A feed returning fewer than configured capacity is not treated as truncated solely because there are no older posts in the response.

## Process lock

`fcntl.flock` provides a non-blocking exclusive runtime lock. A second manual/scheduled process exits before any Reddit request.

## No bypass behavior

The project intentionally contains no retry strategy designed to evade blocking, browser-header spoofing, proxy recommendation, CAPTCHA handling, account rotation, or alternate identity fallback.
