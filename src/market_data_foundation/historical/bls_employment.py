"""Bounded CES/CPS Employment retained candidate contract; no provider IO."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

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
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.canonical.repository import Ledger
from market_data_foundation.historical.employment_contract import BLS_EMPLOYMENT
from market_data_foundation.source_request_policy import valid_transport_attempts

ENDPOINT = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
SERIES_IDS = ("CES0000000001", "LNS14000000", "CES0500000003")
REFERENCE = "2026-09"
REFERENCES = ("2026-09", "2026-08", "2026-07")
RELEASE_SLOT = "Employment/2026-09/retrieval-snapshot"
RELEASE_METADATA = {
    "reference": REFERENCE,
    "scheduled_at": None,
    "actual_at": None,
    "slot_identity": RELEASE_SLOT,
}
PARSER_REVISION = "mdf-bls-employment-2026-09-v1"
SOURCE_IDENTITY = "mdf.source-candidate.bls-v2.employment-2026-09"
BINDING_IDENTITY = "mdf.binding-candidate.bls-v2.employment-2026-09"
CONTRACT_REVISION = "t03-snapshot-2026-09-r1"
RULE = "t03-macro-bls-employment-r1"
COMPLETENESS = "exact_ces_cps_2026_07_08_09_with_source_operands"
MAX_RESPONSE_BYTES = 200_000

_DECIMAL = re.compile(r"-?[0-9]{1,12}(?:\.[0-9]{1,6})?")
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
    "nfp_change_sa": ("CES0000000001", "net_changes/1", "thousand_jobs"),
    "unemployment_rate_sa": ("LNS14000000", "value", "percent"),
    "ahe_mom_sa": ("CES0500000003", "pct_changes/1", "percent"),
    "ahe_yoy_sa": ("CES0500000003", "pct_changes/12", "percent"),
}
# Candidate catalog contract: must be qualified against exact original API bytes
# before any live acquisition is admitted. No catalog absence or source fallback.
CATALOG_CONTRACT = {
    "CES0000000001": ("CE", "all employees, thousands", "total nonfarm"),
    "CES0500000003": (
        "CE",
        "average hourly earnings of all employees",
        "total private",
    ),
    "LNS14000000": ("LN", "percent or rate", "16 years and over"),
}


def finite_members() -> tuple[str, ...]:
    return tuple(
        f"{ref}:bls:employment:{name}"
        for ref in REFERENCES
        for name in sorted(MEASUREMENTS)
    )


def _previous(reference: str, months: int) -> str:
    year, month = map(int, reference.split("-"))
    offset = year * 12 + month - 1 - months
    return f"{offset // 12:04d}-{offset % 12 + 1:02d}"


def _encode(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _reject_sensitive(value: object) -> None:
    if type(value) is dict:
        require(
            all(type(key) is str and _SENSITIVE.search(key) is None for key in value),
            "bls_employment_credential_echo_forbidden",
        )
        for item in value.values():
            _reject_sensitive(item)
    elif type(value) is list:
        for item in value:
            _reject_sensitive(item)


def _decode(raw: bytes) -> dict:
    def pairs(items):
        require(len(dict(items)) == len(items), "bls_employment_duplicate_json_key")
        return dict(items)

    try:
        require(type(raw) is bytes, "bls_employment_immutable_bytes_required")
        require(
            0 < len(raw) <= MAX_RESPONSE_BYTES, "bls_employment_response_size_invalid"
        )
        value = json.loads(raw, object_pairs_hook=pairs)
        require(type(value) is dict, "bls_employment_response_object_required")
        _encode(value)
        _reject_sensitive(value)
        return value
    except UnicodeError, ValueError, TypeError:
        raise ContractError("bls_employment_invalid_json") from None


@dataclass(frozen=True)
class BlsEmploymentRequest:
    endpoint: str = ENDPOINT
    method: str = "POST"
    series_ids: tuple[str, ...] = SERIES_IDS
    startyear: str = "2025"
    endyear: str = "2026"
    calculations: bool = True
    catalog: bool = True

    def persisted(self) -> dict:
        require(self.endpoint == ENDPOINT, "bls_employment_endpoint_mismatch")
        require(self.method == "POST", "bls_employment_method_mismatch")
        require(self.series_ids == SERIES_IDS, "bls_employment_series_request_mismatch")
        require(
            self.startyear == "2025" and self.endyear == "2026",
            "bls_employment_year_request_mismatch",
        )
        require(
            self.calculations is True and self.catalog is True,
            "bls_employment_catalog_calculations_required",
        )
        return {
            "endpoint": self.endpoint,
            "method": self.method,
            "body": {
                "seriesid": list(self.series_ids),
                "startyear": self.startyear,
                "endyear": self.endyear,
                "calculations": self.calculations,
                "catalog": self.catalog,
            },
        }

    @property
    def identity(self) -> str:
        return "bls-employment-v2:" + digest(_encode(self.persisted()))


def _footnotes(value: object) -> list[dict[str, str]]:
    require(type(value) is list, "bls_employment_footnotes_invalid")
    result = []
    for item in value:
        require(type(item) is dict, "bls_employment_footnotes_invalid")
        if not item:
            continue
        require(
            set(item) == {"code", "text"}
            and type(item["code"]) is str
            and bool(item["code"])
            and type(item["text"]) is str
            and bool(item["text"]),
            "bls_employment_footnotes_invalid",
        )
        result.append({"code": item["code"], "text": item["text"]})
    return result


def _rows(value: object) -> dict[str, dict]:
    require(type(value) is list and bool(value), "bls_employment_series_data_invalid")
    result: dict[str, dict] = {}
    latest: list[str] = []
    base = {"year", "period", "periodName", "value", "footnotes"}
    for row in value:
        require(type(row) is dict, "bls_employment_row_invalid")
        require(
            set(row)
            in (base, base | {"calculations"}, base | {"latest", "calculations"}),
            "bls_employment_row_fields_invalid",
        )
        require(row["year"] in {"2025", "2026"}, "bls_employment_row_year_invalid")
        require(
            type(row["period"]) is str
            and re.fullmatch(r"M(?:0[1-9]|1[0-2])", row["period"]),
            "bls_employment_row_period_invalid",
        )
        month = int(row["period"][1:])
        require(
            row["periodName"] == _MONTH_NAMES[month - 1]
            and type(row["value"]) is str
            and _DECIMAL.fullmatch(row["value"]),
            "bls_employment_row_value_invalid",
        )
        _footnotes(row["footnotes"])
        pointer = f"{row['year']}-{month:02d}"
        require(pointer not in result, "bls_employment_duplicate_period")
        result[pointer] = row
        if "latest" in row:
            require(row["latest"] == "true", "bls_employment_latest_invalid")
            latest.append(pointer)
        calculations = row.get("calculations")
        if calculations is not None:
            require(
                type(calculations) is dict
                and set(calculations) == {"net_changes", "pct_changes"},
                "bls_employment_calculations_invalid",
            )
            for group in calculations.values():
                require(type(group) is dict, "bls_employment_calculations_invalid")
                for period, number in group.items():
                    require(
                        period in {"1", "3", "6", "12"}
                        and type(number) is str
                        and _DECIMAL.fullmatch(number),
                        "bls_employment_calculations_invalid",
                    )
    require(len(latest) <= 1, "bls_employment_latest_invalid")
    if latest:
        require(latest[0] == max(result), "bls_employment_latest_not_latest_period")
    return result


@dataclass(frozen=True)
class EmploymentProjection:
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
        require(type(calculations) is dict, "bls_employment_target_calculation_missing")
        group, months = pointer.split("/", 1)
        value = calculations[group].get(months)
        require(
            type(value) is str and _DECIMAL.fullmatch(value),
            "bls_employment_target_calculation_missing",
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


def _parse_response(raw: bytes) -> EmploymentProjection:
    payload = _decode(raw)
    require(
        set(payload) == {"status", "responseTime", "message", "Results"},
        "bls_employment_response_fields_invalid",
    )
    require(payload["status"] == "REQUEST_SUCCEEDED", "bls_employment_request_failed")
    require(
        type(payload["responseTime"]) is int and payload["responseTime"] >= 0,
        "bls_employment_response_time_invalid",
    )
    messages = payload["message"]
    require(
        type(messages) is list and all(type(item) is str for item in messages),
        "bls_employment_provider_messages_invalid",
    )
    results = payload["Results"]
    require(
        type(results) is dict and set(results) == {"series"},
        "bls_employment_results_invalid",
    )
    series = results["series"]
    require(
        type(series) is list and len(series) == 3,
        "bls_employment_series_not_exact_three",
    )
    require(
        set(item.get("seriesID") for item in series if type(item) is dict)
        == set(SERIES_IDS),
        "bls_employment_series_identity_mismatch",
    )
    by_series = {}
    catalogs = {}
    indices = {}
    for index, item in enumerate(series):
        require(
            type(item) is dict and set(item) == {"seriesID", "catalog", "data"},
            "bls_employment_series_fields_invalid",
        )
        sid = item["seriesID"]
        catalog = item["catalog"]
        require(type(catalog) is dict, "bls_employment_catalog_required")
        abbreviation, measure, population = CATALOG_CONTRACT[sid]
        require(
            catalog.get("series_id") == sid
            and catalog.get("seasonality") == "Seasonally Adjusted"
            and catalog.get("survey_abbreviation") == abbreviation
            and type(catalog.get("survey_name")) is str
            and (
                "current population survey"
                if abbreviation == "LN"
                else "current employment statistics"
            )
            in catalog["survey_name"].casefold()
            and type(catalog.get("measure_data_type")) is str
            and catalog["measure_data_type"].casefold() == measure
            and type(catalog.get("series_title")) is str,
            "bls_employment_catalog_identity_mismatch",
        )
        if sid == "LNS14000000":
            require(
                type(catalog.get("cps_labor_force_status")) is str
                and catalog["cps_labor_force_status"].casefold() == "unemployment rate"
                and type(catalog.get("demographic_age")) is str
                and catalog["demographic_age"].casefold() == population
                and type(catalog.get("demographic_gender")) is str
                and catalog["demographic_gender"].casefold() == "both sexes"
                and type(catalog.get("demographic_race")) is str
                and catalog["demographic_race"].casefold() == "all races",
                "bls_employment_cps_population_mismatch",
            )
        else:
            require(
                population in catalog["series_title"].casefold(),
                "bls_employment_catalog_identity_mismatch",
            )
        catalogs[sid] = catalog
        by_series[sid] = _rows(item["data"])
        indices[sid] = index
    projected = {}
    for ref in REFERENCES:
        measurements = {}
        for name, (sid, pointer, unit) in MEASUREMENTS.items():
            rows = by_series[sid]
            require(ref in rows, "bls_employment_reference_not_available")
            current = rows[ref]
            periods = (
                (ref,)
                if pointer == "value"
                else (ref, _previous(ref, int(pointer.split("/")[1])))
            )
            require(
                all(period in rows for period in periods),
                "bls_employment_operand_missing",
            )
            source_value = _measurement(current, pointer, unit)
            if name == "nfp_change_sa":
                require(
                    Decimal(source_value["value"])
                    == Decimal(current["value"]) - Decimal(rows[periods[1]]["value"]),
                    "bls_employment_nfp_change_level_mismatch",
                )
            level_unit = (
                "thousand_jobs"
                if sid == "CES0000000001"
                else "usd_per_hour"
                if sid == "CES0500000003"
                else "percent"
            )

            def source_pointer(period):
                row_index = next(
                    i
                    for i, row in enumerate(series[indices[sid]]["data"])
                    if row is rows[period]
                )
                return f"/Results/series/{indices[sid]}/data/{row_index}"

            measurements[name] = {
                "series_id": sid,
                "reference_period": ref,
                "measurement": pointer,
                "origin": "provider_returned"
                if pointer == "value"
                else "provider_returned_calculation",
                "source_pointer": source_pointer(ref)
                + ("/value" if pointer == "value" else "/calculations/" + pointer),
                "catalog_pointer": f"/Results/series/{indices[sid]}/catalog",
                "catalog": catalogs[sid],
                "survey": "CPS" if sid == "LNS14000000" else "CES",
                "population": "civilian_noninstitutional_people_16_plus"
                if sid == "LNS14000000"
                else "total_nonfarm_payroll_jobs"
                if sid == "CES0000000001"
                else "all_employees_total_private",
                "seasonality": "seasonally_adjusted",
                "operands": [
                    {
                        "reference_period": period,
                        "source_pointer": source_pointer(period) + "/value",
                        "unit": level_unit,
                        **_measurement(rows[period], "value", level_unit),
                    }
                    for period in periods
                ],
                **source_value,
            }
        projected[ref] = measurements
    return EmploymentProjection(
        {
            "reference_period": REFERENCE,
            "references": list(REFERENCES),
            "release_slot": RELEASE_SLOT,
            "revision_policy": "ces_two_monthly_revisions_then_annual_benchmark;"
            "cps_separate_seasonal_revisions;no_finality_inferred",
            "measurements": projected,
        },
        tuple(messages),
        payload["responseTime"],
        digest(raw),
        len(raw),
    )


def parse_response(raw: bytes) -> EmploymentProjection:
    """Parse one exact response and collapse Python shape failures to a contract."""
    try:
        return _parse_response(raw)
    except ContractError:
        raise
    except KeyError, IndexError, TypeError, ValueError:
        raise ContractError("bls_employment_response_shape_invalid") from None


def _source() -> SourceOperationRef:
    return SourceOperationRef(
        identity=SOURCE_IDENTITY,
        contract_revision=CONTRACT_REVISION,
        owner="market-data-foundation",
        product="BLS public data API v2",
        operation="timeseries.data:CES-CPS-employment",
        projection_revision=PARSER_REVISION,
        variant="exact-CES-CPS-2025-2026-catalog-calculations",
        build_revision=None,
        registry_revision=None,
        provider_app_revision=None,
        access_effect="read",
        acquisition_mode="release_capture",
        scope=Scope(),
        temporal_contract=(
            "reference snapshot 2026-07 through 2026-09; release instant unknown; "
            "actual obtained_at retained"
        ),
        completeness_contract=COMPLETENESS,
        pagination_contract="single bounded BLS v2 response; no pagination",
        freshness_contract=(
            "retrieval-bound snapshot; CES prior-two-month and benchmark revisions; "
            "CPS seasonal revisions separate; no finality inference"
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
                identity="bls-employment",
                definition=BLS_EMPLOYMENT.key,
                semantic_revision=BLS_EMPLOYMENT.revision,
                temporal_revision="reference-release-retrieval-revision-v1",
                scope=Scope(),
                evidence=EvidenceRequirement(
                    allowed=frozenset({Grade.ORIGINAL, Grade.PROJECTED}),
                    durable=True,
                    replayable=True,
                ),
            ),
        ),
        compatibility_policy="exact_bls_v2_employment_request_and_parser",
        limitations=(
            "candidate_only_not_active",
            "catalog_shape_and_calculation_paths_not_live_qualified",
            "no_live_acquisition_edge",
            "retrieval_snapshot_not_provider_vintage",
            "revision_state_only_when_source_footnote_declares_it",
            "no_provider_fallback",
        ),
        silent_fallback=False,
    )


EMPLOYMENT_SOURCE = _source()
EMPLOYMENT_BINDING_CANDIDATE = _binding(EMPLOYMENT_SOURCE)


@dataclass(frozen=True)
class EmploymentEvidence:
    raw_bytes: bytes
    request: BlsEmploymentRequest
    requested_at: datetime
    obtained_at: datetime
    acquisition_identity: str
    controlled_external_receipt: str
    provider_data_attempts: int = 1

    def __post_init__(self) -> None:
        require(
            type(self.request) is BlsEmploymentRequest,
            "bls_employment_request_type_invalid",
        )
        self.request.persisted()
        instant(self.requested_at)
        instant(self.obtained_at)
        require(
            self.requested_at <= self.obtained_at, "employment_candidate_time_reversal"
        )
        require(
            type(self.acquisition_identity) is str
            and bool(self.acquisition_identity)
            and type(self.controlled_external_receipt) is str
            and bool(self.controlled_external_receipt),
            "employment_candidate_identity_required",
        )
        require(
            valid_transport_attempts(self.provider_data_attempts),
            "bls_employment_provider_attempts_invalid",
        )
        parse_response(self.raw_bytes)


@dataclass(frozen=True)
class EmploymentPlan:
    source: SourceOperationRef
    binding_candidate: SourceBindingRevision
    run: DataRun
    acquisition: AcquisitionOccurrence
    raw_material: EvidenceMaterial
    semantic_material: EvidenceMaterial
    version: DatasetVersion
    publication_request: PublicationRequest
    content: bytes


def build_plan(
    evidence: EmploymentEvidence,
    *,
    identity: str,
    finished_at: datetime,
    frozen_at: datetime,
) -> EmploymentPlan:
    require(type(evidence) is EmploymentEvidence, "employment_evidence_required")
    instant(finished_at)
    instant(frozen_at)
    require(
        evidence.requested_at <= evidence.obtained_at <= finished_at <= frozen_at,
        "bls_employment_processing_time_invalid",
    )
    require(bool(identity), "bls_employment_identity_required")
    projection = parse_response(evidence.raw_bytes)
    parameters = (
        ("parser_revision", PARSER_REVISION),
        ("request_identity", evidence.request.identity),
        ("release_attempt", "1"),
        ("release_reference", REFERENCE),
        ("release_slot_identity", RELEASE_SLOT),
    )
    transform = Transform("mdf.macro.bls.employment", "t03-r1", parameters)
    run = DataRun(
        identity + ":run",
        RunKind.COLLECT,
        Scope(),
        evidence.requested_at,
        finished_at,
        "success",
        binding=EMPLOYMENT_BINDING_CANDIDATE.key,
        transform=transform,
        trigger="explicit_owner_nonproduction_employment_release_capture",
        budget=1,
    )
    acquisition = AcquisitionOccurrence(
        evidence.acquisition_identity,
        run.key,
        evidence.request.identity,
        EMPLOYMENT_SOURCE.key,
        EMPLOYMENT_BINDING_CANDIDATE.key,
        "bls-employment",
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
        provider_data_attempts=evidence.provider_data_attempts,
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
            "requested_at": evidence.requested_at.isoformat(),
            "acquisition_identity": evidence.acquisition_identity,
            "external_receipt": evidence.controlled_external_receipt,
            "provider_revision": {"state": "not_declared", "value": None},
            "provider_vintage": None,
            "release": RELEASE_METADATA,
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
        named_class="direct_bls_v2_employment_raw",
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
        named_class="canonical_bls_employment_response_and_projection_carrier",
    )
    version = DatasetVersion(
        identity + ":version",
        BLS_EMPLOYMENT.key,
        FiniteScope(finite_members()),
        Scope(),
        frozen_at,
        run.key,
        digest(content),
        binding=EMPLOYMENT_BINDING_CANDIDATE.key,
        mapping="bls-employment",
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
        "source-specific-BLS-CES-CPS-employment-candidate-admission",
        required_completeness=COMPLETENESS,
        claims_replayable=True,
    )
    return EmploymentPlan(
        EMPLOYMENT_SOURCE,
        EMPLOYMENT_BINDING_CANDIDATE,
        run,
        acquisition,
        raw_material,
        semantic_material,
        version,
        publication_request,
        content,
    )


def validate_plan(plan: EmploymentPlan, raw: bytes) -> None:
    """Rebuild the complete pre-materialization graph from its raw acquisition."""
    require(type(plan) is EmploymentPlan, "employment_plan_required")
    acquisition = plan.acquisition
    require(
        plan.run.finished_at is not None
        and acquisition.obtained_at is not None
        and type(acquisition.external_receipt) is str
        and bool(acquisition.external_receipt),
        "bls_employment_plan_acquisition_incomplete",
    )
    identity = plan.run.identity.removesuffix(":run")
    require(
        identity + ":run" == plan.run.identity, "bls_employment_plan_identity_mismatch"
    )
    evidence = EmploymentEvidence(
        raw,
        BlsEmploymentRequest(),
        acquisition.requested_at,
        acquisition.obtained_at,
        acquisition.identity,
        acquisition.external_receipt,
        acquisition.provider_data_attempts or 0,
    )
    expected = build_plan(
        evidence,
        identity=identity,
        finished_at=plan.run.finished_at,
        frozen_at=plan.version.frozen_at,
    )
    require(plan == expected, "bls_employment_plan_lineage_mismatch")


def durable_plan(plan: EmploymentPlan, locator: str) -> EmploymentPlan:
    return replace(
        plan,
        raw_material=replace(
            plan.raw_material, controlled_ref=locator, durable=True, replayable=True
        ),
    )


def persist_before_publication(
    kernel: CanonicalKernel, plan: EmploymentPlan
) -> DatasetVersion:
    require(plan.source == EMPLOYMENT_SOURCE, "bls_employment_source_mismatch")
    require(
        plan.binding_candidate == EMPLOYMENT_BINDING_CANDIDATE,
        "bls_employment_binding_mismatch",
    )
    require(
        plan.raw_material.durable and plan.raw_material.replayable,
        "bls_employment_raw_not_durable",
    )
    for record in (
        plan.source,
        BLS_EMPLOYMENT,
        plan.binding_candidate,
        plan.run,
        plan.acquisition,
        plan.raw_material,
        plan.semantic_material,
    ):
        kernel.add(record)
    return kernel.persist_version(plan.version, plan.content)


def validate_publication_time(
    publication: Publication, obtained_at: datetime, frozen_at: datetime
) -> None:
    """Reject a decision or availability preceding the retained source graph."""
    require(
        publication.decision_at >= frozen_at >= obtained_at
        and publication.usable_at >= publication.decision_at,
        "employment_publication_time_inversion",
    )


def _validate_publication_source(ledger: Ledger, plan: EmploymentPlan) -> None:
    # The time bound must belong to the persisted graph, not caller replacements
    # carrying the same keys but earlier source/freeze timestamps.
    require(
        plan.publication_request.version == plan.version.key
        and ledger.get(plan.version.key) == plan.version
        and ledger.get(plan.acquisition.key) == plan.acquisition,
        "employment_publication_source_mismatch",
    )


class _EmploymentPublicationLedger(PostgresLedger):
    """Check server samples inside the existing atomic publication transaction."""

    def __init__(self, ledger: PostgresLedger, plan: EmploymentPlan) -> None:
        super().__init__(ledger.connection, ledger.schema)
        self.plan = plan

    def server_now(self) -> datetime:
        at = super().server_now()
        require(
            at >= self.plan.version.frozen_at,
            "employment_publication_time_inversion",
        )
        return at

    def publication_by_occurrence(self, occurrence: str) -> Publication | None:
        _validate_publication_source(self, self.plan)
        publication = super().publication_by_occurrence(occurrence)
        if publication is not None:
            validate_publication_time(
                publication,
                self.plan.acquisition.obtained_at,
                self.plan.version.frozen_at,
            )
        return publication


def publish(kernel: CanonicalKernel, plan: EmploymentPlan) -> Publication:
    require(
        plan.acquisition.obtained_at is not None
        and plan.version.frozen_at >= plan.acquisition.obtained_at,
        "employment_publication_time_inversion",
    )

    def checked_clock() -> datetime:
        at = kernel.clock()
        require(at >= plan.version.frozen_at, "employment_publication_time_inversion")
        return at

    ledger = kernel.ledger
    if isinstance(ledger, PostgresLedger):
        # PostgresLedger deliberately ignores caller clocks. Validate its final
        # server timestamp before INSERT/COMMIT without changing shared semantics.
        ledger = _EmploymentPublicationLedger(ledger, plan)
    else:
        _validate_publication_source(ledger, plan)
    publication = ledger.publish(plan.publication_request, checked_clock)
    validate_publication_time(
        publication, plan.acquisition.obtained_at, plan.version.frozen_at
    )
    return publication
