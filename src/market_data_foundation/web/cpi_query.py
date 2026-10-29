"""Ordinary CPI query: read-only local scope, then one explicitly admitted source.

No transport, secret loader, journal or mutable ledger is constructed here.
The provider port accepts one immutable, exact-reference request and returns the
existing normalized content envelope. Deployment assembly remains opt-in.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import AwareDatetime, Field, field_validator

from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    read_as_of,
)
from market_data_foundation.canonical.model import (
    DatasetVersion,
    Grade,
    SourceBindingRevision,
    SourceOperationRef,
)
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical import cpi_history
from market_data_foundation.historical.bls_cpi_direct import (
    BLS_CPI_DIRECT_SOURCE,
    BLS_CPI_SEPTEMBER_RELEASE_SOURCE_CANDIDATE,
)
from market_data_foundation.historical.cpi_local_read import read_local_cpi
from market_data_foundation.historical.cpi_retained_handoff import (
    AUGUST_REFERENCE_SEMANTICS,
    MEASURES,
    SEPTEMBER_REFERENCE_SEMANTICS,
    validate_cpi_projection,
)
from market_data_foundation.web.cpi_api import MeasurementDTO, ScopeFactory
from market_data_foundation.web.dto import DTO
from market_data_foundation.web.package_catalog import CredentialState, PackageError
from market_data_foundation.web.packages import PackageHost
from market_data_foundation.web.source_catalog import (
    ContractKeyDTO,
    DirectoryQuery,
    directory,
)

Permission = Literal["allowed", "denied", "unknown"]


class CpiQuery(DTO):
    reference: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$", max_length=7)
    operation_id: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator("reference")
    @classmethod
    def admitted_month(cls, value):
        cpi_history.month_number(value)
        return value


class CpiQueryFailure(DTO):
    status: Literal["UNKNOWN", "NOT_AVAILABLE", "REJECTED"]
    classification: str = Field(pattern=r"^[a-z_]{1,80}$")
    local_reason: str | None = None


class CpiQueryResult(DTO):
    status: Literal["OK"] = "OK"
    mode: Literal["ordinary"] = "ordinary"
    definition_ref: ContractKeyDTO
    reference: str
    decision_at: AwareDatetime
    channel: Literal["local", "upstream"]
    durable: bool
    local_reason: str | None = None
    operation_id: str | None = None
    source_ref: ContractKeyDTO | None = None
    binding_ref: ContractKeyDTO | None = None
    version_identity: str | None = None
    publication_identity: str | None = None
    obtained_at: AwareDatetime | None = None
    provider_vintage: Literal["UNKNOWN"] = "UNKNOWN"
    evidence_grades: list[str]
    measurements: list[MeasurementDTO] = Field(min_length=4, max_length=4)


class CpiQueryAccess(DTO):
    metadata_permission: Permission = "unknown"
    read_permission: Permission = "unknown"


class CpiSourceAdmission(DTO):
    """Current Host attestation, independent of code contracts/callback presence.

    All permissions and qualification default to unknown. References name exact
    allowed requests; they never expand the request to a range or another month.
    No secret value or generic write capability belongs to this port.
    """

    qualified: bool | None = None
    credential_state: Literal["configured", "missing", "unknown"] = "unknown"
    source_permission: Permission = "unknown"
    query_permission: Permission = "unknown"
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class CpiSourceRequest:
    reference: str
    decision_at: datetime
    operation_id: str
    source: SourceOperationRef
    binding: SourceBindingRevision
    members: tuple[str, ...]


@dataclass(frozen=True)
class CpiSourceResponse:
    # Existing normalized envelope, including the actual obtained_at. Bytes are
    # validated against the selected source's existing semantic contract.
    content: bytes
    evidence_grades: frozenset[Grade]


@dataclass(frozen=True)
class CpiQueryBinding:
    source: SourceOperationRef
    binding: SourceBindingRevision
    admission: CpiSourceAdmission
    fetch: Callable[[CpiSourceRequest], CpiSourceResponse]

    @property
    def operation_id(self):
        return f"{self.source.identity}@{self.source.contract_revision}"


@dataclass(frozen=True)
class CpiQueryHost:
    read_scope: ScopeFactory
    authorize: Callable[[Request], CpiQueryAccess]
    packages: PackageHost
    bindings: tuple[CpiQueryBinding, ...] = ()
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    # Reference matching alone satisfies the existing freshness contract. An
    # optional stricter Host policy must return an explicit result; unknown fails.
    freshness: Callable[
        [object, datetime], Literal["satisfied", "stale", "unknown"]
    ] = lambda _view, _at: "satisfied"


class QueryRejected(Exception):
    def __init__(self, classification, code=409, status="NOT_AVAILABLE"):
        self.classification, self.code, self.status = classification, code, status


def _key(record):
    return ContractKeyDTO(identity=record.key.identity, revision=record.key.revision)


def _fail(classification, code=409, status="NOT_AVAILABLE"):
    raise QueryRejected(classification, code, status)


def _permission(value, name):
    if value != "allowed":
        _fail(name + "_" + value, 403, "REJECTED")


class _ReferenceScope:
    """Only canonical read methods, filtering the as-of selector by reference."""

    def __init__(self, scope, versions):
        self.get = scope.get
        self.get_content = scope.get_content
        self.publications_for_version = scope.publications_for_version
        self.versions_for_definition = lambda _definition: versions


def _content(content, reference, *, source=None):
    document = json.loads(content)
    history_source, _ = cpi_history.source_binding()
    if source == history_source.key or (
        source is None and document.get("schema") == cpi_history.RULE
    ):
        cpi_history.validate_content(document, reference)
    else:
        if source == history_source.key or document.get("schema") == cpi_history.RULE:
            _fail("source_payload_invalid")
        semantics = {
            "2026-08": AUGUST_REFERENCE_SEMANTICS,
            "2026-09": SEPTEMBER_REFERENCE_SEMANTICS,
        }[reference]
        validate_cpi_projection(document["data"], reference=semantics)
    return document


def _measurements(document):
    return [
        MeasurementDTO(**{key: item[key] for key in MeasurementDTO.model_fields})
        for item in document["data"]["measurements"]
    ]


def _local(host, query, at):
    # Nothing may leave this scope until __exit__ has succeeded. In particular,
    # a failing close/rollback cannot become a miss or permit a source call.
    with host.read_scope() as scope:
        versions = scope.versions_for_definition(BLS_CPI_HEADLINES.key)
        if (
            not isinstance(versions, tuple)
            or len(versions) > 128
            or any(
                not isinstance(v, DatasetVersion)
                or v.definition != BLS_CPI_HEADLINES.key
                for v in versions
            )
            or tuple(sorted(versions, key=lambda v: v.identity)) != versions
        ):
            _fail("storage_unavailable", 503, "UNKNOWN")
        candidates = []
        expected = {f"{query.reference}:bls:cpi:{name}" for name in MEASURES}
        for version in versions:
            members = set(version.finite_scope.members)
            if not members:
                _fail("coverage_invalid")
            # Unknown/mixed scope is a contract failure, never absence evidence.
            refs = {m.split(":", 1)[0] for m in members}
            if len(refs) != 1:
                _fail("coverage_invalid")
            reference = next(iter(refs))
            cpi_history.month_number(reference)
            if not members <= {f"{reference}:bls:cpi:{name}" for name in MEASURES}:
                _fail("coverage_invalid")
            if reference == query.reference:
                candidates.append(version)
        selected = _ReferenceScope(scope, tuple(candidates))
        try:
            view = read_as_of(selected, BLS_CPI_HEADLINES.key, as_of=at)
        except ReadContractError as error:
            if not candidates and error.classification == "as_of_no_version":
                result = (None, "local_reference_missing")
            else:
                raise
        else:
            # Canonical exact validation already checked content, lineage and
            # effective publication even for a partial candidate.
            try:
                document = _content(view.content, query.reference)
            except Exception:
                _fail("cpi_payload_invalid_or_mismatched")
            if (
                not {Grade.ORIGINAL, Grade.PROJECTED}
                <= view.publication.request.claimed_grades
            ):
                _fail("quality_not_admitted")
            members = set(view.version.finite_scope.members)
            if members < expected:
                result = (None, "local_coverage_partial")
            else:
                # Reuse the unchanged CPI guard over the exact selected version.
                view = read_local_cpi(
                    scope,
                    reference=query.reference,
                    as_of=at,
                    version_identity=view.version.identity,
                )
                fresh = host.freshness(view, at)
                if fresh == "stale":
                    result = (None, "local_freshness_unsatisfied")
                elif fresh != "satisfied":
                    _fail("local_freshness_unknown", 503, "UNKNOWN")
                else:
                    result = (
                        CpiQueryResult(
                            definition_ref=_key(view.definition),
                            reference=query.reference,
                            decision_at=at,
                            channel="local",
                            durable=True,
                            source_ref=_key(view.source) if view.source else None,
                            binding_ref=_key(view.binding) if view.binding else None,
                            operation_id=(
                                f"{view.source.identity}@{view.source.contract_revision}"
                                if view.source
                                else None
                            ),
                            version_identity=view.version.identity,
                            publication_identity=view.publication.key.identity,
                            obtained_at=(
                                datetime.fromisoformat(document["obtained_at"])
                                if document.get("obtained_at")
                                else None
                            ),
                            evidence_grades=sorted(
                                g.value for g in view.publication.request.claimed_grades
                            ),
                            measurements=_measurements(document),
                        ),
                        None,
                    )
    return result


def _source_entries():
    return {
        entry.identity: entry
        for entry in directory(DirectoryQuery()).entries
        if entry.web_read == "CPI_REQUEST_QUALIFIED"
        and entry.asset == _key(BLS_CPI_HEADLINES)
        and entry.source_contract is not None
        and entry.binding_contract is not None
    }


def _current_credential(host, record, source_name):
    try:
        states = host.packages.credential_states(
            "macro", record, source_ids={source_name}
        )
        if not isinstance(states, (list, tuple)) or not all(
            isinstance(state, CredentialState) for state in states
        ):
            raise TypeError()
        named = {state.source_id: state for state in states}
        if len(named) != len(states):
            raise ValueError()
        state = named.get(source_name)
    except Exception:
        _fail("credential_state_unavailable", 503, "UNKNOWN")
    current = state.credential_state if state is not None else "unknown"
    if current != "configured":
        _fail("credential_" + current)


def _upstream(host, query, at, reason, request):
    try:
        host.packages.access(request, "macro")
        record = host.packages.store.read(host.packages.environment_id, "macro")
    except PackageError as error:
        _fail(
            error.failure.classification,
            error.http_status,
            "REJECTED" if error.failure.status == "REJECTED" else "UNKNOWN",
        )
    except Exception:
        _fail("source_configuration_unavailable", 503, "UNKNOWN")
    if record is None or "cpi" not in record.enabled_capability_ids:
        _fail("source_disabled")
    # Use precisely the real BLS CPI catalog rows, including source and binding
    # revisions. Directory evidence does not establish current qualification.
    entries = _source_entries()
    if not host.bindings:
        _fail("source_binding_unconfigured", 503, "UNKNOWN")
    if query.operation_id is None:
        _fail("source_selection_required")
    entry = entries.get(query.operation_id)
    if entry is None:
        _fail("source_binding_not_admitted")
    matches = [b for b in host.bindings if b.operation_id == query.operation_id]
    if len(matches) != 1:
        _fail("source_binding_unconfigured", 503, "UNKNOWN")
    selected = matches[0]
    if (
        _key(selected.source)
        != ContractKeyDTO(
            **entry.source_contract.model_dump(include={"identity", "revision"})
        )
        or _key(selected.binding)
        != ContractKeyDTO(
            **entry.binding_contract.model_dump(include={"identity", "revision"})
        )
        or selected.binding.source != selected.source.key
        or selected.binding.silent_fallback
        or not selected.binding.mappings
        or any(m.definition != BLS_CPI_HEADLINES.key for m in selected.binding.mappings)
    ):
        _fail("source_binding_not_admitted")
    # Compare whole records, not merely identifiers supplied by the Host.
    from market_data_foundation.historical.bls_cpi_direct import (
        BLS_CPI_DIRECT_BINDING_CANDIDATE,
        BLS_CPI_SEPTEMBER_RELEASE_BINDING_CANDIDATE,
    )

    history_source, history_binding = cpi_history.source_binding()
    contracts = {
        s.key: (s, b, reference)
        for s, b, reference in (
            (history_source, history_binding, None),
            (BLS_CPI_DIRECT_SOURCE, BLS_CPI_DIRECT_BINDING_CANDIDATE, "2026-08"),
            (
                BLS_CPI_SEPTEMBER_RELEASE_SOURCE_CANDIDATE,
                BLS_CPI_SEPTEMBER_RELEASE_BINDING_CANDIDATE,
                "2026-09",
            ),
        )
    }
    real_source, real_binding, reference = contracts[selected.source.key]
    if selected.source != real_source or selected.binding != real_binding:
        _fail("source_binding_not_admitted")
    if reference is not None and query.reference != reference:
        _fail("source_reference_not_admitted")
    # Re-read current nonsecret credential state for this source only. An old
    # binding attestation cannot override a removed or unknown package credential.
    _current_credential(host, record, entry.source_name)
    admission = selected.admission
    if admission.qualified is not True:
        _fail(
            "source_qualification_unknown"
            if admission.qualified is None
            else "source_not_qualified"
        )
    if admission.credential_state != "configured":
        _fail("credential_" + admission.credential_state)
    _permission(admission.source_permission, "source_permission")
    _permission(admission.query_permission, "query_permission")
    if query.reference not in admission.references:
        _fail("source_scope_not_admitted", 403, "REJECTED")
    source_request = CpiSourceRequest(
        query.reference,
        at,
        query.operation_id,
        selected.source,
        selected.binding,
        tuple(sorted(f"{query.reference}:bls:cpi:{name}" for name in MEASURES)),
    )
    try:
        response = selected.fetch(source_request)
    except Exception:
        _fail("source_failed", 503, "UNKNOWN")
    try:
        if (
            not isinstance(response, CpiSourceResponse)
            or type(response.content) is not bytes
            or len(response.content) > 2_000_000
        ):
            _fail("source_payload_invalid")
        if (
            not response.evidence_grades
            or not response.evidence_grades <= selected.source.evidence_capability
            or not response.evidence_grades
            <= selected.binding.mappings[0].evidence.allowed
        ):
            _fail("source_evidence_invalid")
        document = _content(
            response.content, query.reference, source=selected.source.key
        )
        obtained = datetime.fromisoformat(document["obtained_at"])
        if obtained.tzinfo is None or obtained.utcoffset() is None or obtained < at:
            _fail("source_obtained_at_invalid")
        return CpiQueryResult(
            definition_ref=_key(BLS_CPI_HEADLINES),
            reference=query.reference,
            decision_at=at,
            channel="upstream",
            durable=False,
            local_reason=reason,
            operation_id=query.operation_id,
            source_ref=_key(selected.source),
            binding_ref=_key(selected.binding),
            obtained_at=obtained,
            evidence_grades=sorted(g.value for g in response.evidence_grades),
            measurements=_measurements(document),
        )
    except QueryRejected:
        raise
    except Exception:
        _fail("source_payload_invalid")


def failure_response(error, reason=None):
    return JSONResponse(
        status_code=error.code,
        headers={"Cache-Control": "no-store"},
        content=CpiQueryFailure(
            status=error.status,
            classification=error.classification,
            local_reason=reason,
        ).model_dump(mode="json"),
    )


class _QueryRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def guarded(request):
            keys = [k for k, _ in request.query_params.multi_items()]
            has_body = False
            async for chunk in request.stream():
                if chunk:
                    has_body = True
                    break
            if len(keys) != len(set(keys)) or has_body:
                return failure_response(
                    QueryRejected("invalid_request", 422, "REJECTED")
                )
            try:
                return await original(request)
            except RequestValidationError:
                return failure_response(
                    QueryRejected("invalid_request", 422, "REJECTED")
                )

        return guarded


def query_router(host: CpiQueryHost | None):
    router = APIRouter(route_class=_QueryRoute)

    @router.get(
        "/api/r5/cpi/query",
        response_model=CpiQueryResult,
        responses={code: {"model": CpiQueryFailure} for code in (403, 409, 422, 503)},
        operation_id="cpi_query",
    )
    def query(request: Request, query: Annotated[CpiQuery, Query()]):
        reason = None
        try:
            if host is None:
                _fail("query_host_not_configured", 503, "UNKNOWN")
            at = host.clock()
            if (
                not isinstance(at, datetime)
                or at.tzinfo is None
                or at.utcoffset() is None
            ):
                _fail("decision_clock_invalid", 503, "UNKNOWN")
            at = at.astimezone(UTC)
            try:
                access = host.authorize(request)
            except Exception:
                _fail("query_authorization_unavailable", 503, "UNKNOWN")
            if not isinstance(access, CpiQueryAccess):
                _fail("query_authorization_unavailable", 503, "UNKNOWN")
            _permission(access.metadata_permission, "metadata_permission")
            _permission(access.read_permission, "read_permission")
            if (
                query.operation_id is not None
                and query.operation_id not in _source_entries()
            ):
                _fail("source_binding_not_admitted", 422, "REJECTED")
            try:
                local, reason = _local(host, query, at)
            except QueryRejected, ReadContractError:
                raise
            except Exception:
                _fail("storage_unavailable", 503, "UNKNOWN")
            if local is not None:
                return local
            return _upstream(host, query, at, reason, request)
        except QueryRejected as error:
            return failure_response(error, reason)
        except ReadContractError as error:
            classification = error.classification.value
            return failure_response(
                QueryRejected(
                    classification,
                    503 if classification == "storage_unavailable" else 409,
                    "UNKNOWN"
                    if classification == "storage_unavailable"
                    else "NOT_AVAILABLE",
                ),
                reason,
            )
        except Exception:
            return failure_response(
                QueryRejected("query_unavailable", 503, "UNKNOWN"), reason
            )

    return router
