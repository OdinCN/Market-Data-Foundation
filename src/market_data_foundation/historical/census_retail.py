"""Bounded Census MARTS retail snapshot contract; no provider IO."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

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
from market_data_foundation.historical.retail_contract import CENSUS_RETAIL
from market_data_foundation.source_request_policy import valid_transport_attempts

ENDPOINT = "https://api.census.gov/data/timeseries/eits/marts"
REFERENCE = "2026-09"
PREVIOUS = "2026-08"
REFERENCES = (PREVIOUS, REFERENCE)
CATEGORY_CODES = ("44X72", "441", "444", "447", "722")
SUBTRACT = ("441", "444", "447", "722")
FIELDS = (
    "category_code",
    "data_type_code",
    "seasonally_adj",
    "cell_value",
    "error_data",
)
HEADERS = (*FIELDS, "time", "us")
FORMULA_REVISION = "mdf-retail-control-44x72-less-441-444-447-722-v1"
PARSER_REVISION = "mdf-census-marts-2026-09-v1"
SOURCE_IDENTITY = "mdf.source-candidate.census-marts.retail-2026-09"
BINDING_IDENTITY = "mdf.binding-candidate.census-marts.retail-2026-09"
CONTRACT_REVISION = "t05-snapshot-2026-09-r1"
RULE = "t05-macro-census-retail-r1"
RELEASE_SLOT = "MARTS/2026-09/retrieval-snapshot"
RELEASE_METADATA = {
    "reference": REFERENCE,
    "scheduled_at": "2026-10-15T12:30:00+00:00",
    "actual_at": None,
    "slot_identity": RELEASE_SLOT,
}
COMPLETENESS = "marts_sa_us_two_period_five_category_sm_current_headline_mpcsm"
MAX_RESPONSE_BYTES = 100_000
MEASUREMENTS = (
    "headline_level_sa",
    "headline_mom_sa",
    "control_level_sa",
    "control_mom_sa",
)
_DECIMAL = re.compile(r"-?[0-9]{1,12}(?:\.[0-9]{1,6})?")


def _encode(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def finite_members() -> tuple[str, ...]:
    return tuple(f"{REFERENCE}:census:retail:{name}" for name in sorted(MEASUREMENTS))


@dataclass(frozen=True)
class CensusRetailRequest:
    endpoint: str = ENDPOINT
    method: str = "GET"

    def persisted(self) -> dict:
        require(self.endpoint == ENDPOINT, "census_retail_endpoint_mismatch")
        require(self.method == "GET", "census_retail_method_mismatch")
        return {
            "endpoint": self.endpoint,
            "method": self.method,
            "parameters": {
                "get": ",".join(FIELDS),
                "seasonally_adj": "yes",
                "error_data": "no",
                "for": "us:*",
                "time": f"from {PREVIOUS} to {REFERENCE}",
            },
        }

    @property
    def identity(self) -> str:
        return "census-marts:" + digest(_encode(self.persisted()))


@dataclass(frozen=True)
class RetailProjection:
    data: dict
    raw_sha256: str
    raw_size: int


def _parse_response(raw: bytes) -> RetailProjection:
    require(
        type(raw) is bytes and 0 < len(raw) <= MAX_RESPONSE_BYTES,
        "census_retail_raw_bytes_invalid",
    )
    payload = json.loads(raw)
    require(type(payload) is list and len(payload) > 1, "census_retail_table_required")
    header = payload[0]
    require(
        type(header) is list
        and all(type(item) is str for item in header)
        and len(header) == len(HEADERS)
        and set(header) == set(HEADERS),
        "census_retail_header_invalid",
    )
    expected = {(ref, cat, "SM") for ref in REFERENCES for cat in CATEGORY_CODES} | {
        (REFERENCE, "44X72", "MPCSM")
    }
    rows = {}
    for index, values in enumerate(payload[1:], 1):
        require(
            type(values) is list
            and len(values) == len(header)
            and all(type(value) is str for value in values),
            "census_retail_row_shape_invalid",
        )
        row = dict(zip(header, values, strict=True))
        require(
            row["us"] == "1"
            and row["seasonally_adj"] == "yes"
            and row["error_data"] == "no",
            "census_retail_row_semantics_invalid",
        )
        require(
            row["time"] in REFERENCES,
            "census_retail_row_target_invalid",
        )
        key = (row["time"], row["category_code"], row["data_type_code"])
        # The broad MARTS response includes cells outside this product's matrix.
        if key not in expected:
            continue
        require(key not in rows, "census_retail_duplicate_row")
        value = row["cell_value"]
        require(
            _DECIMAL.fullmatch(value) is not None,
            "census_retail_cell_unavailable_or_malformed",
        )
        if row["data_type_code"] == "SM":
            require(Decimal(value) > 0, "census_retail_level_not_positive")
        rows[key] = {
            "reference_period": key[0],
            "category_code": key[1],
            "data_type_code": key[2],
            "value": value,
            "unit": "million_current_usd" if key[2] == "SM" else "percent",
            "source_pointer": f"/{index}/{header.index('cell_value')}",
            "raw_sha256": digest(raw),
        }
    require(expected <= rows.keys(), "census_retail_required_matrix_missing")

    def operand(ref, cat, typ="SM"):
        return dict(rows[ref, cat, typ])

    def inputs(ref):
        return [operand(ref, cat) for cat in ("44X72", *SUBTRACT)]

    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_EVEN
        controls = {
            ref: Decimal(rows[ref, "44X72", "SM"]["value"])
            - sum(Decimal(rows[ref, cat, "SM"]["value"]) for cat in SUBTRACT)
            for ref in REFERENCES
        }
        require(
            all(value > 0 for value in controls.values()),
            "census_retail_control_not_positive",
        )
        mom = ((controls[REFERENCE] / controls[PREVIOUS] - 1) * 100).quantize(
            Decimal("0.000001")
        )
    common = {
        "reference_period": REFERENCE,
        "seasonality": "seasonal_holiday_trading_day_adjusted",
        "price_adjusted": False,
    }
    measurements = {
        "headline_level_sa": {
            **common,
            **operand(REFERENCE, "44X72"),
            "origin": "provider_returned",
            "operands": [operand(REFERENCE, "44X72")],
        },
        "headline_mom_sa": {
            **common,
            **operand(REFERENCE, "44X72", "MPCSM"),
            "origin": "provider_returned_calculation",
            "comparison_period": PREVIOUS,
            "operands": [operand(REFERENCE, "44X72", "MPCSM")],
            "calculation_policy": "provider_MPCSM_preserved_without_recomputation",
        },
        "control_level_sa": {
            **common,
            "value": str(controls[REFERENCE]),
            "unit": "million_current_usd",
            "origin": "product_derived",
            "formula_revision": FORMULA_REVISION,
            "formula": "SM[44X72]-SM[441]-SM[444]-SM[447]-SM[722]",
            "operands": inputs(REFERENCE),
        },
        "control_mom_sa": {
            **common,
            "value": str(mom),
            "unit": "percent",
            "origin": "product_derived",
            "comparison_period": PREVIOUS,
            "formula_revision": FORMULA_REVISION,
            "formula": "(control[current]/control[previous]-1)*100",
            "decimal_policy": "precision_28_half_even_6dp",
            "control_operands": [
                {
                    "reference_period": ref,
                    "value": str(controls[ref]),
                    "operands": inputs(ref),
                }
                for ref in (REFERENCE, PREVIOUS)
            ],
        },
    }
    return RetailProjection(
        {
            "reference_period": REFERENCE,
            "comparison_period": PREVIOUS,
            "release_slot": RELEASE_SLOT,
            "snapshot_semantics": (
                "current_retrieval_snapshot_not_historical_as_published"
            ),
            "revision_policy": (
                "monthly_revision_and_annual_benchmark_seasonal_revision;"
                "no_finality_inferred"
            ),
            "control_definition": {
                "owner": "MDF",
                "revision": FORMULA_REVISION,
                "census_named_field": False,
                "total": "44X72",
                "subtract": list(SUBTRACT),
                "retained_category": "445",
            },
            "source_cells": [dict(rows[key]) for key in sorted(rows)],
            "measurements": measurements,
        },
        digest(raw),
        len(raw),
    )


def parse_response(raw: bytes) -> RetailProjection:
    """Fail closed; never persist provider exceptions or malformed response text."""
    try:
        return _parse_response(raw)
    except ContractError:
        raise
    except KeyError, IndexError, TypeError, ValueError, UnicodeError:
        raise ContractError("census_retail_response_shape_invalid") from None


def _source() -> SourceOperationRef:
    return SourceOperationRef(
        identity=SOURCE_IDENTITY,
        contract_revision=CONTRACT_REVISION,
        owner="market-data-foundation",
        product="U.S. Census Bureau MARTS API",
        operation="timeseries/eits/marts:retail",
        projection_revision=PARSER_REVISION,
        variant="exact-SA-SM-MPCSM-current-previous",
        build_revision=None,
        registry_revision=None,
        provider_app_revision=None,
        access_effect="read",
        acquisition_mode="release_capture",
        scope=Scope(),
        temporal_contract=(
            "reference snapshot 2026-08 through 2026-09; release actual time unknown; "
            "actual obtained_at retained"
        ),
        completeness_contract=COMPLETENESS,
        pagination_contract="single bounded Census MARTS response; no pagination",
        freshness_contract=(
            "retrieval-bound MARTS snapshot; monthly and annual revisions; "
            "no historical as-published or finality inference"
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
                identity="census-retail",
                definition=CENSUS_RETAIL.key,
                semantic_revision=CENSUS_RETAIL.revision,
                temporal_revision="reference-release-retrieval-revision-v1",
                scope=Scope(),
                evidence=EvidenceRequirement(
                    allowed=frozenset({Grade.ORIGINAL, Grade.PROJECTED}),
                    durable=True,
                    replayable=True,
                ),
            ),
        ),
        compatibility_policy="exact_marts_request_and_parser",
        limitations=(
            "candidate_only_not_active",
            "response_shape_not_live_qualified",
            "live_default_off",
            "retrieval_snapshot_not_provider_vintage",
            "revision_state_not_declared_by_api",
            "no_provider_fallback",
        ),
        silent_fallback=False,
    )


RETAIL_SOURCE = _source()
RETAIL_BINDING_CANDIDATE = _binding(RETAIL_SOURCE)


@dataclass(frozen=True)
class RetailEvidence:
    raw_bytes: bytes
    request: CensusRetailRequest
    requested_at: datetime
    obtained_at: datetime
    acquisition_identity: str
    controlled_external_receipt: str
    provider_data_attempts: int = 1

    def __post_init__(self) -> None:
        require(
            type(self.request) is CensusRetailRequest,
            "census_retail_request_type_invalid",
        )
        self.request.persisted()
        instant(self.requested_at)
        instant(self.obtained_at)
        require(self.requested_at <= self.obtained_at, "retail_candidate_time_reversal")
        require(
            type(self.acquisition_identity) is str
            and bool(self.acquisition_identity)
            and type(self.controlled_external_receipt) is str
            and bool(self.controlled_external_receipt),
            "retail_candidate_identity_required",
        )
        require(
            valid_transport_attempts(self.provider_data_attempts),
            "census_retail_provider_attempts_invalid",
        )
        parse_response(self.raw_bytes)


@dataclass(frozen=True)
class RetailPlan:
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
    evidence: RetailEvidence,
    *,
    identity: str,
    finished_at: datetime,
    frozen_at: datetime,
) -> RetailPlan:
    require(type(evidence) is RetailEvidence, "retail_evidence_required")
    instant(finished_at)
    instant(frozen_at)
    require(
        evidence.requested_at <= evidence.obtained_at <= finished_at <= frozen_at,
        "census_retail_processing_time_invalid",
    )
    require(bool(identity), "census_retail_identity_required")
    projection = parse_response(evidence.raw_bytes)
    parameters = (
        ("parser_revision", PARSER_REVISION),
        ("formula_revision", FORMULA_REVISION),
        ("request_identity", evidence.request.identity),
        ("release_attempt", "1"),
        ("release_reference", REFERENCE),
        ("release_slot_identity", RELEASE_SLOT),
    )
    transform = Transform("mdf.macro.census.retail", "t05-r1", parameters)
    run = DataRun(
        identity + ":run",
        RunKind.COLLECT,
        Scope(),
        evidence.requested_at,
        finished_at,
        "success",
        binding=RETAIL_BINDING_CANDIDATE.key,
        transform=transform,
        trigger="explicit_owner_nonproduction_retail_release_capture",
        budget=1,
    )
    acquisition = AcquisitionOccurrence(
        evidence.acquisition_identity,
        run.key,
        evidence.request.identity,
        RETAIL_SOURCE.key,
        RETAIL_BINDING_CANDIDATE.key,
        "census-retail",
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
        "retrieval-bound snapshot; provider revision unknown; current snapshot only",
        "success",
        external_receipt=evidence.controlled_external_receipt,
        provider_data_attempts=evidence.provider_data_attempts,
        pagination="single bounded response",
        recovery_summary="none; no fallback",
    )
    content = _encode(
        {
            "schema": RULE,
            "provider": "Census",
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
        named_class="direct_census_marts_retail_raw",
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
        named_class="canonical_census_retail_response_and_projection_carrier",
    )
    version = DatasetVersion(
        identity + ":version",
        CENSUS_RETAIL.key,
        FiniteScope(finite_members()),
        Scope(),
        frozen_at,
        run.key,
        digest(content),
        binding=RETAIL_BINDING_CANDIDATE.key,
        mapping="census-retail",
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
        "source-specific-Census-MARTS-retail-candidate-admission",
        required_completeness=COMPLETENESS,
        claims_replayable=True,
    )
    return RetailPlan(
        RETAIL_SOURCE,
        RETAIL_BINDING_CANDIDATE,
        run,
        acquisition,
        raw_material,
        semantic_material,
        version,
        publication_request,
        content,
    )


def validate_live_plan(plan: RetailPlan, raw: bytes) -> None:
    """Rebuild the complete pre-materialization graph from its raw acquisition."""
    require(type(plan) is RetailPlan, "retail_plan_required")
    acquisition = plan.acquisition
    require(
        plan.run.finished_at is not None
        and acquisition.obtained_at is not None
        and type(acquisition.external_receipt) is str
        and bool(acquisition.external_receipt),
        "census_retail_plan_acquisition_incomplete",
    )
    identity = plan.run.identity.removesuffix(":run")
    require(
        identity + ":run" == plan.run.identity, "census_retail_plan_identity_mismatch"
    )
    evidence = RetailEvidence(
        raw,
        CensusRetailRequest(),
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
    require(plan == expected, "census_retail_plan_lineage_mismatch")


def durable_plan(plan: RetailPlan, locator: str) -> RetailPlan:
    return replace(
        plan,
        raw_material=replace(
            plan.raw_material, controlled_ref=locator, durable=True, replayable=True
        ),
    )


def persist_before_publication(
    kernel: CanonicalKernel, plan: RetailPlan
) -> DatasetVersion:
    require(plan.source == RETAIL_SOURCE, "census_retail_source_mismatch")
    require(
        plan.binding_candidate == RETAIL_BINDING_CANDIDATE,
        "census_retail_binding_mismatch",
    )
    require(
        plan.raw_material.durable and plan.raw_material.replayable,
        "census_retail_raw_not_durable",
    )
    for record in (
        plan.source,
        CENSUS_RETAIL,
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
        "retail_publication_time_inversion",
    )


def _validate_publication_source(ledger: Ledger, plan: RetailPlan) -> None:
    # The time bound must belong to the persisted graph, not caller replacements
    # carrying the same keys but earlier source/freeze timestamps.
    require(
        plan.publication_request.version == plan.version.key
        and ledger.get(plan.version.key) == plan.version
        and ledger.get(plan.acquisition.key) == plan.acquisition,
        "retail_publication_source_mismatch",
    )


class _RetailPublicationLedger(PostgresLedger):
    """Check server samples inside the existing atomic publication transaction."""

    def __init__(self, ledger: PostgresLedger, plan: RetailPlan) -> None:
        super().__init__(ledger.connection, ledger.schema)
        self.plan = plan

    def server_now(self) -> datetime:
        at = super().server_now()
        require(
            at >= self.plan.version.frozen_at,
            "retail_publication_time_inversion",
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


def publish(kernel: CanonicalKernel, plan: RetailPlan) -> Publication:
    require(
        plan.acquisition.obtained_at is not None
        and plan.version.frozen_at >= plan.acquisition.obtained_at,
        "retail_publication_time_inversion",
    )

    def checked_clock() -> datetime:
        at = kernel.clock()
        require(at >= plan.version.frozen_at, "retail_publication_time_inversion")
        return at

    ledger = kernel.ledger
    if isinstance(ledger, PostgresLedger):
        # PostgresLedger deliberately ignores caller clocks. Validate its final
        # server timestamp before INSERT/COMMIT without changing shared semantics.
        ledger = _RetailPublicationLedger(ledger, plan)
    else:
        _validate_publication_source(ledger, plan)
    publication = ledger.publish(plan.publication_request, checked_clock)
    validate_publication_time(
        publication, plan.acquisition.obtained_at, plan.version.frozen_at
    )
    return publication
