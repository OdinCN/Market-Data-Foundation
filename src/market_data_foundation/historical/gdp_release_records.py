"""Quarterly evidence imports and reads, separate from value revisions."""

import copy
from datetime import date, datetime
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from market_data_foundation.historical.cpi_timeseries_store import canonical, digest
from market_data_foundation.historical.gdp_contract import validate_range
from market_data_foundation.historical.gdp_release_material import (
    ESTIMATES,
    parse_release,
)
from market_data_foundation.historical.gdp_store import (
    GdpStorageError,
    InMemoryGdpStore,
    PostgresGdpStore,
    instant,
    raw_locator,
)
from market_data_foundation.historical.gdp_timeseries import GdpTimeSeriesError
from market_data_foundation.historical.macro_release_records import temporal_bounds


def _error(reason):
    return GdpTimeSeriesError("gdp_release_" + reason)


def _cutoff(value):
    try:
        return instant(
            datetime.fromisoformat(value) if isinstance(value, str) else value
        )
    except ValueError, TypeError, GdpStorageError:
        raise _error("cutoff_invalid") from None


class GdpReleaseRecords:
    def __init__(self, store):
        if not isinstance(store, (InMemoryGdpStore, PostgresGdpStore)):
            raise _error("store_binding_unsupported")
        self.store = store
        self.memory = isinstance(store, InMemoryGdpStore)

    def prepare(self):
        with self.store.operation():
            if self.memory:
                if not hasattr(self.store, "_gdp_release_evidence"):
                    self.store._gdp_release_evidence = {}
                return
            schema = self.store.schema
            try:
                with self.store.connection.transaction():
                    # Never create the value schema on a release read/import.
                    self.store.connection.execute(
                        f"SELECT 1 FROM {schema}.revisions LIMIT 0"
                    )
                    self.store.connection.execute(f"""
                        CREATE TABLE IF NOT EXISTS {schema}.release_events (
                            event_id text PRIMARY KEY,
                            reference_quarter text NOT NULL
                                CHECK(reference_quarter ~ '^[0-9]{{4}}-Q[1-4]$'),
                            estimate_kind text NOT NULL
                                CHECK(estimate_kind IN ('advance','second','third')),
                            source_date date NOT NULL
                        )
                    """)
                    self.store.connection.execute(f"""
                        CREATE TABLE IF NOT EXISTS {schema}.release_evidence (
                            evidence_id text PRIMARY KEY,
                            event_id text NOT NULL
                                REFERENCES {schema}.release_events(event_id),
                            raw_sha256 text NOT NULL,
                            raw_locator text NOT NULL,
                            obtained_at timestamptz NOT NULL,
                            available_at timestamptz CHECK(available_at >= obtained_at),
                            document jsonb NOT NULL
                        )
                    """)
                    self.store.connection.execute(f"""
                        CREATE INDEX IF NOT EXISTS gdp_release_event_available
                        ON {schema}.release_evidence(event_id, available_at)
                    """)
            except psycopg.Error:
                raise _error("schema_failed") from None

    def _rows(self, cutoff):
        if self.memory:
            if not hasattr(self.store, "_gdp_release_evidence"):
                raise _error("schema_not_prepared")
            return [
                copy.deepcopy(row)
                for row in self.store._gdp_release_evidence.values()
                if row["available_at"] is not None
                and (cutoff is None or _cutoff(row["available_at"]) <= cutoff)
            ]
        try:
            rows = self.store.connection.execute(
                "SELECT evidence_id,raw_sha256,raw_locator,"
                "obtained_at,available_at,document "
                f"FROM {self.store.schema}.release_evidence "
                "WHERE available_at IS NOT NULL "
                "AND (%s::timestamptz IS NULL OR available_at<=%s::timestamptz)",
                (cutoff, cutoff),
            ).fetchall()
            return [
                dict(
                    doc,
                    evidence_id=eid,
                    raw_sha256=sha,
                    raw_locator=locator,
                    obtained_at=obtained.isoformat(),
                    available_at=available.isoformat(),
                )
                for eid, sha, locator, obtained, available, doc in rows
            ]
        except psycopg.errors.UndefinedTable:
            raise _error("schema_not_prepared") from None
        except psycopg.Error:
            raise _error("read_failed") from None

    def import_material(self, raw, source_url, *, material_format="retained_document"):
        obtained = instant(self.store.clock())
        doc = parse_release(raw, source_url)
        if material_format not in {"retained_document", "original_http_body"}:
            raise _error("retention_format_invalid")
        if obtained < temporal_bounds(doc["source_version"])[0]:
            raise _error("material_date_after_acquisition")
        doc["retention"] = dict(
            kind=material_format,
            exact_original_retained=material_format == "original_http_body",
            acquisition_method="explicit_operator_import",
            coverage="GDP_summary_and_explicit_prior_quarter_revision_tables",
        )
        sha = digest(raw)
        eid = digest(
            canonical(
                [doc["event_id"], sha, doc["parser_revision"], doc["retention"]]
            ).encode()
        )
        with self.store.operation():
            known = next((r for r in self._rows(None) if r["evidence_id"] == eid), None)
            if known is not None:
                return known
            try:
                locator = self.store.raw_store.put_verified(raw)
                if locator != raw_locator(sha):
                    raise _error("raw_locator_mismatch")
            except GdpStorageError as error:
                raise GdpTimeSeriesError(error.classification) from None
            if self.memory:
                rows = self.store._gdp_release_evidence
                if eid not in rows:
                    rows[eid] = dict(
                        doc,
                        evidence_id=eid,
                        raw_sha256=sha,
                        raw_locator=locator,
                        obtained_at=obtained.isoformat(),
                        available_at=None,
                    )
                available = instant(self.store.clock())
                if available < _cutoff(rows[eid]["obtained_at"]):
                    raise _error("availability_confirmation_unknown")
                rows[eid]["available_at"] = available.isoformat()
                return copy.deepcopy(rows[eid])
            connection, schema = self.store.connection, self.store.schema
            committed = False
            try:
                with connection.transaction():
                    connection.execute(
                        f"INSERT INTO {schema}.release_events VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT DO NOTHING",
                        (
                            doc["event_id"],
                            doc["reference_quarter"],
                            doc["estimate_kind"],
                            date.fromisoformat(doc["source_version"]["value"]),
                        ),
                    )
                    connection.execute(
                        f"INSERT INTO {schema}.release_evidence "
                        "(evidence_id,event_id,raw_sha256,raw_locator,"
                        "obtained_at,document) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (eid, doc["event_id"], sha, locator, obtained, Jsonb(doc)),
                    )
                committed = True
                # After commit only. An interrupted confirmation remains invisible;
                # explicit replay confirms at the replay's real clock, never backdates.
                connection.execute(
                    f"UPDATE {schema}.release_evidence "
                    "SET available_at=clock_timestamp() "
                    "WHERE evidence_id=%s AND available_at IS NULL",
                    (eid,),
                )
                return next(r for r in self._rows(None) if r["evidence_id"] == eid)
            except Exception:
                raise _error(
                    "availability_confirmation_unknown"
                    if committed
                    else "import_outcome_unknown"
                ) from None

    def read(
        self,
        *,
        event_id=None,
        reference_quarter=None,
        source_version=None,
        as_of=None,
        read_mode="event",
    ):
        cutoff = _cutoff(as_of) if as_of is not None else instant(self.store.clock())
        if read_mode not in {"event", "source_history"}:
            raise _error("read_mode_invalid")
        if reference_quarter is not None:
            try:
                validate_range(reference_quarter, reference_quarter)
            except Exception:
                raise _error("reference_quarter_invalid") from None
        if source_version is not None:
            try:
                if date.fromisoformat(source_version).isoformat() != source_version:
                    raise ValueError
            except TypeError, ValueError:
                raise _error("source_version_invalid") from None
        if event_id is not None and (
            not isinstance(event_id, str) or len(event_id) > 160
        ):
            raise _error("event_id_invalid")
        with self.store.read_snapshot():
            try:
                rows = self._rows(cutoff)
            except GdpTimeSeriesError as error:
                if error.classification != "gdp_release_schema_not_prepared":
                    raise
                return dict(
                    events=[],
                    as_of=cutoff.isoformat(),
                    read_mode=read_mode,
                    missing=[error.classification],
                )
        rows = [
            r
            for r in rows
            if (event_id is None or r["event_id"] == event_id)
            and (
                reference_quarter is None
                or reference_quarter in r["reference_quarters"]
            )
            and (
                source_version is None or r["source_version"]["value"] == source_version
            )
        ]
        rows.sort(
            key=lambda r: (
                r["source_version"]["value"],
                r["available_at"],
                r["evidence_id"],
            ),
            reverse=True,
        )
        if read_mode == "source_history":
            return dict(
                events=rows, as_of=cutoff.isoformat(), read_mode=read_mode, missing=[]
            )
        grouped = {}
        for row in rows:
            grouped.setdefault(row["event_id"], []).append(row)
        events = []
        for proofs in grouped.values():
            event = copy.deepcopy(proofs[0])
            scopes = {}
            for proof in proofs:
                for claim in proof["claims"]:
                    key = tuple(
                        claim[k]
                        for k in (
                            "series_id",
                            "reference_quarter",
                            "statistic",
                            "role",
                            "estimate_kind",
                        )
                    )
                    scopes.setdefault(key, []).append((claim, proof["evidence_id"]))
            event["claims"], event["conflicts"] = [], []
            for key, values in scopes.items():
                claim = copy.deepcopy(values[0][0])
                claim["evidence_ids"] = sorted({eid for _, eid in values})
                if len({c["value"] for c, _ in values}) > 1:
                    claim.update(
                        value=None, value_identity=None, confirmation_state="conflict"
                    )
                    event["conflicts"].append(
                        dict(scope=list(key), reason="official_evidence_conflict")
                    )
                event["claims"].append(claim)
            actual = next(c for c in event["claims"] if c["role"] == "actual")
            prior = sorted(
                (c for c in event["claims"] if c["role"] == "previous_estimate"),
                key=lambda c: ESTIMATES.index(c["estimate_kind"]),
            )
            previous = prior[-1] if prior else None
            event["previous_estimate_revision_pp"] = (
                str(Decimal(actual["value"]) - Decimal(previous["value"]))
                if previous
                and actual["confirmation_state"] == "confirmed"
                and previous["confirmation_state"] == "confirmed"
                else None
            )
            event["evidence_ids"] = [p["evidence_id"] for p in proofs]
            event["surprise"] = None
            event["surprise_missing"] = (
                ["consensus_source_unavailable"]
                if actual["confirmation_state"] == "confirmed"
                else ["event_actual_unconfirmed", "consensus_source_unavailable"]
            )
            events.append(event)
        return dict(
            events=events, as_of=cutoff.isoformat(), read_mode=read_mode, missing=[]
        )

    def link_result(self, result):
        result = copy.deepcopy(result)
        events = self.read(as_of=result.get("as_of"))["events"]
        for point in result.get("points", []):
            point["related_releases"] = [
                dict(
                    event_id=e["event_id"],
                    source_version=e["source_version"],
                    estimate_kind=e["estimate_kind"],
                    roles=sorted(
                        {
                            c["role"]
                            for c in e["claims"]
                            if c["series_id"] == point["series_id"]
                            and c["reference_quarter"] == point["quarter"]
                        }
                    ),
                    association="scope_only",
                    continuous_version_bound=False,
                )
                for e in events
                if any(
                    c["series_id"] == point["series_id"]
                    and c["reference_quarter"] == point["quarter"]
                    for c in e["claims"]
                )
            ]
        return result
