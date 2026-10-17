"""No Source or storage-write dependency: rebuild all four raws on each Read."""

from datetime import datetime

from market_data_foundation.canonical.consumer_read import (
    ExactReadView,
    ReadContractError,
    ReadFailure,
    _read_exact_version_admitted,
)
from market_data_foundation.canonical.model import (
    DatasetVersion,
    Key,
    Kind,
    digest,
    instant,
    require,
)
from market_data_foundation.canonical.repository import Ledger
from market_data_foundation.historical.bls_cpi_material_store import MaterialIdentity
from market_data_foundation.historical.fred_alfred_vintage import (
    REFERENCE,
    build_live_plan,
    candidate_from_content,
    durable_plan,
    validate_publication_time,
)
from market_data_foundation.historical.vintage_contract import VINTAGE_BUNDLE


class VintageReadRejected(Exception):
    def __init__(self, classification: str):
        self.classification = classification
        super().__init__(classification)


def _validate_lineage(ledger: Ledger, view: ExactReadView) -> None:
    try:
        candidate = candidate_from_content(view.content)
        run = ledger.get(view.version.run)
        expected = build_live_plan(
            candidate,
            identity=run.identity.removesuffix(":run"),
            finished_at=run.finished_at,
            frozen_at=view.version.frozen_at,
        )
        expected = durable_plan(
            expected,
            tuple(
                MaterialIdentity(digest(r.raw), len(r.raw)).locator
                for r in candidate.reads
            ),
        )
        require(
            view.definition == VINTAGE_BUNDLE
            and view.source == expected.source
            and view.binding == expected.binding_candidate
            and run == expected.run
            and view.version == expected.version
            and view.content == expected.content
            and view.publication.request == expected.publication_request
            and tuple(ledger.get(key) for key in view.version.acquisitions)
            == expected.acquisitions
            and tuple(ledger.get(key) for key in view.version.materials)
            == (*expected.raw_materials, expected.semantic_material),
            "vintage_lineage_mismatch",
        )
        validate_publication_time(
            view.publication,
            max(a.obtained_at for a in expected.acquisitions),
            view.version.frozen_at,
        )
    except Exception:
        raise VintageReadRejected("vintage_payload_or_lineage_mismatched") from None


def read_local_vintage(
    ledger: Ledger,
    *,
    as_of: datetime,
    version_identity: str | None = None,
    reference: str = REFERENCE,
) -> ExactReadView:
    if reference != REFERENCE:
        raise VintageReadRejected("reference_not_admitted")
    instant(as_of)
    if version_identity is not None:
        view = _read_exact_version_admitted(
            ledger,
            VINTAGE_BUNDLE.key,
            Key(Kind.VERSION, version_identity),
            as_of=as_of,
            admitted=frozenset({VINTAGE_BUNDLE.key}),
        )
        _validate_lineage(ledger, view)
        return view
    versions = ledger.versions_for_definition(VINTAGE_BUNDLE.key)
    require(
        type(versions) is tuple
        and all(
            isinstance(v, DatasetVersion) and v.definition == VINTAGE_BUNDLE.key
            for v in versions
        ),
        "vintage_inventory_invalid",
    )
    views = []
    for version in versions:
        try:
            views.append(
                read_local_vintage(
                    ledger, as_of=as_of, version_identity=version.identity
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
    # Latest completed acquisition wins; equal completion instants are ambiguous.
    newest = max(max(a.obtained_at for a in view.acquisitions) for view in views)
    selected = [
        view
        for view in views
        if max(a.obtained_at for a in view.acquisitions) == newest
    ]
    if len(selected) != 1:
        raise ReadContractError(ReadFailure.AS_OF_AMBIGUOUS)
    return selected[0]
