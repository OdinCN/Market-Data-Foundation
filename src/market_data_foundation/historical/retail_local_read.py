"""No-source-on-read exact/as-of guard for the bounded Retail vertical."""

import base64
import binascii
import json
from datetime import datetime

from market_data_foundation.canonical.consumer_read import (
    ExactReadView,
    ReadContractError,
    ReadFailure,
    _read_exact_version_admitted,
)
from market_data_foundation.canonical.model import (
    AcquisitionOccurrence,
    ContractError,
    DataRun,
    DatasetVersion,
    EvidenceMaterial,
    Grade,
    Key,
    Kind,
    digest,
    instant,
)
from market_data_foundation.canonical.repository import Ledger
from market_data_foundation.historical.bls_cpi_material_store import MaterialIdentity
from market_data_foundation.historical.census_retail import (
    PARSER_REVISION,
    REFERENCE,
    RELEASE_METADATA,
    RETAIL_BINDING_CANDIDATE,
    RETAIL_SOURCE,
    RULE,
    CensusRetailRequest,
    RetailEvidence,
    build_live_plan,
    durable_plan,
    finite_members,
    parse_response,
    validate_publication_time,
)
from market_data_foundation.historical.retail_contract import CENSUS_RETAIL
from market_data_foundation.source_request_policy import valid_transport_attempts


class RetailReadRejected(Exception):
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
            "requested_at",
            "acquisition_identity",
            "external_receipt",
            "provider_revision",
            "provider_vintage",
            "release",
            "parser_revision",
            "data",
            "raw",
        }:
            raise ValueError
        request = CensusRetailRequest()
        if (
            document["schema"] != RULE
            or document["provider"] != "Census"
            or document["request"] != request.persisted()
            or document["request_identity"] != request.identity
            or document["provider_revision"] != {"state": "not_declared", "value": None}
            or document["provider_vintage"] is not None
            or document["release"] != RELEASE_METADATA
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
        ):
            raise ValueError
        return raw_bytes
    except (
        ContractError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        UnicodeError,
        binascii.Error,
    ):
        raise RetailReadRejected("retail_payload_invalid_or_mismatched") from None


def _validate_lineage(ledger: Ledger, view: ExactReadView, raw: bytes) -> None:
    """Require the exact source-specific graph reconstructed from embedded raw."""
    try:
        version = view.version
        if (
            view.definition != CENSUS_RETAIL
            or view.source != RETAIL_SOURCE
            or view.binding != RETAIL_BINDING_CANDIDATE
            or version.binding != RETAIL_BINDING_CANDIDATE.key
            or version.mapping != "census-retail"
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
            or not valid_transport_attempts(acquisition.provider_data_attempts)
        ):
            raise ValueError
        validate_publication_time(
            view.publication, acquisition.obtained_at, version.frozen_at
        )
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
                RetailEvidence(
                    raw,
                    CensusRetailRequest(),
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
    except RetailReadRejected:
        raise
    except Exception:
        raise RetailReadRejected("retail_lineage_invalid_or_mismatched") from None


def read_local_retail(
    ledger: Ledger,
    *,
    reference: str,
    as_of: datetime,
    version_identity: str | None = None,
) -> ExactReadView:
    """Read only the exact local canonical graph; never create clients or jobs."""
    if reference != REFERENCE:
        raise RetailReadRejected("reference_not_admitted")
    if version_identity is None:
        instant(as_of)
        versions = ledger.versions_for_definition(CENSUS_RETAIL.key)
        if type(versions) is not tuple or any(
            not isinstance(version, DatasetVersion)
            or version.definition != CENSUS_RETAIL.key
            for version in versions
        ):
            raise RetailReadRejected("retail_version_inventory_invalid")
        views = []
        for version in versions:
            try:
                views.append(
                    read_local_retail(
                        ledger,
                        reference=reference,
                        as_of=as_of,
                        version_identity=version.identity,
                    )
                )
            except ReadContractError as exc:
                if exc.classification not in {
                    ReadFailure.UNPUBLISHED_OR_UNAVAILABLE_AS_OF,
                    ReadFailure.VERSION_WITHDRAWN,
                    ReadFailure.VERSION_SUPERSEDED,
                }:
                    raise
        if not views:
            raise ReadContractError(ReadFailure.AS_OF_NO_VERSION)
        newest = max(view.acquisitions[0].obtained_at for view in views)
        selected = [
            view for view in views if view.acquisitions[0].obtained_at == newest
        ]
        if len(selected) != 1:
            raise ReadContractError(ReadFailure.AS_OF_AMBIGUOUS)
        return selected[0]
    else:
        view = _read_exact_version_admitted(
            ledger,
            CENSUS_RETAIL.key,
            Key(Kind.VERSION, version_identity),
            as_of=as_of,
            admitted=frozenset({CENSUS_RETAIL.key}),
        )
    expected = set(finite_members())
    if set(view.version.finite_scope.members) != expected:
        raise RetailReadRejected("coverage_partial_or_mismatched")
    if view.publication.request.claimed_grades != frozenset(
        {Grade.ORIGINAL, Grade.PROJECTED}
    ):
        raise RetailReadRejected("quality_not_admitted")
    raw = _validate_content(view.content)
    _validate_lineage(ledger, view, raw)
    return view
