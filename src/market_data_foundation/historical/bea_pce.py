"""Bounded BEA NIPA PCE retained candidate. No network or credential loading.

The bundle is a source projection, never provider-original bytes. Mapping is a
candidate constrained by returned row identities and notes, pending real proof.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    AcquisitionOccurrence,
    ContractError,
    DataRun,
    DatasetDefinition,
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

ENDPOINT = "https://apps.bea.gov/api/data/"
REFERENCE = "2026-07"
COMPARISON = "2025-07"
TABLES = ("T20807", "T20804")
SERIES_IDS = ("DPCERGM", "DPCCRGM", "DPCERG", "DPCCRG")
CONTRACT_REVISION = "t04-retained-candidate-r1"
PARSER_REVISION = "bea-pce-nipa-monthly-v2"
FORMULA_REVISION = "pce-index-ratio-12m-v1"
SANITIZER_REVISION = "bea-pce-bounded-results-v2"
BUNDLE_SCHEMA = "bea-pce-retained-results-v2"
RULE = "t04-bea-pce-projected-retained-candidate-r1"
SOURCE_IDENTITY = "mdf.source-candidate.bea.nipa.pce"
COMPLETENESS = "exact_headline_core_mom_and_12m_index_inputs_2026_07"
MAX_RESPONSE_BYTES = 2_000_000
# PCE-only normalized carrier: base64 plus repeated revision-label lineage and
# up to 3x UTF-8-to-JSON escaping. No other product's carrier limit changes.
MAX_NORMALIZED_BYTES = MAX_RESPONSE_BYTES * 16
MEASUREMENTS = ("headline_mom_sa", "core_mom_sa", "headline_yoy_sa", "core_yoy_sa")
GRADES = frozenset({Grade.PROJECTED, Grade.NORMALIZED})
_NUMBER = re.compile(r"-?\d{1,12}(?:\.\d{1,12})?")
_SENSITIVE = re.compile(r"userid|api.?key|credential|secret|token", re.IGNORECASE)

PCE_DEFINITION = DatasetDefinition(
    "mdf.macro.bea.pce.headline-core",
    "t04-r1",
    "Candidate BEA NIPA T20807/T20804 lines 1/25 monthly PCE inflation",
    "provider-returned MoM and product-derived SA YoY, with retained lineage",
    True,
    "table + line + SeriesCode + reference + retrieval-bound revision metadata",
    "percent; YoY Decimal(28), half-even six places from 2017=100 SA indexes",
    "reference, response production, acquisition, MDF known/usable are distinct",
    "LastRevised is metadata, not release time, vintage or atomic snapshot proof",
    COMPLETENESS,
    Scope(),
)

PCE_SOURCE = SourceOperationRef(
    SOURCE_IDENTITY,
    CONTRACT_REVISION,
    "market-data-foundation",
    "BEA NIPA API",
    "GetData:NIPA:T20807:T20804:M",
    PARSER_REVISION,
    "lines-1-25-reference-2026-07-comparison-2025-07",
    None,
    None,
    None,
    "read",
    "retained_projection",
    Scope(),
    "per-response completion; no inferred release timestamp",
    COMPLETENESS,
    "two bounded table responses; no pagination",
    "retrieved snapshot only; matching table labels do not prove atomicity",
    frozenset({Grade.PROJECTED}),
)
PCE_BINDING_CANDIDATE = SourceBindingRevision(
    "mdf.binding-candidate.bea.nipa.pce",
    CONTRACT_REVISION,
    PCE_SOURCE.key,
    (
        OutputMapping(
            "bea-pce-headline-core",
            PCE_DEFINITION.key,
            PCE_DEFINITION.revision,
            "reference-response-completion-mdf-known-usable-v1",
            Scope(),
            EvidenceRequirement(GRADES, durable=True, replayable=True),
        ),
    ),
    "exact_table_line_series_unit_notes_and_formula",
    (
        "candidate_only_not_active",
        "source_mapping_not_independently_verified",
        "exact_original_retained=false",
        "sanitized_projection_not_qualified_original_proof",
        "no_live_execution_edge",
        "no_provider_fallback",
        "no_release_vintage_or_atomic_snapshot_claim",
    ),
    False,
)


def encode(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def decode(raw: bytes, *, max_bytes: int = MAX_RESPONSE_BYTES) -> dict:
    def pairs(items):
        require(len(dict(items)) == len(items), "pce_duplicate_json_key")
        return dict(items)

    try:
        require(
            type(raw) is bytes and 0 < len(raw) <= max_bytes,
            "pce_retained_size_invalid",
        )
        value = json.loads(raw, object_pairs_hook=pairs)
        require(type(value) is dict, "pce_json_object_required")
        encode(value)
        return value
    except ValueError, TypeError, UnicodeError:
        raise ContractError("pce_json_invalid") from None


def reject_sensitive(value: object, credential: str | None = None) -> None:
    if type(value) is dict:
        require(
            all(type(k) is str and not _SENSITIVE.search(k) for k in value),
            "pce_credential_echo_rejected",
        )
        for key, item in value.items():
            reject_sensitive(key, credential)
            reject_sensitive(item, credential)
    elif type(value) is list:
        for item in value:
            reject_sensitive(item, credential)
    elif type(value) is str:
        require(
            not _SENSITIVE.search(value)
            and (credential is None or credential.casefold() not in value.casefold()),
            "pce_credential_echo_rejected",
        )


def request_parameters(table: str) -> dict:
    require(table in TABLES, "pce_table_not_admitted")
    return {
        "method": "GetData",
        "DatasetName": "NIPA",
        "TableName": table,
        "Frequency": "M",
        "Year": "2025,2026",
        "ResultFormat": "JSON",
    }


@dataclass(frozen=True)
class ResponseCapture:
    table: str
    response_bytes: bytes
    requested_at: datetime
    obtained_at: datetime


_DIMENSION_NAMES = frozenset(
    {
        "TableName",
        "SeriesCode",
        "LineNumber",
        "LineDescription",
        "TimePeriod",
        "CL_UNIT",
        "UNIT_MULT",
        "METRIC_NAME",
        "DataValue",
    }
)
_ROW_FIELDS = _DIMENSION_NAMES | {"NoteRef"}


def project_provider_results(table: str, results: dict) -> dict:
    """Validate the bounded NIPA response and retain only the PCE source slice.

    Dimensions and row NoteRef are provider provenance. LastRevised is extracted
    from the table note, never inferred as a release time. Other lines and notes
    receive structural checks only and are discarded, not semantically mapped.
    """
    require(table in TABLES, "pce_table_not_admitted")
    require(
        type(results) is dict
        and {"Statistic", "UTCProductionTime", "Dimensions", "Data", "Notes"}
        <= results.keys()
        and results["Statistic"] == "NIPA Table",
        "pce_results_shape_unqualified",
    )
    production = results["UTCProductionTime"]
    require(
        type(production) is str
        and re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
            r"(?:\.[0-9]{1,6})?Z?",
            production,
        )
        is not None,
        "pce_production_metadata_missing",
    )
    try:
        datetime.fromisoformat(production)
    except ValueError:
        raise ContractError("pce_production_metadata_missing") from None
    dimensions = results["Dimensions"]
    require(
        type(dimensions) is list
        and len(dimensions) == len(_DIMENSION_NAMES)
        and all(
            type(d) is dict
            and {"Name", "DataType", "IsValue", "Ordinal"} <= d.keys()
            and all(type(v) is str for v in d.values())
            for d in dimensions
        )
        and {d["Name"] for d in dimensions} == _DIMENSION_NAMES,
        "pce_dimensions_unqualified",
    )
    notes = results["Notes"]
    require(type(notes) is list and 1 <= len(notes) <= 128, "pce_notes_missing")
    require(
        all(
            type(n) is dict
            and {"NoteRef", "NoteText"} <= n.keys()
            and all(type(v) is str and len(v) <= 16384 for v in n.values())
            for n in notes
        ),
        "pce_notes_invalid",
    )
    table_notes = [n for n in notes if n["NoteRef"] == table]
    require(len(table_notes) == 1, "pce_notes_invalid")
    table_note = {k: table_notes[0][k] for k in ("NoteRef", "NoteText")}
    text = table_note["NoteText"].casefold()
    require(
        ("table 2.8.7." if table == "T20807" else "table 2.8.4.") in text
        and "monthly" in text
        and "price" in text
        and "personal consumption expenditures" in text
        and "not seasonally adjusted" not in text,
        "pce_adjustment_or_table_notes_unqualified",
    )
    if table == "T20807":
        # The real monthly percent-change note omits SA; the fixed series codes
        # carry the mapping, corroborated by the SA level table below.
        require(
            "preceding period" in text
            and "percent" in text
            and "annual rate" not in text,
            "pce_mom_basis_unqualified",
        )
    else:
        require(
            "seasonally adjusted" in text, "pce_adjustment_or_table_notes_unqualified"
        )
        require("2017=100" in text.replace(" ", ""), "pce_index_base_unqualified")
    revision = re.search(
        r" - LastRevised: ([A-Za-z]+ [0-9]{1,2}, [0-9]{4})$", table_note["NoteText"]
    )
    require(revision is not None, "pce_revision_metadata_unqualified")
    label = revision[1]
    try:
        datetime.strptime(label, "%B %d, %Y")
    except ValueError:
        raise ContractError("pce_revision_metadata_unqualified") from None
    rows = results["Data"]
    require(type(rows) is list and 1 <= len(rows) <= 2000, "pce_rows_missing")
    retained = []
    seen = set()
    for row in rows:
        require(
            type(row) is dict
            and _ROW_FIELDS <= row.keys()
            and all(type(v) is str for v in row.values()),
            "pce_row_invalid",
        )
        line, period = row["LineNumber"], row["TimePeriod"]
        if line not in {"1", "25"}:
            continue
        require(row["TableName"] == table, "pce_row_table_mismatch")
        require(
            re.fullmatch(r"202[56]M(?:0[1-9]|1[0-2])", period) is not None,
            "pce_row_identity_invalid",
        )
        require((line, period) not in seen, "pce_duplicate_row")
        seen.add((line, period))
        series = (
            ("DPCERGM" if line == "1" else "DPCCRGM")
            if table == "T20807"
            else ("DPCERG" if line == "1" else "DPCCRG")
        )
        descriptions = (
            {
                "personal consumption expenditures",
                "personal consumption expenditures (pce)",
            }
            if line == "1"
            else {
                "pce excluding food and energy",
                "personal consumption expenditures excluding food and energy",
            }
        )
        require(
            row["SeriesCode"] == series
            and row["LineDescription"].strip().casefold() in descriptions
            and row["METRIC_NAME"] == "Fisher Price Index"
            and row["CL_UNIT"] == ("Percent change" if table == "T20807" else "Level")
            and row["UNIT_MULT"] == "0"
            and table in row["NoteRef"].split(","),
            "pce_mapping_or_unit_unqualified",
        )
        _value(row, positive=table == "T20804")
        retained.append({k: row[k] for k in sorted(_ROW_FIELDS)})
    require(
        {(line, period) for line in ("1", "25") for period in ("2025M07", "2026M07")}
        <= seen,
        "pce_required_rows_missing",
    )
    return {
        "Statistic": results["Statistic"],
        "UTCProductionTime": production,
        "Dimensions": [
            {k: d[k] for k in ("Name", "DataType", "IsValue", "Ordinal")}
            for d in dimensions
        ],
        "Data": retained,
        "Notes": [table_note, {"NoteRef": "LastRevised", "NoteText": label}],
    }


def retain_sanitized_bundle(
    captures: tuple[ResponseCapture, ...], *, credential: str, provenance: str
) -> bytes:
    """Remove Request in memory; return transformed bytes with no original hash.

    No persistence occurs here. Callers must keep the input out of logs/Git and
    use ignored, restricted artifacts for the returned sanitized projection.
    """
    require(type(credential) is str and len(credential) >= 16, "pce_key_required")
    require(tuple(c.table for c in captures) == TABLES, "pce_two_tables_required")
    responses = []
    for capture in captures:
        payload = decode(capture.response_bytes)
        require(set(payload) == {"BEAAPI"}, "pce_envelope_invalid")
        envelope = payload["BEAAPI"]
        require(
            type(envelope) is dict and set(envelope) == {"Request", "Results"},
            "pce_envelope_invalid",
        )
        request = envelope["Request"]
        require(
            type(request) is dict
            and set(request) == {"RequestParam"}
            and type(request["RequestParam"]) is list,
            "pce_returned_request_unqualified",
        )
        returned = {}
        for parameter in request["RequestParam"]:
            require(
                type(parameter) is dict
                and set(parameter) == {"ParameterName", "ParameterValue"}
                and type(parameter["ParameterName"]) is str
                and type(parameter["ParameterValue"]) is str,
                "pce_returned_request_unqualified",
            )
            name = parameter["ParameterName"].casefold()
            require(name not in returned, "pce_returned_request_duplicate")
            returned[name] = parameter["ParameterValue"].casefold()
        expected_request = {
            k.casefold(): v.casefold()
            for k, v in request_parameters(capture.table).items()
        }
        expected_request["userid"] = credential.casefold()
        require(returned == expected_request, "pce_returned_request_mismatch")
        results = envelope["Results"]
        reject_sensitive(results, credential)
        responses.append(
            {
                "request": request_parameters(capture.table),
                "requested_at": capture.requested_at.isoformat(),
                "obtained_at": capture.obtained_at.isoformat(),
                "results": project_provider_results(capture.table, results),
            }
        )
    raw = encode(
        {
            "schema": BUNDLE_SCHEMA,
            "sanitizer_revision": SANITIZER_REVISION,
            "exact_original_retained": False,
            "provenance": provenance,
            "responses": responses,
        }
    )
    parse_bundle(raw)
    return raw


def _time(value: str) -> datetime:
    at = datetime.fromisoformat(value)
    instant(at)
    return at


def _value(row: dict, *, positive: bool) -> Decimal:
    text = row["DataValue"]
    require(type(text) is str and _NUMBER.fullmatch(text), "pce_value_unavailable")
    value = Decimal(text)
    require(not positive or value > 0, "pce_nonpositive_index")
    return value


def _parse_bundle(raw: bytes) -> dict:
    bundle = decode(raw)
    require(
        set(bundle)
        == {
            "schema",
            "sanitizer_revision",
            "exact_original_retained",
            "provenance",
            "responses",
        }
        and bundle["schema"] == BUNDLE_SCHEMA
        and bundle["sanitizer_revision"] == SANITIZER_REVISION
        and bundle["exact_original_retained"] is False
        and bundle["provenance"] in {"synthetic", "retained_unqualified"},
        "pce_retention_class_invalid",
    )
    reject_sensitive(bundle)
    responses = bundle["responses"]
    require(type(responses) is list and len(responses) == 2, "pce_two_tables_required")
    selected = {}
    metadata = {}
    times = []
    starts = []
    for i, (table, response) in enumerate(zip(TABLES, responses, strict=True)):
        require(
            type(response) is dict
            and set(response) == {"request", "requested_at", "obtained_at", "results"}
            and response["request"] == request_parameters(table),
            "pce_request_mismatch",
        )
        start, end = _time(response["requested_at"]), _time(response["obtained_at"])
        require(start <= end, "pce_response_time_reversal")
        starts.append(start)
        times.append(end)
        results = response["results"]
        # Reapplying the same projection validates retained semantics and rejects
        # unprojected extras or a forged extracted LastRevised label on replay.
        require(
            results == project_provider_results(table, results),
            "pce_retained_projection_unqualified",
        )
        notes = results["Notes"]
        metadata[table] = {
            "notes": notes,
            "last_revised_label": notes[1]["NoteText"],
            "utc_production_time_metadata": results["UTCProductionTime"],
            "obtained_at": response["obtained_at"],
        }
        for j, row in enumerate(results["Data"]):
            if row["TimePeriod"] in {"2026M07", "2025M07"}:
                selected[(table, row["LineNumber"], row["TimePeriod"])] = (
                    row,
                    f"/responses/{i}/results/Data/{j}/DataValue",
                )
    require(times[0] <= starts[1], "pce_response_order_unqualified")
    require(
        metadata[TABLES[0]]["last_revised_label"]
        == metadata[TABLES[1]]["last_revised_label"],
        "pce_table_revision_label_mismatch",
    )
    measurements = {}
    for line, family in (("1", "headline"), ("25", "core")):
        mom, pointer = selected[("T20807", line, "2026M07")]
        common = {
            "reference": REFERENCE,
            "adjustment": "seasonally_adjusted",
            "unit": "percent",
        }
        measurements[family + "_mom_sa"] = {
            **common,
            "classification": "provider_returned_calculation",
            "value": mom["DataValue"],
            "series": mom["SeriesCode"],
            "table": "T20807",
            "line": line,
            "value_pointer": pointer,
        }
        operands = []
        for period, reference in (("2026M07", REFERENCE), ("2025M07", COMPARISON)):
            row, pointer = selected[("T20804", line, period)]
            operands.append(
                {
                    "reference": reference,
                    "value": row["DataValue"],
                    "series": row["SeriesCode"],
                    "table": "T20804",
                    "line": line,
                    "value_pointer": pointer,
                    "retained_sha256": digest(raw),
                    "input_revision_label": metadata["T20804"]["last_revised_label"],
                    "input_version_identity": "sha256:" + digest(raw) + "#" + pointer,
                    "input_version_kind": (
                        "retained_content_address_not_canonical_version"
                    ),
                }
            )
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            yoy = (
                Decimal(operands[0]["value"]) / Decimal(operands[1]["value"]) - 1
            ) * 100
            value = format(yoy.quantize(Decimal("0.000001")), ".6f")
        measurements[family + "_yoy_sa"] = {
            **common,
            "classification": "product_derived",
            "value": value,
            "formula": "(current_index / index_12_months_earlier - 1) * 100",
            "formula_revision": FORMULA_REVISION,
            "decimal_precision": 28,
            "rounding": "ROUND_HALF_EVEN",
            "output_places": 6,
            "precision_claim": "representation_only_limited_source_index_precision",
            "inputs": operands,
        }
    return {
        "measurements": measurements,
        "table_metadata": metadata,
        "requested_at": min(starts).isoformat(),
        "obtained_at": max(times).isoformat(),
        "release_at": None,
        "provider_vintage": None,
        "retention": {
            "exact_original_retained": False,
            "sanitizer_revision": SANITIZER_REVISION,
            "class": "sanitized_results_bundle",
            "provenance": bundle["provenance"],
        },
    }


def parse_bundle(raw: bytes) -> dict:
    try:
        return _parse_bundle(raw)
    except ContractError:
        raise
    except KeyError, TypeError, ValueError, AttributeError, ArithmeticError:
        raise ContractError("pce_bundle_incomplete_or_unqualified") from None


@dataclass(frozen=True)
class PceRetainedEvidence:
    raw_bytes: bytes = field(repr=False)
    acquisition_identity: str
    controlled_external_receipt: str

    def __post_init__(self) -> None:
        require(
            bool(self.acquisition_identity and self.controlled_external_receipt),
            "pce_retained_identity_required",
        )
        parse_bundle(self.raw_bytes)

    @property
    def obtained_at(self) -> datetime:
        return _time(parse_bundle(self.raw_bytes)["obtained_at"])


@dataclass(frozen=True)
class PcePlan:
    source: SourceOperationRef
    binding_candidate: SourceBindingRevision
    run: DataRun
    acquisition: AcquisitionOccurrence
    raw_material: EvidenceMaterial
    semantic_material: EvidenceMaterial
    version: DatasetVersion
    publication_request: PublicationRequest
    content: bytes = field(repr=False)


def build_plan(
    evidence: PceRetainedEvidence,
    *,
    identity: str,
    finished_at: datetime,
    frozen_at: datetime,
) -> PcePlan:
    require(
        type(evidence) is PceRetainedEvidence and bool(identity),
        "pce_evidence_required",
    )
    projection = parse_bundle(evidence.raw_bytes)
    requested_at, obtained_at = (
        _time(projection["requested_at"]),
        _time(projection["obtained_at"]),
    )
    instant(finished_at)
    instant(frozen_at)
    require(obtained_at <= finished_at <= frozen_at, "pce_processing_time_reversal")
    parameters = (
        ("parser_revision", PARSER_REVISION),
        ("formula_revision", FORMULA_REVISION),
        ("retention", "sanitized_results_bundle"),
        ("reference", REFERENCE),
    )
    transform = Transform(PCE_DEFINITION.identity, CONTRACT_REVISION, parameters)
    run = DataRun(
        identity + ":run",
        RunKind.REPROCESS,
        Scope(),
        finished_at,
        finished_at,
        "success",
        binding=PCE_BINDING_CANDIDATE.key,
        transform=transform,
        trigger="explicit_retained_candidate",
        budget=0,
    )
    acquisition = AcquisitionOccurrence(
        evidence.acquisition_identity,
        run.key,
        "bea-nipa-pce:" + digest(encode([request_parameters(t) for t in TABLES])),
        PCE_SOURCE.key,
        PCE_BINDING_CANDIDATE.key,
        "bea-pce-headline-core",
        Scope(),
        parameters,
        requested_at,
        obtained_at,
        None,
        None,
        "retained_projection",
        COMPLETENESS,
        "retrieval_snapshot_not_release_or_vintage",
        "success",
        external_receipt=evidence.controlled_external_receipt,
        provider_data_attempts=None,
        pagination="two retained table responses",
        recovery_summary="local replay; no provider request",
    )
    content = encode(
        {
            "schema": RULE,
            "parser_revision": PARSER_REVISION,
            "data": projection,
            "raw": {
                "sha256": digest(evidence.raw_bytes),
                "size": len(evidence.raw_bytes),
                "base64": base64.b64encode(evidence.raw_bytes).decode(),
            },
        }
    )
    require(len(content) <= MAX_NORMALIZED_BYTES, "pce_normalized_size_invalid")
    raw_material = EvidenceMaterial(
        identity + ":raw",
        Grade.PROJECTED,
        digest(evidence.raw_bytes),
        Scope(),
        acquired_from=acquisition.key,
        controlled_ref=evidence.controlled_external_receipt,
        named_class="bea_sanitized_results_bundle_not_original",
    )
    semantic_material = EvidenceMaterial(
        identity + ":semantic",
        Grade.NORMALIZED,
        digest(content),
        Scope(),
        acquired_from=acquisition.key,
        produced_by=run.key,
        controlled_ref=f"mdf-content://{identity}:version",
        durable=True,
        replayable=True,
        named_class="bea_pce_normalized_projection_with_embedded_sanitized_bundle",
    )
    version = DatasetVersion(
        identity + ":version",
        PCE_DEFINITION.key,
        FiniteScope(tuple(f"{REFERENCE}:bea:pce:{m}" for m in sorted(MEASUREMENTS))),
        Scope(),
        frozen_at,
        run.key,
        digest(content),
        binding=PCE_BINDING_CANDIDATE.key,
        mapping="bea-pce-headline-core",
        acquisitions=(acquisition.key,),
        materials=(raw_material.key, semantic_material.key),
        transform=transform,
        completeness=COMPLETENESS,
    )
    publication = PublicationRequest(
        identity + ":publication",
        identity + ":occurrence",
        version.key,
        RULE,
        Decision.PUBLISH,
        Scope(),
        version.materials,
        GRADES,
        "candidate_projected_evidence_not_source_qualification",
        required_completeness=COMPLETENESS,
        claims_replayable=True,
    )
    return PcePlan(
        PCE_SOURCE,
        PCE_BINDING_CANDIDATE,
        run,
        acquisition,
        raw_material,
        semantic_material,
        version,
        publication,
        content,
    )


def validate_plan(plan: PcePlan, raw: bytes) -> None:
    require(type(plan) is PcePlan, "pce_plan_required")
    expected = build_plan(
        PceRetainedEvidence(
            raw, plan.acquisition.identity, plan.acquisition.external_receipt
        ),
        identity=plan.run.identity.removesuffix(":run"),
        finished_at=plan.run.finished_at,
        frozen_at=plan.version.frozen_at,
    )
    require(plan == expected, "pce_plan_lineage_mismatch")


def durable_plan(plan: PcePlan, locator: str) -> PcePlan:
    return replace(
        plan,
        raw_material=replace(
            plan.raw_material, controlled_ref=locator, durable=True, replayable=True
        ),
    )


def persist_before_publication(
    kernel: CanonicalKernel, plan: PcePlan
) -> DatasetVersion:
    require(
        plan.source == PCE_SOURCE and plan.binding_candidate == PCE_BINDING_CANDIDATE,
        "pce_source_binding_mismatch",
    )
    require(
        plan.raw_material.durable and plan.raw_material.replayable,
        "pce_raw_not_durable",
    )
    for record in (
        plan.source,
        PCE_DEFINITION,
        plan.binding_candidate,
        plan.run,
        plan.acquisition,
        plan.raw_material,
        plan.semantic_material,
    ):
        kernel.add(record)
    return kernel.persist_version(plan.version, plan.content)


def publish(kernel: CanonicalKernel, plan: PcePlan):
    return kernel.publish(plan.publication_request)


@dataclass(frozen=True)
class PceRetainedCandidate:
    evidence: PceRetainedEvidence
    plan: PcePlan
