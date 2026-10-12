"""Synthetic JSON observation bridge; no SDK, source access or publication."""

import json
import math
import re
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from market_data_foundation.candidates.stock_snapshot import (
    SDK_RELEASE,
    UNITS,
    Clocks,
    Record,
    StockSnapshotCandidate,
    View,
)
from market_data_foundation.canonical.model import ContractError, require

SCHEMA = "mdf.candidate.go-stock.synthetic-batch.r1"
ORIGIN = "author_created_synthetic"
MAX_WIRE_BYTES = 32_768
_FIELDS = {
    "schema",
    "input_origin",
    "sdk_release",
    "units",
    "batch_id",
    "clock_provenance",
    "actual_observation_clocks",
    "source_event_clock",
    "clocks",
    "mode",
    "failure",
    "parser_revision",
    "supersedes_batch",
    "rows",
}
_CLOCKS = ("requested", "received", "frozen", "known")
_BATCH_ID = re.compile(r"synthetic-[A-Za-z0-9][A-Za-z0-9._-]{0,53}\Z")


def _identity(value: object) -> str:
    require(
        type(value) is str and _BATCH_ID.fullmatch(value) is not None,
        "batch_identity_invalid",
    )
    return value


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


@dataclass(frozen=True)
class _Batch:
    identity: str
    clocks: Clocks
    mode: str
    failure: str | None
    parser_revision: str
    supersedes: str | None
    rows: object


def _decode(wire: bytes) -> _Batch:
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
    require(body["input_origin"] == ORIGIN, "synthetic_origin_required")
    require(body["sdk_release"] == SDK_RELEASE, "sdk_release_mismatch")
    require(body["units"] == list(UNITS), "unit_declaration_mismatch")
    identity = _identity(body["batch_id"])
    require(body["clock_provenance"] == ORIGIN, "synthetic_clocks_required")
    require(
        body["actual_observation_clocks"] == "UNKNOWN"
        and body["source_event_clock"] == "UNKNOWN",
        "real_clock_claim_forbidden",
    )
    clock_body = body["clocks"]
    require(
        type(clock_body) is dict and set(clock_body) == set(_CLOCKS),
        "clock_shape_invalid",
    )
    require(
        all(type(v) is str and 1 <= len(v) <= 64 for v in clock_body.values()),
        "clock_shape_invalid",
    )
    try:
        clocks = Clocks(*(datetime.fromisoformat(clock_body[k]) for k in _CLOCKS))
    except ContractError:
        raise
    except ValueError, OverflowError:
        raise ContractError("clock_encoding_invalid") from None
    mode, failure = body["mode"], body["failure"]
    require(mode in ("observe", "reconnect"), "batch_mode_invalid")
    require(failure in (None, "timeout", "disconnected"), "failure_classification")
    revision = body["parser_revision"]
    require(type(revision) is str and 1 <= len(revision) <= 64, "parser_revision")
    supersedes = body["supersedes_batch"]
    if supersedes is not None:
        _identity(supersedes)
    require(failure is None or body["rows"] is None, "failure_rows_conflict")
    require(
        mode != "reconnect" or (failure == "disconnected" and supersedes is None),
        "reconnect_shape_invalid",
    )
    # The existing reconnect API has a fixed parser revision, so do not drop
    # a caller-supplied revision silently.
    require(
        mode != "reconnect" or revision == "synthetic-parser-r1",
        "reconnect_revision_mismatch",
    )
    return _Batch(identity, clocks, mode, failure, revision, supersedes, body["rows"])


def encode_synthetic_batch(envelope: object) -> bytes:
    """Freeze an author-created envelope; quote validation belongs to candidate."""
    try:
        wire = json.dumps(
            envelope, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except TypeError, ValueError, OverflowError, RecursionError:
        raise ContractError("batch_serialization_invalid") from None
    _decode(wire)
    return wire


@dataclass(frozen=True)
class Observation:
    batch_id: str
    record: Record
    supersedes_batch: str | None = None
    sdk_release: str = SDK_RELEASE
    input_origin: str = ORIGIN
    clock_provenance: str = ORIGIN
    actual_observation_clocks: str = "UNKNOWN"
    source_event_clock: str = "UNKNOWN"


class GoStockObservationBridge:
    """One bounded offline session; delegate all state transitions to candidate."""

    def __init__(self, *, capacity: int = 32, ttl: timedelta = timedelta(seconds=30)):
        self._candidate = StockSnapshotCandidate(capacity=capacity, ttl=ttl)
        self._observations: deque[Observation] = deque(maxlen=capacity)

    @property
    def history(self) -> tuple[Observation, ...]:
        return tuple(self._observations)

    def ingest(self, wire: bytes) -> Observation:
        """Reject envelope errors without mutation; record quote errors atomically."""
        batch = _decode(wire)
        require(
            all(o.batch_id != batch.identity for o in self._observations),
            "batch_identity_reused",
        )
        supersedes = (
            None
            if batch.supersedes is None
            else self.exact(batch.supersedes).record.acquisition
        )
        if batch.mode == "reconnect":
            record = self._candidate.reconnect(clocks=batch.clocks)
        else:
            record = self._candidate.observe(
                batch.rows,
                clocks=batch.clocks,
                units=UNITS,
                parser_revision=batch.parser_revision,
                failure=batch.failure,
                supersedes=supersedes,
            )
        observation = Observation(batch.identity, record, batch.supersedes)
        self._observations.append(observation)
        return observation

    def exact(self, batch_id: str) -> Observation:
        _identity(batch_id)
        for observation in self._observations:
            if observation.batch_id == batch_id:
                return observation
        raise ContractError("batch_exact_not_retained")

    def as_of(self, at: datetime) -> Observation:
        record = self._candidate.as_of(at)
        return next(o for o in self._observations if o.record is record)

    def read(self, security: str, *, at: datetime, batch_id: str | None = None) -> View:
        acquisition = (
            None if batch_id is None else self.exact(batch_id).record.acquisition
        )
        return self._candidate.read(security, at=at, acquisition=acquisition)


def synthetic_offline_demonstration() -> dict:
    """Twelve invented batches, bounded memory, classified summary only."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    bridge = GoStockObservationBridge(capacity=16, ttl=timedelta(seconds=10))

    def rows(price: int) -> list:
        return [
            {
                "Exchange": exchange,
                "Code": code,
                "Kline": {
                    **dict.fromkeys(("Last", "Open", "High", "Low", "Close"), price),
                    "Amount": 345678,
                    "Volume": 23,
                    "Time": base.isoformat(),
                },
                "BuyLevel": [
                    {"Buy": True, "Price": price - i, "Number": i} for i in range(1, 6)
                ],
                "SellLevel": [
                    {"Buy": False, "Price": price + i, "Number": i} for i in range(1, 6)
                ],
                "ServerTime": "synthetic-opaque",
                "Active1": 0,
                "Active2": 0,
            }
            for exchange, code in ((1, "600519"), (0, "000001"))
        ]

    summary = []
    for index in range(12):
        n = index * 5
        request = {3: 0, 4: 10, 10: 20}.get(index, n)
        payload = rows(12000 if index < 2 else 13000)
        failure = "timeout" if index == 7 else "disconnected" if index == 9 else None
        if failure:
            payload = None
        elif index == 5:
            payload[1]["Kline"]["Volume"] = -1
        envelope = {
            "schema": SCHEMA,
            "input_origin": ORIGIN,
            "sdk_release": SDK_RELEASE,
            "units": list(UNITS),
            "batch_id": f"synthetic-demo-{index + 1}",
            "clock_provenance": ORIGIN,
            "actual_observation_clocks": "UNKNOWN",
            "source_event_clock": "UNKNOWN",
            "clocks": {
                key: (base + timedelta(seconds=value)).isoformat()
                for key, value in zip(
                    _CLOCKS, (request, n + 1, n + 2, n + 3), strict=True
                )
            },
            "mode": "reconnect" if index == 9 else "observe",
            "failure": failure,
            "parser_revision": "synthetic-parser-r1",
            "supersedes_batch": None,
            "rows": payload,
        }
        observation = bridge.ingest(encode_synthetic_batch(envelope))
        record = observation.record
        view = bridge.read("sh600519", at=record.clocks.known)
        summary.append(
            {
                "batch_id": observation.batch_id,
                "acquisition": record.acquisition,
                "outcome": record.outcome,
                "classification": record.classification,
                "status": view.status,
                "stale": view.stale,
            }
        )
    return {
        "scope": "bounded_synthetic_offline_demonstration",
        "clock_provenance": ORIGIN,
        "actual_observation_clocks": "UNKNOWN",
        "source_event_clock": "UNKNOWN",
        "source_freshness": "UNKNOWN",
        "rights": "UNKNOWN / EXCLUDED_FROM_PRODUCT_EFFECT",
        "observations": summary,
    }


if __name__ == "__main__":
    print(json.dumps(synthetic_offline_demonstration(), sort_keys=True))
