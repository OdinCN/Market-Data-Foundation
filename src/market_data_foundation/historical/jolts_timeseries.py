"""JOLTS foundation using the existing BLS source, query and revision ports.

No Web registration, database connection, DDL or live execution on import.
"""

import tomllib
from pathlib import Path

from market_data_foundation.historical import cpi_timeseries as _core
from market_data_foundation.historical.bls_timeseries_profile import JOLTS_PROFILE
from market_data_foundation.historical.cpi_request_control import CpiRequestControl
from market_data_foundation.historical.cpi_timeseries_source import BlsRangeSource
from market_data_foundation.historical.cpi_timeseries_source import (
    parse_response as _parse_response,
)
from market_data_foundation.historical.macro_release_material import Material

SERIES_IDS = JOLTS_PROFILE.series_ids
SERIES_METADATA = JOLTS_PROFILE.series_metadata
JoltsTimeSeriesError = _core.CpiTimeSeriesError


class BlsJoltsRangeSource(BlsRangeSource):
    max_range_requests = 8

    def __init__(self, *, allow_live=False, **options):
        if type(allow_live) is not bool:
            raise JoltsTimeSeriesError("jolts_live_opt_in_invalid")
        self._live_allowed = allow_live
        options.setdefault("secret_path", _core.DEFAULT_SECRET_PATH)
        options.setdefault("max_transport_attempts", 1)
        options.setdefault("max_requests", self.max_range_requests)
        if options.get("request_control") is None:
            options["request_control"] = CpiRequestControl(
                _core.DEFAULT_REQUEST_CONTROL_PATH
            )
        super().__init__(profile=JOLTS_PROFILE, **options)

    def fetch(self, series_ids, start_month, end_month):
        if not self._live_allowed:
            raise JoltsTimeSeriesError("jolts_live_opt_in_required")
        return super().fetch(series_ids, start_month, end_month)


def parse_response(raw, series_ids, start_month, end_month, **options):
    return _parse_response(
        raw, series_ids, start_month, end_month, profile=JOLTS_PROFILE, **options
    )


def parse_native_metadata(raw, series_ids=SERIES_IDS):
    """Validate original BLS native-report catalogs; never use an API alias.

    Catalog proof establishes identity, scope, SA and Rate/Level only. It does
    not supply rate denominators, native starts, event dates or revision rules.
    """
    if (
        type(series_ids) is not tuple
        or not series_ids
        or len(set(series_ids)) != len(series_ids)
        or any(s not in SERIES_IDS for s in series_ids)
    ):
        raise JoltsTimeSeriesError("jolts_metadata_series_invalid")
    material = Material(raw)
    tables = {}
    for table, row in material.rows:
        if table.startswith("catalog"):
            tables.setdefault(table, []).append(row)
    result = {}
    names = {
        "job_openings": "Job openings",
        "hires": "Hires",
        "quits": "Quits",
        "layoffs_discharges": "Layoffs and discharges",
    }
    for table, rows in tables.items():
        fields = {}
        for row in rows:
            if len(row) == 2:
                if row[0] in fields:
                    raise JoltsTimeSeriesError("jolts_metadata_duplicate_field")
                fields[row[0]] = row[1]
        sid = fields.get("Series Id:")
        if sid not in series_ids or sid in result:
            raise JoltsTimeSeriesError("jolts_metadata_identity_invalid")
        metadata = SERIES_METADATA[sid]
        expected = {
            "Industry:": "Total nonfarm",
            "State/Region:": "Total US",
            "Area:": "All areas",
            "Size Class:": "All size classes",
            "Data Element:": names[metadata["indicator"]],
            "Rate/Level:": "Rate"
            if metadata["statistic"] == "official_rate"
            else "Level - In Thousands",
        }
        if ["Seasonally adjusted"] not in rows or any(
            fields.get(key) != value for key, value in expected.items()
        ):
            raise JoltsTimeSeriesError("jolts_metadata_scope_or_unit_invalid")
        result[sid] = dict(fields, table=table, seasonal_adjustment="SA")
    if set(result) != set(series_ids):
        raise JoltsTimeSeriesError("jolts_metadata_series_missing")
    return result


class JoltsTimeSeriesService(_core.CpiTimeSeriesService):
    def __init__(self, store, source):
        super().__init__(store, source, profile=JOLTS_PROFILE)

    def query_range(self, start_month, end_month, *, allow_upstream=False, **options):
        """Local by default; temporary source reads require an explicit choice."""
        return super().query_range(
            start_month, end_month, allow_upstream=allow_upstream, **options
        )


def build_service(*, allow_live=False, request_control_path=None, **options):
    """Writer over shared PG/Garage and BLS admission; no bootstrap."""
    source = BlsJoltsRangeSource(
        allow_live=allow_live,
        registered=options.get("registered", True),
        credential_loader=options.get("credential_loader"),
        secret_path=options.get("secret_path", _core.DEFAULT_SECRET_PATH),
        request_control=CpiRequestControl(
            _core.DEFAULT_REQUEST_CONTROL_PATH
            if request_control_path is None
            else Path(request_control_path)
        ),
        max_requests=8,
        max_transport_attempts=1,
    )
    service = _core.build_service(profile=JOLTS_PROFILE, **options)
    return JoltsTimeSeriesService(service.store, source)


def build_read_service(*, runtime_path, secret_path):
    """Short read-only connection; no source credential, raw access or DDL."""
    from market_data_foundation.historical.cpi_timeseries_store import (
        PostgresTimeSeriesStore,
    )
    from market_data_foundation.storage.market_dataset_postgres import connect_dev

    connection = None
    try:
        secret = tomllib.loads(Path(secret_path).read_text())
        connection = connect_dev(
            secret["postgres"]["password"], runtime_path=Path(runtime_path)
        )
        connection.execute("SET default_transaction_read_only=on")
        return JoltsTimeSeriesService(
            PostgresTimeSeriesStore(connection, None, profile=JOLTS_PROFILE),
            BlsJoltsRangeSource(),
        )
    except Exception:
        if connection is not None:
            connection.close()
        raise JoltsTimeSeriesError("jolts_read_configuration_failed") from None


def latest_view_sql():
    units = " ".join(
        f"WHEN '{sid}' THEN '{meta['value_unit']}'"
        for sid, meta in SERIES_METADATA.items()
    )
    return f"""CREATE OR REPLACE VIEW {JOLTS_PROFILE.schema}.jolts_latest AS
        SELECT series_id,month,revision AS local_revision,observed_at,available_at,
            version_status,batch_id,index_value AS value,
            CASE series_id {units} END AS value_unit,
            coalesce(provider_extra,'{{}}'::jsonb) || jsonb_build_object(
                'pct_changes',coalesce(provider_extra->'pct_changes','{{}}'::jsonb)
                || jsonb_strip_nulls(jsonb_build_object(
                    '1',bls_pct_1m::text,'3',bls_pct_3m::text,
                    '6',bls_pct_6m::text,'12',bls_pct_12m::text)))
                AS provider_calculations,footnotes
        FROM {JOLTS_PROFILE.schema}.latest"""


def rates_view_sql():
    """Reviewable SQL only; independent official rates and same-series changes."""
    schema = JOLTS_PROFILE.schema
    rates = ",".join(
        "'" + sid + "'"
        for sid, meta in SERIES_METADATA.items()
        if meta["statistic"] == "official_rate"
    )
    units = " ".join(
        f"WHEN '{sid}' THEN '{meta['value_unit']}'"
        for sid, meta in SERIES_METADATA.items()
    )
    return f"""CREATE OR REPLACE VIEW {schema}.jolts_timeseries_rates AS
        SELECT c.series_id,c.month,c.revision,c.observed_at,c.available_at,
            c.version_status,c.batch_id,c.footnotes,c.provider_extra,
            c.index_value AS value,
            CASE c.series_id {units} END AS value_unit,
            CASE WHEN c.series_id NOT IN ({rates})
                THEN c.index_value-p1.index_value END AS change_1m_level,
            CASE WHEN c.series_id IN ({rates})
                THEN c.index_value-p1.index_value END
                AS change_1m_percentage_points,
            CASE WHEN c.series_id IN ({rates})
                THEN c.index_value-p12.index_value END
                AS change_12m_percentage_points,
            CASE WHEN c.series_id NOT IN ({rates})
                THEN round(c.index_value/nullif(p1.index_value,0)-1,4) END
                AS change_1m_ratio,
            CASE WHEN c.series_id NOT IN ({rates})
                THEN round(c.index_value/nullif(p12.index_value,0)-1,4) END
                AS change_12m_ratio
        FROM {schema}.latest c
        LEFT JOIN {schema}.latest p1 ON p1.series_id=c.series_id
            AND p1.month=(c.month-interval '1 month')::date
            AND p1.version_status='active'
        LEFT JOIN {schema}.latest p12 ON p12.series_id=c.series_id
            AND p12.month=(c.month-interval '12 months')::date
            AND p12.version_status='active'
        WHERE c.version_status='active'"""
