from datetime import UTC, date, datetime, timedelta

import pytest

from market_data_foundation.canonical.model import ContractError, Key, Kind, Scope
from market_data_foundation.domain_contracts.dense_market import (
    ADJUSTMENT_FACTORS,
    CORPORATE_ACTIONS,
    MDF_DERIVED_ADJUSTED_DAILY_BARS,
    P2_HANDOFF_DISPOSITIONS,
    PROVIDER_ADJUSTED_DAILY_BARS,
    RAW_DAILY_BARS,
    SECURITY_UNIVERSE,
    Disposition,
    disposition_for,
    mdf_adjustment_transform,
    require_bounded_universe,
    require_explicit_daily_units,
    require_no_session_inference,
    require_supported_corporate_action,
)
from market_data_foundation.domain_contracts.kd import (
    KD_B043,
    KD_B043_CURRENT_RANK,
    KD_B044_DISPOSITION,
    KDRuntimeDescriptorIdentity,
    require_bounded_current_state,
    require_no_silent_fallback,
    require_principal_scope_within,
    require_runtime_binding_descriptor,
)
from market_data_foundation.domain_contracts.macro import (
    BLS_CPI_HEADLINES,
    BLS_CPI_SOURCE_CANDIDATE,
    MacroRevisionAxes,
    MacroTimes,
    require_binding_ready,
    require_parser_correction,
)
from market_data_foundation.qualification.contracts import Capability

BASE = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)


def test_named_contract_families_are_not_generic_placeholders() -> None:
    assert SECURITY_UNIVERSE.identity == "mdf.security.universe.a-share"
    assert RAW_DAILY_BARS.identity == "mdf.market.bars.daily.raw"
    assert CORPORATE_ACTIONS.identity == "mdf.market.corporate-actions"
    assert ADJUSTMENT_FACTORS.identity == "mdf.market.adjustment-factors"
    assert PROVIDER_ADJUSTED_DAILY_BARS.identity.endswith(".adjusted.provider")
    assert MDF_DERIVED_ADJUSTED_DAILY_BARS.identity.endswith(".adjusted.mdf")
    assert BLS_CPI_HEADLINES.identity == "mdf.macro.bls.cpi.headlines"
    assert KD_B043_CURRENT_RANK.identity == "mdf.kd.sector-analysis.current-rank"
    for definition in (
        SECURITY_UNIVERSE,
        RAW_DAILY_BARS,
        CORPORATE_ACTIONS,
        ADJUSTMENT_FACTORS,
        PROVIDER_ADJUSTED_DAILY_BARS,
        MDF_DERIVED_ADJUSTED_DAILY_BARS,
        BLS_CPI_HEADLINES,
        KD_B043_CURRENT_RANK,
    ):
        assert definition.unit_semantics.strip().lower() not in {
            "mixed",
            "provider-native",
            "unknown",
            "unspecified",
        }


def test_p2_handoff_dispositions_are_complete_and_preserve_limits() -> None:
    assert len(P2_HANDOFF_DISPOSITIONS) == 4
    assert (
        disposition_for(Capability.SECURITY_UNIVERSE).disposition
        == Disposition.NOT_ADMITTED
    )
    assert (
        disposition_for(Capability.CORPORATE_ACTIONS).disposition
        == Disposition.BOUNDED_CONTRACT
    )
    assert (
        disposition_for(Capability.ADJUSTMENT_FACTORS).disposition
        == Disposition.NOT_ADMITTED
    )
    assert (
        disposition_for(Capability.BARS_ADJUSTED).disposition
        == Disposition.NOT_ADMITTED
    )
    for item in P2_HANDOFF_DISPOSITIONS:
        assert item.reason
        assert item.evidence_needed
        assert item.later_gate


def test_incomplete_list_cannot_masquerade_as_complete_universe() -> None:
    members = ("600000.SH", "000001.SZ", "920000.BJ")
    assert (
        require_bounded_universe(
            members,
            completeness="bounded_observed",
            identity_namespace="provider_suffix",
        )
        == members
    )
    assert require_bounded_universe(
        ("600000.XSHG", "000001.XSHE", "920000.XBSE"),
        completeness="bounded_observed",
        identity_namespace="mic",
    ) == ("600000.XSHG", "000001.XSHE", "920000.XBSE")
    with pytest.raises(ContractError, match="universe_completeness_not_admitted"):
        require_bounded_universe(
            members,
            completeness="complete_a_share",
            identity_namespace="provider_suffix",
        )
    with pytest.raises(ContractError, match="security_namespace_not_admitted"):
        require_bounded_universe(
            members,
            completeness="bounded_observed",
            identity_namespace="canonical_guess",
        )
    with pytest.raises(ContractError, match="invalid_security_identity"):
        require_bounded_universe(
            ("920000.XBSE",),
            completeness="bounded_observed",
            identity_namespace="provider_suffix",
        )


def test_unknown_units_cannot_be_silently_normalized() -> None:
    assert require_explicit_daily_units(
        price_unit="CNY", volume_unit="shares", amount_unit="CNY"
    ) == ("CNY", "shares", "CNY")
    for bad in (
        "unknown",
        "provider-native",
        "mixed",
        "unspecified",
        "",
        "bananas",
        "lots",
    ):
        with pytest.raises(ContractError, match="daily_bar_volume_unit_not_admitted"):
            require_explicit_daily_units(
                price_unit="CNY", volume_unit=bad, amount_unit="CNY"
            )
    with pytest.raises(ContractError, match="daily_bar_price_unit_not_admitted"):
        require_explicit_daily_units(
            price_unit="milli-CNY", volume_unit="shares", amount_unit="CNY"
        )


def test_missing_sessions_cannot_be_filled_or_inferred() -> None:
    observed = (date(2024, 1, 2), date(2024, 1, 4))
    assert require_no_session_inference(observed, observed) == observed
    with pytest.raises(ContractError, match="session_inference_not_admitted"):
        require_no_session_inference(
            observed,
            (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)),
        )


def test_corporate_action_and_adjustment_contracts_fail_closed() -> None:
    assert require_supported_corporate_action(
        event_kind="cash_dividend", unit="CNY_per_share"
    ) == ("cash_dividend", "CNY_per_share")
    with pytest.raises(ContractError, match="corporate_action_category_not_admitted"):
        require_supported_corporate_action(event_kind="rights_issue", unit="unknown")
    raw_version = Key(Kind.VERSION, "raw-version-1")
    action_version = Key(Kind.VERSION, "action-version-1")
    for bad_definition in (ADJUSTMENT_FACTORS.key,):
        with pytest.raises(
            ContractError, match="adjustment_input_contract_not_admitted"
        ):
            mdf_adjustment_transform(
                revision="r1",
                raw_definition=RAW_DAILY_BARS.key,
                raw_version=raw_version,
                adjustment_definition=bad_definition,
                adjustment_input_version=action_version,
                anchor_policy="latest_observed_raw_bar",
            )
    with pytest.raises(ContractError, match="adjustment_anchor_policy_not_admitted"):
        mdf_adjustment_transform(
            revision="r1",
            raw_definition=RAW_DAILY_BARS.key,
            raw_version=raw_version,
            adjustment_definition=CORPORATE_ACTIONS.key,
            adjustment_input_version=action_version,
            anchor_policy="qfq",
        )
    transform = mdf_adjustment_transform(
        revision="r1",
        raw_definition=RAW_DAILY_BARS.key,
        raw_version=raw_version,
        adjustment_definition=CORPORATE_ACTIONS.key,
        adjustment_input_version=action_version,
        anchor_policy="latest_observed_raw_bar",
    )
    assert transform.identity == "mdf.market.bars.daily.adjust"


def test_macro_vintage_cannot_backdate_mdf_usable_time() -> None:
    valid = MacroTimes(
        reference_or_report_at=BASE - timedelta(days=30),
        provider_release_or_vintage_at=BASE - timedelta(days=1),
        obtained_at=BASE,
        recorded_at=BASE + timedelta(seconds=1),
        usable_at=BASE + timedelta(seconds=2),
    )
    assert valid.usable_at > valid.provider_release_or_vintage_at
    with pytest.raises(ContractError, match="macro_usable_time_backdated"):
        MacroTimes(
            reference_or_report_at=BASE - timedelta(days=30),
            provider_release_or_vintage_at=BASE - timedelta(days=1),
            obtained_at=BASE,
            recorded_at=BASE + timedelta(seconds=1),
            usable_at=BASE - timedelta(days=1),
        )


def test_macro_parser_correction_does_not_become_provider_revision() -> None:
    before = MacroRevisionAxes(
        series_id="bls.cpi.headlines",
        reference_period="2026-09",
        provider_revision="v1",
        acquisition_identity="acq-1",
        artifact_identity="artifact-1",
        parser_revision="parser-1",
        semantic_revision="semantic-1",
    )
    corrected = MacroRevisionAxes(
        series_id=before.series_id,
        reference_period=before.reference_period,
        provider_revision="v1",
        acquisition_identity=before.acquisition_identity,
        artifact_identity=before.artifact_identity,
        parser_revision="parser-2",
        semantic_revision=before.semantic_revision,
    )
    assert require_parser_correction(before, corrected) == corrected
    with pytest.raises(
        ContractError, match="parser_correction_cannot_change_provider_revision"
    ):
        require_parser_correction(
            before,
            MacroRevisionAxes(
                series_id=before.series_id,
                reference_period=before.reference_period,
                provider_revision="v2",
                acquisition_identity=before.acquisition_identity,
                artifact_identity=before.artifact_identity,
                parser_revision="parser-2",
                semantic_revision=before.semantic_revision,
            ),
        )
    with pytest.raises(ContractError, match="parser_correction_requires_same_evidence"):
        require_parser_correction(
            before,
            MacroRevisionAxes(
                series_id=before.series_id,
                reference_period=before.reference_period,
                provider_revision=before.provider_revision,
                acquisition_identity="acq-other",
                artifact_identity="artifact-other",
                parser_revision="parser-2",
                semantic_revision=before.semantic_revision,
            ),
        )

    repeated = MacroRevisionAxes(
        series_id=before.series_id,
        reference_period=before.reference_period,
        provider_revision=before.provider_revision,
        acquisition_identity="acq-2",
        artifact_identity="artifact-2",
        parser_revision=before.parser_revision,
        semantic_revision=before.semantic_revision,
    )
    assert before.semantic_dedupe_key == repeated.semantic_dedupe_key
    assert before.acquisition_identity != repeated.acquisition_identity


def test_macro_candidate_is_named_but_not_binding_ready() -> None:
    assert BLS_CPI_SOURCE_CANDIDATE.operation == "bls.cpi.headlines"
    assert not BLS_CPI_SOURCE_CANDIDATE.binding_ready
    with pytest.raises(ContractError, match="macro_source_not_binding_ready"):
        require_binding_ready(BLS_CPI_SOURCE_CANDIDATE)


def test_kd_b043_is_bounded_current_state_not_event_complete() -> None:
    assert require_no_silent_fallback(KD_B043) == KD_B043
    assert require_bounded_current_state(
        completeness="bounded_current_window",
        history_claim="sampled_current_state_history",
    ) == ("bounded_current_window", "sampled_current_state_history")
    with pytest.raises(ContractError, match="kd_full_snapshot_not_admitted"):
        require_bounded_current_state(
            completeness="full_snapshot",
            history_claim="sampled_current_state_history",
        )
    with pytest.raises(ContractError, match="kd_event_complete_history_not_admitted"):
        require_bounded_current_state(
            completeness="bounded_current_window",
            history_claim="event_complete_history",
        )


def test_kd_architecture_or_synthetic_descriptor_is_not_binding_ready() -> None:
    with pytest.raises(ContractError, match="kd_runtime_descriptor_required"):
        require_runtime_binding_descriptor(KD_B043, None)
    architecture_only = KDRuntimeDescriptorIdentity(
        build_revision="architecture-build",
        registry_revision="architecture-registry",
        operation_contract_revision="candidate-op-r1",
        projection_revision="candidate-projection-r1",
        source_variant=KD_B043.source_variant_basis,
        observed_from_current_runtime=False,
    )
    with pytest.raises(
        ContractError, match="kd_architecture_descriptor_not_runtime_evidence"
    ):
        require_runtime_binding_descriptor(KD_B043, architecture_only)
    assert not KD_B043.binding_ready


def test_kd_principal_scope_cannot_widen() -> None:
    restricted = Scope(frozenset({"kd:principal-a"}))
    assert require_principal_scope_within(restricted, restricted) == restricted
    with pytest.raises(ContractError, match="scope_widening"):
        require_principal_scope_within(Scope(), restricted)


def test_b044_self_block_does_not_block_p3_globally() -> None:
    assert KD_B044_DISPOSITION.status == "NOT_ADMITTED_SELF_BLOCKED"
    assert KD_B044_DISPOSITION.global_p3_blocker is False
