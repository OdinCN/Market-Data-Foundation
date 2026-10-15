"""Bounded BLS PPI final-demand release contract; no provider IO."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime

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
from market_data_foundation.domain_contracts.macro import BLS_PPI_FINAL_DEMAND

ENDPOINT = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
SERIES_IDS = ("WPSFD4", "WPUFD4")
REFERENCE = "2026-09"
MOM_COMPARISON = "2026-08"
YOY_COMPARISON = "2025-09"
RELEASE = datetime(2026, 10, 15, 12, 30, tzinfo=UTC)
RELEASE_SLOT = "PPI/2026-09/2026-10-15T12:30:00Z"
CALENDAR_SOURCE = "https://www.bls.gov/schedule/2026/10_sched.htm"
PARSER_REVISION = "mdf-bls-ppi-final-demand-2026-09-v1"
SOURCE_IDENTITY = "mdf.source-candidate.bls-v2.ppi.final-demand-2026-09"
BINDING_IDENTITY = "mdf.binding-candidate.bls-v2.ppi.final-demand-2026-09"
CONTRACT_REVISION = "t01-release-2026-09-r1"
RULE = "t01-macro-bls-ppi-final-demand-r1"
COMPLETENESS = "exact_wpsfd4_wpufd4_target_and_comparisons_for_2026_09"
MAX_RESPONSE_BYTES = 100_000

_DECIMAL = re.compile(r"-?\d+(?:\.\d+)?")
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
_SENSITIVE = re.compile(
    r"registration.?key|api.?key|credential|secret|token", re.IGNORECASE
)
MEASUREMENTS = {
    "final_demand_index_sa": ("WPSFD4", "value", "index_point"),
    "final_demand_mom_sa": ("WPSFD4", "pct_changes/1", "percent"),
    "final_demand_index_nsa": ("WPUFD4", "value", "index_point"),
    "final_demand_yoy_nsa": ("WPUFD4", "pct_changes/12", "percent"),
}


def _encode(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _reject_sensitive(value: object) -> None:
    if type(value) is dict:
        require(
            all(type(key) is str and _SENSITIVE.search(key) is None for key in value),
            "bls_ppi_credential_echo_forbidden",
        )
        for item in value.values():
            _reject_sensitive(item)
    elif type(value) is list:
        for item in value:
            _reject_sensitive(item)


def _decode(raw: bytes) -> dict:
    def pairs(items):
        require(len(dict(items)) == len(items), "bls_ppi_duplicate_json_key")
        return dict(items)

    try:
        require(type(raw) is bytes, "bls_ppi_immutable_bytes_required")
        require(0 < len(raw) <= MAX_RESPONSE_BYTES, "bls_ppi_response_size_invalid")
        value = json.loads(raw, object_pairs_hook=pairs)
        require(type(value) is dict, "bls_ppi_response_object_required")
        _encode(value)
        _reject_sensitive(value)
        return value
    except UnicodeError, ValueError, TypeError:
        raise ContractError("bls_ppi_invalid_json") from None


@dataclass(frozen=True)
class BlsPpiRequest:
    endpoint: str = ENDPOINT
    method: str = "POST"
    series_ids: tuple[str, ...] = SERIES_IDS
    startyear: str = "2025"
    endyear: str = "2026"
    calculations: bool = True

    def persisted(self) -> dict:
        require(self.endpoint == ENDPOINT, "bls_ppi_endpoint_mismatch")
        require(self.method == "POST", "bls_ppi_method_mismatch")
        require(self.series_ids == SERIES_IDS, "bls_ppi_series_request_mismatch")
        require(
            self.startyear == "2025" and self.endyear == "2026",
            "bls_ppi_year_request_mismatch",
        )
        require(self.calculations is True, "bls_ppi_calculations_required")
        return {
            "endpoint": self.endpoint,
            "method": self.method,
            "body": {
                "seriesid": list(self.series_ids),
                "startyear": self.startyear,
                "endyear": self.endyear,
                "calculations": self.calculations,
            },
        }

    @property
    def identity(self) -> str:
        return "bls-ppi-v2:" + digest(_encode(self.persisted()))


def _footnotes(value: object) -> list[dict[str, str]]:
    require(type(value) is list, "bls_ppi_footnotes_invalid")
    result = []
    for item in value:
        require(type(item) is dict, "bls_ppi_footnotes_invalid")
        if not item:
            continue
        require(
            set(item) == {"code", "text"}
            and type(item["code"]) is str
            and bool(item["code"])
            and type(item["text"]) is str
            and bool(item["text"]),
            "bls_ppi_footnotes_invalid",
        )
        result.append({"code": item["code"], "text": item["text"]})
    return result


def _rows(value: object) -> dict[str, dict]:
    require(type(value) is list and bool(value), "bls_ppi_series_data_invalid")
    result: dict[str, dict] = {}
    latest: list[str] = []
    base = {"year", "period", "periodName", "value", "footnotes"}
    for row in value:
        require(type(row) is dict, "bls_ppi_row_invalid")
        require(
            set(row)
            in (base, base | {"calculations"}, base | {"latest", "calculations"}),
            "bls_ppi_row_fields_invalid",
        )
        require(row["year"] in {"2025", "2026"}, "bls_ppi_row_year_invalid")
        require(
            type(row["period"]) is str
            and re.fullmatch(r"M(?:0[1-9]|1[0-2])", row["period"]),
            "bls_ppi_row_period_invalid",
        )
        month = int(row["period"][1:])
        require(
            row["periodName"] == _MONTH_NAMES[month - 1]
            and type(row["value"]) is str
            and _DECIMAL.fullmatch(row["value"]),
            "bls_ppi_row_value_invalid",
        )
        _footnotes(row["footnotes"])
        pointer = f"{row['year']}-{month:02d}"
        require(pointer not in result, "bls_ppi_duplicate_period")
        result[pointer] = row
        if "latest" in row:
            require(row["latest"] == "true", "bls_ppi_latest_invalid")
            latest.append(pointer)
        calculations = row.get("calculations")
        if calculations is not None:
            require(
                type(calculations) is dict
                and set(calculations) == {"net_changes", "pct_changes"},
                "bls_ppi_calculations_invalid",
            )
            for group in calculations.values():
                require(type(group) is dict, "bls_ppi_calculations_invalid")
                for period, number in group.items():
                    require(
                        period in {"1", "3", "6", "12"}
                        and type(number) is str
                        and _DECIMAL.fullmatch(number),
                        "bls_ppi_calculations_invalid",
                    )
    require(len(latest) <= 1, "bls_ppi_latest_invalid")
    if latest:
        require(latest[0] == max(result), "bls_ppi_latest_not_latest_period")
    return result


@dataclass(frozen=True)
class PpiProjection:
    data: dict
    provider_messages: tuple[str, ...]
    response_time_ms: int
    raw_sha256: str
    raw_size: int


def _measurement(row: dict, pointer: str, unit: str) -> dict:
    if pointer == "value":
        value = row["value"]
    else:
        calculations = row.get("calculations")
        require(type(calculations) is dict, "bls_ppi_target_calculation_missing")
        _, months = pointer.split("/", 1)
        value = calculations["pct_changes"].get(months)
        require(
            type(value) is str and _DECIMAL.fullmatch(value),
            "bls_ppi_target_calculation_missing",
        )
    notes = _footnotes(row["footnotes"])
    return {
        "value": value,
        "unit": unit,
        "revision_state": "preliminary"
        if any(n["code"] == "P" for n in notes)
        else "not_declared_by_footnote",
        "footnotes": notes,
    }


def _parse_response(raw: bytes) -> PpiProjection:
    payload = _decode(raw)
    require(
        set(payload) == {"status", "responseTime", "message", "Results"},
        "bls_ppi_response_fields_invalid",
    )
    require(payload["status"] == "REQUEST_SUCCEEDED", "bls_ppi_request_failed")
    require(
        type(payload["responseTime"]) is int and payload["responseTime"] >= 0,
        "bls_ppi_response_time_invalid",
    )
    messages = payload["message"]
    require(
        type(messages) is list and all(type(item) is str for item in messages),
        "bls_ppi_provider_messages_invalid",
    )
    results = payload["Results"]
    require(
        type(results) is dict and set(results) == {"series"}, "bls_ppi_results_invalid"
    )
    series = results["series"]
    require(type(series) is list and len(series) == 2, "bls_ppi_series_not_exact_two")
    require(
        tuple(item.get("seriesID") for item in series if type(item) is dict)
        == SERIES_IDS,
        "bls_ppi_series_identity_mismatch",
    )
    by_series: dict[str, dict[str, dict]] = {}
    for item in series:
        require(
            type(item) is dict and set(item) == {"seriesID", "data"},
            "bls_ppi_series_fields_invalid",
        )
        by_series[item["seriesID"]] = _rows(item["data"])
    for rows in by_series.values():
        require(REFERENCE in rows, "bls_ppi_reference_not_available")
    require(MOM_COMPARISON in by_series["WPSFD4"], "bls_ppi_mom_comparison_missing")
    require(YOY_COMPARISON in by_series["WPUFD4"], "bls_ppi_yoy_comparison_missing")
    projected = {}
    for name, (series_id, pointer, unit) in MEASUREMENTS.items():
        projected[name] = {
            "series_id": series_id,
            "reference_period": REFERENCE,
            "comparison_period": (
                MOM_COMPARISON
                if pointer == "pct_changes/1"
                else YOY_COMPARISON
                if pointer == "pct_changes/12"
                else None
            ),
            "measurement": pointer,
            **_measurement(by_series[series_id][REFERENCE], pointer, unit),
        }
    return PpiProjection(
        {
            "reference_period": REFERENCE,
            "release_slot": RELEASE_SLOT,
            "revision_policy": (
                "initial_then_three_interim_months_then_final_at_month_four"
            ),
            "measurements": projected,
        },
        tuple(messages),
        payload["responseTime"],
        digest(raw),
        len(raw),
    )


def parse_response(raw: bytes) -> PpiProjection:
    """Parse one exact response and collapse Python shape failures to a contract."""
    try:
        return _parse_response(raw)
    except ContractError:
        raise
    except KeyError, IndexError, TypeError, ValueError:
        raise ContractError("bls_ppi_response_shape_invalid") from None


def _source() -> SourceOperationRef:
    return SourceOperationRef(
        identity=SOURCE_IDENTITY,
        contract_revision=CONTRACT_REVISION,
        owner="market-data-foundation",
        product="BLS public data API v2",
        operation="timeseries.data:PPI-final-demand",
        projection_revision=PARSER_REVISION,
        variant="exact-WPSFD4-WPUFD4-2025-2026-calculations",
        build_revision=None,
        registry_revision=None,
        provider_app_revision=None,
        access_effect="read",
        acquisition_mode="release_capture",
        scope=Scope(),
        temporal_contract=(
            "reference 2026-09; official slot 2026-10-15T12:30:00Z; "
            "actual obtained_at retained"
        ),
        completeness_contract=COMPLETENESS,
        pagination_contract="single bounded BLS v2 response; no pagination",
        freshness_contract=(
            "retrieval-bound snapshot; PPI interim revisions possible for three "
            "months and final at month four"
        ),
        evidence_capability=frozenset({Grade.ORIGINAL, Grade.PROJECTED}),
    )


def _binding(source: SourceOperationRef) -> SourceBindingRevision:
    return SourceBindingRevision(
        identity=BINDING_IDENTITY,
        revision=CONTRACT_REVISION,
        source=source.key,
        mappings=(
            OutputMapping(
                identity="bls-ppi-final-demand",
                definition=BLS_PPI_FINAL_DEMAND.key,
                semantic_revision=BLS_PPI_FINAL_DEMAND.revision,
                temporal_revision="reference-release-retrieval-revision-v1",
                scope=Scope(),
                evidence=EvidenceRequirement(
                    allowed=frozenset({Grade.ORIGINAL, Grade.PROJECTED}),
                    durable=True,
                    replayable=True,
                ),
            ),
        ),
        compatibility_policy="exact_bls_v2_ppi_final_demand_request_and_parser",
        limitations=(
            "candidate_only_not_active",
            "retrieval_snapshot_not_provider_vintage",
            "revision_state_only_when_source_footnote_declares_it",
            "no_provider_fallback",
        ),
        silent_fallback=False,
    )


PPI_SOURCE = _source()
PPI_BINDING_CANDIDATE = _binding(PPI_SOURCE)


@dataclass(frozen=True)
class PpiLiveEvidence:
    raw_bytes: bytes
    request: BlsPpiRequest
    requested_at: datetime
    obtained_at: datetime
    acquisition_identity: str
    controlled_external_receipt: str
    provider_data_attempts: int = 1

    def __post_init__(self) -> None:
        self.request.persisted()
        instant(self.requested_at)
        instant(self.obtained_at)
        require(self.requested_at <= self.obtained_at, "bls_ppi_live_time_reversal")
        require(
            bool(self.acquisition_identity) and bool(self.controlled_external_receipt),
            "bls_ppi_live_identity_required",
        )
        require(self.provider_data_attempts == 1, "bls_ppi_provider_attempts_invalid")
        parse_response(self.raw_bytes)


@dataclass(frozen=True)
class PpiLivePlan:
    source: SourceOperationRef
    binding_candidate: SourceBindingRevision
    run: DataRun
    acquisition: AcquisitionOccurrence
    raw_material: EvidenceMaterial
    semantic_material: EvidenceMaterial
    version: DatasetVersion
    publication_request: PublicationRequest
    content: bytes


def build_live_plan(
    evidence: PpiLiveEvidence,
    *,
    identity: str,
    finished_at: datetime,
    frozen_at: datetime,
) -> PpiLivePlan:
    require(type(evidence) is PpiLiveEvidence, "bls_ppi_live_evidence_required")
    instant(finished_at)
    instant(frozen_at)
    require(
        RELEASE
        <= evidence.requested_at
        <= evidence.obtained_at
        <= finished_at
        <= frozen_at,
        "bls_ppi_processing_time_invalid",
    )
    require(bool(identity), "bls_ppi_identity_required")
    projection = parse_response(evidence.raw_bytes)
    parameters = (
        ("parser_revision", PARSER_REVISION),
        ("request_identity", evidence.request.identity),
        ("release_attempt", "1"),
        ("release_instant", RELEASE.isoformat()),
        ("release_reference", REFERENCE),
        ("release_slot_identity", RELEASE_SLOT),
    )
    transform = Transform("mdf.macro.bls.ppi.final-demand", "t01-r1", parameters)
    run = DataRun(
        identity + ":run",
        RunKind.COLLECT,
        Scope(),
        evidence.requested_at,
        finished_at,
        "success",
        binding=PPI_BINDING_CANDIDATE.key,
        transform=transform,
        trigger="explicit_owner_nonproduction_ppi_release_capture",
        budget=1,
    )
    acquisition = AcquisitionOccurrence(
        evidence.acquisition_identity,
        run.key,
        evidence.request.identity,
        PPI_SOURCE.key,
        PPI_BINDING_CANDIDATE.key,
        "bls-ppi-final-demand",
        Scope(),
        tuple(
            (key, json.dumps(value, sort_keys=True))
            for key, value in sorted(evidence.request.persisted().items())
        )
        + parameters,
        evidence.requested_at,
        evidence.obtained_at,
        None,
        None,
        "release_capture",
        COMPLETENESS,
        "retrieval-bound snapshot; revision state from row footnotes only",
        "success",
        external_receipt=evidence.controlled_external_receipt,
        provider_data_attempts=1,
        pagination="single bounded response",
        recovery_summary="none; no fallback",
    )
    content = _encode(
        {
            "schema": RULE,
            "provider": "BLS",
            "request": evidence.request.persisted(),
            "request_identity": evidence.request.identity,
            "obtained_at": evidence.obtained_at.isoformat(),
            "provider_revision": {"state": "not_declared", "value": None},
            "provider_vintage": None,
            "release": {
                "reference": REFERENCE,
                "scheduled_at": RELEASE.isoformat(),
                "slot_identity": RELEASE_SLOT,
                "calendar_source": CALENDAR_SOURCE,
            },
            "parser_revision": PARSER_REVISION,
            "data": projection.data,
            "raw": {
                "sha256": projection.raw_sha256,
                "size": projection.raw_size,
                "base64": base64.b64encode(evidence.raw_bytes).decode(),
            },
        }
    )
    raw_material = EvidenceMaterial(
        identity + ":raw",
        Grade.ORIGINAL,
        projection.raw_sha256,
        Scope(),
        acquired_from=acquisition.key,
        controlled_ref=evidence.controlled_external_receipt + "/raw-response.bin",
        durable=False,
        replayable=False,
        named_class="direct_bls_v2_ppi_final_demand_raw",
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
        named_class="canonical_bls_ppi_response_and_projection_carrier",
    )
    version = DatasetVersion(
        identity + ":version",
        BLS_PPI_FINAL_DEMAND.key,
        FiniteScope(
            tuple(f"{REFERENCE}:bls:ppi:{name}" for name in sorted(MEASUREMENTS))
        ),
        Scope(),
        frozen_at,
        run.key,
        digest(content),
        binding=PPI_BINDING_CANDIDATE.key,
        mapping="bls-ppi-final-demand",
        acquisitions=(acquisition.key,),
        materials=(raw_material.key, semantic_material.key),
        transform=transform,
        completeness=COMPLETENESS,
    )
    publication_request = PublicationRequest(
        identity + ":publication",
        identity + ":occurrence",
        version.key,
        RULE,
        Decision.PUBLISH,
        Scope(),
        version.materials,
        frozenset({Grade.ORIGINAL, Grade.PROJECTED}),
        "source-specific-BLS-PPI-final-demand-admission",
        required_completeness=COMPLETENESS,
        claims_replayable=True,
    )
    return PpiLivePlan(
        PPI_SOURCE,
        PPI_BINDING_CANDIDATE,
        run,
        acquisition,
        raw_material,
        semantic_material,
        version,
        publication_request,
        content,
    )


def validate_live_plan(plan: PpiLivePlan, raw: bytes) -> None:
    """Rebuild the complete pre-materialization graph from its raw acquisition."""
    require(type(plan) is PpiLivePlan, "bls_ppi_live_plan_required")
    acquisition = plan.acquisition
    require(
        plan.run.finished_at is not None
        and acquisition.obtained_at is not None
        and type(acquisition.external_receipt) is str
        and bool(acquisition.external_receipt),
        "bls_ppi_plan_acquisition_incomplete",
    )
    identity = plan.run.identity.removesuffix(":run")
    require(identity + ":run" == plan.run.identity, "bls_ppi_plan_identity_mismatch")
    evidence = PpiLiveEvidence(
        raw,
        BlsPpiRequest(),
        acquisition.requested_at,
        acquisition.obtained_at,
        acquisition.identity,
        acquisition.external_receipt,
        acquisition.provider_data_attempts or 0,
    )
    expected = build_live_plan(
        evidence,
        identity=identity,
        finished_at=plan.run.finished_at,
        frozen_at=plan.version.frozen_at,
    )
    require(plan == expected, "bls_ppi_plan_lineage_mismatch")


def durable_plan(plan: PpiLivePlan, locator: str) -> PpiLivePlan:
    return replace(
        plan,
        raw_material=replace(
            plan.raw_material, controlled_ref=locator, durable=True, replayable=True
        ),
    )


def persist_before_publication(
    kernel: CanonicalKernel, plan: PpiLivePlan
) -> DatasetVersion:
    require(plan.source == PPI_SOURCE, "bls_ppi_source_mismatch")
    require(plan.binding_candidate == PPI_BINDING_CANDIDATE, "bls_ppi_binding_mismatch")
    require(
        plan.raw_material.durable and plan.raw_material.replayable,
        "bls_ppi_raw_not_durable",
    )
    for record in (
        plan.source,
        BLS_PPI_FINAL_DEMAND,
        plan.binding_candidate,
        plan.run,
        plan.acquisition,
        plan.raw_material,
        plan.semantic_material,
    ):
        kernel.add(record)
    return kernel.persist_version(plan.version, plan.content)


def publish(kernel: CanonicalKernel, plan: PpiLivePlan) -> Publication:
    return kernel.publish(plan.publication_request)
