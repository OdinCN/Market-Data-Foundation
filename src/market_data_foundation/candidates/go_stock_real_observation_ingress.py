"""Bounded Go decoded-quote ingress; no provider execution or publication."""

import json
import math
import re
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from market_data_foundation.candidates.stock_snapshot import (
    MEMBERS,
    SDK_RELEASE,
    UNITS,
    Clocks,
    Record,
    StockSnapshotCandidate,
    View,
)
from market_data_foundation.canonical.model import ContractError, require

SCHEMA = "mdf.candidate.go-stock.real-observation.r1"
REAL_ORIGIN = "real_go_sdk_decoded_quote"
REAL_CLOCKS = "local_probe_wall_clock_utc"
OFFLINE_ORIGIN = "author_created_offline_fixture"
ENDPOINT = "139.9.52.158:7709"
MAX_WIRE_BYTES = 32_768
MAX_ROUNDS = 3
_ID = re.compile(r"go-real-r1-[1-3]\Z")
_FIELDS = {
    "schema",
    "input_origin",
    "clock_provenance",
    "sdk_release",
    "endpoint",
    "securities",
    "batch_id",
    "round",
    "max_rounds",
    "connection",
    "clocks",
    "quote_elapsed_ns",
    "source_event_clock",
    "kline_time_semantics",
    "server_time_semantics",
    "units",
    "failure",
    "rows",
}


def _object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise ContractError("nonfinite_json_constant")


def _float(token: str) -> float:
    value = float(token)
    require(math.isfinite(value), "nonfinite_json_number")
    return value


def _utc(value: object) -> datetime:
    require(type(value) is str and 1 <= len(value) <= 64, "clock_shape_invalid")
    try:
        result = datetime.fromisoformat(value)
    except ValueError, OverflowError:
        raise ContractError("clock_encoding_invalid") from None
    require(result.utcoffset() == timedelta(0), "utc_clock_required")
    return result


def _duration(value: object) -> None:
    require(type(value) is int and 0 <= value <= 10_000_000_000, "latency_bound")


def _decode(wire: bytes, *, offline: bool) -> tuple[dict, tuple[datetime, ...]]:
    require(type(wire) is bytes, "wire_bytes_required")
    require(0 < len(wire) <= MAX_WIRE_BYTES, "wire_size_bound")
    try:
        body = json.loads(
            wire.decode("utf-8"),
            object_pairs_hook=_object,
            parse_constant=_constant,
            parse_float=_float,
        )
    except ContractError:
        raise
    except UnicodeError, ValueError, RecursionError:
        raise ContractError("malformed_batch_json") from None
    require(type(body) is dict and body.keys() == _FIELDS, "batch_shape_invalid")
    require(body["schema"] == SCHEMA, "batch_schema_mismatch")
    require(
        body["input_origin"] == (OFFLINE_ORIGIN if offline else REAL_ORIGIN),
        "input_origin_mismatch",
    )
    require(
        body["clock_provenance"] == (OFFLINE_ORIGIN if offline else REAL_CLOCKS),
        "clock_provenance_mismatch",
    )
    require(body["sdk_release"] == SDK_RELEASE, "sdk_release_mismatch")
    require(body["endpoint"] == ENDPOINT, "endpoint_mismatch")
    require(body["securities"] == list(MEMBERS), "security_request_mismatch")
    require(body["units"] == list(UNITS), "unit_declaration_mismatch")
    require(
        body["source_event_clock"] == "UNKNOWN"
        and body["kline_time_semantics"] == "SDK_CLIENT_SAMPLED"
        and body["server_time_semantics"] == "OPAQUE",
        "source_clock_claim_forbidden",
    )
    n, maximum = body["round"], body["max_rounds"]
    require(
        type(n) is int and type(maximum) is int and 1 <= n <= maximum <= MAX_ROUNDS,
        "round_budget_invalid",
    )
    require(
        type(body["batch_id"]) is str
        and _ID.fullmatch(body["batch_id"]) is not None
        and body["batch_id"] == f"go-real-r1-{n}",
        "batch_identity_invalid",
    )
    conn = body["connection"]
    require(
        type(conn) is dict
        and set(conn) == {"requested", "received", "elapsed_ns", "redial", "attempts"}
        and conn["redial"] is False
        and type(conn["attempts"]) is int
        and conn["attempts"] == 1,
        "connection_policy_invalid",
    )
    connected = (_utc(conn["requested"]), _utc(conn["received"]))
    _duration(conn["elapsed_ns"])
    _duration(body["quote_elapsed_ns"])
    clock_body = body["clocks"]
    require(
        type(clock_body) is dict
        and set(clock_body) == {"requested", "received", "frozen"},
        "clock_shape_invalid",
    )
    clocks = tuple(_utc(clock_body[k]) for k in ("requested", "received", "frozen"))
    require(connected[0] <= connected[1] <= clocks[0], "connection_clock_order")
    require(clocks[0] <= clocks[1] <= clocks[2], "clock_order_invalid")
    failure = body["failure"]
    require(
        failure in (None, "timeout", "disconnected", "source_error"),
        "failure_classification",
    )
    require(failure is None or body["rows"] is None, "failure_rows_conflict")
    return body, clocks


@dataclass(frozen=True)
class Observation:
    batch_id: str
    record: Record
    input_origin: str
    clock_provenance: str
    known_provenance: str
    envelope_sha256: str
    connection_requested: datetime
    connection_received: datetime
    connection_elapsed_ns: int
    quote_elapsed_ns: int
    source_failure: str | None
    sdk_release: str = SDK_RELEASE
    endpoint: str = ENDPOINT
    source_event_clock: str = "UNKNOWN"

    @property
    def classification(self) -> str | None:
        return self.source_failure or self.record.classification


class GoStockRealObservationIngress:
    """One max-three-batch in-memory session; real and fixture modes cannot mix."""

    def __init__(
        self,
        *,
        offline_fixture_mode: bool = False,
        capacity: int = 3,
        ttl: timedelta = timedelta(seconds=30),
    ):
        require(type(offline_fixture_mode) is bool, "fixture_mode_invalid")
        self._offline = offline_fixture_mode
        self._candidate = StockSnapshotCandidate(capacity=capacity, ttl=ttl)
        self._history: deque[Observation] = deque(maxlen=capacity)
        self._count = 0
        self._maximum: int | None = None
        self._connection: dict | None = None
        self._stopped = False

    @property
    def history(self) -> tuple[Observation, ...]:
        return tuple(self._history)

    def ingest(self, wire: bytes, *, known_at: datetime | None = None) -> Observation:
        """Stamp byte receipt before decode. Invalid envelopes never mutate state."""
        received_here = datetime.now(UTC)
        if self._offline:
            require(type(known_at) is datetime, "offline_known_clock_required")
            require(known_at.utcoffset() == timedelta(0), "utc_clock_required")
        else:
            require(known_at is None, "real_known_override_forbidden")
            known_at = received_here
        body, capture = _decode(wire, offline=self._offline)
        clocks = Clocks(*capture, known_at)
        require(not self._stopped, "capture_already_stopped")
        require(self._count < (self._maximum or MAX_ROUNDS), "session_budget_exhausted")
        require(body["round"] == self._count + 1, "round_not_forward")
        require(self._maximum in (None, body["max_rounds"]), "session_budget_drift")
        require(self._connection in (None, body["connection"]), "connection_drift")
        failure = body["failure"]
        # The shared machine's disconnected transition means unavailable state.
        # Retain source_error separately; it is not proof of a physical disconnect.
        transition = "disconnected" if failure == "source_error" else failure
        record = self._candidate.observe(
            body["rows"],
            clocks=clocks,
            units=UNITS,
            parser_revision="go-real-decoded-shape-r1",
            failure=transition,
        )
        conn = body["connection"]
        observation = Observation(
            body["batch_id"],
            record,
            body["input_origin"],
            body["clock_provenance"],
            OFFLINE_ORIGIN if self._offline else "local_ingress_wall_clock_utc",
            sha256(wire).hexdigest(),
            _utc(conn["requested"]),
            _utc(conn["received"]),
            conn["elapsed_ns"],
            body["quote_elapsed_ns"],
            failure,
        )
        self._history.append(observation)
        self._count += 1
        self._maximum = body["max_rounds"]
        self._connection = conn
        self._stopped = failure is not None
        return observation

    def exact(self, batch_id: str) -> Observation:
        require(
            type(batch_id) is str and _ID.fullmatch(batch_id) is not None,
            "batch_identity_invalid",
        )
        for observation in self._history:
            if observation.batch_id == batch_id:
                return observation
        raise ContractError("batch_exact_not_retained")

    def as_of(self, at: datetime) -> Observation:
        record = self._candidate.as_of(at)
        return next(o for o in self._history if o.record is record)

    def read(self, security: str, *, at: datetime, batch_id: str | None = None) -> View:
        acquisition = (
            None if batch_id is None else self.exact(batch_id).record.acquisition
        )
        return self._candidate.read(security, at=at, acquisition=acquisition)
