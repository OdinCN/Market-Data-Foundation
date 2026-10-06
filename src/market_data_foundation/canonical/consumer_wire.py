"""Deterministic transport-neutral wire encoding for exact consumer reads."""

import base64
import json
from datetime import UTC, datetime
from enum import Enum
from typing import Never

from .consumer_read import (
    AcquisitionReadRecord,
    EvidenceMaterialReadRecord,
    ExactReadView,
    ReadContractError,
    ReadFailure,
    read_as_of,
    read_exact_version,
)
from .model import (
    Assessment,
    DatasetDefinition,
    DatasetVersion,
    EvidenceRequirement,
    FiniteScope,
    Key,
    Kind,
    OutputMapping,
    Publication,
    PublicationRequest,
    Scope,
    SourceBindingRevision,
    SourceOperationRef,
    Transform,
    digest,
)
from .repository import Ledger

WIRE_REVISION = "mdf.consumer.read.v1"


class _WireError(Exception):
    def __init__(self, classification: str) -> None:
        self.classification = classification


def _wire_fail(classification: str) -> Never:
    raise _WireError(classification)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _error(classification: str) -> bytes:
    return _json_bytes(
        {
            "classification": classification,
            "status": "error",
            "wire_revision": WIRE_REVISION,
        }
    )


def _reject_constant(_value: str) -> Never:
    _wire_fail("invalid_json")


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            _wire_fail("invalid_json")
        value[key] = item
    return value


def _request(payload: bytes) -> dict[str, object]:
    if type(payload) is not bytes:
        _wire_fail("invalid_json")
    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_object,
            parse_constant=_reject_constant,
        )
    except _WireError:
        raise
    except UnicodeDecodeError, json.JSONDecodeError, RecursionError:
        _wire_fail("invalid_json")
    if type(value) is not dict:
        _wire_fail("invalid_request")
    return value


def _exact_fields(
    value: dict[str, object], required: set[str], optional: set[str]
) -> None:
    if not required <= value.keys() or not value.keys() <= required | optional:
        _wire_fail("invalid_request")


def _text(value: object) -> str:
    if type(value) is not str or not value:
        _wire_fail("invalid_request")
    return value


def _definition_key(value: object) -> Key:
    if type(value) is not dict:
        _wire_fail("invalid_request")
    _exact_fields(value, {"kind", "identity", "revision"}, set())
    if value["kind"] != Kind.DEFINITION.value:
        _wire_fail("invalid_request")
    try:
        return Key(
            Kind.DEFINITION,
            _text(value["identity"]),
            _text(value["revision"]),
        )
    except Exception:
        _wire_fail("invalid_request")


def _timestamp(value: object) -> datetime:
    if type(value) is not str or not value:
        _wire_fail("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            _wire_fail("invalid_timestamp")
        return parsed
    except _WireError:
        raise
    except TypeError, ValueError, OverflowError:
        _wire_fail("invalid_timestamp")


def _instant(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _enum(value: Enum) -> str:
    return str(value.value)


def _key(value: Key | None) -> dict[str, str] | None:
    if value is None:
        return None
    return {
        "identity": value.identity,
        "kind": value.kind.value,
        "revision": value.revision,
    }


def _scope(value: Scope) -> dict[str, object]:
    return {
        "principals": (None if value.principals is None else sorted(value.principals))
    }


def _transform(value: Transform | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "identity": value.identity,
        "parameters": [list(item) for item in value.parameters],
        "revision": value.revision,
    }


def _requirement(value: EvidenceRequirement) -> dict[str, object]:
    return {
        "allowed": sorted(_enum(item) for item in value.allowed),
        "durable": value.durable,
        "replayable": value.replayable,
    }


def _mapping(value: OutputMapping) -> dict[str, object]:
    return {
        "definition": _key(value.definition),
        "evidence": _requirement(value.evidence),
        "identity": value.identity,
        "scope": _scope(value.scope),
        "semantic_revision": value.semantic_revision,
        "temporal_revision": value.temporal_revision,
    }


def _definition(value: DatasetDefinition) -> dict[str, object]:
    return {
        "completeness_semantics": value.completeness_semantics,
        "domain_contract": value.domain_contract,
        "identity": value.identity,
        "key_semantics": value.key_semantics,
        "purpose": value.purpose,
        "revision": value.revision,
        "revision_semantics": value.revision_semantics,
        "scope": _scope(value.scope),
        "source_qualified": value.source_qualified,
        "temporal_semantics": value.temporal_semantics,
        "unit_semantics": value.unit_semantics,
    }


def _finite_scope(value: FiniteScope) -> dict[str, object]:
    return {
        "end": None if value.end is None else _instant(value.end),
        "members": list(value.members),
        "start": None if value.start is None else _instant(value.start),
    }


def _version(value: DatasetVersion) -> dict[str, object]:
    return {
        "acquisitions": [_key(item) for item in value.acquisitions],
        "binding": _key(value.binding),
        "completeness": value.completeness,
        "content_digest": value.content_digest,
        "definition": _key(value.definition),
        "finite_scope": _finite_scope(value.finite_scope),
        "frozen_at": _instant(value.frozen_at),
        "identity": value.identity,
        "inputs": [_key(item) for item in value.inputs],
        "mapping": value.mapping,
        "materials": [_key(item) for item in value.materials],
        "quality_evidence": [_key(item) for item in value.quality_evidence],
        "run": _key(value.run),
        "scope": _scope(value.scope),
        "supersedes": _key(value.supersedes),
        "transform": _transform(value.transform),
    }


def _publication_request(value: PublicationRequest) -> dict[str, object]:
    return {
        "claimed_grades": sorted(_enum(item) for item in value.claimed_grades),
        "claims_replayable": value.claims_replayable,
        "decision": _enum(value.decision),
        "effective_at": (
            None if value.effective_at is None else _instant(value.effective_at)
        ),
        "evidence": [_key(item) for item in value.evidence],
        "identity": value.identity,
        "occurrence": value.occurrence,
        "predecessor": _key(value.predecessor),
        "reason": value.reason,
        "required_completeness": value.required_completeness,
        "rule_revision": value.rule_revision,
        "scope": _scope(value.scope),
        "supersedes_version": _key(value.supersedes_version),
        "usable_at": None if value.usable_at is None else _instant(value.usable_at),
        "version": _key(value.version),
    }


def _publication(value: Publication) -> dict[str, object]:
    return {
        "decision_at": _instant(value.decision_at),
        "request": _publication_request(value.request),
        "usable_at": _instant(value.usable_at),
    }


def _binding(value: SourceBindingRevision | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "compatibility_policy": value.compatibility_policy,
        "identity": value.identity,
        "limitations": list(value.limitations),
        "mappings": [_mapping(item) for item in value.mappings],
        "revision": value.revision,
        "silent_fallback": value.silent_fallback,
        "source": _key(value.source),
    }


def _source(value: SourceOperationRef | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "access_effect": value.access_effect,
        "acquisition_mode": value.acquisition_mode,
        "build_revision": value.build_revision,
        "completeness_contract": value.completeness_contract,
        "contract_revision": value.contract_revision,
        "evidence_capability": sorted(
            _enum(item) for item in value.evidence_capability
        ),
        "freshness_contract": value.freshness_contract,
        "identity": value.identity,
        "operation": value.operation,
        "owner": value.owner,
        "pagination_contract": value.pagination_contract,
        "product": value.product,
        "projection_revision": value.projection_revision,
        "provider_app_revision": value.provider_app_revision,
        "registry_revision": value.registry_revision,
        "scope": _scope(value.scope),
        "temporal_contract": value.temporal_contract,
        "variant": value.variant,
    }


def _acquisition(value: AcquisitionReadRecord) -> dict[str, object]:
    return {
        "binding": _key(value.binding),
        "canonical_record_digest": value.canonical_record_digest,
        "completeness": value.completeness,
        "external_receipt_present": value.external_receipt_present,
        "freshness": value.freshness,
        "identity": value.identity,
        "mapping": value.mapping,
        "obtained_at": (
            None if value.obtained_at is None else _instant(value.obtained_at)
        ),
        "pagination": value.pagination,
        "parameters": [list(item) for item in value.parameters],
        "provider_data_attempts": value.provider_data_attempts,
        "recovery_summary": value.recovery_summary,
        "request_identity": value.request_identity,
        "requested_at": _instant(value.requested_at),
        "result_mode": value.result_mode,
        "run": _key(value.run),
        "scope": _scope(value.scope),
        "source": _key(value.source),
        "source_frame_identity": value.source_frame_identity,
        "source_observed_at": (
            None
            if value.source_observed_at is None
            else _instant(value.source_observed_at)
        ),
        "terminal_result": value.terminal_result,
    }


def _assessment(value: Assessment | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "basis": [_key(item) for item in value.basis],
        "evaluated_at": _instant(value.evaluated_at),
        "result": value.result,
        "rule_revision": value.rule_revision,
        "target": _key(value.target),
    }


def _material(value: EvidenceMaterialReadRecord) -> dict[str, object]:
    return {
        "acquired_from": _key(value.acquired_from),
        "assessment": _assessment(value.assessment),
        "canonical_record_digest": value.canonical_record_digest,
        "controlled_ref_present": value.controlled_ref_present,
        "digest": value.digest,
        "durable": value.durable,
        "grade": _enum(value.grade),
        "identity": value.identity,
        "named_class": value.named_class,
        "produced_by": _key(value.produced_by),
        "replayable": value.replayable,
        "scope": _scope(value.scope),
    }


def _view(value: ExactReadView) -> dict[str, object]:
    return {
        "acquisitions": [_acquisition(item) for item in value.acquisitions],
        "binding": _binding(value.binding),
        "content": {
            "base64": base64.b64encode(value.content).decode("ascii"),
            "encoding": "base64",
            "sha256": digest(value.content),
        },
        "content_mode": value.content_mode,
        "definition": _definition(value.definition),
        "materials": [_material(item) for item in value.materials],
        "publication": _publication(value.publication),
        "source": _source(value.source),
        "version": _version(value.version),
    }


def handle_request(ledger: Ledger, request_bytes: bytes) -> bytes:
    """Decode one strict request, perform one consumer read, and encode a response."""
    try:
        request = _request(request_bytes)
        if "wire_revision" not in request:
            _wire_fail("invalid_request")
        if request["wire_revision"] != WIRE_REVISION:
            _wire_fail("unsupported_wire_revision")
        mode = request.get("mode")
        if mode == "exact_version":
            _exact_fields(
                request,
                {"wire_revision", "mode", "definition", "version_identity"},
                {"as_of"},
            )
            definition = _definition_key(request["definition"])
            version = Key(Kind.VERSION, _text(request["version_identity"]))
            as_of = _timestamp(request["as_of"]) if "as_of" in request else None
            view = read_exact_version(ledger, definition, version, as_of=as_of)
        elif mode == "as_of":
            _exact_fields(
                request,
                {"wire_revision", "mode", "definition", "as_of"},
                set(),
            )
            definition = _definition_key(request["definition"])
            view = read_as_of(ledger, definition, as_of=_timestamp(request["as_of"]))
        else:
            _wire_fail("invalid_request")
        return _json_bytes(
            {
                "mode": mode,
                "status": "ok",
                "view": _view(view),
                "wire_revision": WIRE_REVISION,
            }
        )
    except _WireError as exc:
        return _error(exc.classification)
    except ReadContractError as exc:
        return _error(exc.classification.value)
    except Exception:
        return _error(ReadFailure.STORAGE_UNAVAILABLE.value)
