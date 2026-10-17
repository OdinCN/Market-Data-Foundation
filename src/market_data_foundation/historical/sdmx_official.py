"""Bounded SDMX 2.1 GenericData replay, exclusively for the T08 two surfaces.

No transport is constructed. Unsupported structures/formats fail closed. Retained
metadata is evidence for an offline candidate, never current Source qualification.
"""

import base64
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    AcquisitionOccurrence,
    DataRun,
    DatasetVersion,
    Decision,
    EvidenceMaterial,
    EvidenceRequirement,
    FiniteScope,
    Grade,
    OutputMapping,
    PublicationRequest,
    RunKind,
    Scope,
    SourceBindingRevision,
    SourceOperationRef,
    Transform,
    digest,
    instant,
    require,
)
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical.sdmx_contract import (
    MAX_BUNDLE_BYTES,
    MAX_CONTENT_BYTES,
    MAX_ENTITY_BYTES,
    MAX_WINDOW_DAYS,
    PARSER,
    REVISION,
    Surface,
    definition_for,
    roles_for,
    surface_for,
)

NS = {
    name: f"http://www.sdmx.org/resources/sdmxml/schemas/v2_1/{name}"
    for name in ("message", "structure", "common", "data/generic")
}
NS["g"] = NS.pop("data/generic")
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def encode(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _one(node, path):
    # Cardinality applies at every parent, including empty duplicate containers.
    for step in path.split("/"):
        found = node.findall(step, NS)
        require(len(found) == 1, "sdmx_missing_or_ambiguous_component")
        node = found[0]
    return node


def _no_text(node):
    return not (node.text or "").strip() and not (node.tail or "").strip()


def _xml(raw: bytes, kind: str):
    require(
        type(raw) is bytes and 0 < len(raw) <= MAX_ENTITY_BYTES,
        "sdmx_entity_bound",
    )
    # Require UTF-8, rejecting UTF-16/32 bypasses of the DTD/entity screen.
    try:
        text = raw.decode("utf-8-sig")
        require("\x00" not in text, "sdmx_xml_encoding")
        require(
            "<!DOCTYPE" not in text.upper() and "<!ENTITY" not in text.upper(),
            "sdmx_unsafe_xml",
        )
        declaration = re.match(r"\s*<\?xml\b[^?]*\?>", text)
        if declaration:
            encoding = re.search(r"encoding\s*=\s*['\"]([^'\"]+)", declaration[0], re.I)
            require(
                encoding is None or encoding[1].lower() in {"utf-8", "utf8"},
                "sdmx_xml_encoding",
            )
        node = ET.fromstring(text)
    except ET.ParseError, UnicodeError:
        raise ValueError("sdmx_invalid_xml") from None
    require(node.tag == f"{{{NS['message']}}}{kind}", "sdmx_format_not_admitted")
    require(sum(1 for _ in node.iter()) <= 30_000, "sdmx_xml_node_bound")
    _real_message(node)
    return node


def _real_message(root):
    """One explicit non-test marker, shared by Source and all retained replay.

    Synthetic fixtures use the same admission rule; a false marker alone never
    proves Source qualification. Missing, conflicting or unsupported markers
    cannot establish that the message is real.
    """
    header = _one(root, "message:Header")
    test = _one(header, "message:Test")
    require(
        [n for n in root.iter() if n.tag.rsplit("}", 1)[-1] == "Header"] == [header]
        and [n for n in root.iter() if n.tag.rsplit("}", 1)[-1] == "Test"] == [test]
        and not header.attrib
        and _no_text(header)
        and not test.attrib
        and len(test) == 0
        and not (test.tail or "").strip()
        and (test.text or "").strip() in {"false", "0"},
        "sdmx_non_test_message_required",
    )


def _identity(node):
    return _identity_values(tuple(node.get(k) for k in ("agencyID", "id", "version")))


def _identity_values(result):
    require(
        all(
            type(v) is str and len(v) <= 128 and re.fullmatch(r"[A-Za-z0-9_@.-]+", v)
            for v in result
        ),
        "sdmx_identity_missing_or_unsafe",
    )
    require(result[2] not in {"latest", "*"}, "sdmx_unversioned_structure")
    return result


def _ref(node, expected_class):
    require(node.tag == "Ref", "sdmx_ref_namespace")
    package = {"DataStructure": "datastructure", "Codelist": "codelist"}[expected_class]
    require(
        set(node.attrib)
        in (
            {"agencyID", "id", "version", "class"},
            {"agencyID", "id", "version", "class", "package"},
        )
        and node.get("package", package) == package
        and len(node) == 0
        and _no_text(node),
        "sdmx_unsupported_reference_structure",
    )
    require(node.get("class") == expected_class, "sdmx_reference_class")
    return _identity(node)


def _reference(node, path, expected_class):
    # Only EnumerationFormat may accompany the dimension's Enumeration.
    # It is validated by the bounded metadata schema, never an alternate Ref.
    for step in path.split("/"):
        node = _one(node, step)
        siblings = (
            node.findall("structure:EnumerationFormat", NS)
            if step == "structure:LocalRepresentation"
            else []
        )
        require(
            not node.attrib
            and len(siblings) <= 1
            and len(node) == 1 + len(siblings)
            and _no_text(node),
            "sdmx_unsupported_reference_structure",
        )
    return _ref(node[0], expected_class)


def _name(node):
    names = node.findall("common:Name", NS)
    require(
        all(
            set(n.attrib) <= {XML_LANG} and len(n) == 0 and not (n.tail or "").strip()
            for n in names
        ),
        "sdmx_unsupported_code_name",
    )
    english = [n.text for n in names if n.get(XML_LANG) == "en"]
    require(len(english) == 1 and bool(english[0]), "sdmx_code_meaning_missing")
    return english[0]


def _normalized(text):
    return re.sub(r"\s+", "", text).casefold()


def validate_window(start: str, end: str):
    require(
        all(
            type(v) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)
            for v in (start, end)
        ),
        "sdmx_window_invalid",
    )
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    require(0 <= (last - first).days < MAX_WINDOW_DAYS, "sdmx_window_bound")
    return first, last


def _metadata(s: Surface, reads, *, with_components=False):
    flow_doc, dsd_doc = (_xml(read.raw, "Structure") for read in reads[:2])
    flow = _one(flow_doc, "message:Structures/structure:Dataflows/structure:Dataflow")
    flow_id = _identity(flow)
    require(flow_id[:2] == (s.agency, s.flow), "sdmx_flow_mismatch")
    dsd_ref = _reference(flow, "structure:Structure", "DataStructure")
    require(dsd_ref[0] == s.agency, "sdmx_dsd_agency_mismatch")
    dsd = _one(
        dsd_doc, "message:Structures/structure:DataStructures/structure:DataStructure"
    )
    require(_identity(dsd) == dsd_ref, "sdmx_dsd_reference_mismatch")
    components = _one(dsd, "structure:DataStructureComponents")
    dims = _one(components, "structure:DimensionList")
    dimensions = dims.findall("structure:Dimension", NS)
    require(
        len(dimensions) == len(s.dimensions)
        and tuple(d.get("id") for d in dimensions) == s.dimensions
        and tuple(d.get("position") for d in dimensions)
        == tuple(str(i) for i in range(1, len(dimensions) + 1)),
        "sdmx_dimension_order_mismatch",
    )
    time = _one(dims, "structure:TimeDimension")
    require(
        time.get("id") == "TIME_PERIOD"
        and time.get("position") == str(len(dimensions) + 1)
        and len(dims) == len(dimensions) + 1,
        "sdmx_time_dimension_mismatch",
    )
    measure = _one(components, "structure:MeasureList/structure:PrimaryMeasure")
    require(measure.get("id") == "OBS_VALUE", "sdmx_measure_mismatch")
    codelists = {}
    # references=none does not promise inline enumerations. When present, every
    # code/name still passes the reviewed strict leaf and cardinality checks.
    containers = dsd_doc.findall("message:Structures/structure:Codelists", NS)
    require(len(containers) <= 1, "sdmx_missing_or_ambiguous_component")
    if containers:
        codelists = _codelists(containers[0])
    mapping = []
    for dimension, code, meaning in zip(
        dimensions, s.key.split("."), s.meanings, strict=True
    ):
        ref = _reference(
            dimension,
            "structure:LocalRepresentation/structure:Enumeration",
            "Codelist",
        )
        name = codelists.get(ref, {}).get(code)
        if ref in codelists:
            require(
                name is not None and _normalized(name) == _normalized(meaning),
                "sdmx_code_or_unit_meaning_mismatch",
            )
        mapping.append(
            {
                "dimension": dimension.get("id"),
                "code": code,
                "meaning": name,
                "codelist": ref,
            }
        )
    xml_attributes = {"flow": dict(flow.attrib), "dsd": dict(dsd.attrib)}
    unit_ref = None
    if s.provider == "ESTAT":
        unit_ref = tuple(mapping[1]["codelist"])
        require(unit_ref[:2] == ("ESTAT", "UNIT"), "sdmx_unit_reference_mismatch")
        unit_doc = _xml(reads[2].raw, "Structure")
        container = _one(unit_doc, "message:Structures/structure:Codelists")
        require(len(container) == 1, "sdmx_unit_codelist_cardinality")
        unit_lists = _codelists(container)
        require(set(unit_lists) == {unit_ref}, "sdmx_unit_reference_mismatch")
        name = unit_lists[unit_ref].get("I25")
        require(
            name is not None and _normalized(name) == _normalized(s.meanings[1]),
            "sdmx_code_or_unit_meaning_mismatch",
        )
        if unit_ref in codelists:
            require(
                {k: _normalized(v) for k, v in codelists[unit_ref].items()}
                == {k: _normalized(v) for k, v in unit_lists[unit_ref].items()},
                "sdmx_unit_evidence_conflict",
            )
        mapping[1]["meaning"] = name
        xml_attributes["unit"] = dict(container[0].attrib)
    from .sdmx_schema import validate_structure, validate_value

    for document, role in ((flow_doc, "flow"), (dsd_doc, "dsd")):
        validate_structure(document, s, role)
    if s.provider == "ESTAT":
        validate_structure(unit_doc, s, "unit")
        codelists.update(unit_lists)
    for dimension, code in zip(dimensions, s.key.split("."), strict=True):
        validate_value(dimension, code, codelists)
    metadata = {
        "agency": s.agency,
        "dataflow": flow_id,
        "dataset": s.flow,
        "dsd": dsd_ref,
        "unit_codelist": unit_ref,
        "dimensions": mapping,
        "unit": s.unit,
        "xml_attributes": xml_attributes,
        "evidence": {
            read.role: {"raw_sha256": digest(read.raw), "raw_size": len(read.raw)}
            for read in reads
            if read.role != "data"
        },
    }
    if with_components:
        return (
            metadata,
            time,
            measure,
            {
                node.get("id"): node
                for node in components.findall(
                    "structure:AttributeList/structure:Attribute", NS
                )
            },
            codelists,
        )
    return metadata


def _codelists(container):
    require(not container.attrib and _no_text(container), "sdmx_codelists_structure")
    result = {}
    for item in container:
        require(
            item.tag == f"{{{NS['structure']}}}Codelist", "sdmx_codelists_structure"
        )
        key = _identity(item)
        require(key not in result, "sdmx_duplicate_codelist")
        codes = {}
        for code in item.findall("structure:Code", NS):
            identity = code.get("id")
            require(identity and identity not in codes, "sdmx_duplicate_code")
            codes[identity] = _name(code)
        result[key] = codes
    return result


def _attributes(node):
    containers = node.findall("g:Attributes", NS)
    require(len(containers) <= 1, "sdmx_duplicate_attributes")
    result = {}
    if containers:
        require(
            not containers[0].attrib and _no_text(containers[0]),
            "sdmx_attribute_ambiguous",
        )
        for value in containers[0]:
            key = value.get("id")
            require(
                value.tag == f"{{{NS['g']}}}Value"
                and key
                and key not in result
                and value.get("value") is not None
                and set(value.attrib) == {"id", "value"}
                and len(value) == 0
                and _no_text(value),
                "sdmx_attribute_ambiguous",
            )
            result[key] = value.get("value")
    return result


def _children(node, names):
    require(
        all(child.tag in {f"{{{NS['g']}}}{n}" for n in names} for child in node),
        "sdmx_unsupported_data_component",
    )


def _data(
    s: Surface, raw: bytes, metadata, start, end, time, measure, attributes, codelists
):
    from .sdmx_schema import validate_value

    def check_attributes(values):
        for key, value in values.items():
            # Undeclared attributes remain opaque provider metadata. A declared
            # local representation always constrains its actual lexical value.
            if key in attributes:
                validate_value(attributes[key], value, codelists)

    root = _xml(raw, "GenericData")
    header = _one(root, "message:Header")
    require(
        set(root.attrib)
        <= {"{http://www.w3.org/2001/XMLSchema-instance}schemaLocation"}
        and _no_text(root)
        and all(
            n.tag in {f"{{{NS['message']}}}Header", f"{{{NS['message']}}}DataSet"}
            for n in root
        ),
        "sdmx_unsupported_data_component",
    )
    names = {
        "ID",
        "Test",
        "Prepared",
        "Sender",
        "Receiver",
        "Structure",
        "DataSetID",
        "Extracted",
        "Source",
    }
    require(
        all(n.tag in {f"{{{NS['message']}}}{name}" for name in names} for n in header),
        "sdmx_unsupported_data_header",
    )
    for name in names - {"Structure"}:
        nodes = header.findall("message:" + name, NS)
        require(len(nodes) <= 1, "sdmx_missing_or_ambiguous_component")
        for node in nodes:
            if name in {"Sender", "Receiver"}:
                require(
                    set(node.attrib) == {"id"} and _no_text(node),
                    "sdmx_unsupported_data_header",
                )
                require(
                    all(
                        n.tag == f"{{{NS['common']}}}Name"
                        and set(n.attrib) <= {XML_LANG}
                        and len(n) == 0
                        and not (n.tail or "").strip()
                        for n in node
                    ),
                    "sdmx_unsupported_data_header",
                )
            else:
                require(
                    not node.attrib
                    and len(node) == 0
                    and not (node.tail or "").strip(),
                    "sdmx_unsupported_data_header",
                )
                if name == "DataSetID":
                    require(node.text == s.flow, "sdmx_data_flow_mismatch")
    structure = _one(header, "message:Structure")
    require(
        set(structure.attrib) == {"structureID", "dimensionAtObservation"}
        and structure.get("dimensionAtObservation") == "TIME_PERIOD",
        "sdmx_observation_dimension_mismatch",
    )
    require(
        len(structure) == 1 and _no_text(structure),
        "sdmx_unsupported_reference_structure",
    )
    usage = structure.findall("common:StructureUsage", NS)
    if usage:
        # Eurostat names the Dataflow, whose exact versioned DSD was retained.
        require(s.provider == "ESTAT" and len(usage) == 1, "sdmx_data_flow_mismatch")
        branch = usage[0]
        require(
            not branch.attrib and len(branch) == 1 and _no_text(branch),
            "sdmx_unsupported_reference_structure",
        )
        ref = branch[0]
        require(
            ref.tag == "Ref"
            and set(ref.attrib) == {"agencyID", "id", "version"}
            and len(ref) == 0
            and _no_text(ref),
            "sdmx_unsupported_reference_structure",
        )
        require(
            _identity(ref) == tuple(metadata["dataflow"]), "sdmx_data_flow_mismatch"
        )
    else:
        branch = _one(structure, "common:Structure")
        urns = branch.findall("URN")
        if urns:
            # One ECB header-only URN, exactly equal to the validated DSD.
            # No URN support is added to flow, enumeration or other Ref scopes.
            require(
                s.provider == "ECB"
                and not branch.attrib
                and len(branch) == 1
                and _no_text(branch)
                and len(urns) == 1,
                "sdmx_unsupported_reference_structure",
            )
            urn = urns[0]
            agency, identity, version = metadata["dsd"]
            require(
                not urn.attrib
                and len(urn) == 0
                and not (urn.tail or "").strip()
                and urn.text
                == (
                    "urn:sdmx:org.sdmx.infomodel.datastructure.DataStructure="
                    f"{agency}:{identity}({version})"
                ),
                "sdmx_data_dsd_mismatch",
            )
        else:
            require(
                _reference(structure, "common:Structure", "DataStructure")
                == tuple(metadata["dsd"]),
                "sdmx_data_dsd_mismatch",
            )
    dataset = _one(root, "message:DataSet")
    require(
        structure.get("structureID")
        and dataset.get("structureRef") == structure.get("structureID")
        and dataset.get("action") in {None, "Replace"},
        "sdmx_dataset_reference_or_action",
    )
    _children(dataset, {"Attributes", "Series"})
    series = _one(dataset, "g:Series")
    _children(series, {"SeriesKey", "Attributes", "Obs"})
    require(
        set(dataset.attrib) <= {"structureRef", "action", "validFromDate"}
        and _no_text(dataset)
        and not series.attrib
        and _no_text(series),
        "sdmx_unsupported_data_component",
    )
    key = _one(series, "g:SeriesKey")
    require(
        not key.attrib
        and _no_text(key)
        and all(
            v.tag == f"{{{NS['g']}}}Value"
            and set(v.attrib) == {"id", "value"}
            and len(v) == 0
            and _no_text(v)
            for v in key
        ),
        "sdmx_unsupported_series_key",
    )
    pairs = [(v.get("id"), v.get("value")) for v in key]
    require(
        len(pairs) == len(s.dimensions)
        and len(dict(pairs)) == len(pairs)
        and dict(pairs) == dict(zip(s.dimensions, s.key.split("."), strict=True)),
        "sdmx_series_key_mismatch",
    )
    data_attrs, series_attrs = _attributes(dataset), _attributes(series)
    check_attributes(data_attrs)
    check_attributes(series_attrs)
    if s.provider == "ECB":
        require(
            series_attrs.get("UNIT") == "USD" and series_attrs.get("UNIT_MULT") == "0",
            "sdmx_series_unit_evidence_required",
        )
    rows, seen = [], set()
    first, last = validate_window(start, end)
    for index, obs in enumerate(series.findall("g:Obs", NS)):
        _children(obs, {"ObsDimension", "ObsValue", "Attributes"})
        dimension = _one(obs, "g:ObsDimension")
        # Validate structure before either numeric or missing interpretation.
        require(
            not obs.attrib
            and _no_text(obs)
            and set(dimension.attrib) == {"value"}
            and len(dimension) == 0
            and _no_text(dimension),
            "sdmx_unsupported_obs_dimension",
        )
        period = dimension.get("value")
        require(type(period) is str, "sdmx_reference_period_missing")
        validate_value(time, period, codelists)
        if s.provider == "ECB":
            require(re.fullmatch(r"\d{4}-\d{2}-\d{2}", period), "sdmx_daily_period")
            day = date.fromisoformat(period)
            in_window = first <= day <= last
        else:
            require(re.fullmatch(r"\d{4}-\d{2}", period), "sdmx_monthly_period")
            date.fromisoformat(period + "-01")
            in_window = start[:7] <= period <= end[:7]
        require(period not in seen and in_window, "sdmx_duplicate_or_outside_period")
        seen.add(period)
        values = obs.findall("g:ObsValue", NS)
        require(len(values) <= 1, "sdmx_ambiguous_value")
        if values:
            require(
                set(values[0].attrib) <= {"value"}
                and len(values[0]) == 0
                and _no_text(values[0]),
                "sdmx_unsupported_obs_value",
            )
        raw_value = values[0].get("value") if values else None
        missing = raw_value in {None, "", ":"}
        value = None
        if not missing:
            validate_value(measure, raw_value, codelists)
            require(
                re.fullmatch(
                    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?",
                    raw_value,
                ),
                "sdmx_numeric_invalid",
            )
            try:
                number = Decimal(raw_value)
            except InvalidOperation:
                raise ValueError("sdmx_numeric_invalid") from None
            require(number.is_finite() and number > 0, "sdmx_numeric_invalid")
            value = str(number)
        attrs = _attributes(obs)
        check_attributes(attrs)
        for level in (data_attrs, series_attrs, attrs):
            require(
                level.get("UNIT_MULT", "0") == "0"
                and level.get("UNIT", "USD" if s.provider == "ECB" else "I25")
                == ("USD" if s.provider == "ECB" else "I25"),
                "sdmx_observation_unit_mismatch",
            )
        rows.append(
            {
                "period": period,
                "raw_value": raw_value,
                "value": value,
                "unavailable": missing,
                "attributes": attrs,
                "raw_sha256": digest(raw),
                "pointer": (
                    f"/message:GenericData/message:DataSet/g:Series/g:Obs[{index + 1}]"
                ),
            }
        )
    require(rows, "sdmx_empty_series")
    return {
        "metadata": metadata,
        "series_key": s.key,
        "dataset_attributes": data_attrs,
        "series_attributes": series_attrs,
        "dataset_xml_attributes": dict(dataset.attrib),
        "header_xml": ET.tostring(header, encoding="unicode"),
        "observations": sorted(rows, key=lambda r: r["period"]),
        "provider_vintage": None,
        "provider_revision_state": "not_asserted",
        "freshness": "not_asserted",
        "release_currency": "not_asserted",
        "revision_policy": "latest_snapshot_only_no_overwritten_vintage_reconstruction",
    }


@dataclass(frozen=True)
class RetainedEntity:
    role: str
    url: str
    raw: bytes
    requested_at: datetime
    obtained_at: datetime
    # These are recorded evidence, not fresh transport calls during replay.
    # Historical receipts may not record transport counts; never invent them.
    attempts: int | None = 1


@dataclass(frozen=True)
class SdmxCandidate:
    provider: str
    start: str
    end: str
    entities: tuple[RetainedEntity, ...]


def _urls(s, metadata, start, end):
    agency, identity, version = metadata["dsd"]
    urls = [
        f"{s.base}/dataflow/{s.agency}/{s.flow}/latest?references=none",
        f"{s.base}/datastructure/{agency}/{identity}/{version}?references=none",
    ]
    if s.provider == "ESTAT":
        urls.append(
            f"{s.base}/codelist/{'/'.join(metadata['unit_codelist'])}?references=none"
        )
    urls.append(f"{s.base}/data/{s.flow}/{s.key}?startPeriod={start}&endPeriod={end}")
    return tuple(urls)


def project(candidate: SdmxCandidate):
    require(type(candidate) is SdmxCandidate, "sdmx_candidate_required")
    s = surface_for(candidate.provider)
    validate_window(candidate.start, candidate.end)
    reads = candidate.entities
    require(
        type(reads) is tuple
        and len(reads) == len(roles_for(s.provider))
        and all(type(r) is RetainedEntity for r in reads)
        and tuple(r.role for r in reads) == roles_for(s.provider),
        "sdmx_exact_provider_entities_required",
    )
    require(
        all(type(r.raw) is bytes and 0 < len(r.raw) <= MAX_ENTITY_BYTES for r in reads)
        and sum(len(r.raw) for r in reads) <= MAX_BUNDLE_BYTES,
        "sdmx_bundle_bound",
    )
    metadata, time, measure, attributes, codelists = _metadata(
        s, reads, with_components=True
    )
    urls = _urls(s, metadata, candidate.start, candidate.end)
    previous = None
    for read, url in zip(reads, urls, strict=True):
        instant(read.requested_at)
        instant(read.obtained_at)
        require(
            # Historical MDD retained metadata used latest. Keep its actual URL;
            # concrete response identities must still match all references.
            # Current Acquisition continues to request only pinned versions.
            read.url
            in (
                {url, url.rsplit("/", 1)[0] + "/latest?references=none"}
                if read.role in {"dsd", "unit"}
                else {url}
            )
            and (
                read.attempts is None
                or (type(read.attempts) is int and 1 <= read.attempts <= 3)
            ),
            "sdmx_request_identity_mismatch",
        )
        require(
            read.requested_at <= read.obtained_at
            and (previous is None or previous <= read.requested_at),
            "sdmx_acquisition_time_invalid",
        )
        previous = read.obtained_at
    return _data(
        s,
        reads[-1].raw,
        metadata,
        candidate.start,
        candidate.end,
        time,
        measure,
        attributes,
        codelists,
    )


def _contracts(provider):
    s, definition = surface_for(provider), definition_for(provider)
    source = SourceOperationRef(
        f"mdf.source-candidate.{provider.lower()}.sdmx",
        REVISION,
        "market-data-foundation",
        f"official {s.agency}",
        f"{s.flow}/{s.key}",
        PARSER,
        "exact-one-series",
        None,
        None,
        None,
        "read",
        "release_capture",
        Scope(),
        definition.temporal_semantics,
        definition.completeness_semantics,
        f"{len(roles_for(provider))} bounded retained entities; no pagination",
        "not_asserted",
        frozenset({Grade.ORIGINAL, Grade.PROJECTED}),
    )
    binding = SourceBindingRevision(
        f"mdf.binding-candidate.{provider.lower()}.sdmx",
        REVISION,
        source.key,
        (
            OutputMapping(
                "snapshot",
                definition.key,
                REVISION,
                REVISION,
                Scope(),
                EvidenceRequirement(
                    frozenset({Grade.ORIGINAL, Grade.PROJECTED}), True, True
                ),
            ),
        ),
        "exact-flow-dsd-unit-if-estat-data-r1",
        (
            "offline_candidate_only",
            "not_source_qualified",
            "no_provider_fallback",
            "current_official_structure_and_format_unverified",
        ),
        silent_fallback=False,
    )
    return source, definition, binding


@dataclass(frozen=True)
class SdmxPlan:
    source: SourceOperationRef
    binding_candidate: SourceBindingRevision
    run: DataRun
    acquisitions: tuple[AcquisitionOccurrence, ...]
    raw_materials: tuple[EvidenceMaterial, ...]
    semantic_material: EvidenceMaterial
    version: DatasetVersion
    publication_request: PublicationRequest
    content: bytes


def build_plan(
    candidate: SdmxCandidate,
    *,
    identity: str,
    finished_at: datetime,
    frozen_at: datetime,
) -> SdmxPlan:
    projection = project(candidate)
    source, definition, binding = _contracts(candidate.provider)
    instant(finished_at)
    instant(frozen_at)
    require(type(identity) is str and bool(identity), "sdmx_identity_required")
    require(
        candidate.entities[-1].obtained_at <= finished_at <= frozen_at,
        "sdmx_processing_time_invalid",
    )
    transform = Transform(definition.identity, REVISION, (("parser", PARSER),))
    run = DataRun(
        identity + ":run",
        RunKind.COLLECT,
        Scope(),
        candidate.entities[0].requested_at,
        finished_at,
        "success",
        binding=binding.key,
        transform=transform,
        trigger="explicit_retained_sdmx_candidate",
        budget=0,
    )
    acquisitions = tuple(
        AcquisitionOccurrence(
            f"{identity}:{read.role}:acquisition",
            run.key,
            digest(read.url.encode()),
            source.key,
            binding.key,
            "snapshot",
            Scope(),
            (("role", read.role), ("url", read.url)),
            read.requested_at,
            read.obtained_at,
            None,
            None,
            "release_capture",
            definition.completeness_semantics,
            "not_asserted",
            "success",
            external_receipt=f"retained-sha256:{digest(read.raw)}",
            provider_data_attempts=read.attempts,
            pagination="single response",
            recovery_summary="retained replay; zero new provider calls",
        )
        for read in candidate.entities
    )
    raws = tuple(
        EvidenceMaterial(
            f"{identity}:{read.role}:raw",
            Grade.ORIGINAL,
            digest(read.raw),
            Scope(),
            acquired_from=acquisition.key,
            controlled_ref=f"embedded-sha256:{digest(read.raw)}",
            named_class=f"{candidate.provider.lower()}_{read.role}_sdmx_raw",
        )
        for read, acquisition in zip(candidate.entities, acquisitions, strict=True)
    )
    content = encode(
        {
            "schema": REVISION,
            "parser": PARSER,
            "provider": candidate.provider,
            "start": candidate.start,
            "end": candidate.end,
            "projection": projection,
            "raw": [
                {
                    "role": r.role,
                    "url": r.url,
                    "base64": base64.b64encode(r.raw).decode(),
                    "sha256": digest(r.raw),
                    "size": len(r.raw),
                    "attempts": r.attempts,
                    "requested_at": r.requested_at.isoformat(),
                    "obtained_at": r.obtained_at.isoformat(),
                }
                for r in candidate.entities
            ],
        }
    )
    require(len(content) <= MAX_CONTENT_BYTES, "sdmx_content_bound")
    semantic = EvidenceMaterial(
        identity + ":semantic",
        Grade.PROJECTED,
        digest(content),
        Scope(),
        produced_by=run.key,
        controlled_ref=f"mdf-content://{identity}:version",
        durable=True,
        replayable=True,
        named_class="provider_cardinality_sdmx_projection_carrier",
    )
    members = tuple(
        f"{candidate.provider}|{projection['metadata']['dataset']}|"
        f"{projection['series_key']}|{r['period']}"
        for r in projection["observations"]
    )
    version = DatasetVersion(
        identity + ":version",
        definition.key,
        FiniteScope(members),
        Scope(),
        frozen_at,
        run.key,
        digest(content),
        binding=binding.key,
        mapping="snapshot",
        acquisitions=tuple(a.key for a in acquisitions),
        materials=tuple(m.key for m in (*raws, semantic)),
        transform=transform,
        completeness=definition.completeness_semantics,
    )
    publication = PublicationRequest(
        identity + ":publication",
        identity + ":occurrence",
        version.key,
        REVISION,
        Decision.PUBLISH,
        Scope(),
        version.materials,
        frozenset({Grade.ORIGINAL, Grade.PROJECTED}),
        "offline-sdmx-candidate",
        required_completeness=definition.completeness_semantics,
        claims_replayable=True,
    )
    return SdmxPlan(
        source,
        binding,
        run,
        acquisitions,
        raws,
        semantic,
        version,
        publication,
        content,
    )


def candidate_from_content(content: bytes) -> SdmxCandidate:
    require(
        type(content) is bytes and len(content) <= MAX_CONTENT_BYTES,
        "sdmx_content_bound",
    )
    doc = json.loads(content)
    require(
        len(doc["raw"]) == len(roles_for(doc["provider"])),
        "sdmx_provider_carriers_required",
    )
    entities = []
    for item in doc["raw"]:
        raw = base64.b64decode(item["base64"], validate=True)
        require(
            type(item["size"]) is int
            and len(raw) == item["size"]
            and digest(raw) == item["sha256"],
            "sdmx_carrier_digest_mismatch",
        )
        _xml(raw, "GenericData" if item["role"] == "data" else "Structure")
        entities.append(
            RetainedEntity(
                item["role"],
                item["url"],
                raw,
                datetime.fromisoformat(item["requested_at"]),
                datetime.fromisoformat(item["obtained_at"]),
                item["attempts"],
            )
        )
    return SdmxCandidate(doc["provider"], doc["start"], doc["end"], tuple(entities))


def validate_plan(plan: SdmxPlan) -> None:
    require(type(plan) is SdmxPlan, "sdmx_plan_required")
    rebuilt = build_plan(
        candidate_from_content(plan.content),
        identity=plan.run.identity.removesuffix(":run"),
        finished_at=plan.run.finished_at,
        frozen_at=plan.version.frozen_at,
    )
    require(plan == rebuilt, "sdmx_plan_or_lineage_mismatch")


def publish_offline(kernel: CanonicalKernel, plan: SdmxPlan):
    """Synthetic reference handoff only; rejects PG/external storage adapters.

    The canonical content carrier independently contains every original entity.
    Raw records do not claim Garage durability. A future live materializer must
    add protected raw writes, authoritative handoff and unknown reconciliation.
    """
    require(type(kernel.ledger) is InMemoryLedger, "sdmx_offline_ledger_required")
    validate_plan(plan)
    at = kernel.clock()
    instant(at)
    require(at >= plan.version.frozen_at, "sdmx_publication_before_freeze")
    for record in (
        plan.source,
        definition_for(candidate_from_content(plan.content).provider),
        plan.binding_candidate,
        plan.run,
        *plan.acquisitions,
        *plan.raw_materials,
        plan.semantic_material,
    ):
        kernel.add(record)
    kernel.persist_version(plan.version, plan.content)
    return kernel.ledger.publish(plan.publication_request, lambda: at)
