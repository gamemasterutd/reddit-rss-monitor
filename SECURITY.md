# Security and private data

This package reads public RSS/Atom URLs and writes local reports. Optional Drive upload and experimental AI review use separate credentials supplied by the person running it.

Keep runtime folders, SQLite databases, reports, configuration, OAuth tokens and API keys out of public repositories. The example configuration contains placeholders; it is not a credential. Collected public posts can still include personal financial information, so review exports before sharing them.

The monthly Drive uploader sends the generated source workbook to the folder you select. A successful upload does not prove a fresh Reddit scan. Keep its rclone configuration private and check the actual permissions during Google consent.

The `.gitignore` and release scan help prevent accidental publication of common private files. They are not a complete secret detector, and ignoring a file does not remove it from existing Git history.

If a credential is exposed, revoke or rotate it at its provider immediately. Removing it from a future commit is not sufficient.

To report a vulnerability, use the repository's private security-reporting feature if enabled. Do not post credentials, collected personal data or an exploit containing private account details in a public issue. If private reporting is unavailable, open an issue requesting a private contact without including sensitive material.
