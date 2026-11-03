"""Isolated PostgreSQL CPI series persistence and verified Garage raw material.

Observation cutoffs remain distinct from conservative post-commit confirmation
cutoffs. Neither reconstructs official historical releases. Changed values and
evidence-backed withdrawals are retained; unchanged rows keep their original
timestamps. No connection or DDL occurs on import.
"""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from threading import RLock
from types import SimpleNamespace
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

from market_data_foundation.historical.bls_cpi_material_store import (
    BUCKET,
)
from market_data_foundation.historical.bls_timeseries_profile import (
    CONSUMPTION_PROFILE,
    CPI_PROFILE,
    EMPLOYMENT_PROFILE,
    JOLTS_PROFILE,
    PCE_PROFILE,
    validate_profile,
)
from market_data_foundation.historical.pce_timeseries_metadata import (
    compact_pce_metadata,
)
from market_data_foundation.historical.verified_garage_raw import VerifiedGarageRawStore

SCHEMA = CPI_PROFILE.schema
RAW_PREFIX = CPI_PROFILE.raw_prefix
MAX_RAW_BYTES = 8 * 1024 * 1024
LOCK_ID = CPI_PROFILE.lock_id
SERIES_IDS = CPI_PROFILE.series_ids


class CpiStorageError(RuntimeError):
    def __init__(self, classification):
        self.classification = classification
        super().__init__(classification)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def raw_locator(sha, *, profile=CPI_PROFILE):
    profile = validate_profile(profile)
    return f"mdf-s3://{BUCKET}/{profile.raw_prefix}{sha}/raw-response.bin"


def _content(point):
    return {
        "index_value": None if point.index_value is None else str(point.index_value),
        "availability": (
            "source_unavailable" if point.index_value is None else "available"
        ),
        "provider_calculations": point.provider_calculations,
        "footnotes": list(point.footnotes),
    }


def batch_identity(batch):
    return digest(
        canonical(
            {
                "raw_sha256": digest(batch.raw),
                "request": batch.request,
                "observed_at": batch.observed_at.isoformat(),
            }
        ).encode()
    )


def _point_document(point, batch, revision, *, profile=CPI_PROFILE):
    profile = validate_profile(profile)
    document = {
        "series_id": point.series_id,
        "month": point.month,
        **_content(point),
        "observed_at": batch.observed_at.astimezone(UTC).isoformat(),
        "batch_id": batch_identity(batch),
        "raw_sha256": digest(batch.raw),
        "raw_locator": raw_locator(digest(batch.raw), profile=profile),
        "revision": revision,
        "available_at": None,
        "version_status": "active",
        "evidence_url": None,
    }
    if profile is PCE_PROFILE:
        values = _relational_content(point, profile=profile)
        document["bea_pct_1m"] = None if values[1] is None else str(values[1])
        document["provider_calculations"] = _provider_calculations(
            values[1:2], values[2], profile=profile
        )
    return document


PCT_PERIODS = ("1", "3", "6", "12")
VALUE_COLUMNS = (
    "index_value,bls_pct_1m,bls_pct_3m,bls_pct_6m,bls_pct_12m,provider_extra,footnotes"
)
EVENT_COLUMNS = "available_at,version_status,evidence_url"
REVISION_COLUMNS = "series_id,month,revision,observed_at,batch_id," + VALUE_COLUMNS


def _level_column(profile):
    return "value" if profile is CONSUMPTION_PROFILE else "index_value"


def _percent_columns(profile=CPI_PROFILE):
    profile = validate_profile(profile)
    if profile is CONSUMPTION_PROFILE:
        return ()
    return (
        ("bea_pct_1m",)
        if profile is PCE_PROFILE
        else tuple(f"bls_pct_{k}m" for k in PCT_PERIODS)
    )


def _value_columns(profile=CPI_PROFILE):
    return ",".join(
        (
            _level_column(profile),
            *_percent_columns(profile),
            "provider_extra",
            "footnotes",
        )
    )


def _revision_columns(profile=CPI_PROFILE):
    return "series_id,month,revision,observed_at,batch_id," + _value_columns(profile)


def _revision_shape(profile=CPI_PROFILE):
    return {
        "series_id": "text",
        "month": "date",
        "revision": "integer",
        "observed_at": "timestamp with time zone",
        "batch_id": "text",
        _level_column(profile): "numeric",
        **dict.fromkeys(_percent_columns(profile), "numeric"),
        "provider_extra": "jsonb",
        "footnotes": "jsonb",
        "available_at": "timestamp with time zone",
        "version_status": "text",
        "evidence_url": "text",
    }


def _check_pce_partial(old_values, values, *, profile, state):
    # Reject an ambiguous partial refresh instead of attributing an old official
    # value to a new response. Changed indices and explicit withdrawals may be NULL.
    if (
        profile is PCE_PROFILE
        and state == "active"
        and old_values is not None
        and old_values[0] == values[0]
        and old_values[1] is not None
        and values[1] is None
    ):
        raise CpiStorageError("pce_timeseries_official_percent_missing_unchanged_index")


def _month_date(month):
    return date.fromisoformat(month + "-01")


def _numeric(value):
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise CpiStorageError("cpi_timeseries_numeric_invalid") from None
    if not number.is_finite():
        raise CpiStorageError("cpi_timeseries_numeric_invalid")
    return number


def _relational_content(point, *, profile=CPI_PROFILE):
    # Only fixed CPI percent changes become numeric columns. Any other provider
    # attributes remain losslessly in the optional, nonduplicating supplement.
    extra = copy.deepcopy(point.provider_calculations)
    if profile is CONSUMPTION_PROFILE:
        return (point.index_value, extra or None, list(point.footnotes))
    if validate_profile(profile) is PCE_PROFILE:
        value = extra.pop("bea_pct_1m", None)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal))
        ):
            raise CpiStorageError("pce_timeseries_official_percent_invalid")
        return (
            point.index_value,
            _numeric(value),
            compact_pce_metadata(extra, series_id=point.series_id),
            list(point.footnotes),
        )
    changes = extra.get("pct_changes", {})
    values = []
    for period in PCT_PERIODS:
        value = changes.get(period) if isinstance(changes, dict) else None
        number = None
        if isinstance(value, (str, int, float, Decimal)) and not isinstance(
            value, bool
        ):
            try:
                number = _numeric(value)
            except CpiStorageError:
                pass
            else:
                changes.pop(period)
        values.append(number)
    if extra.get("pct_changes") == {}:
        extra.pop("pct_changes")
    if extra.get("net_changes") == {}:
        extra.pop("net_changes")
    return (point.index_value, *values, extra or None, list(point.footnotes))


def _provider_calculations(values, extra, *, profile=CPI_PROFILE):
    if profile is CONSUMPTION_PROFILE:
        return copy.deepcopy(extra or {})
    if validate_profile(profile) is PCE_PROFILE:
        result = copy.deepcopy(extra or {})
        if values[0] is not None:
            result["bea_pct_1m"] = str(values[0])
        return result
    if not extra and all(value is None for value in values):
        return {}
    result = {"net_changes": {}, "pct_changes": {}}
    result.update(copy.deepcopy(extra or {}))
    changes = {
        period: str(value)
        for period, value in zip(PCT_PERIODS, values, strict=True)
        if value is not None
    }
    if changes:
        result["pct_changes"].update(changes)
    return result


def _row_document(row, *, profile=CPI_PROFILE):
    profile = validate_profile(profile)
    series, month, revision, observed_at, batch_id, index, *rest = row
    count = len(_percent_columns(profile))
    pcts, extra, footnotes, raw_sha = rest[:count], *rest[count : count + 3]
    available_at, version_status, evidence_url = (
        rest[count + 3 : count + 6]
        if len(rest) >= count + 6
        else (None, "active", None)
    )
    document = {
        "available_at": None if available_at is None else available_at.isoformat(),
        "version_status": version_status,
        "evidence_url": evidence_url,
        "batch_id": batch_id,
        "series_id": series,
        "month": month.strftime("%Y-%m"),
        "revision": revision,
        "observed_at": observed_at.astimezone(UTC).isoformat(),
        "index_value": None if index is None else str(index),
        "availability": (
            "withdrawn"
            if version_status == "withdrawn"
            else "source_unavailable"
            if index is None
            else "available"
        ),
        "provider_calculations": _provider_calculations(pcts, extra, profile=profile),
        "footnotes": footnotes,
        "raw_sha256": raw_sha,
        "raw_locator": raw_locator(raw_sha, profile=profile),
    }
    if profile is PCE_PROFILE:
        document["bea_pct_1m"] = None if pcts[0] is None else str(pcts[0])
    return document


def _latest_view_sql(*, event_columns=False, profile=CPI_PROFILE):
    profile = validate_profile(profile)
    columns = _revision_columns(profile) + (
        "," + EVENT_COLUMNS if event_columns else ""
    )
    if profile is CONSUMPTION_PROFILE:
        cases = " ".join(
            f"WHEN '{sid}' THEN '{meta['value_unit']}'"
            for sid, meta in profile.series_metadata.items()
        )
        columns += (
            f",CASE series_id {cases} END AS value_unit,"
            "'SA'::text AS seasonal_adjustment,series_id!='A072RC' AS annual_rate,"
            "CASE WHEN series_id IN ('DPCERX','DGDSRX','DSERRX','A067RX') "
            "THEN '2017'::text END AS chained_base"
        )
    return f"""CREATE OR REPLACE VIEW {profile.schema}.latest AS
        SELECT DISTINCT ON (series_id,month) {columns}
        FROM {profile.schema}.revisions
        ORDER BY series_id,month,revision DESC"""


def _revision_ddl(*, profile=CPI_PROFILE):
    profile = validate_profile(profile)
    admitted = ",".join("'" + series + "'" for series in profile.series_ids)
    percents = ",\n        ".join(
        name + " numeric" for name in _percent_columns(profile)
    )
    comparison = ">=" if profile in (EMPLOYMENT_PROFILE, JOLTS_PROFILE) else ">"
    zero_guard = (
        "AND (index_value > 0 OR series_id IN "
        "('LNS14000000','LNS11300000','LNS12300000'))"
        if profile is EMPLOYMENT_PROFILE
        else ""
    )
    level = _level_column(profile)
    guard = (
        "(series_id='A072RC' OR value > 0)"
        if profile is CONSUMPTION_PROFILE
        else f"index_value {comparison} 0 {zero_guard}"
    )
    percents = percents + "," if percents else ""
    return f"""CREATE TABLE IF NOT EXISTS {profile.schema}.revisions (
        series_id text NOT NULL CHECK(series_id IN ({admitted})),
        month date NOT NULL CHECK(EXTRACT(DAY FROM month)=1),
        revision integer NOT NULL CHECK(revision > 0),
        observed_at timestamptz NOT NULL,
        batch_id text NOT NULL REFERENCES {profile.schema}.batches(batch_id),
        {level} numeric CHECK({guard}
            AND {level} NOT IN
                ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)),
        {percents}
        provider_extra jsonb,
        footnotes jsonb NOT NULL CHECK(jsonb_typeof(footnotes)='array'),
        available_at timestamptz,
        version_status text NOT NULL DEFAULT 'active'
            CHECK(version_status IN ('active','withdrawn')),
        evidence_url text,
        CHECK(version_status != 'withdrawn' OR
            ({level} IS NULL AND evidence_url IS NOT NULL)),
        PRIMARY KEY(series_id,month,revision)
    )"""


def _rates_view_sql(*, profile=CPI_PROFILE):
    profile = validate_profile(profile)
    if profile is JOLTS_PROFILE:
        from market_data_foundation.historical.jolts_timeseries import rates_view_sql

        return rates_view_sql()
    if profile is CONSUMPTION_PROFILE:
        from market_data_foundation.historical.consumption_timeseries import (
            rates_view_sql,
        )

        return rates_view_sql()
    if profile is EMPLOYMENT_PROFILE:
        from market_data_foundation.historical.employment_timeseries import (
            rates_view_sql,
        )

        return rates_view_sql()
    rates = ",".join(
        f"round(c.index_value / nullif(p{k}.index_value,0) - 1,4) AS change_{k}m_ratio"
        for k in (1, 3, 6, 12)
    )
    if profile is PCE_PROFILE:
        rates += "," + ",".join(
            f"round(power(c.index_value/nullif(p{k}.index_value,0),{exponent})-1,4) "
            f"AS annualized_{k}m_ratio"
            for k, exponent in ((3, 4), (6, 2))
        )
    joins = " ".join(
        f"LEFT JOIN {profile.schema}.latest p{k} ON p{k}.series_id=c.series_id "
        f"AND p{k}.month=(c.month - interval '{k} months')::date "
        f"AND p{k}.version_status='active'"
        for k in (1, 3, 6, 12)
    )
    return f"""CREATE OR REPLACE VIEW {profile.schema}.{profile.rates_view} AS
        SELECT c.*,to_char(c.month,'YYYY-MM') AS month_label,{rates}
        FROM {profile.schema}.latest c {joins} WHERE c.version_status='active'"""


def validate_evidence_url(value, *, profile=CPI_PROFILE):
    profile = validate_profile(profile)
    try:
        url = urlsplit(value)
        valid = (
            isinstance(value, str)
            and url.scheme == "https"
            and url.hostname
            in (
                ("bea.gov", "www.bea.gov", "apps.bea.gov")
                if profile.provider == "BEA"
                else ("bls.gov", "www.bls.gov")
            )
            and url.username is None
            and url.password is None
            and url.port in (None, 443)
            and url.path not in ("", "/")
            and not url.query
            and not url.fragment
            and not any(c.isspace() or ord(c) < 32 for c in value)
        )
    except TypeError, ValueError, AttributeError:
        valid = False
    if not valid:
        raise CpiStorageError("cpi_timeseries_evidence_url_invalid")


def _restoration_expectations(points, evidence_url, *, profile=CPI_PROFILE):
    profile = validate_profile(profile)
    if points is None:
        return None
    try:
        if not isinstance(points, list) or evidence_url is None:
            raise ValueError
        expected = {}
        for point in points:
            if not isinstance(point, dict) or set(point) != {
                "series_id",
                "month",
                "index_value",
            }:
                raise ValueError
            series, month, value = (
                point[k] for k in ("series_id", "month", "index_value")
            )
            if series not in profile.series_ids or not isinstance(month, str):
                raise ValueError
            _month_date(month)
            if isinstance(value, bool) or not isinstance(
                value, (str, int, float, Decimal)
            ):
                raise ValueError
            value = _numeric(value)
            if (
                value is None
                or (
                    value <= 0
                    and not (
                        (profile is CONSUMPTION_PROFILE and series == "A072RC")
                        or (profile is JOLTS_PROFILE and value == 0)
                    )
                )
                or (series, month) in expected
            ):
                raise ValueError
            expected[series, month] = value
        return expected
    except ValueError, TypeError, CpiStorageError:
        raise CpiStorageError(
            "cpi_timeseries_restoration_expected_points_invalid"
        ) from None


def _restoration_matches(point, expected):
    return expected is None or (
        (point.series_id, point.month) in expected
        and point.index_value == expected[point.series_id, point.month]
    )


def _validate_series(series_ids, *, profile=CPI_PROFILE):
    profile = validate_profile(profile)
    if (
        not isinstance(series_ids, (tuple, list))
        or not series_ids
        or any(
            not isinstance(s, str) or s not in profile.series_ids for s in series_ids
        )
        or len(set(series_ids)) != len(series_ids)
    ):
        raise CpiStorageError("cpi_timeseries_series_invalid")


def _validate_batches(batches, *, profile=CPI_PROFILE):
    profile = validate_profile(profile)
    for batch in batches:
        requested = batch.request.get("seriesid")
        if requested is not None:
            _validate_series(requested, profile=profile)
        for point in batch.points:
            if point.series_id not in profile.series_ids or (
                requested is not None and point.series_id not in requested
            ):
                raise CpiStorageError("cpi_timeseries_series_invalid")


def _withdraw(store, series_ids, start_month, end_month, evidence_url):
    # Evidence is a caller-validated official notice reference, not an invented
    # archived provider response. The batch request labels this material clearly.
    from market_data_foundation.historical.cpi_timeseries_source import (
        SourceBatch,
        SourcePoint,
    )

    _validate_series(series_ids, profile=store.profile)
    validate_evidence_url(evidence_url, profile=store.profile)
    first, last = _month_date(start_month), _month_date(end_month)
    months = []
    current = first
    while current <= last:
        months.append(current.strftime("%Y-%m"))
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    request = {
        "event": "withdrawal",
        "evidence_url": evidence_url,
        "seriesid": list(series_ids),
        "start_month": start_month,
        "end_month": end_month,
    }
    existing = {
        (p["series_id"], p["month"]): p
        for p in store.read_range(series_ids, start_month, end_month)
    }
    points = tuple(
        SourcePoint(s, m, None, {}, ())
        for s in series_ids
        for m in months
        if existing.get((s, m), {}).get("version_status") != "withdrawn"
    )
    if not points:
        return {"saved_points": 0, "saved_revisions": 0}
    batch = SourceBatch(
        points,
        canonical(request).encode(),
        request,
        store._clock(),
        0,
    )
    return store.save_batches((batch,), _withdrawal_evidence_url=evidence_url)


def _validate_raw_store(raw_store, profile):
    if getattr(raw_store, "profile", profile) is not profile:
        raise CpiStorageError("bls_timeseries_profile_mismatch")


class GarageCpiRawStore(VerifiedGarageRawStore):
    """Existing monthly namespace over the common verified Garage transfer."""

    def __init__(self, credentials, *, transport=None, clock=None, profile=CPI_PROFILE):
        self.profile = validate_profile(profile)
        super().__init__(
            credentials,
            raw_prefix=self.profile.raw_prefix,
            error_factory=CpiStorageError,
            error_prefix="cpi_timeseries",
            transport=transport,
            clock=clock,
        )


class PostgresTimeSeriesStore:
    """Two tables with numeric observations and a derived latest view."""

    def __init__(self, connection, raw_store, *, clock=None, profile=CPI_PROFILE):
        self.profile = validate_profile(profile)
        _validate_raw_store(raw_store, self.profile)
        self._clock = clock or (lambda: datetime.now(UTC))
        self.connection = connection
        self.raw_store = raw_store
        self._lock = RLock()
        if not connection.autocommit:
            raise CpiStorageError("cpi_timeseries_autocommit_required")

    @contextmanager
    def operation(self):
        with self._lock:
            acquired = False
            failed = False
            try:
                acquired = bool(
                    self.connection.execute(
                        "SELECT pg_try_advisory_lock(%s)", (self.profile.lock_id,)
                    ).fetchone()[0]
                )
            except Exception:
                raise CpiStorageError("cpi_timeseries_storage_failed") from None
            if not acquired:
                raise CpiStorageError("cpi_timeseries_busy")
            try:
                yield
            except BaseException:
                failed = True
                raise
            finally:
                if acquired:
                    try:
                        released = self.connection.execute(
                            "SELECT pg_advisory_unlock(%s)", (self.profile.lock_id,)
                        ).fetchone()[0]
                        if not released:
                            raise CpiStorageError("cpi_timeseries_lock_lost")
                    except Exception:
                        self.connection.close()
                        if not failed:
                            raise CpiStorageError(
                                "cpi_timeseries_lock_release_failed"
                            ) from None

    @contextmanager
    def read_snapshot(self):
        """One committed snapshot; readers never take the family writer lock."""
        with self._lock:
            self._require_top_level_transaction()
            with self.connection.transaction():
                self.connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                yield

    def ensure_schema(self):
        """Create a new relational schema; an old schema needs explicit migration."""
        try:
            with self.connection.transaction():
                self.connection.execute(
                    f"CREATE SCHEMA IF NOT EXISTS {self.profile.schema}"
                )
                owner = self.connection.execute(
                    "SELECT pg_get_userbyid(nspowner)=current_user "
                    "FROM pg_namespace WHERE nspname=%s",
                    (self.profile.schema,),
                ).fetchone()
                if owner != (True,):
                    raise CpiStorageError("cpi_timeseries_schema_owner_mismatch")
                self.connection.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.profile.schema}.batches (
                    batch_id text PRIMARY KEY,
                    raw_sha256 text NOT NULL,
                    observed_at timestamptz NOT NULL,
                    request jsonb NOT NULL,
                    provider_attempts integer NOT NULL CHECK(provider_attempts >= 0)
                )""")
                self.connection.execute(_revision_ddl(profile=self.profile))
                kind = self.connection.execute(
                    "SELECT relkind FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=%s AND c.relname='latest'",
                    (self.profile.schema,),
                ).fetchone()
                if kind is None:
                    self.connection.execute(
                        _latest_view_sql(event_columns=True, profile=self.profile)
                    )
                elif kind != ("v",):
                    raise CpiStorageError("cpi_timeseries_schema_migration_required")
                rows = self.connection.execute(
                    "SELECT table_name,column_name,data_type "
                    "FROM information_schema.columns "
                    "WHERE table_schema=%s "
                    "AND table_name IN ('batches','revisions','latest')",
                    (self.profile.schema,),
                ).fetchall()
                revisions = _revision_shape(self.profile)
                expected = {
                    "batches": {
                        "batch_id": "text",
                        "raw_sha256": "text",
                        "observed_at": "timestamp with time zone",
                        "request": "jsonb",
                        "provider_attempts": "integer",
                    },
                    "revisions": revisions,
                    "latest": {
                        **revisions,
                        **(
                            {
                                "value_unit": "text",
                                "seasonal_adjustment": "text",
                                "annual_rate": "boolean",
                                "chained_base": "text",
                            }
                            if self.profile is CONSUMPTION_PROFILE
                            else {}
                        ),
                    },
                }
                actual = {name: {} for name in expected}
                for table, column, data_type in rows:
                    actual[table][column] = data_type
                if actual != expected:
                    raise CpiStorageError("cpi_timeseries_schema_shape_mismatch")
                self.connection.execute(_rates_view_sql(profile=self.profile))
                if self.profile is JOLTS_PROFILE:
                    from market_data_foundation.historical.jolts_timeseries import (
                        latest_view_sql,
                    )

                    self.connection.execute(latest_view_sql())
                if self.profile is EMPLOYMENT_PROFILE:
                    from market_data_foundation.historical import (
                        employment_timeseries,
                    )

                    self.connection.execute(employment_timeseries.latest_view_sql())
        except CpiStorageError:
            raise
        except Exception:
            raise CpiStorageError("cpi_timeseries_schema_failed") from None

    def read_range(
        self, series_ids, start_month, end_month, as_of=None, vintage_mode="observed"
    ):
        _validate_series(series_ids, profile=self.profile)
        try:
            fields = ",".join(
                "r." + name for name in _revision_columns(self.profile).split(",")
            )
            params = (
                list(series_ids),
                _month_date(start_month),
                _month_date(end_month),
            )
            if as_of is None and vintage_mode == "observed":
                source, distinct, condition, order = (
                    "latest",
                    "",
                    "",
                    "r.series_id,r.month",
                )
            else:
                source = "revisions"
                distinct = "DISTINCT ON(r.series_id,r.month) "
                if vintage_mode == "available":
                    condition = (
                        "AND r.available_at IS NOT NULL "
                        "AND NOT EXISTS (SELECT 1 FROM "
                        f"{self.profile.schema}.revisions unconfirmed "
                        "WHERE unconfirmed.series_id=r.series_id "
                        "AND unconfirmed.month=r.month "
                        "AND unconfirmed.revision > r.revision "
                        "AND unconfirmed.available_at IS NULL "
                        + (
                            "AND unconfirmed.observed_at <= %s "
                            if as_of is not None
                            else ""
                        )
                        + ") "
                    )
                    if as_of is not None:
                        params += (as_of,)
                        condition += "AND r.available_at <= %s "
                else:
                    condition = "AND r.observed_at <= %s "
                order = "r.series_id,r.month,r.revision DESC"
                if as_of is not None:
                    params += (as_of,)
            rows = self.connection.execute(
                f"SELECT {distinct}{fields},b.raw_sha256,"
                "r.available_at,r.version_status,r.evidence_url "
                f"FROM {self.profile.schema}.{source} r "
                f"JOIN {self.profile.schema}.batches b USING(batch_id) "
                "WHERE r.series_id=ANY(%s) AND r.month BETWEEN %s AND %s "
                + condition
                + "ORDER BY "
                + order,
                params,
            ).fetchall()
            return [_row_document(row, profile=self.profile) for row in rows]
        except Exception:
            raise CpiStorageError("cpi_timeseries_storage_read_failed") from None

    def withdraw_range(self, series_ids, start_month, end_month, *, evidence_url):
        return _withdraw(self, series_ids, start_month, end_month, evidence_url)

    def _require_top_level_transaction(self):
        info = getattr(self.connection, "info", None)
        if (
            info is not None
            and info.transaction_status != psycopg.pq.TransactionStatus.IDLE
        ):
            raise CpiStorageError("cpi_timeseries_top_level_transaction_required")

    def confirm_current_availability(self, series_ids, start_month, end_month):
        """Explicitly attest that committed latest rows are consumable now.

        A fresh top-level transaction sees committed data, and the table lock
        prevents any writer replacing selected latest rows before confirmation.
        No historical timestamp or statistical version is manufactured.
        """
        _validate_series(series_ids, profile=self.profile)
        self._require_top_level_transaction()
        try:
            with self.operation():
                with self.connection.transaction():
                    self.connection.execute(
                        f"LOCK TABLE {self.profile.schema}.revisions "
                        "IN SHARE ROW EXCLUSIVE MODE"
                    )
                    rows = self.connection.execute(
                        f"SELECT series_id,month,revision "
                        f"FROM {self.profile.schema}.latest "
                        "WHERE series_id=ANY(%s) AND month BETWEEN %s AND %s "
                        "AND available_at IS NULL ORDER BY series_id,month",
                        (
                            list(series_ids),
                            _month_date(start_month),
                            _month_date(end_month),
                        ),
                    ).fetchall()
                    at = self._clock() if rows else None
                    for series, month, revision in rows:
                        self.connection.execute(
                            f"UPDATE {self.profile.schema}.revisions "
                            "SET available_at=%s "
                            "WHERE series_id=%s AND month=%s AND revision=%s "
                            "AND available_at IS NULL",
                            (at, series, month, revision),
                        )
                return {
                    "confirmed_points": len(rows),
                    "confirmed_at": None if at is None else at.isoformat(),
                }
        except CpiStorageError:
            raise
        except Exception:
            raise CpiStorageError(
                "cpi_timeseries_confirmation_outcome_unknown"
            ) from None

    def save_batches(
        self,
        batches,
        *,
        restoration_evidence_url=None,
        restoration_expected_points=None,
        _withdrawal_evidence_url=None,
    ):
        evidence = _withdrawal_evidence_url or restoration_evidence_url
        if evidence is not None:
            validate_evidence_url(evidence, profile=self.profile)
        state = "withdrawn" if _withdrawal_evidence_url else "active"
        expected = _restoration_expectations(
            restoration_expected_points, restoration_evidence_url, profile=self.profile
        )
        self._require_top_level_transaction()
        batches = tuple(batches)
        _validate_batches(batches, profile=self.profile)
        for batch in batches:
            locator = self.raw_store.put_verified(batch.raw)
            if locator != raw_locator(digest(batch.raw), profile=self.profile):
                raise CpiStorageError("cpi_timeseries_raw_locator_mismatch")
        points = revisions = 0
        inserted = []
        try:
            with self.connection.transaction():
                for batch in sorted(batches, key=lambda b: b.observed_at):
                    bid = batch_identity(batch)
                    self.connection.execute(
                        f"INSERT INTO {self.profile.schema}.batches "
                        "(batch_id,raw_sha256,observed_at,request,provider_attempts) "
                        "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(batch_id) DO NOTHING",
                        (
                            bid,
                            digest(batch.raw),
                            batch.observed_at,
                            Jsonb(batch.request),
                            batch.attempts,
                        ),
                    )
                    for point in batch.points:
                        values = _relational_content(point, profile=self.profile)
                        old = self.connection.execute(
                            "SELECT revision,observed_at,"
                            f"{_value_columns(self.profile)},"
                            "version_status "
                            f"FROM {self.profile.schema}.revisions "
                            "WHERE series_id=%s AND month=%s "
                            "ORDER BY revision DESC LIMIT 1",
                            (point.series_id, _month_date(point.month)),
                        ).fetchone()
                        old_state = old[-1] if old is not None else None
                        if old_state == "withdrawn" and (
                            state == "withdrawn"
                            or restoration_evidence_url is None
                            or not _restoration_matches(point, expected)
                        ):
                            continue
                        _check_pce_partial(
                            None if old is None else old[2:-1],
                            values,
                            profile=self.profile,
                            state=state,
                        )
                        if (
                            old is not None
                            and old_state == state
                            and tuple(old[2:-1]) == values
                        ):
                            continue
                        if old is not None and batch.observed_at <= old[1]:
                            raise CpiStorageError(
                                "cpi_timeseries_observation_order_conflict"
                            )
                        revision = 1 if old is None else old[0] + 1
                        parameters = (
                            point.series_id,
                            _month_date(point.month),
                            revision,
                            batch.observed_at,
                            bid,
                            *values[:-2],
                            Jsonb(values[-2]) if values[-2] is not None else None,
                            Jsonb(values[-1]),
                            state,
                            evidence,
                        )
                        self.connection.execute(
                            f"INSERT INTO {self.profile.schema}.revisions "
                            f"({_revision_columns(self.profile)},"
                            "version_status,evidence_url) "
                            f"VALUES({','.join(['%s'] * len(parameters))})",
                            parameters,
                        )
                        inserted.append(
                            (point.series_id, _month_date(point.month), revision)
                        )
                        points += 1
                        revisions += int(old is not None)
        except CpiStorageError:
            raise
        except psycopg.OperationalError:
            raise CpiStorageError("cpi_timeseries_storage_outcome_unknown") from None
        except Exception:
            raise CpiStorageError("cpi_timeseries_storage_write_failed") from None
        # This timestamp is sampled only after the data transaction has returned
        # successfully. A lost COMMIT acknowledgement never reaches this phase.
        # Failure here leaves data durable but availability unknown; never infer
        # the old commit time or stamp legacy rows on subsequent unchanged reads.
        if inserted:
            confirmed_at = self._clock()
            try:
                with self.connection.transaction():
                    for series, month, revision in inserted:
                        self.connection.execute(
                            f"UPDATE {self.profile.schema}.revisions "
                            "SET available_at=%s "
                            "WHERE series_id=%s AND month=%s AND revision=%s "
                            "AND available_at IS NULL",
                            (confirmed_at, series, month, revision),
                        )
            except Exception:
                raise CpiStorageError(
                    "cpi_timeseries_confirmation_outcome_unknown"
                ) from None
        return {"saved_points": points, "saved_revisions": revisions}

    def status(self):
        with self.read_snapshot():
            return self._status()

    def _status(self):
        try:
            latest = self.connection.execute(
                "SELECT series_id,to_char(max(month),'YYYY-MM') "
                f"FROM {self.profile.schema}.latest "
                f"WHERE {_level_column(self.profile)} IS NOT NULL "
                "AND version_status='active' "
                "GROUP BY series_id"
            ).fetchall()
            count = self.connection.execute(
                "SELECT count(*),count(*)-count(DISTINCT(series_id,month)) "
                f"FROM {self.profile.schema}.revisions"
            ).fetchone()
            batches = self.connection.execute(
                f"SELECT count(*) FROM {self.profile.schema}.batches"
            ).fetchone()[0]
        except Exception:
            raise CpiStorageError("cpi_timeseries_storage_read_failed") from None
        return {
            "schema": self.profile.schema,
            "latest_months": {s: dict(latest).get(s) for s in self.profile.series_ids},
            "observation_count": count[0],
            "revision_count": count[1],
            "batch_count": batches,
        }

    def close(self):
        self.connection.close()


class InMemoryTimeSeriesStore:
    """Deterministic test port; runtime build_service always uses PostgreSQL."""

    def __init__(self, raw_store=None, *, clock=None, profile=CPI_PROFILE):
        self.profile = validate_profile(profile)
        _validate_raw_store(raw_store, self.profile)
        self.raw_store = raw_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._history = {}
        self._batches = {}
        self._lock = RLock()

    @contextmanager
    def operation(self):
        with self._lock:
            yield

    read_snapshot = operation

    def read_range(
        self, series_ids, start_month, end_month, as_of=None, vintage_mode="observed"
    ):
        _validate_series(series_ids, profile=self.profile)
        result = []
        for (series, month), history in sorted(self._history.items()):
            if series not in series_ids or not start_month <= month <= end_month:
                continue
            options = [
                d
                for d in history
                if (vintage_mode != "available" or d.get("available_at") is not None)
                and (
                    as_of is None
                    or datetime.fromisoformat(
                        d["available_at"]
                        if vintage_mode == "available"
                        else d["observed_at"]
                    )
                    <= as_of
                )
            ]
            if options:
                selected = options[-1]
                if vintage_mode == "available" and any(
                    d["revision"] > selected["revision"]
                    and d.get("available_at") is None
                    and (
                        as_of is None
                        or datetime.fromisoformat(d["observed_at"]) <= as_of
                    )
                    for d in history
                ):
                    continue
                result.append(copy.deepcopy(selected))
        return result

    def withdraw_range(self, series_ids, start_month, end_month, *, evidence_url):
        return _withdraw(self, series_ids, start_month, end_month, evidence_url)

    def confirm_current_availability(self, series_ids, start_month, end_month):
        _validate_series(series_ids, profile=self.profile)
        with self.operation():
            rows = [
                history[-1]
                for (series, month), history in self._history.items()
                if series in series_ids
                and start_month <= month <= end_month
                and history[-1].get("available_at") is None
            ]
            at = self._clock().isoformat() if rows else None
            for row in rows:
                row["available_at"] = at
            return {"confirmed_points": len(rows), "confirmed_at": at}

    def save_batches(
        self,
        batches,
        *,
        restoration_evidence_url=None,
        restoration_expected_points=None,
        _withdrawal_evidence_url=None,
    ):
        evidence = _withdrawal_evidence_url or restoration_evidence_url
        if evidence is not None:
            validate_evidence_url(evidence, profile=self.profile)
        state = "withdrawn" if _withdrawal_evidence_url else "active"
        expected = _restoration_expectations(
            restoration_expected_points, restoration_evidence_url, profile=self.profile
        )
        batches = tuple(batches)
        _validate_batches(batches, profile=self.profile)
        for batch in batches:
            if self.raw_store is not None:
                locator = self.raw_store.put_verified(batch.raw)
                if locator != raw_locator(digest(batch.raw), profile=self.profile):
                    raise CpiStorageError("cpi_timeseries_raw_locator_mismatch")
        history = copy.deepcopy(self._history)
        saved = revised = 0
        inserted = []
        for batch in sorted(batches, key=lambda b: b.observed_at):
            for point in batch.points:
                rows = history.setdefault((point.series_id, point.month), [])
                old_state = rows[-1].get("version_status", "active") if rows else None
                if old_state == "withdrawn" and (
                    state == "withdrawn"
                    or restoration_evidence_url is None
                    or not _restoration_matches(point, expected)
                ):
                    continue
                values = _relational_content(point, profile=self.profile)
                old_values = (
                    _relational_content(
                        SimpleNamespace(
                            series_id=point.series_id,
                            index_value=_numeric(rows[-1]["index_value"]),
                            provider_calculations=rows[-1]["provider_calculations"],
                            footnotes=rows[-1]["footnotes"],
                        ),
                        profile=self.profile,
                    )
                    if rows
                    else None
                )
                _check_pce_partial(
                    old_values, values, profile=self.profile, state=state
                )
                if rows and old_state == state and old_values == values:
                    continue
                if rows and batch.observed_at <= datetime.fromisoformat(
                    rows[-1]["observed_at"]
                ):
                    raise CpiStorageError("cpi_timeseries_observation_order_conflict")
                document = _point_document(
                    point, batch, len(rows) + 1, profile=self.profile
                )
                document.update(version_status=state, evidence_url=evidence)
                if state == "withdrawn":
                    document["availability"] = "withdrawn"
                rows.append(document)
                inserted.append(document)
                saved += 1
                revised += int(len(rows) > 1)
        self._history = history
        self._batches.update({batch_identity(b): b for b in batches})
        if inserted:
            at = self._clock().isoformat()
            for document in inserted:
                document["available_at"] = at
        return {"saved_points": saved, "saved_revisions": revised}

    def status(self):
        with self.read_snapshot():
            return self._status()

    def _status(self):
        latest = {s: None for s in self.profile.series_ids}
        for (s, month), rows in self._history.items():
            if (
                rows[-1].get("version_status") != "withdrawn"
                and rows[-1]["index_value"] is not None
                and (latest[s] is None or month > latest[s])
            ):
                latest[s] = month
        return {
            "schema": self.profile.schema,
            "latest_months": latest,
            "observation_count": sum(map(len, self._history.values())),
            "revision_count": sum(len(v) - 1 for v in self._history.values()),
            "batch_count": len(self._batches),
        }

    def close(self):
        pass
