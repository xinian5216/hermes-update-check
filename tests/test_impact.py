"""Personal Impact, Core Feature Readiness and Systemic Critical Risk.

These cover the acceptance scenarios from the phase-3 specification (sections 4-7,
12-13, 17-18): global risk does not decide on its own, an unused feature never
counts, a confirmed unusable critical workflow blocks, and system-wide risks are
profile-independent.
"""

from __future__ import annotations

import pytest

from hermes_update_check.clusters import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    RegressionCluster,
)
from hermes_update_check.impact import (
    FEATURE_FAIL,
    FEATURE_OK,
    FEATURE_UNUSED,
    FEATURE_WARN,
    READINESS_ACCEPTABLE_MIN,
    READINESS_SAFE_MIN,
    claims_unavailability,
    compute_personal_readiness,
    detect_systemic_risks,
    feature_claims_unavailable,
    local_rollback_risk,
)
from hermes_update_check.usage_profile import (
    LEVEL_CRITICAL,
    LEVEL_IMPORTANT,
    LEVEL_OPTIONAL,
    LEVEL_UNUSED,
    UsageProfile,
)


def cluster(
    key: str,
    severity: str = SEVERITY_HIGH,
    confidence: str = CONFIDENCE_HIGH,
    *,
    features: list[str] | None = None,
    text: str = "",
    reports: int = 2,
    reporters: int = 2,
    open_count: int = 2,
    maintainer: int = 0,
    reproduction: int = 0,
) -> RegressionCluster:
    return RegressionCluster(
        key=key,
        zh=key,
        en=key,
        severity=severity,
        confidence=confidence,
        reports=reports,
        unique_reporters=reporters,
        open_count=open_count,
        maintainer_confirmed=maintainer,
        with_reproduction=reproduction,
        affected_features=list(features or []),
        root_causes=reports,
        evidence_text=text,
    )


def profile(**levels) -> UsageProfile:
    return UsageProfile(features=dict(levels), source="config")


# --------------------------------------------------------------------------- #
# Personal impact (doc sections 4-6)
# --------------------------------------------------------------------------- #


def test_a_bug_in_an_unused_feature_has_zero_personal_impact() -> None:
    """Scenario 3: Gemini HIGH + gemini unused -> Personal Impact 0."""
    prof = UsageProfile(features={"sessions": LEVEL_CRITICAL}, providers={"gemini": LEVEL_UNUSED})
    readiness = compute_personal_readiness(
        prof, [cluster("PROVIDER", SEVERITY_HIGH, CONFIDENCE_HIGH, features=["providers.gemini"])]
    )
    assert readiness.impact == 0
    assert readiness.readiness == 100
    assert any("unused" in reason.lower() or "unused" in reason for reason in readiness.reasons_en)


def test_the_same_bug_on_a_critical_provider_is_personal() -> None:
    """Scenario 4: Gemini HIGH + gemini critical -> a real personal impact."""
    prof = UsageProfile(features={"sessions": LEVEL_CRITICAL}, providers={"gemini": LEVEL_CRITICAL})
    readiness = compute_personal_readiness(
        prof, [cluster("PROVIDER", SEVERITY_HIGH, CONFIDENCE_HIGH, features=["providers.gemini"])]
    )
    assert readiness.impact >= 60
    assert any(feature.key == "providers.gemini" and feature.weight == 1.0 for feature in readiness.features)


def test_impact_grows_with_the_usage_level() -> None:
    """Scenarios 17-20: unused -> nothing, optional -> small, important -> medium,
    critical -> large."""
    values = {}
    for level in (LEVEL_UNUSED, LEVEL_OPTIONAL, LEVEL_IMPORTANT, LEVEL_CRITICAL):
        readiness = compute_personal_readiness(
            profile(sessions=level), [cluster("SESSION", SEVERITY_HIGH, CONFIDENCE_HIGH, features=["sessions"])]
        )
        values[level] = readiness.impact
    assert values[LEVEL_UNUSED] == 0
    assert 0 < values[LEVEL_OPTIONAL] < values[LEVEL_IMPORTANT] < values[LEVEL_CRITICAL]


def test_readiness_barely_moves_for_an_optional_feature_but_collapses_for_a_critical_one() -> None:
    optional = compute_personal_readiness(
        profile(browser_tools=LEVEL_OPTIONAL),
        [cluster("CRASH", SEVERITY_HIGH, CONFIDENCE_HIGH, features=["browser_tools"], text="browser is unusable")],
    )
    important = compute_personal_readiness(
        profile(browser_tools=LEVEL_IMPORTANT),
        [cluster("CRASH", SEVERITY_HIGH, CONFIDENCE_HIGH, features=["browser_tools"], text="browser is unusable")],
    )
    critical = compute_personal_readiness(
        profile(sessions=LEVEL_CRITICAL),
        [cluster("SESSION", SEVERITY_HIGH, CONFIDENCE_HIGH, features=["sessions"], text="session data is unrecoverable")],
    )
    # a small dent for an optional feature, a bigger one for an important feature...
    assert optional.readiness >= 80
    assert optional.readiness > important.readiness
    # ...and a collapse when a critical one is confirmed unavailable
    assert critical.readiness < READINESS_ACCEPTABLE_MIN


# --------------------------------------------------------------------------- #
# availability wording
# --------------------------------------------------------------------------- #


def test_a_bug_is_not_an_unavailability_claim() -> None:
    text = "the gateway drops one message per hour in a rare edge case"
    assert feature_claims_unavailable("gateway", text) is False
    assert claims_unavailability(text) is False


def test_unavailability_is_claimed_by_clause_shaped_text() -> None:
    assert feature_claims_unavailable("gateway", "the gateway cannot start after the update") is True
    assert feature_claims_unavailable("sessions", "session data is unrecoverable") is True
    assert claims_unavailability("hermes is unusable now") is True


def test_prevention_wording_is_not_a_failure_report() -> None:
    """The real-data false positive: "halts to prevent SQLite database corruption".

    Neither the availability check nor the systemic detector may read a safety guard
    as an incident report.
    """
    text = "the process throws a FATAL error and halts execution to prevent SQLite database corruption"
    assert claims_unavailability(text) is False
    assert feature_claims_unavailable("sessions", text) is False
    risks = detect_systemic_risks([cluster("DATABASE", SEVERITY_CRITICAL, CONFIDENCE_HIGH, text=text)])
    assert next(risk for risk in risks if risk.key == "DATA_CORRUPTION").detected is False


# --------------------------------------------------------------------------- #
# systemic critical risk (doc section 13)
# --------------------------------------------------------------------------- #


def test_data_corruption_is_detected_and_blocks_when_corroborated() -> None:
    clusters = [
        cluster(
            "DATABASE",
            SEVERITY_CRITICAL,
            CONFIDENCE_HIGH,
            text="state.db is corrupt after the update, data loss",
            reports=3,
            reporters=3,
            maintainer=1,
        )
    ]
    risks = detect_systemic_risks(clusters)
    corruption = next(risk for risk in risks if risk.key == "DATA_CORRUPTION")
    assert corruption.detected and corruption.blocking


def test_a_single_uncorroborated_report_watches_but_does_not_block() -> None:
    clusters = [
        cluster(
            "DATABASE",
            SEVERITY_CRITICAL,
            CONFIDENCE_MEDIUM,
            text="state.db is corrupt",
            reports=1,
            reporters=1,
            open_count=1,
        )
    ]
    corruption = next(risk for risk in detect_systemic_risks(clusters) if risk.key == "DATA_CORRUPTION")
    assert corruption.detected is True
    assert corruption.blocking is False


def test_session_loss_credential_loss_and_startup_failure_are_detected() -> None:
    clusters = [
        cluster("SESSION", SEVERITY_CRITICAL, CONFIDENCE_HIGH, text="all sessions were wiped by the upgrade"),
        cluster("AUTH", SEVERITY_HIGH, CONFIDENCE_HIGH, text="credentials were wiped from auth.json"),
        cluster("CRASH", SEVERITY_HIGH, CONFIDENCE_HIGH, text="hermes fails to start after updating"),
    ]
    keys = {risk.key for risk in detect_systemic_risks(clusters) if risk.detected}
    assert {"SESSION_LOSS", "CREDENTIAL_LOSS", "STARTUP_FAILURE"} <= keys


def test_install_corruption_and_rollback_failure_are_detected() -> None:
    clusters = [
        cluster(
            "UPDATE_FAILURE",
            SEVERITY_HIGH,
            CONFIDENCE_HIGH,
            text="the update leaves the installation unusable and the rollback failed",
        )
    ]
    keys = {risk.key for risk in detect_systemic_risks(clusters) if risk.detected}
    assert "INSTALL_CORRUPTION" in keys or "ROLLBACK_FAILURE" in keys


def test_all_providers_down_is_critical_but_one_provider_is_not() -> None:
    all_down = cluster("PROVIDER", SEVERITY_CRITICAL, CONFIDENCE_HIGH, text="all providers are down")
    one = cluster("PROVIDER", SEVERITY_HIGH, CONFIDENCE_HIGH, text="Gemini is completely down")
    assert next(r for r in detect_systemic_risks([all_down]) if r.key == "ALL_PROVIDERS_DOWN").detected
    assert not next(r for r in detect_systemic_risks([one]) if r.key == "ALL_PROVIDERS_DOWN").detected


def test_a_failing_local_rollback_check_is_a_systemic_risk() -> None:
    risk = local_rollback_risk("FAIL", detail_zh="状态目录不可写", detail_en="state dir not writable")
    assert risk is not None
    assert risk.blocking is True and risk.source == "local"
    assert local_rollback_risk("PASS") is None


def test_systemic_risks_are_independent_of_the_profile() -> None:
    """Doc section 13: data corruption blocks even when personal impact is tiny."""
    prof = UsageProfile(features={"sessions": LEVEL_UNUSED}, source="config")
    readiness = compute_personal_readiness(
        prof,
        [
            cluster(
                "DATABASE",
                SEVERITY_CRITICAL,
                CONFIDENCE_HIGH,
                features=["sessions"],
                text="state.db corrupt, data loss",
                reports=3,
                reporters=3,
                maintainer=1,
            )
        ],
    )
    assert readiness.impact == 0  # the user marked sessions unused...
    assert readiness.blocking_systemic  # ...and it still blocks


# --------------------------------------------------------------------------- #
# readiness bands and the bundle
# --------------------------------------------------------------------------- #


def test_a_clean_release_is_perfect() -> None:
    readiness = compute_personal_readiness(profile(sessions=LEVEL_CRITICAL), [])
    assert readiness.impact == 0 and readiness.readiness == 100
    assert readiness.critical_broken == []


def test_only_used_features_appear_with_issues() -> None:
    prof = UsageProfile(features={"sessions": LEVEL_CRITICAL, "docker": LEVEL_UNUSED}, source="config")
    readiness = compute_personal_readiness(
        prof, [cluster("GATEWAY", SEVERITY_HIGH, CONFIDENCE_HIGH, features=["docker"], text="docker is unusable")]
    )
    assert [f.key for f in readiness.unused_with_issues] == ["docker"]
    row = next(f for f in readiness.features if f.key == "docker")
    assert row.status == FEATURE_UNUSED and row.personal == 0.0


def test_feature_rows_report_ok_warn_and_fail() -> None:
    prof = UsageProfile(
        features={"sessions": LEVEL_CRITICAL, "gateway": LEVEL_IMPORTANT, "tools": LEVEL_IMPORTANT},
        source="config",
    )
    readiness = compute_personal_readiness(
        prof,
        [
            cluster("SESSION", SEVERITY_HIGH, CONFIDENCE_HIGH, features=["sessions"], text="sessions are unusable"),
            cluster("GATEWAY", SEVERITY_MEDIUM, CONFIDENCE_MEDIUM, features=["gateway"], text="an edge case fails"),
        ],
    )
    rows = {feature.key: feature for feature in readiness.features}
    assert rows["sessions"].status == FEATURE_FAIL and rows["sessions"].unavailable is True
    assert rows["gateway"].status == FEATURE_WARN and rows["gateway"].unavailable is False
    assert rows["tools"].status == FEATURE_OK


def test_readiness_and_impact_are_clamped_and_json_safe() -> None:
    readiness = compute_personal_readiness(
        profile(sessions=LEVEL_CRITICAL),
        [
            cluster("SESSION", SEVERITY_CRITICAL, CONFIDENCE_HIGH, features=["sessions"], text="sessions are unusable"),
            cluster("CRASH", SEVERITY_CRITICAL, CONFIDENCE_HIGH, features=["sessions"], text="hermes cannot start"),
        ],
    )
    assert 0 <= readiness.readiness <= 100
    assert 0 <= readiness.impact <= 100
    payload = readiness.to_dict()
    assert payload["features"] and payload["systemic"] is not None
    assert isinstance(payload["profile"]["features"], dict)


def test_low_confidence_evidence_moves_less_than_high_confidence() -> None:
    low = compute_personal_readiness(
        profile(sessions=LEVEL_CRITICAL),
        [cluster("SESSION", SEVERITY_HIGH, CONFIDENCE_LOW, features=["sessions"], text="sessions are unusable")],
    )
    high = compute_personal_readiness(
        profile(sessions=LEVEL_CRITICAL),
        [cluster("SESSION", SEVERITY_HIGH, CONFIDENCE_HIGH, features=["sessions"], text="sessions are unusable")],
    )
    assert low.impact < high.impact


def test_cluster_correlation_is_visible_in_the_numbers() -> None:
    """A cluster billed at 0.4 (secondary) must weigh less than the primary one."""
    primary = cluster("SESSION", SEVERITY_HIGH, CONFIDENCE_HIGH, features=["sessions"], text="sessions are unusable")
    secondary = cluster("CRASH", SEVERITY_HIGH, CONFIDENCE_HIGH, features=["sessions"], text="sessions are unusable")
    secondary.correlation_factor = 0.4
    prof = profile(sessions=LEVEL_IMPORTANT)
    only_primary = compute_personal_readiness(prof, [primary])
    both = compute_personal_readiness(prof, [primary, secondary])
    assert only_primary.impact < both.impact < 100
