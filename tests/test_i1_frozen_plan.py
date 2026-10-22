"""Exact D1 Plan closure at I1 boundaries; only invented offline material."""

from dataclasses import replace

import pytest

from market_data_foundation.canonical.market_consumer_read import contracts as c
from market_data_foundation.canonical.market_material import assess_daily
from market_data_foundation.canonical.market_persistence import required_admissions
from market_data_foundation.canonical.model import ContractError
from market_data_foundation.domain_contracts.market_planning import plan_job
from market_data_foundation.domain_contracts.market_universe import canonical_bytes
from market_data_foundation.historical.market_daily_source import Lookup, LookupStatus
from market_data_foundation.historical.market_daily_worker import DailyRunner
from market_data_foundation.integration.market_vertical import (
    accept_original,
    prepare_version,
    retain_page,
)
from market_data_foundation.integration.market_vertical_read import (
    ConsumerCodec,
    MemoryConsumerProjection,
)
from tests.test_i1_market_vertical import Vertical, native

DAMAGE = ("calendar", "universe", "shards", "source", "binding", "capacity")


def forge(plan, damage):
    if damage in {"calendar", "universe"}:
        return replace(
            plan,
            units=tuple(
                replace(unit, **{damage + "_revision": "unrelated-frozen-revision"})
                for unit in plan.units
            ),
        )
    if damage == "capacity":
        return replace(plan, request_count=plan.request_count + 1)
    shard = plan.shards[0]
    if damage in {"source", "binding"}:
        foreign = replace(getattr(shard.route, damage), identity="foreign")
        shard = replace(
            shard,
            route=replace(shard.route, **{damage: foreign}),
            mapping=replace(shard.mapping, **{damage: foreign}),
        )
    else:
        shard = replace(shard, job_identity="foreign-job")
    # Keep every original receipt's shard. Membership checks alone accept this
    # extra shard, even with its coherent but foreign Source/Binding mapping.
    return replace(plan, shards=(*plan.shards, shard))


def forbidden(*args, **kwargs):
    raise AssertionError("forged Plan reached a material or write boundary")


@pytest.mark.parametrize("damage", DAMAGE)
def test_forged_plan_matches_d2_rejection_before_prepare_reads_or_commits(
    tmp_path, monkeypatch, damage
):
    v = Vertical(tmp_path, no_trade=True)
    v.worker.run()
    v.intake()
    original = v.prepare(v.plan.units[0])
    plan = forge(v.plan, damage)
    assert plan.spec == v.plan.spec
    assert plan != plan_job(plan.spec)
    if damage == "calendar":
        assert plan.shards == v.plan.shards  # Reviewer's exact minimal mutation.
    with pytest.raises(ContractError, match="exact_frozen_d1_plan_required"):
        DailyRunner(plan, v.journal, v.source, sleep=v.sleep)
    before = canonical_bytes(v.ledger.checkpoint())
    journal = tuple(v.journal.events(v.plan.spec.identity))
    calls = tuple(v.source.calls)
    monkeypatch.setattr(v.store, "read", forbidden)
    monkeypatch.setattr(v.ledger, "commit_version", forbidden)
    with pytest.raises(ContractError, match="exact_frozen_d1_plan_required"):
        prepare_version(
            original.key,
            plan.units[0],
            original.assessment.materials,
            original.assessment.evidence,
            v.decoder,
            v.store,
            original.assessment.evaluated_at,
            original.frozen_at,
            plan=plan,
        )
    assert canonical_bytes(v.ledger.checkpoint()) == before
    assert not v.ledger.checkpoint().versions
    assert not v.ledger.checkpoint().publications
    assert tuple(v.journal.events(v.plan.spec.identity)) == journal
    assert tuple(v.source.calls) == calls


@pytest.mark.parametrize("entry", ["retain", "reconcile"])
def test_forged_worker_plan_rejected_before_journal_material_read(
    tmp_path, monkeypatch, entry
):
    v = Vertical(tmp_path)
    v.worker.run()
    request = v.worker.request(v.plan.shards[0], 0)
    layout = v.layout(request)
    result = Lookup(
        request.identity,
        LookupStatus.FOUND,
        "synthetic-original",
        v.journal.read_material(v.journal.receipt(request)),
    )
    journal = tuple(v.journal.events(v.plan.spec.identity))
    before = canonical_bytes(v.ledger.checkpoint())
    v.worker.plan = forge(v.plan, "calendar")
    monkeypatch.setattr(v.journal, "read_material", forbidden)
    monkeypatch.setattr(v.ledger, "retain", forbidden)
    monkeypatch.setattr(v.worker, "accept_reconciliation", forbidden)
    with pytest.raises(ContractError, match="exact_frozen_d1_plan_required"):
        if entry == "retain":
            retain_page(v.worker, request, layout, v.ledger)
        else:
            accept_original(v.worker, request, result)
    assert canonical_bytes(v.ledger.checkpoint()) == before
    assert tuple(v.journal.events(v.plan.spec.identity)) == journal


@pytest.mark.parametrize("damage", DAMAGE)
def test_consumer_rejects_forged_plan_at_registration(tmp_path, monkeypatch, damage):
    v = Vertical(tmp_path)
    before = canonical_bytes(v.ledger.checkpoint())
    monkeypatch.setattr(v.store, "read", forbidden)
    monkeypatch.setattr(v.ledger.reader, "open", forbidden)
    with pytest.raises(ContractError, match="exact_frozen_d1_plan_required"):
        MemoryConsumerProjection(
            v.ledger.reader,
            "i1.synthetic.ledger",
            (forge(v.plan, damage),),
            (ConsumerCodec(v.decoder, native),),
        )
    assert canonical_bytes(v.ledger.checkpoint()) == before


@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("registry", ["original", "forged"])
def test_consumer_rejects_published_forged_calendar_before_material_read(
    tmp_path, monkeypatch, exact, batch, registry
):
    v = Vertical(tmp_path, no_trade=True)
    v.worker.run()
    v.intake()
    forged = forge(v.plan, "calendar")
    # Genuine D3 operations model legacy data accepted before the I1 fix. D3
    # does not own a Plan and cannot establish its D1 derivation by itself.
    for unit, fake in zip(v.plan.units, forged.units, strict=True):
        original = v.prepare(unit)
        assessment = assess_daily(
            fake,
            original.assessment.materials,
            original.assessment.evidence,
            v.decoder,
            v.store,
            original.assessment.evaluated_at,
        )
        version = replace(original, assessment=assessment)
        version = replace(version, admission_subjects=required_admissions(version))
        v.ledger.commit_version(version)
        for subject in version.admission_subjects:
            v.ledger.set_admission(subject, "synthetic-legacy", True)
        v.ledger.publish(v.request(version))
        v.versions.append(version)
    v.rebuild("legacy-forged-plan")
    reader = v.reader()  # Register the genuine Plan first.
    before = canonical_bytes(v.ledger.checkpoint())
    journal = tuple(v.journal.events(v.plan.spec.identity))
    calls = tuple(v.source.calls)
    # Simulate an older registration or subsequent registry pollution. Updating
    # both indexes prevents a unit membership check from hiding the Plan defect.
    if registry == "forged":
        reader.projection.jobs[forged.spec.identity] = forged
        reader.projection.units = {unit.identity: unit for unit in forged.units}
    reads = []

    def unexpected_read(*args):
        reads.append(args)
        forbidden()

    monkeypatch.setattr(v.store, "read", unexpected_read)
    error = "INDEX_INCONSISTENT" if registry == "forged" else "UNREGISTERED_UNIT"
    with pytest.raises(c.ReadError, match=error):
        reader.read(v.query(exact=exact, batch=batch))
    assert not reads
    assert canonical_bytes(v.ledger.checkpoint()) == before
    assert tuple(v.journal.events(v.plan.spec.identity)) == journal
    assert tuple(v.source.calls) == calls
