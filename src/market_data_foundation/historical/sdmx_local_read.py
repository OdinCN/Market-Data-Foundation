"""T08 no-network whole-version Read, reconstructing each retained entity."""

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
    instant,
    require,
)
from market_data_foundation.canonical.repository import Ledger
from market_data_foundation.historical.sdmx_contract import definition_for
from market_data_foundation.historical.sdmx_official import (
    build_plan,
    candidate_from_content,
    validate_window,
)


class SdmxReadRejected(Exception):
    def __init__(self, classification="sdmx_payload_or_lineage_mismatch"):
        self.classification = classification
        super().__init__(classification)


def _validate(ledger, view, provider, start, end):
    try:
        candidate = candidate_from_content(view.content)
        require(
            (candidate.provider, candidate.start, candidate.end)
            == (provider, start, end),
            "sdmx_read_scope_mismatch",
        )
        run = ledger.get(view.version.run)
        expected = build_plan(
            candidate,
            identity=run.identity.removesuffix(":run"),
            finished_at=run.finished_at,
            frozen_at=view.version.frozen_at,
        )
        materials = tuple(ledger.get(k) for k in view.version.materials)
        if any(
            getattr(m, "durable", False)
            for m in materials[: len(expected.raw_materials)]
        ):
            from .sdmx_materialization import durable_plan

            expected = durable_plan(expected)
        require(
            view.definition == definition_for(provider)
            and view.source == expected.source
            and view.binding == expected.binding_candidate
            and run == expected.run
            and view.version == expected.version
            and view.content == expected.content
            and tuple(ledger.get(k) for k in view.version.acquisitions)
            == expected.acquisitions
            and tuple(ledger.get(k) for k in view.version.materials)
            == (*expected.raw_materials, expected.semantic_material),
            "sdmx_exact_graph_mismatch",
        )
        # RESTORE/WITHDRAW/SUPERSEDE retain the kernel lifecycle; verify the
        # original PUBLISH separately rather than comparing a RESTORE to it.
        history = ledger.publications_for_version(view.version.key)
        require(
            history[0].request == expected.publication_request,
            "sdmx_initial_publication_mismatch",
        )
        require(
            all(
                p.decision_at >= expected.version.frozen_at
                and p.usable_at >= p.decision_at
                for p in history
            ),
            "sdmx_publication_time_inversion",
        )
    except Exception:
        raise SdmxReadRejected() from None


def read_local_sdmx(
    ledger: Ledger,
    *,
    provider: str,
    start: str,
    end: str,
    as_of: datetime,
    version_identity: str | None = None,
) -> ExactReadView:
    definition = definition_for(provider)
    validate_window(start, end)
    instant(as_of)
    if version_identity is not None:
        view = _read_exact_version_admitted(
            ledger,
            definition.key,
            Key(Kind.VERSION, version_identity),
            as_of=as_of,
            admitted=frozenset({definition.key}),
        )
        _validate(ledger, view, provider, start, end)
        return view
    versions = ledger.versions_for_definition(definition.key)
    require(
        type(versions) is tuple
        and all(
            isinstance(v, DatasetVersion) and v.definition == definition.key
            for v in versions
        ),
        "sdmx_inventory_invalid",
    )
    views = []
    for version in versions:
        try:
            view = _read_exact_version_admitted(
                ledger,
                definition.key,
                version.key,
                as_of=as_of,
                admitted=frozenset({definition.key}),
            )
        except ReadContractError as exc:
            if exc.classification not in {
                ReadFailure.UNPUBLISHED_OR_UNAVAILABLE_AS_OF,
                ReadFailure.VERSION_WITHDRAWN,
                ReadFailure.VERSION_SUPERSEDED,
            }:
                raise
            continue
        # Validate even a different window before allowing it to be skipped.
        try:
            candidate = candidate_from_content(view.content)
        except Exception:
            raise SdmxReadRejected() from None
        _validate(ledger, view, provider, candidate.start, candidate.end)
        if (candidate.start, candidate.end) == (start, end):
            views.append(view)
    if not views:
        raise ReadContractError(ReadFailure.AS_OF_NO_VERSION)
    newest = max(max(a.obtained_at for a in view.acquisitions) for view in views)
    selected = [
        v for v in views if max(a.obtained_at for a in v.acquisitions) == newest
    ]
    if len(selected) != 1:
        raise ReadContractError(ReadFailure.AS_OF_AMBIGUOUS)
    return selected[0]
