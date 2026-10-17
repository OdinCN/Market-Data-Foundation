"""Exact-seven correction fixtures are synthetic, never current API evidence."""

import copy
import json
import re
import socket
from dataclasses import asdict, replace
from datetime import timedelta

import psycopg
import pytest
from test_sdmx_official import (
    AT,
    assert_fresh_process_read,
    candidate,
    changed,
    read,
)
from test_sdmx_source_storage import Exchange, Protection, clock, source_plan

from market_data_foundation.canonical import codec
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, digest
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import sdmx_materialization as storage
from market_data_foundation.historical import sdmx_official as sdmx
from market_data_foundation.historical import sdmx_source as source
from market_data_foundation.historical.bls_cpi_material_store import (
    InMemoryMaterialStore,
)
from market_data_foundation.historical.sdmx_contract import roles_for
from market_data_foundation.historical.sdmx_local_read import SdmxReadRejected


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline compatibility correction attempted external IO")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


def lean_candidate(provider):
    """No implicit codelists supplied by a references=none DSD response."""
    c = candidate(provider)
    dsd = c.entities[1]
    raw = re.sub(rb"<s:Codelists>.*?</s:Codelists>", b"", dsd.raw)
    return replace(c, entities=(c.entities[0], replace(dsd, raw=raw), *c.entities[2:]))


def build(c):
    return sdmx.build_plan(
        c,
        identity="compat-" + c.provider,
        finished_at=AT + timedelta(seconds=20),
        frozen_at=AT + timedelta(seconds=30),
    )


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
def test_references_none_real_cardinality_and_lineage(tmp_path, provider):
    c = lean_candidate(provider)
    # Independently vary both real-ref-derived identities; no frozen DSD version.
    c = replace(
        c,
        entities=tuple(
            replace(
                e,
                raw=e.raw.replace(b"SYNTHETIC_DSD", b"DIFFERENT_DSD").replace(
                    b"1.0", b"9.7"
                ),
                url=e.url.replace("SYNTHETIC_DSD", "DIFFERENT_DSD").replace(
                    "1.0", "9.7"
                ),
            )
            for e in c.entities
        ),
    )
    exchange = Exchange({provider: c})
    acquisition = source.Acquisition(tmp_path / "artifacts" / "source", source_plan())
    obtained = acquisition.acquire(
        provider, enabled=True, exchange=exchange, clock=clock()
    )
    assert tuple(e.role for e in obtained.entities) == roles_for(provider)
    assert tuple(r.role for r in exchange.calls) == roles_for(provider)
    assert all("references=none" in r.url for r in exchange.calls[:-1])
    assert all("constraint" not in r.url for r in exchange.calls)
    p = build(obtained)
    metadata = json.loads(p.content)["projection"]["metadata"]
    assert metadata["dsd"] == [provider, "DIFFERENT_DSD", "9.7"]
    assert "constraint" not in metadata
    assert set(metadata["evidence"]) == set(roles_for(provider)[:-1])
    for d in metadata["dimensions"]:
        if d["dimension"] == "unit":
            assert d["meaning"] == "Index, 2025=100"
        else:
            assert d["meaning"] is None  # No invented provider code descriptions.
    if provider == "ESTAT":
        assert metadata["unit_codelist"] == ["ESTAT", "UNIT", "9.7"]
        assert [d["dimension"] for d in metadata["dimensions"]] == [
            "freq",
            "unit",
            "coicop18",
            "geo",
        ]
    count = len(roles_for(provider))
    assert len(p.acquisitions) == len(p.raw_materials) == count
    assert len(p.version.materials) == count + 1
    store, ledger, protection = InMemoryMaterialStore(), InMemoryLedger(), Protection()
    handoff = storage.Handoff(tmp_path / "artifacts" / "storage")
    durable, _ = handoff.materialize(
        CanonicalKernel(ledger, lambda: AT + timedelta(seconds=40)),
        p,
        store,
        protection,
        enabled=True,
    )
    assert store.create_attempts == count and protection.held == 0
    assert [a.parameters[0] for a in durable.acquisitions] == [
        ("role", role) for role in roles_for(provider)
    ]
    assert [m.digest for m in durable.raw_materials] == [
        digest(e.raw) for e in obtained.entities
    ]
    assert_fresh_process_read(ledger, provider, durable, rejected=False)
    before = (store.create_attempts, ledger.snapshot())
    result = storage.Handoff(handoff.root).reconcile(
        provider,
        InMemoryLedger.from_snapshot(ledger.snapshot()),
        store,
        protection,
        enabled=True,
    )
    assert result["raw_present"] == [True] * count
    assert result["outcome"] == "confirmed_published"
    assert (store.create_attempts, ledger.snapshot()) == before
    assert acquisition.acquire(provider, enabled=True) == obtained
    assert len(exchange.calls) == count


UNIT_FAILURES = [
    (b'id="I25"', b'id="I15"'),
    (b"Index, 2025=100", b"Index, 2015=100"),
    (b'id="UNIT"', b'id="CL_UNIT"'),
    (b'version="1.0"', b'version="2.0"'),
    (b'agencyID="ESTAT"', b'agencyID="ECB"'),
    (b"<s:Codelists>", b"<s:Codelists/><s:Codelists>"),
    (
        b"</s:Codelists>",
        b'<s:Codelist agencyID="ESTAT" id="OTHER" version="1.0"/></s:Codelists>',
    ),
    (b"</s:Code>", b'<c:Name xml:lang="en">OTHER</c:Name></s:Code>'),
    (
        b"Index, 2025=100</c:Name>",
        b"Index, 2025=100<c:Name>Index, 2015=100</c:Name></c:Name>",
    ),
    (b"</c:Name>", b"</c:Name>OTHER"),
]


@pytest.mark.parametrize("old,new", UNIT_FAILURES)
def test_unit_evidence_fails_before_data_and_reuses_retained_bytes(tmp_path, old, new):
    c = changed(lean_candidate("ESTAT"), 2, old, new)
    acquisition = source.Acquisition(tmp_path / "artifacts", source_plan())
    exchange = Exchange({"ESTAT": c})
    for _ in range(2):
        with pytest.raises(ContractError):
            acquisition.acquire("ESTAT", enabled=True, exchange=exchange, clock=clock())
    assert [r.role for r in exchange.calls] == ["flow", "dsd", "unit"]
    assert acquisition.counts() == {
        "logical": 3,
        "attempts": 3,
        "retries": 0,
        "fallbacks": 0,
    }
    assert (acquisition.root / "ESTAT.unit.bin").read_bytes() == c.entities[2].raw
    with pytest.raises(ContractError):
        build(c)


@pytest.mark.parametrize(
    "role,index,old,new",
    [
        ("dimension", 1, b"coicop18", b"coicop"),
        ("dimension", 1, b"coicop18", b"coicop19"),
        ("unit-ref", 1, b'id="UNIT"', b'id="CL_UNIT"'),
        ("series", -1, b"coicop18", b"coicop"),
        ("series", -1, b'value="I25"', b'value="I15"'),
        ("series", -1, b'value="TOTAL"', b'value="CP00"'),
        ("series", -1, b'value="DE"', b'value="FR"'),
    ],
)
def test_no_dimension_alias_or_series_substitution(tmp_path, role, index, old, new):
    c = changed(lean_candidate("ESTAT"), index, old, new)
    exchange = Exchange({"ESTAT": c})
    acquisition = source.Acquisition(tmp_path / "artifacts", source_plan())
    with pytest.raises(ContractError):
        acquisition.acquire("ESTAT", enabled=True, exchange=exchange, clock=clock())
    assert len(exchange.calls) == (4 if role == "series" else 2)
    with pytest.raises(ContractError):
        build(c)


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("fault", ["extra_cc", "missing", "unit_for_data", "reordered"])
def test_family_raw_cardinality_cannot_hide_a_source(provider, fault):
    c = lean_candidate(provider)
    entities = c.entities
    if fault == "extra_cc":
        entities = (
            *entities[:-1],
            replace(entities[0], role="constraint"),
            entities[-1],
        )
    elif fault == "missing":
        entities = entities[1:]
    elif fault == "unit_for_data":
        entities = (*entities[:-1], replace(entities[-1], role="unit"))
    else:
        entities = (entities[1], entities[0], *entities[2:])
    with pytest.raises(ContractError, match="entities"):
        build(replace(c, entities=entities))


@pytest.mark.parametrize("field", ["UNIT", "UNIT_MULT"])
def test_ecb_requires_explicit_series_unit_evidence(field):
    c = lean_candidate("ECB")
    raw = re.sub(
        rb'<g:Value id="' + field.encode() + rb'" value="[^"]*"/>',
        b"",
        c.entities[-1].raw,
    )
    c = replace(c, entities=(*c.entities[:-1], replace(c.entities[-1], raw=raw)))
    with pytest.raises(ContractError, match="unit_evidence"):
        build(c)


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
def test_mixed_durable_or_changed_raw_lineage_rejected(provider):
    p = storage.durable_plan(build(lean_candidate(provider)))
    for index in range(len(p.raw_materials)):
        ledger = InMemoryLedger()
        # Install an internally canonical graph first, then tamper one raw.
        embedded = build(lean_candidate(provider))
        sdmx.publish_offline(
            CanonicalKernel(ledger, lambda: AT + timedelta(seconds=40)), embedded
        )
        for raw in p.raw_materials:
            ledger._records[
                (raw.key.kind.value, raw.key.identity, raw.key.revision)
            ] = codec.dumps(raw)
        raw = embedded.raw_materials[index]
        ledger._records[(raw.key.kind.value, raw.key.identity, raw.key.revision)] = (
            codec.dumps(raw)
        )
        before = copy.deepcopy(ledger.snapshot())
        with pytest.raises(SdmxReadRejected):
            read(ledger, provider)
        with pytest.raises(SdmxReadRejected):
            read(ledger, provider, version_identity=p.version.identity)
        assert ledger.snapshot() == before


@pytest.mark.parametrize("legacy", [False, True])
def test_old_consumed_budget_never_gets_a_fresh_seven_slot_allowance(tmp_path, legacy):
    root = tmp_path / "artifacts"
    root.mkdir()
    plan = asdict(source_plan())
    if legacy:
        plan.update(
            ecb_constraint=["ECB", "HISTORICAL", "1.0"],
            estat_constraint=["ESTAT", "HISTORICAL", "1.0"],
        )
    budget = {
        "plan": plan,
        "slots": {"ECB.constraint": {"state": "started", "attempts": 1}},
    }
    source.write_json(root / "budget.json", budget)
    original = (root / "budget.json").read_bytes()
    with pytest.raises(ContractError, match="plan_mismatch|budget_invalid"):
        source.Acquisition(root, source_plan()).acquire(
            "ECB", enabled=True, exchange=lambda _: pytest.fail("requery")
        )
    assert (root / "budget.json").read_bytes() == original


def test_orphan_raw_is_terminal_before_transport(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "ECB.flow.bin").write_bytes(b"already retained, receipt absent")
    with pytest.raises(ContractError, match="consumed_slot"):
        source.Acquisition(root, source_plan()).acquire(
            "ECB", enabled=True, exchange=lambda _: pytest.fail("requery")
        )


@pytest.mark.parametrize("old,new", UNIT_FAILURES[:5])
def test_unit_corruption_consistent_snapshot_rejected_after_restart(
    monkeypatch, old, new
):
    original = lean_candidate("ESTAT")
    bad = changed(original, 2, old, new)
    projection = sdmx.project(original)
    projection["metadata"]["evidence"]["unit"] = {
        "raw_sha256": digest(bad.entities[2].raw),
        "raw_size": len(bad.entities[2].raw),
    }
    # Model a hypothetical older permissive build, including consistent hashes.
    with monkeypatch.context() as legacy:
        legacy.setattr(sdmx, "project", lambda _: projection)
        p = build(bad)
    with pytest.raises(ContractError):
        sdmx.validate_plan(p)
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: AT + timedelta(seconds=40))
    with pytest.raises(ContractError):
        sdmx.publish_offline(kernel, p)
    assert ledger.snapshot() == InMemoryLedger().snapshot()
    with monkeypatch.context() as legacy:
        legacy.setattr(sdmx, "validate_plan", lambda _: None)
        sdmx.publish_offline(kernel, p)
    assert_fresh_process_read(ledger, "ESTAT", p, rejected=True)


# Explicit false is XML boolean evidence only, never proof of official origin.
# Keep synthetic-not-official IDs; no fixture is promoted to Source qualification.
HEADER_TEST_MUTATIONS = [
    (b"<m:Test>false</m:Test>", b"<m:Test>true</m:Test>"),
    (b"<m:Test>false</m:Test>", b"<m:Test>1</m:Test>"),
    (b"<m:Test>false</m:Test>", b""),
    (b"<m:Test>false</m:Test>", b"<m:Test/>"),
    (b"<m:Test>false</m:Test>", b"<m:Test>False</m:Test>"),
    (b"<m:Test>false</m:Test>", b"<m:Test>unknown</m:Test>"),
    (b"<m:Test>false</m:Test>", b"<m:Test>false</m:Test><m:Test>true</m:Test>"),
    (b"<m:Test>false</m:Test>", b"<m:Test>false</m:Test><m:Test>false</m:Test>"),
    (b"<m:Test>false</m:Test>", b"<m:Test>false<m:Test>true</m:Test></m:Test>"),
    (b"<m:Test>false</m:Test>", b'<m:Test value="true">false</m:Test>'),
    (b"<m:Test>false</m:Test>", b'<m:Test m:value="true">false</m:Test>'),
    (b"<m:Test>false</m:Test>", b"<m:Test>false</m:Test>1"),
    (b"<m:Test>false</m:Test>", b"<c:Test>false</c:Test>"),
    (b"<m:Test>false</m:Test>", b"<m:Test>false</m:Test><c:Test>true</c:Test>"),
    (b"<m:Header>", b"<m:Header/><m:Header>"),
    (b"<m:Header>", b'<m:Header Test="true">'),
    (b"</m:Header>", b"</m:Header><c:Header><c:Test>true</c:Test></c:Header>"),
]


@pytest.mark.parametrize(
    "provider,index",
    [(p, i) for p in ("ECB", "ESTAT") for i in range(len(roles_for(p)))],
)
@pytest.mark.parametrize("old,new", HEADER_TEST_MUTATIONS)
def test_non_real_headers_stop_live_admission_and_retained_replay(
    tmp_path, provider, index, old, new
):
    c = changed(lean_candidate(provider), index, old, new)
    exchange = Exchange({provider: c})
    acquisition = source.Acquisition(tmp_path / "artifacts", source_plan())
    for _ in range(2):
        with pytest.raises(ContractError):
            acquisition.acquire(
                provider, enabled=True, exchange=exchange, clock=clock()
            )
    assert len(exchange.calls) == index + 1
    role = c.entities[index].role
    assert (
        acquisition.root / (provider + "." + role + ".bin")
    ).read_bytes() == c.entities[index].raw
    assert acquisition.counts() == {
        "logical": index + 1,
        "attempts": index + 1,
        "retries": 0,
        "fallbacks": 0,
    }
    with pytest.raises(ContractError):
        build(c)
    # A consistent retained carrier must fail the same shared header admission.
    doc = json.loads(build(lean_candidate(provider)).content)
    import base64

    raw = c.entities[index].raw
    doc["raw"][index].update(
        base64=base64.b64encode(raw).decode(), sha256=digest(raw), size=len(raw)
    )
    with pytest.raises(ContractError):
        sdmx.candidate_from_content(sdmx.encode(doc))


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("index_kind", ["metadata", "data"])
@pytest.mark.parametrize("marker", [b"true", b"ambiguous"])
def test_legacy_test_message_plan_rejected_by_publish_and_fresh_process_read(
    monkeypatch, provider, index_kind, marker
):
    original = lean_candidate(provider)
    index = 0 if index_kind == "metadata" else -1
    bad = changed(
        original, index, b"<m:Test>false</m:Test>", b"<m:Test>" + marker + b"</m:Test>"
    )
    # Reconstruct the old parser's exact projection, including changed header
    # XML and raw hashes. Rejection cannot depend on unrelated lineage damage.
    with monkeypatch.context() as legacy:
        legacy.setattr(sdmx, "_real_message", lambda _: None)
        p = build(bad)
    with pytest.raises(ContractError):
        sdmx.validate_plan(p)
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: AT + timedelta(seconds=40))
    with pytest.raises(ContractError):
        sdmx.publish_offline(kernel, p)
    assert ledger.snapshot() == InMemoryLedger().snapshot()
    with monkeypatch.context() as legacy:
        legacy.setattr(sdmx, "_real_message", lambda _: None)
        sdmx.publish_offline(kernel, p)
    before = ledger.snapshot()
    for version_identity in (None, p.version.identity):
        with pytest.raises(SdmxReadRejected):
            read(ledger, provider, version_identity=version_identity)
    assert_fresh_process_read(ledger, provider, p, rejected=True)
    assert ledger.snapshot() == before


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("marker", [b"false", b"0", b" \n false\t "])
def test_explicit_non_test_headers_roundtrip_without_qualification(
    tmp_path, provider, marker
):
    original = lean_candidate(provider)
    c = replace(
        original,
        entities=tuple(
            replace(
                e,
                raw=e.raw.replace(
                    b"<m:Test>false</m:Test>", b"<m:Test>" + marker + b"</m:Test>"
                ),
            )
            for e in original.entities
        ),
    )
    exchange = Exchange({provider: c})
    obtained = source.Acquisition(tmp_path / "artifacts", source_plan()).acquire(
        provider, enabled=True, exchange=exchange, clock=clock()
    )
    p = build(obtained)
    sdmx.validate_plan(p)
    assert sdmx.candidate_from_content(p.content) == obtained
    ledger = InMemoryLedger()
    sdmx.publish_offline(CanonicalKernel(ledger, lambda: AT + timedelta(seconds=40)), p)
    assert not read(ledger, provider).definition.source_qualified
    assert_fresh_process_read(ledger, provider, p, rejected=False)
    assert len(exchange.calls) == len(roles_for(provider))
