"""Offline producer order/SQL doubles; no actual PostgreSQL execution proof."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from market_data_foundation.canonical import operator_reads as producer
from market_data_foundation.canonical.codec import dumps, loads
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
)
from market_data_foundation.canonical.model import Key, Kind
from market_data_foundation.canonical.operator_asof import (
    OperatorQueryBudgetError,
    inspect_as_of,
)
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from tests.test_p6_consumer_read import _seed_daily
from tests.test_p6_operator_asof import AT, _store
from tests.test_p6_operator_query_bounds import _history, _unpublished


class SQLDouble:
    """Model query returns, and assert query shape separately from that model.

    This deliberately does not claim to execute the generated SQL. Independent
    real PostgreSQL qualification remains a separate gate.
    """

    def __init__(self, ledger):
        self.records = ledger._records
        self.publications = {}
        for key, payload in self.records.items():
            if key[0] == Kind.PUBLICATION.value:
                publication = loads(payload)
                self.publications[key[1]] = (
                    publication.request.version.identity,
                    payload,
                )
        self.calls = []
        self.returned = []

    def execute(self, query, parameters):
        statement = query.as_string()
        self.calls.append((statement, parameters))
        assert "WITH bounded AS MATERIALIZED" in statement
        assert "octet_length(convert_to(payload, 'UTF8'))" in statement
        assert "CASE WHEN n <= %s AND largest <= %s AND total <= %s" in statement
        assert "THEN payload ELSE NULL END" in statement
        assert "FROM bounded CROSS JOIN admission" in statement
        assert "LIMIT %s" in statement
        assert "ORDER BY" not in statement
        assert not any(word in statement for word in ("INSERT", "UPDATE", "DELETE"))
        filters = parameters[:-4]
        probe, maximum, single_bytes, remaining = parameters[-4:]
        assert probe == maximum + 1
        if '."publications"' in statement:
            if "version_identity = %s" in statement:
                candidates = [
                    p for v, p in self.publications.values() if v == filters[0]
                ]
            else:
                assert "identity = %s" in statement
                value = self.publications.get(filters[0])
                candidates = [] if value is None else [value[1]]
        else:
            assert '."records"' in statement
            if "revision = %s" in statement:
                assert "kind = %s AND identity = %s AND revision = %s" in statement
                value = self.records.get(filters)
                candidates = [] if value is None else [value]
            else:
                assert filters in ((Kind.VERSION.value,), (Kind.BINDING.value,))
                candidates = [p for k, p in self.records.items() if k[0] == filters[0]]
        candidates = candidates[:probe]
        sizes = [len(p.encode("utf-8")) for p in candidates]
        allowed = (
            len(candidates) <= maximum
            and max(sizes, default=0) <= single_bytes
            and sum(sizes) <= remaining
        )
        rows = [
            (p if allowed else None, size, len(candidates), sum(sizes))
            for p, size in zip(candidates, sizes, strict=True)
        ]
        self.returned.append(rows)
        return SimpleNamespace(fetchall=lambda: rows)


def _source(ledger, backend):
    if backend == "memory":
        return ledger
    return PostgresLedger(SQLDouble(ledger), "mdf_p3_canonical_kernel_r1_operator_test")


@pytest.fixture(params=["memory", "sql_double"])
def backend(request):
    return request.param


@pytest.fixture
def decoded(monkeypatch):
    seen = []
    original = producer.loads

    def observe(payload):
        seen.append(payload)
        return original(payload)

    monkeypatch.setattr(producer, "loads", observe)
    return seen


def _budget(source):
    with pytest.raises(OperatorQueryBudgetError) as caught:
        inspect_as_of(source, RAW_DAILY_BARS.key, as_of=AT)
    assert caught.value.args == ("operator_query_budget_exceeded",)
    assert vars(caught.value) == {"classification": caught.value.classification}


def test_missing_capability_fails_without_any_legacy_call():
    class Legacy:
        def get(self, *_args):
            pytest.fail("unbounded fallback")

        versions_for_definition = publications_for_version = get

    with pytest.raises(ReadContractError) as caught:
        inspect_as_of(Legacy(), RAW_DAILY_BARS.key, as_of=AT)
    assert caught.value.classification is ReadFailure.STORAGE_UNAVAILABLE


def test_operator_never_uses_legacy_reads(backend):
    ledger, _, _, _ = _seed_daily()
    source = _source(ledger, backend)

    def forbidden(*_args):
        pytest.fail("legacy path or write/content access")

    for name in (
        "get",
        "versions_for_definition",
        "publications_for_version",
        "put",
        "publish",
        "put_content",
        "get_content",
    ):
        setattr(source, name, forbidden)
    assert inspect_as_of(source, RAW_DAILY_BARS.key, as_of=AT).selection_outcome == (
        "one_publication_candidate"
    )


@pytest.mark.parametrize("count", [128, 129])
def test_all_versions_include_other_definitions_before_decode(backend, decoded, count):
    ledger, plan = _unpublished()
    for number in range(1, count):
        _store(
            ledger,
            replace(
                plan.version,
                identity=f"other:{number:04d}",
                definition=BLS_CPI_HEADLINES.key,
            ),
        )
    source = _source(ledger, backend)
    session = source.operator_reads()
    if count == 129:
        with pytest.raises(producer.OperatorReadAdmissionError):
            session.versions_for_definition(RAW_DAILY_BARS.key, 128)
        assert decoded == []
        _budget(source)
    else:
        assert session.versions_for_definition(RAW_DAILY_BARS.key, 128) == (
            plan.version,
        )
        assert len(decoded) == 128
        assert len(inspect_as_of(source, RAW_DAILY_BARS.key, as_of=AT).candidates) == 1
    if backend == "sql_double" and count == 129:
        assert all(row[0] is None for row in source.connection.returned[0])
        assert len(source.connection.returned[0]) == 129


@pytest.mark.parametrize("count", [512, 513])
def test_history_probe_before_decode_and_complete_clocks(backend, decoded, count):
    ledger, _, plan, initial = _seed_daily()
    _history(ledger, plan, initial, count)
    source = _source(ledger, backend)
    session = source.operator_reads()
    if count == 513:
        with pytest.raises(producer.OperatorReadAdmissionError):
            session.publications_for_version(plan.version.key, 512)
        assert decoded == []
        _budget(source)
    else:
        assert len(session.publications_for_version(plan.version.key, 512)) == 512
        assert len(decoded) == 512
        view = inspect_as_of(source, RAW_DAILY_BARS.key, as_of=AT)
        assert len(view.candidates[0].publications) == 512
        assert view.candidates[0].publications[-1].pending_clocks == (
            "decision_at",
            "usable_at",
        )


@pytest.mark.parametrize("overflow", [False, True])
def test_shared_history_budget_sql_uses_remaining_and_never_truncates(
    backend, decoded, overflow
):
    ledger, _, plan, initial = _seed_daily("a")
    _history(ledger, plan, initial, 256)
    # Build independent chains, then disambiguate the shared helper identities.
    second, _, second_plan, second_initial = _seed_daily("z")
    _history(second, second_plan, second_initial, 256 + int(overflow))
    for key, payload in second._records.items():
        if key[0] == Kind.PUBLICATION.value:
            p = loads(payload)
            request = replace(
                p.request,
                identity="z:" + p.key.identity,
                occurrence="z:" + p.request.occurrence,
                predecessor=None
                if p.request.predecessor is None
                else Key(Kind.PUBLICATION, "z:" + p.request.predecessor.identity),
            )
            _store(ledger, replace(p, request=request))
        else:
            ledger._records[key] = payload
    source = _source(ledger, backend)
    if overflow:
        _budget(source)
    else:
        view = inspect_as_of(source, RAW_DAILY_BARS.key, as_of=AT)
        assert [len(c.publications) for c in view.candidates] == [256, 256]
    if backend == "sql_double":
        histories = [
            params
            for statement, params in source.connection.calls
            if "version_identity = %s" in statement
        ]
        assert [params[-3] for params in histories] == [512, 256]
        if overflow:
            rows = source.connection.returned[-1]
            assert len(rows) == 257 and all(row[0] is None for row in rows)
            assert all('"identity":"z:history:' not in p for p in decoded)


@pytest.mark.parametrize("multibyte", [False, True])
@pytest.mark.parametrize("overflow", [False, True])
def test_exact_payload_byte_boundary_before_decode(
    backend, decoded, multibyte, overflow, monkeypatch
):
    ledger, plan = _unpublished()
    payload = json.dumps(json.loads(dumps(plan.version)), ensure_ascii=False)
    if multibyte:
        payload = payload.replace(plan.version.completeness, "界😀")
    size = len(payload.encode("utf-8"))
    ledger._records[(Kind.VERSION.value, plan.version.identity, "")] = payload
    monkeypatch.setattr(producer, "MAX_PAYLOAD_BYTES", size - int(overflow))
    source = _source(ledger, backend)
    if overflow:
        with pytest.raises(producer.OperatorReadAdmissionError):
            source.operator_reads().get(plan.version.key)
        assert decoded == []
    else:
        assert source.operator_reads().get(plan.version.key).key == plan.version.key
        assert decoded == [payload]
    if backend == "sql_double" and overflow:
        assert source.connection.returned[-1][0][0] is None
    if multibyte:
        assert size > len(payload)


@pytest.mark.parametrize("overflow", [False, True])
def test_aggregate_payload_bytes_repeated_gets(backend, decoded, monkeypatch, overflow):
    ledger, plan = _unpublished()
    size = len(dumps(plan.version).encode("utf-8"))
    monkeypatch.setattr(producer, "MAX_TOTAL_BYTES", 2 * size - int(overflow))
    source = _source(ledger, backend)
    session = source.operator_reads()
    session.get(plan.version.key)
    if overflow:
        with pytest.raises(producer.OperatorReadAdmissionError):
            session.get(plan.version.key)
        assert len(decoded) == 1
        assert session.admission.bytes_read == size
    else:
        session.get(plan.version.key)
        assert len(decoded) == 2
        assert session.admission.bytes_read == 2 * size
    assert source.operator_reads().get(plan.version.key).key == plan.version.key


def test_batch_aggregate_and_late_bad_payload_never_decode_first_row(
    backend, decoded, monkeypatch
):
    ledger, plan = _unpublished()
    _store(ledger, replace(plan.version, identity="z-other"))
    size = sum(
        len(p.encode("utf-8"))
        for k, p in ledger._records.items()
        if k[0] == Kind.VERSION.value
    )
    monkeypatch.setattr(producer, "MAX_TOTAL_BYTES", size - 1)
    source = _source(ledger, backend)
    with pytest.raises(producer.OperatorReadAdmissionError):
        source.operator_reads().versions_for_definition(RAW_DAILY_BARS.key, 128)
    assert decoded == []
    if backend == "sql_double":
        assert all(row[0] is None for row in source.connection.returned[-1])


@pytest.mark.parametrize("stage", ["definition", "version", "publication", "run"])
def test_default_oversize_and_depth_fail_before_json_and_canonical_decode(
    backend, decoded, stage
):
    ledger, _, plan, publication = _seed_daily()
    key = {
        "definition": RAW_DAILY_BARS.key,
        "version": plan.version.key,
        "publication": publication.key,
        "run": plan.run.key,
    }[stage]
    source = _source(ledger, backend)
    payload = "x" * (producer.MAX_PAYLOAD_BYTES + 1)
    if backend == "sql_double" and stage == "publication":
        source.connection.publications[key.identity] = (plan.version.identity, payload)
    else:
        ledger._records[(key.kind.value, key.identity, key.revision)] = payload
    _budget(source)
    assert payload not in decoded
    # A small but deeply nested payload must stop before even json.loads.
    deep = "[" * 65 + "null" + "]" * 65
    if backend == "sql_double" and stage == "publication":
        source.connection.publications[key.identity] = (plan.version.identity, deep)
    else:
        ledger._records[(key.kind.value, key.identity, key.revision)] = deep
    _budget(source)
    assert deep not in decoded


def test_json_depth_quoted_brackets_escapes_and_inclusive_boundary(
    decoded, monkeypatch
):
    admission = producer._Admission()
    assert admission.check(json.dumps('"\\' + "[" * 1000)) > 0
    assert admission.check("[" * 64 + "0" + "]" * 64) == 129
    with pytest.raises(producer.OperatorReadAdmissionError):
        admission.check("[" * 65 + "0" + "]" * 65)
    # Valid canonical tuple tags also hit the guard before recursive decode.
    value = None
    for _ in range(33):
        value = {"type": "tuple", "value": [value]}
    ledger, plan = _unpublished()
    payload = json.dumps(value)
    ledger._records[(Kind.VERSION.value, plan.version.identity, "")] = payload
    with pytest.raises(producer.OperatorReadAdmissionError):
        ledger.operator_reads().get(plan.version.key)
    assert decoded == []


@pytest.mark.parametrize("failure", ["malformed", "missing", "conflict"])
def test_late_failure_classification_no_partial_results(backend, failure, monkeypatch):
    from market_data_foundation.canonical import operator_asof

    ledger, _, _, _ = _seed_daily("a")
    second, _, plan, _ = _seed_daily("z")
    ledger._records.update(second._records)
    source = _source(ledger, backend)
    key = plan.run.key
    if failure == "missing":
        ledger._records.pop((key.kind.value, key.identity, key.revision))
    else:
        payload = (
            "private malformed"
            if failure == "malformed"
            else dumps(replace(ledger.get(key), identity="private-conflict"))
        )
        ledger._records[(key.kind.value, key.identity, key.revision)] = payload
    projected = []
    original = operator_asof.VersionDiagnostic

    def observe(**kwargs):
        projected.append(kwargs["version"])
        return original(**kwargs)

    monkeypatch.setattr(operator_asof, "VersionDiagnostic", observe)
    with pytest.raises(ReadContractError) as caught:
        inspect_as_of(source, RAW_DAILY_BARS.key, as_of=AT)
    assert projected == [Key(Kind.VERSION, "a:version")]
    assert caught.value.classification is ReadFailure.LINEAGE_MISSING_OR_MISMATCHED
    assert caught.value.args == ("lineage_missing_or_mismatched",)


def test_memory_slot_boundary_counts_other_kinds_before_any_version_decode(
    decoded, monkeypatch
):
    ledger, plan = _unpublished()
    count = len(ledger._records)
    monkeypatch.setattr(producer, "MAX_MEMORY_SLOTS", count)
    session = ledger.operator_reads()
    assert session.versions_for_definition(RAW_DAILY_BARS.key, 128) == (plan.version,)
    assert session.admission.slots == count
    decoded.clear()
    monkeypatch.setattr(producer, "MAX_MEMORY_SLOTS", count - 1)
    with pytest.raises(producer.OperatorReadAdmissionError):
        ledger.operator_reads().versions_for_definition(RAW_DAILY_BARS.key, 128)
    assert decoded == []


def test_zero_remaining_sql_history_still_probes_for_one_overflow(decoded):
    ledger, _, plan, _ = _seed_daily()
    source = _source(ledger, "sql_double")
    with pytest.raises(producer.OperatorReadAdmissionError):
        source.operator_reads().publications_for_version(plan.version.key, 0)
    assert source.connection.calls[-1][1][-4:-2] == (1, 0)
    assert source.connection.returned[-1][0][0] is None
    assert decoded == []


@pytest.mark.parametrize("overflow", [False, True])
def test_default_one_mib_boundary_with_serialized_whitespace(
    backend, decoded, overflow
):
    ledger, plan = _unpublished()
    encoded = dumps(plan.version)
    payload = encoded + " " * (
        producer.MAX_PAYLOAD_BYTES - len(encoded) + int(overflow)
    )
    ledger._records[(Kind.VERSION.value, plan.version.identity, "")] = payload
    source = _source(ledger, backend)
    if overflow:
        with pytest.raises(producer.OperatorReadAdmissionError):
            source.operator_reads().get(plan.version.key)
        assert decoded == []
    else:
        assert source.operator_reads().get(plan.version.key) == plan.version
        assert decoded == [payload]


def test_default_64_mib_boundary_counts_repeated_readback(backend, decoded):
    ledger, plan = _unpublished()
    encoded = dumps(plan.version)
    payload = encoded + " " * (producer.MAX_PAYLOAD_BYTES - len(encoded))
    ledger._records[(Kind.VERSION.value, plan.version.identity, "")] = payload
    source = _source(ledger, backend)
    session = source.operator_reads()
    for _ in range(64):
        assert session.get(plan.version.key) == plan.version
    assert session.admission.bytes_read == 67108864
    with pytest.raises(producer.OperatorReadAdmissionError):
        session.get(plan.version.key)
    assert len(decoded) == 64
    if backend == "sql_double":
        assert source.connection.calls[-1][1][-1] == 0
        assert source.connection.returned[-1][0][0] is None


@pytest.mark.parametrize("shape", ["oversize", "deep"])
def test_late_bad_batch_payload_prevents_any_decode(backend, decoded, shape):
    ledger, plan = _unpublished()
    payload = (
        "x" * (producer.MAX_PAYLOAD_BYTES + 1)
        if shape == "oversize"
        else "[" * 65 + "0" + "]" * 65
    )
    ledger._records[(Kind.VERSION.value, "z", "")] = payload
    source = _source(ledger, backend)
    with pytest.raises(producer.OperatorReadAdmissionError):
        source.operator_reads().versions_for_definition(RAW_DAILY_BARS.key, 128)
    assert decoded == []


@pytest.mark.parametrize("wrong_revision", [False, True])
def test_exact_key_revision_and_payload_conflict(backend, wrong_revision):
    from tests.test_p6_consumer_read import _seed_bound_cpi

    ledger, plan, _ = _seed_bound_cpi()
    key = plan.source.key
    source = _source(ledger, backend)
    if wrong_revision:
        payload = ledger._records.pop((key.kind.value, key.identity, key.revision))
        ledger._records[(key.kind.value, key.identity, "private-r2")] = payload
    else:
        ledger._records[(key.kind.value, key.identity, key.revision)] = dumps(
            replace(plan.source, contract_revision="private-r2")
        )
    with pytest.raises(ReadContractError) as caught:
        inspect_as_of(source, BLS_CPI_HEADLINES.key, as_of=AT)
    assert caught.value.classification is ReadFailure.LINEAGE_MISSING_OR_MISMATCHED
    if backend == "sql_double":
        assert any(
            parameters[:3] == (key.kind.value, key.identity, key.revision)
            for _, parameters in source.connection.calls
        )
