"""Synthetic only: canonical/projection atomicity and hostile offline inputs."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta

import pytest

from market_data_foundation.canonical import market_projection
from market_data_foundation.canonical.market_material import (
    DailyBar,
    FieldSemantics,
    MaterialReceipt,
    MemoryMaterialBytes,
    NormalizationContract,
    RowLocator,
    SyntheticDailyDecoder,
    assess_daily,
    byte_digest,
)
from market_data_foundation.canonical.market_persistence import (
    CommitUnknown,
    Fault,
    InjectedFailure,
    MemoryMarketLedger,
    Verification,
    required_admissions,
)
from market_data_foundation.canonical.market_projection import (
    MonthKey,
    ProjectionError,
    ReadCode,
    SlotKey,
)
from market_data_foundation.canonical.market_publication import (
    MarketPublicationRequest,
    MarketVersion,
)
from market_data_foundation.canonical.model import ContractError, Decision, Key, Kind
from market_data_foundation.domain_contracts.market_coverage import (
    CoverageClass,
    CoverageStatus,
    PointEvidence,
    classify_coverage,
    expected_state,
)
from market_data_foundation.domain_contracts.market_planning import (
    DAILY_DEFINITIONS,
    GridPoint,
    PublicationUnit,
    plan_job,
)
from market_data_foundation.domain_contracts.market_universe import (
    DateWindow,
    EvidenceMode,
    InstrumentIdentity,
    canonical_bytes,
    content_id,
)
from market_data_foundation.qualification.contracts import Capability, Fixture
from tests.r5_i6_market_fixture import job

T0 = datetime(2024, 2, 8, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.value = T0 + timedelta(seconds=10)

    def __call__(self):
        return self.value

    def tick(self):
        self.value += timedelta(seconds=1)
        return self.value


class ControlledBytes(MemoryMaterialBytes):
    def __init__(self):
        super().__init__()
        self.failure = None
        self.reads = 0

    def read(self, locator, maximum):
        self.reads += 1
        if self.failure == "unavailable":
            raise OSError("sensitive-message-must-not-escape")
        if self.failure == "corrupt":
            return b"corrupt"
        return super().read(locator, maximum)


class Scenario:
    def __init__(self, *, index=False):
        self.clock = Clock()
        self.store = ControlledBytes()
        self.daily = DAILY_DEFINITIONS[0]
        self.instrument = InstrumentIdentity("synthetic.ns", "000001")
        self.unit = PublicationUnit(
            self.daily.definition,
            "synthetic-universe-r1",
            "synthetic-calendar-r1",
            self.instrument,
            "Asia/Shanghai",
            DateWindow(date(2024, 1, 1), date(2024, 1, 4)),
        )
        self.source = Key(Kind.SOURCE, "synthetic-source", "r1")
        self.binding = Key(Kind.BINDING, "synthetic-binding", "r1")
        self.contract = NormalizationContract(
            self.source,
            self.binding,
            "mapping-r1",
            self.daily,
            Capability.BARS_DAILY,
            "synthetic.daily",
            "synthetic-upstream-r1",
            "synthetic-json-r2",
            (("period", "daily"), ("kind", "invented-stock")),
            tuple(
                FieldSemantics(
                    name,
                    "decimal-text",
                    unit,
                    name,
                    unit,
                    "EXPLICIT",
                    "synthetic-field-fixture",
                    "identity.synthetic.r1",
                )
                for name, unit in (
                    ("open", "CNY"),
                    ("high", "CNY"),
                    ("low", "CNY"),
                    ("close", "CNY"),
                    ("volume", "shares"),
                    ("amount", "CNY"),
                )
            ),
        )
        self.decoder = SyntheticDailyDecoder(self.contract)
        self.fixture = Fixture(
            "r5-i6-d3-synthetic-only",
            Capability.BARS_DAILY,
            (self.instrument.instrument_id,),
            self.unit.window.start,
            self.unit.window.end_exclusive - timedelta(days=1),
            2,
            {"evidence_mode": "synthetic", "units": "invented-fixture-only"},
        )
        self.ledger = self.fresh()
        self.bars = tuple(
            DailyBar(day, "10", "12", "9", "11", "0", "0")
            for day in (date(2024, 1, 1), date(2024, 1, 3))
        )
        chunks = tuple(canonical_bytes(bar) for bar in self.bars)
        self.raw = b"\n".join(chunks)
        first, second = chunks
        self.receipt = MaterialReceipt(
            Key(Kind.MATERIAL, "synthetic-material"),
            "job",
            "shard",
            "attempt",
            "request",
            self.source,
            self.binding,
            "mapping-r1",
            self.unit.definition,
            self.instrument,
            Capability.BARS_DAILY,
            "synthetic.daily",
            "synthetic-upstream-r1",
            "synthetic-json-r2",
            self.contract.request_fields,
            EvidenceMode.SYNTHETIC,
            byte_digest(self.raw),
            len(self.raw),
            "memory://synthetic-raw",
            ("page-0", "page-1"),
            (
                RowLocator("row-0", "page-0", 0, len(first), byte_digest(first)),
                RowLocator(
                    "row-1", "page-1", len(first) + 1, len(second), byte_digest(second)
                ),
            ),
            T0 - timedelta(seconds=2),
            T0 - timedelta(seconds=1),
            T0,
            fixture_id=self.fixture.fixture_id,
            requested_at=T0 - timedelta(seconds=3),
        )
        self.evidence = tuple(
            PointEvidence(
                GridPoint(self.instrument, day),
                CoverageClass.MARKET_CLOSED
                if day.day == 2
                else CoverageClass.OBSERVED_VALID,
                ("synthetic-calendar-or-row-evidence",),
                ("shard",),
                ("attempt",),
                ("request",),
                None if day.day == 2 else self.receipt.key,
                None if day.day == 2 else ("row-0" if day.day == 1 else "row-1"),
            )
            for day in (date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3))
        )
        self.ledger.retain(self.receipt, self.raw)
        self.assessment = self.assess()
        self.version = self.make_version("v1")
        self.slot = SlotKey(self.unit.definition, self.instrument, date(2024, 1, 2))
        self.month = self.slot.month
        if index:
            self.ledger.rebuild("g1", (self.month,))

    def fresh(self):
        return MemoryMarketLedger(
            "offline.synthetic.ledger", self.store, (self.decoder,), self.clock
        )

    def assess(self, **kwargs):
        args = dict(
            unit=self.unit,
            receipts=(self.receipt,),
            evidence=self.evidence,
            decoder=self.decoder,
            store=self.store,
            evaluated_at=T0,
        )
        args.update(kwargs)
        return assess_daily(**args)

    def make_version(self, name):
        value = MarketVersion(
            Key(Kind.VERSION, name),
            self.assessment,
            T0 + timedelta(seconds=1),
            ("temporary",),
        )
        return replace(value, admission_subjects=required_admissions(value))

    def admit(self, version=None):
        for subject in (version or self.version).admission_subjects:
            self.ledger.set_admission(subject, "synthetic-qualified-r1", True)

    def request(
        self, name="p1", decision=Decision.PUBLISH, predecessor=None, version=None
    ):
        return MarketPublicationRequest(
            Key(Kind.PUBLICATION, name),
            "occurrence-" + name,
            (version or self.version).key,
            decision,
            predecessor,
            ("synthetic-decision-evidence",),
            "synthetic-publication-rule-r1",
        )

    def view(self):
        return self.ledger.reader.open("offline.synthetic.ledger")

    def publish(self):
        self.ledger.commit_version(self.version)
        self.admit()
        request = self.request()
        self.ledger.publish(request)
        return request


@pytest.fixture
def s():
    return Scenario(index=True)


def assert_code(code, function):
    with pytest.raises(ProjectionError) as caught:
        function()
    assert caught.value.code == code


def test_raw_readback_normalized_zero_volume_lineage_and_natural_days(s):
    assert s.assessment.bars == s.bars
    assert s.assessment.materials[0].known_at != s.receipt.obtained_at
    assert s.store.reads >= 2
    s.ledger.commit_version(s.version)
    view = s.view()
    assert view.candidates((s.slot,))[0][0].version == s.version.key
    assert len(view.versions((s.version.key,), exact=True)) == 1
    assert view.histories((s.version.key,)) == ((),)
    assert view.select(s.slot, s.clock()) == ("NO_PUBLISHED_CANDIDATE", ())
    directory = view.month_directory((s.month,))
    assert len(directory) == 31
    assert [entry.count for entry in directory[:4]] == [1, 1, 1, 0]
    assert view.lineage(s.version.key) == (s.receipt,)
    with pytest.raises(FrozenInstanceError):
        s.version.frozen_at = T0


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("open", "NaN", "invalid_daily_number"),
        ("high", "8", "invalid_ohlc"),
        ("volume", "-1", "invalid_daily_number"),
        ("amount", 0.5, "decimal_text_required"),
    ],
)
def test_daily_quality_negative(s, field, value, error):
    with pytest.raises(ContractError, match=error):
        replace(s.bars[0], **{field: value})


@pytest.mark.parametrize(
    "change,error",
    [
        ({"known_at": None}, "material_known_at_unknown_or_future"),
        ({"effect": "UNKNOWN"}, "material_effect_unknown"),
        ({"mapping_revision": "another"}, "receipt_contract_mismatch"),
        ({"request_fields": (("period", "minute"),)}, "receipt_contract_mismatch"),
        (
            {"instrument": InstrumentIdentity("another.ns", "000001")},
            "receipt_instrument_mismatch",
        ),
    ],
)
def test_exact_receipt_binding_and_unknown_time(s, change, error):
    with pytest.raises(ContractError, match=error):
        s.assess(receipts=(replace(s.receipt, **change),))


@pytest.mark.parametrize(
    "change",
    [
        {"requested_at": None},
        {"obtained_at": None},
        {"obtained_at": T0 + timedelta(days=366)},
        {"obtained_at": T0 + timedelta(seconds=2)},
        {"requested_at": T0},
    ],
    ids=[
        "request-unknown",
        "acquisition-unknown",
        "future-year",
        "after-freeze",
        "order",
    ],
)
@pytest.mark.parametrize("boundary", ["version", "publication", "restore"])
def test_unverified_acquisition_timing_cannot_become_publishable(s, change, boundary):
    receipt = replace(
        s.receipt, key=Key(Kind.MATERIAL, "acquisition-clock-case"), **change
    )
    s.ledger.retain(receipt, s.raw)
    evidence = tuple(
        replace(item, material=receipt.key) if item.material else item
        for item in s.evidence
    )
    assessment = s.assess(receipts=(receipt,), evidence=evidence)
    version = replace(s.version, assessment=assessment)
    request = s.request(version=version)
    if boundary != "version":
        # Imported/legacy canonical facts must also pass the publication replay gate.
        s.ledger.commit_version(s.version)
        s.admit()
        if boundary == "restore":
            s.ledger.publish(s.request())
            s.clock.tick()
            withdrawal = s.request("withdraw", Decision.WITHDRAW, s.request().key)
            s.ledger.publish(withdrawal)
            s.clock.tick()
            request = s.request("restore", Decision.RESTORE, withdrawal.key, version)
        s.ledger._state.versions[version.key] = version
    before = s.ledger.checkpoint()
    with pytest.raises(ContractError, match="material_acquisition_timing_unverified"):
        if boundary == "version":
            s.ledger.commit_version(version)
        else:
            s.ledger.publish(request)
    assert s.ledger.checkpoint() == before
    assert receipt in before.materials  # Raw retention preserves UNKNOWN clocks.


def test_acquisition_clock_equality_and_independent_unknown_receipt_clock(s):
    receipt = replace(s.receipt, requested_at=T0, obtained_at=T0, received_at=None)
    ledger = s.fresh()
    ledger.retain(receipt, s.raw)
    assessment = s.assess(receipts=(receipt,))
    version = replace(s.version, assessment=assessment, frozen_at=T0)
    ledger.commit_version(version)
    for subject in version.admission_subjects:
        ledger.set_admission(subject, "synthetic-r1", True)
    ledger.publish(s.request(version=version))
    ledger.rebuild("clock-g1", (s.month,))
    view = ledger.reader.open("offline.synthetic.ledger")
    assert view.content(version.key) == assessment.content
    assert view.lineage(version.key)[0] == receipt


def test_unknown_and_conflicting_economics_retain_raw_but_block_assessment(s):
    for status in ("UNKNOWN", "CONFLICT"):
        contract = replace(
            s.contract,
            fields=(
                replace(s.contract.fields[0], status=status),
                *s.contract.fields[1:],
            ),
        )
        with pytest.raises(ContractError, match="economic_semantics_unqualified"):
            s.assess(decoder=SyntheticDailyDecoder(contract))
    assert s.ledger.checkpoint().materials == (s.receipt,)
    retained = replace(s.receipt, mode=EvidenceMode.RETAINED)
    with pytest.raises(ContractError, match="synthetic_decoder_only"):
        s.assess(receipts=(retained,))


def test_receipt_same_identity_different_bytes_and_row_corruption(s):
    changed = s.raw.replace(b'"10"', b'"11"')
    receipt = replace(s.receipt, digest=byte_digest(changed))
    with pytest.raises(ContractError, match="same_identity_different_content"):
        s.ledger.retain(receipt, changed)
    wrong_row = replace(s.receipt.rows[0], digest="wrong")
    with pytest.raises(ContractError, match="row_digest_mismatch"):
        s.assess(receipts=(replace(s.receipt, rows=(wrong_row, s.receipt.rows[1])),))


def test_lineage_and_coverage_cannot_be_forged(s):
    bad = replace(s.evidence[0], request_identities=("other-request",))
    with pytest.raises(ContractError, match="lineage_request_mismatch"):
        s.assess(evidence=(bad, *s.evidence[1:]))
    with pytest.raises(ContractError, match="coverage_partition_mismatch"):
        s.assess(evidence=s.evidence[:-1])
    bad = replace(s.evidence[0], classification=CoverageClass.UNKNOWN)
    with pytest.raises(ContractError, match="coverage_not_publishable"):
        s.assess(evidence=(bad, *s.evidence[1:]))
    forged = replace(s.assessment, bars=(replace(s.bars[0], close="10"), s.bars[1]))
    with pytest.raises(ContractError, match="assessment_replay_mismatch"):
        s.ledger.commit_version(replace(s.version, assessment=forged))


@pytest.mark.parametrize("failure", ["corrupt", "unavailable"])
def test_independent_readback_failure_prevents_canonical_material(s, failure):
    fresh = s.fresh()
    s.store.failure = failure
    with pytest.raises((ContractError, OSError)):
        fresh.retain(s.receipt, s.raw)
    assert fresh.checkpoint().materials == ()
    assert fresh.checkpoint().versions == ()


@pytest.mark.parametrize("operation", ["version", "publication"])
@pytest.mark.parametrize(
    "fault", [Fault.AFTER_CANONICAL, Fault.AFTER_PROJECTION, Fault.BEFORE_COMMIT]
)
def test_canonical_projection_transaction_rolls_back(s, operation, fault):
    if operation == "publication":
        s.ledger.commit_version(s.version)
        s.admit()
        value, commit = s.request(), s.ledger.publish
    else:
        value, commit = s.version, s.ledger.commit_version
    before = s.ledger.checkpoint()
    before_state = repr(s.ledger._state)
    with pytest.raises(InjectedFailure):
        commit(value, fault)
    assert s.ledger.checkpoint() == before
    assert repr(s.ledger._state) == before_state  # Includes canonical heads.
    assert (
        s.ledger.verify(operation, content_id(value.key), content_id(value))
        == Verification.ABSENT
    )
    if operation == "version":
        assert s.view().candidates((s.slot,)) == ((),)
    else:
        assert s.view().histories((s.version.key,)) == ((),)
        assert s.view().version_guards((s.version.key,))[0].change == 0


@pytest.mark.parametrize("operation", ["version", "publication"])
def test_ack_loss_reconcile_idempotence_and_conflict(s, operation):
    if operation == "publication":
        s.ledger.commit_version(s.version)
        s.admit()
        value, commit = s.request(), s.ledger.publish
    else:
        value, commit = s.version, s.ledger.commit_version
    with pytest.raises(CommitUnknown):
        commit(value, Fault.AFTER_COMMIT)
    before = s.ledger.checkpoint()
    before_state = repr(s.ledger._state)
    assert (
        s.ledger.verify(operation, content_id(value.key), content_id(value))
        == Verification.COMMITTED
    )
    assert (
        s.ledger.verify(operation, content_id(value.key), "different")
        == Verification.CONFLICT
    )
    s.ledger.available = False
    assert (
        s.ledger.verify(operation, content_id(value.key), content_id(value))
        == Verification.UNKNOWN
    )
    s.ledger.available = True
    for _ in range(3):
        commit(value)
    assert s.ledger.checkpoint() == before
    assert repr(s.ledger._state) == before_state
    changed = (
        replace(value, rule_revision="different")
        if operation == "publication"
        else replace(value, frozen_at=T0 + timedelta(seconds=2))
    )
    with pytest.raises(ContractError, match="same_identity_different_content"):
        commit(changed)


def test_concurrent_replays_and_overlapping_versions_preserve_all_candidates(s):
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = tuple(pool.map(s.ledger.commit_version, (s.version,) * 2))
    assert receipts[0] == receipts[1]
    other = s.make_version("v2")
    third = s.make_version("v3")
    with ThreadPoolExecutor(max_workers=2) as pool:
        tuple(pool.map(s.ledger.commit_version, (other, third)))
    assert s.view().month_directory((s.month,))[1].count == 3
    assert {item.version for item in s.view().candidates((s.slot,))[0]} == {
        s.version.key,
        other.key,
        third.key,
    }
    s.admit()
    request = s.request()
    with ThreadPoolExecutor(max_workers=2) as pool:
        tuple(pool.map(s.ledger.publish, (request,) * 2))
    assert s.view().version_guards((s.version.key,))[0].change == 1
    with pytest.raises(ContractError, match="publication_occurrence_conflict"):
        s.ledger.publish(replace(request, key=Key(Kind.PUBLICATION, "other-key")))


def test_multiple_published_versions_exact_each_readable_asof_ambiguous(s):
    s.publish()
    other = s.make_version("v2")
    s.ledger.commit_version(other)
    s.admit(other)
    s.ledger.publish(s.request("p2", version=other))
    assert not s.ledger.replacement_groups_supported
    status, keys = s.view().select(s.slot, s.clock())
    assert status == "AMBIGUOUS" and set(keys) == {s.version.key, other.key}
    for key in keys:
        view = s.view()
        assert view.versions((key,), exact=True)[0].version == key
        assert view.content(key) == s.assessment.content
    with pytest.raises(ContractError, match="replacement_groups_unsupported"):
        replace(s.request(), decision=Decision.SUPERSEDE)


def test_withdraw_restore_authority_time_and_current_guard(s):
    published = s.publish()
    as_of = s.clock()
    old_view = s.view()
    before = old_view.version_guards((s.version.key,))[0]
    s.clock.tick()
    withdrawn = s.request("withdraw", Decision.WITHDRAW, published.key)
    s.ledger.publish(withdrawn)
    assert_code(ReadCode.CHANGED, lambda: s.view().select(s.slot, as_of))
    # An already opened view is a repeatable read of the preceding committed state.
    assert old_view.content(s.version.key) == s.assessment.content
    s.clock.tick()
    restored = s.request("restore", Decision.RESTORE, withdrawn.key)
    s.ledger.publish(restored)
    after = s.view().version_guards((s.version.key,))[0]
    assert after.change == before.change + 2 and after.count == 3
    assert s.view().select(s.slot, as_of)[0] == "SELECTED"
    history = s.view().histories((s.version.key,))[0]
    assert [event.decision_at for event in history] == [
        as_of,
        as_of + timedelta(seconds=1),
        as_of + timedelta(seconds=2),
    ]
    with pytest.raises(ContractError, match="publication_predecessor_mismatch"):
        s.ledger.publish(s.request("wrong-withdraw", Decision.WITHDRAW, published.key))


def test_admission_default_denied_revocation_and_readback_rechecked(s):
    s.ledger.commit_version(s.version)
    with pytest.raises(ContractError, match="publication_admission_denied"):
        s.ledger.publish(s.request())
    s.admit()
    s.ledger.publish(s.request())
    subject = s.version.admission_subjects[0]
    before = s.view().admission_guards(s.version.key)
    s.ledger.set_admission(subject, "revoked-r2", False)
    assert_code(ReadCode.ADMISSION, lambda: s.view().content(s.version.key))
    assert s.view().admission_guards(s.version.key) != before
    s.ledger.set_admission(subject, "requalified-r3", True)
    s.store.failure = "unavailable"
    assert_code(ReadCode.UNAVAILABLE, lambda: s.view().content(s.version.key))
    s.clock.tick()
    s.ledger.publish(s.request("withdraw", Decision.WITHDRAW, s.request().key))
    s.clock.tick()
    with pytest.raises(ContractError, match="material_read_unavailable"):
        s.ledger.publish(
            s.request("restore", Decision.RESTORE, Key(Kind.PUBLICATION, "withdraw"))
        )


def test_effective_and_usable_time_and_no_backdated_decisions(s):
    s.ledger.commit_version(s.version)
    s.admit()
    future = s.clock() + timedelta(seconds=5)
    s.ledger.publish(replace(s.request(), effective_at=future, usable_at=future))
    assert s.view().select(s.slot, s.clock())[0] == "NO_PUBLISHED_CANDIDATE"
    s.clock.value = future
    assert s.view().select(s.slot, future)[0] == "SELECTED"
    s.clock.tick()
    bad = replace(s.request("w", Decision.WITHDRAW, s.request().key), usable_at=T0)
    with pytest.raises(ContractError, match="publication_time_backdated"):
        s.ledger.publish(bad)


def test_uncovered_is_not_zero_and_binding_generation_fail_closed(s):
    other = replace(s.month, instrument=InstrumentIdentity("other", "000001"))
    assert_code(ReadCode.NOT_COVERED, lambda: s.view().month_directory((other,)))
    assert_code(ReadCode.BINDING, lambda: s.ledger.reader.open("wrong"))
    assert_code(
        ReadCode.GENERATION,
        lambda: s.ledger.reader.open("offline.synthetic.ledger", "old"),
    )
    before = s.ledger.checkpoint()
    s.view().candidates((s.slot,))
    assert s.ledger.checkpoint() == before
    assert not hasattr(s.ledger.reader, "commit_version")
    s.ledger.suspend()
    assert_code(ReadCode.NOT_READY, s.view)


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_candidate",
        "extra_candidate",
        "count",
        "reverse",
        "length",
        "missing_history",
        "predecessor",
    ],
)
def test_index_faults_are_detected_without_reader_repairs(s, corruption):
    s.publish()
    index = s.ledger._state.index  # Deliberate forbidden-path fault injection only.
    if corruption == "missing_candidate":
        index.slots[s.slot] = ()
    elif corruption == "extra_candidate":
        index.slots[s.slot] *= 2
    elif corruption == "count":
        index.directory[s.slot] = replace(index.directory[s.slot], count=999)
    elif corruption == "reverse":
        index.reverse[s.version.key] = ()
    elif corruption == "length":
        index.versions[s.version.key] = replace(
            index.versions[s.version.key], content_length=999
        )
    elif corruption == "missing_history":
        index.histories[s.version.key] = ()
    else:
        original = index.histories[s.version.key][0]
        bad = replace(
            original, request=replace(original.request, predecessor=original.key)
        )
        index.histories[s.version.key] = (bad,)
        index.guards[s.version.key] = replace(
            index.guards[s.version.key], history_digest=content_id((bad,))
        )
        s.ledger._state.publications[bad.key] = bad
    before = repr(index)
    assert_code(ReadCode.INCONSISTENT, lambda: s.view().select(s.slot, s.clock()))
    assert repr(index) == before


def test_candidate_65_retained_asof_capacity_exact_still_possible(s):
    for number in range(65):
        s.ledger.commit_version(s.make_version(f"v{number}"))
    assert len(s.ledger.checkpoint().versions) == 65
    assert len(s.ledger._state.index.slots[s.slot]) == 65
    assert s.view().month_directory((s.month,))[1].overflow
    assert_code(ReadCode.CAPACITY, lambda: s.view().candidates((s.slot,)))
    version = s.make_version("v1")
    s.admit(version)
    s.ledger.publish(s.request(version=version))
    assert s.view().versions((version.key,), exact=True)[0].version == version.key
    assert s.view().content(version.key) == s.assessment.content


def write_publication_history(s, count):
    s.ledger.commit_version(s.version)
    s.admit()
    predecessor = None
    for number in range(count):
        decision = (
            Decision.PUBLISH
            if number == 0
            else Decision.WITHDRAW
            if number % 2
            else Decision.RESTORE
        )
        request = s.request(f"event-{number}", decision, predecessor)
        s.ledger.publish(request)
        predecessor = request.key
        s.clock.tick()


@pytest.mark.parametrize("method", ["histories", "select", "content", "version_guards"])
@pytest.mark.parametrize("prefix_count", [1, 3])
def test_coherent_stale_history_and_guard_cannot_hide_canonical_withdrawal(
    s, method, prefix_count
):
    published = s.publish()
    key = s.version.key
    predecessor = published.key
    for number in range(1, prefix_count):
        s.clock.tick()
        request = s.request(
            f"prefix-{number}",
            Decision.WITHDRAW if number % 2 else Decision.RESTORE,
            predecessor,
        )
        s.ledger.publish(request)
        predecessor = request.key
    old_history = s.ledger._state.index.histories[key]
    old_guard = s.ledger._state.index.guards[key]
    s.clock.tick()
    s.ledger.publish(s.request("withdraw", Decision.WITHDRAW, predecessor))
    assert_code(ReadCode.CHANGED, lambda: s.view().content(key))
    canonical = s.ledger.checkpoint()
    assert len(canonical.publications) == prefix_count + 1
    # Only the two projection tables are replaced; canonical facts stay intact.
    index = s.ledger._state.index
    index.histories[key] = old_history
    index.guards[key] = old_guard
    before = repr(s.ledger._state)
    reads = s.store.reads
    view = s.view()
    read = (
        (lambda: view.select(s.slot, s.clock()))
        if method == "select"
        else (lambda: view.content(key))
        if method == "content"
        else (lambda: getattr(view, method)((key,)))
    )
    assert_code(ReadCode.INCONSISTENT, read)
    assert s.ledger.checkpoint() == canonical
    assert repr(s.ledger._state) == before
    assert s.store.reads == reads


@pytest.mark.parametrize("method", ["histories", "select", "content", "version_guards"])
def test_canonical_publication_anchor_reads_use_only_bounded_point_lookups(s, method):
    write_publication_history(s, 3)

    class PointLookupOnly(dict):
        lookups = 0

        def __iter__(self):
            pytest.fail("read port enumerated canonical Publications")

        def values(self):
            pytest.fail("read port scanned canonical Publications")

        def items(self):
            pytest.fail("read port scanned canonical Publications")

        def get(self, key, default=None):
            self.lookups += 1
            return super().get(key, default)

    canonical = s.ledger._state.publications
    points = PointLookupOnly(canonical)
    s.ledger._state.publications = points
    view = s.view()
    if method == "select":
        assert view.select(s.slot, s.clock()) == ("SELECTED", (s.version.key,))
    elif method == "content":
        assert view.content(s.version.key) == s.assessment.content
    elif method == "histories":
        assert view.histories((s.version.key,)) == (tuple(canonical.values()),)
    else:
        assert view.version_guards((s.version.key,))[0].count == 3
    # select revalidates at most twice (history selection + content gate).
    assert 1 <= points.lookups <= 2 * (3 + 1)


@pytest.mark.parametrize("count", [0, 2, 3, 65])
@pytest.mark.parametrize("operation", ["rebuild", "restore"])
def test_recovery_reconstructs_heads_from_complete_canonical_facts(s, count, operation):
    write_publication_history(s, count)
    backup = s.ledger.checkpoint()
    key = s.version.key
    if operation == "rebuild":
        # Explicit recovery may replace broken projection and canonical heads.
        s.ledger._state.index.histories[key] = ()
        s.ledger._state.publication_heads.clear()
        ledger = s.ledger
        ledger.rebuild("head-rebuilt", (s.month,))
    else:
        ledger = s.fresh()
        ledger.restore(backup, "head-restored")
    anchor = ledger._state.publication_heads[key]
    assert anchor.version == key
    assert anchor.count == count
    assert anchor.head == (backup.publications[-1].key if count else None)
    assert anchor.history_digest == content_id(backup.publications)
    assert ledger.checkpoint().publications == backup.publications
    assert ledger.checkpoint().versions == backup.versions
    for receipt in backup.receipts:
        assert ledger._state.receipts[(receipt.operation, receipt.identity)] == receipt
    view = ledger.reader.open("offline.synthetic.ledger")
    assert view.version_guards((key,))[0].count == count
    if count > 64:
        assert_code(ReadCode.CAPACITY, lambda: view.histories((key,)))
        assert_code(ReadCode.CAPACITY, lambda: view.content(key))
    else:
        assert view.histories((key,)) == (backup.publications,)
        if count % 2:
            assert view.content(key) == s.assessment.content
        else:
            assert_code(ReadCode.CHANGED, lambda: view.content(key))


@pytest.mark.parametrize("operation", ["rebuild", "restore"])
def test_recovery_cannot_drop_canonical_suffix_with_committed_receipt(s, operation):
    write_publication_history(s, 2)
    backup = s.ledger.checkpoint()
    if operation == "restore":
        ledger = s.fresh()
        backup = replace(backup, publications=backup.publications[:-1])
    else:
        ledger = s.ledger
        del ledger._state.publications[backup.publications[-1].key]
    before = repr(ledger._state)
    with pytest.raises(ContractError, match="recovery_receipt_orphan"):
        if operation == "restore":
            ledger.restore(backup, "head-rejected")
        else:
            ledger.rebuild("head-rejected", (s.month,))
    assert repr(ledger._state) == before


@pytest.mark.parametrize("fault", list(Fault))
def test_withdrawal_canonical_head_is_atomic_and_ack_replay_does_not_advance(s, fault):
    published = s.publish()
    key = s.version.key
    old_head = s.ledger._state.publication_heads[key]
    s.clock.tick()
    request = s.request("withdraw-head", Decision.WITHDRAW, published.key)
    before = repr(s.ledger._state)
    if fault == Fault.AFTER_COMMIT:
        with pytest.raises(CommitUnknown):
            s.ledger.publish(request, fault)
        head = s.ledger._state.publication_heads[key]
        assert head.count == old_head.count + 1 and head.head == request.key
        assert head.history_digest == content_id(s.ledger.checkpoint().publications)
        committed = repr(s.ledger._state)
        s.ledger.publish(request)
        assert repr(s.ledger._state) == committed
        assert_code(ReadCode.CHANGED, lambda: s.view().content(key))
    else:
        with pytest.raises(InjectedFailure):
            s.ledger.publish(request, fault)
        assert repr(s.ledger._state) == before
        assert s.ledger._state.publication_heads[key] == old_head
        assert s.view().content(key) == s.assessment.content


@pytest.mark.parametrize("corruption", ["missing", "identity", "head", "digest"])
def test_canonical_head_defects_fail_closed_without_repair(s, corruption):
    s.publish()
    key = s.version.key
    heads = s.ledger._state.publication_heads
    head = heads[key]
    if corruption == "missing":
        del heads[key]
    elif corruption == "identity":
        heads[key] = replace(head, version=Key(Kind.VERSION, "wrong"))
    elif corruption == "head":
        heads[key] = replace(head, head=Key(Kind.PUBLICATION, "absent"))
    else:
        heads[key] = replace(head, history_digest="wrong")
    before = repr(s.ledger._state)
    assert_code(ReadCode.INCONSISTENT, lambda: s.view().histories((key,)))
    assert_code(ReadCode.INCONSISTENT, lambda: s.view().version_guards((key,)))
    assert_code(ReadCode.INCONSISTENT, lambda: s.view().content(key))
    assert repr(s.ledger._state) == before


def test_publication_writer_cannot_extend_coherent_stale_projection(s):
    published = s.publish()
    key = s.version.key
    history = s.ledger._state.index.histories[key]
    guard = s.ledger._state.index.guards[key]
    s.clock.tick()
    s.ledger.publish(s.request("canonical-withdraw", Decision.WITHDRAW, published.key))
    s.ledger._state.index.histories[key] = history
    s.ledger._state.index.guards[key] = guard
    before = repr(s.ledger._state)
    s.clock.tick()
    with pytest.raises(ContractError, match="publication_head_mismatch"):
        s.ledger.publish(s.request("second-withdraw", Decision.WITHDRAW, published.key))
    assert repr(s.ledger._state) == before


def forbid_history_access_and_serialization(s, monkeypatch):
    class ForbiddenHistories(dict):
        def get(self, *args, **kwargs):
            pytest.fail("history accessed before capacity/guard preflight")

    def forbidden_digest(value):
        pytest.fail("history serialized before capacity/guard preflight")

    monkeypatch.setattr(s.ledger._state.index, "histories", ForbiddenHistories())
    monkeypatch.setattr(market_projection, "content_id", forbidden_digest)


@pytest.mark.parametrize("count", [2, 65])
def test_stale_guard_preflight_uses_canonical_count_before_history_access(
    s, monkeypatch, count
):
    write_publication_history(s, count)
    key = s.version.key
    index = s.ledger._state.index
    index.guards[key] = replace(
        index.guards[key],
        count=1,
        change=1,
        history_digest=content_id(index.histories[key][:1]),
    )
    forbid_history_access_and_serialization(s, monkeypatch)
    assert_code(ReadCode.INCONSISTENT, lambda: s.view().histories((key,)))


def test_publication_history_65_retained_and_read_capacity(s, monkeypatch):
    write_publication_history(s, 65)
    assert len(s.ledger.checkpoint().publications) == 65
    assert s.view().version_guards((s.version.key,))[0].count == 65
    forbid_history_access_and_serialization(s, monkeypatch)
    assert_code(ReadCode.CAPACITY, lambda: s.view().histories((s.version.key,)))


def test_history_actual_count_is_checked_before_serialization(s, monkeypatch):
    write_publication_history(s, 65)
    index = s.ledger._state.index
    index.guards[s.version.key] = replace(
        index.guards[s.version.key], count=64, change=64
    )

    def forbidden_digest(value):
        pytest.fail("history serialized before actual count consistency check")

    monkeypatch.setattr(market_projection, "content_id", forbidden_digest)
    assert_code(ReadCode.INCONSISTENT, lambda: s.view().histories((s.version.key,)))


@pytest.mark.parametrize("previous_reads", [0, 32])
def test_full_history_budget_preflight_precedes_any_history_access(
    s, monkeypatch, previous_reads
):
    write_publication_history(s, 64)
    assert len(s.view().histories((s.version.key,))[0]) == 64
    view = s.view()
    for _ in range(previous_reads):
        assert len(view.histories((s.version.key,))[0]) == 64
    assert view._budget.events == previous_reads * 64
    forbid_history_access_and_serialization(s, monkeypatch)
    # Both a single over-budget batch and exhaustion across calls must preflight.
    keys = (s.version.key,) * (33 - previous_reads)
    assert_code(ReadCode.CAPACITY, lambda: view.histories(keys))


@pytest.mark.parametrize(
    "corruption", ["identity", "negative-count", "noninteger-count", "change"]
)
def test_history_guard_validation_precedes_history_access(s, monkeypatch, corruption):
    s.publish()
    index = s.ledger._state.index
    guard = index.guards[s.version.key]
    changes = {
        "identity": dict(version=Key(Kind.VERSION, "other")),
        "negative-count": dict(count=-1, change=-1),
        "noninteger-count": dict(count=1.0),
        "change": dict(change=999),
    }
    index.guards[s.version.key] = replace(guard, **changes[corruption])
    forbid_history_access_and_serialization(s, monkeypatch)
    assert_code(ReadCode.INCONSISTENT, lambda: s.view().histories((s.version.key,)))


def test_cumulative_budgets_not_reset_between_calls(s):
    view = s.view()
    view.candidates((s.slot,) * 8192)
    assert_code(ReadCode.CAPACITY, lambda: view.candidates((s.slot,)))
    s.publish()
    view = s.view()
    view._budget.content = 8 * 1024 * 1024
    assert_code(ReadCode.CAPACITY, lambda: view.content(s.version.key))
    view = s.view()
    view._budget.metadata = 16 * 1024 * 1024
    assert_code(ReadCode.CAPACITY, lambda: view.month_directory((s.month,)))


def test_rebuild_failure_no_half_switch_and_generation_invalidates(s):
    s.publish()
    before = s.ledger.checkpoint()
    with pytest.raises(InjectedFailure):
        s.ledger.rebuild("g2", (s.month,), Fault.AFTER_PROJECTION)
    assert s.ledger.checkpoint() == before
    assert s.view().generation.generation == "g1"
    s.ledger.rebuild("g2", (s.month,))
    assert_code(
        ReadCode.GENERATION,
        lambda: s.ledger.reader.open("offline.synthetic.ledger", "g1"),
    )
    assert s.view().content(s.version.key) == s.assessment.content


def test_exact_recovery_preserves_time_and_receipts_and_rebuilds_guards(s):
    s.publish()
    backup = s.ledger.checkpoint()
    restored = s.fresh()
    with pytest.raises(InjectedFailure):
        restored.restore(backup, "recovered", Fault.AFTER_PROJECTION)
    assert restored.checkpoint().versions == ()
    assert_code(
        ReadCode.NOT_READY, lambda: restored.reader.open("offline.synthetic.ledger")
    )
    restored.restore(backup, "recovered")
    view = restored.reader.open("offline.synthetic.ledger", "recovered")
    assert view.histories((s.version.key,))[0] == backup.publications
    assert view.content(s.version.key) == s.assessment.content
    assert view.version_guards((s.version.key,))[0].change == 1
    assert (
        restored.verify(
            "publication", content_id(s.request().key), content_id(s.request())
        )
        == Verification.COMMITTED
    )


@pytest.mark.parametrize("imported", [False, True])
def test_rebuild_cannot_reuse_restore_generation(s, imported):
    s.publish()
    ledger = s.fresh()
    ledger.restore(s.ledger.checkpoint(), "restore-g")
    ledger.rebuild("next-g", (s.month,))
    current_generation = "next-g"
    if imported:
        backup = ledger.checkpoint()
        ledger = s.fresh()
        ledger.restore(backup, "imported-g")
        current_generation = "imported-g"
    before = ledger.checkpoint()
    assert_code(
        ReadCode.GENERATION,
        lambda: ledger.reader.open("offline.synthetic.ledger", "restore-g"),
    )

    with pytest.raises(ContractError, match="new_generation_required"):
        ledger.rebuild("restore-g", (s.month,))

    assert ledger.checkpoint() == before
    assert_code(
        ReadCode.GENERATION,
        lambda: ledger.reader.open("offline.synthetic.ledger", "restore-g"),
    )
    view = ledger.reader.open("offline.synthetic.ledger", current_generation)
    assert view.content(s.version.key) == s.assessment.content
    assert view.histories((s.version.key,))[0] == before.publications


@pytest.mark.parametrize("operation", ["rebuild", "restore"])
@pytest.mark.parametrize("imported", [False, True])
def test_generation_operation_replay_never_reactivates_old_generation(
    s, operation, imported
):
    s.publish()
    backup = s.ledger.checkpoint()
    if operation == "restore":
        ledger = s.fresh()
        payload = backup
    else:
        ledger = s.ledger
        payload = ("original-g", (s.month,))

    def replay():
        if operation == "restore":
            return ledger.restore(backup, "original-g")
        return ledger.rebuild("original-g", (s.month,))

    receipt = replay()
    ledger.rebuild("next-g", (s.month,))
    current_generation = "next-g"
    if imported:
        recovery_backup = ledger.checkpoint()
        ledger = s.fresh()
        ledger.restore(recovery_backup, "imported-g")
        current_generation = "imported-g"
    before = ledger.checkpoint()

    assert replay() == receipt
    assert (
        ledger.verify(operation, "original-g", content_id(payload))
        == Verification.COMMITTED
    )
    assert ledger.checkpoint() == before
    assert_code(
        ReadCode.GENERATION,
        lambda: ledger.reader.open("offline.synthetic.ledger", "original-g"),
    )
    assert (
        ledger.reader.open("offline.synthetic.ledger", current_generation).content(
            s.version.key
        )
        == s.assessment.content
    )


@pytest.mark.parametrize("previous_generation", ["g1", "restore-g"])
def test_restore_rejects_historical_generation_from_either_operation(
    s, previous_generation
):
    s.publish()
    ledger = s.fresh()
    ledger.restore(s.ledger.checkpoint(), "restore-g")
    ledger.rebuild("next-g", (s.month,))
    backup = ledger.checkpoint()
    fresh = s.fresh()
    before = fresh.checkpoint()

    with pytest.raises(ContractError, match="new_generation_required"):
        fresh.restore(backup, previous_generation)

    assert fresh.checkpoint() == before
    assert_code(
        ReadCode.NOT_READY, lambda: fresh.reader.open("offline.synthetic.ledger")
    )
    assert_code(
        ReadCode.GENERATION,
        lambda: fresh.reader.open("offline.synthetic.ledger", previous_generation),
    )


@pytest.mark.parametrize(
    "defect",
    [
        "material_missing",
        "receipt_missing",
        "version_orphan",
        "duplicate",
        "wrong_binding",
        "corrupt_bytes",
    ],
)
def test_unqualified_recovery_cannot_become_ready(s, defect):
    s.publish()
    backup = s.ledger.checkpoint()
    if defect == "material_missing":
        backup = replace(backup, materials=())
    elif defect == "receipt_missing":
        backup = replace(backup, receipts=())
    elif defect == "version_orphan":
        backup = replace(backup, versions=())
    elif defect == "duplicate":
        backup = replace(backup, publications=backup.publications * 2)
    elif defect == "wrong_binding":
        backup = replace(backup, ledger_binding="wrong")
    else:
        s.store.failure = "corrupt"
    restored = s.fresh()
    with pytest.raises(ContractError):
        restored.restore(backup, "bad-recovery")
    assert restored.checkpoint().versions == ()
    assert_code(
        ReadCode.NOT_READY, lambda: restored.reader.open("offline.synthetic.ledger")
    )


def test_duplicate_or_unaccounted_raw_rows_never_disappear(s):
    duplicate = replace(s.receipt.rows[0], identity="another-row")
    receipt = replace(s.receipt, rows=(*s.receipt.rows, duplicate))
    with pytest.raises(ContractError, match="material_row_inventory_mismatch"):
        s.assess(receipts=(receipt,))
    closed = replace(s.evidence[0], classification=CoverageClass.MARKET_CLOSED)
    with pytest.raises(ContractError, match="unaccounted_source_row"):
        s.assess(evidence=(closed, *s.evidence[1:]))
    with pytest.raises(ContractError, match="decimal_text_required"):
        replace(s.bars[0], open=None)


@pytest.mark.parametrize("hidden", ["duplicate", "unique", "outside-window"])
@pytest.mark.parametrize("declare_hidden_page", [False, True])
def test_omitted_raw_row_blocks_assessment_and_version_commit(
    s, hidden, declare_hidden_page
):
    first, last = (canonical_bytes(bar) for bar in s.bars)
    middle = canonical_bytes(
        s.bars[0]
        if hidden == "duplicate"
        else replace(
            s.bars[0],
            day=date(2024, 1, 2) if hidden == "unique" else date(2023, 12, 31),
        )
    )
    raw = b"\n".join((first, middle, last))
    receipt = replace(
        s.receipt,
        key=Key(Kind.MATERIAL, "omitted-row"),
        locator="memory://omitted-row",
        digest=byte_digest(raw),
        byte_length=len(raw),
        pages=("page-0", "hidden-page", "page-1")
        if declare_hidden_page
        else s.receipt.pages,
        rows=(
            s.receipt.rows[0],
            replace(s.receipt.rows[1], offset=len(first) + len(middle) + 2),
        ),
    )
    evidence = tuple(
        replace(item, material=receipt.key) if item.material else item
        for item in s.evidence
    )
    s.ledger.retain(receipt, raw)
    before = s.ledger.checkpoint()
    error = (
        "material_row_inventory_mismatch"
        if declare_hidden_page
        else "material_page_framing_mismatch"
    )
    with pytest.raises(ContractError, match=error):
        s.assess(receipts=(receipt,), evidence=evidence)
    # A caller cannot bypass independent enumeration by supplying an assessment.
    assessment = replace(s.assessment, materials=(receipt,), evidence=evidence)
    version = replace(s.version, assessment=assessment)
    with pytest.raises(ContractError, match=error):
        s.ledger.commit_version(version)
    assert s.ledger.checkpoint() == before
    assert s.view().candidates((s.slot,)) == ((),)
    assert (
        s.ledger.verify("version", content_id(version.key), content_id(version))
        == Verification.ABSENT
    )
    with pytest.raises(ContractError, match="publication_version_missing"):
        s.ledger.publish(s.request(version=version))
    assert s.ledger.checkpoint() == before


def test_full_duplicate_inventory_is_not_normalized_away(s):
    first = canonical_bytes(s.bars[0])
    raw = s.raw + b"\n" + first
    receipt = replace(
        s.receipt,
        locator="memory://complete-duplicate",
        digest=byte_digest(raw),
        byte_length=len(raw),
        pages=(*s.receipt.pages, "page-2"),
        rows=(
            *s.receipt.rows,
            RowLocator(
                "row-2", "page-2", len(s.raw) + 1, len(first), byte_digest(first)
            ),
        ),
    )
    s.store.put(receipt.locator, raw)
    with pytest.raises(ContractError, match="duplicate_daily_source_row"):
        s.assess(receipts=(receipt,))


@pytest.mark.parametrize("framing", ["leading-lf", "trailing-lf", "empty-page", "junk"])
def test_decoder_checks_all_page_framing_including_unlocated_bytes(s, framing):
    raw = {
        "leading-lf": b"\n" + s.raw,
        "trailing-lf": s.raw + b"\n",
        "empty-page": s.raw.replace(b"\n", b"\n\n"),
        "junk": s.raw + b"\nnot-json",
    }[framing]
    # No locators: framing must still be checked instead of trusting an empty list.
    receipt = replace(
        s.receipt,
        locator="memory://bad-framing",
        rows=(),
        pages=("page-0", "page-1", "page-2"),
        digest=byte_digest(raw),
        byte_length=len(raw),
    )
    s.store.put(receipt.locator, raw)
    with pytest.raises(
        ContractError, match="material_page_framing_mismatch|invalid_daily_row_encoding"
    ):
        s.assess(receipts=(receipt,))


@pytest.mark.parametrize("defect", ["empty-inventory", "page-label", "reordered"])
def test_physical_inventory_must_match_every_receipt_locator(s, defect):
    rows = {
        "empty-inventory": (),
        "page-label": (
            replace(s.receipt.rows[0], page_identity="page-1"),
            s.receipt.rows[1],
        ),
        "reordered": s.receipt.rows[::-1],
    }[defect]
    with pytest.raises(ContractError, match="material_row_inventory_mismatch"):
        s.assess(receipts=(replace(s.receipt, rows=rows),))


def test_metadata_does_not_expose_content_or_allow_guard_bypass(s):
    s.ledger.commit_version(s.version)
    metadata = s.view().versions((s.version.key,))[0]
    assert metadata.version == s.version.key
    assert metadata.normalization == s.contract
    assert metadata.assessment_digest == s.assessment.digest
    assert not hasattr(metadata, "assessment")
    assert not hasattr(metadata, "bars")
    assert_code(ReadCode.CHANGED, lambda: s.view().content(s.version.key))


def test_exact_point_lookup_does_not_enumerate_sibling_candidates(s):
    s.publish()

    class NoSiblingScan(dict):
        def get(self, *args):
            raise AssertionError("exact path enumerated sibling candidates")

    s.ledger._state.index.slots = NoSiblingScan()
    assert s.view().content(s.version.key) == s.assessment.content


def test_admission_recovery_guard_tampering_is_rejected(s):
    s.publish()
    backup = s.ledger.checkpoint()
    forged = replace(backup.admissions[0], change=999)
    backup = replace(backup, admissions=(forged, *backup.admissions[1:]))
    fresh = s.fresh()
    with pytest.raises(ContractError, match="recovery_receipt_mismatch"):
        fresh.restore(backup, "forged-guard")


@pytest.fixture
def global_case(s):
    """Use real frozen D1 planner/classifier, explicit synthetic no-trade evidence."""
    plan = plan_job(job())
    s.clock.value = T0 + timedelta(days=1)
    route = plan.spec.routes[0]
    fixture = Fixture(
        "r5-i6-d3-global-synthetic",
        Capability.BARS_DAILY,
        (plan.spec.universe.members[0].instrument.instrument_id,),
        plan.spec.window.start,
        plan.spec.window.end_exclusive - timedelta(days=1),
        2,
        {"evidence_mode": "synthetic", "no_trade": "explicit-invented-contract"},
    )
    decoder = SyntheticDailyDecoder(
        replace(
            s.contract,
            source=route.source,
            binding=route.binding,
            mapping_revision=route.mapping_revision,
        )
    )
    ledger = MemoryMarketLedger("global-synthetic", s.store, (decoder,), s.clock)
    evidence = []
    versions = []
    for number, unit in enumerate(plan.units):
        raw = (
            b'{"evidence":"invented calendar and explicit no-trade contract evidence"}'
        )
        receipt = replace(
            s.receipt,
            key=Key(Kind.MATERIAL, f"global-{number}"),
            job=plan.spec.identity,
            fixture_id=fixture.fixture_id,
            source=route.source,
            binding=route.binding,
            mapping_revision=route.mapping_revision,
            instrument=unit.instrument,
            locator=f"memory://global-{number}",
            pages=("page-0",),
            rows=(),
            digest=byte_digest(raw),
            byte_length=len(raw),
        )
        ledger.retain(receipt, raw)
        rows = tuple(
            PointEvidence(
                read.point,
                expected_state(plan, read.point) or CoverageClass.NO_TRADE_CONFIRMED,
                ("invented explicit calendar/lifecycle/no-trade evidence",),
                material=receipt.key,
                no_trade_contract="invented-no-trade-contract-r1",
            )
            for read in unit.expand()
        )
        evidence.extend(rows)
        assessment = assess_daily(unit, (receipt,), rows, decoder, s.store, s.clock())
        version = MarketVersion(
            Key(Kind.VERSION, f"global-v{number}"),
            assessment,
            s.clock(),
            ("temporary",),
        )
        version = replace(version, admission_subjects=required_admissions(version))
        ledger.commit_version(version)
        for subject in version.admission_subjects:
            ledger.set_admission(subject, "synthetic-r1", True)
        versions.append(version)
    manifest = classify_coverage(plan, tuple(evidence))
    assert manifest.axes.bar_obligations == CoverageStatus.COMPLETE
    months = tuple(
        MonthKey(
            unit.definition,
            unit.instrument,
            unit.window.start.year,
            unit.window.start.month,
        )
        for unit in plan.units
    )
    ledger.rebuild("global-g1", months)
    first = replace(
        s.request("global-p0", version=versions[0]), version=versions[0].key
    )
    ledger.publish(first)
    return ledger, manifest, versions, decoder


def test_global_manifest_partial_publication_axes_and_independent_material(
    s, global_case
):
    ledger, manifest, versions, decoder = global_case
    summary = ledger.coverage_manifest(manifest)
    assert summary.material_readback == CoverageStatus.COMPLETE
    assert summary.formal_publication == CoverageStatus.PARTIAL
    assert summary.global_status == CoverageStatus.UNKNOWN
    key = Key(Kind.MATERIAL, "global-manifest-partial")
    with pytest.raises(InjectedFailure):
        ledger.freeze_manifest(key, manifest, Fault.AFTER_CANONICAL)
    assert ledger.checkpoint().manifests == ()
    s.clock.tick()
    receipt = ledger.freeze_manifest(key, manifest)
    assert ledger.freeze_manifest(key, manifest) == receipt
    frozen = ledger.read_manifest(key)
    assert frozen.assessment == summary
    assert frozen.source.revision == manifest.revision
    assert len(ledger.checkpoint().publications) == 1
    ledger.publish(s.request("global-p1", version=versions[1]))
    assert ledger.coverage_manifest(manifest).global_status == CoverageStatus.COMPLETE
    assert ledger.read_manifest(key) == frozen  # Never rewrite a partial snapshot.
    backup = ledger.checkpoint()
    restored = MemoryMarketLedger("global-synthetic", s.store, (decoder,), s.clock)
    restored.restore(backup, "global-recovered")
    assert restored.read_manifest(key) == frozen
    ledger.rebuild("no-coverage", ())
    assert ledger.coverage_manifest(manifest).global_status == CoverageStatus.UNKNOWN


@pytest.fixture
def complete_global_case(s, global_case):
    ledger, manifest, versions, decoder = global_case
    ledger.publish(s.request("global-p1", version=versions[1]))
    assert ledger.coverage_manifest(manifest).global_status == CoverageStatus.COMPLETE
    return ledger, manifest, versions, decoder


@pytest.mark.parametrize(
    "classification", [CoverageClass.NO_TRADE_CONFIRMED, CoverageClass.MARKET_CLOSED]
)
def test_non_observed_dangling_material_cannot_publish_or_complete(
    s, global_case, classification
):
    ledger, manifest, versions, decoder = global_case
    original = next(
        version
        for version in versions
        if any(
            item.classification == classification
            for item in version.assessment.evidence
        )
    )
    evidence = tuple(
        replace(item, material=Key(Kind.MATERIAL, "unretained-evidence"))
        if item.classification == classification
        else item
        for item in original.assessment.evidence
    )
    assessment = replace(original.assessment, evidence=evidence)
    with pytest.raises(ContractError, match="lineage_material_missing"):
        assess_daily(
            assessment.unit,
            assessment.materials,
            evidence,
            decoder,
            s.store,
            s.clock(),
        )
    forged = replace(
        original, key=Key(Kind.VERSION, "dangling-evidence"), assessment=assessment
    )
    forged = replace(forged, admission_subjects=required_admissions(forged))
    before = ledger.checkpoint()
    with pytest.raises(ContractError, match="lineage_material_missing"):
        ledger.commit_version(forged)
    assert ledger.checkpoint() == before
    # Simulate a legacy bad fact to exercise publication replay and global audit.
    ledger._state.versions[original.key] = replace(original, assessment=assessment)
    before = ledger.checkpoint()
    with pytest.raises(ContractError, match="lineage_material_missing"):
        ledger.publish(s.request("bad-evidence-publication", version=original))
    assert ledger.checkpoint() == before
    bad_points = {item.point: item for item in evidence}
    manifest = classify_coverage(
        manifest.plan,
        tuple(
            bad_points.get(entry.point, entry.evidence) for entry in manifest.entries
        ),
    )
    summary = ledger.coverage_manifest(manifest)
    assert summary.material_readback == CoverageStatus.PARTIAL
    assert summary.global_status == CoverageStatus.UNKNOWN
    key = Key(Kind.MATERIAL, "dangling-evidence-manifest")
    ledger.freeze_manifest(key, manifest)
    assert ledger.read_manifest(key).assessment == summary


def test_no_trade_material_is_independently_retained_read_and_in_lineage(
    s, global_case
):
    ledger, _, versions, decoder = global_case
    original = versions[1]
    raw = b'{"evidence":"separate explicit no-trade evidence"}'
    receipt = replace(
        original.assessment.materials[0],
        key=Key(Kind.MATERIAL, "separate-no-trade"),
        locator="memory://separate-no-trade",
        digest=byte_digest(raw),
        byte_length=len(raw),
    )
    s.store.put(receipt.locator, raw)
    evidence = tuple(
        replace(item, material=receipt.key)
        if item.classification == CoverageClass.NO_TRADE_CONFIRMED
        else item
        for item in original.assessment.evidence
    )
    assert any(item.material == receipt.key for item in evidence)
    materials = (*original.assessment.materials, receipt)

    def assess():
        return assess_daily(
            original.assessment.unit, materials, evidence, decoder, s.store, s.clock()
        )

    version = replace(
        original, key=Key(Kind.VERSION, "separate-no-trade"), assessment=assess()
    )
    version = replace(version, admission_subjects=required_admissions(version))
    with pytest.raises(ContractError, match="canonical_material_missing"):
        ledger.commit_version(version)  # Readable bytes alone are insufficient.
    ledger.retain(receipt, raw)
    s.store._blobs[receipt.locator] = b"broken"
    with pytest.raises(ContractError, match="material_readback_mismatch"):
        assess()
    with pytest.raises(ContractError, match="material_readback_mismatch"):
        ledger.commit_version(version)
    s.store._blobs[receipt.locator] = raw
    ledger.commit_version(version)
    for subject in version.admission_subjects:
        ledger.set_admission(subject, "synthetic-r1", True)
    ledger.publish(s.request("separate-no-trade", version=version))
    view = ledger.reader.open("global-synthetic")
    assert view.lineage(version.key) == materials
    assert view.content(version.key) == version.assessment.content


@pytest.mark.parametrize("revised_evidence", [False, True])
def test_same_unit_history_does_not_prevent_current_complete(
    s, complete_global_case, revised_evidence
):
    ledger, manifest, versions, _ = complete_global_case
    original = versions[0]
    replacement = replace(original, key=Key(Kind.VERSION, "replacement"))
    if revised_evidence:
        replacement = replace(
            replacement,
            assessment=replace(
                replacement.assessment,
                evidence=tuple(
                    replace(item, references=("revised explicit evidence",))
                    for item in replacement.assessment.evidence
                ),
            ),
        )
    replacement = replace(
        replacement, admission_subjects=required_admissions(replacement)
    )
    ledger.commit_version(replacement)
    slot = SlotKey(
        original.assessment.unit.definition,
        original.assessment.unit.instrument,
        original.assessment.unit.window.start,
    )

    def check_complete(selected):
        summary = ledger.coverage_manifest(manifest)
        assert summary.global_status == CoverageStatus.COMPLETE
        assert summary.missing_units == ()
        assert set(summary.versions) == {selected.key, versions[1].key}
        view = ledger.reader.open("global-synthetic")
        assert {item.version for item in view.candidates((slot,))[0]} == {
            original.key,
            replacement.key,
        }
        assert view.select(slot, s.clock()) == ("SELECTED", (selected.key,))
        assert view.content(selected.key) == selected.assessment.content

    check_complete(original)  # Unpublished sibling remains a retained candidate.
    for subject in replacement.admission_subjects:
        ledger.set_admission(subject, "synthetic-r1", True)
    original_publication = ledger._state.index.histories[original.key][-1]
    s.clock.tick()
    withdrawal = s.request(
        "withdraw-original", Decision.WITHDRAW, original_publication.key, original
    )
    ledger.publish(withdrawal)
    assert ledger.coverage_manifest(manifest).global_status == CoverageStatus.UNKNOWN
    ledger.publish(s.request("publish-replacement", version=replacement))
    if revised_evidence:
        assert (
            ledger.coverage_manifest(manifest).global_status == CoverageStatus.UNKNOWN
        )
        revised = {item.point: item for item in replacement.assessment.evidence}
        manifest = classify_coverage(
            manifest.plan,
            tuple(
                revised.get(entry.point, entry.evidence) for entry in manifest.entries
            ),
        )
    check_complete(replacement)
    view = ledger.reader.open("global-synthetic")
    assert len(view.histories((original.key,))[0]) == 2
    assert len(ledger.checkpoint().versions) == 3
    assert len(ledger.checkpoint().publications) == 4

    s.clock.tick()
    ledger.publish(
        s.request("restore-original", Decision.RESTORE, withdrawal.key, original)
    )
    assert (
        ledger.reader.open("global-synthetic").select(slot, s.clock())[0] == "AMBIGUOUS"
    )
    assert ledger.coverage_manifest(manifest).global_status == CoverageStatus.UNKNOWN


def test_global_completion_uses_date_candidates_across_different_ranges(
    s, complete_global_case
):
    ledger, manifest, versions, _ = complete_global_case
    original = versions[0]
    unit = original.assessment.unit
    split = unit.window.start + timedelta(days=1)
    replacements = []
    for number, window in enumerate(
        (
            DateWindow(unit.window.start, split),
            DateWindow(split, unit.window.end_exclusive),
        )
    ):
        assessment = replace(
            original.assessment,
            unit=replace(unit, window=window),
            evidence=tuple(
                item
                for item in original.assessment.evidence
                if window.contains(item.point.day)
            ),
        )
        version = replace(
            original,
            key=Key(Kind.VERSION, f"date-part-{number}"),
            assessment=assessment,
        )
        version = replace(version, admission_subjects=required_admissions(version))
        ledger.commit_version(version)
        for subject in version.admission_subjects:
            ledger.set_admission(subject, "synthetic-r1", True)
        replacements.append(version)
    old_publication = ledger._state.index.histories[original.key][-1]
    s.clock.tick()
    ledger.publish(
        s.request(
            "withdraw-whole-unit", Decision.WITHDRAW, old_publication.key, original
        )
    )
    ledger.publish(s.request("first-date-part", version=replacements[0]))
    assert ledger.coverage_manifest(manifest).global_status == CoverageStatus.UNKNOWN
    ledger.publish(s.request("second-date-part", version=replacements[1]))
    summary = ledger.coverage_manifest(manifest)
    assert summary.global_status == CoverageStatus.COMPLETE
    assert summary.missing_units == ()
    assert set(summary.versions) == {
        versions[1].key,
        *(item.key for item in replacements),
    }
    assert len(ledger.checkpoint().versions) == 4


@pytest.mark.parametrize(
    "scope_field,value",
    [
        ("universe_revision", "incompatible-universe-r2"),
        ("calendar_revision", "incompatible-calendar-r2"),
        ("timezone", "UTC"),
    ],
)
@pytest.mark.parametrize("retain_original", [True, False])
def test_global_completion_requires_planned_scope_even_on_material_fallback(
    s, complete_global_case, scope_field, value, retain_original
):
    ledger, manifest, versions, decoder = complete_global_case
    original = versions[0]
    incompatible = replace(
        original,
        key=Key(Kind.VERSION, "incompatible-scope"),
        assessment=replace(
            original.assessment,
            unit=replace(original.assessment.unit, **{scope_field: value}),
        ),
    )
    incompatible = replace(
        incompatible, admission_subjects=required_admissions(incompatible)
    )
    if not retain_original:
        # Public writer setup with no compatible Material assessment for unit 0.
        # Its unpublished incompatible sibling must not fill the fallback axis.
        materials = ledger.checkpoint().materials
        ledger = MemoryMarketLedger("global-synthetic", s.store, (decoder,), s.clock)
        for receipt in materials:
            ledger.retain(receipt, s.store.read(receipt.locator, receipt.byte_length))
        ledger.commit_version(versions[1])
        for subject in versions[1].admission_subjects:
            ledger.set_admission(subject, "synthetic-r1", True)
        ledger.publish(s.request("compatible-unit-1", version=versions[1]))
        ledger.rebuild(
            "scope-g1",
            tuple(
                MonthKey(
                    unit.definition,
                    unit.instrument,
                    unit.window.start.year,
                    unit.window.start.month,
                )
                for unit in manifest.plan.units
            ),
        )
    ledger.commit_version(incompatible)
    for subject in incompatible.admission_subjects:
        ledger.set_admission(subject, "synthetic-r1", True)
    if retain_original:
        s.clock.tick()
        ledger.publish(
            s.request(
                "withdraw-compatible-scope",
                Decision.WITHDRAW,
                Key(Kind.PUBLICATION, "global-p0"),
                original,
            )
        )
    else:
        fallback = ledger.coverage_manifest(manifest)
        assert fallback.material_readback == CoverageStatus.PARTIAL
        assert fallback.formal_publication == CoverageStatus.PARTIAL
        assert fallback.global_status == CoverageStatus.UNKNOWN
        assert fallback.missing_units == (original.assessment.unit.identity,)
        assert incompatible.key not in fallback.versions
    ledger.publish(s.request("publish-incompatible-scope", version=incompatible))
    slot = SlotKey(
        original.assessment.unit.definition,
        original.assessment.unit.instrument,
        original.assessment.unit.window.start,
    )
    # Valid in its own admitted scope, but cannot satisfy this plan's scope.
    assert ledger.reader.open("global-synthetic").select(slot, s.clock()) == (
        "SELECTED",
        (incompatible.key,),
    )
    before = ledger.checkpoint()
    summary = ledger.coverage_manifest(manifest)
    assert summary.formal_publication == CoverageStatus.PARTIAL
    assert summary.global_status == CoverageStatus.UNKNOWN
    assert incompatible.key not in summary.versions
    assert summary.material_readback == (
        CoverageStatus.COMPLETE if retain_original else CoverageStatus.PARTIAL
    )
    assert summary.missing_units == (
        () if retain_original else (original.assessment.unit.identity,)
    )
    assert ledger.checkpoint() == before
    key = Key(Kind.MATERIAL, "incompatible-scope-manifest")
    ledger.freeze_manifest(key, manifest)
    assert ledger.read_manifest(key).assessment == summary


def test_global_complete_rejects_published_strict_subrange(s, complete_global_case):
    ledger, manifest, versions, _ = complete_global_case
    original = versions[0]
    unit = original.assessment.unit
    subunit = replace(
        unit,
        window=DateWindow(unit.window.start, unit.window.start + timedelta(days=1)),
    )
    assessment = replace(
        original.assessment,
        unit=subunit,
        evidence=original.assessment.evidence[:1],
    )
    version = replace(
        original, key=Key(Kind.VERSION, "strict-subrange"), assessment=assessment
    )
    version = replace(version, admission_subjects=required_admissions(version))
    ledger.commit_version(version)
    # An unpublished overlapping candidate is retained but is not an ambiguity.
    assert ledger.coverage_manifest(manifest).global_status == CoverageStatus.COMPLETE
    for subject in version.admission_subjects:
        ledger.set_admission(subject, "synthetic-r1", True)
    request = s.request("strict-subrange-publication", version=version)
    ledger.publish(request)
    slot = SlotKey(unit.definition, unit.instrument, unit.window.start)
    view = ledger.reader.open("global-synthetic")
    assert view.select(slot, s.clock())[0] == "AMBIGUOUS"
    assert ledger.coverage_manifest(manifest).global_status != CoverageStatus.COMPLETE
    key = Key(Kind.MATERIAL, "ambiguous-global-manifest")
    ledger.freeze_manifest(key, manifest)
    frozen = ledger.read_manifest(key)
    assert frozen.assessment.global_status != CoverageStatus.COMPLETE
    s.clock.tick()
    ledger.publish(
        s.request("resolve-subrange", Decision.WITHDRAW, request.key, version)
    )
    assert ledger.coverage_manifest(manifest).global_status == CoverageStatus.COMPLETE
    assert ledger.read_manifest(key) == frozen


@pytest.mark.parametrize(
    "corruption",
    [
        "candidate-tuple",
        "coherent-candidate-deletion",
        "reverse",
        "metadata",
        "history",
        "guard",
        "coherent-history-truncation",
        "admission-guard",
        "generation-schema",
        "not-ready",
        "uncovered",
    ],
)
def test_global_complete_requires_projection_integrity(
    s, complete_global_case, corruption
):
    ledger, manifest, versions, _ = complete_global_case
    version = versions[0]
    unit = version.assessment.unit
    slot = SlotKey(unit.definition, unit.instrument, unit.window.start)
    index = ledger._state.index
    if corruption == "candidate-tuple":
        index.slots[slot] = ()
    elif corruption == "coherent-candidate-deletion":
        del index.slots[slot]
        del index.directory[slot]
        del index.rows[(slot, version.key)]
    elif corruption == "reverse":
        index.reverse[version.key] = ()
    elif corruption == "metadata":
        index.versions[version.key] = replace(
            index.versions[version.key], content_digest="wrong"
        )
    elif corruption == "history":
        index.histories[version.key] = ()
    elif corruption == "guard":
        index.guards[version.key] = replace(index.guards[version.key], change=999)
    elif corruption == "coherent-history-truncation":
        first = index.histories[version.key][0]
        guard = index.guards[version.key]
        s.clock.tick()
        ledger.publish(
            s.request("withdraw-global", Decision.WITHDRAW, first.key, version)
        )
        index = ledger._state.index
        index.histories[version.key] = (first,)
        index.guards[version.key] = guard
    elif corruption == "admission-guard":
        subject = version.admission_subjects[0]
        ledger._state.admissions[subject] = replace(
            ledger._state.admissions[subject], change=999
        )
    elif corruption == "generation-schema":
        index.generation = replace(index.generation, schema_revision="wrong")
    elif corruption == "not-ready":
        index.generation = replace(index.generation, ready=False)
    else:
        index.months.clear()
    if corruption == "candidate-tuple":
        assert_code(
            ReadCode.INCONSISTENT,
            lambda: ledger.reader.open("global-synthetic").select(slot, s.clock()),
        )
    before = repr(ledger._state)
    summary = ledger.coverage_manifest(manifest)
    assert summary.global_status != CoverageStatus.COMPLETE
    assert repr(ledger._state) == before  # Summary never repairs damaged projections.
    key = Key(Kind.MATERIAL, "invalid-projection-global-manifest")
    ledger.freeze_manifest(key, manifest)
    assert ledger.read_manifest(key).assessment == summary


def test_recovery_cannot_reuse_generation_or_overwrite_current_guards(s):
    s.publish()
    backup = s.ledger.checkpoint()
    fresh = s.fresh()
    with pytest.raises(ContractError, match="new_generation_required"):
        fresh.restore(backup, "g1")
    fresh.set_admission("synthetic-existing", "r1", False)
    with pytest.raises(ContractError, match="restore_requires_empty_ledger"):
        fresh.restore(backup, "g2")


def test_wrong_schema_and_unavailable_view_are_classified(s):
    s.ledger._state.index.generation = replace(
        s.ledger._state.index.generation, schema_revision="unsupported"
    )
    assert_code(ReadCode.INCONSISTENT, s.view)
    s.ledger.available = False
    assert_code(ReadCode.UNAVAILABLE, s.view)


@pytest.mark.parametrize(
    "budget_name,initial,method",
    [
        ("references", 32768, "candidates"),
        ("events", 2048, "histories"),
    ],
)
def test_cumulative_candidate_and_history_budgets(s, budget_name, initial, method):
    s.publish()
    view = s.view()
    setattr(view._budget, budget_name, initial)
    args = (s.slot,) if method == "candidates" else (s.version.key,)
    assert_code(ReadCode.CAPACITY, lambda: getattr(view, method)(args))


def test_distinct_version_exact_lineage_limits_before_content(s):
    s.publish()
    view = s.view()
    view._budget.versions_seen.update(
        Key(Kind.VERSION, f"other-{i}") for i in range(256)
    )
    assert_code(ReadCode.CAPACITY, lambda: view.candidates((s.slot,)))
    view = s.view()
    view._budget.exact_seen.update(Key(Kind.VERSION, f"other-{i}") for i in range(128))
    assert_code(ReadCode.CAPACITY, lambda: view.versions((s.version.key,), exact=True))
    view = s.view()
    view._budget.lineage_seen.update(
        (Key(Kind.MATERIAL, f"other-{i}"), "") for i in range(4096)
    )
    assert_code(ReadCode.CAPACITY, lambda: view.lineage(s.version.key))


@pytest.fixture(scope="module")
def guard_budget_case():
    scenario = Scenario(index=True)
    keys = []
    for number in range(257):
        version = scenario.make_version(f"guard-budget-{number}")
        scenario.ledger.commit_version(version)
        keys.append(version.key)
    return scenario, tuple(keys)


def read_version_method(view, method, keys):
    if method in {"admission_guards", "lineage"}:
        return tuple(getattr(view, method)(key) for key in keys)
    return getattr(view, method)(keys)


@pytest.mark.parametrize(
    "method,batch",
    [("version_guards", True), ("version_guards", False), ("admission_guards", False)],
)
def test_guard_reads_enforce_distinct_version_256_257_boundary(
    guard_budget_case, method, batch
):
    scenario, keys = guard_budget_case
    before = scenario.ledger.checkpoint()
    view = scenario.view()
    if batch:
        assert len(read_version_method(view, method, keys[:256])) == 256
    else:
        for key in keys[:256]:
            assert read_version_method(view, method, (key,))
    # Repeated keys, including denied/UNKNOWN admissions, consume no new Version.
    assert len(read_version_method(view, method, keys[:256])) == 256
    assert_code(
        ReadCode.CAPACITY, lambda: read_version_method(view, method, keys[256:])
    )
    assert_code(
        ReadCode.CAPACITY,
        lambda: read_version_method(scenario.view(), method, keys),
    )
    assert scenario.ledger.checkpoint() == before


@pytest.mark.parametrize(
    "last_method",
    ["versions", "histories", "version_guards", "admission_guards", "lineage"],
)
def test_guard_and_metadata_reads_share_one_distinct_version_budget(
    guard_budget_case, last_method
):
    scenario, keys = guard_budget_case
    view = scenario.view()
    for offset, method in enumerate(
        ("versions", "histories", "version_guards", "admission_guards")
    ):
        assert (
            len(
                read_version_method(view, method, keys[offset * 64 : (offset + 1) * 64])
            )
            == 64
        )
    # All methods may revisit any of the same 256 keys without double charging.
    assert len(read_version_method(view, last_method, keys[:256])) == 256
    assert_code(
        ReadCode.CAPACITY, lambda: read_version_method(view, last_method, keys[256:])
    )


def test_decoder_malformed_duplicate_and_unknown_native_semantics(s):
    for raw in (b"invalid-json", b'{"day":"2024-01-01","day":"2024-01-03"}'):
        with pytest.raises(ContractError):
            s.decoder.decode(raw, s.receipt)
    contract = replace(
        s.contract,
        fields=(
            replace(s.contract.fields[0], native_unit="UNKNOWN"),
            *s.contract.fields[1:],
        ),
    )
    with pytest.raises(ContractError, match="economic_semantics_unqualified"):
        s.assess(decoder=SyntheticDailyDecoder(contract))
    with pytest.raises(ContractError, match="exception_classification_required"):
        replace(
            s.receipt, exception_classification="raw exception with sensitive detail"
        )


def test_guard_corruption_and_uncontrolled_canonical_import_fail_closed(s):
    s.publish()
    index = s.ledger._state.index
    guard = index.guards[s.version.key]
    index.guards[s.version.key] = replace(guard, change=999)
    assert_code(ReadCode.INCONSISTENT, lambda: s.view().histories((s.version.key,)))
    index.guards[s.version.key] = guard
    subject = s.version.admission_subjects[0]
    admission = s.ledger._state.admissions[subject]
    s.ledger._state.admissions[subject] = replace(admission, change=999)
    assert_code(ReadCode.INCONSISTENT, lambda: s.view().content(s.version.key))
    s.ledger._state.admissions[subject] = admission
    # Private-state fault injection represents an uncontrolled imported fact.
    s.ledger._state.versions[Key(Kind.VERSION, "uncontrolled")] = s.make_version(
        "uncontrolled"
    )
    s.ledger.suspend()
    with pytest.raises(ContractError, match="recovery_receipt_mismatch"):
        s.ledger.rebuild("unqualified", (s.month,))
    assert_code(ReadCode.NOT_READY, s.view)
