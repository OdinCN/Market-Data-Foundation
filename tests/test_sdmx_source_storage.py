"""Synthetic wire and fault injection only; current official compatibility unproven."""

import copy
import json
import socket
import ssl
from dataclasses import replace
from datetime import timedelta
from itertools import count
from types import SimpleNamespace

import psycopg
import pytest
from test_sdmx_official import (
    AT,
    END,
    START,
    assert_fresh_process_read,
    candidate,
    changed,
    plan,
    read,
)

from market_data_foundation.canonical import codec
from market_data_foundation.canonical.consumer_read import ReadContractError
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, digest
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import sdmx_materialization as storage
from market_data_foundation.historical import sdmx_source as source
from market_data_foundation.historical.bls_cpi_material_store import (
    InMemoryMaterialStore,
    InMemoryWriterProtection,
    MaterialIdentity,
    MaterialWriteOutcomeUnknown,
)
from market_data_foundation.historical.sdmx_local_read import SdmxReadRejected
from market_data_foundation.historical.sdmx_official import build_plan, project
from market_data_foundation.source_request_policy import SourceRequestFailed


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("T08 deterministic test attempted external IO")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


def source_plan():
    return source.SourcePlan(START, END)


def clock():
    sequence = count()
    return lambda: AT + timedelta(seconds=next(sequence))


class Exchange:
    def __init__(self, candidates=None, faults=None):
        self.candidates = candidates or {p: candidate(p) for p in ("ECB", "ESTAT")}
        self.faults = dict(faults or {})
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        fault = self.faults.pop(len(self.calls), None)
        if isinstance(fault, BaseException):
            raise fault
        if fault is not None:
            return source.Response(fault, b"")
        entity = next(
            e
            for e in self.candidates[request.provider].entities
            if e.role == request.role
        )
        assert entity.url == request.url
        return source.Response(
            200,
            entity.raw,
            (
                ("content-type", request.accept),
                ("content-length", str(len(entity.raw))),
            ),
        )


class Protection:
    def __init__(self):
        self.owners = {}
        self.tickets = []

    def acquire(self, identity):
        owner = self.owners.setdefault(identity, InMemoryWriterProtection())
        ticket = owner.acquire(identity)
        self.tickets.append(ticket)
        return ticket

    @property
    def held(self):
        return sum(t._held for t in self.tickets)


def fresh(ledger):
    return InMemoryLedger.from_snapshot(ledger.snapshot())


def test_default_off_has_no_files_or_effects(tmp_path):
    acquisition = source.Acquisition(tmp_path / "artifacts" / "source", source_plan())
    handoff = storage.Handoff(tmp_path / "artifacts" / "storage")
    exchange = Exchange()
    with pytest.raises(ContractError, match="opt_in"):
        acquisition.acquire("ECB", exchange=exchange)
    with pytest.raises(ContractError, match="opt_in"):
        handoff.materialize(None, None, None, None)
    with pytest.raises(ContractError, match="opt_in"):
        handoff.reconcile("ECB", None, None, None)
    assert not list(tmp_path.iterdir()) and not exchange.calls


def test_exact_seven_budget_replay_and_end_to_end(tmp_path):
    root = tmp_path / "artifacts"
    exchange = Exchange(faults={1: 503, 2: TimeoutError("unsafe detail")})
    acquisition = source.Acquisition(root / "source", source_plan())
    sleeps, candidates = [], []
    current = clock()
    for provider in ("ECB", "ESTAT"):
        candidates.append(
            acquisition.acquire(
                provider,
                enabled=True,
                exchange=exchange,
                sleep=sleeps.append,
                clock=current,
            )
        )
    assert sleeps == [1, 2]
    assert acquisition.counts() == {
        "logical": 7,
        "attempts": 9,
        "retries": 2,
        "fallbacks": 0,
    }
    # New object/process identity replays the same durable budget, not a reset.
    resumed = source.Acquisition(root / "source", source_plan())
    for c in candidates:
        assert (
            resumed.acquire(
                c.provider, enabled=True, exchange=lambda _: pytest.fail("requery")
            )
            == c
        )
    assert len(exchange.calls) == 9
    ledger, store, protection = InMemoryLedger(), InMemoryMaterialStore(), Protection()
    kernel = CanonicalKernel(ledger, lambda: AT + timedelta(seconds=100))
    handoff = storage.Handoff(root / "storage")
    for c in candidates:
        p = build_plan(
            c,
            identity="t08-" + c.provider,
            finished_at=AT + timedelta(seconds=50),
            frozen_at=AT + timedelta(seconds=60),
        )
        durable, publication = handoff.materialize(
            kernel, p, store, protection, enabled=True
        )
        assert all(
            m.durable and m.replayable and "/t08-sdmx/" in m.controlled_ref
            for m in durable.raw_materials
        )
        assert publication.decision_at >= durable.version.frozen_at
        assert project(c)["provider_vintage"] is None
        assert not read(fresh(ledger), c.provider).definition.source_qualified
        exact = read(fresh(ledger), c.provider, version_identity=p.version.identity)
        assert exact == read(fresh(ledger), c.provider)
        assert_fresh_process_read(ledger, c.provider, p, rejected=False)
        observation = storage.Handoff(root / "storage").reconcile(
            c.provider, fresh(ledger), store, protection, enabled=True
        )
        assert observation == {
            "outcome": "confirmed_published",
            "raw_present": [True] * len(p.raw_materials),
            "lock_release_proven": True,
        }
    assert protection.held == 0 and store.create_attempts == 7
    assert len(ledger.versions_for_definition(read(ledger).definition.key)) == 1
    assert len(exchange.calls) == 9


@pytest.mark.parametrize(
    "fault,attempts,sleeps",
    [
        (408, 3, [1, 2]),
        (429, 3, [1, 2]),
        (500, 3, [1, 2]),
        (502, 3, [1, 2]),
        (503, 3, [1, 2]),
        (504, 3, [1, 2]),
        (TimeoutError("hidden"), 3, [1, 2]),
        (301, 1, []),
        (302, 1, []),
        (401, 1, []),
        (403, 1, []),
        (404, 1, []),
        (400, 1, []),
        (ssl.SSLCertVerificationError("hidden"), 1, []),
    ],
)
def test_shared_retry_budget_and_no_restart_requery(tmp_path, fault, attempts, sleeps):
    acquisition = source.Acquisition(tmp_path / "artifacts", source_plan())
    exchange = Exchange(faults={1: fault, 2: fault, 3: fault})
    observed = []
    with pytest.raises(SourceRequestFailed) as error:
        acquisition.acquire(
            "ECB", enabled=True, exchange=exchange, sleep=observed.append, clock=clock()
        )
    assert error.value.attempts == attempts and observed == sleeps
    assert "hidden" not in str(error.value)
    with pytest.raises(ContractError, match="consumed_slot"):
        source.Acquisition(acquisition.root, source_plan()).acquire(
            "ECB", enabled=True, exchange=exchange
        )
    assert len(exchange.calls) == attempts
    assert acquisition.counts() == {
        "logical": 1,
        "attempts": attempts,
        "retries": attempts - 1,
        "fallbacks": 0,
    }


@pytest.mark.parametrize(
    "provider,role",
    [
        (p, r)
        for p in ("ECB", "ESTAT")
        for r in (
            ("flow", "dsd", "data") if p == "ECB" else ("flow", "dsd", "unit", "data")
        )
    ],
)
def test_each_http200_retained_before_parse_and_reused(tmp_path, provider, role):
    c = candidate(provider)
    index = tuple(e.role for e in c.entities).index(role)
    broken = replace(
        c,
        entities=tuple(
            replace(e, raw=b"not xml") if e.role == role else e for e in c.entities
        ),
    )
    root = tmp_path / "artifacts"
    acquisition, exchange = (
        source.Acquisition(root, source_plan()),
        Exchange({provider: broken}),
    )
    with pytest.raises((ContractError, ValueError)):
        acquisition.acquire(provider, enabled=True, exchange=exchange, clock=clock())
    assert (root / (provider + "." + role + ".bin")).read_bytes() == b"not xml"
    assert json.loads((root / (provider + "." + role + ".json")).read_bytes())[
        "sha256"
    ] == digest(b"not xml")
    calls = len(exchange.calls)
    with pytest.raises((ContractError, ValueError)):
        source.Acquisition(root, source_plan()).acquire(
            provider, enabled=True, exchange=exchange
        )
    assert len(exchange.calls) == calls == index + 1


def test_current_unit_mismatch_stops_before_data(tmp_path):
    broken = changed(candidate("ESTAT"), 1, b"Index, 2025=100", b"Index, 2015=100")
    exchange = Exchange({"ESTAT": broken})
    acquisition = source.Acquisition(tmp_path / "artifacts", source_plan())
    with pytest.raises(ContractError, match="meaning"):
        acquisition.acquire("ESTAT", enabled=True, exchange=exchange, clock=clock())
    assert [r.role for r in exchange.calls] == ["flow", "dsd", "unit"]


@pytest.mark.parametrize(
    "headers",
    [
        (("content-type", "application/json"),),
        (("content-type", source.STRUCTURE_MEDIA), ("content-encoding", "gzip")),
        (("content-type", source.STRUCTURE_MEDIA), ("content-length", "1")),
        (
            ("content-type", source.STRUCTURE_MEDIA),
            ("Content-Type", source.STRUCTURE_MEDIA),
        ),
        (
            ("content-type", source.STRUCTURE_MEDIA),
            ("transfer-encoding", "chunked"),
            ("content-length", "1"),
        ),
    ],
)
def test_http200_invalid_headers_still_retained_no_retry(tmp_path, headers):
    root = tmp_path / "artifacts"
    calls = []

    def exchange(request):
        calls.append(request)
        return source.Response(200, b"unmodified bytes", headers)

    with pytest.raises(ContractError):
        source.Acquisition(root, source_plan()).acquire(
            "ECB", enabled=True, exchange=exchange, clock=clock()
        )
    assert (root / "ECB.flow.bin").read_bytes() == b"unmodified bytes"
    with pytest.raises(ContractError):
        source.Acquisition(root, source_plan()).acquire(
            "ECB", enabled=True, exchange=exchange
        )
    assert len(calls) == 1


def test_retention_failure_consumes_successful_slot(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    original = source.write_json

    def fail_receipt(path, document):
        if path.name == "ECB.flow.json":
            raise OSError("hidden disk detail")
        return original(path, document)

    monkeypatch.setattr(source, "write_json", fail_receipt)
    exchange = Exchange()
    with pytest.raises(OSError):
        source.Acquisition(root, source_plan()).acquire(
            "ECB", enabled=True, exchange=exchange, clock=clock()
        )
    monkeypatch.setattr(source, "write_json", original)
    with pytest.raises(ContractError, match="consumed_slot"):
        source.Acquisition(root, source_plan()).acquire(
            "ECB", enabled=True, exchange=exchange
        )
    assert len(exchange.calls) == 1


def test_budget_identity_and_receipt_corruption_fail_closed(tmp_path):
    root, exchange = tmp_path / "artifacts", Exchange()
    acquisition = source.Acquisition(root, source_plan())
    acquisition.acquire("ECB", enabled=True, exchange=exchange, clock=clock())
    with pytest.raises(ContractError, match="plan_mismatch"):
        source.Acquisition(root, replace(source_plan(), end="2026-09-29")).acquire(
            "ECB", enabled=True, exchange=exchange
        )
    (root / "ECB.flow.bin").write_bytes(b"corrupt")
    with pytest.raises(ContractError, match="receipt_mismatch"):
        acquisition.acquire("ECB", enabled=True, exchange=exchange)
    assert len(exchange.calls) == 3


@pytest.mark.parametrize(
    "phase",
    [
        "partial_raw",
        "unknown_raw_present",
        "unknown_raw_absent",
        "partial_ledger",
        "commit",
        "publication_before",
        "publication_after",
        "release",
    ],
)
@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
def test_partial_effects_reconcile_no_automatic_writes(tmp_path, phase, provider):
    class Store(InMemoryMaterialStore):
        def create_if_absent(self, identity, payload):
            if self.create_attempts == 1 and phase == "partial_raw":
                raise RuntimeError("hidden")
            if phase == "unknown_raw_absent":
                self.create_attempts += 1
                raise MaterialWriteOutcomeUnknown()
            result = super().create_if_absent(identity, payload)
            if phase == "unknown_raw_present":
                raise MaterialWriteOutcomeUnknown()
            return result

    class Ledger(InMemoryLedger):
        def put(self, record):
            if phase == "partial_ledger" and len(self._records) == 5:
                raise RuntimeError("hidden")
            return super().put(record)

        def publish(self, request, clock):
            if phase == "publication_before":
                raise RuntimeError("hidden")
            publication = super().publish(request, clock)
            if phase == "publication_after":
                raise RuntimeError("hidden")
            return publication

    p, ledger, store, protection = plan(provider), Ledger(), Store(), Protection()
    handoff = storage.Handoff(tmp_path / "artifacts")
    if phase == "release":
        original = handoff._release

        def fail_once(provider):
            handoff._release = original
            raise RuntimeError("hidden")

        handoff._release = fail_once

    def commit():
        if phase == "commit":
            raise RuntimeError("hidden")

    if phase == "unknown_raw_present":
        handoff.materialize(
            CanonicalKernel(ledger, lambda: AT + timedelta(seconds=100)),
            p,
            store,
            protection,
            enabled=True,
        )
    else:
        with pytest.raises(storage.HandoffUnknown):
            handoff.materialize(
                CanonicalKernel(ledger, lambda: AT + timedelta(seconds=100)),
                p,
                store,
                protection,
                enabled=True,
                authoritative_commit=commit,
            )
        assert protection.held > 0
    before = (store.create_attempts, ledger.snapshot())
    with pytest.raises(ContractError, match="consumed"):
        handoff.materialize(
            CanonicalKernel(ledger, lambda: AT), p, store, protection, enabled=True
        )
    with pytest.raises(ContractError, match="independent"):
        handoff.reconcile(provider, ledger, store, protection, enabled=True)
    result = handoff.reconcile(provider, fresh(ledger), store, protection, enabled=True)
    assert result["outcome"] == (
        "confirmed_published"
        if phase in {"unknown_raw_present", "publication_after", "release"}
        else "confirmed_not_published"
    )
    assert (store.create_attempts, ledger.snapshot()) == before and protection.held == 0
    assert (handoff.root / (provider + ".handoff.json")).exists()
    # Even successful reconciliation does not make a terminal intent reusable.
    with pytest.raises(ContractError, match="consumed"):
        handoff.materialize(
            CanonicalKernel(ledger, lambda: AT), p, store, protection, enabled=True
        )


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
def test_process_restart_requires_old_protection_release(tmp_path, provider):
    root = tmp_path / "artifacts"
    p, ledger, store, protection = (
        plan(provider),
        InMemoryLedger(),
        InMemoryMaterialStore(),
        Protection(),
    )
    handoff = storage.Handoff(root)

    def fail():
        raise RuntimeError("hidden")

    with pytest.raises(storage.HandoffUnknown):
        handoff.materialize(
            CanonicalKernel(ledger, lambda: AT),
            p,
            store,
            protection,
            enabled=True,
            authoritative_commit=fail,
        )
    recovered = storage.Handoff(root)
    with pytest.raises(ContractError, match="busy"):
        recovered.reconcile(provider, fresh(ledger), store, protection, enabled=True)
    # Model process death releasing OS/session protection; no provider IO.
    for ticket in protection.tickets:
        ticket.force_gap_for_counterexample()
    assert (
        recovered.reconcile(provider, fresh(ledger), store, protection, enabled=True)[
            "outcome"
        ]
        == "confirmed_not_published"
    )
    assert store.create_attempts == len(p.raw_materials) and protection.held == 0


def test_storage_scope_and_no_other_product_addressing(tmp_path):
    p = plan()
    ids = storage.raw_identities(p)
    assert all(i.key.startswith("t08-sdmx/") for i in ids)
    assert all(i.key != MaterialIdentity(i.sha256, 1).key for i in ids)
    with pytest.raises(ContractError, match="key_invalid"):
        storage.SdmxMaterialIdentity(
            ids[0].sha256, 1, key=MaterialIdentity(ids[0].sha256, 1).key
        )
    for host, schema in [
        ("192.168.10.4", "mdf_macro_fred_alfred_vintage_r1"),
        ("localhost", storage.SCHEMA),
    ]:
        ledger = PostgresLedger(
            SimpleNamespace(info=SimpleNamespace(host=host)), schema
        )
        with pytest.raises(ContractError, match="scope"):
            storage.Handoff(tmp_path / "artifacts").materialize(
                CanonicalKernel(ledger, lambda: AT), p, None, None, enabled=True
            )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("fault", ["raw", "locator", "acquisition", "content"])
def test_durable_read_and_reconciliation_fail_closed(tmp_path, fault):
    p, ledger, store, protection = (
        plan(),
        InMemoryLedger(),
        InMemoryMaterialStore(),
        Protection(),
    )
    handoff = storage.Handoff(tmp_path / "artifacts")
    durable, _ = handoff.materialize(
        CanonicalKernel(ledger, lambda: AT + timedelta(seconds=100)),
        p,
        store,
        protection,
        enabled=True,
    )
    if fault == "raw":
        identity = storage.raw_identities(p)[0]
        store._objects[(identity.bucket, identity.key)] = b"corrupt"
    elif fault == "locator":
        material = durable.raw_materials[0]
        ledger._records[
            (material.key.kind.value, material.key.identity, material.key.revision)
        ] = codec.dumps(replace(material, controlled_ref="mdf-s3://other"))
    elif fault == "acquisition":
        acquisition = durable.acquisitions[0]
        ledger._records[
            (
                acquisition.key.kind.value,
                acquisition.key.identity,
                acquisition.key.revision,
            )
        ] = codec.dumps(replace(acquisition, provider_data_attempts=3))
    else:
        ledger._contents[p.version.identity] = (p.version.content_digest, b"corrupt")
    creates = store.create_attempts
    with pytest.raises((ContractError, SdmxReadRejected, ReadContractError)):
        handoff.reconcile("ECB", copy.deepcopy(ledger), store, protection, enabled=True)
    assert store.create_attempts == creates
    if fault != "raw":
        with pytest.raises((ContractError, SdmxReadRejected, ReadContractError)):
            read(copy.deepcopy(ledger))
    # No automatic release/repair on mismatch: protection remains for diagnosis.
    assert protection.held == 3


def test_unsupported_wire_formats_are_retained_and_terminal(tmp_path):
    root = tmp_path / "artifacts"
    c = candidate()
    raw = c.entities[-1].raw.replace(b"GenericData", b"StructureSpecificData")
    c = replace(c, entities=(*c.entities[:-1], replace(c.entities[-1], raw=raw)))
    exchange = Exchange({"ECB": c})
    with pytest.raises(ContractError, match="format_not_admitted"):
        source.Acquisition(root, source_plan()).acquire(
            "ECB", enabled=True, exchange=exchange, clock=clock()
        )
    assert (root / "ECB.data.bin").read_bytes() == raw and len(exchange.calls) == 3


def test_all_seven_reads_can_use_three_attempts_with_same_budget(tmp_path):
    base = Exchange()
    attempts = {}

    def exchange(request):
        slot = (request.provider, request.role)
        attempts[slot] = attempts.get(slot, 0) + 1
        if attempts[slot] < 3:
            return source.Response(503, b"")
        return base(request)

    sleeps, current = [], clock()
    acquisition = source.Acquisition(tmp_path / "artifacts", source_plan())
    for provider in ("ECB", "ESTAT"):
        acquisition.acquire(
            provider,
            enabled=True,
            exchange=exchange,
            clock=current,
            sleep=sleeps.append,
        )
    assert acquisition.counts() == {
        "logical": 7,
        "attempts": 21,
        "retries": 14,
        "fallbacks": 0,
    }
    assert sleeps == [1, 2] * 7 and len(base.calls) == 7
    budget = json.loads((acquisition.root / "budget.json").read_bytes())
    assert all(
        [o["classification"] for o in s["observations"]]
        == ["http_failure", "http_failure", "retained"]
        for s in budget["slots"].values()
    )


def test_interrupt_during_retry_does_not_reset_attempts(tmp_path):
    root, exchange = tmp_path / "artifacts", Exchange(faults={1: 503})

    def interrupted(_):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        source.Acquisition(root, source_plan()).acquire(
            "ECB", enabled=True, exchange=exchange, sleep=interrupted, clock=clock()
        )
    with pytest.raises(ContractError, match="consumed_slot"):
        source.Acquisition(root, source_plan()).acquire(
            "ECB", enabled=True, exchange=exchange
        )
    assert len(exchange.calls) == 1


@pytest.mark.parametrize(
    "raw",
    [
        b'{"data": []}',
        b"PK\x03\x04zip",
        b"<Structure/>",
        b'<!DOCTYPE x [<!ENTITY a "x">]><x/>',
    ],
)
def test_unverified_response_shapes_never_become_metadata(tmp_path, raw):
    c = candidate()
    c = replace(c, entities=(replace(c.entities[0], raw=raw), *c.entities[1:]))
    exchange = Exchange({"ECB": c})
    root = tmp_path / "artifacts"
    with pytest.raises((ValueError, ContractError)):
        source.Acquisition(root, source_plan()).acquire(
            "ECB", enabled=True, exchange=exchange, clock=clock()
        )
    assert (root / "ECB.flow.bin").read_bytes() == raw and len(exchange.calls) == 1


def test_wrong_package_ref_stops_without_normalizing(tmp_path):
    # Package is now scoped to the referenced class, never arbitrary.
    c = changed(
        candidate(),
        0,
        b'class="DataStructure"',
        b'class="DataStructure" package="codelist"',
    )
    exchange = Exchange({"ECB": c})
    with pytest.raises(ContractError, match="unsupported_reference"):
        source.Acquisition(tmp_path / "artifacts", source_plan()).acquire(
            "ECB", enabled=True, exchange=exchange, clock=clock()
        )
    assert len(exchange.calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://data-api.ecb.europa.eu/service/dataflow/ECB/EXR/latest",
        "https://example.org/service/dataflow/ECB/EXR/latest",
        "https://data-api.ecb.europa.eu/service/data/EXR/D.GBP.EUR.SP00.A?startPeriod=2026-07-01&endPeriod=2026-09-30",
        "https://data-api.ecb.europa.eu/service/dataflow/ECB/EXR/latest?redirect=1",
    ],
)
def test_transport_rejects_unapproved_request_without_io(url):
    with pytest.raises(ContractError, match="url_not_admitted"):
        source.ExactHttpsSource()(source.Request("ECB", "flow", url))


def test_one_shot_transport_uses_exact_accept_identity_and_no_proxy(
    tmp_path, monkeypatch
):
    calls = []

    class WireResponse:
        status = 200

        def read(self, limit):
            assert limit == source.MAX_ENTITY_BYTES + 1
            return candidate().entities[0].raw

        def getheaders(self):
            return [
                ("Content-Type", source.STRUCTURE_MEDIA),
                ("Set-Cookie", "not retained"),
            ]

    class Connection:
        def __init__(self, host, timeout):
            calls.append((host, timeout))

        def request(self, method, path, headers):
            calls.append((method, path, headers))

        def getresponse(self):
            return WireResponse()

        def close(self):
            calls.append("closed")

    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid:1")
    monkeypatch.setattr(source.http.client, "HTTPSConnection", Connection)
    request = source.Request("ECB", "flow", candidate().entities[0].url)
    response = source.ExactHttpsSource()(request)
    assert calls[0] == ("data-api.ecb.europa.eu", 10)
    assert calls[1] == (
        "GET",
        "/service/dataflow/ECB/EXR/latest?references=none",
        {
            "Accept": source.STRUCTURE_MEDIA,
            "Accept-Encoding": "identity",
            "User-Agent": "Market-Data-Foundation/0.1 T08",
        },
    )
    assert calls[2] == "closed"
    assert response.headers == (("content-type", source.STRUCTURE_MEDIA),)


def test_sdmx_schema_allowlist_is_exact():
    from market_data_foundation.canonical.postgres import validate_schema

    assert validate_schema(storage.SCHEMA) == storage.SCHEMA
    for suffix in ("_test", "_prod", "_other", ";drop", " "):
        with pytest.raises(ContractError, match="unsafe_postgres"):
            validate_schema(storage.SCHEMA + suffix)


def test_independent_reconcile_rejects_handoff_carrier_corruption(tmp_path):
    p, ledger, store, protection = (
        plan(),
        InMemoryLedger(),
        InMemoryMaterialStore(),
        Protection(),
    )
    root = tmp_path / "artifacts"
    storage.Handoff(root).materialize(
        CanonicalKernel(ledger, lambda: AT + timedelta(seconds=100)),
        p,
        store,
        protection,
        enabled=True,
    )
    path = root / "ECB.handoff.json"
    doc = json.loads(path.read_bytes())
    doc["content_sha256"] = "0" * 64
    path.write_text(json.dumps(doc))
    with pytest.raises(ContractError, match="digest_mismatch"):
        storage.Handoff(root).reconcile(
            "ECB", fresh(ledger), store, protection, enabled=True
        )
    assert protection.held == 0 and store.create_attempts == 3


def test_correct_devpg_still_requires_owned_capabilities(tmp_path):
    connection = SimpleNamespace(
        info=SimpleNamespace(host=storage.DEV_HOST, backend_pid=8)
    )
    ledger = PostgresLedger(connection, storage.SCHEMA)
    with pytest.raises(ContractError, match="capability_binding"):
        storage.Handoff(tmp_path / "artifacts").materialize(
            CanonicalKernel(ledger, lambda: AT),
            plan(),
            InMemoryMaterialStore(),
            Protection(),
            enabled=True,
            authoritative_commit=lambda: None,
        )
    assert not list(tmp_path.iterdir())


def test_restart_reconcile_never_steals_live_postgres_writer(tmp_path):
    p, ledger, store, protection = (
        plan(),
        InMemoryLedger(),
        InMemoryMaterialStore(),
        Protection(),
    )
    root = tmp_path / "artifacts"
    storage.Handoff(root).materialize(
        CanonicalKernel(ledger, lambda: AT + timedelta(seconds=100)),
        p,
        store,
        protection,
        enabled=True,
    )
    path = root / "ECB.handoff.json"
    doc = json.loads(path.read_bytes())
    doc["state"] = "outcome_unknown"
    doc["writer"] = {
        "kind": "postgres",
        "host": storage.DEV_HOST,
        "schema": storage.SCHEMA,
        "pid": 8,
        "database": "synthetic",
        "port": 5432,
    }
    path.write_text(json.dumps(doc))
    connection = ReadbackConnection(ledger)
    connection.alive = True
    readback = PostgresLedger(connection, storage.SCHEMA)
    from market_data_foundation.historical.bls_cpi_material_store import (
        GarageMaterialStore,
        PostgresAdvisoryWriterProtection,
    )

    bound_store = object.__new__(GarageMaterialStore)
    bound_protection = PostgresAdvisoryWriterProtection(readback.connection)
    with pytest.raises(ContractError, match="old_writer_session_still_alive"):
        storage.Handoff(root).reconcile(
            "ECB", readback, bound_store, bound_protection, enabled=True
        )
    assert any("pg_stat_activity" in q for q in connection.events)
    assert connection.events[-1] == "rollback" and connection.reads == 0
    assert protection.held == 0 and store.create_attempts == 3


def test_devpg_publication_enters_idle_and_checks_graph_inside_transaction(
    tmp_path, monkeypatch
):
    # Models the actual PostgresLedger idle-entry contract without a connection.
    from market_data_foundation.historical.bls_cpi_material_store import (
        GarageMaterialStore,
        PostgresAdvisoryWriterProtection,
    )

    memory, events = InMemoryLedger(), []
    info = SimpleNamespace(
        host=storage.DEV_HOST,
        backend_pid=8,
        dbname="synthetic",
        port=5432,
        transaction_status="idle",
    )
    connection = SimpleNamespace(info=info)

    def get(self, key):
        info.transaction_status = "active"
        return memory.get(key)

    def get_content(self, key):
        info.transaction_status = "active"
        return memory.get_content(key)

    def put(self, record):
        info.transaction_status = "active"
        return memory.put(record)

    def put_content(self, version, content):
        info.transaction_status = "active"
        memory.put_content(version, content)

    def publication_by_occurrence(self, occurrence):
        return memory.publication_by_occurrence(occurrence)

    def publish(self, request, clock):
        assert info.transaction_status == "idle", (
            "read before publication opened caller transaction"
        )
        events.append("publication")
        # Mirrors PostgresLedger's in-transaction occurrence callback.
        assert self.publication_by_occurrence(request.occurrence) is None
        assert info.transaction_status == "active"
        at = self.server_now()
        result = memory.publish(request, lambda: at)
        info.transaction_status = "idle"
        return result

    for name, value in {
        "get": get,
        "get_content": get_content,
        "put": put,
        "put_content": put_content,
        "publication_by_occurrence": publication_by_occurrence,
        "publish": publish,
        "server_now": lambda _: AT + timedelta(seconds=100),
    }.items():
        monkeypatch.setattr(PostgresLedger, name, value)

    class Store(GarageMaterialStore, InMemoryMaterialStore):
        def __init__(self):
            InMemoryMaterialStore.__init__(self)

        create_if_absent = InMemoryMaterialStore.create_if_absent
        read_exact = InMemoryMaterialStore.read_exact

    class OwnedProtection(PostgresAdvisoryWriterProtection, Protection):
        def __init__(self):
            Protection.__init__(self)
            self.connection = connection

        acquire = Protection.acquire

    def commit():
        events.append("commit")
        info.transaction_status = "idle"

    ledger, store, protection = (
        PostgresLedger(connection, storage.SCHEMA),
        Store(),
        OwnedProtection(),
    )
    durable, publication = storage.Handoff(tmp_path / "artifacts").materialize(
        CanonicalKernel(ledger, lambda: AT),
        plan(),
        store,
        protection,
        enabled=True,
        authoritative_commit=commit,
    )
    assert events == ["commit", "publication"] and info.transaction_status == "idle"
    assert publication.request == durable.publication_request and protection.held == 0


class ReadbackConnection:
    """Committed/stale PG branch model using actual PostgresLedger SELECTs.

    No driver connection is opened. The first SELECT samples committed state;
    activity is sampled separately, as in PG. Hooks replay commit/exit races.
    An ambient repeatable-read view stays empty even after commit.
    """

    def __init__(self, committed, *, pid=9, autocommit=False):
        from psycopg.pq import TransactionStatus

        self.info = SimpleNamespace(
            host=storage.DEV_HOST,
            backend_pid=pid,
            dbname="synthetic",
            port=5432,
            transaction_status=TransactionStatus.IDLE,
        )
        self.closed = False
        self._num_transactions = 0
        self._tpc = self._pipeline = None
        self._autocommit = autocommit
        self.committed = committed
        self.view = InMemoryLedger()
        self.events = []
        self.settings = ("repeatable read", "on")
        self.alive = False
        self.recovery = False
        self.fail_cleanup = False
        self.fail_cleanup_on = self.fail_restore_on = None
        self.rollbacks = self.snapshots = 0
        self.on_snapshot = self.on_activity = None
        self.on_restore = None
        self.reported_pid = pid
        self.reads = 0

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value):
        if value is False and self.rollbacks == self.fail_restore_on:
            raise OSError("hidden restore detail")
        self._autocommit = value
        if value is False and self.rollbacks and self.on_restore:
            self.on_restore()

    def execute(self, query, parameters=None):
        from psycopg.pq import TransactionStatus

        statement = query if isinstance(query, str) else query.as_string()
        statement = " ".join(statement.split())
        self.events.append(statement)
        if statement == "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY":
            assert self.info.transaction_status is TransactionStatus.IDLE
            assert self.autocommit is True
            self.info.transaction_status = TransactionStatus.INTRANS
            self.view = None
            return SimpleNamespace()
        assert self.info.transaction_status is TransactionStatus.INTRANS
        if self.view is None:
            self.view = fresh(self.committed)
            self.snapshots += 1
            if self.on_snapshot:
                self.on_snapshot(self.snapshots)
        if statement.startswith("SELECT current_setting"):
            rows = [self.settings]
        elif statement == "SELECT pg_backend_pid(), pg_is_in_recovery()":
            rows = [(self.reported_pid, self.recovery)]
        elif statement.startswith("SELECT EXISTS(SELECT 1 FROM pg_stat_activity"):
            assert parameters == (8,)
            if self.on_activity:
                self.on_activity()
            rows = [(self.alive,)]
        elif ".publications WHERE occurrence = %s" in statement:
            self.reads += 1
            value = self.view.publication_by_occurrence(parameters[0])
            rows = [] if value is None else [(codec.dumps(value),)]
        elif ".publications WHERE identity = %s" in statement:
            from market_data_foundation.canonical.model import Key, Kind

            self.reads += 1
            value = self.view.get(Key(Kind.PUBLICATION, parameters[0]))
            rows = [] if value is None else [(codec.dumps(value),)]
        elif ".publications WHERE version_identity = %s" in statement:
            from market_data_foundation.canonical.model import Key, Kind

            self.reads += 1
            rows = [
                (codec.dumps(p),)
                for p in self.view.publications_for_version(
                    Key(Kind.VERSION, parameters[0])
                )
            ]
        elif (
            ".records WHERE kind = %s AND identity = %s AND revision = %s" in statement
        ):
            from market_data_foundation.canonical.model import Key, Kind

            self.reads += 1
            value = self.view.get(Key(Kind(parameters[0]), *parameters[1:]))
            rows = [] if value is None else [(codec.dumps(value),)]
        elif ".contents WHERE version_identity = %s" in statement:
            from market_data_foundation.canonical.model import Key, Kind

            self.reads += 1
            content = self.view.get_content(Key(Kind.VERSION, parameters[0]))
            rows = [] if content is None else [(digest(content), content)]
        else:
            pytest.fail("Unexpected SQL; readback must not write: " + statement)
        return SimpleNamespace(
            fetchone=lambda: rows[0] if rows else None,
            fetchall=lambda: rows,
        )

    def rollback(self):
        from psycopg.pq import TransactionStatus

        self.events.append("rollback")
        self.rollbacks += 1
        if self.fail_cleanup or self.rollbacks == self.fail_cleanup_on:
            raise OSError("hidden rollback detail")
        self.info.transaction_status = TransactionStatus.IDLE

    def close(self):
        self.events.append("close")
        self.closed = True


def pg_unknown_model(tmp_path, provider, phase="published", *, restart=False):
    """Keep real handoff intent/tickets, substitute only external capabilities."""
    from market_data_foundation.historical.bls_cpi_material_store import (
        GarageMaterialStore,
        PostgresAdvisoryWriterProtection,
    )

    p, committed, store, protection = (
        plan(provider),
        InMemoryLedger(),
        InMemoryMaterialStore(),
        Protection(),
    )
    handoff = storage.Handoff(tmp_path / "artifacts")

    def fail(_provider=None):
        raise RuntimeError("hidden handoff failure")

    if phase == "published":
        handoff._release = fail
    with pytest.raises(storage.HandoffUnknown):
        handoff.materialize(
            CanonicalKernel(committed, lambda: AT + timedelta(seconds=100)),
            p,
            store,
            protection,
            enabled=True,
            authoritative_commit=fail if phase == "partial" else None,
        )
    # Restore normal release; all original tickets remain held after ambiguity.
    handoff._release = storage.Handoff._release.__get__(handoff)
    reader = ReadbackConnection(committed)
    writer = ReadbackConnection(committed, pid=8)
    ledger = PostgresLedger(reader, storage.SCHEMA)
    path = handoff.root / (provider + ".handoff.json")
    doc = json.loads(path.read_bytes())
    doc["writer"] = storage._session(PostgresLedger(writer, storage.SCHEMA))
    path.write_text(json.dumps(doc))
    handoff._writers[provider] = PostgresLedger(writer, storage.SCHEMA)
    bound_store = object.__new__(GarageMaterialStore)
    bound_store.read_exact = store.read_exact
    bound_protection = PostgresAdvisoryWriterProtection(reader if restart else writer)
    # The synthetic ticket mechanics are retained; external acquire is forbidden.
    bound_protection.acquire = protection.acquire
    if restart:
        handoff = storage.Handoff(handoff.root)
    return handoff, ledger, bound_store, bound_protection, protection, writer, committed


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("phase", ["published", "partial"])
@pytest.mark.parametrize("autocommit", [False, True])
def test_pg_reconcile_reads_committed_state_and_releases_after_clean_exit(
    tmp_path, provider, phase, autocommit
):
    from psycopg.pq import TransactionStatus

    handoff, ledger, store, protection, tickets, writer, committed = pg_unknown_model(
        tmp_path, provider, phase
    )
    reader = ledger.connection
    reader.autocommit = autocommit
    assert (
        reader.view.publication_by_occurrence(
            plan(provider).publication_request.occurrence
        )
        is None
    )
    before = committed.snapshot()
    original = handoff._release

    def release(p):
        assert reader.info.transaction_status is TransactionStatus.IDLE
        assert reader.autocommit is autocommit
        assert reader.events[-1] == "rollback"
        original(p)

    handoff._release = release
    result = handoff.reconcile(provider, ledger, store, protection, enabled=True)
    assert result == {
        "outcome": "confirmed_published"
        if phase == "published"
        else "confirmed_not_published",
        "raw_present": [True] * len(plan(provider).raw_materials),
        "lock_release_proven": True,
    }
    assert reader.events[0] == "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert reader.reads > 0 and tickets.held == 0
    assert writer.events == [] and committed.snapshot() == before


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize(
    "ambient",
    ["stale", "active", "error", "unknown", "driver", "tpc", "pipeline", "closed"],
)
def test_pg_reconcile_refuses_old_snapshot_and_caller_scopes_untouched(
    tmp_path, provider, restart, ambient
):
    from psycopg.pq import TransactionStatus

    handoff, ledger, store, protection, tickets, _, committed = pg_unknown_model(
        tmp_path, provider, restart=restart
    )
    reader = ledger.connection
    if ambient in {"stale", "active", "error", "unknown"}:
        reader.info.transaction_status = {
            "stale": TransactionStatus.INTRANS,
            "active": TransactionStatus.ACTIVE,
            "error": TransactionStatus.INERROR,
            "unknown": TransactionStatus.UNKNOWN,
        }[ambient]
    elif ambient == "driver":
        reader._num_transactions = 1
    elif ambient == "tpc":
        reader._tpc = object()
    elif ambient == "pipeline":
        reader._pipeline = object()
    else:
        reader.closed = True
    # The old view lies about a genuinely committed Publication.
    occurrence = plan(provider).publication_request.occurrence
    assert committed.publication_by_occurrence(occurrence) is not None
    assert reader.view.publication_by_occurrence(occurrence) is None
    path = handoff.root / (provider + ".handoff.json")
    before = path.read_bytes()
    with pytest.raises(ReadContractError):
        handoff.reconcile(provider, ledger, store, protection, enabled=True)
    assert reader.events == [] and reader.reads == 0
    assert tickets.held == len(plan(provider).raw_materials)
    assert path.read_bytes() == before  # UNKNOWN, no invented absence/release.


@pytest.mark.parametrize(
    "fault",
    [
        "isolation",
        "read_write",
        "standby",
        "cleanup",
        "writer_transaction",
        "same_backend",
    ],
)
def test_pg_untrusted_read_view_keeps_all_tickets(tmp_path, fault):
    from psycopg.pq import TransactionStatus

    handoff, ledger, store, protection, tickets, writer, _ = pg_unknown_model(
        tmp_path, "ECB"
    )
    reader = ledger.connection
    if fault == "isolation":
        reader.settings = ("read committed", "on")
    elif fault == "read_write":
        reader.settings = ("repeatable read", "off")
    elif fault == "standby":
        reader.recovery = True
    elif fault == "cleanup":
        reader.fail_cleanup = True
    elif fault == "writer_transaction":
        writer.info.transaction_status = TransactionStatus.INTRANS
    else:
        reader.info.backend_pid = 8
    with pytest.raises((ReadContractError, ContractError)):
        handoff.reconcile("ECB", ledger, store, protection, enabled=True)
    assert tickets.held == 3
    doc = json.loads((handoff.root / "ECB.handoff.json").read_bytes())
    assert doc["state"] == "outcome_unknown" and "reconciliation" not in doc
    if fault == "cleanup":
        assert reader.reads > 0 and reader.closed
    if fault in {"writer_transaction", "same_backend"}:
        assert reader.events == [] and writer.events == []


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("alive", [True, False])
def test_pg_restart_checks_old_writer_before_reacquisition(tmp_path, provider, alive):
    handoff, ledger, store, protection, tickets, _, committed = pg_unknown_model(
        tmp_path, provider, restart=True
    )
    ledger.connection.alive = alive
    before = committed.snapshot()
    if alive:
        with pytest.raises(ContractError, match="old_writer_session_still_alive"):
            handoff.reconcile(provider, ledger, store, protection, enabled=True)
        assert ledger.connection.reads == 0
        assert tickets.held == len(plan(provider).raw_materials)
    else:
        for ticket in tickets.tickets:
            ticket.force_gap_for_counterexample()
        result = handoff.reconcile(provider, ledger, store, protection, enabled=True)
        assert result["outcome"] == "confirmed_published" and tickets.held == 0
        assert ledger.connection.reads > 0
    assert committed.snapshot() == before


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("autocommit", [False, True])
@pytest.mark.parametrize("commit_at", ["before_probe", "after_probe_snapshot", "none"])
def test_pg_restart_commit_exit_precedes_authoritative_snapshot(
    tmp_path, provider, autocommit, commit_at
):
    from psycopg.pq import TransactionStatus

    handoff, ledger, store, protection, tickets, writer, committed = pg_unknown_model(
        tmp_path, provider, "partial", restart=True
    )
    reader = ledger.connection
    reader.autocommit = autocommit
    reader.alive = commit_at != "none"
    occurrence = plan(provider).publication_request.occurrence
    assert committed.publication_by_occurrence(occurrence) is None

    def commit_exit():
        committed.publish(
            storage.durable_plan(plan(provider)).publication_request,
            lambda: AT + timedelta(seconds=100),
        )
        reader.alive = False
        reader.events.append("old_writer_commit_exit")

    if commit_at == "before_probe":
        commit_exit()

    def after_snapshot(number):
        if number == 1 and commit_at == "after_probe_snapshot":
            # The original R2 view stays partial while activity reports gone.
            commit_exit()
            assert reader.view.publication_by_occurrence(occurrence) is None
            assert committed.publication_by_occurrence(occurrence) is not None
        if number == 2:
            assert reader.alive is False
            assert reader.rollbacks == 1 and reader.reads == 0

    reader.on_snapshot = after_snapshot
    for ticket in tickets.tickets:
        ticket.force_gap_for_counterexample()
    acquire = protection.acquire

    def protected_acquire(identity):
        assert reader.alive is False
        assert reader.snapshots == 2 and reader.rollbacks == 1
        return acquire(identity)

    protection.acquire = protected_acquire
    release = handoff._release

    def verified_release(p):
        assert reader.info.transaction_status is TransactionStatus.IDLE
        assert reader.autocommit is autocommit
        assert reader.snapshots == reader.rollbacks == 2
        assert reader.reads > 0 and reader.events[-1] == "rollback"
        release(p)

    handoff._release = verified_release
    result = handoff.reconcile(provider, ledger, store, protection, enabled=True)
    assert result["outcome"] == (
        "confirmed_not_published" if commit_at == "none" else "confirmed_published"
    )
    assert result["lock_release_proven"] and tickets.held == 0
    assert writer.events == []
    gate = next(i for i, e in enumerate(reader.events) if "pg_stat_activity" in e)
    begin = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert reader.events[gate + 1 : gate + 3] == ["rollback", begin]


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
def test_pg_restart_writer_commits_after_gate_still_stops(tmp_path, provider):
    handoff, ledger, store, protection, tickets, _, committed = pg_unknown_model(
        tmp_path, provider, "partial", restart=True
    )
    reader = ledger.connection
    reader.alive = True
    path = handoff.root / (provider + ".handoff.json")
    before = path.read_bytes()

    def after_snapshot(number):
        # A bad implementation would proceed with this partial snapshot.
        assert number == 1

    reader.on_snapshot = after_snapshot
    with pytest.raises(ContractError, match="old_writer_session_still_alive"):
        handoff.reconcile(provider, ledger, store, protection, enabled=True)
    committed.publish(
        storage.durable_plan(plan(provider)).publication_request,
        lambda: AT + timedelta(seconds=100),
    )
    reader.alive = False
    assert reader.snapshots == reader.rollbacks == 1 and reader.reads == 0
    assert tickets.held == len(plan(provider).raw_materials)
    assert path.read_bytes() == before


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize(
    "fault",
    [
        "gate_rollback",
        "view_rollback",
        "gate_restore",
        "view_restore",
        "activity_error",
        "activity_unknown",
        "identity",
        "read_write",
        "standby",
        "view_ambient",
    ],
)
def test_pg_restart_unproven_gate_or_cleanup_never_releases(tmp_path, provider, fault):
    from psycopg.pq import TransactionStatus

    handoff, ledger, store, protection, tickets, _, _ = pg_unknown_model(
        tmp_path, provider, restart=True
    )
    reader = ledger.connection
    path = handoff.root / (provider + ".handoff.json")
    before = path.read_bytes()
    if fault.endswith("rollback"):
        reader.fail_cleanup_on = 1 if fault.startswith("gate") else 2
    elif fault.endswith("restore"):
        reader.fail_restore_on = 1 if fault.startswith("gate") else 2
    elif fault == "activity_error":

        def fail():
            raise OSError("hidden activity detail")

        reader.on_activity = fail
    elif fault == "activity_unknown":
        reader.alive = None
    elif fault == "identity":
        reader.reported_pid = 123
    elif fault == "read_write":
        reader.settings = ("repeatable read", "off")
    elif fault == "standby":
        reader.recovery = True
    else:
        # Simulate an unexpected transaction between the two owned scopes.
        # The next scope must reject it without rolling it back.
        def foreign_transaction():
            reader.info.transaction_status = TransactionStatus.INTRANS

        reader.on_restore = foreign_transaction
    if fault in {"view_rollback", "view_restore"}:
        for ticket in tickets.tickets:
            ticket.force_gap_for_counterexample()
    with pytest.raises((ReadContractError, ContractError, OSError)):
        handoff.reconcile(provider, ledger, store, protection, enabled=True)
    assert tickets.held == len(plan(provider).raw_materials)
    assert path.read_bytes() == before
    if fault.startswith("view_") and fault != "view_ambient":
        assert reader.reads > 0 and reader.snapshots == 2
    else:
        assert reader.reads == 0 and reader.snapshots == 1
    if fault.endswith(("rollback", "restore")):
        assert reader.closed
    if fault == "view_ambient":
        assert not reader.closed and reader.rollbacks == 1
        assert reader.info.transaction_status is TransactionStatus.INTRANS


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("close_error", [False, True])
def test_complete_http200_close_error_never_requeries_or_loses_retention(
    tmp_path, monkeypatch, provider, close_error
):
    c = candidate(provider)
    calls, closes, sleeps = [], [], []

    class Connection:
        def __init__(self, host, timeout):
            pass

        def request(self, method, path, headers):
            calls.append(path)
            self.entity = next(e for e in c.entities if e.url.endswith(path))

        def getresponse(self):
            raw = self.entity.raw
            media = (
                source.DATA_MEDIA
                if self.entity.role == "data"
                else source.STRUCTURE_MEDIA
            )
            return SimpleNamespace(
                status=200,
                read=lambda _: raw,
                getheaders=lambda: [
                    ("Content-Type", media),
                    ("Content-Length", str(len(raw))),
                ],
            )

        def close(self):
            closes.append(self.entity.role)
            if close_error:
                raise OSError("hidden cleanup detail")

    monkeypatch.setattr(source.http.client, "HTTPSConnection", Connection)
    acquisition = source.Acquisition(tmp_path / "artifacts", source_plan())
    obtained = acquisition.acquire(
        provider,
        enabled=True,
        exchange=source.ExactHttpsSource(),
        clock=clock(),
        sleep=sleeps.append,
    )
    assert [e.raw for e in obtained.entities] == [e.raw for e in c.entities]
    assert len(calls) == len(set(calls)) == len(c.entities)
    assert closes == [e.role for e in c.entities] and sleeps == []
    assert acquisition.counts() == {
        "logical": len(c.entities),
        "attempts": len(c.entities),
        "retries": 0,
        "fallbacks": 0,
    }
    for e in c.entities:
        slot = provider + "." + e.role
        assert (acquisition.root / (slot + ".bin")).read_bytes() == e.raw
        receipt = json.loads((acquisition.root / (slot + ".json")).read_bytes())
        assert receipt["sha256"] == digest(e.raw) and receipt["attempts"] == 1
    assert (
        source.Acquisition(acquisition.root, source_plan()).acquire(
            provider, enabled=True
        )
        == obtained
    )
    assert len(calls) == len(c.entities)


@pytest.mark.parametrize("failure", ["incomplete", "read_oserror", "protocol"])
def test_close_error_preserves_incomplete_transport_failure(
    tmp_path, monkeypatch, failure
):
    calls, closes, sleeps = [], [], []
    error = {
        "incomplete": source.http.client.IncompleteRead(b"partial", 99),
        "read_oserror": OSError("hidden read failure"),
        "protocol": source.http.client.HTTPException("hidden protocol failure"),
    }[failure]

    def read(_):
        raise error

    class Connection:
        def __init__(self, host, timeout):
            pass

        def request(self, method, path, headers):
            calls.append(path)

        def getresponse(self):
            return SimpleNamespace(status=200, read=read)

        def close(self):
            closes.append(True)
            raise OSError("hidden cleanup failure")

    monkeypatch.setattr(source.http.client, "HTTPSConnection", Connection)
    root = tmp_path / "artifacts"
    with pytest.raises(SourceRequestFailed) as caught:
        source.Acquisition(root, source_plan()).acquire(
            "ECB",
            enabled=True,
            exchange=source.ExactHttpsSource(),
            clock=clock(),
            sleep=sleeps.append,
        )
    attempts = 1 if failure == "protocol" else 3
    assert caught.value.classification == (
        "protocol_failure" if failure == "protocol" else "transport_failure"
    )
    assert len(calls) == len(closes) == caught.value.attempts == attempts
    assert sleeps == ([] if attempts == 1 else [1, 2])
    assert not list(root.glob("*.bin")) and not list(root.glob("ECB.*.json"))
