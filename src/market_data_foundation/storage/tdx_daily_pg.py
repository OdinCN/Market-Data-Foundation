"""Bounded native daily COPY candidate; no connections or DDL on import.

OfflineCurrent is a specification oracle, never evidence that PostgreSQL ran.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from market_data_foundation.canonical.model import require

UNITS = "go-tdx-v1.0.0-native-int-v1"
BASIS = "native_projection"
PRIMARY_SOURCE = "tdx-v100-primary"
SQL_DIR = Path(__file__).resolve().parents[3] / "db" / "tdx_daily"
MAX_ROWS = 51200
TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,80}")


def digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def aware(value):
    require(
        isinstance(value, datetime) and value.utcoffset() is not None,
        "aware_clock_required",
    )


@dataclass(frozen=True)
class Bar:
    market: int
    instrument: str
    trading_day: date
    values: tuple[int, int, int, int, int, int]  # OHLC, Volume, Amount
    period: str = "1d"
    basis: str = BASIS
    units: str = UNITS

    def __post_init__(self):
        require(type(self.market) is int and self.market in (0, 1), "market_invalid")
        require(
            type(self.instrument) is str
            and re.fullmatch(r"[0-9]{6}", self.instrument) is not None,
            "instrument_invalid",
        )
        require(
            type(self.trading_day) is date
            and date(1990, 1, 1) <= self.trading_day <= date(2100, 12, 31),
            "day_invalid",
        )
        require(
            self.period == "1d" and self.basis == BASIS and self.units == UNITS,
            "native_daily_only",
        )
        require(
            type(self.values) is tuple
            and len(self.values) == 6
            and all(type(v) is int and -(2**63) <= v < 2**63 for v in self.values),
            "native_int64_required",
        )
        o, h, low, c, v, a = self.values
        require(low <= min(o, c) <= max(o, c) <= h, "ohlc_invalid")
        require(v >= 0 and a >= 0, "negative_volume_amount")

    @property
    def key(self):
        return (
            self.market,
            self.instrument,
            self.period,
            self.trading_day.isoformat(),
            self.basis,
        )

    @property
    def value_digest(self):
        # Native Last is page decoder context, deliberately absent.
        return digest([self.units, *self.values])

    def copy_row(self):
        return (
            *self.key[:3],
            self.trading_day,
            self.basis,
            *self.values,
            self.units,
            self.value_digest,
        )


# Code-controlled registry; secondary/peer are OFFLINE fixtures, not providers.
SOURCE_REGISTRY = (
    (PRIMARY_SOURCE, 100),
    ("controlled-secondary", 10),
    ("controlled-peer", 10),
)


def source_rank(source):
    require(type(source) is str and source in dict(SOURCE_REGISTRY), "unbound_source")
    return dict(SOURCE_REGISTRY)[source]


@dataclass(frozen=True)
class SourcePolicy:
    """Fixed compatibility descriptor; never an input to writer/oracle authority."""

    priorities: tuple[tuple[str, int], ...] = SOURCE_REGISTRY

    def __post_init__(self):
        require(self.priorities == SOURCE_REGISTRY, "fixed_source_registry_required")

    def rank(self, source):
        return source_rank(source)


@dataclass(frozen=True)
class Batch:
    batch_id: str
    source: str
    observed_at: datetime  # batch completion metadata ONLY
    admitted_at: datetime
    bars: tuple[Bar, ...]
    evidence: dict  # one small summary per request, grouped by market:instrument
    complete: bool
    quality_passed: bool
    day_start: date
    day_end: date

    def received_at(self, bar):
        # Exactly one request per instrument is admitted. No max(page clocks).
        return datetime.fromisoformat(
            self.evidence[f"{bar.market}:{bar.instrument}"][0]["received_at"]
        )

    def request_index(self, bar):
        return self.evidence[f"{bar.market}:{bar.instrument}"][0]["request_index"]

    def validate(self):
        require(
            type(self.batch_id) is str and TOKEN.fullmatch(self.batch_id) is not None,
            "batch_id_invalid",
        )
        rank = source_rank(self.source)
        aware(self.observed_at)
        aware(self.admitted_at)
        require(self.observed_at <= self.admitted_at, "source_clock_order")
        require(
            self.complete is True and self.quality_passed is True,
            "complete_quality_evidence_required",
        )
        require(
            type(self.bars) is tuple and 1 <= len(self.bars) <= MAX_ROWS, "batch_budget"
        )
        require(
            type(self.day_start) is date
            and type(self.day_end) is date
            and self.day_start <= self.day_end,
            "range_invalid",
        )
        require(all(type(b) is Bar for b in self.bars), "bar_required")
        require(type(self.evidence) is dict, "scope_evidence")
        scopes = {f"{b.market}:{b.instrument}" for b in self.bars}
        require(len(scopes) <= 64 and set(self.evidence) == scopes, "scope_evidence")
        request_indices = []
        for scope in scopes:
            items = self.evidence[scope]
            require(
                type(items) is list and len(items) == 1,
                "ambiguous_request_clock_mapping",
            )
            for item in items:
                require(
                    type(item) is dict
                    and set(item)
                    == {
                        "request_index",
                        "received_at",
                        "line_sha256",
                        "wire_sha256",
                        "request_sha256",
                        "projection_sha256",
                    },
                    "request_evidence_shape",
                )
                require(
                    type(item["request_index"]) is int
                    and 0 <= item["request_index"] < 64,
                    "request_index_invalid",
                )
                require(type(item["received_at"]) is str, "request_clock_required")
                received = datetime.fromisoformat(item["received_at"])
                aware(received)
                require(received <= self.observed_at, "request_clock_order")
                request_indices.append(item["request_index"])
                require(
                    all(
                        type(v) is str and re.fullmatch(r"[a-f0-9]{64}", v)
                        for k, v in item.items()
                        if k not in {"request_index", "received_at"}
                    ),
                    "request_evidence_digest",
                )
        require(1 <= len(request_indices) <= 64, "request_budget")
        require(
            len(set(request_indices)) == len(request_indices), "request_identity_reused"
        )
        require(len(json.dumps(self.evidence)) <= 65536, "evidence_budget")
        counts = {}
        for bar in self.bars:
            require(type(bar) is Bar, "bar_required")
            require(
                self.day_start <= bar.trading_day <= self.day_end, "outside_batch_range"
            )
            # Acquisition local day must not precede the projected trading date.
            require(
                bar.trading_day
                <= self.received_at(bar).astimezone(ZoneInfo("Asia/Shanghai")).date(),
                "future_day",
            )
            counts.setdefault(bar.key[:2], set()).add(bar.trading_day)
        require(all(len(v) <= 800 for v in counts.values()), "scope_day_budget")
        return rank

    @property
    def fingerprint(self):
        return digest(
            {
                "source": self.source,
                "batch": self.batch_id,
                "observed": self.observed_at.isoformat(),
                "range": [self.day_start.isoformat(), self.day_end.isoformat()],
                "rows": sorted({(*b.key, *b.values, b.units) for b in self.bars}),
                "evidence": self.evidence,
            }
        )


@dataclass(frozen=True)
class Counts:
    new: int = 0
    unchanged: int = 0
    changed: int = 0
    blocked: int = 0
    replay: bool = False


COPY = """COPY pg_temp.tdx_daily_stage
(market,instrument,period,trading_day,basis,open,high,low,close,volume,amount,
units,value_digest,request_index,received_at) FROM STDIN"""
DUPLICATES = """SELECT 1 FROM pg_temp.tdx_daily_stage
GROUP BY market,instrument,period,trading_day,basis
HAVING count(DISTINCT ROW(open,high,low,close,volume,amount,units)) > 1 LIMIT 1"""


RANGE_CHECK = """SELECT 1 FROM pg_temp.tdx_daily_stage
WHERE trading_day NOT BETWEEN %(day_start)s AND %(day_end)s
    OR trading_day > (received_at AT TIME ZONE 'Asia/Shanghai')::date
    OR received_at > %(observed)s::timestamptz
LIMIT 1"""


def sql_file(name):
    require(
        name
        in {"002_stage.sql", "003_classify.sql", "004_apply.sql", "005_refresh.sql"},
        "sql_allowlist",
    )
    return (SQL_DIR / name).read_text()


# Validate persisted bindings for this instrument scope, including blocked receipts.
# The B-tree scope index bounds this history read; it runs after the writer lock
# and before replay, so a replay cannot mask corrupt persisted authority.
POLICY_CHECK = """WITH scopes AS (
    SELECT * FROM jsonb_to_recordset(%(scopes)s::jsonb)
        AS s(market smallint, instrument text)
), persisted AS (
    SELECT c.value_source AS source,c.source_priority
    FROM mdf_tdx_daily.bars_daily_current c JOIN scopes s USING (market,instrument)
    WHERE c.period='1d' AND c.basis='native_projection'
    UNION ALL
    SELECT r.source,r.source_priority
    FROM mdf_tdx_daily.source_refresh_batches r JOIN scopes s USING (market,instrument)
    WHERE r.period='1d' AND r.basis='native_projection'
)
SELECT 1 FROM persisted
WHERE (%(registry)s::jsonb ->> source)::integer IS DISTINCT FROM source_priority
LIMIT 1"""


def write_batch(conn, batch: Batch):
    """Future authorized caller supplies a fresh idle psycopg3 connection.

    A complete transaction owns COPY, classification, changes, log and receipt.
    DDL and connect are deliberately absent. No retries on unknown commit: caller
    replays the exact immutable batch identity on a fresh connection.
    """
    require(type(batch) is Batch, "batch_required")
    priority = Batch.validate(batch)
    require(
        conn.info.transaction_status == TransactionStatus.IDLE,
        "idle_connection_required",
    )
    params = {
        "registry": Jsonb(dict(SOURCE_REGISTRY)),
        "scopes": Jsonb(
            [
                {"market": market, "instrument": instrument}
                for market, instrument in sorted({b.key[:2] for b in batch.bars})
            ]
        ),
        "source": batch.source,
        "batch": batch.batch_id,
        "priority": priority,
        "observed": batch.observed_at,
        "admitted": batch.admitted_at,
        "digest": batch.fingerprint,
        "evidence": Jsonb(batch.evidence),
        "day_start": batch.day_start,
        "day_end": batch.day_end,
    }
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        cur.execute("SET LOCAL search_path = pg_catalog, pg_temp")
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("SET LOCAL statement_timeout = '30s'")
        # One fixed lock for this small native daily namespace, all sources.
        # Next statement takes its snapshot AFTER acquiring this xact lock.
        cur.execute("SELECT pg_advisory_xact_lock(721603, 1)")
        cur.execute(POLICY_CHECK, params)
        require(cur.fetchone() is None, "persisted_source_rank_mismatch")
        cur.execute(
            """SELECT batch_digest, source_priority FROM
            mdf_tdx_daily.source_refresh_batches
            WHERE source=%(source)s AND batch_id=%(batch)s""",
            params,
        )
        previous = cur.fetchall()
        if previous:
            scopes = len({b.key[:2] for b in batch.bars})
            require(
                len(previous) == scopes
                and all(row == (batch.fingerprint, priority) for row in previous),
                "batch_identity_reuse",
            )
            return Counts(replay=True)
        cur.execute(sql_file("002_stage.sql"))
        with cur.copy(COPY) as copy:
            for bar in batch.bars:
                copy.write_row(
                    (*bar.copy_row(), batch.request_index(bar), batch.received_at(bar))
                )
        cur.execute(RANGE_CHECK, params)
        require(cur.fetchone() is None, "sql_batch_range_invalid")
        cur.execute(DUPLICATES)
        require(cur.fetchone() is None, "conflicting_duplicate_key")
        cur.execute(sql_file("003_classify.sql"), params)
        cur.execute("""SELECT disposition,count(*) FROM pg_temp.tdx_daily_delta
            GROUP BY disposition""")
        counts = dict(cur.fetchall())
        cur.execute(sql_file("004_apply.sql"), params)
        new, changed, logged = cur.fetchone()
        require(
            (new, changed, logged)
            == (
                counts.get("NEW", 0),
                counts.get("CHANGED", 0),
                counts.get("CHANGED", 0),
            ),
            "atomic_write_count_mismatch",
        )
        cur.execute(sql_file("005_refresh.sql"), params)
        return Counts(
            new, counts.get("UNCHANGED", 0), changed, counts.get("BLOCKED", 0)
        )


def cold_read(conn, *, market, instrument, day_start, day_end):
    """Bounded current facts; projections remain source native integers."""
    require(type(market) is int and market in (0, 1), "market_invalid")
    require(
        type(instrument) is str and re.fullmatch(r"[0-9]{6}", instrument),
        "instrument_invalid",
    )
    require(
        type(day_start) is date
        and type(day_end) is date
        and 0 <= (day_end - day_start).days <= 3660,
        "read_range_invalid",
    )
    with conn.cursor() as cur:
        cur.execute(
            """SELECT trading_day,open,high,low,close,volume,amount,
            units,value_digest,value_source,source_observed_at,value_changed_at
            FROM mdf_tdx_daily.bars_daily_current
            WHERE market=%s AND instrument=%s AND period='1d'
                AND basis='native_projection' AND trading_day BETWEEN %s AND %s
            ORDER BY trading_day LIMIT 3661""",
            (market, instrument, day_start, day_end),
        )
        return cur.fetchall()


@dataclass(frozen=True)
class AdjustmentFactor:
    """Independent future input model; never admitted without a named binding."""

    market: int
    instrument: str
    effective_day: date
    numerator: int
    denominator: int
    source: str
    revision: str

    def admit(self, *, trusted_sources=()):
        require(
            self.source in trusted_sources and bool(self.source),
            "trusted_factor_source_required",
        )
        require(
            type(self.numerator) is int
            and self.numerator > 0
            and type(self.denominator) is int
            and self.denominator > 0,
            "positive_rational_factor_required",
        )
        Bar(self.market, self.instrument, self.effective_day, (0, 0, 0, 0, 0, 0))
        require(
            type(self.revision) is str and TOKEN.fullmatch(self.revision),
            "factor_revision_required",
        )
        return self


def winning_authority(candidates):
    # Same rank, different sources: latest confirmation wins, then source name.
    # A different equal-rank source cannot correct values, even with a later clock.
    return max(
        sorted(candidates, key=lambda a: a[1]),
        key=lambda a: (a[0], datetime.fromisoformat(a[2])),
        default=None,
    )


@dataclass
class OfflineCurrent:
    """Pure reference model, with JSON cold rebuild; NOT a PG execution engine."""

    current: dict = field(default_factory=dict)
    corrections: list = field(default_factory=list)
    receipts: dict = field(default_factory=dict)
    confirmations: dict = field(default_factory=dict)

    def apply(self, batch):
        require(type(batch) is Batch, "batch_required")
        rank = Batch.validate(batch)
        # Check all reference state before replay or mutation. A forged descriptor,
        # snapshot or persisted numerical rank cannot alter code-bound authority.
        for record in self.current.values():
            require(
                record["priority"] == source_rank(record["source"]),
                "persisted_source_rank_mismatch",
            )
        for authority in self.confirmations.values():
            require(
                authority[0] == source_rank(authority[1]),
                "persisted_source_rank_mismatch",
            )
        for identity, receipt in self.receipts.items():
            require(
                receipt[1] == source_rank(identity.split("/", 1)[0]),
                "persisted_source_rank_mismatch",
            )
        fingerprint = batch.fingerprint
        identity = f"{batch.source}/{batch.batch_id}"
        previous = self.receipts.get(identity)
        if previous:
            require(previous == [fingerprint, rank], "batch_identity_reuse")
            return Counts(replay=True)
        unique = {}
        for bar in batch.bars:
            key = json.dumps(bar.key)
            require(
                key not in unique or unique[key].values == bar.values,
                "conflicting_duplicate_key",
            )
            unique[key] = bar
        # Validate everything before mutation (reference atomicity).
        result = {"new": 0, "unchanged": 0, "changed": 0, "blocked": 0}
        for key, bar in sorted(unique.items()):
            old = self.current.get(key)
            candidates = []
            if old:
                candidates.append([old["priority"], old["source"], old["observed"]])
            if key in self.confirmations:
                candidates.append(self.confirmations[key])
            authority = winning_authority(candidates)
            received = batch.received_at(bar)
            can_change = (
                not authority
                or rank > authority[0]
                or (
                    rank == authority[0]
                    and batch.source == authority[1]
                    and received > datetime.fromisoformat(authority[2])
                )
            )
            state = (
                "new"
                if old is None
                else (
                    "unchanged"
                    if old["values"] == list(bar.values)
                    else ("changed" if can_change else "blocked")
                )
            )
            result[state] += 1
            if state in {"new", "changed"}:
                record = {
                    "values": list(bar.values),
                    "digest": bar.value_digest,
                    "source": batch.source,
                    "priority": rank,
                    "observed": received.isoformat(),
                    "changed_at": batch.admitted_at.isoformat(),
                    "batch": batch.batch_id,
                }
                if state == "changed":
                    self.corrections.append(
                        {
                            "key": key,
                            "old": old,
                            "new": record,
                            "reason": "vendor_correction_or_unknown",
                        }
                    )
                self.current[key] = record
            if state != "blocked":
                self.confirmations[key] = winning_authority(
                    [*candidates, [rank, batch.source, received.isoformat()]]
                )
        self.receipts[identity] = [fingerprint, rank]
        return Counts(**result)

    def snapshot(self):
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def rebuild(cls, snapshot):
        return cls(**json.loads(snapshot))
