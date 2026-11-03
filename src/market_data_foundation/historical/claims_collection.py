"""Explicit Claims collection and fixed DEV capability binding."""

import tomllib
from datetime import UTC, datetime
from pathlib import Path

from market_data_foundation.historical.bls_cpi_material_store import (
    BUCKET,
    GARAGE_ENDPOINT,
    GarageCredentials,
)
from market_data_foundation.historical.claims_contract import (
    SERIES_IDS,
    ClaimsError,
    buffer_start,
    validate_series,
    weeks,
)
from market_data_foundation.historical.claims_source import (
    ClaimsFredSource,
    ClaimsHttpsTransport,
)
from market_data_foundation.historical.claims_store import (
    GarageClaimsRawStore,
    PostgresClaimsStore,
)
from market_data_foundation.historical.claims_timeseries import ClaimsTimeSeriesService
from market_data_foundation.historical.cpi_timeseries import DEFAULT_SECRET_PATH
from market_data_foundation.historical.cpi_timeseries_store import canonical, digest
from market_data_foundation.historical.fred_alfred_vintage_live import load_api_key
from market_data_foundation.storage.market_dataset_postgres import connect_dev
from market_data_foundation.storage.postgres_runtime_config import DEFAULT_RUNTIME_PATH


def readback_digest(result):
    return digest(
        canonical(
            [
                [r["series_id"], r["week_ending"], r["value"], r.get("provenance")]
                for r in result["rows"]
            ]
        ).encode()
    )


class ClaimsCollectionService(ClaimsTimeSeriesService):
    def _collect(self, start, end, *, series_ids=SERIES_IDS, refresh=False):
        validate_series(series_ids)
        targets = weeks(buffer_start(start), end)
        if not targets:
            raise ClaimsError("claims_empty_collection_range")
        if self.source is None:
            raise ClaimsError("claims_live_opt_in_required")
        selected = series_ids
        fetch_start = buffer_start(start)
        if not refresh:
            with self.store.read_snapshot():
                known = {
                    (r.series_id, r.week_ending)
                    for r in self.store.read_range(series_ids, buffer_start(start), end)
                }
            missing = {
                (sid, w) for sid in series_ids for w in targets if (sid, w) not in known
            }
            selected = tuple(
                sid for sid in series_ids if any(key[0] == sid for key in missing)
            )
            if known and missing:
                fetch_start = min(week for _, week in missing)
        requests, attempts = self.source.request_count, self.source.attempts
        batches = self.source.fetch(selected, fetch_start, end) if selected else ()
        saved = (
            self.store.save_batches(batches)
            if batches
            else dict(saved_points=0, saved_revisions=0)
        )
        result = self.query_range(start, end, series_ids=series_ids)
        coverage = {}
        for sid in series_ids:
            rows = [r for r in result["rows"] if r["series_id"] == sid]
            values = [r["week_ending"] for r in rows if r["value"] is not None]
            latest = max(values, default=None)
            coverage[sid] = dict(
                latest_week=latest,
                available=len(values),
                expected=len(rows),
                missing_weeks=[r["week_ending"] for r in rows if r["value"] is None],
                trailing_missing_weeks=[
                    r["week_ending"]
                    for r in rows
                    if r["value"] is None
                    and (latest is None or r["week_ending"] > latest)
                ],
            )
        return dict(
            saved,
            status=result["status"],
            coverage=result["coverage"],
            series_coverage=coverage,
            provider_requests=self.source.request_count - requests,
            provider_attempts=self.source.attempts - attempts,
            start_date=start,
            end_date=end,
            readback_digest=readback_digest(result),
        )

    def init_range(self, start, end, **options):
        return self._collect(start, end, **options)

    def update_range(self, start, end, **options):
        return self._collect(start, end, refresh=True, **options)

    def close(self):
        self.store.close()


def build_service(
    *,
    live_opt_in=False,
    runtime_path=DEFAULT_RUNTIME_PATH,
    secret_path=DEFAULT_SECRET_PATH,
    max_requests=16,
    credential_loader=None,
    request_control_path=Path(
        "/srv/market-data-foundation/config/local/claims-fred-request-control.json"
    ),
):
    connection = None
    try:
        if type(live_opt_in) is not bool:
            raise ClaimsError("claims_live_opt_in_invalid")
        runtime = tomllib.loads(Path(runtime_path).read_text())
        secret = tomllib.loads(Path(secret_path).read_text())
        s3 = runtime["s3"]
        if not (
            runtime["storage"]["backend"] == "s3"
            and s3["endpoint_url"] == GARAGE_ENDPOINT
            and s3["bucket"] == BUCKET
            and s3["region"] == "garage"
            and s3["key_prefix"] == ""
            and s3["addressing_style"] == "path"
        ):
            raise ClaimsError("claims_storage_binding_invalid")
        raw = GarageClaimsRawStore(
            GarageCredentials(
                secret["s3"]["access_key_id"], secret["s3"]["secret_access_key"]
            )
        )
        connection = connect_dev(
            secret["postgres"]["password"], runtime_path=runtime_path
        )

        def sink(request, response, observed, attempts):
            raw.put_verified(response.body)

        source = (
            ClaimsFredSource(
                transport=ClaimsHttpsTransport(
                    credential_loader()
                    if credential_loader is not None
                    else load_api_key(Path(secret_path))
                ),
                live_opt_in=True,
                max_requests=max_requests,
                raw_sink=sink,
                request_control_path=request_control_path,
            )
            if live_opt_in
            else None
        )
        return ClaimsCollectionService(
            PostgresClaimsStore(connection, raw),
            source,
            clock=lambda: datetime.now(UTC),
        )
    except ClaimsError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise ClaimsError("claims_configuration_failed") from None
