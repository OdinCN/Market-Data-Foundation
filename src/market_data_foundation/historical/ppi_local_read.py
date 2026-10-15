"""No-source-on-read exact/as-of guard for the bounded PPI vertical."""

import base64
import binascii
import json
from datetime import datetime

from market_data_foundation.canonical.consumer_read import (
    ExactReadView,
    _read_as_of_admitted,
    _read_exact_version_admitted,
)
from market_data_foundation.canonical.model import (
    AcquisitionOccurrence,
    DataRun,
    EvidenceMaterial,
    Grade,
    Key,
    Kind,
    digest,
    instant,
)
from market_data_foundation.canonical.repository import Ledger
from market_data_foundation.domain_contracts.macro import BLS_PPI_FINAL_DEMAND
from market_data_foundation.historical.bls_cpi_material_store import MaterialIdentity
from market_data_foundation.historical.bls_ppi_final_demand import (
    CALENDAR_SOURCE,
    MEASUREMENTS,
    PARSER_REVISION,
    PPI_BINDING_CANDIDATE,
    PPI_SOURCE,
    REFERENCE,
    RELEASE,
    RELEASE_SLOT,
    RULE,
    BlsPpiRequest,
    PpiLiveEvidence,
    build_live_plan,
    durable_plan,
    parse_response,
)


class PpiReadRejected(Exception):
    def __init__(self, classification: str):
        self.classification = classification
        super().__init__(classification)


def _validate_content(content: bytes) -> bytes:
    try:
        document = json.loads(content)
        if type(document) is not dict or set(document) != {
            "schema",
            "provider",
            "request",
            "request_identity",
            "obtained_at",
            "provider_revision",
            "provider_vintage",
            "release",
            "parser_revision",
            "data",
            "raw",
        }:
            raise ValueError
        request = BlsPpiRequest()
        if (
            document["schema"] != RULE
            or document["provider"] != "BLS"
            or document["request"] != request.persisted()
            or document["request_identity"] != request.identity
            or document["provider_revision"] != {"state": "not_declared", "value": None}
            or document["provider_vintage"] is not None
            or document["release"]
            != {
                "reference": REFERENCE,
                "scheduled_at": RELEASE.isoformat(),
                "slot_identity": RELEASE_SLOT,
                "calendar_source": CALENDAR_SOURCE,
            }
            or document["parser_revision"] != PARSER_REVISION
        ):
            raise ValueError
        obtained_at = datetime.fromisoformat(document["obtained_at"])
        instant(obtained_at)
        raw = document["raw"]
        if (
            type(raw) is not dict
            or set(raw) != {"sha256", "size", "base64"}
            or type(raw["sha256"]) is not str
            or type(raw["size"]) is not int
            or type(raw["base64"]) is not str
        ):
            raise ValueError
        raw_bytes = base64.b64decode(raw["base64"], validate=True)
        if len(raw_bytes) != raw["size"] or digest(raw_bytes) != raw["sha256"]:
            raise ValueError
        projection = parse_response(raw_bytes)
        if (
            projection.raw_sha256 != raw["sha256"]
            or projection.raw_size != raw["size"]
            or document["data"] != projection.data
            or set(document["data"]["measurements"]) != set(MEASUREMENTS)
        ):
            raise ValueError
        return raw_bytes
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        UnicodeError,
        binascii.Error,
    ):
        raise PpiReadRejected("ppi_payload_invalid_or_mismatched") from None


def _validate_lineage(ledger: Ledger, view: ExactReadView, raw: bytes) -> None:
    """Require the exact source-specific graph reconstructed from embedded raw."""
    try:
        version = view.version
        if (
            view.definition != BLS_PPI_FINAL_DEMAND
            or view.source != PPI_SOURCE
            or view.binding != PPI_BINDING_CANDIDATE
            or version.binding != PPI_BINDING_CANDIDATE.key
            or version.mapping != "bls-ppi-final-demand"
            or len(version.acquisitions) != 1
            or len(version.materials) != 2
            or version.quality_evidence
        ):
            raise ValueError
        run = ledger.get(version.run)
        acquisition = ledger.get(version.acquisitions[0])
        materials = tuple(ledger.get(key) for key in version.materials)
        if (
            not isinstance(run, DataRun)
            or not isinstance(acquisition, AcquisitionOccurrence)
            or not all(isinstance(item, EvidenceMaterial) for item in materials)
            or run.finished_at is None
            or acquisition.obtained_at is None
            or type(acquisition.external_receipt) is not str
            or not acquisition.external_receipt
            or acquisition.provider_data_attempts != 1
        ):
            raise ValueError
        raw_materials = tuple(
            item for item in materials if item.grade == Grade.ORIGINAL
        )
        semantic_materials = tuple(
            item for item in materials if item.grade == Grade.PROJECTED
        )
        if len(raw_materials) != 1 or len(semantic_materials) != 1:
            raise ValueError
        raw_identity = MaterialIdentity(digest(raw), len(raw))
        expected = durable_plan(
            build_live_plan(
                PpiLiveEvidence(
                    raw,
                    BlsPpiRequest(),
                    acquisition.requested_at,
                    acquisition.obtained_at,
                    acquisition.identity,
                    acquisition.external_receipt,
                    acquisition.provider_data_attempts,
                ),
                identity=run.identity.removesuffix(":run"),
                finished_at=run.finished_at,
                frozen_at=version.frozen_at,
            ),
            raw_identity.locator,
        )
        if (
            expected.run.identity != run.identity
            or run != expected.run
            or acquisition != expected.acquisition
            or materials != (expected.raw_material, expected.semantic_material)
            or raw_materials[0].digest != raw_identity.sha256
            or raw_materials[0].controlled_ref != raw_identity.locator
            or semantic_materials[0].digest != version.content_digest
            or version != expected.version
            or view.content != expected.content
            or view.publication.request != expected.publication_request
            or tuple(item.identity for item in view.acquisitions)
            != (expected.acquisition.identity,)
            or tuple(item.identity for item in view.materials)
            != (
                expected.raw_material.identity,
                expected.semantic_material.identity,
            )
        ):
            raise ValueError
    except PpiReadRejected:
        raise
    except Exception:
        raise PpiReadRejected("ppi_lineage_invalid_or_mismatched") from None


def read_local_ppi(
    ledger: Ledger,
    *,
    reference: str,
    as_of: datetime,
    version_identity: str | None = None,
) -> ExactReadView:
    """Read only the exact local canonical graph; never create clients or jobs."""
    if reference != REFERENCE:
        raise PpiReadRejected("reference_not_admitted")
    if version_identity is None:
        view = _read_as_of_admitted(
            ledger,
            BLS_PPI_FINAL_DEMAND.key,
            as_of=as_of,
            admitted=frozenset({BLS_PPI_FINAL_DEMAND.key}),
        )
    else:
        view = _read_exact_version_admitted(
            ledger,
            BLS_PPI_FINAL_DEMAND.key,
            Key(Kind.VERSION, version_identity),
            as_of=as_of,
            admitted=frozenset({BLS_PPI_FINAL_DEMAND.key}),
        )
    expected = {f"{REFERENCE}:bls:ppi:{name}" for name in MEASUREMENTS}
    if set(view.version.finite_scope.members) != expected:
        raise PpiReadRejected("coverage_partial_or_mismatched")
    if view.publication.request.claimed_grades != frozenset(
        {Grade.ORIGINAL, Grade.PROJECTED}
    ):
        raise PpiReadRejected("quality_not_admitted")
    raw = _validate_content(view.content)
    _validate_lineage(ledger, view, raw)
    return view
