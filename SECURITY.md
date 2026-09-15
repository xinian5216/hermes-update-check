# Security policy

## What this tool does with your secrets

It does not store any. Credentials are read from environment variables at runtime
(the configuration only ever holds *variable names*, e.g.
`bot_token_env: HERMES_UPDATE_CHECK_TELEGRAM_TOKEN`). Nothing is written to disk
except cached GitHub API responses, local state (`~/.hermes-update-check/`) and
optional backup snapshots - all outside the repository.

`update` and `rollback` are the only commands that change your system, they always
ask for confirmation (default answer: **no**), and they never run unattended.

## Reporting a vulnerability

Open a [private security advisory](https://github.com/xinian5216/hermes-update-check/security/advisories/new)
or, if that is unavailable, an issue **without any sensitive detail**.

Please do not paste tokens, `state.db` contents, config files, or logs that contain
credentials into an issue. Redact machine paths and hostnames as well.

## Keeping the repository clean

Every push and pull request runs
[`scripts/scan_secrets.py`](../scripts/scan_secrets.py) over the full git history
plus [gitleaks](https://github.com/gitleaks/gitleaks), and GitHub's own secret
scanning and push protection are enabled for this public repository.

Locally, enable the pre-commit hook once per clone:

```bash
git config core.hooksPath .githooks
```

It scans staged content and blocks the commit on a credential format or a private
path (absolute Windows paths, real home directories, public IPs, personal e-mail
domains). Run it manually any time:

```bash
python scripts/scan_secrets.py            # working tree
python scripts/scan_secrets.py --staged   # staged only
python scripts/scan_secrets.py --all-history
python scripts/scan_secrets.py --text "…"  # any outbound text (e-mail/webhook/API body)
cat payload.json | python scripts/scan_secrets.py --stdin
```
