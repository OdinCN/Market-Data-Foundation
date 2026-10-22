"""I1/I3 original-intent reconciliation with in-memory SQL and byte stores only."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from market_data_foundation.canonical.market_material import MemoryMaterialBytes
from market_data_foundation.canonical.market_persistence import (
    CommitUnknown,
    Fault,
    InjectedFailure,
    MemoryMarketLedger,
    Verification,
)
from market_data_foundation.canonical.model import ContractError, Key, Kind
from market_data_foundation.domain_contracts.market_universe import content_id
from market_data_foundation.integration.market_vertical import verify_original
from market_data_foundation.storage.market_dataset_postgres import PostgresMarketDataset
from market_data_foundation.storage.market_dataset_probe import synthetic_fixture
from tests.test_i3_market_dataset import RUN, SQLDouble

LEVELS = ("raw", "source-projection", "status", "metadata")


@pytest.fixture
def physical():
    connection = SQLDouble()
    store = MemoryMaterialBytes()
    receipt, raw, decoder, version, request, _ = synthetic_fixture(
        RUN, lambda *args: "memory://i1-i3"
    )
    ledger = PostgresMarketDataset(connection, store, (decoder,), owner_run=RUN)
    yield SimpleNamespace(
        connection=connection,
        store=store,
        receipt=receipt,
        raw=raw,
        decoder=decoder,
        version=version,
        request=request,
        ledger=ledger,
    )
    connection.db.close()


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("fault", [None, Fault.AFTER_COMMIT])
def test_physical_original_intent_survives_ack_loss_and_rejects_conflicts(
    physical, monkeypatch, level, fault
):
    s = physical
    before = tuple(s.connection.db.iterdump())
    queries = tuple(s.connection.queries)
    # The caller owns this immutable original BEFORE retain can lose its ACK.
    intent = s.ledger.material_intent(s.receipt, level=level)
    assert intent == (s.receipt, level)
    assert tuple(s.connection.db.iterdump()) == before
    assert tuple(s.connection.queries) == queries
    assert not s.store._blobs

    def commit():
        return s.ledger.retain(intent[0], s.raw, level=intent[1], fault=fault)

    if fault is None:
        committed = commit()
        assert committed.payload_digest == content_id((s.receipt, level))
    else:
        with pytest.raises(CommitUnknown):
            commit()

    # A fresh writer reconciles without a returned ACK, store reads or writes.
    fresh = PostgresMarketDataset(s.connection, s.store, owner_run=RUN)
    before = tuple(s.connection.db.iterdump())
    s.connection.queries.clear()

    def forbidden(*args, **kwargs):
        raise AssertionError("verification_must_be_read_only")

    monkeypatch.setattr(s.store, "put", forbidden)
    monkeypatch.setattr(s.store, "read", forbidden)
    monkeypatch.setattr(fresh, "retain", forbidden)
    assert verify_original(fresh, intent) == Verification.COMMITTED
    for wrong_level in set(LEVELS) - {level}:
        assert verify_original(fresh, (s.receipt, wrong_level)) == Verification.CONFLICT
    wrong_receipt = replace(s.receipt, fixture_id="different-original-receipt")
    assert verify_original(fresh, (wrong_receipt, level)) == Verification.CONFLICT
    foreign = replace(s.receipt, key=Key(Kind.MATERIAL, "different-material"))
    assert verify_original(fresh, (foreign, level)) == Verification.ABSENT
    # No implicit physical-level inference or fallback from a pure D3 receipt.
    assert verify_original(fresh, s.receipt) == Verification.CONFLICT
    assert tuple(s.connection.db.iterdump()) == before
    assert all(
        query.startswith(("SET TRANSACTION", "SELECT"))
        for query in s.connection.queries
    )
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" in (
        s.connection.queries
    )


def test_physical_uncommitted_and_unavailable_original_intent(physical):
    s = physical
    intent = s.ledger.material_intent(s.receipt, level="status")
    assert verify_original(s.ledger, intent) == Verification.ABSENT
    with pytest.raises(InjectedFailure):
        s.ledger.retain(intent[0], s.raw, level=intent[1], fault=Fault.BEFORE_COMMIT)
    assert verify_original(s.ledger, intent) == Verification.ABSENT
    s.connection.fail_sql = "schema_metadata"
    assert verify_original(s.ledger, intent) == Verification.UNKNOWN


@pytest.mark.parametrize("fault", [None, Fault.AFTER_COMMIT])
def test_original_d3_pure_receipt_contract_is_preserved(physical, fault):
    s = physical
    ledger = MemoryMarketLedger(
        "original-d3", s.store, (s.decoder,), lambda: s.connection.clock
    )
    if fault is None:
        committed = ledger.retain(s.receipt, s.raw)
        assert committed.payload_digest == content_id(s.receipt)
    else:
        with pytest.raises(CommitUnknown):
            ledger.retain(s.receipt, s.raw, fault)
    before = ledger.checkpoint()
    assert verify_original(ledger, s.receipt) == Verification.COMMITTED
    wrong_receipt = replace(s.receipt, fixture_id="different-original-receipt")
    assert verify_original(ledger, wrong_receipt) == Verification.CONFLICT
    assert verify_original(ledger, (s.receipt, "raw")) == Verification.CONFLICT
    assert ledger.checkpoint() == before
    ledger.available = False
    assert verify_original(ledger, s.receipt) == Verification.UNKNOWN


@pytest.mark.parametrize("operation", ["version", "publication"])
@pytest.mark.parametrize("fault", [None, Fault.AFTER_COMMIT])
def test_physical_version_and_publication_keep_original_payload(
    physical, operation, fault
):
    s = physical
    s.ledger.retain(s.receipt, s.raw)
    if operation == "version":
        value, commit = s.version, s.ledger.commit_version
        wrong = replace(value, admission_subjects=("different",))
    else:
        s.ledger.commit_version(s.version)
        for subject in s.version.admission_subjects:
            s.ledger.set_admission(subject, "synthetic-only", True)
        value, commit = s.request, s.ledger.publish
        wrong = replace(value, occurrence="different")
    if fault is None:
        assert commit(value).payload_digest == content_id(value)
    else:
        with pytest.raises(CommitUnknown):
            commit(value, fault)
    before = tuple(s.connection.db.iterdump())
    assert verify_original(s.ledger, value) == Verification.COMMITTED
    assert verify_original(s.ledger, wrong) == Verification.CONFLICT
    assert tuple(s.connection.db.iterdump()) == before


@pytest.mark.parametrize("level", ["invalid", "source_projection", None, ["raw"]])
def test_invalid_physical_level_rejected_before_any_effect(physical, level):
    s = physical
    before = tuple(s.connection.db.iterdump())
    with pytest.raises(ContractError, match="material_level"):
        s.ledger.material_intent(s.receipt, level=level)
    with pytest.raises(ContractError, match="material_level"):
        verify_original(s.ledger, (s.receipt, level))
    assert tuple(s.connection.db.iterdump()) == before
    assert not s.connection.queries
    assert not s.store._blobs
