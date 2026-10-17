"""Deterministic fail-closed discovery and extraction from retained HTML only."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

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
from market_data_foundation.source_request_policy import valid_transport_attempts

from .fomc_statement_contract import (
    CALENDAR_URL,
    DOCUMENT_ID,
    EXTRACTION_REVISION,
    MAX_BODY_BYTES,
    PROVIDER,
    STATEMENT_DOCUMENT,
    CalendarDiscovery,
    StatementDocument,
)


class DocumentError(ValueError):
    """A safe parser classification, without source content."""


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list[_Node | str] = field(default_factory=list)
    parent: _Node | None = field(default=None, repr=False, compare=False)

    def nodes(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.nodes()

    def text(self) -> str:
        return " ".join(
            "".join(
                child if isinstance(child, str) else " " + child.text() + " "
                for child in self.children
            ).split()
        )

    def has_class(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()


class _HTML(HTMLParser):
    def __init__(self, raw: bytes):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.stack = [self.root]
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BODY_BYTES:
            raise DocumentError("html_size_invalid")
        try:
            self.feed(raw.decode("utf-8", errors="strict"))
            self.close()
        except UnicodeError, ValueError:
            raise DocumentError("html_invalid") from None
        if len(self.stack) != 1:
            raise DocumentError("html_unclosed")

    def handle_starttag(self, tag, attrs):
        if len(dict(attrs)) != len(attrs):
            raise DocumentError("html_duplicate_attribute")
        node = _Node(tag, {k: v or "" for k, v in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag in {node.tag for node in self.stack[1:]}:
            while self.stack[-1].tag != tag:
                self.stack.pop()
            self.stack.pop()

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def validate_statement_url(url: str) -> str:
    """Exact origin and Statement HTML route; never manufacture a target."""
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https"
            and parts.netloc == "www.federalreserve.gov"
            and not parts.query
            and not parts.fragment
            and re.fullmatch(
                r"/newsevents/pressreleases/monetary20260916a\.htm",
                parts.path,
            )
        )
    except ValueError:
        valid = False
    if not valid:
        raise DocumentError("statement_url_invalid")
    return url


def _one(nodes: list[_Node], classification: str) -> _Node:
    if len(nodes) != 1:
        raise DocumentError(classification)
    return nodes[0]


def parse_calendar(raw: bytes) -> CalendarDiscovery:
    root = _HTML(raw).root
    # Official calendar year panel, then the exact month/date meeting row.
    panel = _one(
        [
            n
            for n in root.nodes()
            if n.has_class("panel")
            and any(
                h.tag in {"h2", "h3", "h4"} and h.text() == "2026 FOMC Meetings"
                for h in n.nodes()
            )
        ],
        "calendar_year_ambiguous",
    )
    row = _one(
        [
            n
            for n in panel.nodes()
            if n.has_class("fomc-meeting")
            and [m.text() for m in n.nodes() if m.has_class("fomc-meeting__month")]
            == ["September"]
            and [
                re.sub(r"[–—]", "-", d.text()).rstrip("*")
                for d in n.nodes()
                if d.has_class("fomc-meeting__date")
            ]
            == ["15-16"]
        ],
        "calendar_meeting_ambiguous",
    )
    statement_candidates = []
    for node in row.nodes():
        if node.tag not in {"div", "p"} or not _visible(node):
            continue
        direct_labels = [
            child
            for child in node.children
            if isinstance(child, _Node) and child.tag == "strong"
        ]
        if (
            len(direct_labels) == 1
            and _visible(direct_labels[0])
            and direct_labels[0].text() == "Statement:"
            and _inline_content(direct_labels[0])
        ):
            statement_candidates.append(node)
    statement = _one(statement_candidates, "calendar_statement_ambiguous")
    link = _one(
        [
            child
            for child in statement.children
            if isinstance(child, _Node)
            and child.tag == "a"
            and _visible(child)
            and _inline_content(child)
            and child.text() == "HTML"
        ],
        "calendar_html_ambiguous",
    )
    href = link.attrs.get("href", "")
    if not href or href != href.strip() or any(ord(c) <= 32 for c in href):
        raise DocumentError("statement_url_invalid")
    url = validate_statement_url(urljoin(CALENDAR_URL, href))
    return CalendarDiscovery(sha256(raw).hexdigest(), len(raw), url)


_INERT_TAGS = {
    "script",
    "style",
    "nav",
    "footer",
    "template",
    "noscript",
    "aside",
    "header",
}
_INLINE_TAGS = {"em", "strong", "b", "i", "a", "span", "br", "sup", "sub", "u"}


def _inert(node: _Node) -> bool:
    raw_style = node.attrs.get("style", "")
    if "/*" in raw_style or "*/" in raw_style:
        raise DocumentError("html_style_unsupported")
    style = re.sub(r"\s+", "", raw_style.lower())
    return (
        node.tag in _INERT_TAGS
        or "hidden" in node.attrs
        or node.attrs.get("aria-hidden", "false").lower() != "false"
        or "display:none" in style
        or "visibility:hidden" in style
    )


def _visible(node: _Node) -> bool:
    while node is not None:
        if _inert(node):
            return False
        node = node.parent
    return True


def _inline_content(node: _Node) -> bool:
    return all(n is node or n.tag in _INLINE_TAGS for n in node.nodes()) and not any(
        _inert(n) for n in node.nodes()
    )


def _statement_texts(body: _Node) -> list[str]:
    # Admit only known direct children in document order, including inside wrappers.
    if not _visible(body) or any(_inert(n) for n in body.nodes()):
        raise DocumentError("statement_body_contaminated")

    def ordered(container):
        for child in container.children:
            if isinstance(child, str):
                if child.strip():
                    raise DocumentError("statement_body_contaminated")
                continue
            classes = set(child.attrs.get("class", "").split())
            if child.tag == "div" and classes in (
                {"row"},
                {"col-xs-12"},
                {"col-xs-12", "col-sm-12", "col-md-12"},
            ):
                yield from ordered(child)
            else:
                yield child

    texts = []
    for node in ordered(body):
        text = node.text()
        if "Implementation Note" in text:
            raise DocumentError("statement_body_contaminated")
        if any(
            control in text for control in ("For media inquiries", "Media Contacts")
        ):
            if not text.startswith(
                ("For media inquiries", "Media Contacts")
            ) or not _inline_content(node):
                raise DocumentError("statement_body_contaminated")
            return texts
        if node.tag != "p" or not _inline_content(node) or not text:
            raise DocumentError("statement_body_contaminated")
        texts.append(text)
    raise DocumentError("statement_anchors_invalid")


def _direct_text(node: _Node) -> str:
    return " ".join(
        "".join(child for child in node.children if isinstance(child, str)).split()
    )


def parse_statement(raw: bytes, source_url: str) -> StatementDocument:
    validate_statement_url(source_url)
    root = _HTML(raw).root
    article = _one(
        [n for n in root.nodes() if n.attrs.get("id") == "article" and _visible(n)],
        "statement_article_ambiguous",
    )
    visible = [n for n in article.nodes() if _visible(n)]

    title = _one(
        [n for n in visible if n.tag in {"h1", "h2", "h3"}],
        "statement_title_invalid",
    )
    if title.text() != "Federal Reserve issues FOMC statement" or not _inline_content(
        title
    ):
        raise DocumentError("statement_title_invalid")

    article_time = _one(
        [n for n in visible if n.has_class("article__time")],
        "statement_date_invalid",
    )
    if article_time.text() != "September 16, 2026" or not _inline_content(article_time):
        raise DocumentError("statement_date_invalid")

    release = _one(
        [n for n in visible if n.tag == "p" and n.has_class("releaseTime")],
        "statement_release_invalid",
    )
    if _direct_text(release) != "For release at 2:00 p.m. EDT":
        raise DocumentError("statement_release_invalid")
    release_children = [child for child in release.children if isinstance(child, _Node)]
    if len(release_children) > 1 or any(
        child.tag != "ul"
        or "list-unstyled" not in child.attrs.get("class", "").split()
        or not _visible(child)
        for child in release_children
    ):
        raise DocumentError("statement_release_invalid")

    body = _one(
        [
            n
            for n in visible
            if n.tag == "div"
            and set(n.attrs.get("class", "").split())
            == {"col-xs-12", "col-sm-8", "col-md-8"}
        ],
        "statement_body_ambiguous",
    )
    texts = _statement_texts(body)
    if (
        len(texts) != 4
        or not texts[0].startswith(
            "The Federal Open Market Committee approved the following statement "
            "for release by a "
        )
        or not texts[0].endswith(" vote:")
        or not texts[1].startswith("The Committee decided")
        or not texts[2].startswith("Economic activity")
        or not texts[3].startswith("Inflation remains elevated")
    ):
        raise DocumentError("statement_anchors_invalid")

    updates = [
        n.text()
        for n in root.nodes()
        if n.attrs.get("id") == "lastUpdate" and _visible(n)
    ]
    if len(updates) > 1:
        raise DocumentError("statement_last_update_ambiguous")
    return StatementDocument(
        source_url,
        sha256(raw).hexdigest(),
        len(raw),
        "\n\n".join(texts),
        updates[0] if updates else None,
    )


# Stage-2 canonical construction; Stage-1 parsers above are unchanged.
PARSER_REVISION = EXTRACTION_REVISION
CONTRACT_REVISION = "t07-r1"
SOURCE_IDENTITY = "mdf.source-candidate.fomc.statement"
BINDING_IDENTITY = "mdf.binding-candidate.fomc.statement"
RULE = "t07-fomc-statement-r1"
REFERENCE = DOCUMENT_ID
COMPLETENESS = "one-calendar-one-discovered-statement"
MAPPING = "statement-document"
MAX_RESPONSE_BYTES = MAX_BODY_BYTES
TIMEOUT_SECONDS = 20

STATEMENT_SOURCE = SourceOperationRef(
    SOURCE_IDENTITY,
    CONTRACT_REVISION,
    "market-data-foundation",
    PROVIDER,
    "calendar+discovered-statement",
    PARSER_REVISION,
    "exact-two-public-requests",
    None,
    None,
    None,
    "read",
    "release_capture",
    Scope(),
    "own requested_at/obtained_at; official_published_at is document metadata only",
    COMPLETENESS,
    "two bounded logical reads; no pagination",
    "official Federal Reserve document",
    frozenset({Grade.ORIGINAL, Grade.PROJECTED}),
)
STATEMENT_BINDING_CANDIDATE = SourceBindingRevision(
    BINDING_IDENTITY,
    CONTRACT_REVISION,
    STATEMENT_SOURCE.key,
    (
        OutputMapping(
            MAPPING,
            STATEMENT_DOCUMENT.key,
            CONTRACT_REVISION,
            "official-document-acquisition-instant-r1",
            Scope(),
            EvidenceRequirement(
                frozenset({Grade.ORIGINAL, Grade.PROJECTED}), True, True
            ),
        ),
    ),
    "exact-two-public-requests-and-extraction",
    (
        "candidate_only_not_active",
        "official_federalreserve.gov",
        "no_provider_fallback",
    ),
    silent_fallback=False,
)


@dataclass(frozen=True)
class StatementPlan:
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


def _semantic(candidate):
    return {
        "discovery": {
            **asdict(candidate.discovery),
            "meeting_start": candidate.discovery.meeting_start.isoformat(),
            "meeting_end": candidate.discovery.meeting_end.isoformat(),
        },
        "document": {
            **asdict(candidate.document),
            "meeting_start": candidate.document.meeting_start.isoformat(),
            "meeting_end": candidate.document.meeting_end.isoformat(),
            "official_published_at": (
                candidate.document.official_published_at.isoformat()
            ),
            "text_sha256": candidate.document.text_sha256,
        },
    }


def _candidate_parts(candidate):
    from .fomc_statement_live import StatementCandidate

    require(type(candidate) is StatementCandidate, "statement_candidate_required")
    require(
        type(candidate.reads) is tuple and len(candidate.reads) == 2,
        "statement_two_reads_required",
    )
    discovery = parse_calendar(candidate.reads[0].raw)
    document = parse_statement(candidate.reads[1].raw, discovery.statement_url)
    require(
        candidate.discovery == discovery and candidate.document == document,
        "statement_projection_mismatch",
    )
    requests = []
    for index, (read, url, operation) in enumerate(
        zip(
            candidate.reads,
            (CALENDAR_URL, discovery.statement_url),
            ("calendar", "statement"),
            strict=True,
        ),
        1,
    ):
        artifact = read.artifact
        require(
            type(artifact.logical_read) is int
            and artifact.logical_read == index
            and type(artifact.status_code) is int
            and artifact.status_code == 200
            and artifact.request.url == url
            and valid_transport_attempts(artifact.attempts),
            "statement_public_request_mismatch",
        )
        instant(artifact.requested_at)
        instant(artifact.obtained_at)
        require(
            artifact.requested_at <= artifact.obtained_at, "statement_timing_invalid"
        )
        if requests:
            require(
                datetime.fromisoformat(requests[-1]["obtained_at"])
                <= artifact.requested_at,
                "statement_logical_order_invalid",
            )
        require(
            type(read.raw) is bytes
            and 0 < len(read.raw) <= MAX_RESPONSE_BYTES
            and type(read.paths.raw) is str
            and bool(read.paths.raw)
            and type(read.paths.request) is str
            and bool(read.paths.request),
            "statement_raw_invalid",
        )
        require(
            type(artifact.headers) is tuple
            and all(
                type(pair) is tuple
                and len(pair) == 2
                and all(type(v) is str for v in pair)
                for pair in artifact.headers
            ),
            "statement_headers_invalid",
        )
        requests.append(
            {
                "logical_read": index,
                "operation": operation,
                "url": url,
                "attempts": artifact.attempts,
                "status_code": artifact.status_code,
                "headers": [list(pair) for pair in artifact.headers],
                "requested_at": artifact.requested_at.isoformat(),
                "obtained_at": artifact.obtained_at.isoformat(),
                "raw_path": read.paths.raw,
                "request_path": read.paths.request,
            }
        )
    return requests


def build_live_plan(
    candidate, *, identity: str, finished_at: datetime, frozen_at: datetime
) -> StatementPlan:
    requests = _candidate_parts(candidate)
    instant(finished_at)
    instant(frozen_at)
    require(type(identity) is str and bool(identity), "statement_identity_required")
    require(
        max(read.artifact.obtained_at for read in candidate.reads)
        <= finished_at
        <= frozen_at,
        "statement_processing_time_invalid",
    )
    transform = Transform(
        STATEMENT_DOCUMENT.identity,
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
        binding=STATEMENT_BINDING_CANDIDATE.key,
        transform=transform,
        trigger="explicit_statement_candidate",
        budget=2,
    )
    acquisitions = tuple(
        AcquisitionOccurrence(
            f"{identity}:read:{doc['logical_read']}:{doc['operation']}",
            run.key,
            doc["url"],
            STATEMENT_SOURCE.key,
            STATEMENT_BINDING_CANDIDATE.key,
            MAPPING,
            Scope(),
            (("url", doc["url"]), ("operation", doc["operation"])),
            read.artifact.requested_at,
            read.artifact.obtained_at,
            None,
            None,
            "release_capture",
            COMPLETENESS,
            "official publication is semantic metadata only",
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
            named_class="fomc_" + requests[index - 1]["operation"] + "_raw",
        )
        for index, (read, acquisition) in enumerate(
            zip(candidate.reads, acquisitions, strict=True), 1
        )
    )
    content = _encode(
        {
            "schema": RULE,
            "provider": PROVIDER,
            **_semantic(candidate),
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
        named_class="two_raw_statement_projection_carrier",
    )
    members = (DOCUMENT_ID,)
    version = DatasetVersion(
        identity + ":version",
        STATEMENT_DOCUMENT.key,
        FiniteScope(members),
        Scope(),
        frozen_at,
        run.key,
        digest(content),
        binding=STATEMENT_BINDING_CANDIDATE.key,
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
        "official-document-statement-candidate",
        required_completeness=COMPLETENESS,
        claims_replayable=True,
    )
    return StatementPlan(
        STATEMENT_SOURCE,
        STATEMENT_BINDING_CANDIDATE,
        run,
        acquisitions,
        raw_materials,
        semantic,
        version,
        request,
        content,
    )


def candidate_from_content(content: bytes):
    from .fomc_statement_live import (
        ArtifactPaths,
        Request,
        RequestArtifact,
        RetainedRead,
        StatementCandidate,
    )

    document = json.loads(content)
    require(
        len(document["raw"]) == len(document["requests"]) == 2,
        "statement_carriers_invalid",
    )
    reads = []
    for carrier, doc in zip(document["raw"], document["requests"], strict=True):
        require(
            set(carrier) == {"base64", "sha256", "size"}, "statement_carrier_invalid"
        )
        raw = base64.b64decode(carrier["base64"], validate=True)
        require(
            type(carrier["size"]) is int
            and len(raw) == carrier["size"]
            and digest(raw) == carrier["sha256"],
            "statement_raw_digest_mismatch",
        )
        reads.append(
            RetainedRead(
                raw,
                ArtifactPaths(doc["raw_path"], doc["request_path"]),
                RequestArtifact(
                    doc["logical_read"],
                    Request(doc["url"]),
                    doc["attempts"],
                    datetime.fromisoformat(doc["requested_at"]),
                    datetime.fromisoformat(doc["obtained_at"]),
                    tuple(tuple(pair) for pair in doc["headers"]),
                    doc["status_code"],
                ),
            )
        )
    discovery = parse_calendar(reads[0].raw)
    candidate = StatementCandidate(
        tuple(reads),
        discovery,
        parse_statement(reads[1].raw, discovery.statement_url),
        (),
    )
    requests = _candidate_parts(candidate)
    expected = {
        "schema": RULE,
        "provider": PROVIDER,
        **_semantic(candidate),
        "requests": requests,
        "raw": [
            {
                "base64": base64.b64encode(r.raw).decode(),
                "sha256": r.raw_sha256,
                "size": r.raw_size,
            }
            for r in reads
        ],
        "parser_revision": PARSER_REVISION,
        "contract_revision": CONTRACT_REVISION,
    }
    require(content == _encode(expected), "statement_content_lineage_mismatch")
    return candidate


def validate_live_plan(plan: StatementPlan, raw: tuple[bytes, ...]) -> None:
    require(
        type(plan) is StatementPlan and type(raw) is tuple and len(raw) == 2,
        "statement_plan_required",
    )
    candidate = candidate_from_content(plan.content)
    require(
        tuple(read.raw for read in candidate.reads) == raw, "statement_raw_mismatch"
    )
    expected = build_live_plan(
        candidate,
        identity=plan.run.identity.removesuffix(":run"),
        finished_at=plan.run.finished_at,
        frozen_at=plan.version.frozen_at,
    )
    require(plan == expected, "statement_plan_lineage_mismatch")


def durable_plan(plan: StatementPlan, locators: tuple[str, ...]) -> StatementPlan:
    require(
        len(locators) == len(plan.raw_materials) == 2, "statement_two_locators_required"
    )
    return replace(
        plan,
        raw_materials=tuple(
            replace(material, controlled_ref=locator, durable=True, replayable=True)
            for material, locator in zip(plan.raw_materials, locators, strict=True)
        ),
    )


def _validate_durable_plan(plan: StatementPlan) -> None:
    candidate = candidate_from_content(plan.content)
    expected = build_live_plan(
        candidate,
        identity=plan.run.identity.removesuffix(":run"),
        finished_at=plan.run.finished_at,
        frozen_at=plan.version.frozen_at,
    )
    from .fomc_statement_material_store import StatementMaterialIdentity

    expected = durable_plan(
        expected,
        tuple(
            StatementMaterialIdentity(digest(r.raw), len(r.raw)).locator
            for r in candidate.reads
        ),
    )
    require(plan == expected, "statement_durable_lineage_mismatch")
    require(
        plan.source == STATEMENT_SOURCE
        and plan.binding_candidate == STATEMENT_BINDING_CANDIDATE,
        "statement_source_binding_mismatch",
    )
    require(
        len(plan.raw_materials) == 2
        and all(m.durable and m.replayable for m in plan.raw_materials),
        "statement_raw_not_durable",
    )


def persist_before_publication(
    kernel: CanonicalKernel, plan: StatementPlan
) -> DatasetVersion:
    _validate_durable_plan(plan)
    for record in (
        plan.source,
        STATEMENT_DOCUMENT,
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
        "statement_publication_time_inversion",
    )


def _validate_publication_source(ledger: Ledger, plan: StatementPlan) -> None:
    require(
        ledger.get(plan.version.key) == plan.version
        and ledger.get_content(plan.version.key) == plan.content
        and all(ledger.get(a.key) == a for a in plan.acquisitions)
        and all(
            ledger.get(m.key) == m
            for m in (*plan.raw_materials, plan.semantic_material)
        ),
        "statement_publication_source_mismatch",
    )


class _StatementPublicationLedger(PostgresLedger):
    def __init__(self, ledger: PostgresLedger, plan: StatementPlan):
        super().__init__(ledger.connection, ledger.schema)
        self.plan = plan

    def server_now(self) -> datetime:
        at = super().server_now()
        require(
            at >= self.plan.version.frozen_at, "statement_publication_time_inversion"
        )
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


def publish(kernel: CanonicalKernel, plan: StatementPlan) -> Publication:
    _validate_durable_plan(plan)

    def checked_clock():
        at = kernel.clock()
        require(at >= plan.version.frozen_at, "statement_publication_time_inversion")
        return at

    if not isinstance(kernel.ledger, PostgresLedger):
        _validate_publication_source(kernel.ledger, plan)
    ledger = (
        _StatementPublicationLedger(kernel.ledger, plan)
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
