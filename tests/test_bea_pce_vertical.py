"""Synthetic-only PCE candidate checks; no provider or private infrastructure."""

import json
import socket
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Inexact, getcontext
from types import SimpleNamespace

import psycopg
import pytest

from market_data_foundation.canonical import codec
from market_data_foundation.canonical.consumer_read import ReadContractError
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, Grade, digest
from market_data_foundation.canonical.postgres import PostgresLedger, validate_schema
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical.bea_pce import (
    BUNDLE_SCHEMA,
    FORMULA_REVISION,
    GRADES,
    PCE_BINDING_CANDIDATE,
    REFERENCE,
    SANITIZER_REVISION,
    TABLES,
    PceRetainedCandidate,
    PceRetainedEvidence,
    ResponseCapture,
    build_plan,
    encode,
    parse_bundle,
    request_parameters,
    retain_sanitized_bundle,
)
from market_data_foundation.historical.bls_cpi_material_store import (
    InMemoryMaterialStore,
    InMemoryWriterProtection,
    MaterialIdentity,
)
from market_data_foundation.historical.pce_local_job import (
    DEV_SCHEMA,
    PceJobJournal,
    PceJobSpec,
    PceLocalJob,
)
from market_data_foundation.historical.pce_local_read import (
    PceReadRejected,
    read_local_pce,
)
from market_data_foundation.historical.pce_materialization import (
    materialize_persist_publish,
)

BASE = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("PCE deterministic candidate attempted external IO")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


def bundle():
    """Deliberately fabricated values/metadata; never a source evidence fixture."""
    responses = []
    for i, table in enumerate(TABLES):
        rows = []
        for line, series, description in (
            (
                "1",
                "DPCERGM" if i == 0 else "DPCERG",
                "Personal consumption expenditures",
            ),
            ("25", "DPCCRGM" if i == 0 else "DPCCRG", "PCE excluding food and energy"),
        ):
            for period, number in (("2026M07", "110.000"), ("2025M07", "100.000")):
                rows.append(
                    {
                        "TableName": table,
                        "SeriesCode": series,
                        "LineNumber": line,
                        "TimePeriod": period,
                        "LineDescription": description,
                        "METRIC_NAME": "Percent Change" if i == 0 else "Index",
                        "CL_UNIT": "Percent change" if i == 0 else "Index Numbers",
                        "UNIT_MULT": "0",
                        "DataValue": "0.3" if i == 0 else number,
                    }
                )
        notes = [
            {
                "NoteRef": "1",
                "NoteText": (
                    "Synthetic Table 2.8.7. Monthly price changes "
                    "from preceding month, "
                    "seasonally adjusted."
                    if i == 0
                    else "Synthetic Table 2.8.4. Monthly price indexes, 2017=100, "
                    "seasonally adjusted."
                ),
            },
            {"NoteRef": "LastRevised", "NoteText": "SYNTHETIC revision label A"},
        ]
        responses.append(
            {
                "request": request_parameters(table),
                "requested_at": (BASE + timedelta(seconds=i * 2)).isoformat(),
                "obtained_at": (BASE + timedelta(seconds=i * 2 + 1)).isoformat(),
                "results": {
                    "Data": rows,
                    "Notes": notes,
                    "UTCProductionTime": "SYNTHETIC production metadata",
                },
            }
        )
    return {
        "schema": BUNDLE_SCHEMA,
        "sanitizer_revision": SANITIZER_REVISION,
        "exact_original_retained": False,
        "provenance": "synthetic",
        "responses": responses,
    }


def candidate(name="synthetic-pce", document=None):
    raw = encode(document or bundle())
    evidence = PceRetainedEvidence(
        raw, name + ":acquisition", "mdf-artifact://synthetic/" + name
    )
    plan = build_plan(
        evidence,
        identity=name,
        finished_at=BASE + timedelta(seconds=4),
        frozen_at=BASE + timedelta(seconds=5),
    )
    return PceRetainedCandidate(evidence, plan)


class Clock:
    def __init__(self, start=10):
        self.value = BASE + timedelta(seconds=start)

    def __call__(self):
        self.value += timedelta(milliseconds=1)
        return self.value


def rig(tmp_path, *, ledger=None, store=None, readback=None, commit=lambda: None):
    ledger = ledger or InMemoryLedger()
    store = store or InMemoryMaterialStore()
    protection = InMemoryWriterProtection()
    clock = Clock()
    journal = PceJobJournal(tmp_path / "jobs")
    job = PceLocalJob(
        journal,
        clock,
        CanonicalKernel(ledger, clock),
        store,
        protection,
        readback
        or (lambda: nullcontext(InMemoryLedger.from_snapshot(ledger.snapshot()))),
        commit,
    )
    return job, journal, ledger, store, protection, clock


def submit(job, name, retained):
    return job.submit(
        name, PceJobSpec(expected_raw_sha256=digest(retained.evidence.raw_bytes))
    )


def run(job, name, retained, preflight=lambda _spec: None):
    return job.run(name, enabled=True, retained=retained, preflight=preflight)


def publish(ledger=None, retained=None, clock=None):
    ledger = ledger or InMemoryLedger()
    retained = retained or candidate()
    store = InMemoryMaterialStore()
    result = materialize_persist_publish(
        CanonicalKernel(ledger, clock or Clock()),
        retained.plan,
        retained.evidence.raw_bytes,
        store,
        InMemoryWriterProtection(),
    )
    return ledger, store, result


def test_mapping_formula_input_identity_and_precision_are_explicit():
    old_precision = getcontext().prec
    getcontext().prec = 6
    try:
        parsed = parse_bundle(encode(bundle()))
    finally:
        getcontext().prec = old_precision
    measurements = parsed["measurements"]
    assert set(measurements) == {
        "headline_mom_sa",
        "core_mom_sa",
        "headline_yoy_sa",
        "core_yoy_sa",
    }
    assert measurements["headline_mom_sa"]["value"] == "0.3"
    assert measurements["core_mom_sa"]["series"] == "DPCCRGM"
    for name in ("headline_yoy_sa", "core_yoy_sa"):
        measurement = measurements[name]
        assert measurement["classification"] == "product_derived"
        assert measurement["value"] == "10.000000"
        assert measurement["formula_revision"] == FORMULA_REVISION
        assert measurement["decimal_precision"] == 28
        assert measurement["rounding"] == "ROUND_HALF_EVEN"
        assert [o["reference"] for o in measurement["inputs"]] == ["2026-07", "2025-07"]
        assert all(
            o["retained_sha256"] == digest(encode(bundle()))
            for o in measurement["inputs"]
        )
        assert len({o["input_version_identity"] for o in measurement["inputs"]}) == 2
    assert parsed["release_at"] is None and parsed["provider_vintage"] is None
    assert parsed["retention"]["exact_original_retained"] is False
    assert parsed["obtained_at"] == (BASE + timedelta(seconds=3)).isoformat()


@pytest.mark.parametrize(
    "index, expected",
    [("100.0000005", "0.000000"), ("100.0000015", "0.000002"), ("99", "-1.000000")],
)
def test_yoy_half_even_and_negative_inflation(index, expected):
    document = bundle()
    document["responses"][1]["results"]["Data"][0]["DataValue"] = index
    assert (
        parse_bundle(encode(document))["measurements"]["headline_yoy_sa"]["value"]
        == expected
    )


def test_yoy_does_not_inherit_ambient_decimal_traps():
    document = bundle()
    document["responses"][1]["results"]["Data"][1]["DataValue"] = "99.123"
    old = getcontext().traps[Inexact]
    getcontext().traps[Inexact] = True
    try:
        assert (
            parse_bundle(encode(document))["measurements"]["headline_yoy_sa"]["value"]
            == "10.973235"
        )
    finally:
        getcontext().traps[Inexact] = old


@pytest.mark.parametrize(
    "field,value",
    [
        ("SeriesCode", "DPCCRG"),
        ("LineNumber", "2"),
        ("TableName", "T20805"),
        ("METRIC_NAME", "Annual Rate"),
        ("UNIT_MULT", "1"),
        ("CL_UNIT", "Percent"),
        ("LineDescription", "Food and energy"),
        ("DataValue", "(NA)"),
        ("DataValue", "NaN"),
        ("DataValue", "1e3"),
        ("DataValue", True),
        ("TimePeriod", "2026Q3"),
        ("TimePeriod", "2026M08"),
    ],
)
def test_wrong_mom_row_mapping_or_missing_period_fails_closed(field, value):
    document = bundle()
    document["responses"][0]["results"]["Data"][0][field] = value
    with pytest.raises(ContractError):
        parse_bundle(encode(document))


@pytest.mark.parametrize(
    "case",
    [
        "missing_operand",
        "duplicate",
        "revision",
        "nsa",
        "annual",
        "base",
        "request",
        "scale",
        "zero",
        "negative",
        "time",
        "order",
        "missing_notes",
        "error",
        "original",
        "missing_table",
        "unknown_provenance",
    ],
)
def test_unavailable_or_unqualified_source_is_not_a_publication(case):
    document = bundle()
    first, second = document["responses"]
    if case == "missing_operand":
        second["results"]["Data"].pop(1)
    elif case == "duplicate":
        second["results"]["Data"].append(second["results"]["Data"][0])
    elif case == "revision":
        second["results"]["Notes"][1]["NoteText"] = "SYNTHETIC other revision"
    elif case == "nsa":
        second["results"]["Notes"][0]["NoteText"] += " Not seasonally adjusted"
    elif case == "annual":
        first["results"]["Notes"][0]["NoteText"] += " Annual rates"
    elif case == "base":
        second["results"]["Notes"][0]["NoteText"] = (
            "Monthly Table 2.8.4 price indexes, seasonally adjusted, 2012=100"
        )
    elif case == "request":
        first["request"]["Frequency"] = "Q"
    elif case == "scale":
        second["results"]["Data"][0]["UNIT_MULT"] = "3"
    elif case == "zero":
        second["results"]["Data"][1]["DataValue"] = "0"
    elif case == "negative":
        second["results"]["Data"][1]["DataValue"] = "-1"
    elif case == "time":
        first["obtained_at"] = (BASE - timedelta(seconds=1)).isoformat()
    elif case == "order":
        second["requested_at"] = (BASE - timedelta(seconds=2)).isoformat()
    elif case == "missing_notes":
        second["results"]["Notes"] = []
    elif case == "error":
        first["results"] = {"Error": "SYNTHETIC"}
    elif case == "original":
        document["exact_original_retained"] = True
    elif case == "missing_table":
        document["responses"].pop()
    elif case == "unknown_provenance":
        document["provenance"] = "qualified_original"
    with pytest.raises(ContractError):
        candidate(document=document)


def captures(document, key):
    result = []
    for response in document["responses"]:
        params = dict(response["request"], UserID=key)
        envelope = {
            "Request": {
                "RequestParam": [
                    {"ParameterName": k.upper(), "ParameterValue": v}
                    for k, v in params.items()
                ]
            },
            "Results": response["results"],
        }
        result.append(
            ResponseCapture(
                response["request"]["TableName"],
                encode({"BEAAPI": envelope}),
                datetime.fromisoformat(response["requested_at"]),
                datetime.fromisoformat(response["obtained_at"]),
            )
        )
    return tuple(result)


def test_sanitizer_never_keeps_original_bytes_or_hash_or_request_envelope():
    key = "synthetic-bea-key-for-test-only"
    original = captures(bundle(), key)
    raw = retain_sanitized_bundle(original, credential=key, provenance="synthetic")
    assert key.encode() not in raw
    assert b"BEAAPI" not in raw and b"RequestParam" not in raw
    assert all(digest(c.response_bytes).encode() not in raw for c in original)
    assert json.loads(raw)["exact_original_retained"] is False
    assert raw == encode(bundle())


@pytest.mark.parametrize("echo", ["value", "field", "request"])
def test_sanitizer_rejects_echo_elsewhere_and_wrong_returned_request(echo):
    key = "synthetic-bea-key-for-test-only"
    document = bundle()
    if echo == "value":
        document["responses"][0]["results"]["Notes"][0]["NoteText"] += " " + key
    if echo == "field":
        document["responses"][0]["results"]["UserID"] = key
    original = captures(document, key)
    if echo == "request":
        envelope = json.loads(original[0].response_bytes)
        envelope["BEAAPI"]["Request"]["RequestParam"][0]["ParameterValue"] = "Wrong"
        original = (replace(original[0], response_bytes=encode(envelope)), original[1])
    with pytest.raises(ContractError) as caught:
        retain_sanitized_bundle(original, credential=key, provenance="synthetic")
    assert key not in str(caught.value)


def test_duplicate_json_keys_and_mutable_bytes_rejected():
    with pytest.raises(ContractError):
        parse_bundle(b'{"schema":1,"schema":2}')
    with pytest.raises(ContractError):
        parse_bundle(bytearray(encode(bundle())))


def test_source_projection_and_normalized_are_never_provider_original():
    retained = candidate()
    assert retained.plan.raw_material.grade == Grade.PROJECTED
    assert retained.plan.semantic_material.grade == Grade.NORMALIZED
    assert retained.plan.publication_request.claimed_grades == GRADES
    assert Grade.ORIGINAL not in PCE_BINDING_CANDIDATE.mappings[0].evidence.allowed
    assert retained.plan.acquisition.provider_data_attempts is None
    assert retained.plan.run.budget == 0


def test_materialization_and_no_source_exact_as_of_local_read():
    ledger, store, result = publish()
    snapshot = ledger.snapshot()
    reads = store.reads
    for exact in (None, result.version.identity):
        view = read_local_pce(
            ledger,
            reference=REFERENCE,
            as_of=BASE + timedelta(seconds=20),
            version_identity=exact,
        )
        assert view.version == result.version
        assert view.publication == result.publication
    assert ledger.snapshot() == snapshot and store.reads == reads
    with pytest.raises(PceReadRejected, match="as_of_unavailable"):
        read_local_pce(ledger, reference=REFERENCE, as_of=BASE)
    with pytest.raises(PceReadRejected, match="reference_not_admitted"):
        read_local_pce(ledger, reference="2026-08", as_of=BASE + timedelta(seconds=20))


def test_revisions_remain_exact_readable_and_as_of_uses_mdf_known_time():
    ledger, _, first = publish(retained=candidate("first"))
    document = bundle()
    document["responses"][1]["results"]["Data"][0]["DataValue"] = "111.000"
    for response in document["responses"]:
        response["results"]["Notes"][1]["NoteText"] = "SYNTHETIC revision label B"
    ledger, _, second = publish(ledger, candidate("second", document), Clock(30))
    old = read_local_pce(
        ledger, reference=REFERENCE, as_of=BASE + timedelta(seconds=20)
    )
    new = read_local_pce(
        ledger, reference=REFERENCE, as_of=BASE + timedelta(seconds=40)
    )
    assert old.version == first.version and new.version == second.version
    assert (
        read_local_pce(
            ledger,
            reference=REFERENCE,
            as_of=BASE + timedelta(seconds=40),
            version_identity=first.version.identity,
        )
        == old
    )
    assert (
        json.loads(new.content)["data"]["measurements"]["headline_yoy_sa"]["value"]
        == "11.000000"
    )


def test_equal_decision_time_is_ambiguous():
    def at():
        return BASE + timedelta(seconds=10)

    ledger, _, _ = publish(retained=candidate("first"), clock=at)
    publish(ledger, candidate("second"), at)
    with pytest.raises(PceReadRejected, match="ambiguous"):
        read_local_pce(ledger, reference=REFERENCE, as_of=BASE + timedelta(seconds=20))


@pytest.mark.parametrize(
    "mutation", ["source", "binding", "acquisition", "material", "content"]
)
def test_local_read_rebuilds_and_checks_complete_pce_lineage(mutation):
    ledger, _, result = publish()
    if mutation == "content":
        document = json.loads(result.plan.content)
        document["data"]["measurements"]["headline_yoy_sa"]["value"] = "99.999999"
        content = encode(document)
        version = replace(result.version, content_digest=digest(content))
        key = version.key
        ledger._records[(key.kind.value, key.identity, key.revision)] = codec.dumps(
            version
        )
        ledger._contents[key.identity] = (digest(content), content)
    else:
        record = {
            "source": replace(result.plan.source, owner="wrong"),
            "binding": replace(
                result.plan.binding_candidate, compatibility_policy="wrong"
            ),
            "acquisition": replace(result.plan.acquisition, request_identity="wrong"),
            "material": replace(result.plan.raw_material, named_class="wrong"),
        }[mutation]
        key = record.key
        ledger._records[(key.kind.value, key.identity, key.revision)] = codec.dumps(
            record
        )
    with pytest.raises((PceReadRejected, ReadContractError)):
        read_local_pce(ledger, reference=REFERENCE, as_of=BASE + timedelta(seconds=20))


def test_forged_plan_or_different_raw_rejected_before_store_effects():
    retained = candidate()
    store = InMemoryMaterialStore()
    plan = replace(retained.plan, source=replace(retained.plan.source, owner="wrong"))
    with pytest.raises(ContractError):
        materialize_persist_publish(
            CanonicalKernel(InMemoryLedger(), Clock()),
            plan,
            retained.evidence.raw_bytes,
            store,
            InMemoryWriterProtection(),
        )
    assert store.create_attempts == store.reads == 0


def test_version_without_publication_is_unavailable():
    from market_data_foundation.historical.bea_pce import (
        durable_plan,
        persist_before_publication,
    )

    retained = candidate()
    identity = MaterialIdentity(
        digest(retained.evidence.raw_bytes), len(retained.evidence.raw_bytes)
    )
    ledger = InMemoryLedger()
    plan = durable_plan(retained.plan, identity.locator)
    persist_before_publication(CanonicalKernel(ledger, Clock()), plan)
    with pytest.raises(PceReadRejected, match="as_of_unavailable"):
        read_local_pce(ledger, reference=REFERENCE, as_of=BASE + timedelta(seconds=20))


def test_job_duplicate_is_effect_free_and_restart_journal_is_read_only(tmp_path):
    job, journal, _, store, _, _ = rig(tmp_path)
    retained = candidate()
    submit(job, "pce-success", retained)
    result = run(job, "pce-success", retained)
    assert result["state"] == "succeeded"
    assert result["effects"]["source_candidate_validated"] is True
    assert not any(k.startswith("provider_") for k in result["effects"])
    creates, reads = store.create_attempts, store.reads
    assert run(job, "pce-success", retained) == result
    assert (store.create_attempts, store.reads) == (creates, reads)
    reopened = PceJobJournal(journal.directory, read_only=True)
    assert reopened.get("pce-success") == result


def test_opt_in_digest_and_live_budget_are_enforced(tmp_path):
    job, _, _, store, _, _ = rig(tmp_path)
    retained = candidate()
    submit(job, "pce-gates", retained)
    with pytest.raises(ContractError):
        job.run("pce-gates", enabled=False, preflight=lambda _: None, retained=retained)
    for change in (
        {"mode": "acquire"},
        {"max_provider_requests": 1},
        {"quality": ("provider_original",)},
        {"expected_raw_sha256": None},
    ):
        with pytest.raises(ContractError):
            PceJobSpec(
                **dict(
                    {"expected_raw_sha256": digest(retained.evidence.raw_bytes)},
                    **change,
                )
            )
    assert store.create_attempts == store.reads == 0


def test_preflight_failure_has_no_storage_effect_and_no_exception_message(tmp_path):
    job, _, _, store, _, _ = rig(tmp_path)
    retained = candidate()
    submit(job, "pce-preflight", retained)
    result = run(
        job,
        "pce-preflight",
        retained,
        lambda _: (_ for _ in ()).throw(OSError("private diagnostic must not appear")),
    )
    assert result["state"] == "failed" and result["effects"] == {}
    assert "private diagnostic" not in json.dumps(result)
    assert store.create_attempts == 0


def test_cancel_before_storage_then_explicit_retained_repair_preserves_plan(tmp_path):
    job, journal, _, store, _, clock = rig(tmp_path)
    retained = candidate()
    submit(job, "pce-cancel", retained)
    original_candidate = job._candidate

    def cancel_after_candidate(*args):
        result = original_candidate(*args)
        journal.cancel("pce-cancel", clock())
        return result

    job._candidate = cancel_after_candidate
    result = run(job, "pce-cancel", retained)
    assert result["state"] == "cancelled" and store.create_attempts == 0
    job._candidate = original_candidate
    repaired = job.repair("pce-cancel", "pce-repair", enabled=True)
    assert repaired["repair_of"] == "pce-cancel"
    assert run(job, "pce-repair", retained)["state"] == "succeeded"
    assert journal.get("pce-cancel")["plan"] == result["plan"]
    assert journal.get("pce-cancel")["state"] == "cancelled"
    with pytest.raises(ContractError):
        job.repair("pce-cancel", "pce-branch", enabled=True)


def test_interrupted_attempt_does_not_retry_or_claim_publication(tmp_path):
    job, journal, _, store, _, clock = rig(tmp_path)
    retained = candidate()
    submit(job, "pce-restart", retained)
    journal._update("pce-restart", clock(), state="running", stage="preflight")
    with pytest.raises(ContractError):
        run(job, "pce-restart", retained)
    result = job.reconcile("pce-restart", enabled=True)
    assert (
        result["state"] == "outcome_unknown"
        and result["reconciliation"]["outcome"] == "unresolved"
    )
    assert store.create_attempts == 0


def test_commit_unknown_confirmed_absence_can_repair_without_new_source(tmp_path):
    job, journal, ledger, store, protection, clock = rig(
        tmp_path, commit=lambda: (_ for _ in ()).throw(OSError("private failure"))
    )
    retained = candidate()
    submit(job, "pce-unknown", retained)
    original = run(job, "pce-unknown", retained)
    assert original["state"] == "outcome_unknown"
    with pytest.raises(ContractError):
        job.repair("pce-unknown", "pce-premature", enabled=True)
    reconciled = job.reconcile("pce-unknown", enabled=True)
    assert reconciled["reconciliation"]["outcome"] == "confirmed_not_published"
    assert reconciled["reconciliation"]["version_present"] is True
    assert reconciled["state"] == "outcome_unknown"
    job.repair("pce-unknown", "pce-repaired", enabled=True)
    job.commit = lambda: None
    assert run(job, "pce-repaired", retained)["state"] == "succeeded"
    assert journal.get("pce-unknown")["state"] == "outcome_unknown"
    assert store.create_attempts == 2


def test_publication_disconnect_fresh_controller_reconciles_without_rewriting_unknown(
    tmp_path,
):
    ledger = InMemoryLedger()
    calls = 0

    def readback():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("private readback failure")
        return nullcontext(InMemoryLedger.from_snapshot(ledger.snapshot()))

    job, journal, _, store, protection, clock = rig(
        tmp_path, ledger=ledger, readback=readback
    )
    retained = candidate()
    submit(job, "pce-published", retained)
    assert run(job, "pce-published", retained)["state"] == "outcome_unknown"
    fresh = PceLocalJob(
        PceJobJournal(journal.directory),
        clock,
        CanonicalKernel(ledger, clock),
        store,
        protection,
        lambda: nullcontext(InMemoryLedger.from_snapshot(ledger.snapshot())),
        lambda: None,
    )
    observed = fresh.reconcile("pce-published", enabled=True)
    assert observed["state"] == "outcome_unknown"
    assert observed["reconciliation"]["outcome"] == "confirmed_published"
    assert observed["last_success"] is not None
    assert store.create_attempts == 1
    with pytest.raises(ContractError):
        fresh.repair("pce-published", "pce-invalid", enabled=True)


def test_raw_put_unknown_reports_verified_presence_without_claiming_created(tmp_path):
    store = InMemoryMaterialStore()
    store.outcome_unknown_after_create = True
    job, *_ = rig(tmp_path, store=store)
    retained = candidate()
    submit(job, "pce-raw-unknown", retained)
    result = run(job, "pce-raw-unknown", retained)
    assert result["state"] == "succeeded"
    assert result["effects"]["raw_disposition"] == "unknown_reconciled_present"


def test_schema_is_exact_and_wrong_pg_target_has_no_connection_effects(tmp_path):
    assert validate_schema(DEV_SCHEMA) == DEV_SCHEMA
    with pytest.raises(ContractError):
        validate_schema(DEV_SCHEMA + "_extra")
    connection = SimpleNamespace(info=SimpleNamespace(host="127.0.0.1"))
    with pytest.raises(ContractError, match="nonproduction_target_required"):
        PceLocalJob(
            PceJobJournal(tmp_path / "wrong-pg"),
            Clock(),
            CanonicalKernel(PostgresLedger(connection, DEV_SCHEMA), Clock()),
            InMemoryMaterialStore(),
            InMemoryWriterProtection(),
            lambda: pytest.fail(),
            lambda: pytest.fail(),
        )


def test_partial_canonical_write_remains_unknown_until_observed(tmp_path):
    class VersionWrittenThenRaised(InMemoryLedger):
        fail = True

        def put(self, record):
            result = super().put(record)
            if record.key.kind.value == "DatasetVersion" and self.fail:
                raise OSError("synthetic lost write acknowledgement")
            return result

    ledger = VersionWrittenThenRaised()
    job, journal, _, store, _, _ = rig(tmp_path, ledger=ledger)
    retained = candidate()
    submit(job, "pce-partial", retained)
    result = run(job, "pce-partial", retained)
    assert result["state"] == "outcome_unknown"
    assert (
        ledger.publication_by_occurrence(retained.plan.publication_request.occurrence)
        is None
    )
    observed = job.reconcile("pce-partial", enabled=True)
    assert observed["reconciliation"]["outcome"] == "confirmed_not_published"
    assert observed["reconciliation"]["version_present"] is True
    ledger.fail = False
    job.repair("pce-partial", "pce-partial-repair", enabled=True)
    assert run(job, "pce-partial-repair", retained)["state"] == "succeeded"
    assert journal.get("pce-partial")["state"] == "outcome_unknown"
    assert store.create_attempts == 2


def test_changed_revision_has_new_job_scope_and_old_version_survives(tmp_path):
    job, _, ledger, _, _, _ = rig(tmp_path)
    first = candidate("first")
    document = bundle()
    document["responses"][1]["results"]["Data"][0]["DataValue"] = "112.000"
    second = candidate("second", document)
    submit(job, "pce-first", first)
    first_result = run(job, "pce-first", first)
    assert first_result["state"] == "succeeded"
    submit(job, "pce-second", second)
    assert run(job, "pce-second", second)["state"] == "succeeded"
    latest = read_local_pce(
        ledger, reference=REFERENCE, as_of=BASE + timedelta(seconds=30)
    )
    assert latest.version == second.plan.version
    assert (
        read_local_pce(
            ledger,
            reference=REFERENCE,
            version_identity=first.plan.version.identity,
            as_of=BASE + timedelta(seconds=30),
        ).version
        == first.plan.version
    )


def test_wrong_retained_digest_and_candidate_plan_do_not_write(tmp_path):
    job, _, _, store, _, _ = rig(tmp_path)
    retained = candidate()
    job.submit("pce-digest", PceJobSpec(expected_raw_sha256="0" * 64))
    assert run(job, "pce-digest", retained)["state"] == "failed"
    assert store.create_attempts == 0
    submit(job, "pce-plan", retained)
    forged = replace(
        retained,
        plan=replace(
            retained.plan, version=replace(retained.plan.version, completeness="wrong")
        ),
    )
    assert run(job, "pce-plan", forged)["state"] == "failed"
    assert store.create_attempts == 0


def test_cancel_before_preflight_requires_no_candidate_or_storage(tmp_path):
    job, journal, _, store, _, clock = rig(tmp_path)
    retained = candidate()
    submit(job, "pce-pending-cancel", retained)
    journal.cancel("pce-pending-cancel", clock())
    result = job.run(
        "pce-pending-cancel", enabled=True, preflight=lambda _: pytest.fail()
    )
    assert result["state"] == "cancelled" and result["effects"] == {}
    assert store.create_attempts == store.reads == 0


def test_read_rejects_publication_known_before_processing_time():
    ledger, _, result = publish()
    # Model a legacy corrupt record; the publish boundary now rejects its creation.
    publication = replace(
        result.publication,
        decision_at=BASE + timedelta(seconds=4),
        usable_at=BASE + timedelta(seconds=4),
    )
    key = publication.key
    ledger._records[(key.kind.value, key.identity, key.revision)] = codec.dumps(
        publication
    )
    with pytest.raises(ReadContractError, match="lineage_missing_or_mismatched"):
        read_local_pce(ledger, reference=REFERENCE, as_of=BASE + timedelta(seconds=20))


def test_repair_candidate_must_keep_exact_predecessor_plan(tmp_path):
    job, journal, _, store, _, clock = rig(tmp_path)
    retained = candidate("first")
    submit(job, "pce-original", retained)
    saved = job._candidate

    def cancel(*args):
        result = saved(*args)
        journal.cancel("pce-original", clock())
        return result

    job._candidate = cancel
    assert run(job, "pce-original", retained)["state"] == "cancelled"
    job._candidate = saved
    job.repair("pce-original", "pce-replacement", enabled=True)
    assert run(job, "pce-replacement", candidate("different-plan"))["state"] == "failed"
    assert store.create_attempts == 0


def test_restarted_unknown_without_lock_release_proof_stays_unresolved(tmp_path):
    job, journal, _, store, _, clock = rig(tmp_path)
    retained = candidate()
    submit(job, "pce-unproven", retained)
    journal._update("pce-unproven", clock(), state="running", plan={})
    observed = job.reconcile("pce-unproven", enabled=True)
    assert observed["state"] == "outcome_unknown"
    assert observed["reconciliation"]["outcome"] == "unresolved"
    assert store.create_attempts == store.reads == 0
