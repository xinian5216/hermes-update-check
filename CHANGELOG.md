# Changelog

All notable changes to this project are documented here. The format loosely
follows [Keep a Changelog](https://keepachangelog.com/) and the project uses
semantic versioning.

## [1.1.0] - 2026-09-15

### Added

* **Code provenance** (`provenance.py`, `channel`): the tool no longer trusts
  `hermes --version` alone. It reports branch / commit / nearest release tag /
  ahead-behind counts / dirty worktree and classifies the install as `STABLE`,
  `MAIN`, `PRERELEASE`, `DETACHED`, `CUSTOM` or `UNKNOWN`. A main-tracking install
  that is *ahead* of the latest stable release is reported as "not an update
  candidate" instead of "update available".
* **Component risk model**: `Change Risk`, `Regression Signal`, `Data Confidence`,
  `Environment Risk`, combined into `Overall Risk` = 1-(1-change)(1-regression)(1-environment)
  minus a stability bonus.
* **Hard gates** (`gates.py`): rules that override the score — release age
  (`minimum_release_age_hours`), main branch, dirty worktree, prerelease, active
  database/session/update-failure regressions, insufficient data. Verdict priority is
  data → environment → gates → score → cooling period → `UPDATE`.
* **Regression clustering** (`clusters.py`): nine classes (database, session,
  gateway, config migration, update failure, auth, crash, MCP, provider) graded by
  severity × confidence, where confidence comes from independent reporters,
  maintainer confirmation, linked PRs, reproduction steps, version mentions and
  title-signature agreement.
* **Advisor** (`advisor.py`): one explainable verdict
  (`UPDATE` / `WAIT` / `AVOID` / `INSUFFICIENT_DATA` / `UP_TO_DATE` /
  `AHEAD_OF_STABLE` / `MANUAL_REVIEW`) with the deciding layer, the blocking gates
  and a recommended recheck time.
* **Watch deltas**: notifications only for meaningful transitions (new release,
  risk band change, gate triggered/cleared, CRITICAL cluster appeared/resolved,
  channel change, WAIT→UPDATE) — not for score wobble.
* **One-click installers**: `install.sh` (Linux/macOS/WSL/git-bash) and
  `install.ps1` (Windows), plus a copy-paste prompt for an AI agent in the README.
* **Secret/privacy guard**: `scripts/scan_secrets.py` with a pre-commit hook, a CI
  job (full history + gitleaks) and outbound text mode (`--text`, `--stdin`);
  `SECURITY.md` documents the policy.
* **JSON contract** extended with `local` provenance, `risk` components,
  `hard_gates`, `regressions`, `recommendation`, `recommended_recheck`.
* Report rebuilt as `Hermes Update Advisor` (local installation / latest stable /
  risk / hard gates / regression signals / recommendation).

### Changed

* Missing data never lowers risk any more: unknown parts count at a floor, the
  score is published as a lower bound, and an incomplete observation window makes
  the verdict degrade to `WAIT — INSUFFICIENT OBSERVATION DATA`.
* A zero cache TTL now means "never fresh" (was: a same-instant cache hit).
* Windows-style paths are caught by the privacy scanner with either separator.

### Fixed

* Printing Chinese help text or a report on a Windows console with a legacy code
  page (cp1252/cp936) raised `UnicodeEncodeError` and exited 1; the CLI now
  switches the console to UTF-8 and reconfigures its streams with
  `errors="replace"`, so output degrades gracefully instead of crashing.
  * Found by the new CI installer job, which runs the tool under a plain
    PowerShell console after installing it.
* Date-style release tags (`v2026.9.14`) are no longer compared as versions.
* The local commit is compared against the release tag (a checkout can be *behind*
  the tag while the remote branch is hundreds of commits ahead).
* Clustered regressions are damped (0.6 per cluster, scaled by issue-signal
  confidence) so a noisy repository cannot pin the signal at 100.
* Fresh cache hits are no longer marked as degraded data.

## [1.0.0] - 2026-09-15

### Added

* Initial release: version/install/git detection, GitHub release + compare +
  issue analysis, keyword/age/volume risk scoring, natural-language
  recommendation, preflight checks, post-update health checks, backup, rollback,
  watch mode with Telegram/webhook notifications, YAML configuration, JSON and
  Markdown output, 146 offline tests.
