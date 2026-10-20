"""Bounded direct BLS CPI admission; this module performs no provider I/O."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    AcquisitionOccurrence,
    ContractError,
    DataRun,
    DatasetVersion,
    Decision,
    EvidenceMaterial,
    EvidenceRequirement,
    FiniteScope,
    Grade,
    OutputMapping,
    Publication,
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
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical.cpi_retained_handoff import (
    ACQUISITION as PRIOR_MDD_ACQUISITION,
)
from market_data_foundation.historical.cpi_retained_handoff import (
    AUGUST_REFERENCE_SEMANTICS,
    MEASURES,
    SEPTEMBER_REFERENCE_SEMANTICS,
    CpiReferenceSemantics,
    project_provider_calculations,
)
from market_data_foundation.source_request_policy import valid_transport_attempts

ENDPOINT = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
SERIES_IDS = (
    "CUSR0000SA0",
    "CUUR0000SA0",
    "CUSR0000SA0L1E",
    "CUUR0000SA0L1E",
)
REFERENCE_PERIOD = "2026-08"
PARSER_REVISION = "mdf-bls-cpi-direct-v1"
RULE = "p4-macro-bls-cpi-direct-admission-r1"
COMPLETENESS = "exact_four_cpi_provider_calculations_for_2026_08"
SEPTEMBER_REFERENCE_PERIOD = "2026-09"
SEPTEMBER_PARSER_REVISION = "mdf-bls-cpi-release-reference-2026-09-v1"
SEPTEMBER_RULE = "p4-macro-bls-cpi-release-reference-2026-09-r1"
SEPTEMBER_COMPLETENESS = "exact_four_cpi_provider_calculations_for_2026_09"
MAX_RESPONSE_BYTES = 100_000

_DECIMAL = re.compile(r"-?\d+(?:\.\d+)?")
_SENSITIVE_KEY = re.compile(
    r"registration.?key|api.?key|credential|secret|token", re.IGNORECASE
)
_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_CATALOG = {
    "CUSR0000SA0": ("Seasonally Adjusted", "All items"),
    "CUUR0000SA0": ("Not Seasonally Adjusted", "All items"),
    "CUSR0000SA0L1E": (
        "Seasonally Adjusted",
        "All items less food and energy",
    ),
    "CUUR0000SA0L1E": (
        "Not Seasonally Adjusted",
        "All items less food and energy",
    ),
}


class ReferenceNotAvailable(ContractError):
    """An otherwise valid response does not contain the September target yet."""


def _admit_reference_semantics(value: object) -> CpiReferenceSemantics:
    require(
        type(value) is CpiReferenceSemantics
        and value in (AUGUST_REFERENCE_SEMANTICS, SEPTEMBER_REFERENCE_SEMANTICS),
        "bls_cpi_reference_semantics_not_admitted",
    )
    return value


def _encode(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _decode(raw: bytes) -> dict:
    def pairs(items):
        require(len(dict(items)) == len(items), "bls_cpi_duplicate_json_key")
        return dict(items)

    try:
        require(type(raw) is bytes, "bls_cpi_immutable_bytes_required")
        require(0 < len(raw) <= MAX_RESPONSE_BYTES, "bls_cpi_response_size_invalid")
        value = json.loads(raw, object_pairs_hook=pairs)
        require(type(value) is dict, "bls_cpi_response_object_required")
        _encode(value)
        _reject_credential_echo(value)
        return value
    except (UnicodeError, ValueError, TypeError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("bls_cpi_invalid_json") from None


def _reject_credential_echo(value: object) -> None:
    if type(value) is dict:
        require(
            all(
                type(key) is str and _SENSITIVE_KEY.search(key) is None for key in value
            ),
            "bls_cpi_credential_echo_forbidden",
        )
        for item in value.values():
            _reject_credential_echo(item)
    elif type(value) is list:
        for item in value:
            _reject_credential_echo(item)


@dataclass(frozen=True)
class BlsCpiRequest:
    endpoint: str = ENDPOINT
    method: str = "POST"
    series_ids: tuple[str, ...] = SERIES_IDS
    startyear: str = "2025"
    endyear: str = "2026"
    catalog: bool = True
    calculations: bool = True

    def persisted(self) -> dict:
        require(self.endpoint == ENDPOINT, "bls_cpi_endpoint_mismatch")
        require(self.method == "POST", "bls_cpi_method_mismatch")
        require(self.series_ids == SERIES_IDS, "bls_cpi_series_request_mismatch")
        require(
            self.startyear == "2025" and self.endyear == "2026",
            "bls_cpi_year_request_mismatch",
        )
        require(
            self.catalog is True and self.calculations is True,
            "bls_cpi_options_request_mismatch",
        )
        # Deliberately closed: there is no registrationkey field to serialize.
        return {
            "endpoint": self.endpoint,
            "method": self.method,
            "body": {
                "seriesid": list(self.series_ids),
                "startyear": self.startyear,
                "endyear": self.endyear,
                "catalog": self.catalog,
                "calculations": self.calculations,
            },
        }

    @property
    def identity(self) -> str:
        return "bls-cpi-v2:" + digest(_encode(self.persisted()))


def _catalog(series_id: str, value: object) -> None:
    seasonality, item = _CATALOG[series_id]
    title_item = (
        "All items" if item == "All items" else "All items less food and energy"
    )
    expected = {
        "series_title": (
            f"{title_item} in U.S. city average, all urban consumers, "
            f"{seasonality.lower()}"
        ),
        "series_id": series_id,
        "seasonality": seasonality,
        "survey_name": "Consumer Price Index for All Urban Consumers (CPI-U)",
        "survey_abbreviation": "CU",
        "measure_data_type": item,
        "area": "U.S. city average",
        "item": item,
    }
    require(value == expected, "bls_cpi_catalog_identity_mismatch")


def _footnotes(value: object) -> list[dict[str, str]]:
    require(type(value) is list, "bls_cpi_footnotes_invalid")
    result = []
    for footnote in value:
        require(type(footnote) is dict, "bls_cpi_footnotes_invalid")
        if not footnote:
            continue
        require(
            set(footnote) == {"code", "text"}
            and type(footnote["code"]) is str
            and bool(footnote["code"])
            and type(footnote["text"]) is str
            and bool(footnote["text"]),
            "bls_cpi_footnotes_invalid",
        )
        result.append({"code": footnote["code"], "text": footnote["text"]})
    return result


def _rows(
    series_id: str,
    value: object,
    reference: CpiReferenceSemantics,
) -> dict[str, dict]:
    require(type(value) is list and bool(value), "bls_cpi_series_data_invalid")
    rows: dict[str, dict] = {}
    latest: list[str] = []
    base = {"year", "period", "periodName", "value", "footnotes"}
    for row in value:
        require(type(row) is dict, "bls_cpi_row_invalid")
        require(
            set(row)
            in (base, base | {"calculations"}, base | {"latest", "calculations"}),
            "bls_cpi_row_fields_invalid",
        )
        year, period = row["year"], row["period"]
        require(year in {"2025", "2026"}, "bls_cpi_row_year_invalid")
        require(
            type(period) is str
            and re.fullmatch(r"M(?:0[1-9]|1[0-2])", period) is not None,
            "bls_cpi_row_period_invalid",
        )
        month = int(period[1:])
        require(
            row["periodName"] == _MONTH_NAMES[month - 1] and type(row["value"]) is str,
            "bls_cpi_row_period_invalid",
        )
        _footnotes(row["footnotes"])
        pointer = f"{year}-{month:02d}"
        require(pointer not in rows, "bls_cpi_duplicate_period")
        rows[pointer] = row
        if "latest" in row:
            require(row["latest"] == "true", "bls_cpi_latest_invalid")
            latest.append(pointer)
        if "calculations" in row:
            calculations = row["calculations"]
            require(
                type(calculations) is dict
                and set(calculations) == {"net_changes", "pct_changes"}
                and calculations["net_changes"] == {}
                and type(calculations["pct_changes"]) is dict,
                "bls_cpi_calculations_invalid",
            )
            for months, pct in calculations["pct_changes"].items():
                require(
                    months in {"1", "3", "6", "12"}
                    and type(pct) is str
                    and _DECIMAL.fullmatch(pct) is not None,
                    "bls_cpi_pct_change_invalid",
                )
    require(len(latest) <= 1, "bls_cpi_latest_invalid")
    if latest:
        require(latest[0] == max(rows), "bls_cpi_latest_not_latest_period")
    return rows


@dataclass(frozen=True)
class DirectCpiProjection:
    data: dict
    provider_messages: tuple[str, ...]
    response_time_ms: int
    raw_sha256: str
    raw_size: int


def parse_response(
    raw: bytes,
    *,
    reference: CpiReferenceSemantics = AUGUST_REFERENCE_SEMANTICS,
) -> DirectCpiProjection:
    """Parse the bounded response, taking percentage text only from pct_changes."""
    reference = _admit_reference_semantics(reference)
    payload = _decode(raw)
    require(
        set(payload) == {"status", "responseTime", "message", "Results"},
        "bls_cpi_response_fields_invalid",
    )
    require(payload["status"] == "REQUEST_SUCCEEDED", "bls_cpi_request_failed")
    require(
        type(payload["responseTime"]) is int and payload["responseTime"] >= 0,
        "bls_cpi_response_time_invalid",
    )
    messages = payload["message"]
    require(
        type(messages) is list and all(type(item) is str for item in messages),
        "bls_cpi_provider_messages_invalid",
    )
    results = payload["Results"]
    require(
        type(results) is dict and set(results) == {"series"},
        "bls_cpi_results_invalid",
    )
    series = results["series"]
    require(type(series) is list and len(series) == 4, "bls_cpi_series_not_exact_four")
    require(
        tuple(item.get("seriesID") for item in series if type(item) is dict)
        == SERIES_IDS,
        "bls_cpi_series_identity_mismatch",
    )
    by_series = {}
    for item in series:
        require(
            type(item) is dict and set(item) == {"seriesID", "catalog", "data"},
            "bls_cpi_series_fields_invalid",
        )
        series_id = item["seriesID"]
        _catalog(series_id, item["catalog"])
        by_series[series_id] = _rows(series_id, item["data"], reference)

    target_presence = tuple(
        reference.reference_period in rows for rows in by_series.values()
    )
    if reference == SEPTEMBER_REFERENCE_SEMANTICS:
        if not any(target_presence):
            raise ReferenceNotAvailable("bls_cpi_reference_not_available")
        require(all(target_presence), "bls_cpi_reference_partial")
    else:
        require(all(target_presence), "bls_cpi_source_pointer_missing")
    for rows in by_series.values():
        require(
            reference.mom_comparison_period in rows
            and reference.yoy_comparison_period in rows,
            (
                "bls_cpi_comparison_period_missing"
                if reference == SEPTEMBER_REFERENCE_SEMANTICS
                else "bls_cpi_source_pointer_missing"
            ),
        )

    calculations = {}
    for name, (series_id, _meaning, months) in MEASURES.items():
        rows = by_series[series_id]
        comparison = (
            reference.mom_comparison_period
            if months == 1
            else reference.yoy_comparison_period
        )
        require(comparison in rows, "bls_cpi_comparison_period_missing")
        target = rows[reference.reference_period]
        target_calculations = target.get("calculations")
        require(
            type(target_calculations) is dict
            and str(months) in target_calculations["pct_changes"],
            "bls_cpi_target_calculation_missing",
        )
        notes = _footnotes(target["footnotes"])
        calculations[name] = (
            target_calculations["pct_changes"][str(months)],
            any(note["code"] == "P" for note in notes),
            notes,
        )
    return DirectCpiProjection(
        data=project_provider_calculations(calculations, reference=reference),
        provider_messages=tuple(messages),
        response_time_ms=payload["responseTime"],
        raw_sha256=digest(raw),
        raw_size=len(raw),
    )


@dataclass(frozen=True)
class DirectCpiReferenceContract:
    semantics: CpiReferenceSemantics
    parser_revision: str
    rule: str
    completeness: str
    source_identity: str
    binding_identity: str
    contract_revision: str
    acquisition_mode: str
    temporal_contract: str
    compatibility_policy: str


AUGUST_REFERENCE_CONTRACT = DirectCpiReferenceContract(
    AUGUST_REFERENCE_SEMANTICS,
    PARSER_REVISION,
    RULE,
    COMPLETENESS,
    "mdf.source-candidate.bls-v2.cpi.direct",
    "mdf.binding-candidate.bls-v2.cpi.direct",
    "p4-direct-r1",
    "historical_query",
    "reference 2026-08; actual MDF obtained_at retained",
    "exact_bls_v2_cpi_direct_request_and_parser",
)
SEPTEMBER_REFERENCE_CONTRACT = DirectCpiReferenceContract(
    SEPTEMBER_REFERENCE_SEMANTICS,
    SEPTEMBER_PARSER_REVISION,
    SEPTEMBER_RULE,
    SEPTEMBER_COMPLETENESS,
    "mdf.source-candidate.bls-v2.cpi.release-2026-09",
    "mdf.binding-candidate.bls-v2.cpi.release-2026-09",
    "p4-release-reference-2026-09-r1",
    "release_capture",
    "exact reference 2026-09; actual MDF obtained_at retained",
    "exact_bls_v2_cpi_release_2026_09_request_and_parser",
)
_ADMITTED_REFERENCE_CONTRACTS = (
    AUGUST_REFERENCE_CONTRACT,
    SEPTEMBER_REFERENCE_CONTRACT,
)


def _reference_contract(value: object) -> DirectCpiReferenceContract:
    require(
        type(value) is DirectCpiReferenceContract
        and value in _ADMITTED_REFERENCE_CONTRACTS,
        "bls_cpi_reference_contract_not_admitted",
    )
    return value


def _source(contract: DirectCpiReferenceContract) -> SourceOperationRef:
    return SourceOperationRef(
        identity=contract.source_identity,
        contract_revision=contract.contract_revision,
        owner="market-data-foundation",
        product="BLS public data API v2",
        operation="timeseries.data:CPI",
        projection_revision=contract.parser_revision,
        variant="exact-four-series-2025-2026-catalog-calculations",
        build_revision=None,
        registry_revision=None,
        provider_app_revision=None,
        access_effect="read",
        acquisition_mode=contract.acquisition_mode,
        scope=Scope(),
        temporal_contract=contract.temporal_contract,
        completeness_contract=contract.completeness,
        pagination_contract="single bounded BLS v2 response; no pagination",
        freshness_contract="provider release time and vintage not declared",
        evidence_capability=frozenset({Grade.ORIGINAL, Grade.PROJECTED}),
    )


def _binding(
    contract: DirectCpiReferenceContract,
    source: SourceOperationRef,
) -> SourceBindingRevision:
    return SourceBindingRevision(
        identity=contract.binding_identity,
        revision=contract.contract_revision,
        source=source.key,
        mappings=(
            OutputMapping(
                identity="bls-cpi-headlines",
                definition=BLS_CPI_HEADLINES.key,
                semantic_revision="p3-r1",
                temporal_revision="reference-period-provider-calculation-v1",
                scope=Scope(),
                evidence=EvidenceRequirement(
                    allowed=frozenset({Grade.ORIGINAL, Grade.PROJECTED}),
                    durable=True,
                    replayable=True,
                ),
            ),
        ),
        compatibility_policy=contract.compatibility_policy,
        limitations=(
            "candidate_only_not_active",
            "provider_release_time_not_declared",
            "provider_vintage_not_declared",
            "no_provider_fallback",
        ),
        silent_fallback=False,
    )


BLS_CPI_DIRECT_SOURCE = _source(AUGUST_REFERENCE_CONTRACT)
BLS_CPI_DIRECT_BINDING_CANDIDATE = _binding(
    AUGUST_REFERENCE_CONTRACT,
    BLS_CPI_DIRECT_SOURCE,
)
BLS_CPI_SEPTEMBER_RELEASE_SOURCE_CANDIDATE = _source(SEPTEMBER_REFERENCE_CONTRACT)
BLS_CPI_SEPTEMBER_RELEASE_BINDING_CANDIDATE = _binding(
    SEPTEMBER_REFERENCE_CONTRACT,
    BLS_CPI_SEPTEMBER_RELEASE_SOURCE_CANDIDATE,
)


@dataclass(frozen=True)
class DirectCpiReplayPlan:
    run: DataRun
    raw_material: EvidenceMaterial
    semantic_material: EvidenceMaterial
    version: DatasetVersion
    publication_request: PublicationRequest
    content: bytes


def _content(
    raw: bytes,
    projection: DirectCpiProjection,
    request: BlsCpiRequest,
    contract: DirectCpiReferenceContract,
    mode: str,
    obtained_at: datetime | None,
) -> bytes:
    reference = contract.semantics
    source_pointers = {
        name: {
            "series_id": series_id,
            "reference_period": reference.reference_period,
            "comparison_period": (
                reference.mom_comparison_period
                if months == 1
                else reference.yoy_comparison_period
            ),
            "calculation": f"pct_changes/{months}",
        }
        for name, (series_id, _meaning, months) in MEASURES.items()
    }
    return _encode(
        {
            "schema": contract.rule,
            "mode": mode,
            "request": request.persisted(),
            "request_identity": request.identity,
            "provider": "BLS",
            "provider_messages": list(projection.provider_messages),
            "provider_response_time_ms": projection.response_time_ms,
            "provider_revision": {"state": "not_declared", "value": None},
            "provider_released_at": None,
            "provider_vintage": None,
            "obtained_at": obtained_at.isoformat() if obtained_at else None,
            "parser_revision": contract.parser_revision,
            "source_pointers": source_pointers,
            "data": projection.data,
            "raw": {
                "sha256": projection.raw_sha256,
                "size": projection.raw_size,
                "base64": base64.b64encode(raw).decode(),
            },
        }
    )


def build_offline_replay_plan(
    raw: bytes,
    *,
    identity: str,
    started_at: datetime,
    finished_at: datetime,
    frozen_at: datetime,
    request: BlsCpiRequest = BlsCpiRequest(),
    reference_contract: DirectCpiReferenceContract = AUGUST_REFERENCE_CONTRACT,
) -> DirectCpiReplayPlan:
    """Reprocess retained bytes with no binding and no acquisition occurrence."""
    for value in (started_at, finished_at, frozen_at):
        instant(value)
    require(started_at <= finished_at <= frozen_at, "bls_cpi_replay_time_reversal")
    require(bool(identity), "bls_cpi_identity_required")
    contract = _reference_contract(reference_contract)
    projection = parse_response(raw, reference=contract.semantics)
    transform = Transform(
        "mdf.p4.macro.bls.cpi.direct.offline-replay",
        "r1",
        (
            ("parser_revision", contract.parser_revision),
            ("request_identity", request.identity),
        ),
    )
    run = DataRun(
        identity + ":run",
        RunKind.REPROCESS,
        Scope(),
        started_at,
        finished_at,
        "success",
        transform=transform,
        trigger="offline_exact_retained_bls_raw_equivalence",
    )
    content = _content(
        raw,
        projection,
        request,
        contract,
        "offline_retained_replay",
        None,
    )
    raw_material = EvidenceMaterial(
        identity + ":raw",
        Grade.ORIGINAL,
        projection.raw_sha256,
        Scope(),
        produced_by=run.key,
        controlled_ref=f"mdf-cpi-direct-content://{identity}:version/original.bin",
        durable=True,
        named_class="exact_retained_bls_v2_raw_offline_replay",
    )
    semantic_bytes = _encode(projection.data)
    semantic_material = EvidenceMaterial(
        identity + ":semantic",
        Grade.PROJECTED,
        digest(semantic_bytes),
        Scope(),
        produced_by=run.key,
        controlled_ref=f"mdf-cpi-direct-content://{identity}:version/projection.json",
        durable=True,
        named_class="bls_provider_pct_changes_projection",
    )
    version = DatasetVersion(
        identity + ":version",
        BLS_CPI_HEADLINES.key,
        FiniteScope(
            tuple(
                f"{contract.semantics.reference_period}:bls:cpi:{name}"
                for name in sorted(MEASURES)
            )
        ),
        Scope(),
        frozen_at,
        run.key,
        digest(content),
        materials=(raw_material.key, semantic_material.key),
        transform=transform,
        completeness=contract.completeness,
    )
    publication_request = PublicationRequest(
        identity + ":publication",
        identity + ":occurrence",
        version.key,
        contract.rule,
        Decision.PUBLISH,
        Scope(),
        version.materials,
        frozenset({Grade.ORIGINAL, Grade.PROJECTED}),
        "offline-exact-raw-direct-parser-equivalence",
        required_completeness=contract.completeness,
    )
    return DirectCpiReplayPlan(
        run, raw_material, semantic_material, version, publication_request, content
    )


@dataclass(frozen=True)
class DirectCpiLiveEvidence:
    raw_bytes: bytes
    request: BlsCpiRequest
    request_identity: str
    requested_at: datetime
    obtained_at: datetime
    acquisition_identity: str
    provider_data_attempts: int
    controlled_external_receipt: str
    evidence_mode: str = "direct_execution"
    direct_execution: bool = True
    reference_contract: DirectCpiReferenceContract = AUGUST_REFERENCE_CONTRACT

    def __post_init__(self) -> None:
        require(
            self.evidence_mode == "direct_execution" and self.direct_execution is True,
            "bls_cpi_direct_execution_evidence_required",
        )
        require(
            type(self.request) is BlsCpiRequest,
            "bls_cpi_exact_request_required",
        )
        self.request.persisted()
        require(
            type(self.request_identity) is str
            and self.request_identity == self.request.identity,
            "bls_cpi_request_identity_mismatch",
        )
        instant(self.requested_at)
        instant(self.obtained_at)
        require(self.requested_at <= self.obtained_at, "bls_cpi_live_time_reversal")
        require(
            type(self.acquisition_identity) is str
            and bool(self.acquisition_identity.strip())
            and self.acquisition_identity != PRIOR_MDD_ACQUISITION,
            "bls_cpi_direct_acquisition_identity_not_fresh",
        )
        require(
            valid_transport_attempts(self.provider_data_attempts),
            "bls_cpi_provider_data_attempts_invalid",
        )
        require(
            type(self.controlled_external_receipt) is str
            and bool(self.controlled_external_receipt.strip()),
            "bls_cpi_live_receipt_required",
        )
        contract = _reference_contract(self.reference_contract)
        parse_response(self.raw_bytes, reference=contract.semantics)


@dataclass(frozen=True)
class DirectCpiLivePlan:
    source: SourceOperationRef
    binding_candidate: SourceBindingRevision
    run: DataRun
    acquisition: AcquisitionOccurrence
    raw_material: EvidenceMaterial
    semantic_material: EvidenceMaterial
    version: DatasetVersion
    publication_request: PublicationRequest
    content: bytes


@dataclass(frozen=True)
class ReleaseCausation:
    slot_identity: str
    execution_identity: str
    attempt: int
    reference: str
    release: datetime

    def __post_init__(self) -> None:
        require(bool(self.slot_identity), "bls_cpi_release_slot_identity_required")
        require(
            bool(self.execution_identity),
            "bls_cpi_release_execution_identity_required",
        )
        require(
            type(self.attempt) is int and self.attempt > 0,
            "bls_cpi_release_attempt_invalid",
        )
        require(bool(self.reference), "bls_cpi_release_reference_required")
        instant(self.release)

    @property
    def parameters(self) -> tuple[tuple[str, str], ...]:
        return (
            ("release_attempt", str(self.attempt)),
            ("release_execution_identity", self.execution_identity),
            ("release_instant", self.release.isoformat()),
            ("release_reference", self.reference),
            ("release_slot_identity", self.slot_identity),
        )


def build_future_live_plan(
    evidence: DirectCpiLiveEvidence,
    *,
    identity: str,
    finished_at: datetime,
    frozen_at: datetime,
    release_causation: ReleaseCausation | None = None,
) -> DirectCpiLivePlan:
    """Model a future direct response; callers must supply actual live evidence."""
    require(
        type(evidence) is DirectCpiLiveEvidence,
        "bls_cpi_direct_live_evidence_required",
    )
    instant(finished_at)
    instant(frozen_at)
    require(
        evidence.obtained_at <= finished_at <= frozen_at,
        "bls_cpi_live_processing_time_invalid",
    )
    require(bool(identity), "bls_cpi_identity_required")
    contract = _reference_contract(evidence.reference_contract)
    if contract == SEPTEMBER_REFERENCE_CONTRACT:
        require(
            type(release_causation) is ReleaseCausation,
            "bls_cpi_release_causation_required",
        )
        require(
            release_causation.reference == contract.semantics.reference_period,
            "bls_cpi_release_causation_reference_mismatch",
        )
    else:
        require(
            release_causation is None,
            "bls_cpi_release_causation_not_admitted",
        )
    causation_parameters = (
        release_causation.parameters if release_causation is not None else ()
    )
    source = _source(contract)
    binding = _binding(contract, source)
    projection = parse_response(evidence.raw_bytes, reference=contract.semantics)
    transform = Transform(
        "mdf.p4.macro.bls.cpi.direct.live",
        "r1",
        (
            ("parser_revision", contract.parser_revision),
            ("request_identity", evidence.request_identity),
        )
        + causation_parameters,
    )
    run = DataRun(
        identity + ":run",
        RunKind.COLLECT,
        Scope(),
        evidence.requested_at,
        finished_at,
        "success",
        binding=binding.key,
        transform=transform,
        trigger=(
            "prebound_september_2026_cpi_release_slot"
            if release_causation is not None
            else "future_explicitly_authorized_direct_bls_v2_request"
        ),
        budget=1,
    )
    acquisition = AcquisitionOccurrence(
        evidence.acquisition_identity,
        run.key,
        evidence.request_identity,
        source.key,
        binding.key,
        "bls-cpi-headlines",
        Scope(),
        tuple(
            (key, _encode(value).decode())
            for key, value in sorted(evidence.request.persisted().items())
        )
        + causation_parameters,
        evidence.requested_at,
        evidence.obtained_at,
        None,
        None,
        contract.acquisition_mode,
        contract.completeness,
        "provider_release_time_not_declared",
        "success",
        external_receipt=evidence.controlled_external_receipt,
        provider_data_attempts=evidence.provider_data_attempts,
        pagination="single bounded response",
        recovery_summary="none; no fallback",
    )
    content = _content(
        evidence.raw_bytes,
        projection,
        evidence.request,
        contract,
        "direct_live_provider_response",
        evidence.obtained_at,
    )
    raw_material = EvidenceMaterial(
        identity + ":raw",
        Grade.ORIGINAL,
        projection.raw_sha256,
        Scope(),
        acquired_from=acquisition.key,
        controlled_ref=(evidence.controlled_external_receipt + "/raw-response.bin"),
        durable=False,
        replayable=False,
        named_class="direct_bls_v2_raw_response",
    )
    semantic_material = EvidenceMaterial(
        identity + ":semantic",
        Grade.PROJECTED,
        digest(content),
        Scope(),
        acquired_from=acquisition.key,
        controlled_ref=f"mdf-content://{identity}:version",
        durable=True,
        replayable=True,
        named_class="canonical_bls_response_and_projection_carrier",
    )
    version = DatasetVersion(
        identity + ":version",
        BLS_CPI_HEADLINES.key,
        FiniteScope(
            tuple(
                f"{contract.semantics.reference_period}:bls:cpi:{name}"
                for name in sorted(MEASURES)
            )
        ),
        Scope(),
        frozen_at,
        run.key,
        digest(content),
        binding=binding.key,
        mapping="bls-cpi-headlines",
        acquisitions=(acquisition.key,),
        materials=(raw_material.key, semantic_material.key),
        transform=transform,
        completeness=contract.completeness,
    )
    publication_request = PublicationRequest(
        identity + ":publication",
        identity + ":occurrence",
        version.key,
        contract.rule,
        Decision.PUBLISH,
        Scope(),
        version.materials,
        frozenset({Grade.ORIGINAL, Grade.PROJECTED}),
        "future-authorized-direct-bls-cpi-admission",
        required_completeness=contract.completeness,
        claims_replayable=True,
    )
    return DirectCpiLivePlan(
        source,
        binding,
        run,
        acquisition,
        raw_material,
        semantic_material,
        version,
        publication_request,
        content,
    )


def persist_offline_replay(
    kernel: CanonicalKernel, plan: DirectCpiReplayPlan
) -> DatasetVersion:
    require(plan.run.kind == RunKind.REPROCESS, "bls_cpi_offline_collect_forbidden")
    require(plan.run.binding is None, "bls_cpi_offline_binding_forbidden")
    require(plan.version.binding is None, "bls_cpi_offline_binding_forbidden")
    require(plan.version.mapping is None, "bls_cpi_offline_binding_forbidden")
    require(plan.version.acquisitions == (), "bls_cpi_offline_acquisition_forbidden")
    require(
        plan.raw_material.acquired_from is None
        and plan.semantic_material.acquired_from is None,
        "bls_cpi_offline_acquisition_forbidden",
    )
    for record in (
        BLS_CPI_HEADLINES,
        plan.run,
        plan.raw_material,
        plan.semantic_material,
    ):
        kernel.add(record)
    return kernel.persist_version(plan.version, plan.content)


def publish_offline_replay(
    kernel: CanonicalKernel, plan: DirectCpiReplayPlan
) -> Publication:
    require(plan.version.acquisitions == (), "bls_cpi_offline_acquisition_forbidden")
    return kernel.publish(plan.publication_request)


def persist_live_plan_before_publication(
    kernel: CanonicalKernel, plan: DirectCpiLivePlan
) -> DatasetVersion:
    """Persist exact live lineage; the candidate binding remains inactive metadata."""
    require(plan.run.kind == RunKind.COLLECT, "bls_cpi_live_collect_required")
    from market_data_foundation.historical.cpi_history import source_binding

    history_source, history_binding = source_binding()
    require(
        plan.binding_candidate
        in (
            history_binding,
            BLS_CPI_DIRECT_BINDING_CANDIDATE,
            BLS_CPI_SEPTEMBER_RELEASE_BINDING_CANDIDATE,
        )
        and plan.source.key == plan.binding_candidate.source
        and (
            plan.binding_candidate != history_binding or plan.source == history_source
        ),
        "bls_cpi_live_binding_candidate_mismatch",
    )
    require(
        "candidate_only_not_active" in plan.binding_candidate.limitations,
        "bls_cpi_live_binding_activation_forbidden",
    )
    for record in (
        plan.source,
        BLS_CPI_HEADLINES,
        plan.binding_candidate,
        plan.run,
        plan.acquisition,
        plan.raw_material,
        plan.semantic_material,
    ):
        kernel.add(record)
    return kernel.persist_version(plan.version, plan.content)


def publish_live_plan(kernel: CanonicalKernel, plan: DirectCpiLivePlan) -> Publication:
    """Publish only through the canonical kernel after live-plan persistence."""
    return kernel.publish(plan.publication_request)
