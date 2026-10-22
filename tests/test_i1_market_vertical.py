"""Actual D2 journal -> D3 ledger -> D4 reader; all market facts are invented."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from market_data_foundation.canonical.market_consumer_read import contracts as c
from market_data_foundation.canonical.market_consumer_read.cursor import CursorCodec
from market_data_foundation.canonical.market_consumer_read.reader import MarketReader
from market_data_foundation.canonical.market_material import (
    DailyBar,
    MemoryMaterialBytes,
    RowLocator,
    SyntheticDailyDecoder,
    byte_digest,
)
from market_data_foundation.canonical.market_persistence import (
    CommitUnknown,
    Fault,
    MemoryMarketLedger,
    Verification,
)
from market_data_foundation.canonical.market_projection import MonthKey
from market_data_foundation.canonical.market_publication import MarketPublicationRequest
from market_data_foundation.canonical.model import ContractError, Decision, Key, Kind
from market_data_foundation.domain_contracts.market_coverage import (
    CoverageClass,
    PointEvidence,
    expected_state,
)
from market_data_foundation.domain_contracts.market_planning import PagePlan, plan_job
from market_data_foundation.domain_contracts.market_universe import (
    EvidenceMode,
    canonical_bytes,
    content_id,
)
from market_data_foundation.historical.market_daily_journal import (
    Journal,
    page_metadata,
)
from market_data_foundation.historical.market_daily_source import (
    Lookup,
    LookupStatus,
    NativeField,
    NativeRow,
    OfflineBinding,
    Page,
)
from market_data_foundation.historical.market_daily_worker import DailyRunner
from market_data_foundation.integration.market_vertical import (
    IntakeLayout,
    RetainedPage,
    RetainedPageSource,
    accept_original,
    prepare_version,
    response_identity,
    retain_page,
    verify_original,
)
from market_data_foundation.integration.market_vertical_read import (
    ConsumerCodec,
    MemoryConsumerProjection,
    semantics,
)
from tests.r5_i6_market_fixture import KNOWN, job
from tests.test_r5_i6_d3_material_projection import Scenario


def native(raw, receipt):
    result = json.loads(raw)
    del result["day"]
    return result


class Source:
    def __init__(self, plan, contract, *, no_trade=False):
        self.plan, self.contract, self.no_trade = plan, contract, no_trade
        route = plan.spec.routes[0]
        self.binding = OfflineBinding(
            EvidenceMode.SYNTHETIC,
            route.source,
            route.binding,
            route.mapping_revision,
            route.implementation_identity,
            route.endpoint_contract,
            contract.producer_operation,
            contract.upstream_revision,
            contract.decoder_revision,
        )
        self.calls, self.saved = [], {}
        self.lose_response = False

    def page(self, request):
        points = [
            p
            for p in request.shard.request_superset
            if expected_state(self.plan, p) is None and not self.no_trade
        ]
        chosen = points[request.offset : request.offset + request.count]
        assert len(chosen) <= 1
        raw = (
            canonical_bytes(DailyBar(chosen[0].day, "10", "12", "9", "11", "0", "0"))
            if chosen
            else b'{"evidence":"synthetic explicit empty response"}'
        )
        rows = tuple(
            NativeRow(
                p.day.isoformat(),
                "row:" + p.day.isoformat(),
                tuple(
                    NativeField(
                        k, "decimal-text", v, "synthetic-field", "identity.synthetic"
                    )
                    for k, v in native(raw, None).items()
                ),
            )
            for p in chosen
        )
        return Page(
            request.identity,
            raw,
            rows,
            "synthetic-frozen-snapshot",
            request.offset + request.count >= len(points),
            "synthetic-bounded-exhaustion",
            self.contract.producer_operation,
            self.contract.upstream_revision,
            self.contract.decoder_revision,
            KNOWN + timedelta(seconds=20),
            KNOWN + timedelta(seconds=21),
            KNOWN + timedelta(seconds=22),
        )

    def fetch(self, request, charge):
        self.calls.append(request.identity)
        page = self.page(request)
        self.saved[request.identity] = page
        if self.lose_response:
            raise TimeoutError("must not appear in receipt")
        return page

    def lookup_original(self, request):
        page = self.saved.get(request.identity)
        return Lookup(
            request.identity,
            LookupStatus.FOUND if page else LookupStatus.UNKNOWN,
            "synthetic-original-response",
            page,
        )


class Vertical:
    def __init__(self, directory, *, no_trade=False, spec=None):
        spec = spec or job()
        route = replace(
            spec.routes[0],
            pages=PagePlan(
                0, 1, 5, "ascending", "synthetic-frozen", "synthetic-exhaustion"
            ),
        )
        self.plan = plan_job(replace(spec, routes=(route,)))
        base = Scenario().contract
        self.contract = replace(
            base,
            source=route.source,
            binding=route.binding,
            mapping_revision=route.mapping_revision,
        )
        self.decoder = SyntheticDailyDecoder(self.contract)
        self.now = KNOWN + timedelta(seconds=120)
        self.ns = int((KNOWN + timedelta(seconds=10)).timestamp()) * 10**9
        self.source = Source(self.plan, self.contract, no_trade=no_trade)
        self.journal = Journal(directory, clock_ns=lambda: self.ns)
        self.worker = DailyRunner(
            self.plan, self.journal, self.source, sleep=self.sleep
        )
        self.store = MemoryMaterialBytes()
        self.ledger = self.fresh()
        self.retained, self.versions, self.publications = [], [], []

    def sleep(self, seconds):
        self.ns += int(seconds * 10**9)

    def fresh(self):
        return MemoryMarketLedger(
            "i1.synthetic.ledger", self.store, (self.decoder,), lambda: self.now
        )

    def layout(self, request, *, requested_at=True):
        page = self.journal.read_material(self.journal.receipt(request))
        return IntakeLayout(
            tuple(
                RowLocator(
                    row.locator,
                    response_identity(page),
                    0,
                    len(page.raw),
                    byte_digest(page.raw),
                )
                for row in page.rows
            ),
            self.contract.request_fields,
            "r5-i6-d1-synthetic-only",
            KNOWN + timedelta(seconds=10) if requested_at else None,
            "synthetic-original-request-clock" if requested_at else "UNKNOWN",
        )

    def intake(self):
        self.retained = [
            retain_page(
                self.worker, receipt.request, self.layout(receipt.request), self.ledger
            )
            for shard in self.worker.report().shards
            for receipt in shard.receipts
        ]

    def prepare(self, unit, *, key=None, evidence_transform=None):
        receipts = tuple(
            r.material
            for r in self.retained
            if next(
                s for s in self.plan.shards if s.identity == r.material.shard
            ).window.overlaps(unit.window)
        )
        evidence = []
        for logical in unit.expand():
            point = logical.point
            expected = expected_state(self.plan, point)
            found = next(
                (
                    (r, row)
                    for r in receipts
                    for row in r.rows
                    if row.identity == "row:" + point.day.isoformat()
                ),
                None,
            )
            if found:
                r, row = found
                item = PointEvidence(
                    point,
                    CoverageClass.OBSERVED_VALID,
                    ("synthetic-explicit-row",),
                    (r.shard,),
                    (r.attempt,),
                    (r.request,),
                    r.key,
                    row.identity,
                )
            else:
                item = PointEvidence(
                    point,
                    expected or CoverageClass.NO_TRADE_CONFIRMED,
                    ("synthetic-explicit-calendar-or-no-trade",),
                    no_trade_contract="synthetic-no-trade-contract"
                    if not expected
                    else None,
                )
            evidence.append(item)
        if evidence_transform is not None:
            evidence = evidence_transform(tuple(evidence))
        return prepare_version(
            key or Key(Kind.VERSION, "i1:" + unit.identity),
            unit,
            receipts,
            tuple(evidence),
            self.decoder,
            self.store,
            KNOWN + timedelta(seconds=30),
            KNOWN + timedelta(seconds=40),
            plan=self.plan,
        )

    def request(self, version, decision=Decision.PUBLISH, predecessor=None, name=None):
        name = name or "publish:" + version.key.identity
        return MarketPublicationRequest(
            Key(Kind.PUBLICATION, name),
            name,
            version.key,
            decision,
            predecessor,
            ("synthetic-reviewed-only",),
            "synthetic-rule-r1",
        )

    def finish(self):
        assert self.worker.run().state == "COMPLETE"
        self.intake()
        for unit in self.plan.units:
            version = self.prepare(unit)
            self.ledger.commit_version(version)
            for subject in version.admission_subjects:
                self.ledger.set_admission(subject, "synthetic-r1", True)
            request = self.request(version)
            self.ledger.publish(request)
            self.versions.append(version)
            self.publications.append(request)
        self.rebuild("g1")

    def rebuild(self, generation):
        months = tuple(
            dict.fromkeys(
                MonthKey(
                    u.definition,
                    u.instrument,
                    u.window.start.year,
                    u.window.start.month,
                )
                for u in self.plan.units
            )
        )
        self.ledger.rebuild(generation, months)

    def reader(self, ledger=None):
        projection = MemoryConsumerProjection(
            (ledger or self.ledger).reader,
            "i1.synthetic.ledger",
            (self.plan,),
            (ConsumerCodec(self.decoder, native),),
        )
        budget = c.Budget(c.Limits(), lambda: 0)
        with projection.read_view(budget) as view:
            binding = view.binding
        return MarketReader(
            projection,
            binding,
            CursorCodec("i1-test", "test", {"test": b"synthetic-only-" * 3}),
            lambda: self.now,
        )

    def query(self, *, exact=False, page_size=1000, batch=False):
        spec = self.plan.spec
        item = c.Item(
            "single",
            spec.universe.members[0].instrument,
            spec.window.start,
            spec.window.end_exclusive,
            tuple(v.key for v in self.versions) if exact else (),
        )
        return c.Query(
            spec.routes[0].daily.definition,
            (item, replace(item, item_id="second")) if batch else (item,),
            mode="exact_versions" if exact else "as_of",
            page_size=page_size,
            universe_revision=spec.universe.revision,
            semantics=semantics(spec.routes[0].daily),
        )


@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("batch", [False, True])
def test_actual_end_to_end_exact_asof_single_batch_without_read_effects(
    tmp_path, exact, batch
):
    v = Vertical(tmp_path)
    v.finish()
    before = canonical_bytes(v.ledger.checkpoint())
    journal = tuple(v.journal.events(v.plan.spec.identity))
    calls = tuple(v.source.calls)
    result = v.reader().read(v.query(exact=exact, batch=batch))
    assert result["status"] == "OK"
    assert len(result["units"]) == 9 * (2 if batch else 1)
    assert {u["version"]["identity"] for u in result["units"]} == {
        x.key.identity for x in v.versions
    }
    assert {u["classification"] for u in result["units"]} >= {
        "OBSERVED_VALID",
        "MARKET_CLOSED",
    }
    assert all(u["raw"]["volume"] == "0" for u in result["units"] if u["raw"])
    assert "mdf-i1://" not in json.dumps(result)
    assert "journal:" not in json.dumps(result)
    assert canonical_bytes(v.ledger.checkpoint()) == before
    assert tuple(v.journal.events(v.plan.spec.identity)) == journal
    assert tuple(v.source.calls) == calls
    for retained in v.retained:
        assert retained.source_time is None
        assert verify_original(v.ledger, retained.material) == Verification.COMMITTED


def test_empty_responses_need_explicit_no_trade_evidence(tmp_path):
    v = Vertical(tmp_path, no_trade=True)
    v.finish()
    result = v.reader().read(v.query())
    assert result["status"] == "OK"
    assert all(not u["raw"] for u in result["units"])
    assert "NO_TRADE_CONFIRMED" in {u["classification"] for u in result["units"]}
    version = v.versions[0]
    evidence = tuple(
        replace(e, classification=CoverageClass.UNKNOWN)
        if e.classification == CoverageClass.NO_TRADE_CONFIRMED
        else e
        for e in version.assessment.evidence
    )
    with pytest.raises(ContractError):
        prepare_version(
            version.key,
            version.assessment.unit,
            version.assessment.materials,
            evidence,
            v.decoder,
            v.store,
            version.assessment.evaluated_at,
            version.frozen_at,
            plan=v.plan,
        )


def test_unknown_late_found_same_identity_restart_then_consumer(tmp_path):
    v = Vertical(tmp_path)
    v.source.lose_response = True
    assert v.worker.run().state == "UNKNOWN"
    calls = tuple(v.source.calls)
    v.worker = DailyRunner.restore(
        v.plan.spec.identity, v.journal, v.source, sleep=v.sleep
    )
    for shard in v.plan.shards:
        request = v.worker.request(shard, 0)
        if v.journal.attempt(request) is None:
            continue
        before = tuple(v.journal.events(v.plan.spec.identity))
        result = v.worker.reconcile(request)
        assert result.status == LookupStatus.FOUND
        assert tuple(v.journal.events(v.plan.spec.identity)) == before
        accept_original(v.worker, request, result)
        accept_original(v.worker, request, result)
    assert tuple(v.source.calls) == calls
    v.source.lose_response = False
    v.finish()
    assert len(v.source.calls) == len(set(v.source.calls))
    assert v.reader().read(v.query())["status"] == "OK"


@pytest.mark.parametrize("operation", ["version", "publication"])
def test_lost_commit_ack_is_verified_and_same_identity_replay_is_idempotent(
    tmp_path, operation
):
    v = Vertical(tmp_path)
    v.finish()
    if operation == "version":
        value = replace(v.versions[0], key=Key(Kind.VERSION, "new-version"))
        from market_data_foundation.canonical.market_persistence import (
            required_admissions,
        )

        value = replace(value, admission_subjects=required_admissions(value))
        commit = v.ledger.commit_version
    else:
        v.now += timedelta(seconds=1)
        value = v.request(
            v.versions[0], Decision.WITHDRAW, v.publications[0].key, "withdraw"
        )
        commit = v.ledger.publish
    with pytest.raises(CommitUnknown):
        commit(value, Fault.AFTER_COMMIT)
    before = canonical_bytes(v.ledger.checkpoint())
    assert verify_original(v.ledger, value) == Verification.COMMITTED
    commit(value)
    assert canonical_bytes(v.ledger.checkpoint()) == before
    v.ledger.available = False
    assert verify_original(v.ledger, value) == Verification.UNKNOWN


@pytest.mark.parametrize(
    "change", ["withdraw", "restore", "generation", "admission", "candidate"]
)
def test_signed_cursor_fails_on_relevant_changes(tmp_path, change):
    v = Vertical(tmp_path)
    v.finish()
    reader, query = v.reader(), v.query(page_size=1)
    first = reader.read(query)
    token = first["next_cursor"]
    assert reader.read(query, token) == reader.read(query, token)
    v.now += timedelta(seconds=1)
    if change in {"withdraw", "restore"}:
        withdrawal = v.request(
            v.versions[0], Decision.WITHDRAW, v.publications[0].key, "withdraw"
        )
        v.ledger.publish(withdrawal)
        if change == "restore":
            v.now += timedelta(seconds=1)
            v.ledger.publish(
                v.request(v.versions[0], Decision.RESTORE, withdrawal.key, "restore")
            )
    elif change == "generation":
        v.rebuild("g2")
    elif change == "admission":
        v.ledger.set_admission(
            v.versions[0].admission_subjects[0], "synthetic-r2", False
        )
    else:
        v.ledger.commit_version(
            v.prepare(v.plan.units[0], key=Key(Kind.VERSION, "late-candidate"))
        )
    with pytest.raises(
        c.ReadError, match="CURSOR_(SELECTION|QUALIFICATION|GENERATION)_CHANGED"
    ):
        reader.read(query, token)


def test_withdrawal_blocks_historical_and_exact_without_changing_identity(tmp_path):
    v = Vertical(tmp_path)
    v.finish()
    past = v.now
    v.now += timedelta(seconds=1)
    v.ledger.publish(
        v.request(v.versions[0], Decision.WITHDRAW, v.publications[0].key, "withdraw")
    )
    historical = v.reader().read(replace(v.query(), at=past))
    exact = v.reader().read(v.query(exact=True))
    assert historical["items"][0]["status"] == "CURRENT_PUBLICATION_UNAVAILABLE"
    assert exact["items"][0]["status"] == "VERSION_WITHDRAWN"
    assert historical["units"] == exact["units"] == []


def test_backup_handoff_readback_and_read_clock(tmp_path):
    v = Vertical(tmp_path)
    v.finish()
    reader = v.reader()
    query = v.query(page_size=2)
    page = reader.read(query)
    v.now += timedelta(seconds=3)
    second = reader.read(query, page["next_cursor"])
    assert second["first_view_at"] == page["first_view_at"]
    assert second["read_as_of"] == page["read_as_of"]
    restored = v.fresh()
    restored.restore(v.ledger.checkpoint(), "handoff-g2")
    assert (
        v.reader(restored).read(v.query())["units"] == reader.read(v.query())["units"]
    )
    with pytest.raises(c.ReadError, match="CURSOR_GENERATION_CHANGED"):
        v.reader(restored).read(query, page["next_cursor"])
    with pytest.raises(c.ReadError, match="INVALID_AS_OF"):
        reader.read(replace(v.query(), at=v.now + timedelta(seconds=1)))


def test_unknown_request_clock_is_retained_but_cannot_commit_version(tmp_path):
    v = Vertical(tmp_path)
    v.worker.run()
    v.intake()
    old = v.retained[0]
    # A fresh target keeps the original key; changing receipt content in-place fails.
    request = v.worker.report().shards[0].receipts[0].request
    with pytest.raises(ContractError):
        retain_page(v.worker, request, v.layout(request, requested_at=False), v.ledger)
    v.ledger = v.fresh()
    v.retained[0] = retain_page(
        v.worker, request, v.layout(request, requested_at=False), v.ledger
    )
    for retained in v.retained[1:]:
        v.ledger.retain(
            retained.material, v.store.read(retained.material.locator, 1000)
        )
    assert v.retained[0].material.requested_at is None
    assert old.material.obtained_at == v.retained[0].material.obtained_at
    with pytest.raises(ContractError, match="material_acquisition_timing_unverified"):
        v.ledger.commit_version(v.prepare(v.plan.units[0]))


def test_retained_intake_is_exact_named_bytes_and_never_promotes_synthetic_decoder(
    tmp_path,
):
    v = Vertical(tmp_path)
    binding = replace(
        v.source.binding,
        mode=EvidenceMode.RETAINED,
        retained_materials=("named-synthetic-retained-fixture",),
    )
    pages = []
    for shard in v.plan.shards:
        for index in range(shard.route.pages.page_count):
            request = v.worker.request(shard, index)
            page = replace(
                v.source.page(request), retained_reference=binding.retained_materials[0]
            )
            pages.append(
                RetainedPage(
                    request,
                    page,
                    byte_digest(page.raw),
                    byte_digest(page_metadata(page).encode()),
                )
            )
            if page.terminal:
                break
    source = RetainedPageSource(binding, tuple(pages))
    v.worker = DailyRunner(v.plan, v.journal, source, sleep=v.sleep)
    assert v.worker.run().state == "COMPLETE"
    v.intake()
    assert all(r.material.mode == EvidenceMode.RETAINED for r in v.retained)
    assert source.lookup_original(pages[0].request).page == pages[0].page
    with pytest.raises(ContractError, match="synthetic_decoder_only"):
        v.prepare(v.plan.units[0])
    with pytest.raises(ContractError, match="retained_integrity_mismatch"):
        RetainedPageSource(binding, (replace(pages[0], raw_digest="bad"),))
    with pytest.raises(ContractError, match="duplicate_retained_request"):
        RetainedPageSource(binding, (pages[0], pages[0]))


def test_out_of_order_response_is_retained_as_failed_not_published(tmp_path):
    v = Vertical(tmp_path)
    original_page = v.source.page

    def out_of_order(request):
        if request.page_index == 1:
            # Valid row framing, but repeat the earlier day under this request.
            first = original_page(replace(request, page_index=0))
            return replace(first, request_identity=request.identity)
        return original_page(request)

    v.source.page = out_of_order
    assert v.worker.run().state == "BLOCKED"
    v.intake()
    rejected = [r for r in v.retained if r.material.effect == "FAILED"]
    assert rejected
    assert all(r.material.exception_classification == "PAGE_DRIFT" for r in rejected)
    with pytest.raises(ContractError, match="material_effect_unknown"):
        v.prepare(v.plan.units[0])
    assert not v.ledger.checkpoint().versions


@pytest.mark.parametrize("axis", ["source", "universe", "calendar"])
def test_original_identity_never_aliases_other_frozen_scope(tmp_path, axis):
    v = Vertical(tmp_path / "original")
    v.source.lose_response = True
    assert v.worker.run().state == "UNKNOWN"
    request = v.worker.request(v.plan.shards[0], 0)
    spec = job()
    universe = spec.universe
    if axis == "source":
        source = Key(Kind.SOURCE, "synthetic-other-source", "v1")
        universe = replace(
            universe,
            mappings=tuple(replace(m, source=source) for m in universe.mappings),
        )
        spec = replace(
            spec, universe=universe, routes=(replace(spec.routes[0], source=source),)
        )
    elif axis == "universe":
        universe = replace(universe, exclusion_rule="different synthetic exclusion")
        spec = replace(spec, universe=universe)
    else:
        calendar = replace(universe.calendars[0], calendar_id="different-calendar")
        universe = replace(universe, calendars=(calendar,))
        spec = replace(spec, universe=universe)
    other = Vertical(tmp_path / "other", spec=spec)
    other_request = other.worker.request(other.plan.shards[0], 0)
    assert request.identity != other_request.identity
    before = tuple(v.journal.events(v.plan.spec.identity))
    result = Lookup(
        request.identity,
        LookupStatus.FOUND,
        "wrong-original",
        other.source.page(other_request),
    )
    with pytest.raises((ContractError, RuntimeError)):
        accept_original(v.worker, request, result)
    assert tuple(v.journal.events(v.plan.spec.identity)) == before
    with pytest.raises(ContractError):
        v.worker.reconcile(other_request)


@pytest.mark.parametrize("damage", ["raw", "canonical_receipt", "publication_anchor"])
def test_read_corruption_fails_closed_without_repair(tmp_path, damage):
    v = Vertical(tmp_path)
    v.finish()
    if damage == "raw":
        v.store._blobs[v.retained[0].material.locator] = b"corrupt"
    elif damage == "canonical_receipt":
        material = v.retained[0].material
        v.ledger._state.materials[material.key] = replace(material, request="foreign")
    else:
        v.ledger._state.publication_heads.pop(v.versions[0].key)
    before = canonical_bytes(v.ledger.checkpoint())
    calls = tuple(v.source.calls)
    with pytest.raises(c.ReadError, match="INDEX_INCONSISTENT"):
        v.reader().read(v.query())
    assert canonical_bytes(v.ledger.checkpoint()) == before
    assert tuple(v.source.calls) == calls


def test_all_cursor_pages_are_contiguous_and_tampering_has_no_effect(tmp_path):
    v = Vertical(tmp_path)
    v.finish()
    reader, query = v.reader(), v.query(page_size=2, batch=True)
    page = reader.read(query)
    first_token = page["next_cursor"]
    units = list(page["units"])
    while page["next_cursor"]:
        old_end = page["range"][1]
        page = reader.read(query, page["next_cursor"])
        assert page["range"][0] == old_end
        units.extend(page["units"])
    assert len(units) == 18
    assert len({tuple(u["key"]) for u in units}) == 18
    before = canonical_bytes(v.ledger.checkpoint())
    with pytest.raises(c.ReadError):
        reader.read(query, "x" + first_token[1:])
    with pytest.raises(c.ReadError):
        reader.read(replace(query, page_size=1), first_token)
    assert canonical_bytes(v.ledger.checkpoint()) == before


@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("batch", [False, True])
def test_unpublished_and_wrong_universe_are_not_promoted_by_readback(
    tmp_path, exact, batch
):
    v = Vertical(tmp_path)
    v.worker.run()
    v.intake()
    for unit in v.plan.units:
        version = v.prepare(unit)
        v.ledger.commit_version(version)
        v.versions.append(version)
    v.rebuild("unpublished")
    query = v.query(exact=exact, batch=batch)
    denied = v.reader().read(query)
    assert all(item["qualification"] == "UNKNOWN" for item in denied["items"])
    assert all(item["coverage"] != "COMPLETE" for item in denied["items"])
    assert denied["units"] == []
    for version in v.versions:
        for subject in version.admission_subjects:
            v.ledger.set_admission(subject, "synthetic-r1", True)
    unpublished = v.reader().read(query)
    assert all(u["classification"] == "UNKNOWN" for u in unpublished["units"])
    assert all(item["publication"] == "UNPUBLISHED" for item in unpublished["items"])
    assert all(item["coverage"] != "COMPLETE" for item in unpublished["items"])
    wrong = v.reader().read(replace(query, universe_revision="foreign-universe"))
    assert all(
        item["status"] == "UNIVERSE_MEMBERSHIP_MISMATCH" for item in wrong["items"]
    )
    assert wrong["units"] == []


def test_complete_d3_manifest_requires_both_assessment_and_publication(tmp_path):
    v = Vertical(tmp_path)
    v.finish()
    evidence = tuple(e for version in v.versions for e in version.assessment.evidence)
    manifest = v.worker.coverage(evidence)
    verified = v.ledger.coverage_manifest(manifest)
    assert verified.material_readback.value == "COMPLETE"
    assert verified.formal_publication.value == "COMPLETE"
    assert verified.global_status.value == "COMPLETE"
    v.now += timedelta(seconds=1)
    v.ledger.publish(
        v.request(v.versions[0], Decision.WITHDRAW, v.publications[0].key, "withdraw")
    )
    assert v.ledger.coverage_manifest(manifest).global_status.value == "UNKNOWN"


def test_consumer_never_invokes_writer_network_worker_or_subprocess(
    tmp_path, monkeypatch
):
    import socket
    import subprocess

    v = Vertical(tmp_path)
    v.finish()
    reader = v.reader()

    def forbidden(*args, **kwargs):
        raise AssertionError("consumer effect forbidden")

    for method in ("retain", "commit_version", "publish", "set_admission", "rebuild"):
        monkeypatch.setattr(v.ledger, method, forbidden)
    monkeypatch.setattr(v.worker, "run", forbidden)
    monkeypatch.setattr(v.source, "fetch", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert reader.read(v.query())["status"] == "OK"


def test_reader_reserves_material_validation_budget_before_read(tmp_path, monkeypatch):
    v = Vertical(tmp_path)
    v.finish()
    reader = v.reader()
    reader.limits = replace(reader.limits, content=1)
    reads = []

    def forbidden(*args):
        reads.append(args)
        raise AssertionError("over-budget material read")

    monkeypatch.setattr(v.store, "read", forbidden)
    with pytest.raises(c.ReadError, match="READ_CAPACITY_EXCEEDED"):
        reader.read(v.query())
    assert reads == []


def test_same_d2_receipt_replay_and_conflicting_layout(tmp_path):
    v = Vertical(tmp_path)
    v.worker.run()
    v.intake()
    request = v.worker.report().shards[0].receipts[0].request
    before = canonical_bytes(v.ledger.checkpoint())
    result = retain_page(v.worker, request, v.layout(request), v.ledger)
    assert result == v.retained[0]
    assert canonical_bytes(v.ledger.checkpoint()) == before
    layout = v.layout(request)
    with pytest.raises(ContractError, match="d2_row_inventory_mismatch"):
        retain_page(v.worker, request, replace(layout, rows=()), v.ledger)
    assert canonical_bytes(v.ledger.checkpoint()) == before


def test_cross_scope_units_and_namespace_are_rejected_at_consumer(tmp_path):
    v = Vertical(tmp_path)
    v.finish()
    reader = v.reader()
    query = v.query(exact=True, batch=True)
    # The identity string alone is insufficient; namespace is part of the key.
    item = query.items[1]
    foreign = replace(item, instrument=replace(item.instrument, namespace="foreign"))
    from market_data_foundation.canonical.market_projection import MonthKey

    months = tuple(v.ledger.checkpoint().covered_months) + tuple(
        MonthKey(
            u.definition, foreign.instrument, u.window.start.year, u.window.start.month
        )
        for u in v.plan.units
    )
    v.ledger.rebuild("foreign-empty-months", months)
    result = reader.read(replace(query, items=(query.items[0], foreign)))
    assert result["status"] == "PARTIAL_FAILURE"
    assert result["items"][0]["status"] == "VERIFIED"
    assert result["items"][1]["status"] == "UNIVERSE_MEMBERSHIP_MISMATCH"
    assert all(u["item_id"] == "single" for u in result["units"])
    # Do not permit a consumer adapter with a substituted Calendar/Universe plan.
    reader.projection.units = {}
    with pytest.raises(c.ReadError, match="UNREGISTERED_UNIT"):
        reader.read(v.query())


def test_found_replay_accepts_equivalent_utc_fold_and_rejects_changed_instant(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    v = Vertical(tmp_path)
    original = v.source.page
    folded = datetime(2024, 11, 3, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=0)
    v.source.page = lambda request: replace(original(request), source_time=folded)
    v.source.lose_response = True
    v.worker.run()
    request = v.worker.request(v.plan.shards[0], 0)
    result = v.worker.reconcile(request)
    accept_original(v.worker, request, result)
    before = tuple(v.journal.events(v.plan.spec.identity))
    accept_original(v.worker, request, result)
    assert tuple(v.journal.events(v.plan.spec.identity)) == before
    changed = replace(
        result, page=replace(result.page, source_time=folded.replace(fold=1))
    )
    # Python's original same-zone dataclass equality misses this changed instant.
    assert changed.page == result.page
    with pytest.raises(ContractError, match="original_response_conflict"):
        accept_original(v.worker, request, changed)
    assert tuple(v.journal.events(v.plan.spec.identity)) == before


def test_missing_retained_input_stays_unknown_and_manifest_cannot_mix_jobs(tmp_path):
    v = Vertical(tmp_path / "original")
    binding = replace(
        v.source.binding,
        mode=EvidenceMode.RETAINED,
        retained_materials=("named-retained-input",),
    )
    source = RetainedPageSource(binding, ())
    worker = DailyRunner(v.plan, v.journal, source, sleep=v.sleep)
    assert worker.run().state == "UNKNOWN"
    request = worker.request(v.plan.shards[0], 0)
    assert worker.reconcile(request).status == LookupStatus.UNKNOWN
    assert v.journal.receipt(request) is None
    assert v.source.calls == []

    other = Vertical(tmp_path / "other", spec=replace(job(), occurrence="other-job"))
    materials = []
    for vertical in (v, other):
        req = vertical.worker.request(vertical.plan.shards[0], 0)
        page = replace(
            vertical.source.page(req), retained_reference="named-retained-input"
        )
        materials.append(
            RetainedPage(
                req,
                page,
                byte_digest(page.raw),
                byte_digest(page_metadata(page).encode()),
            )
        )
    with pytest.raises(ContractError, match="retained_manifest_job_mismatch"):
        RetainedPageSource(binding, tuple(materials))


def test_denied_version_does_not_contaminate_healthy_item_or_unpublished_sibling(
    tmp_path,
):
    v = Vertical(tmp_path)
    v.finish()
    sibling = v.prepare(v.plan.units[0], key=Key(Kind.VERSION, "unpublished-sibling"))
    v.ledger.commit_version(sibling)
    assert v.reader().read(v.query())["status"] == "OK"
    # Both items share Source/Binding, but only the first Version loses admission.
    subject = "version:" + content_id(v.versions[0].key)
    v.ledger.set_admission(subject, "synthetic-denied-r2", False)
    query = v.query()
    items = tuple(
        c.Item(
            str(index), unit.instrument, unit.window.start, unit.window.end_exclusive
        )
        for index, unit in enumerate(v.plan.units)
    )
    result = v.reader().read(replace(query, items=items))
    assert result["items"][0]["status"] == "QUALIFICATION_FAILED"
    assert result["items"][1]["status"] == "VERIFIED"
    assert {u["item_id"] for u in result["units"]} == {"1"}
