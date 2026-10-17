"""Pure parsers for the fixed T06 Stage-1 vintage contract."""

# Stage-2: pure canonical construction. Stage-1 parsing above remains unchanged.
import base64
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    AcquisitionOccurrence,
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
from market_data_foundation.historical.vintage_contract import (
    METADATA,
    PROVIDER,
    VINTAGE_BUNDLE,
    SemanticBundle,
)
from market_data_foundation.source_request_policy import valid_transport_attempts

from .vintage_contract import (
    OBSERVATION_END,
    OBSERVATION_START,
    SERIES,
    VintageObservation,
)


class ContractError(ValueError):
    """A retained response does not satisfy the qualification contract."""


def valid_date(value: object) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _document(raw: bytes) -> dict:
    try:
        result = json.loads(raw)
    except ValueError, UnicodeError, TypeError:
        raise ContractError("invalid JSON") from None
    if not isinstance(result, dict):
        raise ContractError("expected JSON object")
    return result


def parse_discovery(raw: bytes) -> tuple[str, str]:
    values = _document(raw).get("vintage_dates")
    if not isinstance(values, list):
        raise ContractError("missing vintage_dates")
    selected: list[str] = []
    for value in values:
        if not valid_date(value):
            raise ContractError("invalid vintage date")
        if selected and value == selected[-1]:
            continue
        if selected and value >= selected[-1]:
            raise ContractError("vintage dates must be descending")
        selected.append(value)
    if len(selected) < 2:
        raise ContractError("two distinct valid vintages required")
    return selected[0], selected[1]


def parse_observations(
    raw: bytes, series: str, vintages: tuple[str, str]
) -> tuple[VintageObservation, ...]:
    if (
        series not in SERIES
        or len(vintages) != 2
        or len(set(vintages)) != 2
        or not all(valid_date(vintage) for vintage in vintages)
    ):
        raise ContractError("invalid series or selection")
    rows = _document(raw).get("observations")
    if not isinstance(rows, list):
        raise ContractError("missing observations")
    observations = []
    seen = set()
    represented = set()
    for row in rows:
        if not isinstance(row, dict) or not all(
            key in row for key in ("realtime_start", "realtime_end", "date", "value")
        ):
            raise ContractError("missing observation fields")
        vintage, end, day, raw_value = (
            row["realtime_start"],
            row["realtime_end"],
            row["date"],
            row["value"],
        )
        if (
            not valid_date(vintage)
            or not valid_date(end)
            or not valid_date(day)
            or vintage not in vintages
            or end < vintage
            or not OBSERVATION_START <= day <= OBSERVATION_END
            or not isinstance(raw_value, str)
        ):
            raise ContractError("invalid observation fields")
        key = (series, vintage, day)
        if key in seen:
            raise ContractError("duplicate observation")
        seen.add(key)
        represented.add(vintage)
        value = None
        if raw_value != ".":
            try:
                value = Decimal(raw_value)
            except InvalidOperation:
                raise ContractError("invalid numeric value") from None
            if not value.is_finite() or not raw_value.strip():
                raise ContractError("invalid numeric value")
        observations.append(
            VintageObservation(
                series, vintage, end, day, raw_value, value, raw_value == "."
            )
        )
    if represented != set(vintages):
        raise ContractError("both vintages must be represented")
    return tuple(observations)


PARSER_REVISION = "t06-vintage-parser-r1"
CONTRACT_REVISION = "t06-r1"
SOURCE_IDENTITY = "mdf.source-candidate.fred-alfred.vintage"
BINDING_IDENTITY = "mdf.binding-candidate.fred-alfred.vintage"
RULE = "t06-fred-alfred-vintage-r1"
REFERENCE = "GDP+PAYEMS|2025-01-01|2026-12-31"
COMPLETENESS = "dual-vintage-GDP+PAYEMS-bounded-2025-01-01:2026-12-31"
MAPPING = "vintage-bundle"
MAX_RESPONSE_BYTES = 1_000_000
TIMEOUT_SECONDS = 20

VINTAGE_SOURCE = SourceOperationRef(
    SOURCE_IDENTITY,
    CONTRACT_REVISION,
    "market-data-foundation",
    PROVIDER,
    "GDP+PAYEMS:discovery+observations",
    PARSER_REVISION,
    "dual-vintage-bounded",
    None,
    None,
    None,
    "read",
    "release_capture",
    Scope(),
    "vintage dates are strings; exact requested_at and obtained_at retained",
    COMPLETENESS,
    "four bounded logical reads; no pagination",
    "retrieval-bound dual-vintage selection; no first-party release claim",
    frozenset({Grade.ORIGINAL, Grade.PROJECTED}),
)
VINTAGE_BINDING_CANDIDATE = SourceBindingRevision(
    BINDING_IDENTITY,
    CONTRACT_REVISION,
    VINTAGE_SOURCE.key,
    (
        OutputMapping(
            MAPPING,
            VINTAGE_BUNDLE.key,
            CONTRACT_REVISION,
            "vintage-date-acquisition-instant-r1",
            Scope(),
            EvidenceRequirement(
                frozenset({Grade.ORIGINAL, Grade.PROJECTED}), True, True
            ),
        ),
    ),
    "exact-four-public-requests-and-parser",
    ("candidate_only_not_active", "aggregator_not_first_party", "no_provider_fallback"),
    silent_fallback=False,
)


@dataclass(frozen=True)
class VintagePlan:
    source: SourceOperationRef
    binding_candidate: SourceBindingRevision
    run: DataRun
    acquisitions: tuple[AcquisitionOccurrence, ...]
    raw_materials: tuple[EvidenceMaterial, ...]
    semantic_material: EvidenceMaterial
    version: DatasetVersion
    publication_request: PublicationRequest
    content: bytes


def _encode(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _candidate_parts(candidate):
    # Local import avoids a cycle with the frozen Stage-1 parser imports.
    from .fred_alfred_vintage_live import VintageCandidate, _request

    require(type(candidate) is VintageCandidate, "vintage_candidate_required")
    require(
        type(candidate.reads) is tuple and len(candidate.reads) == 4,
        "vintage_four_reads_required",
    )
    selections, rows, requests = [], [], []
    for index, series in enumerate(SERIES):
        discovery, observations = candidate.reads[index * 2 : index * 2 + 2]
        selected = parse_discovery(discovery.raw)
        selections.append((series, selected))
        rows.extend(parse_observations(observations.raw, series, selected))
        for offset, read in enumerate((discovery, observations)):
            artifact = read.artifact
            expected = _request(series, selected if offset else None)
            require(
                type(artifact.logical_read) is int
                and type(artifact.status_code) is int
                and artifact.request == expected
                and artifact.logical_read == index * 2 + offset + 1
                and artifact.status_code == 200
                and valid_transport_attempts(artifact.attempts),
                "vintage_public_request_mismatch",
            )
            instant(artifact.requested_at)
            instant(artifact.obtained_at)
            require(
                artifact.requested_at <= artifact.obtained_at, "vintage_timing_invalid"
            )
            require(
                type(read.raw) is bytes
                and 0 < len(read.raw) <= MAX_RESPONSE_BYTES
                and type(read.paths.raw) is str
                and bool(read.paths.raw)
                and type(read.paths.request) is str
                and bool(read.paths.request),
                "vintage_raw_invalid",
            )
            if requests:
                require(
                    datetime.fromisoformat(requests[-1]["obtained_at"])
                    <= artifact.requested_at,
                    "vintage_logical_order_invalid",
                )
            requests.append(
                {
                    "logical_read": artifact.logical_read,
                    "series": series,
                    "operation": "observations" if offset else "discovery",
                    "endpoint": expected.endpoint,
                    "url": expected.url,
                    "parameters": dict(expected.parameters),
                    "attempts": artifact.attempts,
                    "status_code": 200,
                    "requested_at": artifact.requested_at.isoformat(),
                    "obtained_at": artifact.obtained_at.isoformat(),
                    "raw_path": read.paths.raw,
                    "request_path": read.paths.request,
                }
            )
    bundle = SemanticBundle(tuple(METADATA[series] for series in SERIES), tuple(rows))
    require(
        candidate.selections == tuple(selections)
        and candidate.semantic_bundle == bundle,
        "vintage_projection_mismatch",
    )
    return selections, bundle, requests


def build_live_plan(
    candidate, *, identity: str, finished_at: datetime, frozen_at: datetime
) -> VintagePlan:
    selections, bundle, requests = _candidate_parts(candidate)
    instant(finished_at)
    instant(frozen_at)
    require(type(identity) is str and bool(identity), "vintage_identity_required")
    require(
        max(read.artifact.obtained_at for read in candidate.reads)
        <= finished_at
        <= frozen_at,
        "vintage_processing_time_invalid",
    )
    transform = Transform(
        VINTAGE_BUNDLE.identity,
        CONTRACT_REVISION,
        (("parser_revision", PARSER_REVISION),),
    )
    run = DataRun(
        identity + ":run",
        RunKind.COLLECT,
        Scope(),
        min(read.artifact.requested_at for read in candidate.reads),
        finished_at,
        "success",
        binding=VINTAGE_BINDING_CANDIDATE.key,
        transform=transform,
        trigger="explicit_vintage_candidate",
        budget=4,
    )
    acquisitions = tuple(
        AcquisitionOccurrence(
            f"{identity}:read:{doc['logical_read']}:{doc['series']}:{doc['operation']}",
            run.key,
            f"{doc['series']}:{doc['operation']}:{digest(_encode(doc['parameters']))}",
            VINTAGE_SOURCE.key,
            VINTAGE_BINDING_CANDIDATE.key,
            MAPPING,
            Scope(),
            tuple(sorted(doc["parameters"].items()))
            + (("operation", doc["operation"]),),
            read.artifact.requested_at,
            read.artifact.obtained_at,
            None,
            None,
            "release_capture",
            COMPLETENESS,
            "retrieval-bound aggregator vintage",
            "success",
            external_receipt=read.paths.raw,
            provider_data_attempts=read.artifact.attempts,
            pagination="single bounded response",
            recovery_summary="no fallback",
        )
        for read, doc in zip(candidate.reads, requests, strict=True)
    )
    raw_materials = tuple(
        EvidenceMaterial(
            f"{identity}:raw:{index}",
            Grade.ORIGINAL,
            read.raw_sha256,
            Scope(),
            acquired_from=acquisition.key,
            controlled_ref=read.paths.raw,
            named_class="fred_alfred_" + requests[index - 1]["operation"] + "_raw",
        )
        for index, (read, acquisition) in enumerate(
            zip(candidate.reads, acquisitions, strict=True), 1
        )
    )
    content = _encode(
        {
            "schema": RULE,
            "provider": PROVIDER,
            "not_first_party": True,
            "metadata": [asdict(item) for item in bundle.metadata],
            "selected_vintages": dict(selections),
            "observations": [
                {**asdict(row), "value": None if row.value is None else str(row.value)}
                for row in bundle.observations
            ],
            "requests": requests,
            "raw": [
                {
                    "base64": base64.b64encode(read.raw).decode(),
                    "sha256": read.raw_sha256,
                    "size": read.raw_size,
                }
                for read in candidate.reads
            ],
            "parser_revision": PARSER_REVISION,
            "contract_revision": CONTRACT_REVISION,
        }
    )
    semantic = EvidenceMaterial(
        identity + ":semantic",
        Grade.PROJECTED,
        digest(content),
        Scope(),
        produced_by=run.key,
        controlled_ref=f"mdf-content://{identity}:version",
        durable=True,
        replayable=True,
        named_class="four_raw_vintage_projection_carrier",
    )
    members = tuple(
        sorted(
            {f"{row.series_id}|{row.vintage}|{row.date}" for row in bundle.observations}
        )
    )
    version = DatasetVersion(
        identity + ":version",
        VINTAGE_BUNDLE.key,
        FiniteScope(members),
        Scope(),
        frozen_at,
        run.key,
        digest(content),
        binding=VINTAGE_BINDING_CANDIDATE.key,
        mapping=MAPPING,
        acquisitions=tuple(a.key for a in acquisitions),
        materials=tuple(m.key for m in (*raw_materials, semantic)),
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
        "bounded-aggregator-vintage-candidate",
        required_completeness=COMPLETENESS,
        claims_replayable=True,
    )
    return VintagePlan(
        VINTAGE_SOURCE,
        VINTAGE_BINDING_CANDIDATE,
        run,
        acquisitions,
        raw_materials,
        semantic,
        version,
        request,
        content,
    )


def candidate_from_content(content: bytes):
    from .fred_alfred_vintage_live import (
        ArtifactPaths,
        Request,
        RequestArtifact,
        RetainedRead,
        VintageCandidate,
    )

    document = json.loads(content)
    require(
        len(document["raw"]) == len(document["requests"]) == 4,
        "vintage_carriers_invalid",
    )
    reads = []
    for carrier, doc in zip(document["raw"], document["requests"], strict=True):
        require(set(carrier) == {"base64", "sha256", "size"}, "vintage_carrier_invalid")
        raw = base64.b64decode(carrier["base64"], validate=True)
        require(
            type(carrier["size"]) is int
            and len(raw) == carrier["size"]
            and digest(raw) == carrier["sha256"],
            "vintage_raw_digest_mismatch",
        )
        reads.append(
            RetainedRead(
                raw,
                ArtifactPaths(doc["raw_path"], doc["request_path"]),
                RequestArtifact(
                    doc["logical_read"],
                    Request(
                        doc["series"], doc["endpoint"], tuple(doc["parameters"].items())
                    ),
                    doc["attempts"],
                    datetime.fromisoformat(doc["requested_at"]),
                    datetime.fromisoformat(doc["obtained_at"]),
                    doc["status_code"],
                ),
            )
        )
    # JSON sorts parameter keys; restore the exact Stage-1 public Request order.
    from .fred_alfred_vintage_live import _request

    selections, rows = [], []
    for index, series in enumerate(SERIES):
        selected = parse_discovery(reads[index * 2].raw)
        selections.append((series, selected))
        rows.extend(parse_observations(reads[index * 2 + 1].raw, series, selected))
        for offset in range(2):
            pos = index * 2 + offset
            expected = _request(series, selected if offset else None)
            require(
                dict(reads[pos].artifact.request.parameters)
                == dict(expected.parameters),
                "vintage_public_request_mismatch",
            )
            reads[pos] = replace(
                reads[pos],
                artifact=replace(
                    reads[pos].artifact,
                    request=replace(
                        reads[pos].artifact.request, parameters=expected.parameters
                    ),
                ),
            )
    return VintageCandidate(
        tuple(reads),
        tuple(selections),
        SemanticBundle(tuple(METADATA[s] for s in SERIES), tuple(rows)),
        (),
    )


def validate_live_plan(plan: VintagePlan, raw: tuple[bytes, ...]) -> None:
    require(
        type(plan) is VintagePlan and type(raw) is tuple and len(raw) == 4,
        "vintage_plan_required",
    )
    candidate = candidate_from_content(plan.content)
    require(tuple(read.raw for read in candidate.reads) == raw, "vintage_raw_mismatch")
    expected = build_live_plan(
        candidate,
        identity=plan.run.identity.removesuffix(":run"),
        finished_at=plan.run.finished_at,
        frozen_at=plan.version.frozen_at,
    )
    require(plan == expected, "vintage_plan_lineage_mismatch")


def durable_plan(plan: VintagePlan, locators: tuple[str, ...]) -> VintagePlan:
    require(
        len(locators) == len(plan.raw_materials) == 4, "vintage_four_locators_required"
    )
    return replace(
        plan,
        raw_materials=tuple(
            replace(material, controlled_ref=locator, durable=True, replayable=True)
            for material, locator in zip(plan.raw_materials, locators, strict=True)
        ),
    )


def persist_before_publication(
    kernel: CanonicalKernel, plan: VintagePlan
) -> DatasetVersion:
    require(
        plan.source == VINTAGE_SOURCE
        and plan.binding_candidate == VINTAGE_BINDING_CANDIDATE,
        "vintage_source_binding_mismatch",
    )
    require(
        len(plan.raw_materials) == 4
        and all(m.durable and m.replayable for m in plan.raw_materials),
        "vintage_raw_not_durable",
    )
    for record in (
        plan.source,
        VINTAGE_BUNDLE,
        plan.binding_candidate,
        plan.run,
        *plan.acquisitions,
        *plan.raw_materials,
        plan.semantic_material,
    ):
        kernel.add(record)
    return kernel.persist_version(plan.version, plan.content)


def validate_publication_time(
    publication: Publication, obtained_at: datetime, frozen_at: datetime
) -> None:
    require(
        publication.decision_at >= frozen_at >= obtained_at
        and publication.usable_at >= publication.decision_at,
        "vintage_publication_time_inversion",
    )


def _validate_publication_source(ledger: Ledger, plan: VintagePlan) -> None:
    require(
        ledger.get(plan.version.key) == plan.version
        and all(ledger.get(a.key) == a for a in plan.acquisitions),
        "vintage_publication_source_mismatch",
    )


class _VintagePublicationLedger(PostgresLedger):
    def __init__(self, ledger: PostgresLedger, plan: VintagePlan):
        super().__init__(ledger.connection, ledger.schema)
        self.plan = plan

    def server_now(self) -> datetime:
        at = super().server_now()
        require(at >= self.plan.version.frozen_at, "vintage_publication_time_inversion")
        return at

    def publication_by_occurrence(self, occurrence: str) -> Publication | None:
        _validate_publication_source(self, self.plan)
        publication = super().publication_by_occurrence(occurrence)
        if publication is not None:
            validate_publication_time(
                publication,
                max(a.obtained_at for a in self.plan.acquisitions),
                self.plan.version.frozen_at,
            )
        return publication


def publish(kernel: CanonicalKernel, plan: VintagePlan) -> Publication:
    def checked_clock():
        at = kernel.clock()
        require(at >= plan.version.frozen_at, "vintage_publication_time_inversion")
        return at

    if not isinstance(kernel.ledger, PostgresLedger):
        _validate_publication_source(kernel.ledger, plan)
    ledger = (
        _VintagePublicationLedger(kernel.ledger, plan)
        if isinstance(kernel.ledger, PostgresLedger)
        else kernel.ledger
    )
    publication = ledger.publish(plan.publication_request, checked_clock)
    validate_publication_time(
        publication,
        max(a.obtained_at for a in plan.acquisitions),
        plan.version.frozen_at,
    )
    return publication
