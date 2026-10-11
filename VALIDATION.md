# Public preview validation

Checked on 12 October 2026.

The release validator passed 99 local tests on macOS with Python 3.9.6. It also passed Python compilation, shell syntax checks, a standard-library runtime import audit and a public-file scan. The source copy contains no runtime credentials, collected reports, private configuration or account-specific work notes. The initial and workflow commits use the account's GitHub noreply email.

The first [hosted validation run](https://github.com/gamemasterutd/reddit-rss-monitor/actions/runs/38173107378) succeeded on Python 3.9 and Python 3.13. Subsequent run status is available in the Actions tab.

These tests use fixtures, mocks and a localhost server. They exercise post identity, checkpoint/cache handling, cooldowns, ranked observations, comment parsing/selection, report structure, joins, mocked Drive upload, AI validation, deduplication and custom instructions. They do not contact Reddit or call an AI provider.

The public-file scan detects common private files and token/link patterns; it is not an exhaustive secret detector. Review every contribution's diff before publishing it.

## Limits

- No live Reddit availability guarantee. RSS retirement is announced for 13 November 2026.
- No live AI provider/model combination was tested for this release; provider modules remain experimental.
- No verification that the comment RSS sample contains the complete discussion.
- No numerical vote counts, automatic image reading, automatic Reddit posting or built-in live market research.
- No guarantee that every new relevant post receives its first comment check before 24 hours.
- Tests do not install a real launchd job or authorize a Google Drive account.

Run `bash scripts/validate_release.sh` from a checkout to repeat the local checks. This validates local behavior; source access and output quality need separate checks for the actual use case.
