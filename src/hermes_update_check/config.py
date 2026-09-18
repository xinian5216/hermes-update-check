"""Configuration loading, validation and path resolution.

Precedence (low -> high):
    built-in defaults  <  config file  <  HERMES_UPDATE_CHECK_* env vars  <  CLI flags

Two invariants matter for safety and are enforced here:

* ``auto_update`` defaults to false and every read of it is logged;
* unknown keys produce warnings instead of silence, so a typo in the risk gate
  ("risk_threshold" vs "risk_thresold") cannot quietly disable protection.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Optional

from . import TOOL_NAME
from .errors import ConfigError
from .usage_profile import UsageProfile

try:  # pragma: no cover - environment dependent
    import yaml

    _YAML_AVAILABLE = True
except Exception:  # pragma: no cover - environment dependent
    yaml = None  # type: ignore[assignment]
    _YAML_AVAILABLE = False

ENV_PREFIX = "HERMES_UPDATE_CHECK_"
CONFIG_ENV_VAR = ENV_PREFIX + "CONFIG"

DEFAULTS: dict[str, Any] = {
    "risk_threshold": 40,
    "minimum_release_age_days": 0,
    "auto_update": False,
    "backup_before_update": True,
    "check_github_issues": True,
    "check_issue_baseline": True,
    "preferred_channel": "stable",
    "allow_prerelease": False,
    "language": "zh",
    "plain_output": False,
    "github": {
        "repo": "NousResearch/hermes-agent",
        "token_env": "GITHUB_TOKEN",
        "token_env_fallbacks": ["GH_TOKEN", ENV_PREFIX + "GITHUB_TOKEN"],
        "cache_ttl_minutes": 360,
        "max_issue_searches": 3,
        # how many issues per run get their comments fetched (1 API call each)
        "issue_enrichment_limit": 3,
    },
    "network": {"timeout_seconds": 15, "retries": 2},
    "paths": {"hermes_home": None, "state_dir": None},
    "hard_gates": {
        "enabled": True,
        # phase 3: only these five things still block (see docs/README section 六)
        "block_on_systemic_risk": True,
        "block_on_critical_workflow": True,
        "block_on_rollback_safety": True,
        "block_on_insufficient_data": True,
        # local safety, not release quality - still a blocker by default
        "block_dirty_worktree": True,
        # phase 3: demoted to warnings (set true only if you want the old behaviour)
        "block_main_branch_update": False,
        "block_prerelease": False,
        "warn_active_gateway_regression": True,
        "warn_active_mcp_regression": True,
        "warn_active_provider_regression": True,
        "warn_active_auth_regression": True,
        "warn_active_crash_regression": True,
    },
    "release_age_policy": {
        "block_hours": 6,
        "caution_hours": 12,
        "acceptable_hours": 24,
    },
    "smoke_tests": {
        "cli_start": True,
        "session_open": True,
        "config_load": True,
        "gateway": False,
        "mcp_load": False,
    },
    "usage_profile": {
        "features": {},
        "providers": {},
    },
    "update": {
        "branch": "main",
        "extra_args": [],
        "restart_gateway": False,
        "health_check_after_update": True,
        "auto_rollback_on_failed_health": False,
        "min_free_disk_gib": 2.0,
    },
    "watch": {
        "notify_on_new_release": True,
        "notify_on_risk_improvement": True,
        "notify_when_safe": True,
        "notify_on_first_run": False,
        "min_interval_hours": 20,
    },
    "notify": {
        "telegram": {"enabled": False, "bot_token_env": ENV_PREFIX + "TELEGRAM_TOKEN", "chat_id": ""},
        "webhook": {"enabled": False, "url": "", "bearer_token_env": ENV_PREFIX + "WEBHOOK_TOKEN"},
    },
    "logging": {"level": "INFO", "file": None, "console": False},
    "local_overrides": {
        "enabled": True,
        "block_unknown_changes": True,
        "block_drifted_overrides": True,
        "auto_preserve": True,
        "reapply": {"strategy": "three_way", "block_on_low_confidence": False},
        "backup": {"patches": True, "keep_versions": 10},
    },
    "risk": {
        "keyword_cap": 40,
        "age_cap": 18,
        "volume_cap": 18,
        "issues_cap": 30,
        "context_cap": 12,
        "bonus_cap": 15,
    },
}


#: Phase-2 keys that are still *accepted* (and reported as deprecated) but are no
#: longer part of the active defaults. Keeping them in the schema means an old config
#: loads without "unknown key" noise (doc section 27).
LEGACY_KEYS: dict[str, Any] = {
    "hard_gates": {
        "minimum_release_age_hours": None,
        "block_active_database_regression": None,
        "block_active_session_regression": None,
        "block_active_gateway_regression": None,
        "block_active_update_failure": None,
    },
}


def _config_schema() -> dict[str, Any]:
    """DEFAULTS plus the legacy keys - used only for the unknown-key warning."""
    schema = {key: (_copy_value(value) if isinstance(value, Mapping) else value) for key, value in DEFAULTS.items()}
    for section, values in LEGACY_KEYS.items():
        schema.setdefault(section, {})
        if isinstance(schema[section], Mapping):
            schema[section] = {**schema[section], **values}
    return schema


# --------------------------------------------------------------------------- #
# dataclasses (typed view over the merged mapping)
# --------------------------------------------------------------------------- #


@dataclass
class GitHubConfig:
    repo: str = "NousResearch/hermes-agent"
    token_env: str = "GITHUB_TOKEN"
    token_env_fallbacks: list[str] = field(default_factory=list)
    cache_ttl_minutes: int = 360
    max_issue_searches: int = 3
    issue_enrichment_limit: int = 3


@dataclass
class HardGateConfig:
    """Which rules may still stop an update (phase 3 keeps this list short)."""

    enabled: bool = True
    # blocking
    block_on_systemic_risk: bool = True
    block_on_critical_workflow: bool = True
    block_on_rollback_safety: bool = True
    block_on_insufficient_data: bool = True
    block_dirty_worktree: bool = True
    # phase 4: unregistered/drifted changes are what actually blocks; changes
    # registered as managed overrides and unchanged since registration do not.
    block_unknown_changes: bool = True
    block_drifted_overrides: bool = True
    # warnings (caps the verdict at ACCEPTABLE)
    block_main_branch_update: bool = False
    block_prerelease: bool = False
    warn_active_gateway_regression: bool = True
    warn_active_mcp_regression: bool = True
    warn_active_provider_regression: bool = True
    warn_active_auth_regression: bool = True
    warn_active_crash_regression: bool = True
    # --- deprecated (phase 2) ------------------------------------------------
    # These still load so an old config keeps working; the migration in
    # `_migrate_legacy_gates` maps them onto the phase-3 switches and the report
    # says what to change. `None` means "not present in the file".
    minimum_release_age_hours: Optional[float] = None
    block_active_database_regression: Optional[bool] = None
    block_active_session_regression: Optional[bool] = None
    block_active_gateway_regression: Optional[bool] = None
    block_active_update_failure: Optional[bool] = None


@dataclass
class LocalOverridesConfig:
    """Managed local overrides (phase 4): intentional customization is not corruption."""

    enabled: bool = True
    block_unknown_changes: bool = True
    block_drifted_overrides: bool = True
    auto_preserve: bool = True
    reapply_strategy: str = "three_way"
    block_on_low_confidence: bool = False
    backup_patches: bool = True
    keep_versions: int = 10

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "block_unknown_changes": self.block_unknown_changes,
            "block_drifted_overrides": self.block_drifted_overrides,
            "auto_preserve": self.auto_preserve,
            "reapply": {"strategy": self.reapply_strategy, "block_on_low_confidence": self.block_on_low_confidence},
            "backup": {"patches": self.backup_patches, "keep_versions": self.keep_versions},
        }


@dataclass
class ReleaseAgePolicy:
    """Segmented release-age policy (doc section 9).

    ``< block_hours`` blocks, the caution band caps the verdict at ACCEPTABLE,
    ``acceptable_hours`` marks the end of the caution zone. Anything older is
    "normal" and is only reflected in Change Risk.
    """

    block_hours: float = 6.0
    caution_hours: float = 12.0
    acceptable_hours: float = 24.0

    def band(self, age_hours: float) -> str:
        if age_hours < self.block_hours:
            return "block"
        if age_hours < self.caution_hours:
            return "caution"
        if age_hours < self.acceptable_hours:
            return "acceptable"
        return "normal"

    def to_dict(self) -> dict[str, float]:
        return {
            "block_hours": self.block_hours,
            "caution_hours": self.caution_hours,
            "acceptable_hours": self.acceptable_hours,
        }


@dataclass
class SmokeTestConfig:
    """Post-update smoke tests (doc section 20); all read-only by design."""

    cli_start: bool = True
    session_open: bool = True
    config_load: bool = True
    gateway: bool = False
    mcp_load: bool = False

    def enabled_tests(self) -> list[str]:
        return [
            name
            for name in ("cli_start", "session_open", "config_load", "gateway", "mcp_load")
            if getattr(self, name, False)
        ]

    def to_dict(self) -> dict[str, bool]:
        return {
            "cli_start": self.cli_start,
            "session_open": self.session_open,
            "config_load": self.config_load,
            "gateway": self.gateway,
            "mcp_load": self.mcp_load,
        }


@dataclass
class NetworkConfig:
    timeout_seconds: float = 15.0
    retries: int = 2


@dataclass
class PathsConfig:
    hermes_home: Path | None = None
    state_dir: Path | None = None


@dataclass
class UpdateConfig:
    branch: str = "main"
    extra_args: list[str] = field(default_factory=list)
    restart_gateway: bool = False
    health_check_after_update: bool = True
    auto_rollback_on_failed_health: bool = False
    min_free_disk_gib: float = 2.0


@dataclass
class WatchConfig:
    notify_on_new_release: bool = True
    notify_on_risk_improvement: bool = True
    notify_when_safe: bool = True
    notify_on_first_run: bool = False
    min_interval_hours: float = 20.0


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token_env: str = ENV_PREFIX + "TELEGRAM_TOKEN"
    chat_id: str = ""


@dataclass
class WebhookConfig:
    enabled: bool = False
    url: str = ""
    bearer_token_env: str = ENV_PREFIX + "WEBHOOK_TOKEN"


@dataclass
class NotifyConfig:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: Path | None = None
    console: bool = False


@dataclass
class RiskWeights:
    keyword_cap: float = 40.0
    age_cap: float = 18.0
    volume_cap: float = 18.0
    issues_cap: float = 30.0
    context_cap: float = 12.0
    bonus_cap: float = 15.0


@dataclass
class Config:
    risk_threshold: int = 40
    minimum_release_age_days: float = 5.0
    auto_update: bool = False
    backup_before_update: bool = True
    check_github_issues: bool = True
    check_issue_baseline: bool = True
    preferred_channel: str = "stable"
    allow_prerelease: bool = False
    language: str = "zh"
    plain_output: bool = False
    github: GitHubConfig = field(default_factory=GitHubConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    risk: RiskWeights = field(default_factory=RiskWeights)
    hard_gates: HardGateConfig = field(default_factory=HardGateConfig)
    release_age_policy: ReleaseAgePolicy = field(default_factory=ReleaseAgePolicy)
    smoke_tests: SmokeTestConfig = field(default_factory=SmokeTestConfig)
    local_overrides: LocalOverridesConfig = field(default_factory=LocalOverridesConfig)
    usage_profile: UsageProfile = field(default_factory=UsageProfile)
    #: deprecation notices produced while loading (shown by `config show`)
    deprecated: list[str] = field(default_factory=list)
    # bookkeeping
    source_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def repo(self) -> str:
        return self.github.repo

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name in {"raw"}:
                continue
            value = getattr(self, f.name)
            out[f.name] = _serialise(value)
        out["source_path"] = str(self.source_path) if self.source_path else None
        return out


def _serialise(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [_serialise(v) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        return {f.name: _serialise(getattr(value, f.name)) for f in fields(value)}
    return value


# --------------------------------------------------------------------------- #
# default paths
# --------------------------------------------------------------------------- #


def default_config_path() -> Path:
    """`~/.config/hermes-update-check/config.yaml` (Windows: `%APPDATA%\\...`)."""
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / TOOL_NAME / "config.yaml"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / TOOL_NAME / "config.yaml"
    return Path.home() / ".config" / TOOL_NAME / "config.yaml"


def default_state_dir() -> Path:
    """`~/.hermes-update-check` - state, cache, snapshots, logs, update_state.json."""
    override = os.environ.get(ENV_PREFIX + "STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ("." + TOOL_NAME)


def resolve_hermes_home(cfg: Config | None = None) -> Path:
    """Resolve $HERMES_HOME the way Hermes does, falling back to ~/.hermes.

    Never hardcode the path: profiles and VPS installs routinely move it.
    """
    if cfg is not None and cfg.paths.hermes_home is not None:
        return cfg.paths.hermes_home.expanduser()
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".hermes"


def resolve_state_dir(cfg: Config | None = None) -> Path:
    if cfg is not None and cfg.paths.state_dir is not None:
        return cfg.paths.state_dir.expanduser()
    return default_state_dir()


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #


def load_config(
    path: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Load, merge and validate configuration. Raises ConfigError only for fatal problems."""
    env_map: Mapping[str, str] = env if env is not None else os.environ
    warnings: list[str] = []

    if path is None:
        raw_path = env_map.get(CONFIG_ENV_VAR)
        config_path = Path(raw_path).expanduser() if raw_path else default_config_path()
    else:
        config_path = Path(path).expanduser()

    file_data: dict[str, Any] = {}
    if config_path.exists():
        file_data = _read_config_file(config_path, warnings)
    merged = deep_merge(DEFAULTS, file_data)

    _apply_env_overrides(merged, env_map)

    cfg = _build_config(merged, warnings)
    cfg.source_path = config_path if config_path.exists() else None
    _validate(cfg, warnings)
    cfg.warnings = warnings
    return cfg


def _read_config_file(path: Path, warnings: list[str]) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc

    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        if not _YAML_AVAILABLE:
            raise ConfigError(
                f"PyYAML is required to read {path}",
                hint="pip install PyYAML   (or use a JSON config file: --config config.json)",
            )
        try:
            data = yaml.safe_load(text)  # type: ignore[union-attr]
        except Exception as exc:  # yaml.YAMLError and friends
            raise ConfigError(
                f"invalid YAML in {path}: {exc}",
                hint="check indentation; run `hermes-update-check config show` after fixing",
            ) from exc
    else:
        import json

        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ConfigError(f"invalid JSON in {path}: {exc}") from exc

    if data is None:
        warnings.append(f"config file {path} is empty; using defaults")
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must contain a mapping at the top level")
    return data


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursive merge; `override` wins. Lists and scalars are replaced, not merged."""
    result: dict[str, Any] = {k: _copy_value(v) for k, v in base.items()}
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge(result[key], value)  # type: ignore[arg-type]
        else:
            result[key] = _copy_value(value)
    return result


def _copy_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _copy_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_value(v) for v in value]
    return value


def _apply_env_overrides(merged: MutableMapping[str, Any], env: Mapping[str, str]) -> None:
    """Scalar env overrides for the knobs that matter on a headless VPS."""
    scalar_map: dict[str, tuple[str, type]] = {
        "RISK_THRESHOLD": ("risk_threshold", int),
        "MINIMUM_RELEASE_AGE_DAYS": ("minimum_release_age_days", float),
        "AUTO_UPDATE": ("auto_update", _as_bool),
        "BACKUP_BEFORE_UPDATE": ("backup_before_update", _as_bool),
        "CHECK_GITHUB_ISSUES": ("check_github_issues", _as_bool),
        "CHECK_ISSUE_BASELINE": ("check_issue_baseline", _as_bool),
        "PREFERRED_CHANNEL": ("preferred_channel", str),
        "ALLOW_PRERELEASE": ("allow_prerelease", _as_bool),
        "LANGUAGE": ("language", str),
        "PLAIN_OUTPUT": ("plain_output", _as_bool),
    }
    for env_suffix, (key, caster) in scalar_map.items():
        raw = env.get(ENV_PREFIX + env_suffix)
        if raw is None or raw == "":
            continue
        try:
            merged[key] = caster(raw)  # type: ignore[operator]
        except (TypeError, ValueError):
            pass

    nested: dict[str, tuple[tuple[str, ...], type]] = {
        "STATE_DIR": (("paths", "state_dir"), str),
        "HERMES_HOME": (("paths", "hermes_home"), str),
        "LOG_LEVEL": (("logging", "level"), str),
        "REPO": (("github", "repo"), str),
        "TIMEOUT_SECONDS": (("network", "timeout_seconds"), float),
        "CACHE_TTL_MINUTES": (("github", "cache_ttl_minutes"), int),
    }
    for env_suffix, (key_path, caster) in nested.items():
        raw = env.get(ENV_PREFIX + env_suffix)
        if raw is None or raw == "":
            continue
        node: Any = merged
        for part in key_path[:-1]:
            node = node.setdefault(part, {})
        try:
            node[key_path[-1]] = caster(raw)  # type: ignore[operator]
        except (TypeError, ValueError):
            pass


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _build_config(data: Mapping[str, Any], warnings: list[str]) -> Config:
    cfg = Config(
        risk_threshold=_as_int(data.get("risk_threshold"), 40),
        minimum_release_age_days=_as_float(data.get("minimum_release_age_days"), 5.0),
        auto_update=bool(data.get("auto_update", False)),
        backup_before_update=bool(data.get("backup_before_update", True)),
        check_github_issues=bool(data.get("check_github_issues", True)),
        check_issue_baseline=bool(data.get("check_issue_baseline", True)),
        preferred_channel=str(data.get("preferred_channel", "stable")).lower(),
        allow_prerelease=bool(data.get("allow_prerelease", False)),
        language=str(data.get("language", "zh")).lower(),
        plain_output=bool(data.get("plain_output", False)),
        raw=dict(data),
    )

    gh = data.get("github") or {}
    cfg.github = GitHubConfig(
        repo=str(gh.get("repo", DEFAULTS["github"]["repo"])),
        token_env=str(gh.get("token_env", DEFAULTS["github"]["token_env"])),
        token_env_fallbacks=[str(x) for x in (gh.get("token_env_fallbacks") or [])],
        cache_ttl_minutes=_as_int(gh.get("cache_ttl_minutes"), 360),
        max_issue_searches=_as_int(gh.get("max_issue_searches"), 3),
        issue_enrichment_limit=_as_int(gh.get("issue_enrichment_limit"), 3),
    )

    overrides_cfg = data.get("local_overrides") or {}
    reapply_cfg = overrides_cfg.get("reapply") or {}
    backup_cfg = overrides_cfg.get("backup") or {}

    hg = data.get("hard_gates") or {}
    cfg.hard_gates = HardGateConfig(
        enabled=bool(hg.get("enabled", True)),
        block_on_systemic_risk=bool(hg.get("block_on_systemic_risk", True)),
        block_on_critical_workflow=bool(hg.get("block_on_critical_workflow", True)),
        block_on_rollback_safety=bool(hg.get("block_on_rollback_safety", True)),
        block_dirty_worktree=bool(hg.get("block_dirty_worktree", True)),
        block_unknown_changes=bool(overrides_cfg.get("block_unknown_changes", True)),
        block_drifted_overrides=bool(overrides_cfg.get("block_drifted_overrides", True)),
        block_main_branch_update=bool(hg.get("block_main_branch_update", False)),
        block_prerelease=bool(hg.get("block_prerelease", False)),
        warn_active_gateway_regression=bool(hg.get("warn_active_gateway_regression", True)),
        warn_active_mcp_regression=bool(hg.get("warn_active_mcp_regression", True)),
        warn_active_provider_regression=bool(hg.get("warn_active_provider_regression", True)),
        warn_active_auth_regression=bool(hg.get("warn_active_auth_regression", True)),
        warn_active_crash_regression=bool(hg.get("warn_active_crash_regression", True)),
        block_on_insufficient_data=bool(hg.get("block_on_insufficient_data", True)),
        minimum_release_age_hours=(
            _as_float(hg.get("minimum_release_age_hours"), 0.0) if "minimum_release_age_hours" in hg else None
        ),
        block_active_database_regression=(
            bool(hg["block_active_database_regression"]) if "block_active_database_regression" in hg else None
        ),
        block_active_session_regression=(
            bool(hg["block_active_session_regression"]) if "block_active_session_regression" in hg else None
        ),
        block_active_gateway_regression=(
            bool(hg["block_active_gateway_regression"]) if "block_active_gateway_regression" in hg else None
        ),
        block_active_update_failure=(
            bool(hg["block_active_update_failure"]) if "block_active_update_failure" in hg else None
        ),
    )

    policy = data.get("release_age_policy") or {}
    cfg.release_age_policy = ReleaseAgePolicy(
        block_hours=_as_float(policy.get("block_hours"), 6.0),
        caution_hours=_as_float(policy.get("caution_hours"), 12.0),
        acceptable_hours=_as_float(policy.get("acceptable_hours"), 24.0),
    )

    smoke = data.get("smoke_tests") or {}
    cfg.smoke_tests = SmokeTestConfig(
        cli_start=bool(smoke.get("cli_start", True)),
        session_open=bool(smoke.get("session_open", True)),
        config_load=bool(smoke.get("config_load", True)),
        gateway=bool(smoke.get("gateway", False)),
        mcp_load=bool(smoke.get("mcp_load", False)),
    )

    cfg.local_overrides = LocalOverridesConfig(
        enabled=bool(overrides_cfg.get("enabled", True)),
        block_unknown_changes=bool(overrides_cfg.get("block_unknown_changes", True)),
        block_drifted_overrides=bool(overrides_cfg.get("block_drifted_overrides", True)),
        auto_preserve=bool(overrides_cfg.get("auto_preserve", True)),
        reapply_strategy=str(reapply_cfg.get("strategy", "three_way")),
        block_on_low_confidence=bool(reapply_cfg.get("block_on_low_confidence", False)),
        backup_patches=bool(backup_cfg.get("patches", True)),
        keep_versions=_as_int(backup_cfg.get("keep_versions"), 10),
    )

    profile = data.get("usage_profile")
    cfg.usage_profile = UsageProfile.from_dict(profile if isinstance(profile, Mapping) else None, warnings=warnings)

    _migrate_legacy_config(cfg, data, warnings)

    net = data.get("network") or {}
    cfg.network = NetworkConfig(
        timeout_seconds=_as_float(net.get("timeout_seconds"), 15.0),
        retries=_as_int(net.get("retries"), 2),
    )

    paths = data.get("paths") or {}
    cfg.paths = PathsConfig(
        hermes_home=_as_path(paths.get("hermes_home")),
        state_dir=_as_path(paths.get("state_dir")),
    )

    upd = data.get("update") or {}
    cfg.update = UpdateConfig(
        branch=str(upd.get("branch", "main")),
        extra_args=[str(x) for x in (upd.get("extra_args") or [])],
        restart_gateway=bool(upd.get("restart_gateway", False)),
        health_check_after_update=bool(upd.get("health_check_after_update", True)),
        auto_rollback_on_failed_health=bool(upd.get("auto_rollback_on_failed_health", False)),
        min_free_disk_gib=_as_float(upd.get("min_free_disk_gib"), 2.0),
    )

    watch = data.get("watch") or {}
    cfg.watch = WatchConfig(
        notify_on_new_release=bool(watch.get("notify_on_new_release", True)),
        notify_on_risk_improvement=bool(watch.get("notify_on_risk_improvement", True)),
        notify_when_safe=bool(watch.get("notify_when_safe", True)),
        notify_on_first_run=bool(watch.get("notify_on_first_run", False)),
        min_interval_hours=_as_float(watch.get("min_interval_hours"), 20.0),
    )

    notify = data.get("notify") or {}
    tg = notify.get("telegram") or {}
    hook = notify.get("webhook") or {}
    cfg.notify = NotifyConfig(
        telegram=TelegramConfig(
            enabled=bool(tg.get("enabled", False)),
            bot_token_env=str(tg.get("bot_token_env", DEFAULTS["notify"]["telegram"]["bot_token_env"])),
            chat_id=str(tg.get("chat_id", "") or ""),
        ),
        webhook=WebhookConfig(
            enabled=bool(hook.get("enabled", False)),
            url=str(hook.get("url", "") or ""),
            bearer_token_env=str(hook.get("bearer_token_env", DEFAULTS["notify"]["webhook"]["bearer_token_env"])),
        ),
    )

    log = data.get("logging") or {}
    cfg.logging = LoggingConfig(
        level=str(log.get("level", "INFO")).upper(),
        file=_as_path(log.get("file")),
        console=bool(log.get("console", False)),
    )

    risk = data.get("risk") or {}
    cfg.risk = RiskWeights(
        keyword_cap=_as_float(risk.get("keyword_cap"), 40.0),
        age_cap=_as_float(risk.get("age_cap"), 18.0),
        volume_cap=_as_float(risk.get("volume_cap"), 18.0),
        issues_cap=_as_float(risk.get("issues_cap"), 30.0),
        context_cap=_as_float(risk.get("context_cap"), 12.0),
        bonus_cap=_as_float(risk.get("bonus_cap"), 15.0),
    )

    _warn_unknown_keys(data, _config_schema(), warnings, prefix="")
    return cfg


def _warn_unknown_keys(data: Mapping[str, Any], schema: Mapping[str, Any], warnings: list[str], prefix: str) -> None:
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if key not in schema:
            warnings.append(f"unknown config key: {dotted} (ignored)")
            continue
        if isinstance(value, Mapping) and isinstance(schema[key], Mapping):
            _warn_unknown_keys(value, schema[key], warnings, prefix=f"{dotted}.")


def _migrate_legacy_config(cfg: Config, data: Mapping[str, Any], warnings: list[str]) -> None:
    """Phase-2 keys keep working, but the user is told what replaced them.

    Doc section 27: never error on an old config - migrate it and say so.
    """
    hg = data.get("hard_gates") or {}
    notices: list[str] = []

    age_hours = cfg.hard_gates.minimum_release_age_hours
    if age_hours is not None:
        if age_hours > 0:
            cfg.release_age_policy.block_hours = min(float(age_hours), cfg.release_age_policy.caution_hours)
        notices.append(
            f"Deprecated: hard_gates.minimum_release_age_hours = {age_hours:g}\n"
            f"  Use: release_age_policy (block_hours={cfg.release_age_policy.block_hours:g}, "
            f"caution_hours={cfg.release_age_policy.caution_hours:g}, "
            f"acceptable_hours={cfg.release_age_policy.acceptable_hours:g})\n"
            "  A release is no longer blocked just for being young: under 6 h blocks, 6-12 h caps the verdict "
            "at ACCEPTABLE, and beyond that the age only feeds Change Risk."
        )
    legacy_switches = (
        ("block_active_database_regression", "block_on_systemic_risk", "systemic"),
        ("block_active_session_regression", "block_on_systemic_risk", "systemic"),
        ("block_active_update_failure", "block_on_systemic_risk", "systemic"),
        ("block_active_gateway_regression", "warn_active_gateway_regression", "warning"),
    )
    for old_key, new_key, _kind in legacy_switches:
        if old_key not in hg:
            continue
        notices.append(f"Deprecated: hard_gates.{old_key}\n  Use: hard_gates.{new_key}")
        if not bool(hg[old_key]):
            # an explicit "false" is honoured: that class stops contributing
            setattr(cfg.hard_gates, new_key, False)
    if bool(hg.get("block_main_branch_update", False)):
        notices.append(
            "Note: hard_gates.block_main_branch_update = true keeps the phase-2 behaviour "
            "(a main-branch checkout blocks the update). The phase-3 default is a warning."
        )
    if bool(hg.get("block_prerelease", False)):
        notices.append(
            "Note: hard_gates.block_prerelease = true keeps blocking prereleases; phase 3 only warns by default."
        )
    if cfg.minimum_release_age_days > 0 and "minimum_release_age_days" in data:
        notices.append(
            f"Deprecated: minimum_release_age_days = {cfg.minimum_release_age_days:g}\n"
            "  Use: release_age_policy - the value no longer delays a recommendation by days; it now only caps "
            "the verdict at ACCEPTABLE while the release is inside the window."
        )

    cfg.deprecated = notices
    for notice in notices:
        warnings.append(notice.splitlines()[0])


def _validate(cfg: Config, warnings: list[str]) -> None:
    if not 0 <= cfg.risk_threshold <= 100:
        warnings.append(f"risk_threshold {cfg.risk_threshold} out of range 0-100; clamped")
        cfg.risk_threshold = int(max(0, min(100, cfg.risk_threshold)))
    if cfg.minimum_release_age_days < 0:
        warnings.append("minimum_release_age_days < 0 makes no sense; using 0")
        cfg.minimum_release_age_days = 0.0
    if cfg.preferred_channel not in {"stable", "prerelease", "main"}:
        warnings.append(f"preferred_channel '{cfg.preferred_channel}' unknown; using 'stable'")
        cfg.preferred_channel = "stable"
    if cfg.language not in {"zh", "en"}:
        warnings.append(f"language '{cfg.language}' unknown; using 'zh'")
        cfg.language = "zh"
    if cfg.logging.level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        warnings.append(f"logging.level '{cfg.logging.level}' unknown; using INFO")
        cfg.logging.level = "INFO"
    if cfg.auto_update:
        warnings.append(
            "auto_update=true is set: `update` will no longer ask for confirmation. "
            "Set it back to false if that is not what you want."
        )
    if cfg.network.timeout_seconds <= 0:
        cfg.network.timeout_seconds = 15.0
    if cfg.network.retries < 0:
        cfg.network.retries = 0

    policy = cfg.release_age_policy
    if policy.block_hours < 0:
        warnings.append("release_age_policy.block_hours < 0 makes no sense; using 0")
        policy.block_hours = 0.0
    if policy.caution_hours < policy.block_hours:
        warnings.append("release_age_policy.caution_hours < block_hours; raising it to block_hours")
        policy.caution_hours = policy.block_hours
    if policy.acceptable_hours < policy.caution_hours:
        warnings.append("release_age_policy.acceptable_hours < caution_hours; raising it to caution_hours")
        policy.acceptable_hours = policy.caution_hours


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_path(value: Any) -> Path | None:
    if value in (None, "", "null"):
        return None
    return Path(str(value)).expanduser()


# --------------------------------------------------------------------------- #
# writing the example config
# --------------------------------------------------------------------------- #


def write_default_config(path: Path | None = None, *, force: bool = False) -> Path:
    """Write a commented example config (used by `config init`)."""
    target = Path(path).expanduser() if path else default_config_path()
    if target.exists() and not force:
        raise ConfigError(f"config already exists: {target}", hint="use --force to overwrite")
    example = Path(__file__).resolve().parents[2] / "config.example.yaml"
    if example.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        return target
    if not _YAML_AVAILABLE:
        raise ConfigError("PyYAML is required to write a YAML config", hint="pip install PyYAML")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(DEFAULTS, sort_keys=False, allow_unicode=True), encoding="utf-8")  # type: ignore[union-attr]
    return target
