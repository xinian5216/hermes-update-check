"""Usage profile: levels, weights, detection and cluster -> feature attribution."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_update_check.usage_profile import (
    DEFAULT_LEVEL_FOR_UNKNOWN_FEATURE,
    DEFAULT_LEVEL_FOR_UNKNOWN_PROVIDER,
    LEVEL_CRITICAL,
    LEVEL_IMPORTANT,
    LEVEL_OPTIONAL,
    LEVEL_UNUSED,
    LEVEL_WEIGHTS,
    PROVIDER_AGGREGATE_KEY,
    UsageProfile,
    affected_features,
    builtin_default_profile,
    detect_usage_profile,
    mentions_all_providers,
    provider_names_in,
    resolve_profile,
)


# --------------------------------------------------------------------------- #
# weights (doc section 6)
# --------------------------------------------------------------------------- #


def test_level_weights_match_the_spec() -> None:
    assert LEVEL_WEIGHTS[LEVEL_CRITICAL] == 1.0
    assert LEVEL_WEIGHTS[LEVEL_IMPORTANT] == 0.6
    assert LEVEL_WEIGHTS[LEVEL_OPTIONAL] == 0.25
    assert LEVEL_WEIGHTS[LEVEL_UNUSED] == 0.0


def test_an_unused_feature_has_no_weight_but_a_critical_one_has_all_of_it() -> None:
    profile = UsageProfile(features={"docker": LEVEL_UNUSED, "sessions": LEVEL_CRITICAL})
    assert profile.weight("docker") == 0.0
    assert profile.is_active("docker") is False
    assert profile.weight("sessions") == 1.0
    assert profile.critical_keys() == ["sessions"]


def test_unknown_features_are_treated_as_important_and_unknown_providers_as_unused() -> None:
    """Missing usage information is not evidence that a feature is unused - but the
    provider list is exhaustive by construction, so an unlisted provider is unused."""
    profile = UsageProfile(features={"sessions": LEVEL_UNUSED}, providers={"openai": LEVEL_CRITICAL})
    assert profile.level("a-brand-new-cluster") == DEFAULT_LEVEL_FOR_UNKNOWN_FEATURE
    assert profile.weight("a-brand-new-cluster") == 0.6
    assert profile.level("providers.ollama") == DEFAULT_LEVEL_FOR_UNKNOWN_PROVIDER
    assert profile.level("providers.openai") == LEVEL_CRITICAL


def test_the_aggregate_provider_key_takes_the_most_important_provider() -> None:
    profile = UsageProfile(providers={"openai": LEVEL_CRITICAL, "gemini": LEVEL_UNUSED})
    assert profile.level(PROVIDER_AGGREGATE_KEY) == LEVEL_CRITICAL
    assert profile.weight(PROVIDER_AGGREGATE_KEY) == 1.0


def test_a_group_level_default_covers_unlisted_providers() -> None:
    profile = UsageProfile(providers={PROVIDER_AGGREGATE_KEY: LEVEL_IMPORTANT})
    assert profile.level("providers.something-new") == LEVEL_IMPORTANT


# --------------------------------------------------------------------------- #
# parsing / serialisation
# --------------------------------------------------------------------------- #


def test_from_dict_validates_levels_and_warns_loudly() -> None:
    warnings: list[str] = []
    profile = UsageProfile.from_dict(
        {"features": {"sessions": "CRITICAL", "tools": "very"}, "providers": {"openai": "unused"}},
        warnings=warnings,
    )
    assert profile.features["tools"] == LEVEL_IMPORTANT  # degraded, not dropped
    assert profile.providers["openai"] == LEVEL_UNUSED
    assert any("tools" in warning for warning in warnings)


def test_from_dict_tolerates_garbage() -> None:
    assert UsageProfile.from_dict(None).features == {}
    assert UsageProfile.from_dict("nonsense").features == {}  # type: ignore[arg-type]
    warnings: list[str] = []
    UsageProfile.from_dict({"features": "not-a-mapping"}, warnings=warnings)
    assert warnings


def test_yaml_block_round_trips() -> None:
    profile = UsageProfile(features={"sessions": LEVEL_CRITICAL}, providers={"openai": LEVEL_IMPORTANT})
    block = profile.yaml_block()
    assert block.startswith("usage_profile:\n")
    assert "features:" in block and "sessions: critical" in block
    assert "providers:" in block and "openai: important" in block


def test_builtin_default_marks_the_usual_desktop_stack_critical() -> None:
    profile = builtin_default_profile(["openai"])
    assert profile.weight("sessions") == 1.0
    assert profile.weight("docker") == 0.0
    assert profile.weight("providers.openai") == 0.6
    assert profile.source == "builtin-default"
    assert profile.notes_zh  # tells the user to run `profile detect`


# --------------------------------------------------------------------------- #
# detection (doc section 3)
# --------------------------------------------------------------------------- #


def test_detection_claims_important_for_configured_and_unused_for_the_rest(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "state.db").write_bytes(b"")
    (home / "config.yaml").write_text(
        "model:\n  provider: openai\nproviders:\n  openai:\n    api_key_env: OPENAI_API_KEY\n"
        "browser:\n  cloud_provider: browserbase\n",
        encoding="utf-8",
    )
    detection = detect_usage_profile(hermes_home=home, env={})
    profile = detection.profile
    assert profile.features["cli_agent"] == LEVEL_IMPORTANT
    assert profile.features["sessions"] == LEVEL_IMPORTANT  # via state.db
    assert profile.features["browser_tools"] == LEVEL_IMPORTANT
    assert profile.features["mcp"] == LEVEL_UNUSED
    assert profile.features["docker"] == LEVEL_UNUSED
    assert profile.providers["openai"] == LEVEL_IMPORTANT
    # it never guesses critical, and it says so
    assert LEVEL_CRITICAL not in profile.features.values()
    assert any("critical" in note for note in detection.notes_en)


def test_detection_reads_env_names_but_never_values(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    home.mkdir()
    detection = detect_usage_profile(
        hermes_home=home,
        config_data={},
        env={"TELEGRAM_BOT_TOKEN": "secret-value", "OLLAMA_HOST": "http://localhost:11434"},
    )
    assert detection.profile.features["telegram"] == LEVEL_IMPORTANT
    # provider entries are stored by bare name; lookups use the `providers.<name>` key
    assert detection.profile.providers["ollama"] == LEVEL_IMPORTANT
    assert detection.profile.level("providers.ollama") == LEVEL_IMPORTANT
    # only the variable *name* shows up as evidence
    assert "TELEGRAM_BOT_TOKEN" in detection.evidence["telegram"][0]
    assert "secret-value" not in str(detection.evidence)


def test_detection_survives_a_broken_config(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: [this is: not valid yaml\n  - at all\n", encoding="utf-8")
    detection = detect_usage_profile(hermes_home=home, env={})
    # falls back to a key-only scan instead of raising
    assert detection.profile.features["cli_agent"] in {LEVEL_IMPORTANT, LEVEL_UNUSED}


def test_docker_needs_an_explicit_backend(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    home.mkdir()
    # container_* keys exist in configs that never run containers
    detection = detect_usage_profile(
        hermes_home=home, config_data={"terminal": {"container_cpu": 2, "container_memory": "2g"}}, env={}
    )
    assert detection.profile.features["docker"] == LEVEL_UNUSED

    detection = detect_usage_profile(hermes_home=home, config_data={"terminal": {"backend": "docker"}}, env={})
    assert detection.profile.features["docker"] == LEVEL_IMPORTANT


def test_resolve_profile_prefers_config_then_detection(tmp_path: Path) -> None:
    class FakeCfg:
        usage_profile = UsageProfile(features={"sessions": LEVEL_CRITICAL}, source="config")

    configured = resolve_profile(FakeCfg(), hermes_home=tmp_path)
    assert configured.level("sessions") == LEVEL_CRITICAL
    assert configured.source == "config"

    class EmptyCfg:
        usage_profile = UsageProfile()

    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    detected = resolve_profile(EmptyCfg(), hermes_home=home, env={})
    assert detected.source == "detected"
    assert detected.features["cli_agent"] == LEVEL_IMPORTANT


# --------------------------------------------------------------------------- #
# cluster -> feature attribution (doc sections 14-16)
# --------------------------------------------------------------------------- #


def test_provider_issues_are_split_per_provider() -> None:
    assert affected_features("PROVIDER", "Gemini OAuth is broken") == ["providers.gemini"]
    assert affected_features("PROVIDER", "OpenAI returns 401 for every key") == ["providers.openai"]
    assert affected_features("PROVIDER", "OpenRouter and Gemini both fail") == [
        "providers.openrouter",
        "providers.gemini",
    ]


def test_a_provider_system_failure_is_not_attributed_to_one_provider() -> None:
    text = "all providers are down, the provider system is completely broken"
    assert mentions_all_providers(text) is True
    assert affected_features("PROVIDER", text) == [PROVIDER_AGGREGATE_KEY]


def test_an_unidentifiable_provider_issue_uses_the_aggregate_key() -> None:
    assert affected_features("PROVIDER", "provider layer returns garbage") == [PROVIDER_AGGREGATE_KEY]


def test_provider_names_are_read_from_headlines_only_when_asked() -> None:
    assert provider_names_in("Gemini flash cannot read images") == ["gemini"]
    assert provider_names_in("config snippet: base_url = https://openrouter.ai/api") == ["openrouter"]


def test_gateway_and_session_issues_map_to_their_features() -> None:
    assert affected_features("GATEWAY", "gateway deadlock on restart") == ["gateway"]
    assert affected_features("GATEWAY", "telegram relay dies with the gateway") == ["gateway", "telegram"]
    assert affected_features("SESSION", "sessions are gone") == ["sessions"]
    assert affected_features("MCP", "mcp server fails to load") == ["mcp"]


def test_a_generic_crash_is_attributed_to_the_subsystem_it_names() -> None:
    """The real-data bug: a kanban/cron worker bug is not "the CLI is broken"."""
    assert affected_features("CRASH", "kanban worker crashes on dispatch") == ["tools"]
    assert affected_features("CRASH", "the gateway process crashes") == ["gateway"]
    assert affected_features("CRASH", "hermes crashes at launch") == ["cli_agent"]
