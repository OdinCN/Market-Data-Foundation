"""Inactive, synthetic-only Go quote candidate; no source access or admission."""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Mapping

from market_data_foundation.canonical.model import (
    ContractError,
    DatasetDefinition,
    Scope,
    instant,
    require,
)

SDK_RELEASE = "injoyai/tdx v0.0.87@51c1ed51020fa43727300339dd92cbecf06a7d2e"
MEMBERS = ("sh600519", "sz000001")
IDENTITIES = {(1, "600519"): MEMBERS[0], (0, "000001"): MEMBERS[1]}
UNITS = ("integer_li", "sdk_volume_count_UNKNOWN", "sdk_level_count_UNKNOWN")
SDK_INT64_MAX = (1 << 63) - 1
SDK_UINT16_MAX = (1 << 16) - 1
# Number is Go int (32/64 bits); CutInt decodes through int32. Use that
# nonnegative subset on both platforms, independent of the Python host width.
SDK_LEVEL_NUMBER_MAX = (1 << 31) - 1
INACTIVE_DEFINITION = DatasetDefinition(
    identity="mdf.candidate.market.stock.snapshot.go",
    revision="inactive-r1",
    domain_contract="synthetic two-stock observation-driven snapshot candidate",
    purpose="offline review only; rights UNKNOWN; no product publication",
    source_qualified=False,
    key_semantics="candidate-local acquisition identity; explicit exchange/code",
    unit_semantics="Price/Amount integer li; volume and level trading units UNKNOWN",
    temporal_semantics="MDF observation/known clocks; source event clock UNKNOWN",
    revision_semantics="new known-time record for parser correction; retain lineage",
    completeness_semantics="bounded samples; no gap-free events or source freshness",
    scope=Scope(frozenset({"candidate:offline-review"})),
)


@dataclass(frozen=True)
class Clocks:
    requested: datetime
    received: datetime
    frozen: datetime
    known: datetime

    def __post_init__(self) -> None:
        for value in (self.requested, self.received, self.frozen, self.known):
            instant(value)
        require(
            self.requested <= self.received <= self.frozen <= self.known,
            "clock_order_invalid",
        )


@dataclass(frozen=True)
class Quote:
    security: str
    # Last/Open/High/Low/Close, decoded SDK integer li; not raw wire prices.
    prices_li: tuple[int, ...]
    amount_li: int
    volume_sdk: int
    bids: tuple[tuple[int, int], ...]
    asks: tuple[tuple[int, int], ...]
    client_sampled_at: datetime
    server_time_raw: str
    active_raw: tuple[int, int]

    @property
    def close_cny(self) -> Decimal:
        return _cny(self.prices_li[-1])

    @property
    def amount_cny(self) -> Decimal:
        return _cny(self.amount_li)

    @property
    def content(self) -> tuple:
        """Exclude client clock and opaque protocol counters from dedup content."""
        return (
            self.security,
            self.prices_li,
            self.amount_li,
            self.volume_sdk,
            self.bids,
            self.asks,
        )


def _cny(value: int) -> Decimal:
    """Construct exact integer-li yuan without Decimal context arithmetic."""
    return Decimal((0, tuple(int(digit) for digit in str(value)), -3))


def _integer(value: object, *, maximum: int) -> int:
    require(type(value) is int and 0 <= value <= maximum, "invalid_integer_field")
    return value


def _parse(row: Mapping) -> Quote:
    require(isinstance(row, dict), "invalid_quote_shape")
    exchange, code = row["Exchange"], row["Code"]
    require(type(exchange) is int and type(code) is str, "invalid_identity")
    require((exchange, code) in IDENTITIES, "security_out_of_scope")
    kline = row["Kline"]
    require(isinstance(kline, dict), "invalid_kline_shape")
    prices = tuple(
        _integer(kline[k], maximum=SDK_INT64_MAX)
        for k in ("Last", "Open", "High", "Low", "Close")
    )
    sampled = datetime.fromisoformat(kline["Time"])
    instant(sampled)

    def levels(name: str, buy: bool) -> tuple[tuple[int, int], ...]:
        values = row[name]
        require(isinstance(values, list) and len(values) == 5, "five_levels_required")
        result = []
        for item in values:
            require(isinstance(item, dict), "invalid_level_shape")
            require(item["Buy"] is buy, "level_side_invalid")
            result.append(
                (
                    _integer(item["Price"], maximum=SDK_INT64_MAX),
                    _integer(item["Number"], maximum=SDK_LEVEL_NUMBER_MAX),
                )
            )
        return tuple(result)

    require(type(row["ServerTime"]) is str, "invalid_server_time_shape")
    require(len(row["ServerTime"]) <= 64, "server_time_bound")
    return Quote(
        IDENTITIES[exchange, code],
        prices,
        _integer(kline["Amount"], maximum=SDK_INT64_MAX),
        _integer(kline["Volume"], maximum=SDK_INT64_MAX),
        levels("BuyLevel", True),
        levels("SellLevel", False),
        sampled,
        row["ServerTime"],
        (
            _integer(row["Active1"], maximum=SDK_UINT16_MAX),
            _integer(row["Active2"], maximum=SDK_UINT16_MAX),
        ),
    )


def parse_synthetic(rows: object, *, units: tuple[str, str, str]) -> tuple[Quote, ...]:
    """Validate decoded synthetic shape atomically; never infer exchange units."""
    require(units == UNITS, "unit_declaration_mismatch")
    require(isinstance(rows, list) and len(rows) == 2, "two_members_required")
    try:
        result = tuple(_parse(row) for row in rows)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("malformed_snapshot") from None
    require({q.security for q in result} == set(MEMBERS), "member_set_mismatch")
    return tuple(sorted(result, key=lambda q: MEMBERS.index(q.security)))


@dataclass(frozen=True)
class State:
    quote: Quote | None = None
    observed_at: datetime | None = None
    requested_at: datetime | None = None
    acquisition: str | None = None
    unavailable: bool = False


@dataclass(frozen=True)
class Record:
    acquisition: str
    segment: int
    clocks: Clocks
    parser_revision: str
    outcome: str
    classification: str | None
    quotes: tuple[Quote, ...]
    states: tuple[State, State]
    supersedes: str | None = None


@dataclass(frozen=True)
class View:
    state: State
    status: str
    age: timedelta | None
    stale: bool
    source_event_at: None = None
    source_freshness: str = "UNKNOWN"
    rights: str = "UNKNOWN / EXCLUDED_FROM_PRODUCT_EFFECT"


class StockSnapshotCandidate:
    """Bounded memory only. Exact/as-of address retained candidate records."""

    def __init__(self, *, capacity: int = 32, ttl: timedelta = timedelta(seconds=30)):
        require(type(capacity) is int and 2 <= capacity <= 256, "history_bound_invalid")
        require(timedelta(0) < ttl <= timedelta(minutes=5), "ttl_bound_invalid")
        self._history: deque[Record] = deque(maxlen=capacity)
        self._states = (State(), State())
        self._ttl = ttl
        self._segment = 0
        self._ordinal = 0
        self._watermark: datetime | None = None
        self._segment_start: datetime | None = None

    @property
    def history(self) -> tuple[Record, ...]:
        return tuple(self._history)

    def observe(
        self,
        rows: object,
        *,
        clocks: Clocks,
        units: tuple[str, str, str] = UNITS,
        parser_revision: str = "synthetic-parser-r1",
        failure: str | None = None,
        supersedes: str | None = None,
    ) -> Record:
        require(
            type(parser_revision) is str and 1 <= len(parser_revision) <= 64,
            "parser_revision",
        )
        require(failure in {None, "timeout", "disconnected"}, "failure_classification")
        if self._history:
            require(
                clocks.known > self._history[-1].clocks.known, "known_time_not_forward"
            )
        correction = None
        if supersedes is not None:
            correction = self.exact(supersedes)
            require(
                correction.segment == self._segment
                and correction.clocks.requested == clocks.requested
                and correction.parser_revision != parser_revision
                and all(s.acquisition == supersedes for s in self._states),
                "correction_not_current",
            )
        quotes: tuple[Quote, ...] = ()
        classification = failure
        if failure is None:
            try:
                quotes = parse_synthetic(rows, units=units)
            except ContractError as exc:
                classification = str(exc)
        late = (
            self._segment_start is not None and clocks.requested < self._segment_start
        ) or (self._watermark is not None and clocks.requested < self._watermark)
        ambiguous = self._watermark == clocks.requested and correction is None
        self._ordinal += 1
        acquisition = f"sample-{self._ordinal}"
        if late or ambiguous:
            outcome = "late" if late else "order_uncertain"
        elif classification:
            outcome = "failed"
            self._states = tuple(
                State(s.quote, s.observed_at, s.requested_at, s.acquisition, True)
                for s in self._states
            )
            self._watermark = clocks.requested
        else:
            duplicate = all(
                s.quote is not None and s.quote.content == q.content
                for s, q in zip(self._states, quotes, strict=True)
            )
            outcome = (
                "corrected" if correction else "duplicate" if duplicate else "sample"
            )
            self._states = tuple(
                State(
                    q,
                    correction.states[index].observed_at
                    if correction
                    else clocks.received,
                    clocks.requested,
                    acquisition,
                )
                for index, q in enumerate(quotes)
            )
            self._watermark = clocks.requested
        record = Record(
            acquisition,
            self._segment,
            clocks,
            parser_revision,
            outcome,
            classification,
            quotes,
            self._states,
            supersedes,
        )
        self._history.append(record)
        return record

    def reconnect(self, *, clocks: Clocks) -> Record:
        """Caller-declared offline observation segment, no actual connection."""
        require(
            (self._watermark is None or clocks.requested > self._watermark)
            and (
                self._segment_start is None or clocks.requested >= self._segment_start
            ),
            "reconnect_request_not_forward",
        )
        record = self.observe(None, clocks=clocks, failure="disconnected")
        self._segment += 1
        self._segment_start = clocks.known
        self._watermark = None
        return record

    def exact(self, acquisition: str) -> Record:
        for record in self._history:
            if record.acquisition == acquisition:
                return record
        raise ContractError("candidate_exact_not_retained")

    def as_of(self, at: datetime) -> Record:
        instant(at)
        for record in reversed(self._history):
            if record.clocks.known <= at:
                return record
        raise ContractError("candidate_asof_not_retained")

    def read(
        self, security: str, *, at: datetime, acquisition: str | None = None
    ) -> View:
        require(security in MEMBERS, "security_out_of_scope")
        instant(at)
        if acquisition is None and not self._history:
            return View(State(), "unknown", None, False)
        if acquisition is None:
            record = self.as_of(at)
        else:
            record = self.exact(acquisition)
            require(record.clocks.known <= at, "record_not_yet_known")
        state = record.states[MEMBERS.index(security)]
        age = None if state.observed_at is None else at - state.observed_at
        stale = age is not None and age > self._ttl
        status = (
            "unavailable"
            if state.unavailable
            else "unknown"
            if state.quote is None
            else "stale"
            if stale
            else "observed"
        )
        return View(state, status, age, stale)
