"""Synthetic grammar regressions plus explicit host-only historical byte replay.

No captured XML is checked in. --sdmx-historical-evidence must point at the
ignored original evidence directory; absent opt-in skips only historical tests.
"""

import hashlib
import json
import socket
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from test_sdmx_official import AT, assert_fresh_process_read, changed
from test_sdmx_source_compatibility import build, lean_candidate
from test_sdmx_source_storage import Exchange, Protection, clock, source_plan

from market_data_foundation.canonical.consumer_read import ReadContractError
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, digest
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import sdmx_materialization as storage
from market_data_foundation.historical import sdmx_official as sdmx
from market_data_foundation.historical import sdmx_source as source
from market_data_foundation.historical.bls_cpi_material_store import (
    InMemoryMaterialStore,
)
from market_data_foundation.historical.sdmx_contract import roles_for, surface_for
from market_data_foundation.historical.sdmx_local_read import (
    SdmxReadRejected,
    read_local_sdmx,
)

# Fixed identities independently provided in the read-only historical intake.
HISTORICAL = {
    "ECB": {
        "flow": (
            990,
            "36e36f8c406a42996b0a46b6e89f562558cac36b6159e5792ec27a96a793285c",
        ),
        "dsd": (
            24416,
            "6fb30d7a7dce0a85e1e3ae24e0d5548aae8c935f74ec8f3854cb0bfbef7b3c15",
        ),
        "data": (
            15283,
            "a02803b0df7572786db2494ec6c23166d1ee24cef8acb4d55022c3c312c04c15",
        ),
    },
    "ESTAT": {
        "flow": (
            9303,
            "5b502ea1ea8b3e383a5f4c51d31f7e76335152d6f7dc3c643b25d4065a22d79b",
        ),
        "dsd": (
            6024,
            "7e427bd24529f0245d90026edc67b58d7e2d12b6ddb92280ef4e4f56cc3dbd6d",
        ),
        "unit": (
            489788,
            "4620e188dae550d349ad85d6e0fa5c5cb85f794c44a5ca9553617ca3dfa800e0",
        ),
        "data": (
            1281,
            "e70e01b8f693aaed4e746f497f333e9265a50eb6d10b718741c6edc1329e6310",
        ),
    },
}


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("historical wire replay attempted external IO")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


def scoped_candidate(provider):
    """Small synthetic messages with the observed scopes, never official bytes."""
    c = lean_candidate(provider)
    s = surface_for(provider)
    c = changed(
        c, 0, b'class="DataStructure"', b'class="DataStructure" package="datastructure"'
    )
    c = changed(c, 1, b'class="Codelist"', b'class="Codelist" package="codelist"')
    scheme = "ECB_CONCEPTS" if provider == "ECB" else s.flow

    def concept(identity):
        return (
            f'<s:ConceptIdentity><Ref agencyID="{provider}" id="{identity}" '
            f'class="Concept" package="conceptscheme" maintainableParentID="{scheme}" '
            'maintainableParentVersion="4.2"/></s:ConceptIdentity>'
        ).encode()

    for dim in s.dimensions:
        position = s.dimensions.index(dim) + 1
        opening = f'<s:Dimension id="{dim}" position="{position}">'.encode()
        c = changed(c, 1, opening, opening + concept(dim))
    c = changed(
        c,
        1,
        b"</s:Enumeration>",
        b'</s:Enumeration><s:EnumerationFormat textType="String" '
        b'minLength="1" maxLength="8"/>',
    )
    time_position = len(s.dimensions) + 1
    c = changed(
        c,
        1,
        f'<s:TimeDimension id="TIME_PERIOD" position="{time_position}">'.encode(),
        f'<s:TimeDimension id="TIME_PERIOD" position="{time_position}">'.encode()
        + concept("TIME_PERIOD"),
    )
    c = changed(
        c,
        1,
        b'<s:PrimaryMeasure id="OBS_VALUE">',
        b'<s:PrimaryMeasure id="OBS_VALUE">' + concept("OBS_VALUE"),
    )
    attachment = (
        (
            f'<s:Group id="synthetic"><s:GroupDimension><s:DimensionReference>'
            f'<Ref id="{s.dimensions[0]}"/></s:DimensionReference>'
            "</s:GroupDimension></s:Group><s:AttributeList>"
            '<s:Attribute id="X_UNKNOWN" assignmentStatus="Conditional">'
        ).encode()
        + concept("X_UNKNOWN")
        + (
            b'<s:LocalRepresentation><s:TextFormat textType="String" maxLength="32"/>'
            b"</s:LocalRepresentation><s:AttributeRelationship><s:PrimaryMeasure>"
            b'<Ref id="OBS_VALUE"/></s:PrimaryMeasure></s:AttributeRelationship>'
            b"</s:Attribute></s:AttributeList>"
        )
    )
    c = changed(c, 1, b"</s:DimensionList>", b"</s:DimensionList>" + attachment)
    if provider == "ECB":
        c = changed(
            c,
            -1,
            b'<Ref agencyID="ECB" id="SYNTHETIC_DSD" version="1.0" '
            b'class="DataStructure"/>',
            b"<URN>urn:sdmx:org.sdmx.infomodel.datastructure.DataStructure="
            b"ECB:SYNTHETIC_DSD(1.0)</URN>",
        )
    else:
        c = changed(
            c,
            0,
            b'id="PRC_HICP_MINR" version="1.0"',
            b'id="PRC_HICP_MINR" version="9.7"',
        )
        c = changed(
            c,
            -1,
            b'<c:Structure><Ref agencyID="ESTAT" id="SYNTHETIC_DSD" '
            b'version="1.0" class="DataStructure"/></c:Structure>',
            b'<c:StructureUsage><Ref agencyID="ESTAT" id="PRC_HICP_MINR" '
            b'version="9.7"/></c:StructureUsage>',
        )
    return c


def roundtrip(c, *, fresh_process=True):
    p = build(c)
    sdmx.validate_plan(p)
    assert sdmx.candidate_from_content(p.content) == c
    assert [r.digest for r in p.raw_materials] == [digest(e.raw) for e in c.entities]
    ledger = InMemoryLedger()
    sdmx.publish_offline(CanonicalKernel(ledger, lambda: AT + timedelta(seconds=40)), p)
    restarted = InMemoryLedger.from_snapshot(ledger.snapshot())
    before = restarted.snapshot()
    for identity in (None, p.version.identity):
        with pytest.raises(ReadContractError):
            read_local_sdmx(
                restarted,
                provider=c.provider,
                start=c.start,
                end=c.end,
                as_of=c.entities[-1].obtained_at,
                version_identity=identity,
            )
        view = read_local_sdmx(
            restarted,
            provider=c.provider,
            start=c.start,
            end=c.end,
            as_of=AT + timedelta(seconds=100),
            version_identity=identity,
        )
        assert view.content == p.content
        assert sdmx.candidate_from_content(view.content) == c
        assert not view.definition.source_qualified
        assert view.version.frozen_at == AT + timedelta(seconds=30)
        assert view.publication.usable_at == AT + timedelta(seconds=40)
        assert [(a.requested_at, a.obtained_at) for a in view.acquisitions] == [
            (e.requested_at, e.obtained_at) for e in c.entities
        ]
    if fresh_process:
        assert_fresh_process_read(
            restarted, c.provider, p, rejected=False, start=c.start, end=c.end
        )
    assert restarted.snapshot() == before
    return p


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
def test_scoped_synthetic_references_roundtrip_and_source_pinning(tmp_path, provider):
    c = scoped_candidate(provider)
    p = roundtrip(c)
    projection = json.loads(p.content)["projection"]
    assert [r["unavailable"] for r in projection["observations"]] == [False, True, True]
    assert projection["metadata"]["dimensions"][0]["meaning"] is None
    exchange = Exchange({provider: c})
    obtained = source.Acquisition(tmp_path / "artifacts", source_plan()).acquire(
        provider, enabled=True, exchange=exchange, clock=clock()
    )
    assert tuple(r.raw for r in obtained.entities) == tuple(r.raw for r in c.entities)
    assert all(
        "/latest?" not in r.url for r in exchange.calls if r.role in {"dsd", "unit"}
    )


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
def test_scoped_prefixes_and_inert_annotations(provider):
    c = scoped_candidate(provider)
    annotation = (
        b"<c:Annotations><c:Annotation><c:AnnotationType>PROVIDER_METADATA_ONLY</c:AnnotationType>"
        b'<c:AnnotationText xml:lang="en">&lt;Ref id="opaque text"/&gt;'
        b"</c:AnnotationText>"
        b"</c:Annotation></c:Annotations>"
    )
    c = changed(c, 0, b"<s:Structure>", annotation + b"<s:Structure>")

    def prefixes(raw):
        for old, new in (
            (b"m", b"message"),
            (b"s", b"structure"),
            (b"c", b"common"),
            (b"g", b"generic"),
        ):
            raw = raw.replace(b"xmlns:" + old + b"=", b"xmlns:" + new + b"=")
            raw = raw.replace(b"<" + old + b":", b"<" + new + b":").replace(
                b"</" + old + b":", b"</" + new + b":"
            )
        return raw

    c = replace(c, entities=tuple(replace(e, raw=prefixes(e.raw)) for e in c.entities))
    roundtrip(c)


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
def test_retained_latest_urls_and_unknown_attempts_remain_honest(provider):
    c = scoped_candidate(provider)
    c = replace(
        c,
        entities=tuple(
            replace(
                e,
                attempts=None,
                url=e.url.rsplit("/", 1)[0] + "/latest?references=none"
                if e.role in {"dsd", "unit"}
                else e.url,
            )
            for e in c.entities
        ),
    )
    p = roundtrip(c)
    assert all(a.provider_data_attempts is None for a in p.acquisitions)


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("previous", ["t08-source-compat-r1", "t08-historical-wire-r1"])
def test_previous_contract_snapshot_is_not_auto_migrated(
    provider, previous, monkeypatch
):
    from market_data_foundation.historical import sdmx_contract as contract

    c = lean_candidate(provider)
    ledger = InMemoryLedger()
    with monkeypatch.context() as legacy:
        legacy.setattr(contract, "REVISION", previous)
        legacy.setattr(sdmx, "REVISION", previous)
        legacy.setattr(sdmx, "PARSER", previous + "-parser")
        p = build(c)
        sdmx.publish_offline(
            CanonicalKernel(ledger, lambda: AT + timedelta(seconds=40)), p
        )
    with pytest.raises(ContractError):
        sdmx.validate_plan(p)
    for identity in (None, p.version.identity):
        with pytest.raises((ReadContractError, SdmxReadRejected)):
            read_local_sdmx(
                InMemoryLedger.from_snapshot(ledger.snapshot()),
                provider=provider,
                start=c.start,
                end=c.end,
                as_of=AT + timedelta(seconds=100),
                version_identity=identity,
            )


SCOPE_FAILURES = [
    (0, b'package="datastructure"', b'package="codelist"'),
    (
        0,
        b'package="datastructure"',
        b'package="datastructure" maintainableParentID="OTHER"',
    ),
    (0, b"<Ref ", b"<c:Ref "),
    (1, b'package="codelist"', b'package="conceptscheme"'),
    (1, b'class="Concept"', b'class="Codelist"'),
    (1, b'maintainableParentVersion="4.2"', b'maintainableParentVersion="latest"'),
    (
        1,
        b'maintainableParentVersion="4.2"',
        b'maintainableParentVersion="4.2" version="4.2"',
    ),
    (1, b"<s:ConceptIdentity>", b'<s:ConceptIdentity other="ignored">'),
    (1, b"</s:ConceptIdentity>", b'<Ref id="OTHER"/></s:ConceptIdentity>'),
    (1, b'<Ref id="OBS_VALUE"/>', b'<Ref id="OTHER"/>'),
    (1, b'<Ref id="OBS_VALUE"/>', b'<Ref id="OBS_VALUE" class="PrimaryMeasure"/>'),
    (1, b"<s:EnumerationFormat ", b'<s:EnumerationFormat other="ignored" '),
    (1, b'maxLength="8"', b'maxLength="0"'),
    (1, b"</s:Enumeration>", b'</s:Enumeration><s:TextFormat textType="String"/>'),
    (1, b"</s:Enumeration>", b"<URN>OTHER</URN></s:Enumeration>"),
    (1, b"</s:DimensionList>", b"</s:DimensionList><s:AttributeList/>"),
]


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("index,old,new", SCOPE_FAILURES)
def test_scoped_reference_decoys_reject(provider, index, old, new, monkeypatch):
    c = scoped_candidate(provider)
    bad = changed(c, index, old, new)
    with pytest.raises((ContractError, ValueError)):
        build(bad)
    reject_legacy_snapshot(c, bad, monkeypatch)


def reject_legacy_snapshot(c, bad, monkeypatch):
    projection = sdmx.project(c)
    for before, after in zip(c.entities, bad.entities, strict=True):
        if before.raw == after.raw:
            continue
        if after.role == "data":
            for row in projection["observations"]:
                row["raw_sha256"] = digest(after.raw)
        else:
            projection["metadata"]["evidence"][after.role] = {
                "raw_sha256": digest(after.raw),
                "raw_size": len(after.raw),
            }
    with monkeypatch.context() as legacy:
        legacy.setattr(sdmx, "project", lambda _: projection)
        p = build(bad)
    with pytest.raises((ContractError, ValueError)):
        sdmx.validate_plan(p)
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: AT + timedelta(seconds=40))
    with pytest.raises((ContractError, ValueError)):
        sdmx.publish_offline(kernel, p)
    assert ledger.snapshot() == InMemoryLedger().snapshot()
    with monkeypatch.context() as legacy:
        legacy.setattr(sdmx, "validate_plan", lambda _: None)
        sdmx.publish_offline(kernel, p)
    restarted = InMemoryLedger.from_snapshot(ledger.snapshot())
    before = restarted.snapshot()
    for identity in (None, p.version.identity):
        with pytest.raises(SdmxReadRejected):
            read_local_sdmx(
                restarted,
                provider=c.provider,
                start=c.start,
                end=c.end,
                as_of=AT + timedelta(seconds=100),
                version_identity=identity,
            )
    assert_fresh_process_read(
        restarted, c.provider, p, rejected=True, start=c.start, end=c.end
    )
    assert restarted.snapshot() == before
    return p


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize(
    "fault", ["identity", "version", "extra", "text", "namespace", "attribute"]
)
def test_header_reference_rejects_through_old_snapshot(provider, fault, monkeypatch):
    c = scoped_candidate(provider)
    if provider == "ECB":
        old = (
            b"<URN>urn:sdmx:org.sdmx.infomodel.datastructure.DataStructure="
            b"ECB:SYNTHETIC_DSD(1.0)</URN>"
        )
        new = {
            "identity": old.replace(b"ECB:", b"ESTAT:"),
            "version": old.replace(b"(1.0)", b"(9.7)"),
            "extra": old + b'<Ref id="OTHER"/>',
            "text": old + b"OTHER",
            "namespace": old.replace(b"URN", b"c:URN"),
            "attribute": old.replace(b"<URN>", b'<URN id="OTHER">'),
        }[fault]
    else:
        old = b'<Ref agencyID="ESTAT" id="PRC_HICP_MINR" version="9.7"/>'
        new = {
            "identity": old.replace(b"PRC_HICP_MINR", b"SYNTHETIC_DSD"),
            "version": old.replace(b"9.7", b"1.0"),
            "extra": old + b'<Ref id="OTHER"/>',
            "text": old + b"OTHER",
            "namespace": old.replace(b"<Ref", b"<c:Ref"),
            "attribute": old.replace(b"/>", b' class="DataStructure"/>'),
        }[fault]
    bad = changed(c, -1, old, new)
    with pytest.raises((ContractError, ValueError)):
        build(bad)
    reject_legacy_snapshot(c, bad, monkeypatch)


@pytest.fixture(scope="module")
def historical_root(request):
    option = request.config.getoption("--sdmx-historical-evidence")
    if option is None:
        pytest.skip("requires --sdmx-historical-evidence; no implicit Source access")
    root = Path(option).resolve(strict=True)
    assert "artifacts" in root.parts
    # Reject checked-in fixtures or an unignored evidence directory.
    result = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-q", str(root / "index.json")],
        check=False,
    )
    assert result.returncode == 0
    return root


def load_historical(root, provider):
    index = json.loads((root / "index.json").read_bytes())
    entities = []
    files = {}
    for role in roles_for(provider):
        identity = index[provider][role]
        assert len(identity) == 32 and all(c in "0123456789abcdef" for c in identity)
        folder = root / identity
        raw = (folder / "entity.bin").read_bytes()
        receipt_bytes = (folder / "evidence.json").read_bytes()
        receipt = json.loads(receipt_bytes)
        assert (
            hashlib.sha256(receipt_bytes).hexdigest()
            == (folder / "evidence.sha256").read_text().strip()
        )
        assert (len(raw), digest(raw)) == HISTORICAL[provider][role]
        assert receipt["sha256"] == digest(raw) and receipt["bytes"] == len(raw)
        assert (
            receipt["provider"],
            receipt["resource"],
            receipt["status"],
            receipt["outcome"],
        ) == (provider, role, 200, "retained")
        assert (receipt["start"], receipt["end"]) == ("2026-07-01", "2026-09-16")
        entities.append(
            sdmx.RetainedEntity(
                role,
                receipt["url"],
                raw,
                datetime.fromisoformat(receipt["started_at"]),
                datetime.fromisoformat(receipt["obtained_at"]),
                attempts=None,
            )
        )
        for name in ("entity.bin", "evidence.json", "evidence.sha256"):
            path = folder / name
            files[path] = path.read_bytes()
    c = sdmx.SdmxCandidate(provider, "2026-07-01", "2026-09-16", tuple(entities))
    return c, files


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
def test_historical_original_wire_replay(historical_root, provider, tmp_path):
    c, files = load_historical(historical_root, provider)
    p = roundtrip(c)
    projection = json.loads(p.content)["projection"]
    assert all(a.provider_data_attempts is None for a in p.acquisitions)
    assert projection["provider_vintage"] is None
    assert not any(r["unavailable"] for r in projection["observations"])
    if provider == "ESTAT":
        assert projection["metadata"]["dsd"] == ["ESTAT", "PRC_HICP_MINR", "23.0"]
        assert projection["metadata"]["unit_codelist"] == ["ESTAT", "UNIT", "82.0"]
        assert projection["metadata"]["dimensions"][1]["meaning"] == "Index, 2025=100"
        assert [r["raw_value"] for r in projection["observations"]] == [
            "103.20",
            "103.36",
        ]
        assert [d["dimension"] for d in projection["metadata"]["dimensions"]] == [
            "freq",
            "unit",
            "coicop18",
            "geo",
        ]
    else:
        assert projection["metadata"]["dsd"] == ["ECB", "ECB_EXR1", "1.0"]
        assert projection["observations"][0]["raw_value"] == "1.1383"
        assert projection["observations"][-1]["raw_value"] == "1.1537"
    assert all(
        d["meaning"] is None
        for d in projection["metadata"]["dimensions"]
        if d["dimension"] != "unit"
    )
    ledger, store, protection = InMemoryLedger(), InMemoryMaterialStore(), Protection()
    durable, _ = storage.Handoff(tmp_path / "artifacts").materialize(
        CanonicalKernel(ledger, lambda: AT + timedelta(seconds=40)),
        p,
        store,
        protection,
        enabled=True,
    )
    assert store.create_attempts == len(c.entities) and protection.held == 0
    assert [m.digest for m in durable.raw_materials] == [
        digest(e.raw) for e in c.entities
    ]
    assert_fresh_process_read(
        ledger, provider, durable, rejected=False, start=c.start, end=c.end
    )
    assert {path: path.read_bytes() for path in files} == files


@pytest.mark.parametrize(
    "index,old,new",
    [
        (2, b"Index, 2025=100", b"Index, 2015=100"),
        (2, b'id="I25"', b'id="MISSING_I25"'),
        (2, b'version="82.0"', b'version="83.0"'),
        (
            2,
            b"Index, 2025=100</c:Name>",
            b"Index, 2025=100<c:Name>Index, 2015=100</c:Name></c:Name>",
        ),
        (
            1,
            b'id="UNIT" package="codelist" version="82.0"',
            b'id="UNIT" package="codelist" version="81.0"',
        ),
        (1, b'id="coicop18" position="3"', b'id="coicop18" position="4"'),
        (
            1,
            b'maintainableParentVersion="10.0"',
            b'maintainableParentVersion="10.0" version="10.0"',
        ),
        (-1, b'id="unit" value="I25"', b'id="unit" value="I15"'),
        (-1, b'id="coicop18" value="TOTAL"', b'id="coicop" value="TOTAL"'),
        (-1, b"<c:StructureUsage>", b'<c:StructureUsage><Ref id="OTHER"/>'),
    ],
)
def test_historical_estat_adversarial_copies_reject(
    historical_root, index, old, new, monkeypatch
):
    # Mutate only an in-memory adversarial copy; originals stay untouched.
    c, files = load_historical(historical_root, "ESTAT")
    bad = changed(c, index, old, new)
    with pytest.raises((ContractError, ValueError)):
        build(bad)
    reject_legacy_snapshot(c, bad, monkeypatch)
    assert {path: path.read_bytes() for path in files} == files


# Six mutations per provider reproduce all twelve professional-review cases.
REPRESENTATION_FAILURES = (
    "frequency_integer",
    "time_integer",
    "measure_integer",
    "measure_maxLength_one",
    "measure_enumerated",
    "measure_enumerated_zero",
)


def representation_copy(c, fault):
    """Mutate only parsed in-memory copies, never retained evidence files."""
    tree = ET.fromstring(c.entities[1].raw)
    ns = sdmx.NS

    def tag(name):
        return f"{{{ns['structure']}}}{name}"

    if fault == "frequency_integer":
        rep = tree.find(".//structure:Dimension/structure:LocalRepresentation", ns)
        fmt = rep.find("structure:EnumerationFormat", ns)
        if fmt is None:
            fmt = ET.SubElement(rep, tag("EnumerationFormat"))
        fmt.set("textType", "BigInteger")
    elif fault == "time_integer":
        tree.find(
            ".//structure:TimeDimension/structure:LocalRepresentation/structure:TextFormat",
            ns,
        ).set("textType", "BigInteger")
    else:
        rep = tree.find(
            ".//structure:MeasureList/structure:PrimaryMeasure/structure:LocalRepresentation",
            ns,
        )
        if fault == "measure_integer":
            rep[0].set("textType", "BigInteger")
        elif fault == "measure_maxLength_one":
            rep[0].set("maxLength", "1")
        else:
            rep.clear()
            enum = ET.SubElement(rep, tag("Enumeration"))
            ET.SubElement(
                enum,
                "Ref",
                agencyID=c.provider,
                id="DISALLOWED_MEASURE_CODES",
                version="1.0",
                package="codelist",
                **{"class": "Codelist"},
            )
            if fault == "measure_enumerated_zero":
                structures = tree.find("message:Structures", ns)
                lists = structures.find("structure:Codelists", ns)
                if lists is None:
                    lists = ET.SubElement(structures, tag("Codelists"))
                codes = ET.SubElement(
                    lists,
                    tag("Codelist"),
                    agencyID=c.provider,
                    id="DISALLOWED_MEASURE_CODES",
                    version="1.0",
                )
                zero = ET.SubElement(codes, tag("Code"), id="0")
                ET.SubElement(
                    zero, f"{{{ns['common']}}}Name", {sdmx.XML_LANG: "en"}
                ).text = "Zero"
    entities = list(c.entities)
    entities[1] = replace(entities[1], raw=ET.tostring(tree, encoding="utf-8"))
    return replace(c, entities=tuple(entities))


def reject_representation(c, bad, tmp_path, monkeypatch):
    with pytest.raises((ContractError, ValueError)):
        build(bad)
    forged = reject_legacy_snapshot(c, bad, monkeypatch)
    ledger, store, protection = InMemoryLedger(), InMemoryMaterialStore(), Protection()
    handoff = storage.Handoff(tmp_path / "artifacts" / "handoff")
    with pytest.raises((ContractError, ValueError)):
        handoff.materialize(
            CanonicalKernel(ledger, lambda: AT), forged, store, protection, enabled=True
        )
    assert not handoff.root.exists()
    assert store.create_attempts == protection.held == 0
    assert ledger.snapshot() == InMemoryLedger().snapshot()

    # Real filesystem retention/recovery with an injected offline exchange. URLs
    # are constructed by Acquisition from validated refs (including pinned DSD).
    calls = []

    def exchange(request):
        calls.append(request)
        raw = next(e.raw for e in bad.entities if e.role == request.role)
        return source.Response(
            200,
            raw,
            (("content-type", request.accept), ("content-length", str(len(raw)))),
        )

    root = tmp_path / "artifacts" / "source"
    acquisition = source.Acquisition(root, source.SourcePlan(c.start, c.end))
    with pytest.raises((ContractError, ValueError)):
        acquisition.acquire(c.provider, enabled=True, exchange=exchange, clock=clock())
    count = len(calls)
    assert 2 <= count <= len(c.entities)
    assert (root / f"{c.provider}.dsd.bin").read_bytes() == bad.entities[1].raw
    # Reopening and revalidating a retained successful HTTP200 never re-requests.
    with pytest.raises((ContractError, ValueError)):
        source.Acquisition(root, acquisition.plan).acquire(
            c.provider, enabled=True, exchange=lambda _: pytest.fail("repeat Source IO")
        )
    assert len(calls) == count
    assert acquisition.counts() == {
        "logical": count,
        "attempts": count,
        "retries": 0,
        "fallbacks": 0,
    }


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("fault", REPRESENTATION_FAILURES)
def test_representation_review_synthetic_closure(
    provider, fault, tmp_path, monkeypatch
):
    c = scoped_candidate(provider)
    reject_representation(c, representation_copy(c, fault), tmp_path, monkeypatch)


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("fault", REPRESENTATION_FAILURES)
def test_historical_representation_twelve_review_closures(
    historical_root, provider, fault, tmp_path, monkeypatch
):
    c, files = load_historical(historical_root, provider)
    reject_representation(c, representation_copy(c, fault), tmp_path, monkeypatch)
    assert {path: path.read_bytes() for path in files} == files


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize(
    "component,field,value",
    [
        ("TimeDimension", "textType", "String"),
        ("TimeDimension", "textType", "Duration"),
        ("TimeDimension", "maxLength", "1"),
        ("TimeDimension", "minLength", "40"),
        ("TimeDimension", "isSequence", "true"),
        ("PrimaryMeasure", "minLength", "10"),
        ("PrimaryMeasure", "isSequence", "true"),
        ("PrimaryMeasure", "textType", "ObservationalTimePeriod"),
        ("PrimaryMeasure", "remove", "LocalRepresentation"),
        ("TimeDimension", "remove", "LocalRepresentation"),
    ],
)
def test_representation_unsupported_or_conflicting_replay(
    provider, component, field, value, monkeypatch
):
    c = scoped_candidate(provider)
    tree = ET.fromstring(c.entities[1].raw)
    path = (
        ".//structure:MeasureList/structure:PrimaryMeasure"
        if component == "PrimaryMeasure"
        else ".//structure:TimeDimension"
    )
    node = tree.find(path, sdmx.NS)
    rep = node.find("structure:LocalRepresentation", sdmx.NS)
    if field == "remove":
        node.remove(rep)
    else:
        rep[0].set(field, value)
    bad = replace(
        c,
        entities=(
            c.entities[0],
            replace(c.entities[1], raw=ET.tostring(tree, encoding="utf-8")),
            *c.entities[2:],
        ),
    )
    with pytest.raises((ContractError, ValueError)):
        build(bad)
    reject_legacy_snapshot(c, bad, monkeypatch)


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize(
    "kind,valid,invalid",
    [
        ("BigInteger", "+123", "1.2"),
        ("BigInteger", "123", "１２３"),
        ("Double", "1.25E2", "NaN"),
        ("Double", "1.25", "1e309"),
        ("Double", "0", "1e-999"),
        ("String", "x", "x" * 33),
    ],
)
def test_attribute_representation_is_lexical_and_role_specific(
    provider, kind, valid, invalid, monkeypatch
):
    c = scoped_candidate(provider)
    c = changed(
        c,
        1,
        b'textType="String" maxLength="32"',
        f'textType="{kind}" maxLength="32"'.encode(),
    )
    c = changed(c, -1, b'value="preserve-me"', f'value="{valid}"'.encode())
    roundtrip(c)
    bad = changed(c, -1, f'value="{valid}"'.encode(), f'value="{invalid}"'.encode())
    with pytest.raises((ContractError, ValueError)):
        build(bad)
    reject_legacy_snapshot(c, bad, monkeypatch)


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
def test_attribute_integer_enumeration_checks_actual_code(provider, monkeypatch):
    c = scoped_candidate(provider)
    tree = ET.fromstring(c.entities[1].raw)
    ns = sdmx.NS
    node = tree.find('.//structure:Attribute[@id="X_UNKNOWN"]', ns)
    rep = node.find("structure:LocalRepresentation", ns)
    rep.clear()
    tag = "{" + ns["structure"] + "}"
    enum = ET.SubElement(rep, tag + "Enumeration")
    ET.SubElement(
        enum,
        "Ref",
        agencyID=provider,
        id="SYNTHETIC_INTEGER_CODES",
        version="1.0",
        package="codelist",
        **{"class": "Codelist"},
    )
    ET.SubElement(
        rep,
        tag + "EnumerationFormat",
        textType="BigInteger",
        minLength="1",
        maxLength="2",
    )
    lists = ET.SubElement(tree.find("message:Structures", ns), tag + "Codelists")
    codes = ET.SubElement(
        lists,
        tag + "Codelist",
        agencyID=provider,
        id="SYNTHETIC_INTEGER_CODES",
        version="1.0",
    )
    code = ET.SubElement(codes, tag + "Code", id="4")
    ET.SubElement(
        code, "{" + ns["common"] + "}Name", {sdmx.XML_LANG: "en"}
    ).text = "Four"
    c = replace(
        c,
        entities=(
            c.entities[0],
            replace(c.entities[1], raw=ET.tostring(tree, encoding="utf-8")),
            *c.entities[2:],
        ),
    )
    c = changed(c, -1, b'value="preserve-me"', b'value="4"')
    roundtrip(c)
    bad = changed(c, -1, b'value="4"', b'value="5"')
    with pytest.raises(ContractError, match="outside_enumeration"):
        build(bad)
    reject_legacy_snapshot(c, bad, monkeypatch)
