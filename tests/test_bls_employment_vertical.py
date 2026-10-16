"""Synthetic CES/CPS fixtures only; no original-source qualification claim."""

import base64
import json
import socket
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from market_data_foundation.canonical import codec
from market_data_foundation.canonical.consumer_read import ReadContractError
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, digest
from market_data_foundation.canonical.postgres import PostgresLedger, validate_schema
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical.bls_cpi_material_store import (
    InMemoryMaterialStore,
    InMemoryWriterProtection,
    MaterialIdentity,
)
from market_data_foundation.historical.bls_employment import (
    MEASUREMENTS,
    REFERENCES,
    SERIES_IDS,
    BlsEmploymentRequest,
    EmploymentEvidence,
    build_plan,
    durable_plan,
    parse_response,
    persist_before_publication,
    publish,
)
from market_data_foundation.historical.employment_candidate import (
    EmploymentRetainedCandidate,
)
from market_data_foundation.historical.employment_contract import BLS_EMPLOYMENT
from market_data_foundation.historical.employment_local_job import (
    DEV_SCHEMA,
    EmploymentJobJournal,
    EmploymentJobSpec,
    EmploymentLocalJob,
    _encode_plan,
)
from market_data_foundation.historical.employment_local_read import (
    EmploymentReadRejected,
    _validate_content,
    read_local_employment,
)
from market_data_foundation.historical.employment_materialization import (
    materialize_persist_publish,
)

BASE = datetime(2026, 10, 16, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Employment deterministic check attempted external IO")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def row(year, month, value, net=None, pct=None):
    return {
        "year": str(year),
        "period": f"M{month:02d}",
        "periodName": datetime(year, month, 1).strftime("%B"),
        "value": value,
        "footnotes": [{"code": "P", "text": "Preliminary"}],
        "calculations": {"net_changes": net or {}, "pct_changes": pct or {}},
    }


def response():
    catalogs = [
        ("CE", "all employees, thousands", "All employees, total nonfarm"),
        (
            "LN",
            "unemployment rate",
            "Unemployment rate, 16 years and over, both sexes, all races",
        ),
        (
            "CE",
            "average hourly earnings of all employees",
            "Average hourly earnings, total private",
        ),
    ]
    rows = [
        [row(2026, m, str(1000 + m * 10), {"1": "10"}) for m in (9, 8, 7, 6)],
        [row(2026, m, "4.0") for m in (9, 8, 7)],
        [row(2026, m, "30.03", pct={"1": "0.7", "12": "3.2"}) for m in (9, 8, 7, 6)]
        + [row(2025, m, "29.10") for m in (9, 8, 7)],
    ]
    return {
        "status": "REQUEST_SUCCEEDED",
        "responseTime": 10,
        "message": [],
        "Results": {
            "series": [
                {
                    "seriesID": sid,
                    "catalog": {
                        "series_id": sid,
                        "seasonality": "Seasonally Adjusted",
                        "survey_abbreviation": catalog[0],
                        "survey_name": (
                            "Synthetic Current Population Survey"
                            if catalog[0] == "LN"
                            else "Synthetic Current Employment Statistics"
                        ),
                        "measure_data_type": catalog[1],
                        "series_title": catalog[2],
                    },
                    "data": data,
                }
                for sid, catalog, data in zip(SERIES_IDS, catalogs, rows, strict=True)
            ]
        },
    }


def candidate(identity="original", raw=None, offset=0):
    raw = encode(response()) if raw is None else raw
    at = BASE + timedelta(seconds=offset)
    evidence = EmploymentEvidence(
        raw,
        BlsEmploymentRequest(),
        at,
        at,
        identity + ":acq",
        "mdf-artifact://synthetic-employment/" + identity,
    )
    return EmploymentRetainedCandidate(
        evidence, build_plan(evidence, identity=identity, finished_at=at, frozen_at=at)
    )


class Clock:
    def __init__(self):
        self.value = BASE + timedelta(seconds=100)

    def __call__(self):
        self.value += timedelta(milliseconds=1)
        return self.value


def rig(tmp_path):
    clock = Clock()
    ledger = InMemoryLedger()
    store = InMemoryMaterialStore()
    protection = InMemoryWriterProtection()
    journal = EmploymentJobJournal(tmp_path)
    engine = EmploymentLocalJob(
        journal,
        clock,
        CanonicalKernel(ledger, clock),
        store,
        protection,
        lambda: nullcontext(InMemoryLedger.from_snapshot(ledger.snapshot())),
        lambda: None,
    )
    return engine, ledger, store, protection


def target(item):
    return EmploymentJobSpec(expected_raw_sha256=digest(item.evidence.raw_bytes))


def run(engine, identity, item):
    return engine.run(identity, enabled=True, preflight=lambda _: None, retained=item)


@pytest.mark.parametrize("attempts", [2, 3])
def test_retry_attempt_count_materializes_and_reads(attempts):
    base = candidate(f"employment-retry-{attempts}")
    retry_evidence = replace(base.evidence, provider_data_attempts=attempts)
    retry_plan = build_plan(
        retry_evidence,
        identity=f"employment-retry-{attempts}",
        finished_at=retry_evidence.obtained_at,
        frozen_at=retry_evidence.obtained_at,
    )
    ledger = InMemoryLedger()
    result = materialize_persist_publish(
        CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=1)),
        retry_plan,
        retry_evidence.raw_bytes,
        InMemoryMaterialStore(),
        InMemoryWriterProtection(),
    )
    for kwargs in (
        {"version_identity": result.version.identity},
        {},
    ):
        view = read_local_employment(
            ledger,
            reference="2026-09",
            as_of=BASE + timedelta(seconds=2),
            **kwargs,
        )
        assert view.acquisitions[0].provider_data_attempts == attempts


def test_source_values_calculations_units_population_and_source_operands():
    data = parse_response(encode(response())).data
    assert list(data["measurements"]) == list(REFERENCES)
    for ref in REFERENCES:
        values = data["measurements"][ref]
        assert set(values) == set(MEASUREMENTS)
        assert values["nfp_change_sa"]["value"] == "10"
        assert values["nfp_change_sa"]["unit"] == "thousand_jobs"
        assert values["nfp_change_sa"]["survey"] == "CES"
        assert values["unemployment_rate_sa"]["survey"] == "CPS"
        assert "people" in values["unemployment_rate_sa"]["population"]
        assert (
            values["ahe_mom_sa"]["value"] == "0.7"
        )  # never derived from rounded levels
        assert values["ahe_yoy_sa"]["origin"] == "provider_returned_calculation"
        assert values["ahe_mom_sa"]["operands"][0]["unit"] == "usd_per_hour"
        assert values["nfp_change_sa"]["source_pointer"].endswith(
            "/calculations/net_changes/1"
        )
        assert (
            values["ahe_yoy_sa"]["operands"][1]["reference_period"]
            == "2025-" + ref[-2:]
        )
    assert not BLS_EMPLOYMENT.source_qualified


@pytest.mark.parametrize(
    "mutation",
    [
        "series",
        "catalog",
        "seasonality",
        "population",
        "net",
        "pct",
        "operand",
        "duplicate",
        "echo",
        "status",
        "footnote",
    ],
)
def test_source_contract_fails_closed(mutation):
    value = response()
    series = value["Results"]["series"]
    if mutation == "series":
        series[1]["seriesID"] = SERIES_IDS[0]
    elif mutation == "catalog":
        del series[0]["catalog"]
    elif mutation == "seasonality":
        series[0]["catalog"]["seasonality"] = "Not Seasonally Adjusted"
    elif mutation == "population":
        series[1]["catalog"]["series_title"] = "men only"
    elif mutation == "net":
        series[0]["data"][0]["calculations"]["net_changes"]["1"] = "11"
    elif mutation == "pct":
        del series[2]["data"][0]["calculations"]["pct_changes"]["12"]
    elif mutation == "operand":
        series[2]["data"].pop()
    elif mutation == "duplicate":
        series[0]["data"].append(series[0]["data"][0])
    elif mutation == "echo":
        value["registrationKey"] = "withheld"
    elif mutation == "status":
        value["status"] = "REQUEST_FAILED"
    elif mutation == "footnote":
        series[0]["data"][0]["footnotes"] = [{"code": "P"}]
    with pytest.raises(ContractError):
        parse_response(encode(value))


def test_raw_order_and_response_time_are_not_economic_revision_assertions():
    first = response()
    second = response()
    second["Results"]["series"].reverse()
    for series in second["Results"]["series"]:
        series["data"].reverse()
    second["responseTime"] = 123
    parsed = parse_response(encode(second))
    assert parsed.data["measurements"]["2026-07"]["nfp_change_sa"]["value"] == "10"
    assert parsed.raw_sha256 != parse_response(encode(first)).raw_sha256
    assert "no_finality_inferred" in parsed.data["revision_policy"]


def test_durable_raw_publication_exact_and_asof_with_prior_month_revision(tmp_path):
    engine, ledger, store, protection = rig(tmp_path)
    old = candidate()
    result = materialize_persist_publish(
        engine.kernel, old.plan, old.evidence.raw_bytes, store, protection
    )
    old_known = result.publication.decision_at
    revised = response()
    # Revising July changes both its net change and the following month's operand.
    revised["Results"]["series"][0]["data"][2]["value"] = "1075"
    revised["Results"]["series"][0]["data"][2]["calculations"]["net_changes"]["1"] = (
        "15"
    )
    revised["Results"]["series"][0]["data"][1]["calculations"]["net_changes"]["1"] = "5"
    new = candidate("revision", encode(revised), offset=1)
    newer = materialize_persist_publish(
        engine.kernel, new.plan, new.evidence.raw_bytes, store, protection
    )
    before = read_local_employment(ledger, reference="2026-07", as_of=old_known)
    after = read_local_employment(
        ledger, reference="2026-07", as_of=newer.publication.decision_at
    )
    exact = read_local_employment(
        ledger,
        reference="2026-07",
        as_of=newer.publication.decision_at,
        version_identity=old.plan.version.identity,
    )
    assert before == exact
    assert before.content == result.plan.content
    assert before.version != after.version
    assert (
        json.loads(after.content)["data"]["measurements"]["2026-07"]["nfp_change_sa"][
            "value"
        ]
        == "15"
    )
    assert after.acquisitions[0].obtained_at == BASE + timedelta(seconds=1)
    assert json.loads(after.content)["provider_vintage"] is None
    assert json.loads(after.content)["release"]["actual_at"] is None
    assert after.materials[0].durable


def test_raw_and_version_without_publication_are_unreadable(tmp_path):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate()
    engine.commit = lambda: (_ for _ in ()).throw(RuntimeError("private"))
    engine.submit("original", target(item))
    result = run(engine, "original", item)
    assert result["state"] == "outcome_unknown"
    assert ledger.get(item.plan.version.key) is not None
    with pytest.raises(ReadContractError):
        read_local_employment(ledger, reference="2026-07", as_of=engine.clock())
    observed = engine.reconcile("original", enabled=True)
    assert observed["state"] == "outcome_unknown"
    assert observed["reconciliation"]["outcome"] == "confirmed_not_published"
    original_history = engine.journal.history("original")
    engine.repair("original", "repair", enabled=True)
    engine.commit = lambda: None
    repaired = run(engine, "repair", item)
    assert repaired["state"] == "succeeded"
    assert repaired["plan"] == observed["plan"]
    assert repaired["raw"] == observed["raw"]
    assert engine.journal.history("original") == original_history
    assert "private" not in repr(original_history)
    with pytest.raises(ContractError):
        engine.repair("original", "branch", enabled=True)


def test_retained_job_idempotent_and_local_read_has_no_io_or_job_dependency(tmp_path):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate()
    engine.submit("original", target(item))
    result = run(engine, "original", item)
    assert result["state"] == "succeeded"
    assert result["attempts"] == 1
    assert not any("provider_request" in key for key in result["effects"])
    assert run(engine, "original", None) == result
    assert engine.reconcile("original", enabled=True) == result
    snapshot = InMemoryLedger.from_snapshot(ledger.snapshot())
    before = snapshot.snapshot()
    for ref in REFERENCES:
        read_local_employment(snapshot, reference=ref, as_of=engine.clock())
    assert before == snapshot.snapshot()
    with pytest.raises(EmploymentReadRejected):
        read_local_employment(snapshot, reference="2026-06", as_of=engine.clock())
    with pytest.raises(ContractError):
        engine.repair("original", "repair", enabled=True)


@pytest.mark.parametrize(
    "phase", ["preflight", "cancel", "cancel_after_plan", "restart"]
)
def test_failure_cancel_restart_are_explicit_and_bounded(tmp_path, phase):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate()
    engine.submit("original", target(item))
    if phase == "cancel":
        engine.journal.cancel("original", engine.clock())
    if phase == "restart":
        engine.journal._update(
            "original",
            engine.clock(),
            state="running",
            attempts=1,
            attempt_id="original-a1",
        )
        with pytest.raises(ContractError, match="restart_reconciliation"):
            run(engine, "original", item)
        observed = engine.reconcile("original", enabled=True)
        assert observed["state"] == "outcome_unknown"
        assert observed["reconciliation"]["outcome"] == "unresolved"
        return
    if phase == "cancel_after_plan":
        original = engine._check_repair_candidate

        def cancel_after_plan(identity, plan):
            original(identity, plan)
            engine.journal.cancel(identity, engine.clock())

        engine._check_repair_candidate = cancel_after_plan

    def preflight(_):
        if phase == "preflight":
            raise RuntimeError("private")

    observed = engine.run("original", enabled=True, preflight=preflight, retained=item)
    assert observed["state"] == ("failed" if phase == "preflight" else "cancelled")
    assert not observed["effects"].get("canonical_possible")
    if phase == "cancel_after_plan":
        engine.repair("original", "repair", enabled=True)
        engine._check_repair_candidate = original
        assert run(engine, "repair", item)["state"] == "succeeded"


def test_publication_lost_ack_reconciles_presence_without_rewriting_unknown(tmp_path):
    engine, ledger, store, protection = rig(tmp_path)
    original_publish = ledger.publish

    def lost_ack(request, clock):
        original_publish(request, clock)
        raise OSError("private")

    ledger.publish = lost_ack
    item = candidate()
    engine.submit("original", target(item))
    assert run(engine, "original", item)["state"] == "outcome_unknown"
    result = engine.reconcile("original", enabled=True)
    assert result["state"] == "outcome_unknown"
    assert result["reconciliation"]["outcome"] == "confirmed_published"
    assert result["last_success"]


@pytest.mark.parametrize("source_offset,frozen_offset", [(200, 200), (0, 200)])
def test_publish_rejects_decision_before_source_or_freeze(
    tmp_path, source_offset, frozen_offset
):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate(offset=source_offset)
    plan = build_plan(
        item.evidence,
        identity="original",
        finished_at=item.evidence.obtained_at,
        frozen_at=BASE + timedelta(seconds=frozen_offset),
    )
    identity = MaterialIdentity(
        digest(item.evidence.raw_bytes), len(item.evidence.raw_bytes)
    )
    plan = durable_plan(plan, identity.locator)
    persist_before_publication(engine.kernel, plan)
    before = ledger.snapshot()
    with pytest.raises(ContractError, match="employment_publication_time_inversion"):
        publish(engine.kernel, plan)
    assert ledger.snapshot() == before
    assert ledger.publication_by_occurrence(plan.publication_request.occurrence) is None


@pytest.mark.parametrize("part", ["version", "acquisition"])
def test_publish_time_bound_requires_exact_persisted_source(tmp_path, part):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate(offset=2)
    identity = MaterialIdentity(
        digest(item.evidence.raw_bytes), len(item.evidence.raw_bytes)
    )
    plan = durable_plan(
        build_plan(
            item.evidence,
            identity="original",
            finished_at=item.evidence.obtained_at,
            frozen_at=BASE + timedelta(seconds=200),
        ),
        identity.locator,
    )
    persist_before_publication(engine.kernel, plan)
    if part == "version":
        plan = replace(
            plan,
            version=replace(plan.version, frozen_at=item.evidence.obtained_at),
        )
    else:
        plan = replace(plan, acquisition=replace(plan.acquisition, obtained_at=BASE))
    before = ledger.snapshot()
    with pytest.raises(ContractError, match="employment_publication_source_mismatch"):
        publish(engine.kernel, plan)
    assert ledger.snapshot() == before


@pytest.mark.parametrize("decision_offset", [200, 201])
def test_publish_accepts_decision_at_or_after_source_and_freeze(
    tmp_path, decision_offset
):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate(offset=200)
    at = BASE + timedelta(seconds=decision_offset)
    engine.kernel.clock = lambda: at
    result = materialize_persist_publish(
        engine.kernel, item.plan, item.evidence.raw_bytes, store, protection
    )
    assert result.publication.decision_at == at
    assert (
        read_local_employment(ledger, reference="2026-09", as_of=at).version
        == item.plan.version
    )


@pytest.mark.parametrize("source_offset,frozen_offset", [(200, 200), (0, 200)])
@pytest.mark.parametrize("usable_offset", [100, 300])
def test_read_rejects_legacy_temporal_inversion_without_rewriting_history(
    tmp_path, source_offset, frozen_offset, usable_offset
):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate(offset=source_offset)
    plan = build_plan(
        item.evidence,
        identity="original",
        finished_at=item.evidence.obtained_at,
        frozen_at=BASE + timedelta(seconds=frozen_offset),
    )
    identity = MaterialIdentity(
        digest(item.evidence.raw_bytes), len(item.evidence.raw_bytes)
    )
    plan = durable_plan(plan, identity.locator)
    persist_before_publication(engine.kernel, plan)
    # Simulate a Publication admitted by the old unguarded boundary, preserving
    # the exact request/lineage that the family reader reconstructs.
    legacy = ledger.publish(
        plan.publication_request, lambda: BASE + timedelta(seconds=100)
    )
    legacy = replace(legacy, usable_at=BASE + timedelta(seconds=usable_offset))
    key = legacy.key
    ledger._records[(key.kind.value, key.identity, key.revision)] = codec.dumps(legacy)
    before = ledger.snapshot()
    for at in (legacy.usable_at, BASE + timedelta(seconds=400)):
        for version in (None, plan.version.identity):
            with pytest.raises(
                EmploymentReadRejected, match="employment_lineage_invalid"
            ):
                read_local_employment(
                    ledger, reference="2026-09", as_of=at, version_identity=version
                )
    with pytest.raises(ContractError, match="employment_publication_time_inversion"):
        publish(engine.kernel, plan)
    assert ledger.snapshot() == before


@pytest.mark.parametrize(
    "server_offsets,accepted",
    [([199], False), ([200, 199], False), ([200, 200], True)],
)
def test_pg_publish_guards_final_server_sample_inside_transaction(
    tmp_path, monkeypatch, server_offsets, accepted
):
    engine, memory, store, protection = rig(tmp_path)
    item = candidate(offset=200)
    identity = MaterialIdentity(
        digest(item.evidence.raw_bytes), len(item.evidence.raw_bytes)
    )
    plan = durable_plan(item.plan, identity.locator)
    persist_before_publication(engine.kernel, plan)

    class Connection:
        info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)

        def __init__(self):
            self.insertions = []
            self.committed = False

        @contextmanager
        def transaction(self):
            try:
                yield
            except Exception:
                self.insertions.clear()
                raise
            else:
                self.committed = True

        def execute(self, query, parameters=None):
            if "INSERT INTO" in query.as_string():
                self.insertions.append(parameters)

    connection = Connection()
    samples = iter(BASE + timedelta(seconds=value) for value in server_offsets)
    monkeypatch.setattr(PostgresLedger, "server_now", lambda _: next(samples))
    for method in (
        "get",
        "get_content",
        "publication_by_occurrence",
        "publications_for_version",
    ):
        delegate = getattr(memory, method)
        monkeypatch.setattr(
            PostgresLedger,
            method,
            lambda self, *args, delegate=delegate: delegate(*args),
        )
    kernel = CanonicalKernel(
        PostgresLedger(connection, DEV_SCHEMA), lambda: pytest.fail("local clock used")
    )
    if accepted:
        result = publish(kernel, plan)
        assert result.decision_at == plan.version.frozen_at
        assert connection.committed and len(connection.insertions) == 1
    else:
        with pytest.raises(
            ContractError, match="employment_publication_time_inversion"
        ):
            publish(kernel, plan)
        assert not connection.committed and not connection.insertions


def test_older_retained_job_succeeds_exact_while_asof_keeps_newer_acquisition(tmp_path):
    engine, ledger, store, protection = rig(tmp_path)
    new_raw = response()
    new_raw["Results"]["series"][1]["data"][0]["value"] = "4.1"
    newer = candidate("newer", encode(new_raw), offset=2)
    older = candidate("older", offset=1)
    engine.submit("newer", target(newer))
    first = run(engine, "newer", newer)
    history = engine.journal.history("newer")
    engine.submit("older", target(older))
    result = run(engine, "older", older)
    assert result["state"] == "succeeded"
    assert (
        result["reconciliation"]["evidence"]["version_identity"]
        == older.plan.version.identity
    )
    assert engine.journal.get("newer") == first
    assert engine.journal.history("newer") == history
    assert (
        read_local_employment(ledger, reference="2026-09", as_of=engine.clock()).version
        == newer.plan.version
    )
    exact = read_local_employment(
        ledger,
        reference="2026-09",
        as_of=engine.clock(),
        version_identity=older.plan.version.identity,
    )
    assert exact.content == older.plan.content
    assert engine.reconcile("older", enabled=True) == result


def test_job_readback_keeps_ambiguity_fail_closed(tmp_path):
    engine, ledger, store, protection = rig(tmp_path)
    first = candidate("first")
    raw = response()
    raw["responseTime"] = 11
    tied = candidate("tied", encode(raw))
    engine.submit("first", target(first))
    assert run(engine, "first", first)["state"] == "succeeded"
    engine.submit("tied", target(tied))
    result = run(engine, "tied", tied)
    assert result["state"] == "outcome_unknown"
    assert result["failure_stage"] == "independent_readback"


def test_raw_get_failure_records_actual_release_and_fresh_reconcile_repair(tmp_path):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate()
    read = store.read_exact
    creates = []
    create = store.create_if_absent

    def counted_create(identity, raw):
        creates.append(identity)
        return create(identity, raw)

    def unavailable(_):
        raise OSError("private transient raw read")

    store.create_if_absent = counted_create
    store.read_exact = unavailable
    engine.submit("original", target(item))
    result = run(engine, "original", item)
    assert result["state"] == "outcome_unknown"
    assert result["writer_session"] == {
        "kind": "synthetic",
        "lock_release_proven": True,
    }
    assert len(creates) == 1
    assert ledger.snapshot() == InMemoryLedger().snapshot()
    history = engine.journal.history("original")
    store.read_exact = read
    fresh = EmploymentLocalJob(
        EmploymentJobJournal(tmp_path),
        engine.clock,
        engine.kernel,
        store,
        protection,
        engine.readback,
        lambda: None,
    )
    observed = fresh.reconcile("original", enabled=True)
    assert observed["state"] == "outcome_unknown"
    assert observed["reconciliation"]["outcome"] == "confirmed_not_published"
    assert observed["reconciliation"]["raw_state"] == "verified"
    assert len(creates) == 1
    assert fresh.journal.history("original")[: len(history)] == history
    preserved = fresh.journal.history("original")
    fresh.repair("original", "repair", enabled=True)
    repaired = run(fresh, "repair", item)
    assert repaired["state"] == "succeeded"
    assert repaired["plan"] == result["plan"]
    assert repaired["raw"] == result["raw"]
    assert fresh.journal.history("original") == preserved
    assert "private transient" not in repr(preserved)


def test_raw_get_and_release_failure_never_records_release_proof(tmp_path):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate()
    acquire = protection.acquire
    held = []

    def broken_release(identity):
        ticket = acquire(identity)
        held.append((ticket, identity))
        return SimpleNamespace(
            assert_held=ticket.assert_held,
            mark_outcome_unknown=ticket.mark_outcome_unknown,
            release=lambda _: (_ for _ in ()).throw(OSError("private release failure")),
        )

    protection.acquire = broken_release
    store.read_exact = lambda _: (_ for _ in ()).throw(OSError("private read failure"))
    engine.submit("original", target(item))
    result = run(engine, "original", item)
    assert result["state"] == "outcome_unknown"
    assert result["writer_session"]["lock_release_proven"] is False
    assert (
        engine.reconcile("original", enabled=True)["reconciliation"]["outcome"]
        == "unresolved"
    )
    with pytest.raises(ContractError, match="repair_reconciliation_required"):
        engine.repair("original", "repair", enabled=True)
    for ticket, identity in held:
        ticket.release(identity)


@pytest.mark.parametrize(
    "observed_raw,raw_state", [(None, "absent"), (b"{}", "mismatched")]
)
def test_released_lock_does_not_allow_repair_without_verified_raw(
    tmp_path, observed_raw, raw_state
):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate()
    store.read_exact = lambda _: (_ for _ in ()).throw(OSError("private read failure"))
    engine.submit("original", target(item))
    result = run(engine, "original", item)
    assert result["writer_session"]["lock_release_proven"] is True
    store.read_exact = lambda _: observed_raw
    result = engine.reconcile("original", enabled=True)
    assert result["state"] == "outcome_unknown"
    assert result["reconciliation"]["outcome"] == "confirmed_not_published"
    assert result["reconciliation"]["raw_state"] == raw_state
    with pytest.raises(ContractError, match="repair_reconciliation_required"):
        engine.repair("original", "repair", enabled=True)


def test_fresh_controller_restarts_from_durable_journal_without_provider(tmp_path):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate()
    engine.submit("original", target(item))
    engine.journal._update(
        "original",
        engine.clock(),
        state="running",
        attempts=1,
        attempt_id="original-a1",
        plan=_encode_plan(item.plan),
        raw={
            "sha256": digest(item.evidence.raw_bytes),
            "size": len(item.evidence.raw_bytes),
            "locator": MaterialIdentity(
                digest(item.evidence.raw_bytes), len(item.evidence.raw_bytes)
            ).locator,
        },
        writer_session={"kind": "synthetic", "lock_release_proven": True},
    )
    # Construction does not alter the existing pending/running document.
    fresh = EmploymentLocalJob(
        EmploymentJobJournal(tmp_path),
        engine.clock,
        engine.kernel,
        store,
        protection,
        engine.readback,
        lambda: None,
    )
    result = fresh.reconcile("original", enabled=True)
    assert result["state"] == "outcome_unknown"
    assert result["reconciliation"]["outcome"] == "confirmed_not_published"
    assert result["reconciliation"]["raw_state"] == "absent"
    with pytest.raises(ContractError):
        fresh.repair("original", "repair", enabled=True)


@pytest.mark.parametrize(
    "part", ["plan", "digest", "quality", "definition", "mode", "budget"]
)
def test_wrong_candidate_or_spec_rejected_before_effects(tmp_path, part):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate()
    if part in {"quality", "definition", "mode", "budget"}:
        kwargs = {
            "quality": {"quality": ("original",)},
            "definition": {"definition": "other"},
            "mode": {"mode": "acquire"},
            "budget": {"max_provider_requests": 1},
        }[part]
        with pytest.raises(ContractError):
            replace(target(item), **kwargs)
        return
    spec = target(item)
    if part == "digest":
        spec = replace(spec, expected_raw_sha256="0" * 64)
    else:
        item = replace(
            item,
            plan=replace(
                item.plan,
                version=replace(item.plan.version, mapping="other"),
            ),
        )
    engine.submit("original", spec)
    result = run(engine, "original", item)
    assert result["state"] == "failed"
    assert not result["effects"].get("canonical_possible")
    assert ledger.snapshot() == InMemoryLedger().snapshot()


def test_local_payload_and_lineage_tampering_fail_closed(tmp_path):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate()
    materialize_persist_publish(
        engine.kernel, item.plan, item.evidence.raw_bytes, store, protection
    )
    document = json.loads(item.plan.content)
    document["data"]["measurements"]["2026-07"]["ahe_mom_sa"]["value"] = "9.9"
    with pytest.raises(EmploymentReadRejected):
        _validate_content(encode(document))
    document = json.loads(item.plan.content)
    document["raw"]["base64"] = base64.b64encode(b"{}").decode()
    with pytest.raises(EmploymentReadRejected):
        _validate_content(encode(document))
    # Change source acquisition metadata while keeping raw/projected payload intact.
    tampered = replace(item.plan.acquisition, external_receipt="mdf-artifact://other")
    key = tampered.key
    ledger._records[(key.kind.value, key.identity, key.revision)] = codec.dumps(
        tampered
    )
    altered = InMemoryLedger.from_snapshot(ledger.snapshot())
    with pytest.raises(EmploymentReadRejected):
        read_local_employment(altered, reference="2026-07", as_of=engine.clock())


def test_namespace_admission_and_readonly_journal_do_not_create_state(tmp_path):
    assert validate_schema(DEV_SCHEMA) == DEV_SCHEMA
    with pytest.raises(ContractError):
        validate_schema("public")
    EmploymentJobJournal(tmp_path / "absent", read_only=True)
    assert not (tmp_path / "absent").exists()
    assert BlsEmploymentRequest().persisted()["body"] == {
        "seriesid": list(SERIES_IDS),
        "startyear": "2025",
        "endyear": "2026",
        "calculations": True,
        "catalog": True,
    }


def test_asof_selects_latest_acquisition_even_when_older_snapshot_publishes_later(
    tmp_path,
):
    engine, ledger, store, protection = rig(tmp_path)
    new = candidate("newer-source", offset=2)
    old = candidate("older-replay", offset=1)
    for item in (new, old):
        materialize_persist_publish(
            engine.kernel, item.plan, item.evidence.raw_bytes, store, protection
        )
    view = read_local_employment(ledger, reference="2026-08", as_of=engine.clock())
    assert view.version.identity == new.plan.version.identity


def test_asof_rejects_same_acquisition_time_ambiguity_and_does_not_hide_corruption(
    tmp_path,
):
    engine, ledger, store, protection = rig(tmp_path)
    for identity in ("first", "second"):
        item = candidate(identity)
        materialize_persist_publish(
            engine.kernel, item.plan, item.evidence.raw_bytes, store, protection
        )
    with pytest.raises(ReadContractError, match="as_of_ambiguous"):
        read_local_employment(ledger, reference="2026-08", as_of=engine.clock())
    ledger._contents[item.plan.version.identity] = (digest(b"{}"), b"{}")
    with pytest.raises(ReadContractError, match="content_digest_mismatch"):
        read_local_employment(ledger, reference="2026-08", as_of=engine.clock())


def test_unpublished_newer_snapshot_cannot_replace_published_old_version(tmp_path):
    from market_data_foundation.historical.bls_employment import (
        durable_plan,
        persist_before_publication,
    )

    engine, ledger, store, protection = rig(tmp_path)
    old, new = candidate(), candidate("newer", offset=1)
    materialize_persist_publish(
        engine.kernel, old.plan, old.evidence.raw_bytes, store, protection
    )
    identity = MaterialIdentity(
        digest(new.evidence.raw_bytes), len(new.evidence.raw_bytes)
    )
    store.create_if_absent(identity, new.evidence.raw_bytes)
    persist_before_publication(engine.kernel, durable_plan(new.plan, identity.locator))
    view = read_local_employment(ledger, reference="2026-09", as_of=engine.clock())
    assert view.version.identity == old.plan.version.identity


def test_raw_mismatch_is_rejected_before_any_store_or_canonical_effect(tmp_path):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate()
    before = ledger.snapshot()
    with pytest.raises(ContractError):
        materialize_persist_publish(engine.kernel, item.plan, b"{}", store, protection)
    assert before == ledger.snapshot()


def test_unknown_raw_put_requires_verified_readback_and_never_claims_created(tmp_path):
    from market_data_foundation.historical.bls_cpi_material_store import (
        MaterialWriteOutcomeUnknown,
    )

    engine, ledger, store, protection = rig(tmp_path)
    create = store.create_if_absent

    def lost_ack(identity, raw):
        create(identity, raw)
        raise MaterialWriteOutcomeUnknown()

    store.create_if_absent = lost_ack
    item = candidate()
    engine.submit("original", target(item))
    result = run(engine, "original", item)
    assert result["state"] == "succeeded"
    assert result["effects"]["raw_disposition"] == "unknown_reconciled_present"
    assert result["effects"]["raw_write_reconciled_outcome_unknown"]


@pytest.mark.parametrize("failure", ["raw_get", "readback_exit", "publication_lookup"])
def test_unknown_handle_release_is_durable_before_fallible_observation(
    tmp_path, failure
):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate()
    commits = []

    def lost_commit_reply():
        commits.append(True)
        raise OSError("private lost commit reply")

    engine.commit = lost_commit_reply
    engine.submit("original", target(item))
    unknown = run(engine, "original", item)
    assert unknown["state"] == "outcome_unknown"
    assert unknown["failure_stage"] == "ledger_handoff"
    assert unknown["writer_session"]["lock_release_proven"] is False
    history = engine.journal.history("original")
    snapshot = ledger.snapshot()
    read = store.read_exact
    readback = engine.readback
    handle = engine._unknown_handles["original"]
    release = handle._ticket.release
    boundaries = []

    def checked_release(identity):
        before = EmploymentJobJournal(tmp_path, read_only=True).get("original")
        assert not (before["reconciliation"] or {}).get("lock_release_proven")
        release(identity)

    handle._ticket.release = checked_release

    def fail_after_release(*_):
        # A separate SQLite connection must see committed evidence at the
        # failing boundary, before the catch block can write any failure event.
        durable = EmploymentJobJournal(tmp_path, read_only=True)
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

    engine.readback = fallible_readback
    if failure == "raw_get":
        store.read_exact = fail_after_release
    unresolved = engine.reconcile("original", enabled=True)
    assert boundaries == [failure]
    assert unresolved["reconciliation"]["outcome"] == "unresolved"
    assert unresolved["reconciliation"]["lock_release_proven"] is True
    assert "original" not in engine._unknown_handles
    with pytest.raises(ContractError, match="repair_reconciliation_required"):
        engine.repair("original", "premature", enabled=True)

    fresh = EmploymentLocalJob(
        EmploymentJobJournal(tmp_path),
        engine.clock,
        engine.kernel,
        store,
        protection,
        readback,
        lambda: commits.append(True),
    )
    assert not fresh._unknown_handles
    # Another observational failure after controller reconstruction must also
    # preserve the evidence; no original in-memory handle is available now.
    store.read_exact = fail_after_release
    again = fresh.reconcile("original", enabled=True)
    assert again["reconciliation"]["outcome"] == "unresolved"
    assert again["reconciliation"]["lock_release_proven"] is True
    store.read_exact = read
    observed = fresh.reconcile("original", enabled=True)
    assert observed["reconciliation"] == {
        "outcome": "confirmed_not_published",
        "raw_state": "verified",
        "version_present": True,
        "lock_release_proven": True,
    }
    assert ledger.snapshot() == snapshot
    assert commits == [True]
    assert fresh.journal.history("original")[: len(history)] == history
    assert {
        k: v
        for k, v in observed.items()
        if k not in {"updated_at", "stage", "classification", "reconciliation"}
    } == {
        k: v
        for k, v in unknown.items()
        if k not in {"updated_at", "stage", "classification", "reconciliation"}
    }
    with pytest.raises(ContractError, match="terminal_immutable"):
        fresh.journal._update(
            "original", fresh.clock(), writer_session={"lock_release_proven": True}
        )
    preserved = fresh.journal.history("original")
    fresh.repair("original", "repair", enabled=True)
    assert fresh.repair("original", "repair", enabled=True)["attempts"] == 0
    with pytest.raises(ContractError, match="scope_already_registered"):
        fresh.repair("original", "branch", enabled=True)
    # Keep the real retained-plan lineage gate in the path to success.
    changed = candidate(identity="other-acquisition")
    with pytest.raises(ContractError, match="repair_plan_mismatch"):
        fresh._candidate("repair", changed, target(changed))
    repaired = run(fresh, "repair", item)
    assert repaired["state"] == "succeeded"
    assert repaired["attempts"] == 1
    assert repaired["plan"] == unknown["plan"]
    assert repaired["raw"] == unknown["raw"]
    assert repaired["spec"]["max_provider_requests"] == 0
    assert commits == [True, True]
    assert fresh.journal.get("original") == observed
    assert fresh.journal.history("original") == preserved
    assert "private" not in repr(preserved)


@pytest.mark.parametrize("failure", ["release", "handle_readback", "raw_mismatch"])
def test_unknown_handle_release_failures_and_raw_mismatch_reject_repair(
    tmp_path, failure
):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate()
    engine.commit = lambda: (_ for _ in ()).throw(OSError("private lost reply"))
    engine.submit("original", target(item))
    unknown = run(engine, "original", item)
    history = engine.journal.history("original")
    handle = engine._unknown_handles["original"]
    release = handle._ticket.release

    def rejected(*_):
        raise OSError("private reconciliation failure")

    if failure == "release":
        handle._ticket.release = rejected
    elif failure == "handle_readback":
        observed = InMemoryLedger.from_snapshot(ledger.snapshot())
        observed.get = rejected
        engine.readback = lambda: nullcontext(observed)
    else:
        store.read_exact = lambda _: b"{}"
    try:
        result = engine.reconcile("original", enabled=True)
        assert result["state"] == "outcome_unknown"
        assert result["writer_session"] == unknown["writer_session"]
        if failure == "raw_mismatch":
            assert handle._resolved is True
            assert result["reconciliation"]["lock_release_proven"] is True
            assert result["reconciliation"]["outcome"] == "confirmed_not_published"
            assert result["reconciliation"]["raw_state"] == "mismatched"
        else:
            assert handle._resolved is False
            assert not result["reconciliation"].get("lock_release_proven")
            assert result["reconciliation"]["outcome"] == "unresolved"
        fresh = EmploymentLocalJob(
            EmploymentJobJournal(tmp_path),
            engine.clock,
            engine.kernel,
            store,
            protection,
            lambda: nullcontext(InMemoryLedger.from_snapshot(ledger.snapshot())),
            lambda: None,
        )
        result = fresh.reconcile("original", enabled=True)
        assert result["reconciliation"]["outcome"] == (
            "confirmed_not_published" if failure == "raw_mismatch" else "unresolved"
        )
        with pytest.raises(ContractError, match="repair_reconciliation_required"):
            fresh.repair("original", "repair", enabled=True)
        assert fresh.journal.history("original")[: len(history)] == history
        assert fresh.journal.get("original")["attempts"] == 1
        assert "private" not in repr(fresh.journal.history("original"))
    finally:
        if not handle._resolved:
            release(handle.identity)


def test_revision_job_uses_separate_digest_scope_and_original_attempt_remains(tmp_path):
    engine, ledger, store, protection = rig(tmp_path)
    old = candidate()
    engine.submit("old", target(old))
    first = run(engine, "old", old)
    revised = response()
    revised["Results"]["series"][1]["data"][1]["value"] = "4.1"
    new = candidate("new-source", encode(revised), offset=1)
    engine.submit("new", target(new))
    assert run(engine, "new", new)["state"] == "succeeded"
    assert engine.journal.get("old") == first
    assert (
        json.loads(
            read_local_employment(
                ledger, reference="2026-08", as_of=engine.clock()
            ).content
        )["data"]["measurements"]["2026-08"]["unemployment_rate_sa"]["value"]
        == "4.1"
    )


def test_owner_nonproduction_requires_owned_pg_garage_and_independent_session(tmp_path):
    engine, ledger, store, protection = rig(tmp_path)
    item = candidate()
    engine.submit(
        "original", replace(target(item), effect_domain="owner_nonproduction")
    )
    result = run(engine, "original", item)
    assert result["state"] == "failed"
    assert not result["effects"].get("canonical_possible")


def test_public_consumer_contract_does_not_admit_employment(tmp_path):
    from market_data_foundation.canonical.consumer_read import read_exact_version

    engine, ledger, store, protection = rig(tmp_path)
    item = candidate()
    materialize_persist_publish(
        engine.kernel, item.plan, item.evidence.raw_bytes, store, protection
    )
    with pytest.raises(ReadContractError, match="definition_not_admitted"):
        read_exact_version(
            ledger, BLS_EMPLOYMENT.key, item.plan.version.key, as_of=engine.clock()
        )
