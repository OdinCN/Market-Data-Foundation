"""Evidence-backed release reads beside the existing continuous-value tables.

No provider IO or DDL on construction/read. Only explicit prepare/import mutate.
Event claims do not insert or relabel continuous economic revisions.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def temporal_bounds(value):
    """Evidence bounds used for ordering only; the source precision stays intact."""
    if isinstance(value, str):
        at = instant(value)
        return at, at
    if not isinstance(value, dict):
        raise CpiTimeSeriesError("release_time_evidence_invalid")
    if value.get("precision") == "date":
        day = date.fromisoformat(value["value"])
        timezone = value.get("timezone")
        if not timezone:
            raise CpiTimeSeriesError("release_date_timezone_missing")
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError, ValueError, TypeError:
            raise CpiTimeSeriesError("release_date_timezone_invalid") from None
        return (
            datetime.combine(day, datetime.min.time(), zone).astimezone(UTC),
            datetime.combine(
                day + timedelta(days=1), datetime.min.time(), zone
            ).astimezone(UTC),
        )
    if value.get("precision") == "interval":
        start, end = instant(value["start"]), instant(value["end"])
        if start > end:
            raise CpiTimeSeriesError("release_time_evidence_invalid")
        return start, end
    return temporal_bounds(value["value"])


def proven_before(value, lower_bound):
    upper = temporal_bounds(value)[1]
    return (
        upper <= lower_bound
        if isinstance(value, dict) and value.get("precision") == "date"
        else upper < lower_bound
    )


def official_event_time(event):
    """Normalize legacy official US date proof without changing stored evidence."""
    value = (
        event.get("actual_at")
        or event.get("event_time_evidence")
        or event["source_version"]
    )
    if isinstance(value, dict) and not value.get("timezone"):
        version = event["source_version"]
        if version.get("basis") in {
            "BLS_official_archive_filename",
            "official_material_release_date",
        }:
            official_url = event.get("source_url", "")
            if official_url.startswith(
                (
                    "https://www.bls.gov/",
                    "https://www.bea.gov/",
                    "https://www.census.gov/",
                )
            ):
                value = dict(value, timezone="America/New_York")
    return value


def release_surprise(
    actual, consensus, *, event_id, actual_at=None, event_time_evidence=None
):
    """Comparable, evidenced pre-release consensus; no prior-value gate.

    No consensus provider is wired here. Accepts already qualified snapshots,
    not free-form HTTP/CLI forecasts.
    """
    if (
        actual is None
        or actual.get("role") != "actual"
        or actual.get("confirmation_state") != "confirmed"
    ):
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
    if actual_at is None and event_time_evidence is None:
        return None, ["actual_publication_order_not_proven"]
    try:
        released = temporal_bounds(actual_at or event_time_evidence)[0]
        if any(
            not proven_before(consensus.get(key), released)
            for key in ("source_available_at", "frozen_at")
        ):
            return None, ["consensus_not_frozen_before_release"]
        if (
            temporal_bounds(consensus["source_available_at"])[0]
            > temporal_bounds(consensus["frozen_at"])[1]
        ):
            return None, ["consensus_freeze_precedes_source_availability"]
        actual_value = Decimal(str(actual["value"]))
        expected = Decimal(str(consensus["value"]))
        if not actual_value.is_finite() or not expected.is_finite():
            raise ValueError
        return str(actual_value - expected), []
    except CpiTimeSeriesError as error:
        if error.classification in {
            "release_date_timezone_missing",
            "release_date_timezone_invalid",
        }:
            return None, [error.classification]
        return None, ["consensus_value_or_time_evidence_invalid"]
    except InvalidOperation, ValueError, TypeError, KeyError:
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
ALTER TABLE {schema}.release_events ADD COLUMN IF NOT EXISTS reference_months jsonb;
UPDATE {schema}.release_events SET reference_months=jsonb_build_array(reference_month)
    WHERE reference_months IS NULL;
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
CREATE TABLE IF NOT EXISTS {schema}.release_annotations (
    record_id text PRIMARY KEY,
    event_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('consensus','association')),
    obtained_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    available_at timestamptz,
    document jsonb NOT NULL,
    CHECK (available_at IS NULL OR available_at >= obtained_at)
);
CREATE INDEX IF NOT EXISTS release_annotations_event_available
    ON {schema}.release_annotations(event_id, available_at);
CREATE INDEX IF NOT EXISTS release_evidence_event_available
    ON {schema}.release_evidence(event_id, available_at);
"""


class ReleaseRecords:
    """Reuse a qualified time-series store's connection/raw port, no fallback."""

    def __init__(self, store):
        from market_data_foundation.historical.retail_timeseries_store import (
            InMemoryRetailStore,
            PostgresRetailStore,
        )

        if not isinstance(
            store,
            (
                PostgresTimeSeriesStore,
                InMemoryTimeSeriesStore,
                InMemoryRetailStore,
                PostgresRetailStore,
            ),
        ):
            raise CpiTimeSeriesError("release_store_binding_unsupported")
        self.store = store
        self.profile = getattr(
            store,
            "profile",
            SimpleNamespace(
                key="retail",
                schema=getattr(store, "schema", "mdf_retail_timeseries_dev_r1"),
                lock_id=78328741642028105,
            ),
        )
        self.schema = self.profile.schema
        self.memory = isinstance(store, (InMemoryTimeSeriesStore, InMemoryRetailStore))
        self._version_snapshot = None

    @property
    def clock(self):
        return getattr(self.store, "_clock", None) or getattr(self.store, "clock", None)

    def _operation(self):
        return (
            self.store.operation()
            if hasattr(self.store, "operation")
            else getattr(self.store, "lock", nullcontext())
        )

    def prepare(self):
        if self.memory:
            if not hasattr(self.store, "_release_evidence"):
                self.store._release_evidence = {}
            if not hasattr(self.store, "_release_annotations"):
                self.store._release_annotations = {}
            return
        with self.store.connection.transaction():
            self.store.connection.execute(migration_sql(self.profile))

    def import_employment(
        self, raw, source_url, *, material_format="retained_document", **options
    ):
        if self.profile.key != "employment":
            raise CpiTimeSeriesError("employment_release_profile_required")
        return self.import_material(
            raw, source_url, material_format=material_format, **options
        )

    def import_material(
        self,
        raw,
        source_url,
        *,
        material_format="retained_document",
        acquisition_method="explicit_operator_import",
        supersedes_evidence=None,
    ):
        from market_data_foundation.historical.macro_release_material import (
            parse_material,
        )

        obtained_at = instant(self.clock())
        doc = parse_material(raw, source_url, self.profile.key)
        if material_format not in {
            "retained_document",
            "original_http_body",
            "official_summary_projection",
            "official_selected_projection",
        }:
            raise CpiTimeSeriesError("release_material_format_unsupported")
        if supersedes_evidence is not None:
            previous = next(
                (
                    r
                    for r in self._rows(None)
                    if r["evidence_id"] == supersedes_evidence
                ),
                None,
            )
            if previous is None or previous["event_id"] != doc["event_id"]:
                raise CpiTimeSeriesError("release_correction_evidence_invalid")
            doc["supersedes_evidence"] = supersedes_evidence
        doc["retention"] = {
            "kind": material_format,
            "exact_original_retained": material_format == "original_http_body",
            "acquisition_method": acquisition_method,
            "coverage": "summary_only_no_statistical_tables"
            if material_format == "official_summary_projection"
            else "selected_official_material"
            if material_format == "official_selected_projection"
            else "caller_supplied_document",
        }
        sha = digest(raw)
        eid = digest(
            canonical(
                [
                    doc["event_id"],
                    sha,
                    doc["parser_revision"],
                    doc["retention"],
                    supersedes_evidence,
                ]
            ).encode()
        )
        with self._operation():
            if self.memory:
                if not hasattr(self.store, "_release_evidence"):
                    raise CpiTimeSeriesError("release_schema_not_prepared")
                rows = self.store._release_evidence
                if eid not in rows:
                    locator = self._retain(raw)
                    available = instant(self.clock())
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
                        (
                            f"INSERT INTO {self.schema}.release_events "
                            "(event_id,family,reference_month,reference_months) "
                            "VALUES "
                            f"(%s,%s,%s,%s) "
                            "ON CONFLICT DO NOTHING"
                        ),
                        (
                            doc["event_id"],
                            doc["family"],
                            doc["reference_month"],
                            Jsonb(doc["reference_months"]),
                        ),
                    )
                    connection.execute(
                        (
                            f"UPDATE {self.schema}.release_events SET "
                            f"reference_months=(SELECT jsonb_agg(DISTINCT v) FROM "
                            f"jsonb_array_elements(reference_months || %s::jsonb) AS "
                            f"items(v)) WHERE event_id=%s"
                        ),
                        (Jsonb(doc["reference_months"]), doc["event_id"]),
                    )
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
        raw_store = getattr(self.store, "raw_store", None)
        archive = getattr(self.store, "archive", None)
        if raw_store is None and archive is not None:
            target = Path(archive.directory) / "release-material" / digest(raw)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                fd, name = tempfile.mkstemp(prefix="release-", dir=target.parent)
                try:
                    with os.fdopen(fd, "wb") as output:
                        output.write(raw)
                        output.flush()
                        os.fsync(output.fileno())
                    try:
                        os.link(name, target)
                    except FileExistsError:
                        pass
                finally:
                    os.unlink(name)
            if target.read_bytes() != raw:
                raise CpiTimeSeriesError("release_raw_digest_conflict")
            return str(target)
        if raw_store is None:
            raise CpiTimeSeriesError("release_raw_store_required")
        try:
            return raw_store.put_verified(raw)
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
            # A missing optional table must not abort the outer read snapshot.
            with self.store.connection.transaction():
                rows = self.store.connection.execute(
                    "SELECT evidence_id,raw_sha256,raw_locator,"
                    "obtained_at,available_at,document "
                    f"FROM {self.schema}.release_evidence "
                    "WHERE available_at IS NOT NULL "
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

    def _read_snapshot(self):
        snapshot = getattr(self.store, "read_snapshot", None)
        return snapshot() if snapshot is not None else self._operation()

    def read(
        self,
        *,
        event_id=None,
        as_of=None,
        read_mode="event",
        source_version=None,
        reference_month=None,
        _include_context=True,
    ):
        with self._read_snapshot():
            return self._read(
                event_id=event_id,
                as_of=as_of,
                read_mode=read_mode,
                source_version=source_version,
                reference_month=reference_month,
                _include_context=_include_context,
            )

    def _read(
        self,
        *,
        event_id=None,
        as_of=None,
        read_mode="event",
        source_version=None,
        reference_month=None,
        _include_context=True,
    ):
        cutoff = instant(as_of) if as_of is not None else instant(self.clock())
        if read_mode not in {"event", "source_history"}:
            raise CpiTimeSeriesError("release_read_mode_unsupported")
        if source_version is not None:
            try:
                if date.fromisoformat(source_version).isoformat() != source_version:
                    raise ValueError
            except ValueError, TypeError:
                raise CpiTimeSeriesError("release_source_version_invalid") from None
        if reference_month is not None and not re.fullmatch(
            r"[0-9]{4}-(0[1-9]|1[0-2])", reference_month
        ):
            raise CpiTimeSeriesError("release_reference_month_invalid")
        try:
            rows = self._rows(cutoff)
            unavailable = []
        except CpiTimeSeriesError as error:
            if error.classification != "release_schema_not_prepared":
                raise
            rows, unavailable = [], [error.classification]
        annotations = self._annotations(cutoff)
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
            current = copy.deepcopy(evidence[-1])
            selected, conflicts = {}, []
            for proof in evidence:
                for item in proof["claims"]:
                    key = tuple(
                        item[k]
                        for k in (
                            "series_id",
                            "reference_month",
                            "statistic",
                            "unit",
                            "adjustment",
                            "role",
                        )
                    )
                    candidate = dict(item, evidence_id=proof["evidence_id"])
                    old = selected.get(key)
                    corrected = (
                        old and proof.get("supersedes_evidence") == old["evidence_id"]
                    )
                    if old and (old["value"] != item["value"] or corrected):
                        conflicts.append(
                            dict(
                                scope=dict(
                                    zip(
                                        (
                                            "series_id",
                                            "reference_month",
                                            "statistic",
                                            "unit",
                                            "adjustment",
                                            "role",
                                        ),
                                        key,
                                        strict=True,
                                    )
                                ),
                                selected_evidence_id=proof["evidence_id"]
                                if corrected
                                else old["evidence_id"],
                                alternative_evidence_id=old["evidence_id"]
                                if corrected
                                else proof["evidence_id"],
                                selection_basis="explicit_parser_correction"
                                if corrected
                                else "first_proven_claim_pending_resolution",
                            )
                        )
                        if not corrected:
                            continue
                    if old and old["value"] == item["value"] and not corrected:
                        continue
                    selected[key] = candidate
            current["claims"] = list(selected.values())
            scope_keys = (
                "series_id",
                "reference_month",
                "statistic",
                "unit",
                "adjustment",
            )
            proven_scopes = {
                tuple(c[k] for k in scope_keys)
                for c in selected.values()
                if c["role"] == "actual"
            }
            absences = {}
            for proof in evidence:
                for missing_scope in proof.get("missing_scopes", []):
                    scope = tuple(missing_scope[k] for k in scope_keys)
                    if scope not in proven_scopes:
                        absences[scope] = dict(
                            missing_scope, evidence_id=proof["evidence_id"]
                        )
            current["missing_scopes"] = list(absences.values())
            current["conflicts"] = conflicts
            current["reference_months"] = sorted(
                {
                    ref
                    for p in evidence
                    for ref in p.get("reference_months", [p["reference_month"]])
                }
            )
            current["evidence"] = evidence
            current["material_deficiencies"] = [
                dict(
                    evidence_id=p["evidence_id"],
                    missing=p.get("missing", []),
                    missing_scopes=p.get("missing_scopes", []),
                )
                for p in evidence
            ]
            # Re-evaluate aggregate coverage from selected claims. Per-material
            # parsing/coverage limitations remain on each original proof.
            from market_data_foundation.historical.macro_release_material import (
                _scope_absence,
            )

            _scope_absence(current, self.profile.key)
            current["missing"] = [
                reason
                for reason in dict.fromkeys(
                    r for p in evidence for r in p.get("missing", [])
                )
                if reason
                in {
                    "actual_publication_instant_not_proven",
                    "consensus_source_unavailable",
                    "continuous_version_not_bound_to_material",
                }
            ]
            if current["missing_scopes"]:
                current["missing"].append("some_official_scopes_not_confirmed")
            unresolved = any(
                c["selection_basis"] == "first_proven_claim_pending_resolution"
                and any(
                    c["selected_evidence_id"] == item["evidence_id"]
                    and all(item[k] == c["scope"][k] for k in c["scope"])
                    for item in current["claims"]
                )
                for c in conflicts
            )
            if unresolved:
                current["missing"].append("official_material_claim_conflict")
            current["state"] = (
                "confirmed"
                if any(c["role"] == "actual" for c in current["claims"])
                else "unknown"
            )
            current["material_state"] = (
                "partial" if current["missing_scopes"] or unresolved else "complete"
            )
            if _include_context:
                self._enrich(
                    current, cutoff, annotation_rows=annotations, evidence_rows=rows
                )
            else:
                for item in current["claims"]:
                    association = next(
                        (
                            a
                            for a in reversed(annotations)
                            if a["kind"] == "association"
                            and a["event_id"] == identity
                            and a["value_identity"] == item["value_identity"]
                            and a["evidence_id"] == item["evidence_id"]
                        ),
                        None,
                    )
                    item["continuous_version"] = (
                        association["version"] if association else None
                    )
            if (
                reference_month is None
                or reference_month in current["reference_months"]
            ):
                events.append(current)
        events.sort(
            key=lambda r: (r["source_version"]["value"], r["event_id"]), reverse=True
        )
        if not events:
            unavailable.append("official_release_evidence_not_available_at_cutoff")
        forecasts = [
            a
            for a in annotations
            if a["kind"] == "consensus"
            and (event_id is None or a["event_id"] == event_id)
            and (reference_month is None or a["reference_month"] == reference_month)
        ]
        return {
            "read_mode": read_mode,
            "as_of": cutoff.isoformat(),
            "as_of_basis": "available",
            "events": events,
            "consensus_snapshots": forecasts,
            "missing": unavailable,
            "provider_requests": 0,
            "provider_attempts": 0,
            "persisted": False,
            "history_basis": "named_source_archive_evidence_only_no_latest_fallback",
            "coverage": (
                "only retained official material scopes; ALFRED not implemented"
            ),
        }

    def _annotations(self, cutoff):
        if self.memory:
            return [
                copy.deepcopy(v)
                for v in getattr(self.store, "_release_annotations", {}).values()
                if instant(v["available_at"]) <= cutoff
            ]
        try:
            # A missing optional table must not abort the outer read snapshot.
            with self.store.connection.transaction():
                rows = self.store.connection.execute(
                    "SELECT record_id,event_id,kind,obtained_at,available_at,document "
                    f"FROM {self.schema}.release_annotations "
                    "WHERE available_at <= %s",
                    (cutoff,),
                ).fetchall()
            return [
                dict(
                    doc,
                    record_id=rid,
                    event_id=eid,
                    kind=kind,
                    obtained_at=got.isoformat(),
                    available_at=at.isoformat(),
                )
                for rid, eid, kind, got, at, doc in rows
            ]
        except psycopg.errors.UndefinedTable:
            return []
        except psycopg.Error:
            raise CpiTimeSeriesError("release_storage_failed") from None

    def _append_annotation(self, event_id, kind, document):
        rid = digest(canonical([event_id, kind, document]).encode())
        got = instant(self.clock())
        if self.memory:
            if not hasattr(self.store, "_release_annotations"):
                raise CpiTimeSeriesError("release_schema_not_prepared")
            self.store._release_annotations.setdefault(
                rid,
                dict(
                    document,
                    record_id=rid,
                    event_id=event_id,
                    kind=kind,
                    obtained_at=got.isoformat(),
                    available_at=instant(self.clock()).isoformat(),
                ),
            )
        else:
            try:
                with self.store.connection.transaction():
                    self.store.connection.execute(
                        (
                            f"INSERT INTO {self.schema}.release_annotations "
                            "(record_id,event_id,kind,document) VALUES "
                            "(%s,%s,%s,%s) ON CONFLICT DO NOTHING"
                        ),
                        (rid, event_id, kind, Jsonb(document)),
                    )
                self.store.connection.execute(
                    (
                        f"UPDATE {self.schema}.release_annotations SET "
                        f"available_at=clock_timestamp() WHERE record_id=%s AND "
                        f"available_at IS NULL"
                    ),
                    (rid,),
                )
            except psycopg.Error:
                raise CpiTimeSeriesError("release_annotation_storage_failed") from None
        return next(
            v for v in self._annotations(instant(self.clock())) if v["record_id"] == rid
        )

    def freeze_consensus(self, document):
        """Import a forecast snapshot with today's MDF acquisition time.

        Historical source availability/freeze evidence is separate and immutable.
        This is an operator import port, not an invented live consensus provider.
        """
        required = {
            "event_id",
            "series_id",
            "reference_month",
            "statistic",
            "unit",
            "adjustment",
            "source",
            "method",
            "value",
            "source_available_at",
            "frozen_at",
            "provenance",
        }
        if not required <= set(document) or any(not document[k] for k in required):
            raise CpiTimeSeriesError("consensus_provenance_missing")
        if not document["event_id"].startswith(
            {
                "employment": "bls:employment-situation:",
                "cpi": "bls:cpi:",
                "ppi": "bls:ppi:",
                "jolts": "bls:jolts:",
                "pce": "bea:personal-income-and-outlays:",
                "consumption": "bea:personal-income-and-outlays:",
                "retail": "census:advance-retail:",
            }[self.profile.key]
        ):
            raise CpiTimeSeriesError("consensus_scope_mismatch")
        series = document["series_id"]
        if self.profile.key == "retail":
            scopes = {
                "44X72:SM": ("level", "million_USD", "sa"),
                "44X72:MPCSM": ("mom_pct", "percent", "sa"),
            }
            valid = scopes.get(series) == tuple(
                document[k] for k in ("statistic", "unit", "adjustment")
            )
        else:
            metadata = self.profile.series_metadata.get(series)
            units = {"CES0500000002": "hours", "CES0500000003": "USD/hour"}
            if self.profile.key == "consumption":
                native = metadata.get("value_unit") if metadata else None
                statistics = (
                    {"level"} if series == "A072RC" else {"level", "mom_pct", "yoy_pct"}
                )
                unit = native if document["statistic"] == "level" else "percent"
            elif self.profile.key == "jolts":
                unit = metadata.get("value_unit") if metadata else None
                statistics = {"level"}
            elif self.profile.key == "employment":
                native = units.get(
                    series, "thousand_jobs" if series.startswith("CES") else "percent"
                )
                statistics = (
                    {"level", "net_change_1m"}
                    if native == "thousand_jobs"
                    else {"level", "mom_pct", "yoy_pct"}
                    if series == "CES0500000003"
                    else {"level"}
                )
                unit = (
                    "percent"
                    if document["statistic"] in {"mom_pct", "yoy_pct"}
                    else native
                )
            else:
                unit = "index" if document["statistic"] == "index" else "percent"
                statistics = {"index", "mom_pct", "yoy_pct"}
            valid = (
                bool(metadata)
                and document["unit"] == unit
                and document["adjustment"] == metadata["seasonal_adjustment"].lower()
                and document["statistic"] in statistics
            )
        if not valid:
            raise CpiTimeSeriesError("consensus_scope_mismatch")
        provenance = document["provenance"]
        if (
            not isinstance(provenance, dict)
            or not provenance.get("locator")
            or not re.fullmatch("[0-9a-f]{64}", provenance.get("sha256", ""))
        ):
            raise CpiTimeSeriesError("consensus_provenance_missing")
        try:
            if (
                not isinstance(document["value"], str)
                or not Decimal(document["value"]).is_finite()
            ):
                raise ValueError
            temporal_bounds(document["source_available_at"])
            temporal_bounds(document["frozen_at"])
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", document["reference_month"]):
                raise ValueError
        except ValueError, TypeError, InvalidOperation, KeyError:
            raise CpiTimeSeriesError(
                "consensus_value_or_time_evidence_invalid"
            ) from None
        return self._append_annotation(document["event_id"], "consensus", document)

    def bind_version(self, *, event_id, evidence_id, value_identity, version, proof):
        """Explicit proof path; equality alone is rejected.

        Numeric response metadata must reference the retained official material
        URL. An operator supplies those bounded numeric bytes; their digest
        must match the actual persisted batch. Most BLS API responses do not carry
        such a token and therefore correctly remain unbound.
        """
        row = next(
            (
                r
                for r in self._rows(None)
                if r["event_id"] == event_id and r["evidence_id"] == evidence_id
            ),
            None,
        )
        item = (
            next(
                (c for c in row["claims"] if c["value_identity"] == value_identity),
                None,
            )
            if row
            else None
        )
        if item is None:
            raise CpiTimeSeriesError("association_claim_not_found")
        candidates = self._versions(
            item["series_id"], item["reference_month"], instant(self.clock())
        )
        candidate = next(
            (
                v
                for v in candidates
                if all(
                    v.get(k) == version.get(k)
                    for k in ("revision", "batch_id", "raw_sha256")
                )
            ),
            None,
        )
        raw = proof.get("numeric_response")
        token = proof.get("official_version_token")
        metadata_match = False
        if type(raw) is bytes and len(raw) <= 2_000_000:
            try:
                metadata = json.loads(raw)
                pointer = proof.get("version_metadata_pointer", "")
                if not isinstance(pointer, str) or not pointer.startswith("/"):
                    raise ValueError
                for part in pointer[1:].split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    metadata = (
                        metadata[int(part)]
                        if isinstance(metadata, list)
                        else metadata[part]
                    )
                metadata_match = metadata == token
            except ValueError, TypeError, KeyError, IndexError, UnicodeError:
                pass
        if (
            not metadata_match
            or candidate is None
            or proof.get("kind") != "official_version_reference"
            or type(raw) is not bytes
            or not isinstance(token, str)
            or token != row["source_url"]
            or len(raw) > 2_000_000
            or digest(raw) != candidate.get("raw_sha256")
            or token not in raw.decode("utf-8", errors="replace")
            or not proof.get("version_metadata_pointer")
        ):
            raise CpiTimeSeriesError("association_explicit_version_proof_required")
        value = self._statistic(candidate, item, instant(self.clock()))
        if value is None or Decimal(value["value"]) != Decimal(item["value"]):
            raise CpiTimeSeriesError("association_statistic_value_mismatch")
        return self._append_annotation(
            event_id,
            "association",
            dict(
                evidence_id=evidence_id,
                value_identity=value_identity,
                version={
                    k: candidate.get(k)
                    for k in (
                        "series_id",
                        "month",
                        "revision",
                        "batch_id",
                        "raw_sha256",
                        "available_at",
                    )
                },
                proof={k: v for k, v in proof.items() if k != "numeric_response"},
            ),
        )

    def _prefetch_versions(self, series, start, end, cutoff):
        """One bounded numeric history read for a range response, avoiding N+1 PG IO."""
        if self.memory or self.profile.key == "retail" or not series:
            return
        from market_data_foundation.historical.cpi_timeseries_store import (
            _revision_columns,
            _row_document,
        )

        fields = ",".join(
            "r." + name for name in _revision_columns(self.profile).split(",")
        )
        rows = self.store.connection.execute(
            f"SELECT {fields},b.raw_sha256,r.available_at,r.version_status,"
            f"r.evidence_url FROM {self.schema}.revisions r "
            f"JOIN {self.schema}.batches b USING(batch_id) "
            "WHERE r.series_id=ANY(%s) AND r.month BETWEEN %s AND %s "
            "AND r.available_at <= %s ORDER BY r.series_id,r.month,r.revision",
            (list(series), start + "-01", end + "-01", cutoff),
        ).fetchall()
        values = {}
        for row in rows:
            item = _row_document(row, profile=self.profile)
            values.setdefault((item["series_id"], item["month"]), []).append(item)
        self._version_snapshot = dict(
            series=series, start=start, end=end, cutoff=cutoff, values=values
        )

    def _versions(self, series, reference, cutoff):
        """All locally captured numeric revisions at the SAME consumable cutoff."""
        snapshot = self._version_snapshot
        if (
            snapshot
            and series in snapshot["series"]
            and snapshot["start"] <= reference <= snapshot["end"]
            and cutoff <= snapshot["cutoff"]
        ):
            return [
                v
                for v in snapshot["values"].get((series, reference), [])
                if instant(v["available_at"]) <= cutoff
            ]
        if self.profile.key == "retail":
            category, kind = series.split(":")
            if self.memory:
                values = getattr(self.store, "revisions", {}).get(
                    (reference, category, kind), []
                )
                return [
                    dict(
                        series_id=series,
                        month=reference,
                        index_value=None if v.cell.value is None else str(v.cell.value),
                        revision=v.revision,
                        batch_id=v.batch_id,
                        observed_at=v.observed_at.isoformat(),
                        available_at=v.available_at.isoformat(),
                        raw_sha256=v.cell.raw_sha256,
                    )
                    for v in values
                    if v.available_at and v.available_at <= cutoff
                ]
            rows = self.store.connection.execute(
                "SELECT revision,batch_id,raw_sha256,observed_at,available_at,value "
                f"FROM {self.schema}.revisions WHERE category_code=%s "
                "AND data_type_code=%s AND month=%s AND available_at<=%s "
                "ORDER BY revision",
                (category, kind, reference + "-01", cutoff),
            ).fetchall()
            return [
                dict(
                    series_id=series,
                    month=reference,
                    revision=r,
                    batch_id=b,
                    raw_sha256=sha,
                    observed_at=o.isoformat(),
                    available_at=a.isoformat(),
                    index_value=None if v is None else str(v),
                )
                for r, b, sha, o, a, v in rows
            ]
        if self.memory:
            return [
                copy.deepcopy(v)
                for v in self.store._history.get((series, reference), [])
                if v.get("available_at") and instant(v["available_at"]) <= cutoff
            ]
        from market_data_foundation.historical.cpi_timeseries_store import (
            _revision_columns,
            _row_document,
        )

        fields = ",".join(
            "r." + name for name in _revision_columns(self.profile).split(",")
        )
        rows = self.store.connection.execute(
            f"SELECT {fields},b.raw_sha256,r.available_at,r.version_status,"
            f"r.evidence_url FROM {self.schema}.revisions r "
            f"JOIN {self.schema}.batches b USING(batch_id) "
            "WHERE r.series_id=%s AND r.month=%s AND r.available_at <= %s "
            "ORDER BY r.revision",
            (series, reference + "-01", cutoff),
        ).fetchall()
        return [_row_document(r, profile=self.profile) for r in rows]

    def _statistic(self, version, item, cutoff):
        if (
            version.get("version_status") == "withdrawn"
            or version.get("index_value") is None
        ):
            return None
        stat = item["statistic"]
        if stat in {"index", "level"} or self.profile.key == "retail":
            value, operands = version["index_value"], [version]
        else:
            calculations = version.get("provider_calculations", {})
            value = (
                (
                    version["bea_pct_1m"]
                    if version.get("bea_pct_1m") is not None
                    else calculations.get("bea_pct_1m")
                )
                if stat == "mom_pct" and self.profile.key == "pce"
                else calculations.get("pct_changes", {}).get(
                    "1" if stat == "mom_pct" else "12"
                )
                if stat in {"mom_pct", "yoy_pct"}
                else calculations.get("net_changes", {}).get("1")
            )
            operands = [version]
            if value is None and stat == "net_change_1m":
                from market_data_foundation.historical.cpi_timeseries import (
                    month_at,
                    month_number,
                )

                prior = month_at(month_number(version["month"]) - 1)
                before = self._versions(item["series_id"], prior, cutoff)
                if before and before[-1].get("index_value") is not None:
                    value = str(
                        Decimal(version["index_value"])
                        - Decimal(before[-1]["index_value"])
                    )
                    operands.append(before[-1])
        if value is None:
            return None
        return dict(
            value=str(value),
            statistic=stat,
            basis="mdf_continuous_derived"
            if len(operands) > 1
            else "mdf_captured_provider_value",
            as_of=cutoff.isoformat(),
            versions=[
                {
                    k: v.get(k)
                    for k in (
                        "series_id",
                        "month",
                        "revision",
                        "batch_id",
                        "observed_at",
                        "available_at",
                        "raw_sha256",
                    )
                }
                for v in operands
            ],
        )

    def _enrich(self, event, cutoff, *, annotation_rows=None, evidence_rows=None):
        annotations = sorted(
            [
                a
                for a in (
                    annotation_rows
                    if annotation_rows is not None
                    else self._annotations(cutoff)
                )
                if a["event_id"] == event["event_id"]
            ],
            key=lambda a: (instant(a["available_at"]), a["record_id"]),
        )
        event["missing"] = [
            r
            for r in event["missing"]
            if r != "mdf_pre_release_value_requires_proven_release_instant"
        ]
        event["consensus_snapshots"] = [
            a for a in annotations if a["kind"] == "consensus"
        ]
        for item in event["claims"]:
            item["confirmation_state"] = (
                "conflicted"
                if any(
                    c["selected_evidence_id"] == item["evidence_id"]
                    and c["selection_basis"] == "first_proven_claim_pending_resolution"
                    and all(c["scope"][k] == item[k] for k in c["scope"])
                    for c in event.get("conflicts", [])
                )
                else "confirmed"
            )
            versions = self._versions(
                item["series_id"], item["reference_month"], cutoff
            )
            first = versions[0] if versions else None
            # First capture's derived operands use the first capture's own cutoff.
            item["mdf_first_captured"] = (
                self._statistic(first, item, instant(first["available_at"]))
                if first
                else None
            )
            item["mdf_first_captured_missing"] = (
                None
                if item["mdf_first_captured"]
                else "mdf_statistic_not_captured_at_cutoff"
            )
            association = next(
                (
                    a
                    for a in reversed(annotations)
                    if a["kind"] == "association"
                    and a["value_identity"] == item["value_identity"]
                    and a["evidence_id"] == item["evidence_id"]
                ),
                None,
            )
            item["continuous_version"] = association["version"] if association else None
            item["association"] = association
            item["association_missing"] = (
                None if association else "continuous_version_not_bound_to_material"
            )
            if item["role"] == "actual":
                from market_data_foundation.historical.cpi_timeseries import (
                    month_at,
                    month_number,
                )

                try:
                    event_start, event_end = temporal_bounds(official_event_time(event))
                except CpiTimeSeriesError as error:
                    if error.classification not in {
                        "release_date_timezone_missing",
                        "release_date_timezone_invalid",
                    }:
                        raise
                    item["mdf_pre_event_guaranteed_previous"] = None
                    item["mdf_pre_event_last_consumable_previous"] = None
                    item["mdf_pre_event_previous_basis"] = None
                    item["mdf_pre_event_previous_missing"] = error.classification
                    if error.classification not in event["missing"]:
                        event["missing"].append(error.classification)
                else:
                    prior = month_at(month_number(item["reference_month"]) - 1)
                    prior_cutoff = min(cutoff, event_start - timedelta(microseconds=1))
                    before = self._versions(item["series_id"], prior, prior_cutoff)
                    guaranteed = (
                        self._statistic(before[-1], item, prior_cutoff)
                        if before
                        else None
                    )
                    uncertain = any(
                        event_start <= instant(v["available_at"]) < event_end
                        for v in self._versions(item["series_id"], prior, cutoff)
                    )
                    item["mdf_pre_event_guaranteed_previous"] = guaranteed
                    item["mdf_pre_event_last_consumable_previous"] = (
                        None if uncertain else guaranteed
                    )
                    item["mdf_pre_event_previous_basis"] = (
                        "strictly_before_official_release_date_lower_bound"
                        if event.get("actual_at") is None
                        else "strictly_before_proven_event_instant"
                    )
                    item["mdf_pre_event_previous_missing"] = (
                        None
                        if item["mdf_pre_event_last_consumable_previous"]
                        else "pre_event_last_value_order_unknown"
                        if uncertain
                        else "no_consumable_prior_value_before_event_bound"
                    )
                keys = (
                    "series_id",
                    "reference_month",
                    "statistic",
                    "unit",
                    "adjustment",
                )
                forecasts = [
                    a
                    for a in annotations
                    if a["kind"] == "consensus"
                    and all(a.get(k) == item.get(k) for k in keys)
                ]
                item["consensus_snapshots"] = forecasts
                item["consensus"] = forecasts[-1] if forecasts else None
                item["surprise"], item["surprise_missing"] = release_surprise(
                    item,
                    item["consensus"],
                    event_id=event["event_id"],
                    actual_at=event.get("actual_at"),
                    event_time_evidence=official_event_time(event),
                )
        if any(c.get("surprise") is not None for c in event["claims"]):
            event["missing"] = [
                r for r in event["missing"] if r != "consensus_source_unavailable"
            ]
        # Separate prior event material is evidence of that earlier actual, never a
        # claim that its MDF availability was contemporaneous with publication.
        event["prior_event_values"] = []
        if any(
            c["series_id"] == "CES0000000001"
            and c["statistic"] == "net_change_1m"
            and c["role"] == "actual"
            for c in event["claims"]
        ):
            event["missing"] = [
                r
                for r in event["missing"]
                if r != "nfp_current_net_change_not_found_in_material"
            ]
        previous_claims = {}
        for other in sorted(
            evidence_rows if evidence_rows is not None else self._rows(cutoff),
            key=lambda r: (r["available_at"], r["evidence_id"]),
        ):
            if (
                other["event_id"] == event["event_id"]
                or other["source_version"]["value"] >= event["source_version"]["value"]
            ):
                continue
            for c in other["claims"]:
                if c["role"] == "actual" and any(
                    c["reference_month"] == x["reference_month"]
                    and all(
                        c[k] == x[k]
                        for k in ("series_id", "statistic", "unit", "adjustment")
                    )
                    for x in event["claims"]
                    if x["role"] == "prior_revised"
                ):
                    identity = (
                        other["event_id"],
                        *(
                            c[k]
                            for k in (
                                "series_id",
                                "reference_month",
                                "statistic",
                                "unit",
                                "adjustment",
                            )
                        ),
                    )
                    candidate = dict(
                        c,
                        evidence_id=other["evidence_id"],
                        event_id=other["event_id"],
                        available_at=other["available_at"],
                        confirmation_state="confirmed",
                    )
                    previous = previous_claims.get(identity)
                    if (
                        previous
                        and previous["value"] != c["value"]
                        and other.get("supersedes_evidence") != previous["evidence_id"]
                    ):
                        previous["confirmation_state"] = "conflicted"
                        previous.setdefault("alternative_evidence_ids", []).append(
                            other["evidence_id"]
                        )
                        continue
                    if previous and previous["value"] == c["value"]:
                        continue
                    previous_claims[identity] = candidate
        event["prior_event_values"] = list(previous_claims.values())

    def link_result(self, result):
        with self._read_snapshot():
            return self._snapshot_link_result(result)

    def _snapshot_link_result(self, result):
        """Related scope is distinct from a proven numeric-version association."""
        cutoff = (
            instant(result["as_of"]) if result.get("as_of") else instant(self.clock())
        )
        points = result.get("points", [])
        try:
            if points and self.profile.key != "retail":
                from market_data_foundation.historical.cpi_timeseries import (
                    month_at,
                    month_number,
                )

                start = month_at(month_number(min(p["month"] for p in points)) - 3)
                self._prefetch_versions(
                    {p["series_id"] for p in points},
                    start,
                    max(p["month"] for p in points),
                    cutoff,
                )
            return self._link_result(result, cutoff)
        except (CpiTimeSeriesError, psycopg.Error) as error:
            # Optional evidence failure does not erase an already successful
            # numeric read. The dedicated release read still reports its failure.
            result["release_records_missing"] = [
                getattr(error, "classification", "release_storage_failed")
            ]
            return result
        finally:
            self._version_snapshot = None

    def _link_result(self, result, cutoff):
        events = self._read(as_of=cutoff.isoformat(), _include_context=False)["events"]
        for point in result.get("points", []):
            if self.profile.key != "retail" and point.get("series_id"):
                versions = self._versions(point["series_id"], point["month"], cutoff)
                first = versions[0] if versions else None
                point["mdf_first_captured"] = (
                    self._statistic(
                        first,
                        dict(
                            series_id=point["series_id"],
                            statistic="level"
                            if self.profile.key
                            in {"employment", "consumption", "jolts"}
                            else "index",
                        ),
                        instant(first["available_at"]),
                    )
                    if first
                    else None
                )
            links = []
            for event in events:
                claims = [
                    c
                    for c in event["claims"]
                    if c["reference_month"] == point["month"]
                    and (
                        self.profile.key == "retail"
                        or c["series_id"] == point.get("series_id")
                    )
                ]
                if claims:
                    links.append(
                        dict(
                            event_id=event["event_id"],
                            source_version=dict(event["source_version"]),
                            roles=sorted({c["role"] for c in claims}),
                            evidence_ids=list(
                                dict.fromkeys(c["evidence_id"] for c in claims)
                            ),
                            association="explicit_version_proof"
                            if any(
                                c.get("continuous_version")
                                and c["continuous_version"].get("revision")
                                == point.get("revision")
                                and c["continuous_version"].get("batch_id")
                                == point.get("batch_id")
                                for c in claims
                            )
                            else "related_scope_only",
                            reason=None
                            if any(
                                c.get("continuous_version")
                                and c["continuous_version"].get("revision")
                                == point.get("revision")
                                and c["continuous_version"].get("batch_id")
                                == point.get("batch_id")
                                for c in claims
                            )
                            else "continuous_version_not_bound_to_material",
                        )
                    )
            point["related_release_events"] = links
        return result
