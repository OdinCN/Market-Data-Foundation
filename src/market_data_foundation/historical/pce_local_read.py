"""PCE local exact/as-of reads. No provider, job or storage-write dependency."""

import base64
from datetime import datetime

from market_data_foundation.canonical.consumer_read import (
    ExactReadView,
    ReadContractError,
    ReadFailure,
    _read_exact_version_admitted,
)
from market_data_foundation.canonical.model import Key, Kind, digest, instant
from market_data_foundation.canonical.repository import Ledger
from market_data_foundation.historical.bea_pce import (
    MAX_NORMALIZED_BYTES,
    PCE_BINDING_CANDIDATE,
    PCE_DEFINITION,
    PCE_SOURCE,
    REFERENCE,
    PceRetainedEvidence,
    build_plan,
    decode,
    durable_plan,
)
from market_data_foundation.historical.pce_material_store import PceMaterialIdentity


class PceReadRejected(Exception):
    def __init__(self, classification: str):
        self.classification = classification
        super().__init__(classification)


def _validate(ledger: Ledger, view: ExactReadView) -> ExactReadView:
    try:
        document = decode(view.content, max_bytes=MAX_NORMALIZED_BYTES)
        carrier = document["raw"]
        raw = base64.b64decode(carrier["base64"], validate=True)
        identity = PceMaterialIdentity(digest(raw), len(raw))
        version = view.version
        if (
            carrier["sha256"] != identity.sha256
            or carrier["size"] != identity.size
            or len(version.acquisitions) != 1
            or view.definition != PCE_DEFINITION
            or view.source != PCE_SOURCE
            or view.binding != PCE_BINDING_CANDIDATE
        ):
            raise ValueError
        run = ledger.get(version.run)
        acquisition = ledger.get(version.acquisitions[0])
        expected = durable_plan(
            build_plan(
                PceRetainedEvidence(
                    raw, acquisition.identity, acquisition.external_receipt
                ),
                identity=run.identity.removesuffix(":run"),
                finished_at=run.finished_at,
                frozen_at=version.frozen_at,
            ),
            identity.locator,
        )
        if (
            run != expected.run
            or acquisition != expected.acquisition
            or version != expected.version
            or view.content != expected.content
            or view.publication.request != expected.publication_request
            or view.publication.decision_at < version.frozen_at
            or view.publication.usable_at < view.publication.decision_at
            or tuple(ledger.get(k) for k in version.materials)
            != (expected.raw_material, expected.semantic_material)
        ):
            raise ValueError
    except Exception:
        raise PceReadRejected("pce_payload_or_lineage_mismatched") from None
    return view


def read_local_pce(
    ledger: Ledger,
    *,
    reference: str,
    as_of: datetime,
    version_identity: str | None = None,
) -> ExactReadView:
    """One bounded reference; latest available MDF decision, never provider vintage.

    Every eligible graph is validated before selection. Equal decision times are
    ambiguous; a missing/unavailable source is never filled from a different one.
    """
    if reference != REFERENCE:
        raise PceReadRejected("pce_reference_not_admitted")
    instant(as_of)

    def exact(identity):
        return _validate(
            ledger,
            _read_exact_version_admitted(
                ledger,
                PCE_DEFINITION.key,
                Key(Kind.VERSION, identity),
                as_of=as_of,
                admitted=frozenset({PCE_DEFINITION.key}),
            ),
        )

    if version_identity is not None:
        return exact(version_identity)
    available = []
    try:
        versions = ledger.versions_for_definition(PCE_DEFINITION.key)
    except Exception:
        raise PceReadRejected("pce_storage_unavailable") from None
    for version in versions:
        try:
            available.append(exact(version.identity))
        except ReadContractError as exc:
            if exc.classification not in {
                ReadFailure.UNPUBLISHED_OR_UNAVAILABLE_AS_OF,
                ReadFailure.VERSION_WITHDRAWN,
                ReadFailure.VERSION_SUPERSEDED,
            }:
                raise
    if not available:
        raise PceReadRejected("pce_as_of_unavailable")
    latest = max(v.publication.decision_at for v in available)
    selected = [v for v in available if v.publication.decision_at == latest]
    if len(selected) != 1:
        raise PceReadRejected("pce_as_of_ambiguous")
    return selected[0]
