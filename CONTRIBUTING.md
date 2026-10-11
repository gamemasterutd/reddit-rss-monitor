# Contributing

Start with a small issue that describes the problem, the expected result and a reproducible example. Use fictional data or a redacted fixture. Never attach an API key, OAuth token, private configuration, collected database or workbook containing someone's personal details.

The current collector uses RSS, which Reddit has announced it will retire on 13 November 2026. Contributions to filtering, state handling and reporting are welcome. A replacement source connector needs a supported access route and explicit coverage limits. Discuss it before starting a large implementation.

## Local checks

Run from the project folder:

```bash
bash scripts/validate_release.sh
```

Validation compiles the code, checks shell syntax, runs fixture tests, audits standard-library imports and checks the public source tree for common private files or token patterns. It does not perform live scraping, install a scheduler or change your Google Drive connection. A passing scan cannot detect every possible secret; inspect your diff too.

Keep Python 3.9 source compatibility, standard-library-only runtime imports and tests that use temporary state. Changes to post identity, checkpoint/cache handling, request cooldowns or output joins should include a test of the concrete failure being fixed.

## Pull requests

Explain the problem, resulting behavior and how you checked it. Keep changes focused. GitHub Actions uses local fixtures and mocks; it should never run Reddit collection or an AI provider call. Review all committed files for private data before submitting.
