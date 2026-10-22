"""Pure offline ingress for retained Go JSON projections and BJ report bytes.

No callbacks, SDK initialization, IO, retries, persistence or source fallback.
The bytes are material candidates; a digest is not a canonical Material receipt.
"""

import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from hashlib import sha256
from io import StringIO
from zoneinfo import ZoneInfo

from market_data_foundation.canonical.model import (
    ContractError,
    Key,
    Kind,
    instant,
    require,
)
from market_data_foundation.domain_contracts.market_universe import (
    AssetClass,
    EvidenceMode,
    canonical_bytes,
    text_fields,
)
from market_data_foundation.qualification.contracts import Capability

from .schema import (
    CLIENT_OPERATIONS,
    DECODE_REVISION,
    MAX_MATERIAL_BYTES,
    MAX_ROWS,
    MODEL_OPERATIONS,
    PAGE_SIZE,
    STRING_OPERATIONS,
    UPSTREAM_COMMIT,
    Exchange,
    Operation,
    Route,
    category_hints,
    operation_category,
)


def _integer(value: object, low: int, high: int) -> int:
    require(type(value) is int and low <= value <= high, "native_integer_out_of_range")
    return value


def _code(value: object) -> str:
    require(
        type(value) is str and re.fullmatch(r"[0-9]{6}", value) is not None,
        "six_ascii_digit_code_required",
    )
    return value


def _exchange(value: object) -> Exchange:
    require(type(value) is str and value in {"sh", "sz", "bj"}, "invalid_raw_exchange")
    return Exchange(value.upper())


@dataclass(frozen=True)
class DirectoryRequest:
    operation: Operation
    exchanges: tuple[Exchange, ...]
    route: Route
    snapshot_id: str
    start: int | None = None
    limit: int | None = None

    def __post_init__(self) -> None:
        require(isinstance(self.operation, Operation), "unsupported_operation")
        require(isinstance(self.route, Route), "unsupported_route")
        text_fields(self.snapshot_id)
        require(
            type(self.exchanges) is tuple
            and bool(self.exchanges)
            and all(isinstance(item, Exchange) for item in self.exchanges)
            and len(set(self.exchanges)) == len(self.exchanges),
            "explicit_unique_exchanges_required",
        )
        op = self.operation
        if op in {Operation.GET_COUNT, Operation.GET_CODE}:
            require(
                len(self.exchanges) == 1
                and Exchange.BJ not in self.exchanges
                and self.route == Route.STANDARD,
                "standard_sh_sz_only",
            )
        elif op in {Operation.GET_CODE_ALL, Operation.PARSE_BJ_REPORT}:
            require(len(self.exchanges) == 1, "one_exchange_required")
            expected = (
                Route.BJ_REPORT if self.exchanges == (Exchange.BJ,) else Route.STANDARD
            )
            require(self.route == expected, "directory_route_mismatch")
            if op == Operation.PARSE_BJ_REPORT:
                require(self.exchanges == (Exchange.BJ,), "bj_report_only")
        elif op in CLIENT_OPERATIONS:
            require(self.route == Route.CLIENT_FILTER, "client_filter_route_required")
            expected = (
                {Exchange.SH, Exchange.SZ}
                if op == Operation.GET_ETF_CODE_ALL
                else set(Exchange)
            )
            require(set(self.exchanges) == expected, "client_market_scope_mismatch")
        else:
            require(self.route == Route.CACHE, "retained_cache_route_required")
        if op == Operation.GET_CODE:
            _integer(self.start, 0, 65535)
        else:
            require(self.start is None, "unexpected_start")
        if self.limit is not None:
            require(
                op in MODEL_OPERATIONS | (STRING_OPERATIONS - CLIENT_OPERATIONS),
                "unexpected_limit",
            )
            _integer(self.limit, -2147483648, 2147483647)


@dataclass(frozen=True)
class OfflineMaterial:
    request: DirectoryRequest
    payload: bytes = field(repr=False)
    expected_sha256: str
    source: Key
    binding: Key
    fixture_id: str
    mode: EvidenceMode
    obtained_at: datetime
    received_at: datetime
    known_at: datetime
    implementation_sha256: str
    upstream_commit: str
    capability: Capability = Capability.SECURITY_UNIVERSE

    def __post_init__(self) -> None:
        require(type(self.request) is DirectoryRequest, "directory_request_required")
        require(type(self.payload) is bytes, "immutable_bytes_required")
        require(len(self.payload) <= MAX_MATERIAL_BYTES, "material_byte_budget")
        require(
            self.expected_sha256 == sha256(self.payload).hexdigest(), "digest_mismatch"
        )
        require(
            isinstance(self.source, Key)
            and isinstance(self.binding, Key)
            and self.source.kind == Kind.SOURCE
            and self.binding.kind == Kind.BINDING,
            "source_binding_required",
        )
        require(
            isinstance(self.mode, EvidenceMode)
            and self.mode in {EvidenceMode.RETAINED, EvidenceMode.SYNTHETIC},
            "offline_evidence_only",
        )
        require(
            self.capability
            in {Capability.SECURITY_UNIVERSE, Capability.SECURITY_METADATA}
            and isinstance(self.capability, Capability),
            "directory_capability_required",
        )
        text_fields(self.fixture_id)
        require(self.upstream_commit == UPSTREAM_COMMIT, "upstream_identity_mismatch")
        require(
            type(self.implementation_sha256) is str
            and re.fullmatch(r"[0-9a-f]{64}", self.implementation_sha256) is not None,
            "implementation_digest_required",
        )
        for value in (self.obtained_at, self.received_at, self.known_at):
            require(isinstance(value, datetime), "instant_required")
            instant(value)
        require(
            self.obtained_at.astimezone(UTC)
            <= self.received_at.astimezone(UTC)
            <= self.known_at.astimezone(UTC),
            "material_clock_order",
        )

    @property
    def material_id(self) -> str:
        return sha256(
            canonical_bytes(
                (
                    DECODE_REVISION,
                    self.request,
                    self.expected_sha256,
                    self.source,
                    self.binding,
                    self.fixture_id,
                    self.mode,
                    self.obtained_at,
                    self.received_at,
                    self.known_at,
                    self.implementation_sha256,
                    self.upstream_commit,
                    self.capability,
                )
            )
        ).hexdigest()

    @property
    def snapshot_day(self) -> date:
        return self.obtained_at.astimezone(ZoneInfo("Asia/Shanghai")).date()


@dataclass(frozen=True)
class DirectoryRecord:
    exchange: Exchange
    code: str
    raw_fields: tuple[tuple[str, str | int | float], ...]
    response_struct: str
    material_id: str
    row_locator: str
    hints: tuple[AssetClass, ...]
    metadata_status: str
    origin: str

    @property
    def symbol(self) -> str:
        return self.exchange.value.lower() + self.code

    @property
    def market(self) -> int:
        return self.exchange.market

    def raw(self, field_name: str) -> str | int | float | None:
        return dict(self.raw_fields).get(field_name)

    @property
    def last_price_status(self) -> str:
        if self.metadata_status != "OBSERVED_NATIVE_FIELDS":
            return "UNKNOWN"
        if AssetClass.A_SHARE in self.hints:
            return "UPSTREAM_STOCK_INVALID"
        if AssetClass.INDEX in self.hints:
            return "UPSTREAM_INDEX_YUAN_PREVIOUS_CLOSE_UNQUALIFIED"
        return "UNKNOWN"


@dataclass(frozen=True)
class ValidationIssue:
    classification: str
    locators: tuple[str, ...] = ()


@dataclass(frozen=True)
class DirectoryResponse:
    material: OfflineMaterial
    records: tuple[DirectoryRecord, ...]
    declared_count: int | None
    issues: tuple[ValidationIssue, ...]
    termination: str


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_field")
        result[key] = value
    return result


def _json(payload: bytes):
    try:
        return json.loads(payload, object_pairs_hook=_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        # Our classifications are constant; upstream parser errors never escape.
        if isinstance(exc, ContractError):
            raise
        raise ContractError("malformed_json") from None


def _shape(value: object, keys: set[str]) -> dict:
    require(type(value) is dict and set(value) == keys, "response_schema_mismatch")
    return value


def _records_issues(records, operation):
    issues = []
    seen = {}
    expected = operation_category(operation)
    for record in records:
        key = record.exchange, record.code
        if key in seen:
            prior = seen[key]
            issues.append(
                ValidationIssue(
                    "duplicate_code"
                    if prior.raw_fields == record.raw_fields
                    else "conflicting_duplicate_code",
                    (prior.row_locator, record.row_locator),
                )
            )
        else:
            seen[key] = record
        if expected is not None and expected not in record.hints:
            issues.append(
                ValidationIssue("operation_category_conflict", (record.row_locator,))
            )
        # client.go:355-371 enumerates SH/SZ only, plus one fixed BJ seed.
        if (
            operation == Operation.GET_INDEX_CODE_ALL
            and record.exchange == Exchange.BJ
            and record.code != "899050"
        ):
            issues.append(
                ValidationIssue("index_producer_scope_conflict", (record.row_locator,))
            )
    return tuple(issues)


def _record(
    material, exchange, code, raw, struct, locator, status, origin="RESPONSE_ROW"
):
    require(exchange in material.request.exchanges, "row_market_outside_request")
    return DirectoryRecord(
        exchange,
        _code(code),
        tuple(sorted(raw.items())),
        struct,
        material.material_id,
        f"{material.material_id}:{locator}",
        category_hints(exchange, code),
        status,
        origin,
    )


def _native_row(row: dict, *, model: bool) -> None:
    for name in ("name", "code", "exchange") if model else ("Name", "Code"):
        require(type(row[name]) is str, "native_string_required")
    name, code, multiple, decimal, last = (
        ("name", "code", "multiple", "decimal", "lastPrice")
        if model
        else ("Name", "Code", "Multiple", "Decimal", "LastPrice")
    )
    _code(row[code])
    _integer(row[multiple], 0, 65535)
    _integer(row[decimal], -128, 127)
    require(type(row[last]) in (int, float), "finite_float64_required")
    try:
        price = float(row[last])
    except OverflowError:
        raise ContractError("finite_float64_required") from None
    require(math.isfinite(price), "finite_float64_required")
    # Go encoding/json emits integral float64 as JSON integers. The typed view
    # restores float64; the unchanged payload retains the original JSON token.
    row[last] = price
    if model:
        for name in ("id", "editDate", "inDate"):
            _integer(row[name], -(2**63), 2**63 - 1)


def decode(material: OfflineMaterial) -> DirectoryResponse:
    """Decode exact native Go JSON shapes, or explicitly bound raw BJ GBK text.

    A well-formed but anomalous response remains available with all duplicate
    rows. Schema/type corruption raises a classification-only ContractError.
    """
    require(type(material) is OfflineMaterial, "offline_material_required")
    op = material.request.operation
    if op == Operation.PARSE_BJ_REPORT:
        return _bj_report(material)
    value = _json(material.payload)
    if op == Operation.GET_COUNT:
        row = _shape(value, {"Count"})
        count = _integer(row["Count"], 0, 65535)
        return DirectoryResponse(material, (), count, (), "COUNT_ONLY")
    declared = None
    if op in {Operation.GET_CODE, Operation.GET_CODE_ALL}:
        response = _shape(value, {"Count", "List"})
        declared = _integer(response["Count"], 0, 65535)
        values = response["List"]
    else:
        values = value
    # nil Go slices marshal to null; count/list agreement is still checked.
    if values is None:
        values = []
    require(type(values) is list, "response_list_required")
    require(len(values) <= MAX_ROWS, "row_budget")
    records = []
    for index, row in enumerate(values):
        if op in STRING_OPERATIONS:
            require(type(row) is str and len(row) == 8, "prefixed_symbol_required")
            ex, code = _exchange(row[:2]), _code(row[2:])
            seed = op == Operation.GET_INDEX_CODE_ALL and row == "bj899050"
            records.append(
                _record(
                    material,
                    ex,
                    code,
                    {"FullCode": row},
                    "[]string",
                    f"/{index}",
                    "UNKNOWN",
                    "UPSTREAM_HARDCODED_SEED" if seed else "RESPONSE_ROW",
                )
            )
            continue
        model = op in MODEL_OPERATIONS
        keys = (
            {
                "id",
                "name",
                "code",
                "exchange",
                "multiple",
                "decimal",
                "lastPrice",
                "editDate",
                "inDate",
            }
            if model
            else {"Name", "Code", "Multiple", "Decimal", "LastPrice"}
        )
        row = _shape(row, keys)
        _native_row(row, model=model)
        ex = _exchange(row["exchange"]) if model else material.request.exchanges[0]
        code = row["code"] if model else row["Code"]
        status = (
            "SDK_DEFAULT_NOT_OBSERVED"
            if material.request.route == Route.BJ_REPORT
            else "OBSERVED_NATIVE_FIELDS"
        )
        if model and ex == Exchange.BJ:
            # Codes.update copies BJ report defaults into CodeModel (codes.go:
            # 174-221). Cache presence, timestamps and nonzero values alone
            # cannot establish independent economic field evidence.
            status = "UNKNOWN_CACHE_FIELD_PROVENANCE"
        records.append(
            _record(
                material,
                ex,
                code,
                row,
                "CodeModel" if model else "Code",
                f"/{index}" if model else f"/List/{index}",
                status,
            )
        )
    issues = list(_records_issues(records, op))
    if op == Operation.GET_INDEX_CODE_ALL and (
        not records or records[0].symbol != "bj899050"
    ):
        issues.append(ValidationIssue("missing_or_reordered_index_seed"))
    if material.request.route == Route.BJ_REPORT:
        for record in records:
            if any(
                record.raw(name) != 0 for name in ("Multiple", "Decimal", "LastPrice")
            ):
                issues.append(
                    ValidationIssue(
                        "bj_report_unexpected_metadata", (record.row_locator,)
                    )
                )
    if declared is not None and declared != len(records):
        issues.append(ValidationIssue("declared_count_mismatch"))
    if op == Operation.GET_CODE and len(records) > PAGE_SIZE:
        issues.append(ValidationIssue("oversized_page"))
    if material.request.limit is not None and material.request.limit > 0:
        if len(records) > material.request.limit:
            issues.append(ValidationIssue("cache_limit_exceeded"))
    return DirectoryResponse(
        material,
        tuple(records),
        declared,
        tuple(issues),
        "SHORT_PAGE"
        if op == Operation.GET_CODE and len(records) < PAGE_SIZE
        else "FULL_PAGE"
        if op == Operation.GET_CODE
        else "UPSTREAM_AGGREGATE_UNVERIFIED",
    )


def _bj_report(material: OfflineMaterial) -> DirectoryResponse:
    try:
        text = material.payload.decode("gbk")
    except UnicodeError:
        raise ContractError("malformed_bj_gbk") from None
    records, issues = [], []
    input_rows = 0
    for index, line in enumerate(StringIO(text)):
        line = line.removesuffix("\n").rstrip("\r")
        if not line or line.startswith("#"):
            continue
        # Malformed rows also allocate issues and must consume the row budget.
        require(input_rows < MAX_ROWS, "row_budget")
        input_rows += 1
        parts = line.split("|")
        # Upstream silently skips malformed lines and ignores the market field.
        # Retain an issue here so skipped/foreign rows cannot look complete.
        if len(parts) < 4 or not parts[1]:
            issues.append(
                ValidationIssue("malformed_bj_report_row", (f"line:{index + 1}",))
            )
            continue
        if parts[0] != "44" or parts[2] != "2":
            issues.append(
                ValidationIssue("bj_report_market_conflict", (f"line:{index + 1}",))
            )
        records.append(
            _record(
                material,
                Exchange.BJ,
                _code(parts[1]),
                {
                    "Code": parts[1],
                    "Name": parts[3],
                    "ReportCategory": parts[0],
                    "ReportMarket": parts[2],
                },
                "tdxbjmore.cfg",
                f"line:{index + 1}",
                "UNKNOWN",
            )
        )
    issues.extend(_records_issues(records, material.request.operation))
    return DirectoryResponse(
        material, tuple(records), None, tuple(issues), "REPORT_EOF"
    )


@dataclass(frozen=True)
class PageAssembly:
    responses: tuple[DirectoryResponse, ...]
    records: tuple[DirectoryRecord, ...]
    issues: tuple[ValidationIssue, ...]
    termination: str


def assemble_pages(
    before: DirectoryResponse,
    pages: tuple[DirectoryResponse, ...],
    after: DirectoryResponse,
) -> PageAssembly:
    """Validate supplied SH/SZ pages, including an empty terminal at exact 1000.

    Count bracketing detects count drift, not same-count constituent changes.
    No claim of an atomic upstream snapshot or full-market membership is made.
    """
    require(
        type(pages) is tuple and len(pages) <= 66, "immutable_bounded_pages_required"
    )
    require(
        all(type(item) is DirectoryResponse for item in (before, *pages, after)),
        "decoded_input_required",
    )
    require(
        before.material.request.operation
        == after.material.request.operation
        == Operation.GET_COUNT,
        "bracketing_count_required",
    )
    base = before.material
    issues, records = [], []
    terminal = False
    for response in (before, *pages, after):
        material = response.material
        require(decode(material) == response, "decoded_content_mismatch")
        require(
            (
                material.source,
                material.binding,
                material.mode,
                material.fixture_id,
                material.capability,
                material.request.snapshot_id,
                material.request.exchanges,
                material.upstream_commit,
                material.implementation_sha256,
                material.snapshot_day,
            )
            == (
                base.source,
                base.binding,
                base.mode,
                base.fixture_id,
                base.capability,
                base.request.snapshot_id,
                base.request.exchanges,
                base.upstream_commit,
                base.implementation_sha256,
                base.snapshot_day,
            ),
            "page_binding_drift",
        )
        issues.extend(response.issues)
    if before.declared_count != after.declared_count:
        issues.append(ValidationIssue("directory_count_drift"))
    if pages:
        require(
            before.material.obtained_at.astimezone(UTC)
            <= pages[0].material.obtained_at.astimezone(UTC)
            and pages[-1].material.obtained_at.astimezone(UTC)
            <= after.material.obtained_at.astimezone(UTC),
            "page_clock_order",
        )
    for index, response in enumerate(pages):
        request = response.material.request
        require(request.operation == Operation.GET_CODE, "getcode_page_required")
        if request.start != index * PAGE_SIZE:
            issues.append(ValidationIssue("page_offset_gap_or_reorder"))
        if index and response.material.obtained_at.astimezone(UTC) < pages[
            index - 1
        ].material.obtained_at.astimezone(UTC):
            issues.append(ValidationIssue("page_clock_order"))
        if terminal:
            issues.append(ValidationIssue("page_after_termination"))
        records.extend(response.records)
        terminal = terminal or len(response.records) < PAGE_SIZE
    if not terminal:
        issues.append(ValidationIssue("termination_not_observed"))
    if len(records) != before.declared_count:
        issues.append(ValidationIssue("total_count_mismatch"))
    issues.extend(_records_issues(records, Operation.GET_CODE))
    return PageAssembly(
        (before, *pages, after),
        tuple(records),
        tuple(issues),
        "PAGE_TERMINATED_COUNT_MATCH" if not issues else "UNKNOWN",
    )


@dataclass(frozen=True)
class DirectoryObservation:
    fixture_id: str
    capability: Capability
    material_id: str
    classification: str
    exception_classification: str | None
    record_count: int | None
    issue_classifications: tuple[str, ...]


def observe(material: OfflineMaterial) -> DirectoryObservation:
    """In-memory qualification observation; contains no raw exception message.

    No file is written. Callers must retain raw bytes/observations only under
    ignored artifacts/, and this result never grants live Source qualification.
    """
    try:
        response = decode(material)
    except ContractError:
        return DirectoryObservation(
            material.fixture_id,
            material.capability,
            material.material_id,
            "DECODE_REJECTED",
            "ContractError",
            None,
            (),
        )
    return DirectoryObservation(
        material.fixture_id,
        material.capability,
        material.material_id,
        "QUALITY_REJECTED" if response.issues else "OFFLINE_DECODED_UNQUALIFIED",
        None,
        len(response.records),
        tuple(sorted({item.classification for item in response.issues})),
    )
