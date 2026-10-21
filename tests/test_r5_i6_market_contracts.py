from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone

import pytest
from r5_i6_market_fixture import (
    BINDING,
    FIXTURE,
    KNOWN,
    MATERIAL,
    SOURCE,
    START,
    WINDOW,
    budget,
    job,
    universe,
)

from market_data_foundation.canonical.model import ContractError, Key, Kind
from market_data_foundation.domain_contracts.dense_market import (
    RAW_DAILY_BARS,
    SECURITY_UNIVERSE,
    require_bounded_universe,
)
from market_data_foundation.domain_contracts.market_coverage import (
    CoverageClass,
    CoverageStatus,
    PointEvidence,
    classify_coverage,
    lifecycle_calendar_state,
    plan_recovery,
)
from market_data_foundation.domain_contracts.market_planning import (
    DAILY_DEFINITIONS,
    GridPoint,
    JobMode,
    LogicalReadUnit,
    RecoveryOrigin,
    plan_job,
)
from market_data_foundation.domain_contracts.market_universe import (
    REAL_QUALIFICATIONS,
    AssetClass,
    CalendarDay,
    DateWindow,
    EvidenceMode,
    InstrumentIdentity,
    Lifecycle,
    MembershipMode,
    Qualification,
    QualificationStatus,
    canonical_bytes,
)


def point(offset):
    return GridPoint(universe().members[0].instrument, START + timedelta(days=offset))


def evidence(plan, offset, classification, **kwargs):
    target = point(offset)
    shard_ids = tuple(
        shard.identity for shard in plan.shards if target in shard.target_points
    )
    return PointEvidence(
        target,
        classification,
        ("synthetic-evidence",),
        original_shards=shard_ids,
        **kwargs,
    )


def all_classes(plan):
    return (
        evidence(
            plan,
            1,
            CoverageClass.OBSERVED_VALID,
            material=MATERIAL,
            source_row_locator="synthetic.row.0.volume=0",
        ),
        evidence(
            plan,
            3,
            CoverageClass.NO_TRADE_CONFIRMED,
            no_trade_contract="synthetic-confirmed-suspension-and-missing-row-contract",
        ),
        evidence(
            plan,
            4,
            CoverageClass.COLLECTION_FAILED,
            effects_confirmed_absent=True,
            retryable_confirmed=True,
        ),
        evidence(
            plan,
            5,
            CoverageClass.MISSING_EXPECTED,
            effects_confirmed_absent=True,
            retryable_confirmed=True,
        ),
        evidence(plan, 6, CoverageClass.QUALITY_REJECTED, material=MATERIAL),
    )


def test_revisions_are_immutable_content_not_mutable_directory_aliases():
    content = universe()
    with pytest.raises(FrozenInstanceError):
        content.universe_id = "changed"
    changed = replace(
        content, members=(replace(content.members[0], original_code="renamed-code"),)
    )
    assert changed.revision != content.revision
    assert changed.members[0].instrument == content.members[0].instrument
    assert changed.member_digest != content.member_digest
    changed_calendar = replace(
        content.calendars[0],
        days=(replace(content.calendars[0].days[0], arrangement="special-open"),)
        + content.calendars[0].days[1:],
    )
    assert changed_calendar.revision != content.calendars[0].revision
    assert replace(content, calendars=(changed_calendar,)).revision != content.revision
    with pytest.raises(ContractError, match="immutable_tuple"):
        replace(content, members=list(content.members))


def test_canonical_input_normalizes_instants_and_keeps_namespace_and_dates():
    assert canonical_bytes(KNOWN) == canonical_bytes(
        KNOWN.astimezone(timezone(timedelta(hours=8)))
    )
    assert canonical_bytes(date(2024, 1, 29)) == b'"2024-01-29"'
    with pytest.raises(ContractError, match="naive_time"):
        canonical_bytes(datetime(2024, 1, 1))
    with pytest.raises(ContractError, match="noncanonical_content"):
        canonical_bytes({"mutable": "object"})
    unit = LogicalReadUnit(DAILY_DEFINITIONS[0].definition, point(1))
    assert (
        replace(
            unit,
            point=replace(
                point(1),
                instrument=InstrumentIdentity(
                    "other.namespace", point(1).instrument.instrument_id
                ),
            ),
        ).identity
        != unit.identity
    )
    assert (
        replace(
            unit, definition=replace(unit.definition, revision="another-revision")
        ).identity
        != unit.identity
    )


def test_real_qualifications_and_legacy_bounds_never_become_full_market():
    assert len(REAL_QUALIFICATIONS) == 4
    assert all(gates == Qualification() for _, gates in REAL_QUALIFICATIONS)
    assert FIXTURE.semantics["evidence_mode"] == "synthetic"
    for category in AssetClass:
        spec = job(universe(category))
        assert plan_job(spec).points
        with pytest.raises(ContractError, match="offline_only"):
            spec.require_execution()
    with pytest.raises(ContractError, match="full_market_not_admitted"):
        replace(universe(), completeness="complete_a_share")
    with pytest.raises(ContractError, match="completeness_not_admitted"):
        require_bounded_universe(
            ("600000.SH",),
            completeness="full_market",
            identity_namespace="provider_suffix",
        )
    assert SECURITY_UNIVERSE.revision == RAW_DAILY_BARS.revision == "p3-r1"
    with pytest.raises(ContractError, match="real_qualification_remains_unknown"):
        content = universe()
        calendar = replace(
            content.calendars[0],
            provenance=replace(content.calendars[0].provenance, mode=EvidenceMode.LIVE),
            qualification=QualificationStatus.UNKNOWN,
        )
        replace(
            content,
            provenance=replace(content.provenance, mode=EvidenceMode.LIVE),
            calendars=(calendar,),
        )


@pytest.mark.parametrize("category", list(AssetClass))
def test_category_definitions_do_not_inherit_stock_or_other_category_semantics(
    category,
):
    daily = next(item for item in DAILY_DEFINITIONS if item.category == category)
    assert daily.definition != RAW_DAILY_BARS.key
    assert len({item.definition for item in DAILY_DEFINITIONS}) == 4
    if category in (AssetClass.INDEX, AssetClass.SECTOR_INDEX):
        assert daily.price_unit == "index_points"
        with pytest.raises(ContractError, match="index_units"):
            replace(daily, volume_unit="shares")
    else:
        with pytest.raises(ContractError, match="stock_etf_units"):
            replace(daily, price_basis="qfq")


def test_historical_union_contains_delisted_and_reused_codes_without_merging_identity():
    content = universe()
    member = content.members[0]
    old = replace(
        member,
        instrument=InstrumentIdentity("synthetic.stable", "retired-identity"),
        membership=(DateWindow(START, date(2024, 2, 1)),),
        lifecycle=Lifecycle(WINDOW, START, date(2024, 2, 1), ("synthetic-retirement",)),
    )
    new = replace(
        member,
        membership=(DateWindow(date(2024, 2, 1), WINDOW.end_exclusive),),
        lifecycle=Lifecycle(WINDOW, date(2024, 2, 1), None, ("synthetic-listing",)),
    )
    original = content.mappings[0]
    historical = replace(
        content,
        members=(new, old),
        mappings=(
            replace(original, instrument=old.instrument, validity=old.membership[0]),
            replace(original, validity=new.membership[0]),
        ),
    )
    plan = plan_job(job(historical))
    assert len(plan.points) == 18
    assert {item.instrument for item in plan.points} == {old.instrument, new.instrument}
    manifest = classify_coverage(plan)
    states = {entry.point: entry.classification for entry in manifest.entries}
    assert (
        states[GridPoint(old.instrument, date(2024, 2, 2))]
        == CoverageClass.DELISTED_OR_TERMINATED
    )
    assert states[GridPoint(new.instrument, START)] == CoverageClass.NOT_YET_LISTED
    current = replace(
        historical,
        members=(new,),
        mappings=(historical.mappings[0],)
        if historical.mappings[0].instrument == new.instrument
        else (historical.mappings[1],),
    )
    assert current.revision != historical.revision
    assert len(plan_job(job(current)).points) == 9
    assert plan == plan_job(
        job(
            replace(
                historical,
                members=tuple(reversed(historical.members)),
                mappings=tuple(reversed(historical.mappings)),
            )
        )
    )


def test_mapping_changes_are_explicit_and_unmapped_or_colliding_members_fail():
    content = universe()
    with pytest.raises(ContractError, match="explicit_source_mapping_required"):
        plan_job(job(replace(content, mappings=())))
    with pytest.raises(ContractError, match="explicit_source_mapping_required"):
        plan_job(
            replace(
                job(),
                routes=(
                    replace(
                        job().routes[0], source=Key(Kind.SOURCE, "other-source", "v1")
                    ),
                ),
            )
        )
    with pytest.raises(ContractError, match="ambiguous_source_mapping"):
        replace(content, mappings=(content.mappings[0], content.mappings[0]))
    other = replace(
        content.members[0],
        instrument=InstrumentIdentity("other.namespace", "invented-stable-id"),
    )
    with pytest.raises(ContractError, match="source_code_collision"):
        replace(
            content,
            members=content.members + (other,),
            mappings=content.mappings
            + (replace(content.mappings[0], instrument=other.instrument),),
        )
    split = date(2024, 2, 1)
    mappings = (
        replace(content.mappings[0], validity=DateWindow(START, split)),
        replace(
            content.mappings[0],
            request_code="explicit-new-code",
            validity=DateWindow(split, WINDOW.end_exclusive),
        ),
    )
    plan = plan_job(job(replace(content, mappings=mappings)))
    assert {shard.mapping.request_code for shard in plan.shards} == {
        "invented-request-code",
        "explicit-new-code",
    }
    assert all(shard.mapping.validity.encloses(shard.window) for shard in plan.shards)


def test_snapshot_and_known_time_cannot_be_backdated_into_history():
    content = universe()
    with pytest.raises(ContractError, match="known_at_backdated"):
        replace(content, known_at=KNOWN - timedelta(days=1))
    with pytest.raises(ContractError, match="known_at_backdated"):
        replace(content.calendars[0], known_at=KNOWN - timedelta(days=1))
    snapshot = replace(
        content,
        mode=MembershipMode.SNAPSHOT,
        effective=DateWindow(date(2024, 2, 1), date(2024, 2, 2)),
        history_window=DateWindow(date(2024, 2, 1), date(2024, 2, 2)),
    )
    with pytest.raises(ContractError, match="snapshot_cannot_claim_history"):
        replace(content, mode=MembershipMode.SNAPSHOT)
    with pytest.raises(ContractError, match="universe_window_not_covered"):
        job(snapshot)


def test_plans_are_stable_month_bounded_and_exhaustively_expandable():
    spec = job()
    first = plan_job(spec)
    assert first == plan_job(spec)
    assert first.identity == plan_job(spec).identity
    assert len(first.points) == 9
    assert len(first.units) == 2
    assert {read.point for unit in first.units for read in unit.expand()} == set(
        first.points
    )
    assert sum(len(unit.expand()) for unit in first.units) == len(first.points)
    assert all(shard.instrument == shard.mapping.instrument for shard in first.shards)
    assert first.request_count == len(
        first.shards
    )  # One explicit page per single member request.
    assert first.shards[0].route.pages.end_offset_exclusive == 5
    assert replace(spec, occurrence="another-occurrence").identity != spec.identity
    assert {
        shard.identity
        for shard in plan_job(replace(spec, occurrence="another-occurrence")).shards
    }.isdisjoint({shard.identity for shard in first.shards})
    assert (
        replace(spec, storage_policy_revision="different-policy").identity
        != spec.identity
    )
    with pytest.raises(ContractError, match="month_bounded"):
        replace(first.units[0], window=WINDOW)
    with pytest.raises(ContractError, match="new_daily_definition_required"):
        replace(spec.routes[0].daily, definition=RAW_DAILY_BARS.key)


@pytest.mark.parametrize(
    "field,value,classification",
    [
        ("max_members", 0, "grid_budget"),
        ("max_days", 8, "grid_budget"),
        ("max_grid_points", 8, "grid_budget"),
        ("max_shards", 1, "shard_budget"),
        ("max_publication_units", 1, "publication_unit_budget"),
        ("max_requests", 1, "request_material_or_row"),
        ("max_rows", 1, "request_material_or_row"),
        ("max_material_bytes", 1, "request_material_or_row"),
        ("max_versions", 1, "version_publication_budget"),
        ("max_publications", 1, "version_publication_budget"),
        ("max_connections", 1, "connection_probe_budget"),
    ],
)
def test_budgets_fail_without_truncating_the_denominator(field, value, classification):
    with pytest.raises(ContractError, match=classification):
        plan_job(replace(job(), budget=replace(budget(), **{field: value})))


def test_preflight_bounds_before_date_expansion(monkeypatch):
    import market_data_foundation.domain_contracts.market_planning as planning

    def forbidden(*args):
        raise AssertionError("date expansion occurred before preflight")

    monkeypatch.setattr(planning, "dates", forbidden)
    with pytest.raises(ContractError, match="grid_budget"):
        plan_job(replace(job(), budget=replace(budget(), max_grid_points=8)))
    with pytest.raises(ContractError, match="incomplete_trading_date"):
        replace(job(), completed_through=START)
    with pytest.raises(ContractError, match="zero_retry"):
        replace(budget(), sdk_redials=1)


def test_nine_classes_partition_grid_and_keep_unknown_in_denominator():
    plan = plan_job(job())
    manifest = classify_coverage(plan, all_classes(plan))
    assert manifest.denominator == 9
    assert dict(manifest.counts) == {kind: 1 for kind in CoverageClass}
    assert sum(count for _, count in manifest.counts) == manifest.denominator
    assert len({entry.point for entry in manifest.entries}) == manifest.denominator
    assert manifest.axes.bar_obligations == CoverageStatus.UNKNOWN
    assert (
        manifest.axes.material_readback
        == manifest.axes.formal_publication
        == manifest.global_status
        == CoverageStatus.UNKNOWN
    )
    missing = classify_coverage(plan)
    assert dict(missing.counts)[CoverageClass.UNKNOWN] == 6
    assert dict(missing.counts)[CoverageClass.NO_TRADE_CONFIRMED] == 0


def test_complete_explanations_are_not_formal_publication_or_global_completion():
    plan = plan_job(job())
    rows = tuple(
        evidence(
            plan,
            offset,
            CoverageClass.OBSERVED_VALID,
            material=MATERIAL,
            source_row_locator=f"synthetic.row.{offset}",
        )
        for offset in (1, 3, 4, 5, 6, 7)
    )
    complete = classify_coverage(plan, rows)
    assert complete.axes.bar_obligations == CoverageStatus.COMPLETE
    assert complete.global_status == CoverageStatus.UNKNOWN
    partial = classify_coverage(
        plan,
        rows[:-1]
        + (
            evidence(
                plan, 7, CoverageClass.COLLECTION_FAILED, effects_confirmed_absent=True
            ),
        ),
    )
    assert partial.axes.bar_obligations == CoverageStatus.PARTIAL
    assert (
        classify_coverage(plan, rows, predecessor=partial).predecessor_revision
        == partial.revision
    )
    assert plan_recovery(complete, budget()).backfill == ()


@pytest.mark.parametrize("is_open", [False, True], ids=["closed", "open"])
@pytest.mark.parametrize("is_member", [False, True], ids=["membership-gap", "member"])
@pytest.mark.parametrize(
    "lifecycle_case", ["known", "absent", "first-trade-missing", "outside-window"]
)
@pytest.mark.parametrize("calendar_case", ["qualified", "unqualified", "day-missing"])
def test_membership_lifecycle_calendar_cross_product_fails_closed(
    is_open, is_member, lifecycle_case, calendar_case
):
    content = universe()
    member = content.members[0]
    target = point(2)
    if not is_member:
        member = replace(
            member,
            membership=(
                DateWindow(point(1).day, target.day),
                DateWindow(point(3).day, point(8).day),
            ),
        )
    lifecycle = member.lifecycle
    if lifecycle_case == "absent":
        lifecycle = Lifecycle(None, None, None, ())
    elif lifecycle_case == "first-trade-missing":
        lifecycle = replace(lifecycle, first_trade=None)
    elif lifecycle_case == "outside-window":
        lifecycle = replace(
            lifecycle, known_window=DateWindow(point(3).day, WINDOW.end_exclusive)
        )
    calendar = content.calendars[0]
    calendar = replace(
        calendar,
        qualification=(
            QualificationStatus.UNKNOWN
            if calendar_case == "unqualified"
            else QualificationStatus.QUALIFIED
        ),
        days=tuple(
            replace(day, is_open=is_open) if day.day == target.day else day
            for day in calendar.days
            if calendar_case != "day-missing" or day.day != target.day
        ),
    )
    plan = plan_job(
        job(
            replace(
                content,
                members=(replace(member, lifecycle=lifecycle),),
                calendars=(calendar,),
            )
        )
    )
    qualified = is_member and lifecycle_case == "known" and calendar_case == "qualified"
    structural = (
        (None if is_open else CoverageClass.MARKET_CLOSED)
        if qualified
        else CoverageClass.UNKNOWN
    )
    assert lifecycle_calendar_state(plan, target) == structural
    manifest = classify_coverage(plan)
    assert manifest.entries[2].classification == (structural or CoverageClass.UNKNOWN)
    assert manifest.axes.member_enumeration == CoverageStatus.COMPLETE
    assert manifest.axes.lifecycle_calendar == (
        CoverageStatus.COMPLETE if qualified else CoverageStatus.UNKNOWN
    )
    assert manifest.axes.bar_obligations == CoverageStatus.UNKNOWN
    assert manifest.denominator == len(plan.points) == 9
    assert sum(count for _, count in manifest.counts) == manifest.denominator
    assert len({entry.point for entry in manifest.entries}) == manifest.denominator
    recovery = plan_recovery(manifest, budget())
    assert recovery.backfill == recovery.reprocess_first == ()
    assert (target in recovery.investigate_original) == (
        structural != CoverageClass.MARKET_CLOSED
    )


@pytest.mark.parametrize("is_open", [False, True], ids=["closed", "open"])
@pytest.mark.parametrize("calendar_case", ["qualified", "unqualified", "day-missing"])
@pytest.mark.parametrize(
    "offset,classification",
    [(0, CoverageClass.NOT_YET_LISTED), (8, CoverageClass.DELISTED_OR_TERMINATED)],
)
def test_evidenced_lifecycle_boundaries_precede_membership_and_calendar(
    is_open, calendar_case, offset, classification
):
    content = universe()
    target = point(offset)
    assert not content.members[0].includes(target.day)
    calendar = content.calendars[0]
    calendar = replace(
        calendar,
        qualification=(
            QualificationStatus.UNKNOWN
            if calendar_case == "unqualified"
            else QualificationStatus.QUALIFIED
        ),
        days=tuple(
            replace(day, is_open=is_open) if day.day == target.day else day
            for day in calendar.days
            if calendar_case != "day-missing" or day.day != target.day
        ),
    )
    plan = plan_job(job(replace(content, calendars=(calendar,))))
    assert lifecycle_calendar_state(plan, target) == classification
    manifest = classify_coverage(plan)
    assert manifest.entries[offset].classification == classification
    recovery = plan_recovery(manifest, budget())
    assert target not in recovery.investigate_original
    assert recovery.backfill == recovery.reprocess_first == ()


@pytest.mark.parametrize("is_open", [False, True], ids=["closed-gap", "open-gap"])
def test_membership_gap_preserves_nine_classes_axes_and_confirmed_gap_recovery(is_open):
    content = universe()
    member = replace(
        content.members[0],
        membership=(
            DateWindow(point(1).day, point(2).day),
            DateWindow(point(3).day, point(8).day),
        ),
    )
    calendar = replace(
        content.calendars[0],
        days=tuple(
            replace(day, is_open=is_open)
            if day.day == point(2).day
            else replace(day, is_open=False)
            if day.day == point(3).day
            else day
            for day in content.calendars[0].days
        ),
    )
    plan = plan_job(job(replace(content, members=(member,), calendars=(calendar,))))
    rows = all_classes(plan)
    rows = (
        (rows[0],)
        + rows[2:]
        + (
            evidence(
                plan,
                7,
                CoverageClass.NO_TRADE_CONFIRMED,
                no_trade_contract="synthetic-confirmed-no-trade",
            ),
        )
    )
    manifest = classify_coverage(plan, rows)
    assert manifest.denominator == 9
    assert dict(manifest.counts) == {kind: 1 for kind in CoverageClass}
    assert tuple(entry.point for entry in manifest.entries) == plan.points
    assert sum(count for _, count in manifest.counts) == manifest.denominator
    assert manifest.entries[2].classification == CoverageClass.UNKNOWN
    assert manifest.entries[3].classification == CoverageClass.MARKET_CLOSED
    assert manifest.axes.member_enumeration == CoverageStatus.COMPLETE
    assert (
        manifest.axes.lifecycle_calendar
        == manifest.axes.bar_obligations
        == manifest.axes.material_readback
        == manifest.axes.formal_publication
        == manifest.global_status
        == CoverageStatus.UNKNOWN
    )
    recovery = plan_recovery(manifest, budget())
    assert len(recovery.backfill) == 1
    assert recovery.backfill[0].target_points == (point(4), point(5))
    assert recovery.backfill[0].request_superset == (point(4), point(5))
    assert recovery.reprocess_first == (point(6),)
    assert recovery.investigate_original == (point(2),)
    for classification in (
        CoverageClass.MARKET_CLOSED,
        CoverageClass.NOT_YET_LISTED,
        CoverageClass.DELISTED_OR_TERMINATED,
        CoverageClass.OBSERVED_VALID,
        CoverageClass.NO_TRADE_CONFIRMED,
    ):
        claim = evidence(
            plan,
            2,
            classification,
            material=MATERIAL,
            source_row_locator="synthetic-gap-row",
            no_trade_contract="synthetic-no-trade-claim",
            effects_confirmed_absent=True,
        )
        with pytest.raises(ContractError, match="obligation_not_qualified"):
            classify_coverage(plan, rows + (claim,))


def test_calendar_lifecycle_and_unresolved_effects_cannot_be_guessed():
    plan = plan_job(job())
    with pytest.raises(ContractError, match="unresolved_effect_is_unknown"):
        evidence(plan, 4, CoverageClass.COLLECTION_FAILED, retryable_confirmed=True)
    with pytest.raises(ContractError, match="unknown_cannot_be_retried"):
        evidence(plan, 4, CoverageClass.UNKNOWN, retryable_confirmed=True)
    with pytest.raises(ContractError, match="no_trade_source_contract"):
        evidence(plan, 3, CoverageClass.NO_TRADE_CONFIRMED)
    with pytest.raises(ContractError, match="contradicts_calendar"):
        classify_coverage(
            plan,
            (
                evidence(
                    plan,
                    2,
                    CoverageClass.OBSERVED_VALID,
                    material=MATERIAL,
                    source_row_locator="bad-closed-day-row",
                ),
            ),
        )
    with pytest.raises(ContractError, match="explanation_not_evidenced"):
        classify_coverage(plan, (evidence(plan, 3, CoverageClass.NOT_YET_LISTED),))
    content = universe()
    unknown_calendar = replace(
        content,
        calendars=(
            replace(content.calendars[0], qualification=QualificationStatus.UNKNOWN),
        ),
    )
    unknown_plan = plan_job(job(unknown_calendar))
    with pytest.raises(ContractError, match="obligation_not_qualified"):
        classify_coverage(
            unknown_plan,
            (
                evidence(
                    unknown_plan,
                    4,
                    CoverageClass.MISSING_EXPECTED,
                    effects_confirmed_absent=True,
                ),
            ),
        )
    no_day = replace(
        content,
        calendars=(
            replace(
                content.calendars[0],
                days=tuple(
                    day for day in content.calendars[0].days if day.day != point(2).day
                ),
            ),
        ),
    )
    assert (
        next(
            entry.classification
            for entry in classify_coverage(plan_job(job(no_day))).entries
            if entry.point == point(2)
        )
        == CoverageClass.UNKNOWN
    )


def test_extra_members_dates_and_duplicates_are_retained_as_anomalies():
    plan = plan_job(job())
    observed = all_classes(plan)[0]
    extras = (
        replace(
            observed,
            point=GridPoint(InstrumentIdentity("other.namespace", "alien"), START),
        ),
        replace(observed, point=replace(point(1), day=WINDOW.end_exclusive)),
    )
    manifest = classify_coverage(plan, (observed, observed) + extras)
    assert len(manifest.duplicates) == len(manifest.extras) == 2
    assert manifest.denominator == sum(count for _, count in manifest.counts) == 9
    assert (
        next(
            entry for entry in manifest.entries if entry.point == point(1)
        ).classification
        == CoverageClass.QUALITY_REJECTED
    )
    with pytest.raises(ContractError, match="anomalies_require_review"):
        plan_recovery(manifest, budget())


def test_backfill_only_confirmed_gaps_merges_neighbors_and_preserves_original_source():
    plan = plan_job(job())
    manifest = classify_coverage(plan, all_classes(plan))
    recovery = plan_recovery(manifest, budget())
    assert len(recovery.backfill) == 1
    candidate = recovery.backfill[0]
    assert candidate.target_points == candidate.request_superset == (point(4), point(5))
    assert candidate.original_job == plan.spec.identity
    assert candidate.manifest_revision == manifest.revision
    assert candidate.mapping.source == SOURCE and candidate.mapping.binding == BINDING
    assert candidate.route == plan.spec.routes[0]
    assert recovery.reprocess_first == (point(6),)
    assert recovery.investigate_original == (point(7),)
    assert recovery == plan_recovery(manifest, budget())
    with pytest.raises(ContractError, match="recovery_request_budget"):
        plan_recovery(manifest, replace(budget(), recovery_requests=0))
    origin = RecoveryOrigin(
        plan.spec.identity,
        candidate.original_shards,
        manifest.revision,
        candidate.identity,
        candidate.publication_occurrence_rule,
    )
    with pytest.raises(ContractError, match="confirmed_gap_candidates"):
        plan_job(replace(job(), mode=JobMode.BACKFILL, origin=origin))


def test_retained_material_is_reprocessed_and_success_between_gaps_is_not_requested():
    plan = plan_job(job())
    rows = (
        evidence(
            plan,
            3,
            CoverageClass.MISSING_EXPECTED,
            effects_confirmed_absent=True,
            retryable_confirmed=True,
        ),
        evidence(
            plan,
            4,
            CoverageClass.OBSERVED_VALID,
            material=MATERIAL,
            source_row_locator="synthetic-success",
        ),
        evidence(
            plan,
            5,
            CoverageClass.COLLECTION_FAILED,
            effects_confirmed_absent=True,
            retryable_confirmed=True,
        ),
        evidence(
            plan,
            6,
            CoverageClass.MISSING_EXPECTED,
            effects_confirmed_absent=True,
            retryable_confirmed=True,
            material=MATERIAL,
        ),
    )
    recovery = plan_recovery(classify_coverage(plan, rows), budget())
    assert len(recovery.backfill) == 2
    assert {
        point for candidate in recovery.backfill for point in candidate.request_superset
    } == {point(3), point(5)}
    assert recovery.reprocess_first == (point(6),)


def test_planning_and_recovery_have_no_network_process_database_or_write_effects(
    monkeypatch,
):
    import socket
    import subprocess
    from pathlib import Path

    import psycopg

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected external effect")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    plan = plan_job(job())
    assert plan_recovery(classify_coverage(plan, all_classes(plan)), budget()).backfill


def test_full_leap_year_and_31_day_month_units_expand_without_gaps():
    content = universe()
    year = DateWindow(date(2024, 1, 1), date(2025, 1, 1))
    member = replace(
        content.members[0],
        membership=(year,),
        lifecycle=Lifecycle(year, year.start, None, ("synthetic-full-year-lifecycle",)),
    )
    calendar = replace(
        content.calendars[0],
        window=year,
        days=tuple(
            CalendarDay(
                year.start + timedelta(days=offset),
                True,
                "synthetic-session",
                "synthetic-calendar",
            )
            for offset in range(366)
        ),
    )
    content = replace(
        content,
        effective=year,
        history_window=year,
        members=(member,),
        calendars=(calendar,),
        mappings=(replace(content.mappings[0], validity=year),),
    )
    plan = plan_job(
        replace(job(content), window=year, completed_through=date(2024, 12, 31))
    )
    assert len(plan.units) == 12
    assert len(plan.points) == 366
    assert max(len(unit.expand()) for unit in plan.units) == 31
    february = next(unit for unit in plan.units if unit.window.start.month == 2)
    assert len(february.expand()) == 29
    assert sum(len(unit.expand()) for unit in plan.units) == len(plan.points)
    assert {read.point for unit in plan.units for read in unit.expand()} == set(
        plan.points
    )
    assert plan.connection_capacity == plan.probe_capacity == len(plan.shards)
    assert plan.version_capacity == plan.publication_capacity == 12


def test_categories_and_source_qualifications_are_independent_in_mixed_universe():
    content = universe()
    members = tuple(
        replace(
            content.members[0],
            category=category,
            instrument=InstrumentIdentity("synthetic.stable", category.value),
        )
        for category in AssetClass
    )
    mappings = tuple(
        replace(
            content.mappings[0],
            instrument=member.instrument,
            request_namespace=f"explicit.{member.category}",
        )
        for member in members
    )
    mixed = replace(content, members=members, mappings=mappings)
    spec = job(mixed)
    plan = plan_job(spec)
    assert len(plan.points) == 36
    assert len({unit.definition for unit in plan.units}) == 4
    unknown_routes = tuple(
        replace(route, qualification=QualificationStatus.UNKNOWN)
        if route.daily.category == AssetClass.ETF
        else route
        for route in spec.routes
    )
    unknown = classify_coverage(plan_job(replace(spec, routes=unknown_routes)))
    assert unknown.axes.lifecycle_calendar == CoverageStatus.COMPLETE
    target = GridPoint(
        next(
            member.instrument for member in members if member.category == AssetClass.ETF
        ),
        START + timedelta(days=4),
    )
    claim = PointEvidence(
        target,
        CoverageClass.OBSERVED_VALID,
        ("synthetic-row-validation",),
        material=MATERIAL,
        source_row_locator="synthetic-row",
    )
    with pytest.raises(ContractError, match="obligation_not_qualified"):
        classify_coverage(unknown.plan, (claim,))
    etf_gates = tuple(
        (category, Qualification()) if category == AssetClass.ETF else (category, gates)
        for category, gates in content.qualifications
    )
    isolated = replace(mixed, qualifications=etf_gates)
    with pytest.raises(ContractError, match="obligation_not_qualified"):
        classify_coverage(plan_job(job(isolated)), (claim,))


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_rows", 2),
        ("max_connections", 0),
        ("max_versions", 0),
        ("max_publications", 0),
    ],
)
def test_recovery_uses_provider_page_and_publication_capacity_not_only_gap_count(
    field, value
):
    plan = plan_job(job())
    manifest = classify_coverage(plan, all_classes(plan))
    with pytest.raises(ContractError, match="recovery_.*budget_exceeded"):
        plan_recovery(manifest, replace(budget(), **{field: value}))
