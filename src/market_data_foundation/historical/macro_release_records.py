"""Evidence-backed release reads beside the existing continuous-value tables.

No provider IO or DDL on construction/read. Only explicit prepare/import mutate.
Event claims do not insert or relabel continuous economic revisions.
"""

from __future__ import annotations

import copy
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
from market_data_foundation.historical.cpi_timeseries_store import (
    CpiStorageError,
    InMemoryTimeSeriesStore,
    PostgresTimeSeriesStore,
    canonical,
    digest,
)

PARSER = "bls-employment-release-narrative-v1"
MONTHS = (
    "January February March April May June July August September "
    "October November December"
).split()
NUMBER = r"[+−-]?\d[\d,]*"
ARCHIVE = re.compile(r"/news.release/archives/empsit_(\d{2})(\d{2})(\d{4})\.(htm|txt)")


def instant(value):
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(UTC)
    except ValueError, TypeError:
        raise CpiTimeSeriesError("release_as_of_invalid") from None


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "br", "div", "h1", "h2", "pre", "td", "tr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def archive_date(url):
    try:
        parts = urlsplit(url)
        match = ARCHIVE.fullmatch(parts.path)
        if (
            parts.scheme != "https"
            or parts.netloc != "www.bls.gov"
            or parts.query
            or parts.fragment
            or match is None
        ):
            raise ValueError
        month, day, year, _ = match.groups()
        return date(int(year), int(month), int(day))
    except ValueError, TypeError:
        raise CpiTimeSeriesError("employment_official_archive_url_required") from None


def _jobs(value):
    return str(Decimal(value.replace(",", "").replace("−", "-")) / 1000)


def release_surprise(actual, consensus, *, event_id, actual_at):
    """Comparable, evidenced pre-release consensus; no prior-value gate.

    No consensus provider is wired here. Accepts already qualified snapshots,
    not free-form HTTP/CLI forecasts.
    """
    if actual is None or actual.get("role") != "actual":
        return None, ["event_actual_unconfirmed"]
    if consensus is None:
        return None, ["consensus_source_unavailable"]
    keys = ("series_id", "reference_month", "statistic", "unit", "adjustment")
    if consensus.get("event_id") != event_id or any(
        consensus.get(key) != actual.get(key) for key in keys
    ):
        return None, ["consensus_scope_mismatch"]
    if not consensus.get("source") or not consensus.get("method"):
        return None, ["consensus_provenance_missing"]
    if actual_at is None:
        return None, ["actual_publication_instant_not_proven"]
    try:
        released = instant(actual_at)
        if any(
            instant(consensus.get(key)) >= released
            for key in ("source_available_at", "frozen_at")
        ):
            return None, ["consensus_not_frozen_before_release"]
        actual_value = Decimal(actual["value"])
        expected = Decimal(consensus["value"])
        if not actual_value.is_finite() or not expected.is_finite():
            raise ValueError
        return str(actual_value - expected), []
    except CpiTimeSeriesError, InvalidOperation, ValueError, TypeError, KeyError:
        return None, ["consensus_value_or_time_evidence_invalid"]


def parse_employment_release(raw, source_url):
    """Bounded official archive narrative adapter; unsupported prose stays missing.

    Archive filename proves a source version DATE, not actual publication instant.
    Embargo instructions are deliberately not interpreted as actual release time.
    """
    released = archive_date(source_url)
    if type(raw) is not bytes or not 0 < len(raw) <= 2_000_000:
        raise CpiTimeSeriesError("employment_release_material_size_invalid")
    try:
        parser = _Text()
        parser.feed(raw.decode("utf-8-sig"))
        text = re.sub(r"\s+", " ", "".join(parser.parts)).strip()
    except UnicodeError, ValueError:
        raise CpiTimeSeriesError("employment_release_material_invalid") from None
    title = re.search(
        r"THE EMPLOYMENT SITUATION\s*[-–—]{1,2}\s*("
        + "|".join(MONTHS)
        + r")\s+(\d{4})",
        text,
        re.I,
    )
    if title is None:
        raise CpiTimeSeriesError("employment_release_reference_month_missing")
    year = int(title[2])
    month = next(
        i for i, name in enumerate(MONTHS, 1) if name.lower() == title[1].lower()
    )
    reference = f"{year:04d}-{month:02d}"
    if not date(year, month, 1) < released:
        raise CpiTimeSeriesError("employment_release_reference_date_conflict")
    event_id = "bls:employment-situation:" + released.isoformat()
    claims = []

    def claim(value, role, ref, match):
        scope = {
            "series_id": "CES0000000001",
            "statistic": "net_change_1m",
            "reference_month": ref,
            "unit": "thousand_jobs",
            "adjustment": "sa",
        }
        claims.append(
            {
                **scope,
                "role": role,
                "value": _jobs(value),
                # Content identity is NOT a continuous revision or official vintage.
                "value_identity": digest(
                    canonical({**scope, "value": _jobs(value)}).encode()
                ),
                "continuous_version": None,
                "locator": {
                    "kind": "normalized_text_span",
                    "start": match.start(),
                    "end": match.end(),
                    "excerpt": match[0],
                },
            }
        )

    current = re.search(
        r"Total nonfarm payroll employment\s+(increased|rose|declined|decreased|fell)"
        r"\s+by\s+(" + NUMBER + r")\s+in\s+" + MONTHS[month - 1] + r"\b",
        text,
        re.I,
    )
    if current:
        number = current[2]
        if current[1].lower() in {"declined", "decreased", "fell"}:
            number = "-" + number.lstrip("+−-")
        claim(number, "actual", reference, current)
    else:
        current = re.search(
            r"Total nonfarm payroll employment\s+changed little\s+in\s+"
            + MONTHS[month - 1]
            + r"\s*\(("
            + NUMBER
            + r")\)",
            text,
            re.I,
        )
        if current:
            claim(current[1], "actual", reference, current)

    # Bind only the explicitly named NFP revision paragraph, never arbitrary tables.
    paragraph = re.search(
        r"The change in total nonfarm payroll employment for\s+.*?(?:\.(?:\s|$)|$)",
        text,
        re.I,
    )
    if paragraph:
        pattern = (
            r"(?:employment|the change) for\s+(" + "|".join(MONTHS) + r")"
            r"\s+was revised\s+(?:up|down)\s+by\s+"
            + NUMBER
            + r",\s+from\s+("
            + NUMBER
            + r")\s+to\s+("
            + NUMBER
            + r")"
        )
        for revision in re.finditer(pattern, text, re.I):
            if not paragraph.start() <= revision.start() < paragraph.end():
                continue
            prior_month = MONTHS.index(revision[1].title()) + 1
            prior_year = year - (prior_month >= month)
            prior = f"{prior_year:04d}-{prior_month:02d}"
            distance = (year - prior_year) * 12 + month - prior_month
            if distance not in {1, 2}:
                continue
            claim(revision[2], "official_previous", prior, revision)
            claim(revision[3], "prior_revised", prior, revision)
    previous = f"{year - (month == 1):04d}-{(month - 2) % 12 + 1:02d}"
    missing = [
        "actual_publication_instant_not_proven",
        "consensus_source_unavailable",
        "mdf_pre_release_value_requires_proven_release_instant",
        "continuous_version_not_bound_to_material",
        "other_employment_statistics_not_covered_by_nfp_narrative_adapter",
    ]
    if not any(c["role"] == "actual" for c in claims):
        missing.append("nfp_current_net_change_not_found_in_material")
    if not any(
        c["role"] == "prior_revised" and c["reference_month"] == previous
        for c in claims
    ):
        missing.append("nfp_previous_month_revision_not_found_in_material")
    actual = next((claim for claim in claims if claim["role"] == "actual"), None)
    surprise, surprise_missing = release_surprise(
        actual, None, event_id=event_id, actual_at=None
    )
    return {
        "event_id": event_id,
        "family": "employment",
        "reference_month": reference,
        "actual_at": None,
        "source_version": {
            "value": released.isoformat(),
            "precision": "date",
            "basis": "BLS_official_archive_filename",
            "source": "BLS Employment Situation archive",
        },
        "source_url": source_url,
        "parser_revision": PARSER,
        "claims": claims,
        "consensus": None,
        "surprise": surprise,
        "surprise_missing": surprise_missing,
        "missing": missing,
    }


def migration_sql(profile):
    """Additive, explicit operator migration. Caller owns transaction and grants."""
    schema = profile.schema
    return f"""-- Existing schema/tables required. No continuous values change.
SET LOCAL lock_timeout = '5s';
SELECT pg_advisory_xact_lock({profile.lock_id});
CREATE TABLE IF NOT EXISTS {schema}.release_events (
    event_id text PRIMARY KEY,
    family text NOT NULL,
    reference_month text NOT NULL
);
CREATE TABLE IF NOT EXISTS {schema}.release_evidence (
    evidence_id text PRIMARY KEY,
    event_id text NOT NULL REFERENCES {schema}.release_events(event_id),
    raw_sha256 text NOT NULL,
    raw_locator text NOT NULL,
    obtained_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    available_at timestamptz,
    document jsonb NOT NULL,
    CHECK (available_at IS NULL OR available_at >= obtained_at)
);
CREATE INDEX IF NOT EXISTS release_evidence_event_available
    ON {schema}.release_evidence(event_id, available_at);
"""


def unsupported_release_records(family, params, *, now):
    """Honest read contract for an asset whose material adapter is not wired."""
    if set(params) - {"event_id", "as_of", "read_mode", "source_version"}:
        raise CpiTimeSeriesError("release_parameters_unsupported")
    cutoff = instant(params.get("as_of", now))
    mode = params.get("read_mode", "event")
    if mode not in {"event", "source_history"}:
        raise CpiTimeSeriesError("release_read_mode_unsupported")
    if "source_version" in params:
        try:
            value = params["source_version"]
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError, TypeError:
            raise CpiTimeSeriesError("release_source_version_invalid") from None
    return {
        "read_mode": mode,
        "as_of": cutoff.isoformat(),
        "as_of_basis": "available",
        "events": [],
        "missing": [family + "_release_material_adapter_not_implemented"],
        "provider_requests": 0,
        "provider_attempts": 0,
        "persisted": False,
        "history_basis": "named_source_archive_evidence_only_no_latest_fallback",
    }


class ReleaseRecords:
    """Reuse a qualified time-series store's connection/raw port, no fallback."""

    def __init__(self, store):
        if not isinstance(store, (PostgresTimeSeriesStore, InMemoryTimeSeriesStore)):
            raise CpiTimeSeriesError("release_store_binding_unsupported")
        self.store = store
        self.profile = store.profile
        self.schema = store.profile.schema
        self.memory = isinstance(store, InMemoryTimeSeriesStore)

    def prepare(self):
        if self.memory:
            if not hasattr(self.store, "_release_evidence"):
                self.store._release_evidence = {}
            return
        with self.store.connection.transaction():
            self.store.connection.execute(migration_sql(self.profile))

    def import_employment(
        self, raw, source_url, *, material_format="retained_document"
    ):
        if self.profile.key != "employment":
            raise CpiTimeSeriesError("employment_release_profile_required")
        obtained_at = instant(self.store._clock())
        doc = parse_employment_release(raw, source_url)
        if material_format not in {
            "retained_document",
            "original_http_body",
            "official_summary_projection",
        }:
            raise CpiTimeSeriesError("release_material_format_unsupported")
        doc["retention"] = {
            "kind": material_format,
            "exact_original_retained": material_format == "original_http_body",
            "acquisition_method": "explicit_operator_import",
            "coverage": (
                "summary_only_no_statistical_tables"
                if material_format == "official_summary_projection"
                else "caller_supplied_document"
            ),
        }
        sha = digest(raw)
        eid = digest(
            canonical(
                [doc["event_id"], sha, doc["parser_revision"], doc["retention"]]
            ).encode()
        )
        with self.store.operation():
            if self.memory:
                if not hasattr(self.store, "_release_evidence"):
                    raise CpiTimeSeriesError("release_schema_not_prepared")
                rows = self.store._release_evidence
                if any(
                    r["event_id"] == doc["event_id"]
                    and r["reference_month"] != doc["reference_month"]
                    for r in rows.values()
                ):
                    raise CpiTimeSeriesError("release_event_reference_conflict")
                if eid not in rows:
                    locator = self._retain(raw)
                    available = instant(self.store._clock())
                    if available < obtained_at:
                        raise CpiTimeSeriesError("release_availability_time_invalid")
                    rows[eid] = {
                        **doc,
                        "evidence_id": eid,
                        "raw_sha256": sha,
                        "raw_locator": locator,
                        "obtained_at": obtained_at.isoformat(),
                        "available_at": available.isoformat(),
                    }
                return copy.deepcopy(rows[eid])
            connection = self.store.connection
            try:
                # Verify schema BEFORE raw writes. GET never invokes this path.
                connection.execute(
                    f"SELECT evidence_id FROM {self.schema}.release_evidence LIMIT 0"
                )
                locator = self._retain(raw)
                with connection.transaction():
                    connection.execute(
                        f"INSERT INTO {self.schema}.release_events VALUES (%s,%s,%s) "
                        "ON CONFLICT DO NOTHING",
                        (doc["event_id"], doc["family"], doc["reference_month"]),
                    )
                    existing = connection.execute(
                        f"SELECT reference_month FROM {self.schema}.release_events "
                        "WHERE event_id=%s",
                        (doc["event_id"],),
                    ).fetchone()
                    if existing[0] != doc["reference_month"]:
                        raise CpiTimeSeriesError("release_event_reference_conflict")
                    connection.execute(
                        f"INSERT INTO {self.schema}.release_evidence "
                        "(evidence_id,event_id,raw_sha256,raw_locator,document) "
                        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (eid, doc["event_id"], sha, locator, Jsonb(doc)),
                    )
                # Conservative post-commit availability; interrupted imports stay
                # invisible until an explicit retry confirms them at its real time.
                connection.execute(
                    f"UPDATE {self.schema}.release_evidence "
                    "SET available_at=clock_timestamp() "
                    "WHERE evidence_id=%s AND available_at IS NULL",
                    (eid,),
                )
                return next(r for r in self._rows(None) if r["evidence_id"] == eid)
            except psycopg.errors.UndefinedTable:
                raise CpiTimeSeriesError("release_schema_not_prepared") from None
            except psycopg.Error:
                raise CpiTimeSeriesError("release_storage_failed") from None

    def _retain(self, raw):
        if self.store.raw_store is None:
            raise CpiTimeSeriesError("release_raw_store_required")
        try:
            return self.store.raw_store.put_verified(raw)
        except CpiStorageError as error:
            raise CpiTimeSeriesError(error.classification) from None

    def _rows(self, cutoff):
        if self.memory:
            rows = getattr(self.store, "_release_evidence", {}).values()
            return [
                copy.deepcopy(r)
                for r in rows
                if cutoff is None or instant(r["available_at"]) <= cutoff
            ]
        try:
            rows = self.store.connection.execute(
                "SELECT evidence_id,raw_sha256,raw_locator,"
                "obtained_at,available_at,document "
                f"FROM {self.schema}.release_evidence WHERE available_at IS NOT NULL "
                "AND (%s::timestamptz IS NULL OR available_at <= %s::timestamptz)",
                (cutoff, cutoff),
            ).fetchall()
            return [
                {
                    **doc,
                    "evidence_id": eid,
                    "raw_sha256": sha,
                    "raw_locator": locator,
                    "obtained_at": obtained.isoformat(),
                    "available_at": available.isoformat(),
                }
                for eid, sha, locator, obtained, available, doc in rows
            ]
        except psycopg.errors.UndefinedTable:
            raise CpiTimeSeriesError("release_schema_not_prepared") from None
        except psycopg.Error:
            raise CpiTimeSeriesError("release_storage_failed") from None

    def read(
        self, *, event_id=None, as_of=None, read_mode="event", source_version=None
    ):
        cutoff = instant(as_of) if as_of is not None else instant(self.store._clock())
        if read_mode not in {"event", "source_history"}:
            raise CpiTimeSeriesError("release_read_mode_unsupported")
        if source_version is not None:
            try:
                if date.fromisoformat(source_version).isoformat() != source_version:
                    raise ValueError
            except ValueError, TypeError:
                raise CpiTimeSeriesError("release_source_version_invalid") from None
        try:
            rows = self._rows(cutoff)
            unavailable = []
        except CpiTimeSeriesError as error:
            if error.classification != "release_schema_not_prepared":
                raise
            rows, unavailable = [], [error.classification]
        groups = {}
        for row in rows:
            if event_id is not None and row["event_id"] != event_id:
                continue
            if (
                source_version is not None
                and row["source_version"]["value"] != source_version
            ):
                continue
            groups.setdefault(row["event_id"], []).append(row)
        events = []
        for identity, evidence in groups.items():
            evidence.sort(key=lambda r: (instant(r["available_at"]), r["evidence_id"]))
            current = evidence[-1]
            events.append(
                {
                    **current,
                    "evidence": evidence,
                    "state": "confirmed"
                    if any(c["role"] == "actual" for c in current["claims"])
                    else "unknown",
                }
            )
        events.sort(
            key=lambda r: (r["source_version"]["value"], r["event_id"]), reverse=True
        )
        if not events:
            unavailable.append("official_release_evidence_not_available_at_cutoff")
        if self.profile.key != "employment":
            unavailable.append(
                self.profile.key + "_release_material_adapter_not_implemented"
            )
        return {
            "read_mode": read_mode,
            "as_of": cutoff.isoformat(),
            "as_of_basis": "available",
            "events": events,
            "missing": unavailable,
            "provider_requests": 0,
            "provider_attempts": 0,
            "persisted": False,
            "history_basis": "named_source_archive_evidence_only_no_latest_fallback",
        }
