"""Synthetic Census MARTS contract checks; no real response qualification."""

import json
import socket
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
)
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, digest
from market_data_foundation.canonical.postgres import PostgresLedger, validate_schema
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical.bls_cpi_material_store import (
    InMemoryMaterialStore,
    InMemoryWriterProtection,
    MaterialIdentity,
    MaterialWriteOutcomeUnknown,
)
from market_data_foundation.historical.census_retail import (
    CATEGORY_CODES,
    HEADERS,
    PREVIOUS,
    REFERENCE,
    RETAIL_BINDING_CANDIDATE,
    CensusRetailRequest,
    RetailEvidence,
    build_live_plan,
    parse_response,
    validate_live_plan,
)
from market_data_foundation.historical.census_retail_live import RetailCandidate
from market_data_foundation.historical.retail_contract import CENSUS_RETAIL
from market_data_foundation.historical.retail_local_job import (
    DEV_SCHEMA,
    RetailJobJournal,
    RetailJobSpec,
    RetailLocalJob,
    _decode_plan,
    _encode_plan,
)
from market_data_foundation.historical.retail_local_read import (
    RetailReadRejected,
    _validate_content,
    read_local_retail,
)
from market_data_foundation.historical.retail_materialization import (
    RetailMaterializationOutcomeUnknown,
    materialize_persist_publish,
)

BASE = datetime(2026, 10, 16, tzinfo=UTC)
PUBLISHED = BASE + timedelta(seconds=1)


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Retail deterministic tests attempted external IO")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


def encode(value):
    return json.dumps(value, separators=(",", ":")).encode()


def row(reference, category, typ, value):
    values = {
        "category_code": category,
        "data_type_code": typ,
        "seasonally_adj": "yes",
        "cell_value": value,
        "error_data": "no",
        "time": reference,
        "us": "1",
    }
    return [values[key] for key in HEADERS]


def response():
    # Five SM categories, two periods, and only current headline MPCSM.
    table = [list(HEADERS)]
    for reference, levels in (
        (PREVIOUS, ("1000", "200", "100", "50", "150")),
        (REFERENCE, ("1100", "210", "105", "55", "160")),
    ):
        table.extend(
            row(reference, category, "SM", value)
            for category, value in zip(CATEGORY_CODES, levels, strict=True)
        )
    # Intentionally differs from the SM quotient: preserve this provider cell.
    table.append(row(REFERENCE, "44X72", "MPCSM", "9.7"))
    return table


def without_raw_digest(value):
    if isinstance(value, dict):
        return {
            key: without_raw_digest(item)
            for key, item in value.items()
            if key != "raw_sha256"
        }
    if isinstance(value, list):
        return [without_raw_digest(item) for item in value]
    return value


def evidence(raw=None, *, attempts=1):
    return RetailEvidence(
        encode(response()) if raw is None else raw,
        CensusRetailRequest(),
        BASE,
        BASE + timedelta(milliseconds=1),
        "mdf-census-retail-synthetic",
        "mdf-artifact://t05-synthetic/run",
        attempts,
    )


def plan(*, attempts=1):
    return build_live_plan(
        evidence(attempts=attempts),
        identity="t05-retail-synthetic",
        finished_at=BASE + timedelta(milliseconds=2),
        frozen_at=BASE + timedelta(milliseconds=3),
    )


def materialize(*, attempts=1, store=None, commit=lambda: None):
    ledger = InMemoryLedger()
    result = materialize_persist_publish(
        CanonicalKernel(ledger, lambda: PUBLISHED),
        plan(attempts=attempts),
        evidence().raw_bytes,
        store if store is not None else InMemoryMaterialStore(),
        InMemoryWriterProtection(),
        authoritative_commit=commit,
    )
    return ledger, result


def retail_job(tmp_path):
    ledger = InMemoryLedger()
    store = InMemoryMaterialStore()
    protection = InMemoryWriterProtection()
    job = RetailLocalJob(
        RetailJobJournal(tmp_path),
        lambda: PUBLISHED,
        CanonicalKernel(ledger, lambda: PUBLISHED),
        store,
        protection,
        lambda: nullcontext(InMemoryLedger.from_snapshot(ledger.snapshot())),
        lambda: None,
    )
    item = RetailCandidate(evidence(), plan(), Path("synthetic-unused"))
    job.submit(
        "original",
        RetailJobSpec(
            mode="retained",
            max_provider_requests=0,
            expected_raw_sha256=digest(item.evidence.raw_bytes),
        ),
    )
    return job, ledger, store, protection, item


def run_retained(job, item):
    return job.run("original", enabled=True, preflight=lambda _: None, retained=item)


def test_retail_schema_exact_admission_and_constructor_without_connection_io():
    class UntouchableConnection:
        def __getattribute__(self, name):
            pytest.fail("PostgresLedger constructor touched connection")

    connection = UntouchableConnection()
    assert validate_schema(DEV_SCHEMA) == DEV_SCHEMA
    ledger = PostgresLedger(connection, DEV_SCHEMA)
    assert ledger.schema == DEV_SCHEMA
    assert ledger.connection is connection
    for unsafe in (
        "public",
        DEV_SCHEMA.replace("_r1", "_r2"),
        DEV_SCHEMA + "_adjacent",
        "prefix_" + DEV_SCHEMA,
        DEV_SCHEMA + ".records",
        DEV_SCHEMA + ";DROP SCHEMA public",
        DEV_SCHEMA + "\n",
    ):
        with pytest.raises(ContractError, match="unsafe_postgres_namespace"):
            validate_schema(unsafe)
        with pytest.raises(ContractError, match="unsafe_postgres_namespace"):
            PostgresLedger(connection, unsafe)


def test_minimal_matrix_and_mapping():
    projection = parse_response(encode(response()))
    data = projection.data
    measurements = data["measurements"]
    assert len(data["source_cells"]) == 11
    assert data["reference_period"] == "2026-09"
    assert data["comparison_period"] == "2026-08"
    assert data["control_definition"]["retained_category"] == "445"
    assert data["control_definition"]["subtract"] == ["441", "444", "447", "722"]
    expected = {
        "headline_level_sa": ("1100", "provider_returned", "million_current_usd"),
        "headline_mom_sa": ("9.7", "provider_returned_calculation", "percent"),
        "control_level_sa": ("570", "product_derived", "million_current_usd"),
        "control_mom_sa": ("14.000000", "product_derived", "percent"),
    }
    for name, (value, origin, unit) in expected.items():
        assert measurements[name]["value"] == value
        assert measurements[name]["origin"] == origin
        assert measurements[name]["unit"] == unit
        assert measurements[name]["reference_period"] == REFERENCE
    assert measurements["headline_mom_sa"]["operands"][0]["data_type_code"] == "MPCSM"
    for item in measurements["control_level_sa"]["operands"]:
        assert item["data_type_code"] == "SM"
    for period in measurements["control_mom_sa"]["control_operands"]:
        assert all(item["data_type_code"] == "SM" for item in period["operands"])
    assert CENSUS_RETAIL.source_qualified is False
    assert "response_shape_not_live_qualified" in RETAIL_BINDING_CANDIDATE.limitations


@pytest.mark.parametrize("missing", range(1, 12))
def test_every_required_cell_is_strict(missing):
    table = response()
    del table[missing]
    with pytest.raises(ContractError, match="required_matrix_missing"):
        parse_response(encode(table))


@pytest.mark.parametrize("value", ["-2.000", "(S)", "NaN", "malformed", "0"])
def test_broad_query_non_target_cells_do_not_alter_projection(value):
    table = response()
    table.extend(
        row(ref, category, "MPCSM", value)
        for ref in (PREVIOUS, REFERENCE)
        for category in CATEGORY_CODES
        if (ref, category) != (REFERENCE, "44X72")
    )
    table.extend(
        row(ref, category, typ, value)
        for ref in (PREVIOUS, REFERENCE)
        for category, typ in (("445", "SM"), ("445", "MPCSM"), ("44X72", "MPC"))
    )
    table.append(table[-1])  # Non-target duplicates are also outside the index.
    raw = encode(table)
    parsed = parse_response(raw)
    assert len(parsed.data["source_cells"]) == 11

    baseline = parse_response(encode(response()))
    assert without_raw_digest(parsed.data) == without_raw_digest(baseline.data)
    assert parsed.raw_sha256 == digest(raw)
    assert parsed.raw_size == len(raw)
    for cell in parsed.data["source_cells"]:
        assert cell["raw_sha256"] == digest(raw)
        _, row_index, col_index = cell["source_pointer"].split("/")
        assert table[int(row_index)][int(col_index)] == cell["value"]


@pytest.mark.parametrize("target", range(1, 12))
@pytest.mark.parametrize("value", ["(S)", "NaN", "malformed"])
def test_every_target_cell_rejects_unavailable_or_malformed_values(target, value):
    table = response()
    table[target][HEADERS.index("cell_value")] = value
    with pytest.raises(ContractError, match="cell_unavailable_or_malformed"):
        parse_response(encode(table))


@pytest.mark.parametrize("target", range(1, 12))
def test_every_target_cell_rejects_duplicates(target):
    table = response()
    table.append(table[target])
    with pytest.raises(ContractError, match="duplicate_row"):
        parse_response(encode(table))


@pytest.mark.parametrize(
    ("field", "value", "classification"),
    [
        ("time", "2026-07", "row_target_invalid"),
        ("seasonally_adj", "no", "row_semantics_invalid"),
        ("error_data", "yes", "row_semantics_invalid"),
        ("us", "*", "row_semantics_invalid"),
        ("cell_value", None, "row_shape_invalid"),
    ],
)
def test_non_target_rows_still_require_structure_and_common_semantics(
    field, value, classification
):
    table = response()
    unrelated = row(REFERENCE, "445", "SM", "(S)")
    unrelated[HEADERS.index(field)] = value
    table.append(unrelated)
    with pytest.raises(ContractError, match=classification):
        parse_response(encode(table))


@pytest.mark.parametrize(
    ("field", "value", "classification"),
    [
        ("time", "2026-07", "row_target_invalid"),
        ("category_code", "445", "required_matrix_missing"),
        ("data_type_code", "MPC", "required_matrix_missing"),
        ("seasonally_adj", "no", "row_semantics_invalid"),
        ("error_data", "yes", "row_semantics_invalid"),
        ("us", "*", "row_semantics_invalid"),
        ("cell_value", "(S)", "cell_unavailable_or_malformed"),
        ("cell_value", "NaN", "cell_unavailable_or_malformed"),
        ("cell_value", "0", "level_not_positive"),
        ("cell_value", "-1", "level_not_positive"),
    ],
)
def test_invalid_cell_fails_closed(field, value, classification):
    table = response()
    table[1][HEADERS.index(field)] = value
    with pytest.raises(ContractError, match=classification):
        parse_response(encode(table))


@pytest.mark.parametrize("kind", ["duplicate", "header", "row", "json", "control"])
def test_invalid_response_is_classified(kind):
    table = response()
    if kind == "duplicate":
        table.append(table[1])
    elif kind == "header":
        table[0][0] = "unknown"
    elif kind == "row":
        table[1].pop()
    elif kind == "control":
        table[1][HEADERS.index("cell_value")] = "1"
    with pytest.raises(ContractError):
        parse_response(b"not json" if kind == "json" else encode(table))


def test_header_and_row_order_are_not_fixed():
    table = response()
    permuted = [list(reversed(table[0]))] + [
        list(reversed(values)) for values in reversed(table[1:])
    ]
    parsed = parse_response(encode(permuted))
    assert parsed.data["measurements"]["headline_mom_sa"]["value"] == "9.7"
    for cell in parsed.data["source_cells"]:
        _, row_index, col_index = cell["source_pointer"].split("/")
        assert permuted[int(row_index)][int(col_index)] == cell["value"]


@pytest.mark.parametrize(
    "duplicates",
    [("seasonally_adj",), ("error_data",), ("seasonally_adj", "error_data")],
)
@pytest.mark.parametrize("reverse_columns", [False, True])
def test_matching_duplicate_predicates_preserve_projection(duplicates, reverse_columns):
    baseline = response()
    # Both duplicates reproduce the retained R2 header, including its positions.
    columns = [*HEADERS[:5], *duplicates, *HEADERS[5:]]
    if reverse_columns:
        columns.reverse()
    table = [columns] + [
        [values[HEADERS.index(field)] for field in columns] for values in baseline[1:]
    ]
    # Unavailable non-target cells stay outside the target projection.
    unrelated = list(table[-1])
    unrelated[columns.index("category_code")] = "445"
    unrelated[columns.index("cell_value")] = "(S)"
    table.extend([unrelated, unrelated])
    raw = encode(table)
    parsed = parse_response(raw)
    expected = parse_response(encode(baseline))

    def without_source_pointer(value):
        if isinstance(value, dict):
            return {
                key: without_source_pointer(item)
                for key, item in value.items()
                if key != "source_pointer"
            }
        if isinstance(value, list):
            return [without_source_pointer(item) for item in value]
        return value

    assert without_source_pointer(without_raw_digest(parsed.data)) == (
        without_source_pointer(without_raw_digest(expected.data))
    )
    assert parsed.raw_sha256 == digest(raw)
    assert parsed.raw_size == len(raw)
    for cell in parsed.data["source_cells"]:
        assert cell["raw_sha256"] == digest(raw)
        _, row_index, col_index = cell["source_pointer"].split("/")
        assert int(col_index) == columns.index("cell_value")
        assert table[int(row_index)][int(col_index)] == cell["value"]


@pytest.mark.parametrize("field", ["seasonally_adj", "error_data"])
@pytest.mark.parametrize("duplicate_index", [0, 1])
@pytest.mark.parametrize("non_target", [False, True])
def test_duplicate_predicate_mismatch_fails_closed(field, duplicate_index, non_target):
    table = response()
    position = HEADERS.index(field)
    for values in table:
        values.append(values[position])
    target = table[1]
    if non_target:
        target[HEADERS.index("category_code")] = "445"
    target[position if duplicate_index == 0 else -1] = (
        "no" if field == "seasonally_adj" else "yes"
    )
    with pytest.raises(
        ContractError, match="^census_retail_duplicate_dimension_mismatch$"
    ):
        parse_response(encode(table))


@pytest.mark.parametrize(
    "field", ["category_code", "data_type_code", "cell_value", "time", "us"]
)
def test_unauthorized_duplicate_header_fails_closed(field):
    table = response()
    for values in table:
        values.append(values[HEADERS.index(field)])
    with pytest.raises(ContractError, match="^census_retail_header_invalid$"):
        parse_response(encode(table))


@pytest.mark.parametrize("field", HEADERS)
def test_missing_header_fails_closed(field):
    table = response()
    for values in table:
        del values[HEADERS.index(field)]
    with pytest.raises(ContractError, match="^census_retail_header_invalid$"):
        parse_response(encode(table))


@pytest.mark.parametrize("field", ["unknown", "seasonally_adj", "error_data"])
def test_extra_or_triplicate_header_fails_closed(field):
    table = response()
    table[0].extend([field, field])
    for values in table[1:]:
        values.extend(["yes", "yes"])
    with pytest.raises(ContractError, match="^census_retail_header_invalid$"):
        parse_response(encode(table))


@pytest.mark.parametrize("attempts", [1, 2, 3])
def test_plan_materialization_exact_and_as_of_read(attempts):
    candidate = plan(attempts=attempts)
    validate_live_plan(candidate, evidence().raw_bytes)
    assert _decode_plan(_encode_plan(candidate)) == candidate
    assert RetailJobSpec.from_document(RetailJobSpec().document()) == RetailJobSpec()
    ledger, result = materialize(attempts=attempts)
    for options in ({}, {"version_identity": result.version.identity}):
        view = read_local_retail(
            ledger, reference=REFERENCE, as_of=PUBLISHED, **options
        )
        assert view.version == result.version
        assert view.acquisitions[0].provider_data_attempts == attempts
        assert view.content == result.plan.content
        assert all(item.durable and item.replayable for item in view.materials)
    with pytest.raises(ReadContractError) as failure:
        read_local_retail(
            ledger,
            reference=REFERENCE,
            version_identity=result.version.identity,
            as_of=PUBLISHED - timedelta(microseconds=1),
        )
    assert failure.value.classification == ReadFailure.UNPUBLISHED_OR_UNAVAILABLE_AS_OF
    with pytest.raises(RetailReadRejected, match="reference_not_admitted"):
        read_local_retail(ledger, reference=PREVIOUS, as_of=PUBLISHED)


def test_tampered_plan_is_rejected_before_storage():
    candidate = replace(plan(), content=b"tampered")
    with pytest.raises(ContractError, match="lineage_mismatch"):
        materialize_persist_publish(
            CanonicalKernel(InMemoryLedger(), lambda: PUBLISHED),
            candidate,
            evidence().raw_bytes,
            InMemoryMaterialStore(),
            InMemoryWriterProtection(),
        )


@pytest.mark.parametrize("field", ["request", "data", "raw", "provider_revision"])
def test_local_content_is_recomputed_from_raw(field):
    document = json.loads(plan().content)
    document[field] = {}
    with pytest.raises(RetailReadRejected, match="payload_invalid_or_mismatched"):
        _validate_content(encode(document))


def test_raw_readback_failure_prevents_publication():
    class CorruptStore(InMemoryMaterialStore):
        def read_exact(self, identity):
            return b"corrupt"

    ledger = InMemoryLedger()
    before = ledger.snapshot()
    with pytest.raises(ContractError, match="raw_readback_mismatch"):
        materialize_persist_publish(
            CanonicalKernel(ledger, lambda: PUBLISHED),
            plan(),
            evidence().raw_bytes,
            CorruptStore(),
            InMemoryWriterProtection(),
        )
    assert ledger.snapshot() == before


def test_unknown_raw_write_requires_exact_readback():
    class UnknownStore(InMemoryMaterialStore):
        def create_if_absent(self, identity, payload):
            super().create_if_absent(identity, payload)
            raise MaterialWriteOutcomeUnknown()

    _, result = materialize(store=UnknownStore())
    assert result.verification.reconciled_outcome_unknown is True
    assert result.verification.disposition is None
    assert result.verification.identity.sha256 == digest(evidence().raw_bytes)


def test_commit_failure_preserves_unknown_outcome_for_reconciliation():
    ledger = InMemoryLedger()

    def fail():
        raise OSError("synthetic commit failure")

    with pytest.raises(RetailMaterializationOutcomeUnknown) as failure:
        materialize_persist_publish(
            CanonicalKernel(ledger, lambda: PUBLISHED),
            plan(),
            evidence().raw_bytes,
            InMemoryMaterialStore(),
            InMemoryWriterProtection(),
            authoritative_commit=fail,
        )
    assert failure.value.reconcile(ledger) == "confirmed_committed"
    assert (
        ledger.publication_by_occurrence(plan().publication_request.occurrence) is None
    )


def test_raw_get_failure_records_actual_release_before_unknown_and_fresh_reconcile(
    tmp_path, monkeypatch
):
    job, ledger, store, protection, item = retail_job(tmp_path)
    read = store.read_exact
    unknown = job._unknown
    boundaries = []

    def unavailable(_):
        raise OSError("private transient raw read")

    def checked_unknown(*args):
        # Proof must already be committed before the exception handler records
        # the unknown outcome, and the original lock must actually be gone.
        durable = RetailJobJournal(tmp_path, read_only=True)
        assert durable.get("original")["writer_session"] == {
            "kind": "synthetic",
            "lock_release_proven": True,
        }
        raw_identity = MaterialIdentity(
            digest(item.evidence.raw_bytes), len(item.evidence.raw_bytes)
        )
        ticket = protection.acquire(raw_identity)
        ticket.release(raw_identity)
        boundaries.append(True)
        return unknown(*args)

    monkeypatch.setattr(job, "_unknown", checked_unknown)
    store.read_exact = unavailable
    result = run_retained(job, item)
    assert boundaries == [True]
    assert result["state"] == "outcome_unknown"
    assert ledger.snapshot() == InMemoryLedger().snapshot()
    history = job.journal.history("original")
    store.read_exact = read
    fresh = RetailLocalJob(
        RetailJobJournal(tmp_path),
        job.clock,
        job.kernel,
        store,
        protection,
        job.readback,
        lambda: None,
    )
    observed = fresh.reconcile("original", enabled=True)
    assert observed["reconciliation"] == {
        "outcome": "confirmed_not_published",
        "raw_state": "verified",
        "version_present": False,
    }
    assert fresh.journal.history("original")[: len(history)] == history
    fresh.repair("original", "repair", enabled=True)
    repaired = fresh.run(
        "repair", enabled=True, preflight=lambda _: None, retained=item
    )
    assert repaired["state"] == "succeeded"
    assert repaired["writer_session"]["lock_release_proven"] is True
    assert "private" not in repr(fresh.journal.history("original"))


@pytest.mark.parametrize("failure", ["raw_get", "readback_exit", "publication_lookup"])
def test_unknown_handle_release_is_durable_before_fallible_observation(
    tmp_path, failure
):
    job, ledger, store, protection, item = retail_job(tmp_path)
    commits = []

    def lost_commit_reply():
        commits.append(True)
        raise OSError("private lost commit reply")

    job.commit = lost_commit_reply
    unknown = run_retained(job, item)
    assert unknown["state"] == "outcome_unknown"
    assert unknown["failure_stage"] == "ledger_handoff"
    assert unknown["writer_session"]["lock_release_proven"] is False
    history = job.journal.history("original")
    snapshot = ledger.snapshot()
    read = store.read_exact
    readback = job.readback
    handle = job._unknown_handles["original"]
    release = handle._ticket.release
    boundaries = []

    def checked_release(identity):
        before = RetailJobJournal(tmp_path, read_only=True).get("original")
        assert not (before["reconciliation"] or {}).get("lock_release_proven")
        release(identity)

    handle._ticket.release = checked_release

    def fail_after_release(*_):
        # A separate SQLite connection must see committed evidence at the
        # failing boundary, before the catch block can write any failure event.
        durable = RetailJobJournal(tmp_path, read_only=True)
        document = durable.get("original")
        assert handle._resolved is True
        assert document["reconciliation"]["lock_release_proven"] is True
        assert document["reconciliation"]["outcome"] == "unresolved"
        assert (
            durable.history("original")[-1]["reconciliation"]
            == document["reconciliation"]
        )
        boundaries.append(failure)
        raise OSError("private transient observation failure")

    reads = 0

    @contextmanager
    def fallible_readback():
        nonlocal reads
        reads += 1
        observed = InMemoryLedger.from_snapshot(ledger.snapshot())
        if failure == "publication_lookup" and reads == 2:
            observed.publication_by_occurrence = fail_after_release
        yield observed
        if failure == "readback_exit" and reads == 1:
            fail_after_release()

    job.readback = fallible_readback
    if failure == "raw_get":
        store.read_exact = fail_after_release
    unresolved = job.reconcile("original", enabled=True)
    assert boundaries == [failure]
    assert unresolved["reconciliation"]["outcome"] == "unresolved"
    assert unresolved["reconciliation"]["lock_release_proven"] is True
    assert "original" not in job._unknown_handles
    with pytest.raises(ContractError, match="repair_reconciliation_required"):
        job.repair("original", "premature", enabled=True)

    fresh = RetailLocalJob(
        RetailJobJournal(tmp_path),
        job.clock,
        job.kernel,
        store,
        protection,
        readback,
        lambda: commits.append(True),
    )
    assert not fresh._unknown_handles
    # Repeated observation failure after restart must preserve the same proof.
    store.read_exact = fail_after_release
    again = fresh.reconcile("original", enabled=True)
    assert again["reconciliation"]["outcome"] == "unresolved"
    assert again["reconciliation"]["lock_release_proven"] is True
    with pytest.raises(ContractError, match="repair_reconciliation_required"):
        fresh.repair("original", "still-premature", enabled=True)
    store.read_exact = read
    observed = fresh.reconcile("original", enabled=True)
    assert observed["reconciliation"] == {
        "outcome": "confirmed_not_published",
        "raw_state": "verified",
        "version_present": True,
        "lock_release_proven": True,
    }
    assert observed["writer_session"] == unknown["writer_session"]
    assert ledger.snapshot() == snapshot
    assert commits == [True]
    assert fresh.journal.history("original")[: len(history)] == history
    fresh.repair("original", "repair", enabled=True)
    assert "private" not in repr(fresh.journal.history("original"))
