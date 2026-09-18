"""Which parts of Hermes this user actually depends on.

Phase 3 makes the verdict *personal*: a regression only matters if it hits a
feature the user relies on. This module owns that mapping.

- :data:`LEVEL_WEIGHTS` is the whole idea in one line: a bug in an ``unused``
  feature weighs nothing, a bug in a ``critical`` one weighs everything.
- :class:`UsageProfile` is the user's declaration (``usage_profile`` in the config).
- :func:`detect_usage_profile` proposes one by reading the local Hermes install, and
  deliberately claims only ``important`` for anything it can prove is configured -
  it never guesses what is *critical* to the user.
- :func:`affected_features` maps a regression cluster (plus its text) onto feature
  keys, so an "Gemini OAuth broken" issue lands on ``providers.gemini`` and not on
  every provider at once.

Everything here is a pure function of files and environment *names* (never values),
so it stays offline and safe to run anywhere.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

LEVEL_CRITICAL = "critical"
LEVEL_IMPORTANT = "important"
LEVEL_OPTIONAL = "optional"
LEVEL_UNUSED = "unused"

LEVELS: tuple[str, ...] = (LEVEL_CRITICAL, LEVEL_IMPORTANT, LEVEL_OPTIONAL, LEVEL_UNUSED)

#: How much a regression in a feature counts against *this* user.
LEVEL_WEIGHTS: dict[str, float] = {
    LEVEL_CRITICAL: 1.0,
    LEVEL_IMPORTANT: 0.6,
    LEVEL_OPTIONAL: 0.25,
    LEVEL_UNUSED: 0.0,
}

LEVEL_LABELS_ZH = {
    LEVEL_CRITICAL: "关键",
    LEVEL_IMPORTANT: "重要",
    LEVEL_OPTIONAL: "可选",
    LEVEL_UNUSED: "未使用",
}
LEVEL_LABELS_EN = {
    LEVEL_CRITICAL: "critical",
    LEVEL_IMPORTANT: "important",
    LEVEL_OPTIONAL: "optional",
    LEVEL_UNUSED: "unused",
}

#: An unknown *feature* key is treated as important: not knowing whether someone
#: relies on a feature is not evidence that they do not ("missing information is
#: never safe"). Unknown *providers* default to unused instead - the provider list
#: in a profile is exhaustive by construction (detection fills in every configured
#: one), so an unlisted provider really is one the user does not run.
DEFAULT_LEVEL_FOR_UNKNOWN_FEATURE = LEVEL_IMPORTANT
DEFAULT_LEVEL_FOR_UNKNOWN_PROVIDER = LEVEL_UNUSED

#: Aggregate key used when an issue cannot be attributed to one provider.
PROVIDER_AGGREGATE_KEY = "providers"


@dataclass(frozen=True)
class FeatureSpec:
    """One row of the built-in feature catalogue."""

    key: str
    zh: str
    en: str
    #: config key paths (lower-case segments) that prove the feature is configured
    config_keys: tuple[str, ...] = ()
    #: ``(config key path, accepted values)`` - for features that are on only when a
    #: setting has a particular value (a Docker backend, a non-empty provider list)
    config_values: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: entries inside HERMES_HOME that prove the feature is in use
    home_entries: tuple[str, ...] = ()
    #: environment variable *names* (presence only - values are never read)
    env_names: tuple[str, ...] = ()


FEATURE_SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        key="cli_agent",
        zh="CLI / Agent",
        en="CLI agent",
        config_keys=("model", "agent", "moa", "delegation", "fallback_providers"),
    ),
    FeatureSpec(
        key="desktop",
        zh="桌面端",
        en="Desktop app",
        config_keys=("desktop", "display", "streaming", "pets"),
        home_entries=("desktop", "desktop-plugins"),
    ),
    FeatureSpec(
        key="sessions",
        zh="Session / 会话数据",
        en="Sessions",
        config_keys=("database", "compression", "memory", "group_sessions_per_user"),
        home_entries=("state.db", "sessions", "shared-state.db", "projects.db"),
    ),
    FeatureSpec(
        key="tools",
        zh="工具系统",
        en="Tool system",
        config_keys=("code_execution", "terminal", "tool_loop_guardrails", "skills", "platform_toolsets", "plugins"),
    ),
    FeatureSpec(
        key="mcp",
        zh="MCP",
        en="MCP",
        config_keys=("mcp", "mcp_servers", "mcpServers"),
        home_entries=("mcp.json", "mcp", "mcp_servers"),
    ),
    FeatureSpec(
        key="gateway",
        zh="Gateway",
        en="Gateway",
        config_keys=("gateway", "channels"),
        # `bot_relay/` exists on every install (desktop bot mode writes it), so it is
        # not evidence of a gateway; only configuration is.
        home_entries=("gateway.json",),
    ),
    FeatureSpec(
        key="telegram",
        zh="Telegram",
        en="Telegram",
        config_keys=("telegram",),
        env_names=("TELEGRAM_BOT_TOKEN", "HERMES_TELEGRAM_TOKEN", "HERMES_UPDATE_CHECK_TELEGRAM_TOKEN"),
    ),
    FeatureSpec(
        key="web_tools",
        zh="Web / 搜索工具",
        en="Web tools",
        config_keys=("web", "search", "web_tools", "browser_use"),
    ),
    FeatureSpec(
        key="browser_tools",
        zh="浏览器工具",
        en="Browser tools",
        config_keys=("browser",),
        home_entries=("browser_profiles",),
    ),
    FeatureSpec(
        key="docker",
        zh="Docker",
        en="Docker",
        # only an explicit Docker backend counts - `terminal.container_*` keys exist
        # in configs that never run Docker, so they are not evidence on their own
        config_values=(("terminal.backend", ("docker", "container")),),
        env_names=("DOCKER_HOST",),
    ),
)

FEATURE_BY_KEY = {spec.key: spec for spec in FEATURE_SPECS}

#: Known provider names and the words an issue uses to name them.
PROVIDER_PATTERNS: dict[str, tuple[str, ...]] = {
    "openai": (r"\bopenai\b", r"\bgpt-?[45]\b", r"\bcodex\b"),
    "anthropic": (r"\banthropic\b", r"\bclaude\b"),
    "openrouter": (r"\bopenrouter\b",),
    "gemini": (r"\bgemini\b", r"\bgoogle (?:ai|generative)\b", r"\bvertex\b"),
    "ollama": (r"\bollama\b",),
    "deepseek": (r"\bdeepseek\b",),
    "moonshot": (r"\bmoonshot\b", r"\bkimi\b", r"\bk3\b"),
    "xai": (r"\bxai\b", r"\bgrok\b"),
    "mistral": (r"\bmistral\b",),
    "groq": (r"\bgroq\b",),
    "together": (r"\btogether\b",),
    "bedrock": (r"\bbedrock\b", r"\baws\b"),
    "azure": (r"\bazure\b",),
    "ark": (r"\bark\b", r"\bvolc\b", r"\bbytedance\b"),
}
_PROVIDER_COMPILED = {
    name: [re.compile(p, re.IGNORECASE) for p in patterns] for name, patterns in PROVIDER_PATTERNS.items()
}

#: Generic provider words that mean "the provider layer", not one provider.
_ALL_PROVIDER_RE = re.compile(
    r"\ball providers\b|\bevery provider\b|\bprovider system\b|\bcompletely broken\b", re.IGNORECASE
)

_ENV_PROVIDER_HINTS: tuple[tuple[str, str], ...] = (
    ("OPENAI_API_KEY", "openai"),
    ("ANTHROPIC_API_KEY", "anthropic"),
    ("OPENROUTER_API_KEY", "openrouter"),
    ("GEMINI_API_KEY", "gemini"),
    ("GOOGLE_API_KEY", "gemini"),
    ("DEEPSEEK_API_KEY", "deepseek"),
    ("MOONSHOT_API_KEY", "moonshot"),
    ("XAI_API_KEY", "xai"),
    ("MISTRAL_API_KEY", "mistral"),
    ("GROQ_API_KEY", "groq"),
    ("TOGETHER_API_KEY", "together"),
    ("AZURE_OPENAI_API_KEY", "azure"),
    ("OLLAMA_HOST", "ollama"),
)

#: Cluster key -> the features an issue in that cluster can affect.
_CLUSTER_FEATURES: dict[str, tuple[str, ...]] = {
    "DATABASE": ("sessions",),
    "SESSION": ("sessions",),
    "GATEWAY": ("gateway",),
    "MCP": ("mcp",),
    "CRASH": ("cli_agent", "desktop"),
    "CONFIG_MIGRATION": ("config",),
    "UPDATE_FAILURE": ("cli_agent",),
}


#: Words that identify which subsystem a generic crash belongs to.
_SUBSYSTEM_WORDS: tuple[tuple[str, str], ...] = (
    ("gateway", r"\bgateways?\b"),
    ("telegram", r"\b(?:telegram|weixin|discord|slack|signal|bot relay|bot mode)\b"),
    ("sessions", r"\b(?:sessions?|transcripts?|resume|handoff)\b"),
    ("mcp", r"\bmcp\b"),
    ("tools", r"\b(?:tools?|kanban|cron|cronjob|worker|skills?|plugins?|dashboard|scheduler|delegation)\b"),
    ("browser_tools", r"\b(?:browser|playwright|cdp)\b"),
    ("web_tools", r"\b(?:web search|search tool|web fetch)\b"),
)


def provider_names_in(text: str) -> list[str]:
    """Which providers an issue text names (never 'all providers')."""
    if not text:
        return []
    found: list[str] = []
    for name, patterns in _PROVIDER_COMPILED.items():
        if any(pattern.search(text) for pattern in patterns):
            found.append(name)
    return found


def mentions_all_providers(text: str) -> bool:
    return bool(text) and bool(_ALL_PROVIDER_RE.search(text))


def affected_features(cluster_key: str, text: str = "", *, titles: str = "") -> list[str]:
    """Map one regression cluster (with its evidence text) onto feature keys.

    Provider issues are split per provider *by their headline* - "Gemini OAuth
    broken" must not be counted against OpenAI, and a provider mentioned somewhere
    inside a config dump is not evidence. A generic crash is attributed to the
    subsystem the text names (a cron-worker bug is not "the CLI is broken").
    """
    key = (cluster_key or "").upper()

    if key in {"PROVIDER", "AUTH"}:
        named = provider_names_in(titles or text)
        if named and not mentions_all_providers(titles or text):
            return [f"providers.{name}" for name in named]
        return [PROVIDER_AGGREGATE_KEY]

    if key == "CRASH":
        found = [feature for feature, pattern in _SUBSYSTEM_WORDS if re.search(pattern, text, re.IGNORECASE)]
        return found or ["cli_agent"]

    features: list[str] = list(_CLUSTER_FEATURES.get(key, ("cli_agent",)))
    if key == "GATEWAY" and text and re.search(r"\btelegram\b", text, re.IGNORECASE):
        features.append("telegram")
    return features


@dataclass
class UsageProfile:
    """The user's declaration of what matters (``usage_profile`` in the config)."""

    features: dict[str, str] = field(default_factory=dict)
    providers: dict[str, str] = field(default_factory=dict)
    source: str = "default"
    auto_generated: bool = False
    notes_zh: list[str] = field(default_factory=list)
    notes_en: list[str] = field(default_factory=list)

    # -- lookup ------------------------------------------------------------- #
    def level(self, key: str) -> str:
        """Level for a feature key, with explicit fallbacks (see module docstring)."""
        if key in self.features:
            return self.features[key]
        if key.startswith("providers."):
            name = key.split(".", 1)[1]
            if name in self.providers:
                return self.providers[name]
            group = self.providers.get(PROVIDER_AGGREGATE_KEY)
            return group if group is not None else DEFAULT_LEVEL_FOR_UNKNOWN_PROVIDER
        if key == PROVIDER_AGGREGATE_KEY:
            return self._provider_group_level()
        return DEFAULT_LEVEL_FOR_UNKNOWN_FEATURE

    def weight(self, key: str) -> float:
        return LEVEL_WEIGHTS.get(self.level(key), LEVEL_WEIGHTS[LEVEL_IMPORTANT])

    def is_active(self, key: str) -> bool:
        return self.weight(key) > 0.0

    def _provider_group_level(self) -> str:
        levels = [level for name, level in self.providers.items() if name != PROVIDER_AGGREGATE_KEY]
        if not levels:
            group = self.providers.get(PROVIDER_AGGREGATE_KEY)
            return group if group is not None else DEFAULT_LEVEL_FOR_UNKNOWN_PROVIDER
        return max(levels, key=lambda level: LEVEL_WEIGHTS.get(level, 0.0))

    def critical_keys(self) -> list[str]:
        return sorted(
            [key for key, level in self.features.items() if level == LEVEL_CRITICAL]
            + [
                f"providers.{name}"
                for name, level in self.providers.items()
                if level == LEVEL_CRITICAL and name != PROVIDER_AGGREGATE_KEY
            ]
        )

    def active_keys(self) -> list[str]:
        return sorted(
            [key for key, level in self.features.items() if LEVEL_WEIGHTS.get(level, 0.0) > 0]
            + [
                f"providers.{name}"
                for name, level in self.providers.items()
                if LEVEL_WEIGHTS.get(level, 0.0) > 0 and name != PROVIDER_AGGREGATE_KEY
            ]
        )

    def label(self, key: str, *, lang: str = "zh") -> str:
        if key.startswith("providers."):
            name = key.split(".", 1)[1]
            return f"Provider · {name}" if lang == "zh" else f"Provider {name}"
        if key == PROVIDER_AGGREGATE_KEY:
            return "Provider（未细分）" if lang == "zh" else "Providers (unspecified)"
        spec = FEATURE_BY_KEY.get(key)
        if spec is None:
            return key
        return spec.zh if lang == "zh" else spec.en

    # -- serialisation ------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "features": dict(sorted(self.features.items())),
            "providers": dict(sorted(self.providers.items())),
            "source": self.source,
            "auto_generated": self.auto_generated,
            "notes_zh": self.notes_zh,
            "notes_en": self.notes_en,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, *, warnings: Optional[list[str]] = None) -> UsageProfile:
        """Build a profile from the config section; unknown levels degrade loudly."""
        warn = warnings if warnings is not None else []
        if not isinstance(data, Mapping):
            return cls(source="default")
        profile = cls(source="config")
        for section, target in (("features", profile.features), ("providers", profile.providers)):
            raw = data.get(section)
            if raw is None:
                continue
            if not isinstance(raw, Mapping):
                warn.append(f"usage_profile.{section} must be a mapping of name: level; ignored")
                continue
            for name, level in raw.items():
                key = str(name).strip()
                if not key:
                    continue
                value = str(level).strip().lower()
                if value not in LEVEL_WEIGHTS:
                    warn.append(
                        f"usage_profile.{section}.{key} = '{level}' is not one of {', '.join(LEVELS)}; "
                        f"treated as {LEVEL_IMPORTANT}"
                    )
                    value = LEVEL_IMPORTANT
                target[key] = value
        return profile

    def yaml_block(self, *, indent: str = "  ") -> str:
        """The snippet a user pastes into (or edits in) their config file."""
        lines = ["usage_profile:", f"{indent}features:"]
        for key in sorted(self.features):
            lines.append(f"{indent * 2}{key}: {self.features[key]}")
        lines.append(f"{indent}providers:")
        for name in sorted(self.providers):
            lines.append(f"{indent * 2}{name}: {self.providers[name]}")
        return "\n".join(lines) + "\n"

    def describe_lines(self, *, lang: str = "zh") -> list[str]:
        def render(pairs: Iterable[tuple[str, str]]) -> list[str]:
            return [f"  {self.label(key, lang=lang):<28} {level}" for key, level in pairs]

        lines = ["Features:" if lang == "en" else "功能："]
        lines.extend(render(sorted(self.features.items())))
        lines.append("Providers:" if lang == "en" else "Provider：")
        lines.extend(render(sorted(self.providers.items())))
        return lines


def builtin_default_profile(providers: Sequence[str] = ()) -> UsageProfile:
    """The conservative default used when no ``usage_profile`` is configured.

    It mirrors what a typical desktop + CLI user relies on, and marks every provider
    *detected on this machine* as important. Anything it cannot see stays out of the
    way (docker/web/browser), which is the same promise the explicit profile makes.
    """
    profile = UsageProfile(
        features={
            "cli_agent": LEVEL_CRITICAL,
            "desktop": LEVEL_CRITICAL,
            "sessions": LEVEL_CRITICAL,
            "tools": LEVEL_CRITICAL,
            "mcp": LEVEL_IMPORTANT,
            "gateway": LEVEL_IMPORTANT,
            "telegram": LEVEL_IMPORTANT,
            "web_tools": LEVEL_IMPORTANT,
            "browser_tools": LEVEL_OPTIONAL,
            "docker": LEVEL_UNUSED,
        },
        providers=dict.fromkeys(providers, LEVEL_IMPORTANT),
        source="builtin-default",
        notes_zh=[
            "未配置 usage_profile：正在使用内置默认画像（不影响核心功能，但请运行 `profile detect` 按实际使用调整）"
        ],
        notes_en=[
            "no usage_profile configured: using the built-in default profile "
            "(run `profile detect` to match your actual setup)"
        ],
    )
    if not providers:
        profile.providers[PROVIDER_AGGREGATE_KEY] = LEVEL_IMPORTANT
    return profile


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #


@dataclass
class UsageDetection:
    """Detection result: the proposed profile plus why each row was proposed."""

    profile: UsageProfile
    evidence: dict[str, list[str]] = field(default_factory=dict)
    notes_zh: list[str] = field(default_factory=list)
    notes_en: list[str] = field(default_factory=list)


def _config_key_paths(data: Any, prefix: str = "", depth: int = 0) -> list[str]:
    """Lower-case key paths of a config mapping (values are never inspected here)."""
    paths: list[str] = []
    if isinstance(data, Mapping) and depth <= 3:
        for key, value in data.items():
            path = f"{prefix}{str(key).lower()}"
            paths.append(path)
            paths.extend(_config_key_paths(value, path + ".", depth + 1))
    return paths


def _config_value(data: Any, path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


def _read_hermes_config(path: Path) -> dict[str, Any]:
    """Read the Hermes config for *structure* only (values are never used as secrets).

    Falls back to a line-based key scan when YAML is unavailable, so detection still
    finds the sections it cares about on a minimal install.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        import yaml

        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {match.lower(): {} for match in re.findall(r"(?m)^\s*([A-Za-z_][\w-]*)\s*:", text)}


def detect_usage_profile(
    *,
    hermes_home: Path,
    config_data: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    install_kind: str = "unknown",
    extra_providers: Sequence[str] = (),
) -> UsageDetection:
    """Propose a profile from what is provably configured on this machine.

    Detection claims ``important`` for what it can see and ``unused`` for what it
    cannot - it never claims ``critical``. The user makes that call; the notes say so.
    """
    env_map = env if env is not None else os.environ
    data = dict(config_data) if config_data is not None else _read_hermes_config(hermes_home / "config.yaml")
    key_paths = _config_key_paths(data)

    detection = UsageDetection(
        profile=UsageProfile(source="detected", auto_generated=True),
        notes_zh=[
            "自动检测只区分「已配置 (important)」与「未配置 (unused)」——它不会替你判断什么是关键功能",
            "请用 `profile edit` 把真正关键的功能改成 critical（例如你每天用的 CLI、Session、常用 Provider）",
        ],
        notes_en=[
            "detection only tells configured (important) from not-configured (unused); "
            "it never guesses what is critical to you",
            "run `profile edit` and promote what you really depend on to critical "
            "(your daily CLI, sessions, the provider you actually use)",
        ],
    )

    for spec in FEATURE_SPECS:
        evidence: list[str] = []
        for needle in spec.config_keys:
            hit = next((path for path in key_paths if path == needle or path.startswith(needle + ".")), None)
            if hit:
                evidence.append(f"config:{hit}")
                break
        for path, accepted in spec.config_values:
            value = _config_value(data, path)
            if isinstance(value, str) and value.strip().lower() in {a.lower() for a in accepted}:
                evidence.append(f"config:{path}={value.strip().lower()}")
                break
        for entry in spec.home_entries:
            if (hermes_home / entry).exists():
                evidence.append(f"home:{entry}")
                break
        for name in spec.env_names:
            if env_map.get(name):
                evidence.append(f"env:{name}")
                break
        if spec.key == "docker" and install_kind == "docker":
            evidence.append("install:docker")

        level = LEVEL_IMPORTANT if evidence else LEVEL_UNUSED
        detection.profile.features[spec.key] = level
        if evidence:
            detection.evidence[spec.key] = evidence

    providers: dict[str, list[str]] = {}
    raw_providers = data.get("providers")
    if isinstance(raw_providers, Mapping):
        for name in raw_providers:
            providers.setdefault(str(name).strip().lower(), []).append("config:providers")
    model = data.get("model")
    if isinstance(model, Mapping):
        active = model.get("provider")
        if isinstance(active, str) and active.strip():
            providers.setdefault(active.strip().lower(), []).append("config:model.provider")
    fallbacks = data.get("fallback_providers")
    if isinstance(fallbacks, Sequence) and not isinstance(fallbacks, (str, bytes)):
        for entry in fallbacks:
            name = entry if isinstance(entry, str) else (entry.get("provider") if isinstance(entry, Mapping) else None)
            if isinstance(name, str) and name.strip():
                providers.setdefault(name.strip().lower(), []).append("config:fallback_providers")
    for env_name, provider in _ENV_PROVIDER_HINTS:
        if env_map.get(env_name):
            providers.setdefault(provider, []).append(f"env:{env_name}")
    for name in extra_providers:
        if name:
            providers.setdefault(str(name).strip().lower(), []).append("detected")

    for name in sorted(providers):
        detection.profile.providers[name] = LEVEL_IMPORTANT
        detection.evidence[f"providers.{name}"] = providers[name]
    if providers:
        detection.profile.providers[PROVIDER_AGGREGATE_KEY] = LEVEL_IMPORTANT

    detection.notes_zh.append(
        f"检测到 {len(providers)} 个 Provider、"
        f"{sum(1 for spec in FEATURE_SPECS if detection.profile.features[spec.key] == LEVEL_IMPORTANT)} 个已配置功能"
    )
    detection.notes_en.append(
        f"detected {len(providers)} provider(s) and "
        f"{sum(1 for spec in FEATURE_SPECS if detection.profile.features[spec.key] == LEVEL_IMPORTANT)} configured feature(s)"
    )
    return detection


def resolve_profile(
    cfg: Any,
    *,
    hermes_home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    warnings: Optional[list[str]] = None,
) -> UsageProfile:
    """The profile to *use*: configured one, else detection, else built-in default.

    The middle step matters: a machine with no ``usage_profile`` still gets a profile
    that reflects what is actually installed, instead of a fixed guess.
    """
    configured = getattr(cfg, "usage_profile", None)
    if isinstance(configured, UsageProfile) and (configured.features or configured.providers):
        configured.source = "config"
        return configured

    home = hermes_home
    if home is not None:
        detection = detect_usage_profile(hermes_home=home, env=env)
        profile = detection.profile
        profile.notes_zh = list(detection.notes_zh)
        profile.notes_en = list(detection.notes_en)
        return profile

    return builtin_default_profile()
