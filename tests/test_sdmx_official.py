"""Synthetic SDMX messages only. No fixture is a captured official response."""

import base64
import copy
import json
import socket
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import escape

import psycopg
import pytest

from market_data_foundation.canonical import codec
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
)
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    ContractError,
    Decision,
    Grade,
    digest,
)
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import sdmx_official as sdmx
from market_data_foundation.historical.sdmx_contract import definition_for
from market_data_foundation.historical.sdmx_local_read import (
    SdmxReadRejected,
    read_local_sdmx,
)

AT = datetime(2026, 10, 17, tzinfo=UTC)
START, END = "2026-07-01", "2026-09-30"
# Independent fixture declarations; deliberately synthetic DSD IDs.
FIXTURES = {
    "ECB": (
        "https://data-api.ecb.europa.eu/service",
        "EXR",
        (
            ("FREQ", "D", "Daily"),
            ("CURRENCY", "USD", "US dollar"),
            ("CURRENCY_DENOM", "EUR", "Euro"),
            ("EXR_TYPE", "SP00", "Spot"),
            ("EXR_SUFFIX", "A", "Average"),
        ),
        ("2026-07-01", "2026-07-02", "2026-09-30"),
    ),
    "ESTAT": (
        "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1",
        "PRC_HICP_MINR",
        (
            ("freq", "M", "Monthly"),
            ("unit", "I25", "Index, 2025=100"),
            ("coicop18", "TOTAL", "Total"),
            ("geo", "DE", "Germany"),
        ),
        ("2026-07", "2026-08", "2026-09"),
    ),
}
DECLARATIONS = (
    'xmlns:m="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message" '
    'xmlns:s="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure" '
    'xmlns:c="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common" '
    'xmlns:g="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/generic"'
)


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("T08 deterministic tests attempted external IO")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


def candidate(provider="ECB", at=AT, start=START, end=END):
    base, flow, dims, periods = FIXTURES[provider]
    key = ".".join(code for _, code, _ in dims)
    dsd_id = "SYNTHETIC_DSD"

    def ref(identity, cls):
        return (
            f'<Ref agencyID="{provider}" id="{identity}" version="1.0" class="{cls}"/>'
        )

    def message(body):
        return (
            f"<m:Structure {DECLARATIONS}>"
            "<m:Header><m:Test>false</m:Test></m:Header>"
            f"<m:Structures>{body}"
            "</m:Structures></m:Structure>"
        ).encode()

    flow_xml = message(
        f'<s:Dataflows><s:Dataflow agencyID="{provider}" id="{flow}" version="1.0">'
        f"<s:Structure>{ref(dsd_id, 'DataStructure')}</s:Structure>"
        "</s:Dataflow></s:Dataflows>"
    )
    dimensions = "".join(
        f'<s:Dimension id="{dim}" position="{i}"><s:LocalRepresentation>'
        "<s:Enumeration>"
        f"{ref('UNIT' if dim == 'unit' else 'CL_' + dim, 'Codelist')}"
        "</s:Enumeration>"
        "</s:LocalRepresentation></s:Dimension>"
        for i, (dim, _, _) in enumerate(dims, 1)
    )
    codelists = "".join(
        f'<s:Codelist agencyID="{provider}" '
        f'id="{"UNIT" if dim == "unit" else "CL_" + dim}" version="1.0">'
        f'<s:Code id="{code}"><c:Name xml:lang="en">{escape(name)}</c:Name>'
        "</s:Code></s:Codelist>"
        for dim, code, name in dims
    )
    measure_format = (
        'textType="String" maxLength="30" isSequence="false"'
        if provider == "ECB"
        else 'textType="Double"'
    )
    dsd_xml = message(
        f"<s:Codelists>{codelists}</s:Codelists><s:DataStructures>"
        f'<s:DataStructure agencyID="{provider}" id="{dsd_id}" version="1.0">'
        "<s:DataStructureComponents><s:DimensionList>"
        + dimensions
        + f'<s:TimeDimension id="TIME_PERIOD" position="{len(dims) + 1}">'
        '<s:LocalRepresentation><s:TextFormat textType="ObservationalTimePeriod"/>'
        "</s:LocalRepresentation></s:TimeDimension>"
        '</s:DimensionList><s:MeasureList><s:PrimaryMeasure id="OBS_VALUE">'
        f"<s:LocalRepresentation><s:TextFormat {measure_format}/>"
        "</s:LocalRepresentation></s:PrimaryMeasure>"
        "</s:MeasureList></s:DataStructureComponents></s:DataStructure>"
        "</s:DataStructures>"
    )
    unit_xml = message(
        '<s:Codelists><s:Codelist agencyID="ESTAT" id="UNIT" version="1.0">'
        '<s:Code id="I25"><c:Name xml:lang="en">Index, 2025=100</c:Name>'
        "</s:Code></s:Codelist></s:Codelists>"
    )
    series_key = "".join(f'<g:Value id="{d}" value="{c}"/>' for d, c, _ in dims)
    # A numeric string, an explicit missing marker, and a row without ObsValue.
    observations = (
        f'<g:Obs><g:ObsDimension value="{periods[0]}"/>'
        '<g:ObsValue value="123.4500"/><g:Attributes>'
        '<g:Value id="OBS_STATUS" value="P"/>'
        '<g:Value id="X_UNKNOWN" value="preserve-me"/></g:Attributes></g:Obs>'
        f'<g:Obs><g:ObsDimension value="{periods[1]}"/><g:ObsValue value=":"/>'
        '<g:Attributes><g:Value id="OBS_STATUS" value="M"/></g:Attributes></g:Obs>'
        f'<g:Obs><g:ObsDimension value="{periods[2]}"/></g:Obs>'
    )
    data_xml = (
        f"<m:GenericData {DECLARATIONS}><m:Header>"
        "<m:ID>synthetic-not-official</m:ID><m:Test>false</m:Test>"
        "<m:Prepared>2026-10-01T00:00:00Z</m:Prepared>"
        '<m:Structure structureID="synthetic" dimensionAtObservation="TIME_PERIOD">'
        f"<c:Structure>{ref(dsd_id, 'DataStructure')}</c:Structure>"
        "</m:Structure></m:Header>"
        '<m:DataSet structureRef="synthetic" action="Replace" '
        'validFromDate="2026-10-01T00:00:00Z">'
        '<g:Attributes><g:Value id="LAST_UPDATE" value="2026-10-01T00:00:00Z"/>'
        "</g:Attributes><g:Series>"
        f"<g:SeriesKey>{series_key}</g:SeriesKey><g:Attributes>"
        '<g:Value id="UNIT_MULT" value="0"/>'
        f'<g:Value id="UNIT" value="{"USD" if provider == "ECB" else "I25"}"/>'
        "</g:Attributes>"
        f"{observations}</g:Series></m:DataSet></m:GenericData>"
    ).encode()
    roles = ("flow", "dsd") + (("unit",) if provider == "ESTAT" else ()) + ("data",)
    urls = (
        (
            f"{base}/dataflow/{provider}/{flow}/latest?references=none",
            f"{base}/datastructure/{provider}/{dsd_id}/1.0?references=none",
        )
        + (
            (f"{base}/codelist/ESTAT/UNIT/1.0?references=none",)
            if provider == "ESTAT"
            else ()
        )
        + (f"{base}/data/{flow}/{key}?startPeriod={start}&endPeriod={end}",)
    )
    raws = (
        (flow_xml, dsd_xml) + ((unit_xml,) if provider == "ESTAT" else ()) + (data_xml,)
    )
    entities = tuple(
        sdmx.RetainedEntity(
            role,
            url,
            raw,
            at + timedelta(seconds=i * 2),
            at + timedelta(seconds=i * 2 + 1),
            attempts=i % 3 + 1,
        )
        for i, (role, url, raw) in enumerate(zip(roles, urls, raws, strict=True))
    )
    return sdmx.SdmxCandidate(provider, start, end, entities)


def changed(c, index, old, new):
    entities = list(c.entities)
    assert old in entities[index].raw
    entities[index] = replace(
        entities[index], raw=entities[index].raw.replace(old, new)
    )
    return replace(c, entities=tuple(entities))


def plan(provider="ECB", identity="test", at=AT, **kw):
    return sdmx.build_plan(
        candidate(provider, at, **kw),
        identity=identity,
        finished_at=at + timedelta(seconds=8),
        frozen_at=at + timedelta(seconds=9),
    )


def read(ledger, provider="ECB", at=AT + timedelta(seconds=100), **kw):
    return read_local_sdmx(
        ledger, provider=provider, start=START, end=END, as_of=at, **kw
    )


def published(provider="ECB", identity="test", at=AT):
    p = plan(provider, identity, at)
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: at + timedelta(seconds=10))
    publication = sdmx.publish_offline(kernel, p)
    return p, ledger, kernel, publication


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
def test_projection_source_semantics_and_missing(provider):
    c = candidate(provider)
    projection = sdmx.project(c)
    metadata = projection["metadata"]
    assert metadata["agency"] == provider
    assert metadata["dataflow"][1] == FIXTURES[provider][1] == metadata["dataset"]
    assert metadata["dsd"] == (provider, "SYNTHETIC_DSD", "1.0")
    assert projection["dataset_attributes"]["LAST_UPDATE"] == "2026-10-01T00:00:00Z"
    assert projection["dataset_xml_attributes"]["validFromDate"].startswith(
        "2026-10-01"
    )
    rows = projection["observations"]
    assert rows[0]["value"] == rows[0]["raw_value"] == "123.4500"
    assert rows[0]["attributes"] == {"OBS_STATUS": "P", "X_UNKNOWN": "preserve-me"}
    assert [r["unavailable"] for r in rows] == [False, True, True]
    assert [r["value"] for r in rows[1:]] == [None, None]
    assert [r["raw_value"] for r in rows[1:]] == [":", None]
    assert all(r["raw_sha256"] == digest(c.entities[-1].raw) for r in rows)
    assert rows[1]["pointer"].endswith("g:Obs[2]")
    assert projection["provider_vintage"] is None
    assert projection["provider_revision_state"] == "not_asserted"
    if provider == "ECB":
        assert metadata["unit"] == "USD per EUR"
        assert "2026-07-03" not in [r["period"] for r in rows]  # omitted != missing
    else:
        assert metadata["unit"] == "index 2025=100"
        assert metadata["dimensions"][1]["meaning"] == "Index, 2025=100"


UNSUPPORTED_OBS_VALUES = [
    b"<g:ObsValue>123.4500</g:ObsValue>",
    b"<g:ObsValue>:</g:ObsValue>",
    b'<g:ObsValue value="123.4500">456</g:ObsValue>',
    b'<g:ObsValue value=":">123.4500</g:ObsValue>',
    b'<g:ObsValue><g:Value value="123.4500"/></g:ObsValue>',
    b'<g:ObsValue value="123.4500"><g:Value/></g:ObsValue>',
    b'<g:ObsValue value=":"><g:Value/></g:ObsValue>',
    b'<g:ObsValue amount="123.4500"/>',
    b'<g:ObsValue value="123.4500" other="ignored"/>',
    b'<g:ObsValue value=":" other="ignored"/>',
    b'<g:ObsValue g:value="123.4500"/>',
    b'<g:ObsValue xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    b'xsi:nil="true"/>',
    b'<g:ObsValue value="123.4500"/>456',
]

EXTRA_ATTACHMENT = (
    b'<s:DataStructure><Ref agencyID="OTHER" id="OTHER" version="1.0" '
    b'class="DataStructure"/></s:DataStructure>'
)
METADATA_SHAPE_MUTATIONS = [
    (0, b"<m:Structures>", b"<m:Structures/><m:Structures>"),
    (0, b"<s:Dataflows>", b"<s:Dataflows/><s:Dataflows>"),
    (0, b"<s:Structure>", b"<s:Structure/><s:Structure>"),
    (0, b"</s:Structure>", b"<URN>OTHER</URN></s:Structure>"),
    (0, b"</s:Structure>", b"<s:StructureUsage/></s:Structure>"),
    (0, b"<s:Structure>", b'<s:Structure other="ignored">'),
    (0, b'class="DataStructure"/>', b'class="DataStructure">OTHER</Ref>'),
    (
        0,
        b'class="DataStructure"/>',
        b'class="DataStructure"><Ref id="OTHER"/></Ref>',
    ),
    (0, b'class="DataStructure"/>', b'class="DataStructure" urn="OTHER"/>'),
    (1, b"<s:DataStructures>", b"<s:DataStructures/><s:DataStructures>"),
    (1, b"<s:Codelists>", b"<s:Codelists/><s:Codelists>"),
    (
        1,
        b"<s:DataStructureComponents>",
        b"<s:DataStructureComponents/><s:DataStructureComponents>",
    ),
    (1, b"<s:DimensionList>", b"<s:DimensionList/><s:DimensionList>"),
    (1, b"<s:MeasureList>", b"<s:MeasureList/><s:MeasureList>"),
    (1, b"<s:LocalRepresentation>", b"<s:LocalRepresentation/><s:LocalRepresentation>"),
    (1, b"<s:Enumeration>", b"<s:Enumeration/><s:Enumeration>"),
    (1, b"<s:LocalRepresentation>", b'<s:LocalRepresentation other="ignored">'),
    (1, b"</s:Enumeration>", b"<URN>OTHER</URN></s:Enumeration>"),
    (1, b"</s:LocalRepresentation>", b"<s:TextFormat/></s:LocalRepresentation>"),
    (1, b"</s:Enumeration>", b"OTHER</s:Enumeration>"),
    (0, b"</s:Structure>", EXTRA_ATTACHMENT + b"</s:Structure>"),
    (1, b"</s:Enumeration>", EXTRA_ATTACHMENT + b"</s:Enumeration>"),
    (1, b"</s:LocalRepresentation>", b"<Ref/></s:LocalRepresentation>"),
    (-1, b"<c:Structure>", b"<c:Structure/><c:Structure>"),
    (-1, b"</c:Structure>", b"<URN>OTHER</URN></c:Structure>"),
    (-1, b"</m:Structure>", b"<c:StructureUsage/></m:Structure>"),
]


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("expression", UNSUPPORTED_OBS_VALUES)
def test_unsupported_obsvalue_expressions_fail_closed(provider, expression):
    c = changed(candidate(provider), -1, b'<g:ObsValue value="123.4500"/>', expression)
    with pytest.raises(ContractError, match="sdmx_unsupported_obs_value"):
        sdmx.project(c)
    with pytest.raises(ContractError, match="sdmx_unsupported_obs_value"):
        sdmx.build_plan(
            c,
            identity="bad-value",
            finished_at=AT + timedelta(seconds=8),
            frozen_at=AT + timedelta(seconds=9),
        )


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize(("index", "old", "new"), METADATA_SHAPE_MUTATIONS)
def test_metadata_reference_shapes_fail_closed(provider, index, old, new):
    c = changed(candidate(provider), index, old, new)
    with pytest.raises(ContractError, match="sdmx_"):
        sdmx.project(c)
    with pytest.raises(ContractError, match="sdmx_"):
        sdmx.build_plan(
            c,
            identity="bad-ref",
            finished_at=AT + timedelta(seconds=8),
            frozen_at=AT + timedelta(seconds=9),
        )


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize(
    ("expression", "raw_value"),
    [
        (b"", None),
        (b"<g:ObsValue/>", None),
        (b'<g:ObsValue value=""/>', ""),
        (b'<g:ObsValue value=":"/>', ":"),
        (b"<g:ObsValue> \n </g:ObsValue>", None),
        (b'<g:ObsValue value="123.4500"> \n </g:ObsValue>', "123.4500"),
    ],
)
def test_supported_obsvalue_roundtrip(provider, expression, raw_value):
    c = changed(candidate(provider), -1, b'<g:ObsValue value="123.4500"/>', expression)
    p = sdmx.build_plan(
        c,
        identity="supported-value",
        finished_at=AT + timedelta(seconds=8),
        frozen_at=AT + timedelta(seconds=9),
    )
    sdmx.validate_plan(p)
    ledger = InMemoryLedger()
    sdmx.publish_offline(CanonicalKernel(ledger, lambda: AT + timedelta(seconds=10)), p)
    restarted = InMemoryLedger.from_snapshot(ledger.snapshot())
    before = restarted.snapshot()
    for version_identity in (None, p.version.identity):
        view = read(restarted, provider, version_identity=version_identity)
        doc = json.loads(view.content)
        row = doc["projection"]["observations"][0]
        assert row["raw_value"] == raw_value
        assert row["unavailable"] == (raw_value != "123.4500")
        assert row["value"] == ("123.4500" if raw_value == "123.4500" else None)
        assert len(view.acquisitions) == len(doc["raw"]) == len(c.entities)
        assert sdmx.candidate_from_content(view.content) == c
        assert not view.definition.source_qualified
    assert restarted.snapshot() == before


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
def test_minimal_metadata_references_allow_formatting_whitespace(provider):
    c = candidate(provider)
    c = replace(
        c,
        entities=tuple(
            replace(entity, raw=entity.raw.replace(b"><", b"> \n <"))
            for entity in c.entities
        ),
    )
    p = sdmx.build_plan(
        c,
        identity="formatted",
        finished_at=AT + timedelta(seconds=8),
        frozen_at=AT + timedelta(seconds=9),
    )
    sdmx.validate_plan(p)
    ledger = InMemoryLedger()
    sdmx.publish_offline(CanonicalKernel(ledger, lambda: AT + timedelta(seconds=10)), p)
    restarted = InMemoryLedger.from_snapshot(ledger.snapshot())
    for version_identity in (None, p.version.identity):
        view = read(restarted, provider, version_identity=version_identity)
        assert sdmx.candidate_from_content(view.content) == c


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("finding", ["obs-text", "local-representation", "attachment"])
def test_review_findings_rejected_at_validation_publish_and_restarted_read(
    provider, finding, monkeypatch
):
    original = candidate(provider)
    if finding == "obs-text":
        index, old, new = (
            -1,
            b'<g:ObsValue value="123.4500"/>',
            b"<g:ObsValue>123.4500</g:ObsValue>",
        )
        classification = "sdmx_unsupported_obs_value"
    elif finding == "local-representation":
        index, old, new = (
            1,
            b"<s:LocalRepresentation>",
            b"<s:LocalRepresentation/><s:LocalRepresentation>",
        )
        classification = "sdmx_missing_or_ambiguous_component"
    else:
        index, old, new = (
            0,
            b"</s:Structure>",
            EXTRA_ATTACHMENT + b"</s:Structure>",
        )
        classification = "sdmx_unsupported_reference_structure"
    bad = changed(original, index, old, new)
    # Construct the internally consistent graph previously accepted by R1.
    # Only the old projection is supplied; all raw hashes/lineage are built normally.
    projection = sdmx.project(original)
    if finding == "obs-text":
        projection["observations"][0].update(
            raw_value=None, value=None, unavailable=True
        )
        for row in projection["observations"]:
            row["raw_sha256"] = digest(bad.entities[-1].raw)
    else:
        entity = bad.entities[index]
        projection["metadata"]["evidence"][entity.role] = {
            "raw_sha256": digest(entity.raw),
            "raw_size": len(entity.raw),
        }
    with monkeypatch.context() as legacy:
        legacy.setattr(sdmx, "project", lambda _: projection)
        p = sdmx.build_plan(
            bad,
            identity="legacy",
            finished_at=AT + timedelta(seconds=8),
            frozen_at=AT + timedelta(seconds=9),
        )
    assert sdmx.candidate_from_content(p.content) == bad
    with pytest.raises(ContractError, match=classification):
        sdmx.validate_plan(p)
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: AT + timedelta(seconds=10))
    before = ledger.snapshot()
    with pytest.raises(ContractError, match=classification):
        sdmx.publish_offline(kernel, p)
    assert ledger.snapshot() == before
    # Model an already-published R1 snapshot without bypassing the new Read parser.
    with monkeypatch.context() as legacy:
        legacy.setattr(sdmx, "validate_plan", lambda _: None)
        sdmx.publish_offline(kernel, p)
    restarted = InMemoryLedger.from_snapshot(ledger.snapshot())
    before = restarted.snapshot()
    for version_identity in (None, p.version.identity):
        with pytest.raises(SdmxReadRejected):
            read(restarted, provider, version_identity=version_identity)
    assert restarted.snapshot() == before


def assert_fresh_process_read(ledger, provider, p, *, rejected, start=START, end=END):
    script = """
import json
import socket
import sys
from datetime import datetime

sys.path.insert(0, "src")
import psycopg
from market_data_foundation.canonical.model import digest
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical.sdmx_local_read import (
    SdmxReadRejected, read_local_sdmx,
)

def forbidden(*args, **kwargs):
    raise AssertionError("fresh SDMX Read attempted external IO")

socket.socket.connect = socket.create_connection = socket.getaddrinfo = forbidden
psycopg.connect = forbidden
job = json.load(sys.stdin)
ledger = InMemoryLedger.from_snapshot(job["snapshot"])
before = ledger.snapshot()
for version_identity in (None, job["version"]):
    try:
        view = read_local_sdmx(
            ledger, provider=job["provider"], start=job["start"], end=job["end"],
            as_of=datetime.fromisoformat(job["as_of"]),
            version_identity=version_identity,
        )
    except SdmxReadRejected:
        assert job["rejected"]
    else:
        assert not job["rejected"]
        assert digest(view.content) == job["content_digest"]
assert ledger.snapshot() == before
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(
            {
                "snapshot": ledger.snapshot(),
                "provider": provider,
                "version": p.version.identity,
                "start": start,
                "end": end,
                "as_of": (AT + timedelta(seconds=100)).isoformat(),
                "content_digest": digest(p.content),
                "rejected": rejected,
            }
        ),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


R3_LEAF_CASES = (
    [
        f"dimension-{shape}-{state}"
        for shape in ("text", "tail", "child")
        for state in ("missing", "numeric")
    ]
    + ["name-child", "name-tail"]
    + [
        f"attribute-{level}-{shape}"
        for level in ("dataset", "series", "observation")
        for shape in ("child", "text", "tail")
    ]
)


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("finding", R3_LEAF_CASES)
def test_r3_semantic_leaves_fail_closed_through_restarted_read(
    provider, finding, monkeypatch
):
    original = candidate(provider)
    index = -1
    if finding.startswith("dimension-"):
        _, shape, state = finding.split("-")
        period = FIXTURES[provider][3][0].encode()
        dimension = b'<g:ObsDimension value="' + period + b'"'
        value = b'<g:ObsValue value="123.4500"/>'
        old = dimension + b"/>" + value
        new = {
            "text": dimension + b">123.4500</g:ObsDimension>",
            "tail": dimension + b"/>123.4500",
            "child": dimension + b">" + value + b"</g:ObsDimension>",
        }[shape] + (value if state == "numeric" else b"")
        classification = "sdmx_unsupported_obs_dimension"
    elif finding.startswith("name-"):
        index = 1
        name, conflict = (
            (b"US dollar", b"Euro")
            if provider == "ECB"
            else (b"Index, 2025=100", b"Index, 2015=100")
        )
        old = name + b"</c:Name>"
        new = (
            name + b"<c:Name>" + conflict + b"</c:Name></c:Name>"
            if finding == "name-child"
            else old + conflict
        )
        classification = "sdmx_unsupported_code_name"
    else:
        _, level, shape = finding.split("-")
        key, value = {
            "dataset": (b"LAST_UPDATE", b"2026-10-01T00:00:00Z"),
            "series": (b"UNIT_MULT", b"0"),
            "observation": (b"OBS_STATUS", b"P"),
        }[level]
        leaf = b'<g:Value id="' + key + b'" value="' + value + b'"'
        old = leaf + b"/>"
        new = {
            "child": leaf + b'><g:Value id="UNIT_MULT" value="3"/></g:Value>',
            "text": leaf + b">3</g:Value>",
            "tail": old + b"3",
        }[shape]
        classification = "sdmx_attribute_ambiguous"
    bad = changed(original, index, old, new)
    with pytest.raises(ContractError, match=classification):
        sdmx.project(bad)
    with pytest.raises(ContractError, match=classification):
        sdmx.build_plan(
            bad,
            identity="bad-leaf",
            finished_at=AT + timedelta(seconds=8),
            frozen_at=AT + timedelta(seconds=9),
        )

    # Reproduce R2's accepted projection, retaining consistent raw hashes,
    # material identities and lineage rather than merely corrupting a good graph.
    projection = sdmx.project(original)
    if index == 1:
        projection["metadata"]["evidence"]["dsd"] = {
            "raw_sha256": digest(bad.entities[1].raw),
            "raw_size": len(bad.entities[1].raw),
        }
    else:
        if finding.startswith("dimension-") and finding.endswith("-missing"):
            projection["observations"][0].update(
                raw_value=None, value=None, unavailable=True
            )
        for row in projection["observations"]:
            row["raw_sha256"] = digest(bad.entities[-1].raw)
    with monkeypatch.context() as legacy:
        legacy.setattr(sdmx, "project", lambda _: projection)
        p = sdmx.build_plan(
            bad,
            identity="legacy-r2",
            finished_at=AT + timedelta(seconds=8),
            frozen_at=AT + timedelta(seconds=9),
        )
    assert sdmx.candidate_from_content(p.content) == bad
    with pytest.raises(ContractError, match=classification):
        sdmx.validate_plan(p)
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: AT + timedelta(seconds=10))
    before = ledger.snapshot()
    with pytest.raises(ContractError, match=classification):
        sdmx.publish_offline(kernel, p)
    assert ledger.snapshot() == before
    with monkeypatch.context() as legacy:
        legacy.setattr(sdmx, "validate_plan", lambda _: None)
        sdmx.publish_offline(kernel, p)
    restarted = InMemoryLedger.from_snapshot(ledger.snapshot())
    before = restarted.snapshot()
    for version_identity in (None, p.version.identity):
        with pytest.raises(SdmxReadRejected):
            read(restarted, provider, version_identity=version_identity)
    assert restarted.snapshot() == before
    assert_fresh_process_read(restarted, provider, p, rejected=True)


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("raw_value", [None, "", ":", "123.4500"])
def test_r3_plain_semantic_leaves_roundtrip(provider, raw_value):
    c = candidate(provider)
    period = FIXTURES[provider][3][0].encode()
    c = changed(
        c,
        -1,
        b'<g:ObsDimension value="' + period + b'"/>',
        b'<g:ObsDimension value="' + period + b'"> \n </g:ObsDimension> \n ',
    )
    c = changed(
        c,
        -1,
        b'<g:ObsValue value="123.4500"/>',
        b"" if raw_value is None else f'<g:ObsValue value="{raw_value}"/>'.encode(),
    )
    # Plain Name text, attribute leaf whitespace, and multilingual names remain
    # legal. Provider update metadata and unknown attribute codes stay intact.
    c = changed(c, 1, b"</c:Name>", b" \n </c:Name> \n ")
    c = changed(c, 1, b"</s:Code>", b'<c:Name xml:lang="de">Beispiel</c:Name></s:Code>')
    for key, value in [
        (b"LAST_UPDATE", b"2026-10-01T00:00:00Z"),
        (b"UNIT_MULT", b"0"),
        (b"OBS_STATUS", b"P"),
        (b"X_UNKNOWN", b"preserve-me"),
    ]:
        leaf = b'<g:Value id="' + key + b'" value="' + value + b'"'
        c = changed(c, -1, leaf + b"/>", leaf + b"> \n </g:Value> \n ")
    p = sdmx.build_plan(
        c,
        identity="plain-leaves",
        finished_at=AT + timedelta(seconds=8),
        frozen_at=AT + timedelta(seconds=9),
    )
    sdmx.validate_plan(p)
    ledger = InMemoryLedger()
    sdmx.publish_offline(CanonicalKernel(ledger, lambda: AT + timedelta(seconds=10)), p)
    restarted = InMemoryLedger.from_snapshot(ledger.snapshot())
    before = restarted.snapshot()
    for version_identity in (None, p.version.identity):
        view = read(restarted, provider, version_identity=version_identity)
        projection = json.loads(view.content)["projection"]
        row = projection["observations"][0]
        assert row["raw_value"] == raw_value
        assert row["value"] == ("123.4500" if raw_value == "123.4500" else None)
        assert row["unavailable"] == (raw_value != "123.4500")
        assert row["attributes"] == {"OBS_STATUS": "P", "X_UNKNOWN": "preserve-me"}
        assert projection["series_attributes"] == {
            "UNIT_MULT": "0",
            "UNIT": "USD" if provider == "ECB" else "I25",
        }
        assert projection["dataset_attributes"]["LAST_UPDATE"] == "2026-10-01T00:00:00Z"
        assert (
            len(view.acquisitions)
            == len(json.loads(view.content)["raw"])
            == len(c.entities)
        )
        assert sdmx.candidate_from_content(view.content) == c
        assert not view.definition.source_qualified
        assert view.version.frozen_at == AT + timedelta(seconds=9)
        assert view.publication.usable_at == AT + timedelta(seconds=10)
    assert restarted.snapshot() == before
    assert_fresh_process_read(restarted, provider, p, rejected=False)


R4_SERIES_KEY_SHAPES = [
    "value-child",
    "value-text",
    "value-tail",
    "value-attribute",
    "value-namespaced-attribute",
    "container-text",
    "container-tail",
    "container-attribute",
    "container-namespaced-attribute",
]


def malformed_series_key(provider, shape):
    original = candidate(provider)
    dim, code, conflict = (
        (b"CURRENCY", b"USD", b"EUR")
        if provider == "ECB"
        else (b"unit", b"I25", b"I15")
    )
    leaf = b'<g:Value id="' + dim + b'" value="' + code + b'"'
    old, new = {
        "value-child": (
            leaf + b"/>",
            leaf + b'><g:Value id="' + dim + b'" value="' + conflict + b'"/></g:Value>',
        ),
        "value-text": (leaf + b"/>", leaf + b">" + conflict + b"</g:Value>"),
        "value-tail": (leaf + b"/>", leaf + b"/>" + conflict),
        "value-attribute": (leaf + b"/>", leaf + b' other="conflict"/>'),
        "value-namespaced-attribute": (leaf + b"/>", leaf + b' g:value="OTHER"/>'),
        "container-text": (b"<g:SeriesKey>", b"<g:SeriesKey>" + conflict),
        "container-tail": (b"</g:SeriesKey>", b"</g:SeriesKey>" + conflict),
        "container-attribute": (b"<g:SeriesKey>", b'<g:SeriesKey other="conflict">'),
        "container-namespaced-attribute": (
            b"<g:SeriesKey>",
            b'<g:SeriesKey g:value="OTHER">',
        ),
    }[shape]
    return changed(original, -1, old, new)


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("shape", R4_SERIES_KEY_SHAPES)
def test_r4_series_key_fail_closed_through_restarted_read(provider, shape, monkeypatch):
    bad = malformed_series_key(provider, shape)
    classification = "sdmx_unsupported_series_key"
    with pytest.raises(ContractError, match=classification):
        sdmx.build_plan(
            bad,
            identity="bad-series-key",
            finished_at=AT + timedelta(seconds=8),
            frozen_at=AT + timedelta(seconds=9),
        )

    # Model the R3-accepted graph with the original interpretation and consistent
    # raw hashes, carriers and lineage, rather than corrupting an accepted plan.
    projection = sdmx.project(candidate(provider))
    for row in projection["observations"]:
        row["raw_sha256"] = digest(bad.entities[-1].raw)
    with monkeypatch.context() as legacy:
        legacy.setattr(sdmx, "project", lambda _: projection)
        p = sdmx.build_plan(
            bad,
            identity="legacy-r3",
            finished_at=AT + timedelta(seconds=8),
            frozen_at=AT + timedelta(seconds=9),
        )
    assert sdmx.candidate_from_content(p.content) == bad
    with pytest.raises(ContractError, match=classification):
        sdmx.validate_plan(p)
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: AT + timedelta(seconds=10))
    before = ledger.snapshot()
    with pytest.raises(ContractError, match=classification):
        sdmx.publish_offline(kernel, p)
    assert ledger.snapshot() == before
    with monkeypatch.context() as legacy:
        legacy.setattr(sdmx, "validate_plan", lambda _: None)
        sdmx.publish_offline(kernel, p)
    restarted = InMemoryLedger.from_snapshot(ledger.snapshot())
    before = restarted.snapshot()
    for version_identity in (None, p.version.identity):
        with pytest.raises(SdmxReadRejected):
            read(restarted, provider, version_identity=version_identity)
    assert restarted.snapshot() == before
    assert_fresh_process_read(restarted, provider, p, rejected=True)


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize("raw_value", [None, "", ":", "123.4500"])
def test_r4_series_key_plain_leaves_roundtrip(provider, raw_value):
    c = candidate(provider)
    # Key order is immaterial; attribute order and whitespace are formatting.
    compact = b"".join(
        f'<g:Value id="{dim}" value="{code}"/>'.encode()
        for dim, code, _ in FIXTURES[provider][2]
    )
    formatted = b" \n " + b"".join(
        f'<g:Value value="{code}" id="{dim}"> \n </g:Value> \n '.encode()
        for dim, code, _ in reversed(FIXTURES[provider][2])
    )
    c = changed(c, -1, compact, formatted)
    c = changed(c, -1, b"</g:SeriesKey>", b"</g:SeriesKey> \n ")
    c = changed(
        c,
        -1,
        b'<g:ObsValue value="123.4500"/>',
        b"" if raw_value is None else f'<g:ObsValue value="{raw_value}"/>'.encode(),
    )
    p = sdmx.build_plan(
        c,
        identity="plain-series-key",
        finished_at=AT + timedelta(seconds=8),
        frozen_at=AT + timedelta(seconds=9),
    )
    sdmx.validate_plan(p)
    ledger = InMemoryLedger()
    sdmx.publish_offline(CanonicalKernel(ledger, lambda: AT + timedelta(seconds=10)), p)
    restarted = InMemoryLedger.from_snapshot(ledger.snapshot())
    before = restarted.snapshot()
    for version_identity in (None, p.version.identity):
        view = read(restarted, provider, version_identity=version_identity)
        doc = json.loads(view.content)
        projection = doc["projection"]
        row = projection["observations"][0]
        assert row["raw_value"] == raw_value
        assert row["value"] == ("123.4500" if raw_value == "123.4500" else None)
        assert row["unavailable"] == (raw_value != "123.4500")
        assert projection["series_key"] == ".".join(
            code for _, code, _ in FIXTURES[provider][2]
        )
        assert projection["metadata"] == json.loads(
            sdmx.encode(sdmx.project(candidate(provider))["metadata"])
        )
        assert len(view.acquisitions) == len(doc["raw"]) == len(c.entities)
        assert sdmx.candidate_from_content(view.content) == c
        assert not view.definition.source_qualified
    assert restarted.snapshot() == before
    assert_fresh_process_read(restarted, provider, p, rejected=False)


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
@pytest.mark.parametrize(
    ("index", "old", "new"),
    [
        (0, b'version="1.0"', b'version="latest"'),
        (0, b'id="SYNTHETIC_DSD"', b'id="OTHER_DSD"'),
        (1, b'position="1"', b'position="2"'),
        (1, b'position="1"', b'position="01"'),
        (1, b'id="TIME_PERIOD"', b'id="OTHER_TIME"'),
        (1, b'id="OBS_VALUE"', b'id="OTHER_VALUE"'),
        (1, b">Daily<", b">Annual<"),
        (1, b'class="Codelist"', b'class="DataStructure"'),
        (1, b"<s:Enumeration>", b'<s:Enumeration unsupported="true">'),
        (-1, b'id="SYNTHETIC_DSD"', b'id="OTHER_DSD"'),
        (-1, b'structureRef="synthetic"', b'structureRef="other"'),
        (-1, b'action="Replace"', b'action="Delete"'),
        (
            -1,
            b'dimensionAtObservation="TIME_PERIOD"',
            b'dimensionAtObservation="AllDimensions"',
        ),
        (-1, b'value="0"', b'value="3"'),
        (-1, b'<g:ObsValue value="123.4500"/>', b'<g:ObsValue value="0"/>'),
        (-1, b'<g:ObsValue value="123.4500"/>', b'<g:ObsValue value="NaN"/>'),
        (-1, b'<g:ObsValue value="123.4500"/>', b'<g:ObsValue value="1_000"/>'),
        (
            -1,
            b'<g:ObsValue value="123.4500"/>',
            b'<g:ObsValue value="1"/><g:ObsValue value="2"/>',
        ),
        (-1, b"<g:Series>", b"<g:Series><g:Group/>"),
    ],
)
def test_structure_and_data_mismatch(provider, index, old, new):
    c = candidate(provider)
    # Source-specific substitutions preserve independent fixture dimensions.
    if provider == "ESTAT":
        old = old.replace(b">Daily<", b">Monthly<").replace(b">D<", b">M<")
        new = new.replace(b">M<", b">Q<") if b"<c:Value>" in new else new
    with pytest.raises((ContractError, ValueError)):
        sdmx.project(changed(c, index, old, new))


def test_eurostat_candidate_meaning_and_order_fail_closed():
    c = candidate("ESTAT")
    for old, new in [
        (b"Index, 2025=100", b"Index, 2015=100"),
        (b">Germany<", b">France<"),
        (b'id="unit" position="2"', b'id="geo" position="2"'),
    ]:
        with pytest.raises(ContractError):
            sdmx.project(changed(c, 1, old, new))
    with pytest.raises(ContractError):
        sdmx.project(changed(c, -1, b'id="geo" value="DE"', b'id="geo" value="FR"'))


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
def test_ambiguity_and_reference_window(provider):
    c = candidate(provider)
    a, b, _ = FIXTURES[provider][3]
    for old, new in [
        (a.encode(), b.encode()),
        (a.encode(), b"2020-01-01" if provider == "ECB" else b"2020-01"),
        (b"<g:SeriesKey>", b"<g:SeriesKey/><g:SeriesKey>"),
        (
            b'<g:Value id="OBS_STATUS" value="P"/>',
            b'<g:Value id="OBS_STATUS" value="P"/><g:Value id="OBS_STATUS" value="R"/>',
        ),
        (b"</g:Series>", b"</g:Series><g:Series/>"),
    ]:
        with pytest.raises((ContractError, ValueError)):
            sdmx.project(changed(c, -1, old, new))


@pytest.mark.parametrize(
    "provider,index",
    [(p, i) for p in ("ECB", "ESTAT") for i in range(3 if p == "ECB" else 4)],
)
@pytest.mark.parametrize(
    "fault", ["truncated", "doctype", "entity", "encoding", "oversize"]
)
def test_xml_safety_and_bounds(provider, index, fault):
    c = candidate(provider)
    entities = list(c.entities)
    raw = entities[index].raw
    raw = {
        "truncated": raw[:-20],
        "doctype": b'<!DOCTYPE x [<!ENTITY z "boom">]>' + raw,
        "entity": b'<!ENTITY z "boom">' + raw,
        "encoding": raw.decode().encode("utf-16"),
        "oversize": b" " * 2_000_001,
    }[fault]
    entities[index] = replace(entities[index], raw=raw)
    with pytest.raises((ContractError, ValueError)):
        sdmx.project(replace(c, entities=tuple(entities)))


@pytest.mark.parametrize(
    "fault",
    [
        "provider",
        "role",
        "url",
        "attempts",
        "boolean",
        "naive",
        "time",
        "order",
        "missing",
        "window",
    ],
)
def test_candidate_identity_and_times(fault):
    c = candidate()
    if fault == "provider":
        c = replace(c, provider="mirror")
    elif fault == "window":
        c = replace(c, start="2026-01-01")
    elif fault == "missing":
        c = replace(c, entities=c.entities[:-1])
    else:
        kw = {
            "role": {"role": "observations"},
            "url": {"url": "https://mirror.invalid"},
            "attempts": {"attempts": 4},
            "boolean": {"attempts": True},
            "naive": {"obtained_at": AT.replace(tzinfo=None)},
            "time": {"obtained_at": AT - timedelta(days=1)},
            "order": {"requested_at": AT - timedelta(days=1)},
        }[fault]
        c = replace(c, entities=(*c.entities[:-1], replace(c.entities[-1], **kw)))
    with pytest.raises((ContractError, ValueError)):
        sdmx.project(c)


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
def test_metadata_ambiguity_and_extra_reference_targets(provider):
    c = candidate(provider)
    for index, old, new in [
        (0, b"<s:Dataflows>", b"<s:Dataflows><s:Dataflow/>"),
        (1, b"<s:Codelists>", b"<s:Codelists><s:Codelist/>"),
        (0, b"</s:Structure>", EXTRA_ATTACHMENT + b"</s:Structure>"),
        (1, b"</s:Enumeration>", EXTRA_ATTACHMENT + b"</s:Enumeration>"),
    ]:
        with pytest.raises((ContractError, ValueError)):
            sdmx.project(changed(c, index, old, new))


def test_bounded_raws_with_large_header_remain_replayable():
    c = candidate()
    entities = []
    for index, entity in enumerate(c.entities):
        raw = entity.raw
        padding = b" " * (1_950_000 - len(raw))
        if entity.role == "data":
            raw = raw.replace(
                b"</m:Header>", b"<m:Source>" + padding + b"</m:Source></m:Header>"
            )
        else:
            raw += padding
        entities.append(replace(entity, raw=raw))
    p = sdmx.build_plan(
        replace(c, entities=tuple(entities)),
        identity="large",
        finished_at=AT + timedelta(seconds=8),
        frozen_at=AT + timedelta(seconds=9),
    )
    assert len(p.content) > 9_000_000
    sdmx.validate_plan(p)


@pytest.mark.parametrize("provider", ["ECB", "ESTAT"])
def test_version_publication_roundtrip_exact_and_asof(provider):
    p, ledger, _, publication = published(provider)
    sdmx.validate_plan(p)
    assert (
        len(p.acquisitions) == len(p.raw_materials) == len(candidate(provider).entities)
    )
    assert (
        len(p.version.materials) == len(p.raw_materials) + 1
        and len(p.version.finite_scope.members) == 3
    )
    assert p.run.budget == 0
    assert not definition_for(provider).source_qualified
    assert not p.binding_candidate.silent_fallback
    assert [m.grade for m in p.raw_materials] == [Grade.ORIGINAL] * len(p.raw_materials)
    assert all(not m.durable for m in p.raw_materials)
    assert p.semantic_material.durable and p.semantic_material.replayable
    rebuilt = sdmx.candidate_from_content(p.content)
    assert rebuilt == candidate(provider)
    assert [m.digest for m in p.raw_materials] == [
        digest(r.raw) for r in rebuilt.entities
    ]
    independent = InMemoryLedger.from_snapshot(ledger.snapshot())
    before = copy.deepcopy(independent)
    exact = read(independent, provider, version_identity=p.version.identity)
    assert exact == read(independent, provider)
    assert exact.content == p.content and exact.publication == publication
    assert (
        independent == before
    )  # Read is observational, including all raw reconstruction.
    assert exact.version.frozen_at == AT + timedelta(seconds=9)
    assert exact.publication.decision_at == AT + timedelta(seconds=10)
    assert exact.publication.usable_at == exact.publication.decision_at
    with pytest.raises(ReadContractError) as err:
        read(independent, provider, at=AT + timedelta(seconds=9))
    assert err.value.classification == ReadFailure.AS_OF_NO_VERSION


@pytest.mark.parametrize(
    "provider,index",
    [(p, i) for p in ("ECB", "ESTAT") for i in range(3 if p == "ECB" else 4)],
)
@pytest.mark.parametrize(
    "axis", ["digest", "bytes", "url", "time", "projection", "parser"]
)
def test_plan_carrier_and_projection_corruption(provider, index, axis):
    p = plan(provider)
    doc = json.loads(p.content)
    if axis == "digest":
        doc["raw"][index]["sha256"] = "0" * 64
    elif axis == "bytes":
        doc["raw"][index]["base64"] = base64.b64encode(b"wrong").decode()
    elif axis == "url":
        doc["raw"][index]["url"] += "&alternate=true"
    elif axis == "time":
        doc["raw"][index]["obtained_at"] = AT.isoformat()
    elif axis == "projection":
        doc["projection"]["observations"][0]["value"] = "999"
    else:
        doc["parser"] = "unversioned"
    with pytest.raises((ContractError, ValueError)):
        sdmx.validate_plan(replace(p, content=sdmx.encode(doc)))


@pytest.mark.parametrize(
    "record_type",
    ["source", "binding_candidate", "run", "acquisition", "raw", "semantic", "version"],
)
def test_local_read_rejects_graph_tamper(record_type):
    p, ledger, _, _ = published()
    record, changes = {
        "source": (p.source, {"product": "mirror"}),
        "binding_candidate": (p.binding_candidate, {"limitations": ()}),
        "run": (p.run, {"budget": 99}),
        "acquisition": (p.acquisitions[1], {"provider_data_attempts": 3}),
        "raw": (p.raw_materials[2], {"controlled_ref": "wrong"}),
        "semantic": (p.semantic_material, {"named_class": "wrong"}),
        "version": (p.version, {"completeness": "assumed"}),
    }[record_type]
    ledger._records[
        (record.key.kind.value, record.key.identity, record.key.revision)
    ] = codec.dumps(replace(record, **changes))
    with pytest.raises((SdmxReadRejected, ReadContractError)):
        read(ledger)


def test_read_rejects_rehashed_projection():
    p, ledger, _, _ = published()
    doc = json.loads(p.content)
    doc["projection"]["observations"][0]["value"] = "999"
    content = sdmx.encode(doc)
    for record in [
        replace(p.version, content_digest=digest(content)),
        replace(p.semantic_material, digest=digest(content)),
    ]:
        ledger._records[
            (record.key.kind.value, record.key.identity, record.key.revision)
        ] = codec.dumps(record)
    ledger._contents[p.version.identity] = (digest(content), content)
    with pytest.raises((SdmxReadRejected, ReadContractError)):
        read(ledger)


def test_asof_latest_acquisition_not_publication_and_ambiguity():
    p, ledger, kernel, _ = published()
    p2 = plan(identity="newer", at=AT + timedelta(seconds=1))
    kernel.clock = lambda: AT + timedelta(seconds=20)
    sdmx.publish_offline(kernel, p2)
    p3 = plan(identity="older_late_publication", at=AT - timedelta(days=1))
    kernel.clock = lambda: AT + timedelta(seconds=30)
    sdmx.publish_offline(kernel, p3)
    assert read(ledger, at=AT + timedelta(seconds=15)).version == p.version
    assert read(ledger).version == p2.version
    assert read(ledger, version_identity=p.version.identity).version == p.version
    p4 = plan(identity="equal", at=AT + timedelta(seconds=1))
    sdmx.publish_offline(kernel, p4)
    with pytest.raises(ReadContractError) as err:
        read(ledger)
    assert err.value.classification == ReadFailure.AS_OF_AMBIGUOUS


def test_source_families_and_windows_stay_distinct():
    ecb, ledger, kernel, _ = published()
    estat = plan("ESTAT", "estat")
    sdmx.publish_offline(kernel, estat)
    assert ecb.version.definition != estat.version.definition
    assert read(ledger).version == ecb.version
    assert read(ledger, "ESTAT").version == estat.version
    with pytest.raises(ReadContractError):
        read(ledger, "ESTAT", version_identity=ecb.version.identity)
    with pytest.raises(ContractError):
        plan(identity="shorter", end="2026-09-29")
    shorter = changed(candidate(end="2026-09-29"), -1, b"2026-09-30", b"2026-09-29")
    other = sdmx.build_plan(
        shorter,
        identity="shorter",
        finished_at=ecb.run.finished_at,
        frozen_at=ecb.version.frozen_at,
    )
    sdmx.publish_offline(kernel, other)
    assert read(ledger).version == ecb.version
    assert (
        read_local_sdmx(
            ledger,
            provider="ECB",
            start=START,
            end="2026-09-29",
            as_of=AT + timedelta(seconds=100),
        ).version
        == other.version
    )


def test_offline_handoff_rejects_external_adapter_and_early_publication():
    p = plan()

    class ExternalLedger:
        def __getattr__(self, name):
            pytest.fail("must reject external ledger before IO")

    with pytest.raises(ContractError, match="offline_ledger"):
        sdmx.publish_offline(CanonicalKernel(ExternalLedger(), lambda: AT), p)
    ledger = InMemoryLedger()
    with pytest.raises(ContractError, match="publication_before_freeze"):
        sdmx.publish_offline(CanonicalKernel(ledger, lambda: AT), p)
    assert not ledger._records


def test_withdraw_restore_and_usable_time_boundary():
    p, ledger, kernel, publication = published()
    kernel.clock = lambda: AT + timedelta(seconds=20)
    withdrawal = kernel.publish(
        replace(
            p.publication_request,
            identity="withdraw",
            occurrence="withdraw",
            decision=Decision.WITHDRAW,
            predecessor=publication.key,
        )
    )
    with pytest.raises(ReadContractError):
        read(ledger)
    kernel.clock = lambda: AT + timedelta(seconds=30)
    kernel.publish(
        replace(
            p.publication_request,
            identity="restore",
            occurrence="restore",
            decision=Decision.RESTORE,
            predecessor=withdrawal.key,
            usable_at=AT + timedelta(seconds=40),
        )
    )
    with pytest.raises(ReadContractError):
        read(ledger, at=AT + timedelta(seconds=35))
    assert read(ledger, at=AT + timedelta(seconds=40)).version == p.version
    assert read(ledger, at=AT + timedelta(seconds=15)).version == p.version
