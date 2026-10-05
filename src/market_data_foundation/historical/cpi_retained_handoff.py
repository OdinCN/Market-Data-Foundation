"""One offline MDD CPI handoff. No MDD client, provider I/O, or active binding."""

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    ContractError,
    DataRun,
    DatasetVersion,
    Decision,
    EvidenceMaterial,
    FiniteScope,
    Grade,
    Publication,
    PublicationRequest,
    RunKind,
    Scope,
    Transform,
    digest,
    instant,
    require,
)
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES

PRODUCT_REVISION = "5844bfb9f4cf5eb39004d787fb77a141924a66b5"
RETAINED_ID = "ret_e66f87eb35223025bfc1c1f02b09f8ef52556ab867ffb984ac1e2394dc12b9f0"
ACQUISITION = "e5248f21-1de8-41f5-9918-1dae1aa50e39"
RAW_SHA256 = "2b239967c61197eaf88895a045be216867c772d303bc2ee929b7849575cb8f4c"
RAW_SIZE = 16194
# Canonical JSON digest of the exact captured retained semantic record. This
# binds values/footnotes as well as descriptors without parsing original.bin.
SEMANTIC_RECORD_SHA256 = (
    "9b36751b276c2e0e37fc39d497d2d8a37774f4b997a66d45a775a8061d234c40"
)
OBSERVATION_CHANGE = "2e31a59815851225f9f14e7cee2a20b1d164bd628d84480a9bff1a39914e6c92"
PARSER = "semantics:71ba383a31a44cfefd7ea7bc557d1e131f05cff7df5ae0762e43c7b10c26fd10"
OBTAINED = "2026-09-11T13:09:57.394089+00:00"
USABLE = "2026-09-11T13:10:00.252815+00:00"
CAPABILITY = "bls.cpi.headlines"
CAPABILITY_REVISION = "bls-headlines-v1"
API_CONTRACT = "macro-consumer-v1"
COMPLETENESS = "bounded_retained_four_measurements"
RULE = "p4-macro-bls-cpi-retained-handoff-r1"

# Descriptors only: values come exclusively from the admitted MDD v1 record.
MEASURES = {
    "core_mom": ("CUSR0000SA0L1E", "All items less food and energy", 1),
    "core_yoy": ("CUUR0000SA0L1E", "All items less food and energy", 12),
    "headline_mom": ("CUSR0000SA0", "All items", 1),
    "headline_yoy": ("CUUR0000SA0", "All items", 12),
}


def project_provider_calculations(
    calculations: dict[str, tuple[str, bool, list[dict[str, str]]]],
) -> dict:
    """Build the shared CPI projection only from provider pct_changes values."""
    require(set(calculations) == set(MEASURES), "cpi_calculations_not_exact_four")
    measurements = []
    for name, (series, meaning, months) in MEASURES.items():
        value, preliminary, footnotes = calculations[name]
        require(
            type(value) is str and re.fullmatch(r"-?\d+(\.\d+)?", value) is not None,
            "cpi_provider_calculation_invalid",
        )
        require(type(preliminary) is bool, "cpi_preliminary_invalid")
        require(
            type(footnotes) is list
            and all(
                type(item) is dict
                and set(item) == {"code", "text"}
                and type(item["code"]) is str
                and type(item["text"]) is str
                for item in footnotes
            ),
            "cpi_footnotes_invalid",
        )
        measurements.append(
            {
                "name": name,
                "measure_identity": "bls:cpi:" + name,
                "series_id": series,
                "meaning": meaning,
                "change_months": months,
                "comparison_period": "2026-07" if months == 1 else "2025-08",
                "seasonal_adjustment": (
                    "Seasonally Adjusted" if months == 1 else "Not Seasonally Adjusted"
                ),
                "reference_period": "2026-08",
                "unit": "percent",
                "classification": "provider_returned_calculation",
                "value": value,
                "availability": "available",
                "unavailable_reason": None,
                "preliminary": preliminary,
                "finality": "not_asserted",
                "footnotes": footnotes,
            }
        )
    data = {
        "product": "CPI",
        "reference_period": "2026-08",
        "measurements": measurements,
        "availability": {
            "state": "available",
            "basis": "retained_measure_completeness_not_historical_cutoff_eligibility",
        },
    }
    validate_cpi_projection(data)
    return data


def validate_cpi_projection(data: object) -> dict:
    """Validate the semantic projection shared by retained and direct paths."""
    require(
        type(data) is dict
        and set(data)
        == {"product", "reference_period", "measurements", "availability"},
        "cpi_data_fields_not_admitted",
    )
    require(
        data["product"] == "CPI" and data["reference_period"] == "2026-08",
        "cpi_data_identity_mismatch",
    )
    require(
        data["availability"]
        == {
            "state": "available",
            "basis": "retained_measure_completeness_not_historical_cutoff_eligibility",
        },
        "cpi_semantic_availability_not_admitted",
    )
    measurements = data["measurements"]
    require(
        type(measurements) is list and len(measurements) == 4,
        "cpi_measurements_not_exact_four",
    )
    names = []
    for m in measurements:
        require(
            type(m) is dict
            and set(m)
            == {
                "name",
                "measure_identity",
                "series_id",
                "meaning",
                "change_months",
                "comparison_period",
                "seasonal_adjustment",
                "reference_period",
                "unit",
                "classification",
                "value",
                "availability",
                "unavailable_reason",
                "preliminary",
                "finality",
                "footnotes",
            },
            "cpi_measurement_fields_not_admitted",
        )
        name = m["name"]
        require(
            type(name) is str and name in MEASURES, "cpi_measurement_name_not_admitted"
        )
        series, meaning, months = MEASURES[name]
        names.append(name)
        require(
            m["measure_identity"] == "bls:cpi:" + name
            and m["series_id"] == series
            and m["meaning"] == meaning
            and type(m["change_months"]) is int
            and m["change_months"] == months
            and m["seasonal_adjustment"]
            == ("Seasonally Adjusted" if months == 1 else "Not Seasonally Adjusted")
            and m["comparison_period"] == ("2026-07" if months == 1 else "2025-08")
            and m["reference_period"] == "2026-08",
            "cpi_measurement_identity_mismatch",
        )
        require(
            m["unit"] == "percent"
            and m["classification"] == "provider_returned_calculation",
            "cpi_measurement_unit_classification_not_admitted",
        )
        require(
            type(m["value"]) is str
            and re.fullmatch(r"-?\d+(\.\d+)?", m["value"]) is not None
            and m["availability"] == "available"
            and m["unavailable_reason"] is None
            and type(m["preliminary"]) is bool
            and m["finality"] == "not_asserted"
            and type(m["footnotes"]) is list,
            "cpi_measurement_value_not_admitted",
        )
    require(set(names) == set(MEASURES), "cpi_measurements_not_exact_four")
    return data


def _encode(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _json(raw: bytes) -> dict:
    def pairs(items):
        require(len(dict(items)) == len(items), "cpi_duplicate_json_key")
        return dict(items)

    try:
        require(type(raw) is bytes, "cpi_immutable_bytes_required")
        value = json.loads(raw, object_pairs_hook=pairs)
        require(type(value) is dict, "cpi_json_object_required")
        _encode(value)  # Reject non-finite JSON numbers.
        return value
    except (ValueError, TypeError, UnicodeError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("cpi_invalid_json") from None


def _time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
        instant(result)
        return result
    except TypeError, ValueError:
        raise ContractError("cpi_invalid_time") from None


@dataclass(frozen=True)
class CpiHandoffReceipt:
    current_bytes: bytes
    retained_bytes: bytes
    raw_bytes: bytes
    lineage_bytes: bytes


@dataclass(frozen=True)
class CpiCanonicalMaterials:
    retained_bytes: bytes
    raw_bytes: bytes


def _semantic_lineage() -> dict:
    return {
        "product_revision": PRODUCT_REVISION,
        "api_contract": API_CONTRACT,
        "capability": CAPABILITY,
        "capability_revision": CAPABILITY_REVISION,
        "reference_period": "2026-08",
        "retained_id": RETAINED_ID,
        "acquisition_identity": ACQUISITION,
        "artifact_sha256": RAW_SHA256,
        "artifact_size": RAW_SIZE,
        "observation_change_identity": OBSERVATION_CHANGE,
        "parser_revision": PARSER,
        "change_kind": "parser_correction",
        "provider_revision": {"state": "not_declared", "value": None},
        "obtained_at": OBTAINED,
        "usable_at": USABLE,
        "provider_released_at": None,
        "provider_vintage": None,
    }


def admit(receipt: CpiHandoffReceipt) -> tuple[dict, dict]:
    """Validate source semantics and lineage; raw BLS bytes remain opaque."""
    current, retained, lineage = (
        _json(receipt.current_bytes),
        _json(receipt.retained_bytes),
        _json(receipt.lineage_bytes),
    )
    for envelope in (current, retained):
        for field, expected in (
            ("api_contract", API_CONTRACT),
            ("product_revision", PRODUCT_REVISION),
            ("capability_id", CAPABILITY),
        ):
            require(envelope.get(field) == expected, "cpi_" + field + "_mismatch")
    require(
        retained.get("retained_semantics")
        == "application_retained_semantic_revision_not_provider_vintage",
        "cpi_retained_semantics_mismatch",
    )
    record = retained.get("retained")
    require(
        type(record) is dict and current.get("current") == record,
        "cpi_current_retained_disagreement",
    )
    require(
        set(record)
        == {
            "provider",
            "reference_period",
            "obtained_at",
            "usable_at",
            "provider_released_at",
            "revision",
            "data",
            "retained_id",
        },
        "cpi_record_fields_not_admitted",
    )
    for field, expected in (
        ("provider", "BLS"),
        ("reference_period", "2026-08"),
        ("retained_id", RETAINED_ID),
        ("obtained_at", OBTAINED),
        ("usable_at", USABLE),
        ("provider_released_at", None),
    ):
        require(record[field] == expected, "cpi_" + field + "_mismatch")
    require(
        _time(record["usable_at"]) >= _time(record["obtained_at"]),
        "cpi_usable_before_obtained",
    )
    revision = {
        "acquisition_identity": ACQUISITION,
        "artifact_identities": {"data": RAW_SHA256},
        "observation_change_identity": OBSERVATION_CHANGE,
        "parser_revision": PARSER,
        "capability_revision": CAPABILITY_REVISION,
        "change_kind": "parser_correction",
        "retained_revision_identity": RETAINED_ID,
        "provider_revision": {"state": "not_declared", "value": None},
    }
    require(record["revision"] == revision, "cpi_revision_mismatch")
    expected_lineage = _semantic_lineage()
    require(
        set(lineage) == set(expected_lineage) | {"capture"},
        "cpi_lineage_fields_not_admitted",
    )
    require(
        all(lineage[k] == v for k, v in expected_lineage.items()),
        "cpi_lineage_mismatch",
    )
    capture = lineage["capture"]
    require(
        type(capture) is dict
        and set(capture)
        == {
            "captured_at",
            "pg_read_only",
            "raw_access",
            "backend",
            "raw_reparsed",
        },
        "cpi_capture_fields_not_admitted",
    )
    require(
        capture["pg_read_only"] is True
        and capture["raw_reparsed"] is False
        and capture["backend"] == "s3"
        and capture["raw_access"] == "existing_mdd_artifact_store.read",
        "cpi_capture_not_admitted",
    )
    _time(capture["captured_at"])
    require(type(receipt.raw_bytes) is bytes, "cpi_immutable_bytes_required")
    require(len(receipt.raw_bytes) == RAW_SIZE, "cpi_raw_size_mismatch")
    require(digest(receipt.raw_bytes) == RAW_SHA256, "cpi_raw_sha_mismatch")
    data = record["data"]
    validate_cpi_projection(data)
    require(
        digest(_encode(record)) == SEMANTIC_RECORD_SHA256,
        "cpi_semantic_record_identity_mismatch",
    )
    return record, expected_lineage


@dataclass(frozen=True)
class CpiHandoffPlan:
    receipt: CpiHandoffReceipt
    run: DataRun
    raw_material: EvidenceMaterial
    semantic_material: EvidenceMaterial
    version: DatasetVersion
    publication_request: PublicationRequest
    content: bytes


def build_handoff_plan(
    receipt: CpiHandoffReceipt,
    *,
    identity: str,
    started_at: datetime,
    finished_at: datetime,
    frozen_at: datetime,
) -> CpiHandoffPlan:
    record, lineage = admit(receipt)
    for value in (started_at, finished_at, frozen_at):
        instant(value)
    capture = _json(receipt.lineage_bytes)["capture"]
    require(
        _time(capture["captured_at"]) <= started_at <= finished_at <= frozen_at,
        "cpi_mdf_processing_backdated",
    )
    require(_time(USABLE) < started_at, "cpi_mdf_processing_backdated")
    require(bool(identity), "cpi_identity_required")
    transform = Transform(
        "mdf.p4.macro.bls.cpi.retained-handoff",
        "r1",
        tuple((key, _encode(value).decode()) for key, value in sorted(lineage.items())),
    )
    run = DataRun(
        identity + ":run",
        RunKind.REPROCESS,
        Scope(),
        started_at,
        finished_at,
        "success",
        transform=transform,
        trigger="offline_mdd_retained_handoff",
    )
    # ORIGINAL is the existing provider-original byte grade: these are an EXACT
    # copy produced by REPROCESS, with no MDF acquisition or reparsing. PROJECTED
    # describes the MDD consumer semantic record, never the original BLS bytes.
    # Only semantic/raw material lives in the canonical content envelope.
    # current.json and full lineage.json remain exact ignored intake evidence;
    # their capture observations must not participate in DatasetVersion identity.
    # Member refs below are resolved only by this bounded adapter, not by a
    # generic storage backend.
    content = _encode(
        {
            "schema": RULE,
            "mdd_lineage": lineage,
            "admitted_record": record,
            "materials": {
                "original.bin": base64.b64encode(receipt.raw_bytes).decode(),
                "retained.json": base64.b64encode(receipt.retained_bytes).decode(),
            },
            "completeness": COMPLETENESS,
            "provider_freshness": "not_asserted",
        }
    )
    raw_material = EvidenceMaterial(
        identity + ":raw",
        Grade.ORIGINAL,
        RAW_SHA256,
        Scope(),
        produced_by=run.key,
        controlled_ref=f"mdf-cpi-content://{identity}:version/original.bin",
        durable=True,
        named_class="exact_mdd_retained_raw_byte_copy",
    )
    semantic_material = EvidenceMaterial(
        identity + ":semantic",
        Grade.PROJECTED,
        digest(receipt.retained_bytes),
        Scope(),
        produced_by=run.key,
        controlled_ref=f"mdf-cpi-content://{identity}:version/retained.json",
        durable=True,
        named_class="admitted_mdd_v1_semantic_projection",
    )
    version = DatasetVersion(
        identity + ":version",
        BLS_CPI_HEADLINES.key,
        FiniteScope(tuple("2026-08:bls:cpi:" + name for name in sorted(MEASURES))),
        Scope(),
        frozen_at,
        run.key,
        digest(content),
        materials=(raw_material.key, semantic_material.key),
        transform=transform,
        completeness=COMPLETENESS,
    )
    request = PublicationRequest(
        identity + ":publication",
        identity + ":occurrence",
        version.key,
        RULE,
        Decision.PUBLISH,
        Scope(),
        version.materials,
        frozenset({Grade.ORIGINAL, Grade.PROJECTED}),
        "bounded-offline-cpi-handoff",
        required_completeness=COMPLETENESS,
    )
    return CpiHandoffPlan(
        receipt, run, raw_material, semantic_material, version, request, content
    )


def _check_plan(plan: CpiHandoffPlan) -> None:
    expected = build_handoff_plan(
        plan.receipt,
        identity=plan.run.identity.removesuffix(":run"),
        started_at=plan.run.started_at,
        finished_at=plan.run.finished_at,
        frozen_at=plan.version.frozen_at,
    )
    require(plan == expected, "cpi_handoff_plan_modified")


def persist_handoff(kernel: CanonicalKernel, plan: CpiHandoffPlan) -> DatasetVersion:
    _check_plan(plan)
    require(kernel.clock() >= plan.version.frozen_at, "cpi_mdf_freeze_in_future")
    for record in (
        BLS_CPI_HEADLINES,
        plan.run,
        plan.raw_material,
        plan.semantic_material,
    ):
        kernel.add(record)
    return kernel.persist_version(plan.version, plan.content)


def publish_handoff(kernel: CanonicalKernel, plan: CpiHandoffPlan) -> Publication:
    _check_plan(plan)
    # The bounded member references are backed by this exact canonical carrier.
    # Verify them here rather than extending the shared storage/grade framework.
    for record in (plan.run, plan.raw_material, plan.semantic_material, plan.version):
        require(kernel.ledger.get(record.key) == record, "cpi_stored_plan_mismatch")
    content = kernel.ledger.get_content(plan.version.key)
    require(content == plan.content, "cpi_stored_content_mismatch")
    require(
        readback_materials(content)
        == CpiCanonicalMaterials(plan.receipt.retained_bytes, plan.receipt.raw_bytes),
        "cpi_stored_materials_mismatch",
    )

    def processing_clock():
        now = kernel.clock()
        instant(now)
        require(now >= plan.version.frozen_at, "cpi_publication_backdated")
        return now

    # Use the accepted kernel; PostgreSQL still owns its server-timed commit.
    # Guard its caller clock too, before entering that publication transaction.
    processing_clock()
    return CanonicalKernel(kernel.ledger, processing_clock).publish(
        plan.publication_request
    )


def readback_materials(content: bytes) -> CpiCanonicalMaterials:
    """Extract exact canonical members and revalidate their pinned semantics."""
    payload = _json(content)
    try:

        def extract(section, member):
            return base64.b64decode(payload[section][member], validate=True)

        retained_bytes = extract("materials", "retained.json")
        raw_bytes = extract("materials", "original.bin")
        retained = _json(retained_bytes)
        require(
            set(retained)
            == {
                "api_contract",
                "product_revision",
                "capability_id",
                "retained",
                "retained_semantics",
            }
            and retained["api_contract"] == API_CONTRACT
            and retained["product_revision"] == PRODUCT_REVISION
            and retained["capability_id"] == CAPABILITY
            and retained["retained_semantics"]
            == "application_retained_semantic_revision_not_provider_vintage",
            "cpi_stored_semantics_mismatch",
        )
        record = retained["retained"]
        require(
            set(payload)
            == {
                "schema",
                "mdd_lineage",
                "admitted_record",
                "materials",
                "completeness",
                "provider_freshness",
            }
            and set(payload["materials"]) == {"original.bin", "retained.json"}
            and type(record) is dict
            and digest(_encode(record)) == SEMANTIC_RECORD_SHA256
            and len(raw_bytes) == RAW_SIZE
            and digest(raw_bytes) == RAW_SHA256,
            "cpi_content_fields_not_admitted",
        )
        require(
            payload["schema"] == RULE
            and payload["admitted_record"] == record
            and payload["mdd_lineage"] == _semantic_lineage()
            and payload["completeness"] == COMPLETENESS
            and payload["provider_freshness"] == "not_asserted",
            "cpi_stored_semantics_mismatch",
        )
        return CpiCanonicalMaterials(retained_bytes, raw_bytes)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("cpi_content_members_invalid") from None
