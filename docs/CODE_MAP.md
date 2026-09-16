# Code map (generated - do not edit by hand)

Read this before opening files: it is regenerated from the source by
`python scripts/build_index.py` and CI fails when it is stale.

## Quick facts

- package: `hermes_update_check` - 28 modules, 11245 lines
- tests: 344 test functions in 23 files (offline, no network)
- docs: `README.md` (user guide), `SECURITY.md` (privacy policy), `CHANGELOG.md`
- invariants: never updates Hermes without an explicit `y`; unknown data is reported as
  UNKNOWN, never as safe; exit codes are a public contract (see `errors.py`)

## Entry points

| entry | where |
|---|---|
| `hermes-update-check` console script | `cli.py:main` |
| `python -m hermes_update_check` | `__main__.py` |
| one-click installers | `install.sh`, `install.ps1` |
| secret/privacy scan | `scripts/scan_secrets.py` |

## CLI surface (parsed from `cli.py`)

| command | help |
|---|---|
| `check` | check for updates and print the short risk verdict |
| `report` | full report (factors, issues, recommendation) |
| `watch` | cron-friendly check: notify only when something changed |
| `update` | update Hermes after risk gate + preflight + confirmation |
| `rollback` | restore the version recorded in update_state.json |
| `health` | run the post-update health checks |
| `preflight` | run the pre-update checks only |
| `config` | show / locate / create the configuration |
| `notify-test` | send a test notification to every configured channel |
| `version` | print the tool version |

## Modules

| module | lines | what it is for |
|---|---|---|
| [`risk`](../hermes_update_check/risk.py) | 1195 | The risk engine: everything that turns observations into an Update Risk Score |
| [`cli`](../hermes_update_check/cli.py) | 1015 | Command line interface |
| [`checker`](../hermes_update_check/checker.py) | 786 | Orchestration: gather every input, then hand it to the risk engine |
| [`report`](../hermes_update_check/report.py) | 685 | Report rendering: the human-readable answer, in Chinese or English |
| [`clusters`](../hermes_update_check/clusters.py) | 649 | Regression clustering: turn raw issue hits into *credible* regression signals |
| [`config`](../hermes_update_check/config.py) | 616 | Configuration loading, validation and path resolution |
| [`updater`](../hermes_update_check/updater.py) | 604 | Update execution: snapshot -> update -> health check -> (rollback) |
| [`provenance`](../hermes_update_check/provenance.py) | 564 | Code provenance: *what code is actually running*, not just what it calls itself |
| [`github_api`](../hermes_update_check/github_api.py) | 546 | GitHub API access for the Hermes repository: releases, compares, issue searches |
| [`local_env`](../hermes_update_check/local_env.py) | 533 | Local environment detection: what Hermes is installed here, how, and in what state |
| [`util`](../hermes_update_check/util.py) | 512 | Small, dependency-free helpers: subprocess runner, JSON IO, time parsing, hashing |
| [`health`](../hermes_update_check/health.py) | 511 | Post-update health checks: did the update actually leave a working install? |
| [`gates`](../hermes_update_check/gates.py) | 491 | Hard gates: rules that override the risk score |
| [`preflight`](../hermes_update_check/preflight.py) | 485 | Pre-update checks: is this machine actually in a state where an update is safe to start? |
| [`advisor`](../hermes_update_check/advisor.py) | 419 | The advisor: turn provenance + risk + gates into one explainable verdict |
| [`http`](../hermes_update_check/http.py) | 309 | HTTP layer: stdlib-only client with timeouts, retries, on-disk cache and rate-limit awareness |
| [`versioning`](../hermes_update_check/versioning.py) | 299 | Version parsing and comparison |
| [`state`](../hermes_update_check/state.py) | 266 | State files: `update_state.json`, watch state, cache/snapshot directories |
| [`console`](../hermes_update_check/console.py) | 228 | Terminal rendering: pretty with `rich`, correct with plain text |
| [`errors`](../hermes_update_check/errors.py) | 120 | Exceptions and process exit codes |
| [`notify.telegram`](../hermes_update_check/notify/telegram.py) | 111 | Telegram channel (first-class notifier - `my chat` is where alerts are read) |
| [`logging_setup`](../hermes_update_check/logging_setup.py) | 73 | Logging setup: file + optional console, quiet by default on stdout |
| [`notify.base`](../hermes_update_check/notify/base.py) | 68 | Notifier interface and the message object every channel receives |
| [`notify`](../hermes_update_check/notify/__init__.py) | 52 | Notification backends: one small interface, pluggable channels |
| [`notify.webhook`](../hermes_update_check/notify/webhook.py) | 50 | Generic JSON webhook channel (Slack/Discord/n8n/your own endpoint) |
| [`i18n`](../hermes_update_check/i18n.py) | 36 | Tiny two-language (zh/en) translation helper |
| [`__init__`](../hermes_update_check/__init__.py) | 12 | hermes-update-check - safety-first update checker and risk assessor for Hermes Agent |
| [`__main__`](../hermes_update_check/__main__.py) | 10 | ``python -m hermes_update_check`` entry point |

### `__init__` — hermes-update-check - safety-first update checker and risk assessor for Hermes Agent

`hermes_update_check/__init__.py` (12 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `TOOL_NAME` | 11 | 'hermes-update-check' |

### `__main__` — ``python -m hermes_update_check`` entry point

`hermes_update_check/__main__.py` (10 lines)

_no public symbols_

### `advisor` — The advisor: turn provenance + risk + gates into one explainable verdict

`hermes_update_check/advisor.py` (419 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `RECOMMEND_AHEAD_OF_STABLE` | 43 | 'AHEAD_OF_STABLE' |
| constant | `RECOMMEND_MANUAL_REVIEW` | 44 | 'MANUAL_REVIEW' |
| constant | `DECIDED_BY` | 46 | {'update_status': '更新状态', 'insufficient_data': '数据不足', 'environment': … |
| constant | `RECHECK_CRITICAL_HOURS` | 56 | 12.0 |
| constant | `RECHECK_SEVERE_HOURS` | 57 | 24.0 |
| constant | `RECHECK_ROUTINE_HOURS` | 58 | 24.0 |
| constant | `RECHECK_WAIT_DAYS` | 59 | 3.0 |
| constant | `RECHECK_AVOID_DAYS` | 60 | 7.0 |
| class | **Recommendation** — is_blocking, decided_by_label, to_dict | 64 | The final, human-readable verdict |
| function | `advise(cfg: Config, *, provenance: CodeProvenance, decision: UpdateDecision, assessment: Optional[RiskAssessment], gates: GateReport, clusters: list[RegressionCluster] = …, release: Optional[Release] = …, now: Optional[datetime] = …) -> Recommendation` | 119 | Apply the priority order and produce the final recommendation |
| function | `recommended_recheck(*, action: str, gate_deadline: Optional[datetime], clusters: list[RegressionCluster], routine_hours: float, now: datetime) -> tuple[Optional[float], str, str]` | 362 | How soon is it worth looking again? |

### `checker` — Orchestration: gather every input, then hand it to the risk engine

`hermes_update_check/checker.py` (786 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `ISSUE_FALLBACK_QUERY` | 64 | 'label:bug' |
| constant | `BASELINE_MIN_DAYS` | 66 | 3.0 |
| constant | `BASELINE_MAX_DAYS` | 67 | 14.0 |
| class | **UpdateCheck** — degraded, update_status, channel, action, tracking_main, recommended_action | 71 | The complete result of one check - everything the report needs |
| function | `build_http_client(cfg: Config, state_root: Path, *, logger: Optional[logging.Logger] = …, no_cache: bool = …) -> tuple[HttpClient, bool]` | 286 | Create the HTTP client with token and disk cache |
| function | `resolve_github_token(cfg: Config) -> tuple[Optional[str], bool]` | 302 | Find a GitHub token in the environment (never in the config file) |
| function | `build_github_client(cfg: Config, state_root: Path, *, logger: Optional[logging.Logger] = …, no_cache: bool = …) -> tuple[GitHubClient, bool, HttpClient]` | 316 |  |
| function | `run_check(cfg: Config, *, env: Optional[LocalEnv] = …, client: Optional[GitHubClient] = …, state_root: Optional[Path] = …, no_cache: bool = …, include_issues: Optional[bool] = …, logger: Optional[logging.Logger] = …) -> UpdateCheck` | 333 | Full check: local env -> releases -> compare -> issues -> risk assessment |
| function | `collect_issue_signal(cfg: Config, client: GitHubClient, release: Release, *, provenance: Optional[CodeProvenance] = …, logger: Optional[logging.Logger] = …, use_cache: bool = …) -> IssueSignal` | 562 | Search GitHub issues filed after the release, plus a baseline window |
| function | `issue_signal_confidence(signal: IssueSignal) -> int` | 741 | Issue Signal Confidence (0-100): how much should the issue data be trusted? |
| function | `summarize_release_line(release: Release) -> str` | 769 | One-line label like ``v0.21.3 (v2026.9.14) - 0.6 days old`` |
| function | `level_of(score: Optional[int]) -> str` | 777 |  |
| function | `iso_now() -> str` | 781 |  |
| function | `normalise_release_tag(tag: Optional[str]) -> Optional[str]` | 785 |  |

### `cli` — Command line interface

`hermes_update_check/cli.py` (1015 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| function | `build_parser() -> argparse.ArgumentParser` | 71 |  |
| function | `make_output_encoding_safe() -> None` | 168 | Never crash on a console that cannot represent the text we print |
| function | `main(argv: Optional[Sequence[str]] = …) -> int` | 195 |  |
| function | `cmd_check(args: argparse.Namespace, cfg: Config, console: Console, store: StateStore, logger: Any) -> int` | 281 |  |
| function | `cmd_report(args: argparse.Namespace, cfg: Config, console: Console, store: StateStore, logger: Any) -> int` | 296 |  |
| function | `cmd_watch(args: argparse.Namespace, cfg: Config, console: Console, store: StateStore, logger: Any) -> int` | 322 | Cron entry point: stay silent unless something meaningful changed |
| function | `cmd_update(args: argparse.Namespace, cfg: Config, console: Console, store: StateStore, logger: Any) -> int` | 413 |  |
| function | `cmd_rollback(args: argparse.Namespace, cfg: Config, console: Console, store: StateStore, logger: Any) -> int` | 538 |  |
| function | `cmd_health(args: argparse.Namespace, cfg: Config, console: Console, store: StateStore, logger: Any) -> int` | 598 |  |
| function | `cmd_preflight(args: argparse.Namespace, cfg: Config, console: Console, store: StateStore, logger: Any) -> int` | 615 |  |
| function | `cmd_config(args: argparse.Namespace, cfg: Config, console: Console) -> int` | 625 |  |
| function | `cmd_notify_test(args: argparse.Namespace, cfg: Config, console: Console, logger: Any) -> int` | 647 |  |

### `clusters` — Regression clustering: turn raw issue hits into *credible* regression signals

`hermes_update_check/clusters.py` (649 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `SEVERITY_CRITICAL` | 23 | 'CRITICAL' |
| constant | `SEVERITY_HIGH` | 24 | 'HIGH' |
| constant | `SEVERITY_MEDIUM` | 25 | 'MEDIUM' |
| constant | `SEVERITY_LOW` | 26 | 'LOW' |
| constant | `CONFIDENCE_HIGH` | 28 | 'HIGH' |
| constant | `CONFIDENCE_MEDIUM` | 29 | 'MEDIUM' |
| constant | `CONFIDENCE_LOW` | 30 | 'LOW' |
| constant | `SEVERITY_WEIGHTS` | 32 | {SEVERITY_CRITICAL: 1.0, SEVERITY_HIGH: 0.75, SEVERITY_MEDIUM: 0.45, S… |
| constant | `CONFIDENCE_WEIGHTS` | 38 | {CONFIDENCE_HIGH: 1.0, CONFIDENCE_MEDIUM: 0.6, CONFIDENCE_LOW: 0.3} |
| constant | `SEVERITY_RANK` | 40 | {SEVERITY_CRITICAL: 3, SEVERITY_HIGH: 2, SEVERITY_MEDIUM: 1, SEVERITY_… |
| constant | `UNKNOWN_REGRESSION_FLOOR` | 43 | 25 |
| class | **ClusterSpec** | 47 |  |
| constant | `DEFAULT_ENRICHMENT_LIMIT` | 190 | 3 |
| class | **IssueEnrichment** — to_dict | 194 | Extra facts fetched for a single issue (comments / association) |
| class | **RegressionCluster** — contribution, is_severe, is_active, headline_zh, headline_en, detail_zh | 219 | One graded regression cluster |
| function | `cluster_keys_for(issue: Issue, *, include_body: bool = …) -> list[str]` | 311 | Which clusters an issue belongs to (title + labels first, then body) |
| function | `severity_for(key: str, text: str) -> str` | 323 | Cluster severity, promoted to CRITICAL when the text says so |
| function | `build_clusters(issues: Iterable[Issue], *, enrichment: Optional[dict[int, IssueEnrichment]] = …, release_version: Optional[str] = …, release_tag: Optional[str] = …) -> list[RegressionCluster]` | 382 | Grade every cluster found in the scanned issues |
| function | `grade_confidence(*, reports: int, unique_reporters: int, open_count: int, maintainer_confirmed: int, linked_pr: int, with_reproduction: int, mentions_version: int, signature_groups: int) -> str` | 500 | Credibility of a cluster, 0..1, mapped to LOW/MEDIUM/HIGH |
| class | **RegressionSignal** — display, to_dict | 545 | The Regression Signal (0-100) plus the floor used when data is missing |
| constant | `CLUSTER_WEIGHT` | 579 | 0.6 |
| constant | `EVIDENCE_FLOOR` | 583 | 0.55 |
| function | `regression_signal(clusters: Iterable[RegressionCluster], *, volume_ratio: float = …, signal_confidence: Optional[int] = …, unavailable: bool = …, unavailable_reason: str = …) -> RegressionSignal` | 586 | Combine cluster evidence into the Regression Signal |

### `config` — Configuration loading, validation and path resolution

`hermes_update_check/config.py` (616 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `ENV_PREFIX` | 31 | 'HERMES_UPDATE_CHECK_' |
| class | **GitHubConfig** | 105 |  |
| class | **HardGateConfig** | 115 | Rules that override the numeric risk score |
| class | **NetworkConfig** | 131 |  |
| class | **PathsConfig** | 137 |  |
| class | **UpdateConfig** | 143 |  |
| class | **WatchConfig** | 153 |  |
| class | **TelegramConfig** | 162 |  |
| class | **WebhookConfig** | 169 |  |
| class | **NotifyConfig** | 176 |  |
| class | **LoggingConfig** | 182 |  |
| class | **RiskWeights** | 189 |  |
| class | **Config** — repo, to_dict | 199 |  |
| function | `default_config_path() -> Path` | 254 | `~/.config/hermes-update-check/config.yaml` (Windows: `%APPDATA%\...`) |
| function | `default_state_dir() -> Path` | 269 | `~/.hermes-update-check` - state, cache, snapshots, logs, update_state.json |
| function | `resolve_hermes_home(cfg: Config | None = …) -> Path` | 277 | Resolve $HERMES_HOME the way Hermes does, falling back to ~/.hermes |
| function | `resolve_state_dir(cfg: Config | None = …) -> Path` | 290 |  |
| function | `load_config(path: Path | str | None = …, *, env: Mapping[str, str] | None = …) -> Config` | 301 | Load, merge and validate configuration |
| function | `deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]` | 366 | Recursive merge; `override` wins |
| function | `write_default_config(path: Path | None = …, *, force: bool = …) -> Path` | 602 | Write a commented example config (used by `config init`) |

### `console` — Terminal rendering: pretty with `rich`, correct with plain text

`hermes_update_check/console.py` (228 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| function | `rich_available() -> bool` | 70 |  |
| class | **Console** — print, print_markup, heading, blank, badge, kv_table | 75 | Minimal rendering facade used by every command |

### `errors` — Exceptions and process exit codes

`hermes_update_check/errors.py` (120 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `EXIT_OK` | 22 | 0 |
| constant | `EXIT_ERROR` | 23 | 1 |
| constant | `EXIT_USAGE` | 24 | 2 |
| constant | `EXIT_CONFIG` | 25 | 3 |
| constant | `EXIT_WAIT` | 26 | 10 |
| constant | `EXIT_INSUFFICIENT_DATA` | 27 | 11 |
| constant | `EXIT_HEALTH_FAILED` | 28 | 12 |
| constant | `EXIT_ABORTED` | 29 | 13 |
| constant | `EXIT_PREFLIGHT_FAILED` | 30 | 14 |
| class | **HermesUpdateCheckError** | 33 | Base class for every error raised by this tool |
| class | **ConfigError** | 53 | Invalid or unreadable configuration |
| class | **NetworkError** | 59 | A network operation failed (timeout, DNS, TLS, HTTP 5xx...) |
| class | **GitHubError** | 63 | GitHub API returned an unexpected response |
| class | **RateLimitError** | 79 | GitHub rate limit exhausted (HTTP 403/429 with rate-limit headers) |
| class | **CommandError** | 95 | An external command (hermes, git, uv, ...) failed |
| class | **PreflightError** | 99 | A blocking pre-update check failed |
| class | **HealthCheckFailed** | 105 | The post-update health check failed |
| class | **AbortedError** | 111 | The user (or a non-interactive stdin) refused to continue |
| class | **InsufficientDataError** | 117 | Not enough information to produce a trustworthy risk assessment |

### `gates` — Hard gates: rules that override the risk score

`hermes_update_check/gates.py` (491 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `GATE_BLOCK` | 47 | 'BLOCK' |
| constant | `GATE_WARN` | 48 | 'WARN' |
| constant | `GATE_PASS` | 49 | 'PASS' |
| constant | `GATE_SKIP` | 50 | 'SKIP' |
| class | **EnvironmentState** — abnormal, to_dict | 62 | Cheap, file-level sanity of the local install (no subprocesses) |
| function | `probe_environment(env: LocalEnv, *, logger: Optional[logging.Logger] = …) -> EnvironmentState` | 88 | Check the local environment without touching the network or spawning work |
| class | **GateResult** — blocking, to_dict | 127 |  |
| class | **GateReport** — blocking, warnings, blocked, environment_abnormal, blocked_by, earliest_recheck | 159 |  |
| function | `evaluate_gates(cfg: Config, *, provenance: CodeProvenance, decision: UpdateDecision, release: Optional[Release], assessment: Optional[RiskAssessment], clusters: list[RegressionCluster] = …, environment: Optional[EnvironmentState] = …, now: Optional[datetime] = …) -> GateReport` | 201 | Evaluate every configured gate |
| function | `describe_gate_lines(report: GateReport, *, lang: str = …) -> list[str]` | 471 | Compact one-line-per-gate rendering used by the report and the CLI |
| function | `severe_cluster_keys(clusters, *, threshold: str = …) -> list[str]` | 483 |  |
| function | `critical_cluster_keys(clusters) -> list[str]` | 490 |  |

### `github_api` — GitHub API access for the Hermes repository: releases, compares, issue searches

`hermes_update_check/github_api.py` (546 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `API_ROOT` | 21 | 'https://api.github.com' |
| constant | `COMPARE_COMMIT_LIMIT` | 24 | 250 |
| constant | `MAX_QUERY_LENGTH` | 27 | 250 |
| constant | `MAX_OR_TERMS` | 31 | 6 |
| class | **Release** — when, age_hours, age_days, to_dict | 35 | One GitHub release |
| class | **CommitInfo** — subject, pr_number | 82 |  |
| class | **CompareResult** — truncated, pr_numbers, commit_subjects, commit_corpus, to_dict | 101 |  |
| class | **Issue** — label_text, to_dict | 148 |  |
| constant | `MAINTAINER_ASSOCIATIONS` | 181 | {'OWNER', 'MEMBER', 'COLLABORATOR'} |
| function | `is_maintainer_association(value: str | None) -> bool` | 184 |  |
| class | **IssueComment** — from_maintainer, to_dict | 189 |  |
| class | **IssueSearchResult** | 209 |  |
| class | **GitHubClient** — list_releases, latest_release, compare, search_issues, get_rate_limit, get_issue_comments | 219 | Thin, typed wrapper over the three endpoints this tool needs |
| function | `extract_display_version(name: str, body: str, tag: str) -> Optional[str]` | 443 | Find the SemVer-ish display version behind a date tag |
| function | `extract_pr_count(text: str | None) -> Optional[int]` | 482 | Estimate the number of PRs a release bundles, from its own release notes |
| function | `build_issue_query(repo: str, *, created_after: Optional[datetime] = …, created_before: Optional[datetime] = …, keywords: Sequence[str] = …, label: Optional[str] = …, state: Optional[str] = …) -> str` | 506 | Compose a GitHub issue-search query string |
| function | `parse_repo_slug(slug: str) -> tuple[str, str]` | 542 |  |

### `health` — Post-update health checks: did the update actually leave a working install?

`hermes_update_check/health.py` (511 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| class | **HealthReport** — failures, warnings, healthy, to_dict, summary_line | 41 |  |
| function | `run_health_checks(cfg: Config, env: LocalEnv, *, state_root: Optional[Path] = …, logger: Optional[logging.Logger] = …, smoke_test: bool = …, timeout: float = …, gateway_timeout: float = …, gateway_was_running: Optional[bool] = …) -> HealthReport` | 75 | Run the full health suite |

### `http` — HTTP layer: stdlib-only client with timeouts, retries, on-disk cache and rate-limit awareness

`hermes_update_check/http.py` (309 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `MAX_CONSECUTIVE_FAILURES` | 34 | 4 |
| class | **HttpResult** — ok, degraded, header, to_dict | 38 | Outcome of one HTTP GET, including cache provenance |
| class | **DiskCache** — get, set, clear | 79 | Tiny JSON cache: one file per cache key, TTL checked on read |
| class | **HttpClient** — get_json, probe_rate_limit | 136 | Blocking JSON GET client with retry, cache and rate-limit handling |
| function | `rate_limit_message(reset_header: Optional[str]) -> str` | 282 |  |
| function | `require_ok(result: HttpResult, *, what: str) -> Any` | 303 | Turn an HttpResult into data or raise the right typed error |

### `i18n` — Tiny two-language (zh/en) translation helper

`hermes_update_check/i18n.py` (36 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| class | **Translator** — is_zh, t, set_lang | 13 | Pick one of the two strings based on the configured language |

### `local_env` — Local environment detection: what Hermes is installed here, how, and in what state

`hermes_update_check/local_env.py` (533 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `DEFAULT_TIMEOUT` | 26 | 30.0 |
| constant | `UPDATE_CHECK_TIMEOUT` | 27 | 90.0 |
| class | **GitState** — on_main, at_release_tag, clean, to_dict | 33 | git facts about the install directory (all optional / best-effort) |
| class | **LocalEnv** — config_path, env_file, state_db, logs_dir, sessions_dir, backups_dir | 81 | Everything we know about the local Hermes installation |
| function | `detect_local_env(cfg: Config, *, logger: Optional[logging.Logger] = …, run_upstream_check: bool = …, timeout: float = …) -> LocalEnv` | 175 | Inspect the local Hermes installation |
| function | `detect_git_state(install_dir: Optional[Path], *, log: Optional[logging.Logger] = …) -> GitState` | 274 | Collect git facts (no network: never fetches) |
| class | **ProcessInfo** — short | 396 |  |
| function | `list_hermes_processes(*, timeout: float = …) -> tuple[list[ProcessInfo], Optional[str]]` | 407 | Running processes that look like Hermes (best-effort, never fatal) |
| function | `gateway_status(hermes_cli: Optional[str], *, timeout: float = …) -> ProcResult` | 504 | Run ``hermes gateway status`` (empty ProcResult when the CLI is missing) |
| function | `parse_gateway_status(result: ProcResult) -> tuple[str, str]` | 511 | Classify gateway status output into PASS / WARN / FAIL + a short detail |
| function | `require_hermes_home(cfg: Config) -> Path` | 525 | Hard requirement for commands that touch the installation |

### `logging_setup` — Logging setup: file + optional console, quiet by default on stdout

`hermes_update_check/logging_setup.py` (73 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `LOGGER_NAME` | 14 | 'hermes_update_check' |
| constant | `_FORMAT` | 15 | '%(asctime)s %(levelname)-7s %(name)s: %(message)s' |
| constant | `_DATEFMT` | 16 | '%Y-%m-%d %H:%M:%S' |
| constant | `_BACKUPS` | 18 | 3 |
| function | `setup_logging(*, level: str = …, log_file: Path | None = …, console: bool = …) -> logging.Logger` | 21 | Configure the package logger exactly once and return it |
| function | `get_logger(name: str | None = …) -> logging.Logger` | 64 | Child logger; works even before setup_logging() was called |

### `notify` — Notification backends: one small interface, pluggable channels

`hermes_update_check/notify/__init__.py` (52 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| function | `build_notifiers(cfg) -> list[Notifier]` | 26 | Instantiate every configured notification channel |
| function | `notify_all(notifiers: list[Notifier], message: NotificationMessage) -> list[NotifyResult]` | 38 | Send to every channel; a failing channel never breaks the others |
| function | `describe_notifiers(notifiers: list[Notifier]) -> str` | 49 |  |

### `notify.base` — Notifier interface and the message object every channel receives

`hermes_update_check/notify/base.py` (68 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| class | **NotificationMessage** — to_text, to_dict | 10 | Channel-neutral notification payload |
| class | **NotifyResult** — to_dict | 49 |  |
| class | **Notifier** — ready, send | 58 | Base class: implement ``name``, ``ready`` and ``send`` |

### `notify.telegram` — Telegram channel (first-class notifier - `my chat` is where alerts are read)

`hermes_update_check/notify/telegram.py` (111 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `API_ROOT` | 27 | 'https://api.telegram.org' |
| constant | `MAX_LENGTH` | 28 | 4096 |
| class | **TelegramNotifier** — token, ready, send | 32 |  |
| function | `discover_chat_id(token: str, *, timeout: float = …) -> list[dict[str, object]]` | 91 | Helper for setup: list chats that recently messaged the bot |

### `notify.webhook` — Generic JSON webhook channel (Slack/Discord/n8n/your own endpoint)

`hermes_update_check/notify/webhook.py` (50 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| class | **WebhookNotifier** — ready, send | 15 |  |

### `preflight` — Pre-update checks: is this machine actually in a state where an update is safe to start?

`hermes_update_check/preflight.py` (485 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `STATUS_PASS` | 20 | 'PASS' |
| constant | `STATUS_WARN` | 21 | 'WARN' |
| constant | `STATUS_FAIL` | 22 | 'FAIL' |
| constant | `STATUS_SKIP` | 23 | 'SKIP' |
| class | **CheckResult** — blocking, ok, name, detail, remediation, to_dict | 29 |  |
| class | **PreflightReport** — failures, warnings, passed, blocking_failures, ok_to_proceed, to_dict | 66 |  |
| function | `run_preflight(cfg: Config, env: LocalEnv, *, state_root: Optional[Path] = …, logger: Optional[logging.Logger] = …, check_processes: bool = …, update_help_text: Optional[str] = …) -> PreflightReport` | 98 | Run every pre-update check |

### `provenance` — Code provenance: *what code is actually running*, not just what it calls itself

`hermes_update_check/provenance.py` (564 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `CHANNEL_STABLE` | 43 | 'STABLE' |
| constant | `CHANNEL_MAIN` | 44 | 'MAIN' |
| constant | `CHANNEL_PRERELEASE` | 45 | 'PRERELEASE' |
| constant | `CHANNEL_DETACHED` | 46 | 'DETACHED' |
| constant | `CHANNEL_CUSTOM` | 47 | 'CUSTOM' |
| constant | `CHANNEL_UNKNOWN` | 48 | 'UNKNOWN' |
| constant | `DEV_BRANCHES` | 59 | {'main', 'master', 'develop', 'development', 'dev'} |
| constant | `UPDATE_STATUS_UP_TO_DATE` | 62 | 'up_to_date' |
| constant | `UPDATE_STATUS_AVAILABLE` | 63 | 'update_available' |
| constant | `UPDATE_STATUS_AHEAD` | 64 | 'ahead_of_stable' |
| constant | `UPDATE_STATUS_MANUAL_REVIEW` | 65 | 'manual_review' |
| constant | `UPDATE_STATUS_UNKNOWN` | 66 | 'unknown' |
| class | **CodeProvenance** — ahead_of_stable, exact_release_tag, is_development_channel, channel_note_zh, channel_note_en, to_dict | 72 | Where the running code actually sits relative to the release tags |
| class | **UpdateDecision** — headline_zh, headline_en, to_dict | 152 | Is an update even the right question to ask? |
| function | `resolve_provenance(env: LocalEnv, releases: Sequence[Release] = …, *, latest: Optional[Release] = …, compare: Optional[CompareFn] = …) -> CodeProvenance` | 198 | Build the provenance model from the local environment plus GitHub data |
| function | `decide_update(prov: CodeProvenance, latest: Optional[Release], *, preferred_channel: str = …, allow_prerelease: bool = …) -> UpdateDecision` | 405 | Decide *whether* a newer release is the right thing to move to |
| function | `channel_mismatch(prov: CodeProvenance, preferred_channel: str) -> Optional[tuple[str, str]]` | 535 | Warning text when the install does not track the preferred channel |
| function | `now_version_age_days(published: Optional[datetime], *, now: Optional[datetime] = …) -> Optional[float]` | 559 |  |

### `report` — Report rendering: the human-readable answer, in Chinese or English

`hermes_update_check/report.py` (685 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `REPORT_TITLE` | 35 | 'Hermes Update Advisor' |
| class | **Reporter** — render, recommendation_lines, to_markdown | 49 | Renders an UpdateCheck for humans (rich) or machines (markdown/json) |

### `risk` — The risk engine: everything that turns observations into an Update Risk Score

`hermes_update_check/risk.py` (1195 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `LEVEL_LOW` | 53 | 'LOW' |
| constant | `LEVEL_LOW_MEDIUM` | 54 | 'LOW-MEDIUM' |
| constant | `LEVEL_MEDIUM` | 55 | 'MEDIUM' |
| constant | `LEVEL_HIGH` | 56 | 'HIGH' |
| constant | `LEVEL_VERY_HIGH` | 57 | 'VERY HIGH' |
| constant | `LEVEL_UNKNOWN` | 58 | 'UNKNOWN' |
| constant | `BAND_LOW_MAX` | 60 | 20 |
| constant | `BAND_LOW_MEDIUM_MAX` | 61 | 40 |
| constant | `BAND_MEDIUM_MAX` | 62 | 60 |
| constant | `BAND_HIGH_MAX` | 63 | 80 |
| constant | `RECOMMEND_UPDATE` | 65 | 'UPDATE' |
| constant | `RECOMMEND_WAIT` | 66 | 'WAIT' |
| constant | `RECOMMEND_AVOID` | 67 | 'AVOID' |
| constant | `RECOMMEND_UNKNOWN` | 68 | 'INSUFFICIENT_DATA' |
| constant | `RECOMMEND_UP_TO_DATE` | 69 | 'UP_TO_DATE' |
| function | `level_for_score(score: int | None) -> str` | 74 | Map a 0-100 score onto the documented bands |
| function | `is_severe_level(level: str) -> bool` | 89 |  |
| class | **KeywordCategory** — compiled | 99 |  |
| class | **KeywordHit** — intensity | 255 |  |
| class | **KeywordScan** — raw_points, matched_categories, total_hits | 272 |  |
| function | `scan_keywords(text: str | None, *, extra: Sequence[str] = …) -> KeywordScan` | 292 | Scan release notes / commit subjects for risk and low-risk language |
| function | `keyword_points(scan: KeywordScan, cap: float) -> float` | 344 | Scale the weighted keyword hits into the keyword group cap |
| function | `age_points(age_hours: Optional[float], cap: float) -> tuple[float, Optional[str]]` | 351 | Release-age contribution; very matured releases earn a small bonus |
| function | `commit_volume_points(total_commits: Optional[int], cap: float) -> tuple[float, Optional[str]]` | 370 | Diff-size contribution from the commit count |
| function | `pr_volume_points(pr_count: Optional[int], cap: float) -> float` | 387 |  |
| function | `release_type_points(*, latest_version: Optional[str], current_version: Optional[str], cap: float) -> tuple[float, Optional[str]]` | 401 | X.Y.0 feature releases and major bumps are treated more carefully than patches |
| function | `rollup_points(release_body: str | None, total_commits: Optional[int], cap: float) -> tuple[float, Optional[str]]` | 415 | A 'patch' that rolls up hundreds of PRs is a feature release in disguise |
| constant | `UNRELIABLE_ISSUE_COUNT` | 466 | 1000 |
| class | **IssueSignal** — available, to_dict | 470 | Aggregated post-release issue evidence (see ``checker.collect_issue_signal``) |
| function | `classify_issues(issues: Iterable[Issue]) -> dict[str, list[Issue]]` | 512 | Group issues into severe regression clusters by title/label text |
| function | `issue_volume_points(signal: Optional[IssueSignal], cap: float) -> tuple[float, list[tuple[str, str]]]` | 526 | The *quantitative* half of the issue analysis: how much noise is there? |
| function | `issue_points(signal: Optional[IssueSignal], cap: float) -> tuple[float, list[tuple[str, str]], Optional[str]]` | 619 | Post-release issue contribution (legacy additive view) |
| function | `context_points(*, tracking_main: bool, dirty_tree: bool, install_kind: str, target_prerelease: bool, cap: float) -> tuple[float, list[str]]` | 664 |  |
| function | `bonus_points(*, age_hours: Optional[float], scan: KeywordScan, signal: Optional[IssueSignal], total_commits: Optional[int], cap: float) -> tuple[float, list[str]]` | 692 | Stability bonuses (negative points), capped |
| class | **RiskFactor** — label, to_dict | 724 |  |
| class | **CheckContext** | 750 | Everything the risk engine is allowed to look at (fully mockable) |
| class | **RiskAssessment** — score_or_unknown, stability_or_unknown, component_rows, to_dict | 788 | The graded result |
| function | `assess(ctx: CheckContext) -> RiskAssessment` | 883 | Run every factor and combine them into the final assessment |
| function | `risk_threshold_check(score: int, ctx: CheckContext) -> int` | 1127 | Applied risk used for the recommendation (currently identical to the score) |

### `state` — State files: `update_state.json`, watch state, cache/snapshot directories

`hermes_update_check/state.py` (266 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `UPDATE_STATE_NAME` | 20 | 'update_state.json' |
| constant | `WATCH_STATE_NAME` | 21 | 'watch_state.json' |
| constant | `STATUS_IN_PROGRESS` | 23 | 'in_progress' |
| constant | `STATUS_SUCCEEDED` | 24 | 'succeeded' |
| constant | `STATUS_HEALTH_FAILED` | 25 | 'health_check_failed' |
| constant | `STATUS_ROLLED_BACK` | 26 | 'rolled_back' |
| constant | `STATUS_FAILED` | 27 | 'failed' |
| class | **UpdateState** — to_dict, from_dict, mark, created_at, human_summary | 31 | Pre-update facts needed to undo an update, plus the outcome |
| class | **WatchState** — record, mark_notified, to_dict, from_dict | 96 | What watch mode remembers between runs, so it can stay silent when nothing changed |
| function | `confidence_bucket(confidence: int) -> str` | 177 | Coarse confidence bands - small numeric wobbles must not trigger notifications |
| class | **StateStore** — cache_dir, logs_dir, snapshots_dir, reports_dir, log_file, update_state_path | 186 | Filesystem layout + read/write helpers for all persisted state |

### `updater` — Update execution: snapshot -> update -> health check -> (rollback)

`hermes_update_check/updater.py` (604 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| class | **SnapshotEntry** — to_dict | 53 |  |
| class | **Snapshot** — to_dict | 75 |  |
| class | **UpdateOutcome** — to_dict | 91 |  |
| class | **RollbackOutcome** — to_dict | 121 |  |
| function | `create_snapshot(env: LocalEnv, state_root: Path, *, logger: Optional[logging.Logger] = …) -> Snapshot` | 147 | Fingerprint (and partially copy) the Hermes home before an update |
| function | `build_update_command(cfg: Config, env: LocalEnv, *, backup: bool, yes: bool, branch: Optional[str] = …, extra_args: Sequence[str] = …) -> list[str]` | 223 | Compose the ``hermes update`` invocation (nothing is executed here) |
| function | `run_update(cfg: Config, env: LocalEnv, *, state_root: Optional[Path] = …, backup: bool = …, yes: bool = …, branch: Optional[str] = …, extra_args: Sequence[str] = …, dry_run: bool = …, skip_health_check: bool = …, auto_rollback: bool = …, on_line: LineCallback = …, logger: Optional[logging.Logger] = …, timeout: float = …) -> UpdateOutcome` | 245 | Execute the update |
| function | `pre_update_gateway_running(env: LocalEnv, *, logger: Optional[logging.Logger] = …) -> Optional[bool]` | 401 | Was the gateway running *before* we touched anything? |
| function | `dependency_install_command(env: LocalEnv) -> list[str]` | 420 | How to reinstall Hermes' Python dependencies after a git checkout |
| function | `run_rollback(cfg: Config, env: LocalEnv, *, store: Optional[StateStore] = …, state: Optional[UpdateState] = …, yes: bool = …, to_ref: Optional[str] = …, reinstall_deps: bool = …, restore_backup: Optional[str] = …, in_place_restore: bool = …, dry_run: bool = …, on_line: LineCallback = …, logger: Optional[logging.Logger] = …, timeout: float = …) -> RollbackOutcome` | 434 | Restore the pre-update version: git ref + dependencies (+ optional backup) |

### `util` — Small, dependency-free helpers: subprocess runner, JSON IO, time parsing, hashing

`hermes_update_check/util.py` (512 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| function | `utcnow() -> datetime` | 29 | Timezone-aware UTC now (naive datetimes are a bug factory) |
| function | `parse_iso8601(value: str | None) -> datetime | None` | 34 | Parse the ISO-8601 timestamps the GitHub API returns (``...Z`` included) |
| function | `hours_between(older: datetime, newer: datetime | None = …) -> float` | 57 | Hours from ``older`` to ``newer`` (default: now) |
| function | `humanize_hours(hours: float, *, lang: str = …) -> str` | 63 | '3.2 days', '5.4 hours', '2.1 months' style age string |
| function | `iso(dt: datetime | None) -> str | None` | 77 | Serialise a datetime in UTC ISO-8601 with a trailing Z |
| function | `format_local(dt: datetime | None) -> str` | 84 |  |
| function | `later_of(*dts) -> datetime | None` | 90 |  |
| function | `days_ago(days: float, *, now: datetime | None = …) -> datetime` | 95 |  |
| function | `ensure_dir(path: Path) -> Path` | 104 |  |
| function | `read_json(path: Path) -> Any | None` | 109 | Read JSON; return None when the file is missing or corrupt |
| function | `write_json(path: Path, data: Any) -> Path` | 118 | Atomically write JSON (tmp file + os.replace) so a crashed run cannot corrupt state |
| function | `write_text(path: Path, text: str) -> Path` | 135 |  |
| class | **suppress_oserror** | 150 | ``contextlib.suppress(OSError)`` without the import ceremony |
| function | `sha256_file(path: Path, *, chunk: int = …) -> str | None` | 160 | Hash a file (None when unreadable); used to record config fingerprints |
| function | `dir_size_bytes(path: Path, *, limit_files: int = …) -> int` | 175 | Best-effort recursive size; never raises |
| function | `human_bytes(num: float) -> str` | 194 |  |
| function | `disk_free_bytes(path: Path | str) -> int | None` | 202 |  |
| class | **ProcResult** — ok, output, first_lines, to_dict | 215 | Outcome of an external command |
| function | `resolve_executable(name: str | os.PathLike[str], *, extra_dirs: Sequence[Path] = …) -> str | None` | 258 | Find an executable cross-platform |
| function | `run_process(cmd: Sequence[str], *, timeout: float = …, cwd: Path | None = …, env: Mapping[str, str] | None = …, stdin_text: str | None = …) -> ProcResult` | 285 | Run a command with a hard timeout; capture output; never raise |
| function | `wrap_command(argv: Sequence[str]) -> list[str]` | 368 | Public wrapper: make an argv list executable on this platform (Windows .cmd/.bat/.ps1) |
| function | `run_streaming(cmd: Sequence[str], *, timeout: float = …, cwd: Path | None = …, env: Mapping[str, str] | None = …, on_line: Optional[Callable[[str], None]] = …, max_buffer_chars: int = …) -> ProcResult` | 373 | Run a long command, streaming its output line by line to ``on_line`` |
| function | `platform_summary() -> dict[str, str]` | 479 |  |
| function | `unique_preserve_order(items: Iterable[str]) -> list[str]` | 491 |  |
| function | `clamp(value: float, low: float, high: float) -> float` | 501 |  |
| class | **Degradation** | 506 | A note about data we could not obtain (rendered as UNKNOWN, never as 'safe') |

### `versioning` — Version parsing and comparison

`hermes_update_check/versioning.py` (299 lines)

| kind | symbol | line | purpose |
|---|---|---|---|
| constant | `_PRE_RANK` | 42 | {'dev': 0, 'alpha': 1, 'beta': 2, 'pre': 2, 'rc': 3} |
| class | **Version** — is_prerelease, key | 46 | A comparable semantic version |
| function | `parse_version(text: str | None) -> Optional[Version]` | 68 | Parse ``v0.21.3`` / ``0.21`` / ``0.21.3-rc1`` into a Version (None when hopeless) |
| function | `semver_key(text: str | None) -> Tuple[int, int, int, int, int]` | 88 | Sort key for a version string; unparseable versions sort lowest |
| function | `compare_versions(left: str | None, right: str | None) -> int` | 94 | -1 if left < right, 0 if equal, 1 if left > right (unparseable sorts last) |
| function | `parse_calver(text: str | None) -> Optional[Tuple[int, int, int, int]]` | 102 | Parse date tags such as ``v2026.9.14`` / ``2026.8.16.2`` |
| function | `compare_tags(left: str | None, right: str | None) -> int` | 117 | Compare two release tags: prefer calver, fall back to semver, then string order |
| function | `classify_bump(current: str | None, target: str | None) -> str` | 134 | 'major' | 'minor' | 'patch' | 'none' | 'unknown' |
| function | `is_feature_release(version_text: str | None) -> bool` | 148 | X.Y.0 releases carry new features and deserve extra caution |
| function | `extract_versions(text: str | None) -> list[str]` | 154 | Every version-looking token in a blob of text (release notes, changelogs) |
| function | `is_prerelease_text(text: str | None) -> bool` | 161 | True for prerelease/beta/rc markers inside a version or a release title |
| class | **VersionOutput** — version_or_unknown | 196 | Structured form of `hermes --version` |
| function | `parse_version_output(text: str | None) -> VersionOutput` | 216 | Parse the output of ``hermes --version`` across known layouts |
| function | `normalise_tag(tag: str | None) -> Optional[str]` | 268 | Public alias: ``2026.9.14`` -> ``v2026.9.14`` |
| function | `latest_by_version(items: Iterable[str]) -> Optional[str]` | 273 | Highest version in a list of version strings (None for an empty list) |
| function | `versions_between(current: str | None, ordered_desc: Sequence[str]) -> Optional[int]` | 282 | How many releases sit between ``current`` and the newest entry of a descending list |

## Tests

| file | tests | lines | focus |
|---|---|---|---|
| [`tests/test_advisor.py`](../tests/test_advisor.py) | 15 | 234 | Advisor tests: priority order, recheck computation, and 'unknown is not safe' |
| [`tests/test_checker.py`](../tests/test_checker.py) | 13 | 274 | Orchestration tests: run_check with an injected (fake) GitHub client |
| [`tests/test_cli.py`](../tests/test_cli.py) | 13 | 210 | CLI-level tests: exit codes, JSON output, commands that never touch the network |
| [`tests/test_cli_commands.py`](../tests/test_cli_commands.py) | 13 | 298 | End-to-end-ish command tests: every CLI handler, with the outside world stubbed |
| [`tests/test_clusters.py`](../tests/test_clusters.py) | 16 | 274 | Regression-cluster tests: grading, independence and credibility |
| [`tests/test_config.py`](../tests/test_config.py) | 19 | 218 | Config loading/validation tests |
| [`tests/test_entrypoints.py`](../tests/test_entrypoints.py) | 10 | 122 | Smoke tests for the two entry points that had no direct coverage |
| [`tests/test_gates.py`](../tests/test_gates.py) | 20 | 370 | Hard-gate tests: gates must override a low score and never be bypassed silently |
| [`tests/test_github_api.py`](../tests/test_github_api.py) | 12 | 215 | GitHub payload parsing tests (fixtures only - no network) |
| [`tests/test_http_cache.py`](../tests/test_http_cache.py) | 10 | 118 | HTTP layer tests: cache TTL, stale fallback, failure handling (no external network) |
| [`tests/test_local_env.py`](../tests/test_local_env.py) | 19 | 271 | Environment-detection tests: the layer that reads the *real* machine |
| [`tests/test_notify.py`](../tests/test_notify.py) | 14 | 257 | Notification-channel tests: payload shape, failure handling, no secret leakage |
| [`tests/test_preflight_health.py`](../tests/test_preflight_health.py) | 13 | 184 | Preflight and health-check tests (local filesystem only) |
| [`tests/test_provenance.py`](../tests/test_provenance.py) | 16 | 274 | Code provenance tests: the five cases from the design brief, plus the rest |
| [`tests/test_repo_hygiene.py`](../tests/test_repo_hygiene.py) | 3 | 74 | Repository hygiene: nothing important may be silently ignored or stale |
| [`tests/test_report.py`](../tests/test_report.py) | 9 | 138 | Report rendering tests (plain-text console, no network) |
| [`tests/test_risk.py`](../tests/test_risk.py) | 33 | 564 | Risk-engine tests: the scoring rules are the product, so they are pinned here |
| [`tests/test_scan_secrets.py`](../tests/test_scan_secrets.py) | 14 | 222 | Tests for the secret/privacy scanner - the guard needs its own guard |
| [`tests/test_state.py`](../tests/test_state.py) | 9 | 130 | State persistence tests: update_state.json and watch_state.json |
| [`tests/test_updater.py`](../tests/test_updater.py) | 19 | 344 | Updater mechanics: command building, snapshots, dry runs (nothing is executed) |
| [`tests/test_util.py`](../tests/test_util.py) | 28 | 269 | Unit tests for the shared helpers - the layer every other module leans on |
| [`tests/test_versioning.py`](../tests/test_versioning.py) | 12 | 125 | Version parsing/comparison tests - the part that must never be wrong |
| [`tests/test_watch.py`](../tests/test_watch.py) | 14 | 194 | Watch-mode delta tests: only meaningful transitions may notify |
