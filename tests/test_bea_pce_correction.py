"""T04 correction counterexamples; synthetic bytes and PG protocol doubles only."""

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from psycopg.pq import TransactionStatus
from test_bea_pce_vertical import (
    BASE,
    Clock,
    bundle,
    candidate,
    rig,
    run,
    submit,
)
from test_bea_pce_vertical import no_external_io as no_external_io

from market_data_foundation.canonical import codec
from market_data_foundation.canonical.consumer_read import ReadContractError
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, Publication, digest
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical.bea_pce import (
    MAX_NORMALIZED_BYTES,
    MAX_RESPONSE_BYTES,
    REFERENCE,
    PceRetainedCandidate,
    PceRetainedEvidence,
    build_plan,
    durable_plan,
    encode,
    parse_bundle,
    persist_before_publication,
)
from market_data_foundation.historical.bls_cpi_material_store import (
    GarageCredentials,
    GarageMaterialStore,
    InMemoryMaterialStore,
    InMemoryWriterProtection,
    MaterialIdentity,
    WireResponse,
    WriteDisposition,
)
from market_data_foundation.historical.pce_local_job import DEV_SCHEMA, PceJobSpec
from market_data_foundation.historical.pce_local_read import (
    PceReadRejected,
    read_local_pce,
)
from market_data_foundation.historical.pce_material_store import PceMaterialIdentity
from market_data_foundation.historical.pce_materialization import (
    PceMaterializationOutcomeUnknown,
    PceUnknownPhase,
    materialize_persist_publish,
)


def _persist(retained, ledger=None):
    ledger = ledger or InMemoryLedger()
    raw = retained.evidence.raw_bytes
    identity = PceMaterialIdentity(digest(raw), len(raw))
    plan = durable_plan(retained.plan, identity.locator)
    persist_before_publication(CanonicalKernel(ledger, Clock()), plan)
    return ledger, plan


@pytest.mark.parametrize("offset", [0, 100])
def test_authoritative_memory_publication_rejects_future_freeze(offset):
    document = bundle()
    for response in document["responses"]:
        for field in ("requested_at", "obtained_at"):
            response[field] = (
                datetime.fromisoformat(response[field]) + timedelta(seconds=offset)
            ).isoformat()
    evidence = PceRetainedEvidence(encode(document), "future:acq", "synthetic:future")
    retained = PceRetainedCandidate(
        evidence,
        build_plan(
            evidence,
            identity="future",
            finished_at=BASE + timedelta(seconds=4 + offset),
            frozen_at=BASE + timedelta(seconds=5 + offset),
        ),
    )
    ledger, plan = _persist(retained)
    before = ledger.snapshot()
    decision = BASE + timedelta(seconds=4 if offset == 0 else 10)
    with pytest.raises(ContractError, match="pce_publication_before_freeze"):
        # Bypass the source/job helper: the authoritative ledger must enforce it.
        ledger.publish(plan.publication_request, lambda: decision)
    assert ledger.snapshot() == before
    assert ledger.get(plan.acquisition.key) == plan.acquisition
    assert ledger.get(plan.version.key).frozen_at == plan.version.frozen_at
    for at in (decision, BASE + timedelta(seconds=200)):
        with pytest.raises(PceReadRejected, match="as_of_unavailable"):
            read_local_pce(ledger, reference=REFERENCE, as_of=at)


def test_future_acquisition_is_checked_independently_of_freeze():
    ledger, plan = _persist(candidate())
    acquisition = replace(plan.acquisition, obtained_at=BASE + timedelta(seconds=30))
    key = acquisition.key
    # Model a preexisting malformed graph; do not rely only on plan validation.
    ledger._records[(key.kind.value, key.identity, key.revision)] = codec.dumps(
        acquisition
    )
    before = ledger.snapshot()
    with pytest.raises(ContractError, match="pce_publication_before_acquisition"):
        ledger.publish(plan.publication_request, lambda: BASE + timedelta(seconds=10))
    assert ledger.snapshot() == before


@pytest.mark.parametrize("seconds", [5, 10])
def test_valid_publication_preserves_actual_time_and_as_of_boundary(seconds):
    ledger, plan = _persist(candidate())
    decision = BASE + timedelta(seconds=seconds)
    publication = ledger.publish(plan.publication_request, lambda: decision)
    assert publication.decision_at == publication.usable_at == decision
    assert ledger.get(plan.version.key) == plan.version
    assert ledger.get(plan.acquisition.key) == plan.acquisition
    with pytest.raises(PceReadRejected, match="as_of_unavailable"):
        read_local_pce(
            ledger, reference=REFERENCE, as_of=decision - timedelta(microseconds=1)
        )
    assert (
        read_local_pce(ledger, reference=REFERENCE, as_of=decision).publication
        == publication
    )


def test_materialization_rejection_retains_unknown_protection_until_reconciled():
    retained = candidate()
    ledger, store, protection = (
        InMemoryLedger(),
        InMemoryMaterialStore(),
        InMemoryWriterProtection(),
    )
    with pytest.raises(PceMaterializationOutcomeUnknown) as caught:
        materialize_persist_publish(
            CanonicalKernel(ledger, lambda: BASE),
            retained.plan,
            retained.evidence.raw_bytes,
            store,
            protection,
        )
    unknown = caught.value
    assert unknown.phase == PceUnknownPhase.PUBLICATION
    assert isinstance(unknown.__cause__, ContractError)
    assert str(unknown.__cause__) == "pce_publication_before_freeze"
    assert not ledger.publications_for_version(retained.plan.version.key)
    with pytest.raises(ContractError, match="material_writer_busy"):
        protection.acquire(unknown.identity)
    assert unknown.reconcile(ledger) == "confirmed_not_published"
    protection.acquire(unknown.identity).release(unknown.identity)
    assert store.create_attempts == 1


@pytest.mark.parametrize("as_of", [4, 20])
def test_ordinary_read_still_rejects_legacy_inverted_publication(as_of):
    ledger, plan = _persist(candidate())
    ledger.put(
        Publication(
            plan.publication_request,
            BASE + timedelta(seconds=4),
            BASE + timedelta(seconds=4),
        )
    )
    before = ledger.snapshot()
    with pytest.raises(ContractError, match="pce_publication_before_freeze"):
        ledger.publish(plan.publication_request, lambda: BASE + timedelta(seconds=20))
    with pytest.raises((PceReadRejected, ReadContractError)):
        read_local_pce(
            ledger, reference=REFERENCE, as_of=BASE + timedelta(seconds=as_of)
        )
    assert ledger.snapshot() == before


class _PublicationConnection:
    """Only the publication SQL protocol; no actual PG connection or storage."""

    def __init__(self, memory, seconds):
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self.memory = memory
        self.samples = iter(BASE + timedelta(seconds=s) for s in seconds)
        self.statements = []
        self.pending = []
        self.commits = self.rollbacks = 0

    @contextmanager
    def transaction(self):
        self.info.transaction_status = TransactionStatus.INTRANS
        try:
            yield
        except Exception:
            self.pending.clear()
            self.rollbacks += 1
            raise
        else:
            for publication in self.pending:
                self.memory.put(publication)
            self.pending.clear()
            self.commits += 1
        finally:
            self.info.transaction_status = TransactionStatus.IDLE

    def execute(self, statement, params=None):
        rendered = statement if isinstance(statement, str) else statement.as_string()
        self.statements.append(rendered)
        if rendered == "SELECT clock_timestamp()":
            assert any("LOCK TABLE" in s for s in self.statements)
            return SimpleNamespace(fetchone=lambda: (next(self.samples),))
        if "LOCK TABLE" in rendered:
            return None
        assert "INSERT INTO" in rendered and "publications" in rendered
        self.pending.append(codec.loads(params[-1]))


class _PublicationPostgresLedger(PostgresLedger):
    def __init__(self, memory, seconds):
        super().__init__(_PublicationConnection(memory, seconds), DEV_SCHEMA)
        self.memory = memory

    def get(self, key):
        return self.memory.get(key)

    def get_content(self, key):
        return self.memory.get_content(key)

    def publication_by_occurrence(self, occurrence):
        return self.memory.publication_by_occurrence(occurrence)

    def publications_for_version(self, version):
        return self.memory.publications_for_version(version)


@pytest.mark.parametrize(
    "samples, usable, expected_error",
    [
        ([4], None, "pce_publication_before_freeze"),
        ([10, 4], None, "pce_publication_before_freeze"),
        ([10, 12], 11, "usable_before_decision"),
        ([5, 5], None, None),
        ([10, 12], None, None),
        ([10, 12], 15, None),
    ],
)
def test_pg_publication_uses_final_actual_server_time(samples, usable, expected_error):
    memory, plan = _persist(candidate())
    ledger = _PublicationPostgresLedger(memory, samples)
    request = replace(
        plan.publication_request,
        usable_at=None if usable is None else BASE + timedelta(seconds=usable),
    )
    before = memory.snapshot()

    def forbidden_client_clock():
        pytest.fail("PG publication must use observed server time")

    if expected_error:
        with pytest.raises(ContractError, match=expected_error):
            ledger.publish(request, forbidden_client_clock)
        assert memory.snapshot() == before
        assert ledger.connection.commits == 0 and ledger.connection.rollbacks == 1
        assert not any("INSERT INTO" in s for s in ledger.connection.statements)
    else:
        publication = ledger.publish(request, forbidden_client_clock)
        assert publication.decision_at == BASE + timedelta(seconds=samples[-1])
        assert publication.usable_at == BASE + timedelta(
            seconds=samples[-1] if usable is None else usable
        )
        assert memory.publication_by_occurrence(request.occurrence) == publication
        assert ledger.connection.commits == 1 and ledger.connection.rollbacks == 0
    assert memory.get(plan.acquisition.key) == plan.acquisition
    assert memory.get(plan.version.key) == plan.version


def test_pg_existing_inverted_occurrence_is_rejected_without_insert():
    memory, plan = _persist(candidate())
    memory.put(Publication(plan.publication_request, BASE, BASE))
    ledger = _PublicationPostgresLedger(memory, [])
    before = memory.snapshot()
    with pytest.raises(ContractError, match="pce_publication_before_freeze"):
        ledger.publish(plan.publication_request, Clock())
    assert memory.snapshot() == before
    assert ledger.connection.rollbacks == 1 and ledger.connection.commits == 0
    assert not any("INSERT INTO" in s for s in ledger.connection.statements)


def _sized_candidate(size, name="sized-pce"):
    raw = encode(bundle())
    assert len(raw) < size
    raw += b" " * (size - len(raw))
    evidence = PceRetainedEvidence(raw, name + ":acquisition", "synthetic:" + name)
    return PceRetainedCandidate(
        evidence,
        build_plan(
            evidence,
            identity=name,
            finished_at=BASE + timedelta(seconds=4),
            frozen_at=BASE + timedelta(seconds=5),
        ),
    )


@pytest.mark.parametrize("size", [100_000, 100_001, 2_000_000])
def test_pce_size_contract_through_job_materialization_restart_and_read(tmp_path, size):
    retained = _sized_candidate(size)
    job, journal, ledger, store, _, _ = rig(tmp_path)
    spec = PceJobSpec(expected_raw_sha256=digest(retained.evidence.raw_bytes))
    assert spec.max_response_bytes == MAX_RESPONSE_BYTES
    submit(job, "sized", retained)
    result = run(job, "sized", retained)
    assert result["state"] == "succeeded"
    assert journal.get("sized")["raw"]["size"] == size
    identity = PceMaterialIdentity(digest(retained.evidence.raw_bytes), size)
    assert store.read_exact(identity) == retained.evidence.raw_bytes
    assert (
        PceMaterialIdentity.from_locator(
            identity.locator, expected_sha256=identity.sha256, expected_size=size
        )
        == identity
    )
    fresh = InMemoryLedger.from_snapshot(ledger.snapshot())
    for version in (None, retained.plan.version.identity):
        view = read_local_pce(
            fresh,
            reference=REFERENCE,
            as_of=BASE + timedelta(seconds=20),
            version_identity=version,
        )
        assert view.content == retained.plan.content
    creates = store.create_attempts
    assert run(job, "sized", retained) == result
    assert store.create_attempts == creates == 1


@pytest.mark.parametrize("size", [0, True, 2_000_001])
def test_pce_identity_rejects_invalid_sizes(size):
    with pytest.raises(ContractError, match="pce_material_size_invalid"):
        PceMaterialIdentity("a" * 64, size)


def test_pce_oversize_bundle_and_forged_plan_fail_before_effects():
    retained = _sized_candidate(2_000_000)
    raw = retained.evidence.raw_bytes + b" "
    store, ledger = InMemoryMaterialStore(), InMemoryLedger()
    with pytest.raises(ContractError):
        parse_bundle(raw)
    with pytest.raises(ContractError):
        materialize_persist_publish(
            CanonicalKernel(ledger, Clock()),
            retained.plan,
            raw,
            store,
            InMemoryWriterProtection(),
        )
    assert store.create_attempts == store.reads == 0
    assert ledger.snapshot() == InMemoryLedger().snapshot()


def test_maximum_bundle_preserves_normalized_carrier_and_lineage():
    # Size is a material contract, not permission for opaque revision metadata.
    retained = _sized_candidate(MAX_RESPONSE_BYTES)
    raw, plan = retained.evidence.raw_bytes, retained.plan
    assert 2_000_000 < len(plan.content) <= MAX_NORMALIZED_BYTES
    result = materialize_persist_publish(
        CanonicalKernel(ledger := InMemoryLedger(), Clock()),
        plan,
        raw,
        InMemoryMaterialStore(),
        InMemoryWriterProtection(),
    )
    assert (
        read_local_pce(
            ledger, reference=REFERENCE, as_of=BASE + timedelta(seconds=20)
        ).version
        == result.version
    )


@pytest.mark.parametrize("size", [100_000, 100_001, 2_000_000, 2_000_001])
def test_cpi_identity_boundary_is_unchanged(size):
    if size == 100_000:
        assert MaterialIdentity("a" * 64, size).size == size
    else:
        with pytest.raises(ContractError, match="material_size_invalid"):
            MaterialIdentity("a" * 64, size)


def test_old_small_carrier_reuse_requires_exact_bytes_not_semantic_equivalence():
    old = candidate("old")
    raw = old.evidence.raw_bytes
    old_identity = MaterialIdentity(digest(raw), len(raw))
    identity = PceMaterialIdentity(digest(raw), len(raw))
    assert identity.locator == old_identity.locator
    store = InMemoryMaterialStore()
    assert store.create_if_absent(old_identity, raw) == WriteDisposition.CREATED
    assert store.create_if_absent(identity, raw) == WriteDisposition.ALREADY_PRESENT
    changed = json.dumps(
        dict(reversed(list(json.loads(raw).items()))), separators=(",", ":")
    ).encode()
    assert len(changed) == len(raw) and changed != raw
    assert json.loads(raw) == json.loads(changed)
    old_projection, new_projection = parse_bundle(raw), parse_bundle(changed)
    assert old_projection != new_projection  # Exact retained lineage differs.
    assert {k: v["value"] for k, v in old_projection["measurements"].items()} == {
        k: v["value"] for k, v in new_projection["measurements"].items()
    }
    changed_identity = PceMaterialIdentity(digest(changed), len(changed))
    assert changed_identity.locator != identity.locator
    with pytest.raises(ContractError, match="material_content_mismatch"):
        store.create_if_absent(identity, changed)
    assert store.read_exact(identity) == raw
    assert store.create_if_absent(changed_identity, changed) == WriteDisposition.CREATED
    # A semantically equal but byte-different existing object cannot be reused.
    store._objects[(identity.bucket, identity.key)] = changed
    with pytest.raises(ContractError, match="material_existing_mismatch"):
        store.create_if_absent(identity, raw)
    assert store.read_exact(identity) == changed


@pytest.mark.parametrize("size", [100_001, 2_000_000])
def test_large_pce_identity_works_with_verified_garage_protocol_double(size):
    raw = _sized_candidate(size).evidence.raw_bytes
    identity = PceMaterialIdentity(digest(raw), size)

    class Wire:
        def __init__(self):
            self.calls = []
            self.responses = iter(
                [WireResponse(404, b""), WireResponse(200, b""), WireResponse(200, raw)]
            )

        def send(self, request):
            self.calls.append(request)
            return next(self.responses)

    wire = Wire()
    store = GarageMaterialStore(
        GarageCredentials("synthetic-access", "synthetic-value"),
        transport=wire,
        clock=lambda: BASE,
    )
    assert store.create_if_absent(identity, raw) == WriteDisposition.CREATED
    assert store.read_exact(identity) == raw
    assert [r.method for r in wire.calls] == ["GET", "PUT", "GET"]
    assert wire.calls[1].body == raw


@pytest.mark.parametrize("size", [100_001, 2_000_000])
def test_large_garage_existing_nonidentical_bytes_fail_without_put(size):
    raw = _sized_candidate(size).evidence.raw_bytes
    changed = raw[:-1] + b"\t"
    assert len(raw) == len(changed) and json.loads(raw) == json.loads(changed)

    class Wire:
        def __init__(self):
            self.calls = []

        def send(self, request):
            self.calls.append(request)
            return WireResponse(200, changed)

    wire = Wire()
    store = GarageMaterialStore(
        GarageCredentials("synthetic-access", "synthetic-value"), transport=wire
    )
    with pytest.raises(ContractError, match="material_existing_mismatch"):
        store.create_if_absent(PceMaterialIdentity(digest(raw), size), raw)
    assert [r.method for r in wire.calls] == ["GET"]


@pytest.mark.parametrize(
    "kwargs",
    [{"sha256": "A" * 64}, {"bucket": "other"}, {"key": "materials/other"}],
)
def test_pce_identity_preserves_digest_bucket_key_allowlists(kwargs):
    with pytest.raises(ContractError):
        PceMaterialIdentity(**({"sha256": "a" * 64, "size": 100_001} | kwargs))
