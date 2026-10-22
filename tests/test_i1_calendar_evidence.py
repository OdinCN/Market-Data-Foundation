"""Frozen D1 calendar authority at both I1 boundaries; invented offline facts."""

from dataclasses import replace
from datetime import timedelta

import pytest

from market_data_foundation.canonical.market_consumer_read import contracts as c
from market_data_foundation.canonical.market_material import assess_daily
from market_data_foundation.canonical.model import ContractError, Key, Kind
from market_data_foundation.domain_contracts.market_coverage import (
    CoverageClass,
    classify_coverage,
)
from market_data_foundation.domain_contracts.market_planning import plan_job
from market_data_foundation.domain_contracts.market_universe import (
    QualificationStatus,
    canonical_bytes,
)
from market_data_foundation.integration.market_vertical import prepare_version
from tests.r5_i6_market_fixture import job
from tests.test_i1_market_vertical import Vertical


def falsely_closed(evidence):
    return tuple(
        replace(e, classification=CoverageClass.MARKET_CLOSED, no_trade_contract=None)
        if e.classification == CoverageClass.NO_TRADE_CONFIRMED
        else e
        for e in evidence
    )


def test_open_day_false_closure_is_rejected_before_version_effects(
    tmp_path, monkeypatch
):
    v = Vertical(tmp_path, no_trade=True)
    v.worker.run()
    v.intake()
    unit = v.plan.units[0]
    valid = v.prepare(unit)
    evidence = falsely_closed(valid.assessment.evidence)
    assert evidence != valid.assessment.evidence
    with pytest.raises(ContractError, match="coverage_explanation_not_evidenced"):
        classify_coverage(v.plan, evidence)
    before = canonical_bytes(v.ledger.checkpoint())
    journal = tuple(v.journal.events(v.plan.spec.identity))
    calls = tuple(v.source.calls)

    def forbidden(*args):
        raise AssertionError("invalid D1 evidence must fail before material reads")

    monkeypatch.setattr(v.store, "read", forbidden)
    with pytest.raises(ContractError, match="coverage_explanation_not_evidenced"):
        v.prepare(unit, evidence_transform=falsely_closed)
    assert canonical_bytes(v.ledger.checkpoint()) == before
    assert not v.ledger.checkpoint().versions
    assert not v.ledger.checkpoint().publications
    assert tuple(v.journal.events(v.plan.spec.identity)) == journal
    assert tuple(v.source.calls) == calls


@pytest.mark.parametrize(
    "damage, error",
    [
        ("calendar_revision", "frozen_plan_unit_required"),
        ("universe_revision", "frozen_plan_unit_required"),
        ("job", "lineage_scope_mismatch"),
        ("shard", "lineage_scope_mismatch"),
        ("source", "lineage_scope_mismatch"),
        ("binding", "lineage_scope_mismatch"),
        ("substituted_plan", "lineage_scope_mismatch"),
        ("missing_closed", "coverage_partition_mismatch"),
        ("missing_open", "coverage_partition_mismatch"),
        ("duplicate_date", "coverage_partition_mismatch"),
    ],
)
def test_prepare_anchors_original_plan_and_requires_every_date(
    tmp_path, monkeypatch, damage, error
):
    v = Vertical(tmp_path, no_trade=True)
    v.worker.run()
    v.intake()
    version = v.prepare(v.plan.units[0])
    unit, plan = version.assessment.unit, v.plan
    receipts, evidence = version.assessment.materials, version.assessment.evidence
    if damage in {"calendar_revision", "universe_revision"}:
        unit = replace(unit, **{damage: "foreign-frozen-revision"})
    elif damage in {"job", "shard", "source", "binding"}:
        value = (
            replace(getattr(receipts[0], damage), identity="foreign")
            if damage in {"source", "binding"}
            else "foreign-original"
        )
        receipts = (replace(receipts[0], **{damage: value}), *receipts[1:])
    elif damage == "substituted_plan":
        plan = plan_job(replace(plan.spec, occurrence="foreign-occurrence"))
        assert unit in plan.units  # Same unit alone does not prove the original Job.
    elif damage.startswith("missing_"):
        target = (
            CoverageClass.MARKET_CLOSED
            if damage == "missing_closed"
            else CoverageClass.NO_TRADE_CONFIRMED
        )
        assert any(e.classification == target for e in evidence)
        evidence = tuple(e for e in evidence if e.classification != target)
    else:
        evidence = (evidence[0], *evidence)
    before = canonical_bytes(v.ledger.checkpoint())

    def forbidden(*args):
        raise AssertionError("scope and partition must fail before material reads")

    monkeypatch.setattr(v.store, "read", forbidden)
    with pytest.raises(ContractError, match=error):
        prepare_version(
            version.key,
            unit,
            receipts,
            evidence,
            v.decoder,
            v.store,
            version.assessment.evaluated_at,
            version.frozen_at,
            plan=plan,
        )
    assert canonical_bytes(v.ledger.checkpoint()) == before


@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("batch", [False, True])
def test_real_closed_and_legal_no_trade_remain_complete(tmp_path, exact, batch):
    v = Vertical(tmp_path, no_trade=True)
    v.finish()
    evidence = tuple(e for version in v.versions for e in version.assessment.evidence)
    manifest = classify_coverage(v.plan, evidence)
    expected = {
        e.point.day.isoformat(): e.classification.value for e in manifest.entries
    }
    assert {"MARKET_CLOSED", "NO_TRADE_CONFIRMED"} <= set(expected.values())
    result = v.reader().read(v.query(exact=exact, batch=batch))
    assert result["status"] == "OK"
    assert all(item["coverage"] == "COMPLETE" for item in result["items"])
    assert len(result["units"]) == (2 if batch else 1) * len(v.plan.points)
    for row in result["units"]:
        assert row["classification"] == expected[row["day"]]
        assert row["evidence"]
        assert row["raw"] == {}


@pytest.mark.parametrize("unknown", ["calendar", "lifecycle"])
def test_unknown_frozen_obligation_cannot_be_filled_as_closed(tmp_path, unknown):
    spec = job()
    universe = spec.universe
    if unknown == "calendar":
        calendar = replace(
            universe.calendars[0], qualification=QualificationStatus.UNKNOWN
        )
        universe = replace(universe, calendars=(calendar,))
    else:
        member = universe.members[0]
        member = replace(member, lifecycle=replace(member.lifecycle, known_window=None))
        universe = replace(universe, members=(member,))
    v = Vertical(tmp_path, no_trade=True, spec=replace(spec, universe=universe))
    v.worker.run()
    v.intake()

    def fill_unknown(evidence):
        assert any(e.classification == CoverageClass.UNKNOWN for e in evidence)
        return tuple(
            replace(e, classification=CoverageClass.MARKET_CLOSED)
            if e.classification == CoverageClass.UNKNOWN
            else e
            for e in evidence
        )

    before = canonical_bytes(v.ledger.checkpoint())
    with pytest.raises(ContractError, match="coverage_obligation_not_qualified"):
        v.prepare(v.plan.units[0], evidence_transform=fill_unknown)
    assert canonical_bytes(v.ledger.checkpoint()) == before


@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("damage", ["false_closed", "closed_as_no_trade", "references"])
def test_d4_rejects_legacy_calendar_damage_without_stitching_or_effects(
    tmp_path, exact, damage
):
    v = Vertical(tmp_path, no_trade=True)
    v.finish()
    healthy = v.versions[0]
    legacy = v.prepare(healthy.assessment.unit, key=Key(Kind.VERSION, "legacy-damaged"))
    evidence = legacy.assessment.evidence
    if damage == "false_closed":
        evidence = falsely_closed(evidence)
    elif damage == "closed_as_no_trade":
        evidence = tuple(
            replace(
                e,
                classification=CoverageClass.NO_TRADE_CONFIRMED,
                no_trade_contract="synthetic-no-trade-contract",
            )
            if e.classification == CoverageClass.MARKET_CLOSED
            else e
            for e in evidence
        )
    else:
        # Simulate deserialization corruption that bypassed dataclass validation.
        lost = replace(evidence[0])
        object.__setattr__(lost, "references", ())
        evidence = (lost, *evidence[1:])
    # Bypass I1 preparation to model already-persisted legacy damage. D3 has no
    # frozen Plan; its genuine commit/publish/rebuild currently accepts this.
    assessment = assess_daily(
        legacy.assessment.unit,
        legacy.assessment.materials,
        evidence,
        v.decoder,
        v.store,
        legacy.assessment.evaluated_at,
    )
    legacy = replace(legacy, assessment=assessment)
    v.ledger.commit_version(legacy)
    for subject in legacy.admission_subjects:
        v.ledger.set_admission(subject, "synthetic-legacy", True)
    v.now += timedelta(seconds=1)
    v.ledger.publish(v.request(legacy))
    v.versions[0] = legacy
    v.rebuild("legacy-damage")
    # The healthy older Version and the healthy adjacent month remain present.
    # Neither may be stitched in to claim COMPLETE for either batch item.
    assert healthy in v.ledger.checkpoint().versions
    before = canonical_bytes(v.ledger.checkpoint())
    journal = tuple(v.journal.events(v.plan.spec.identity))
    calls = tuple(v.source.calls)
    with pytest.raises(c.ReadError, match="INDEX_INCONSISTENT"):
        v.reader().read(v.query(exact=exact, batch=True))
    assert canonical_bytes(v.ledger.checkpoint()) == before
    assert tuple(v.journal.events(v.plan.spec.identity)) == journal
    assert tuple(v.source.calls) == calls


@pytest.mark.parametrize("exact", [False, True])
def test_d4_rejects_missing_date_evidence_without_repair(tmp_path, exact):
    v = Vertical(tmp_path, no_trade=True)
    v.finish()
    version = v.versions[0]
    v.ledger._state.versions[version.key] = replace(
        version,
        assessment=replace(
            version.assessment, evidence=version.assessment.evidence[1:]
        ),
    )
    before = canonical_bytes(v.ledger.checkpoint())
    with pytest.raises(c.ReadError, match="INDEX_INCONSISTENT"):
        v.reader().read(v.query(exact=exact, batch=True))
    assert canonical_bytes(v.ledger.checkpoint()) == before
