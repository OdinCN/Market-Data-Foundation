"""Exact existing PG/Garage/FRED capability binding, without a worker or Web hook."""

import tomllib
from pathlib import Path

from market_data_foundation.historical.bls_cpi_material_store import (
    BUCKET,
    GARAGE_ENDPOINT,
    GarageCredentials,
)
from market_data_foundation.historical.cpi_timeseries import DEFAULT_SECRET_PATH
from market_data_foundation.historical.financial_daily import (
    SERIES_IDS,
    FinancialError,
    permitted_series,
    require_source_permission,
    series_ids,
)
from market_data_foundation.historical.financial_daily_service import (
    FinancialDailyService,
)
from market_data_foundation.historical.financial_daily_source import DailyFredSource
from market_data_foundation.historical.financial_daily_store import (
    SCHEMA,
    GarageDailyRawStore,
    PostgresDailyStore,
)
from market_data_foundation.storage.market_dataset_postgres import (
    connect_dev,
    preflight,
)
from market_data_foundation.storage.postgres_runtime_config import DEFAULT_RUNTIME_PATH


def bound_fred_credential():
    """Reuse the existing macro FRED binding; never write credential configuration."""
    from market_data_foundation.historical.fred_alfred_vintage_live import load_api_key
    from market_data_foundation.web.package_catalog import PackageCatalog
    from market_data_foundation.web.package_store import LocalPackageStore
    from market_data_foundation.web.packages import PackageHost

    config = DEFAULT_SECRET_PATH.parent
    store = LocalPackageStore(config / "package-config-dev", PackageCatalog())
    host = PackageHost(
        store,
        "mdf-dev",
        credential_loader=lambda _environment, _package, source: (
            load_api_key(DEFAULT_SECRET_PATH) if source == "FRED" else None
        ),
    )
    key = host.load_credential("macro", "FRED")
    if key is None:
        raise FinancialError("financial_bound_credential_missing")
    return key


DEFAULT_CONTROL = Path(
    "/srv/market-data-foundation-control/transactions/MDF-CLAIMS-20261102-R1/artifacts/segment5/dev/fred-control.json"
)


def build_service(
    *,
    write=False,
    live_opt_in=False,
    selected=SERIES_IDS,
    credential_loader=None,
    runtime_path=DEFAULT_RUNTIME_PATH,
    secret_path=DEFAULT_SECRET_PATH,
    schema=SCHEMA,
    source_binding="origin-r1",
):
    series_ids(selected)
    if live_opt_in:
        if not write:
            raise FinancialError("financial_live_collection_write_required")
        if source_binding not in {"origin-r1", "fred-r1"}:
            raise FinancialError("financial_source_binding_invalid")
        if source_binding == "fred-r1":
            if not permitted_series(selected):
                require_source_permission(selected[0])
            if credential_loader is None:
                raise FinancialError("financial_bound_credential_loader_required")
    connection = None
    try:
        runtime = tomllib.loads(Path(runtime_path).read_text())
        secret = tomllib.loads(Path(secret_path).read_text())
        s3 = runtime["s3"]
        if (
            runtime["storage"]["backend"] != "s3"
            or s3["endpoint_url"] != GARAGE_ENDPOINT
            or s3["bucket"] != BUCKET
            or s3["region"] != "garage"
            or s3["key_prefix"] != ""
            or s3["addressing_style"] != "path"
        ):
            raise FinancialError("financial_storage_binding_invalid")
        connection = connect_dev(
            secret["postgres"]["password"], runtime_path=runtime_path
        )
        preflight(connection)
        raw = (
            GarageDailyRawStore(
                GarageCredentials(
                    secret["s3"]["access_key_id"], secret["s3"]["secret_access_key"]
                )
            )
            if write
            else None
        )
        source = None
        if live_opt_in and source_binding == "origin-r1":
            from market_data_foundation.historical.financial_daily_origin import (
                OriginSource,
            )

            source = OriginSource.live(
                control_directory=Path(
                    "/srv/market-data-foundation-control/transactions/MDF-MACRO-RELIABLE-UPDATE-20261104-R1/runtime/financial-daily-origin-control"
                ),
                live_opt_in=True,
                raw_sink=lambda _request, response, _observed, _attempts: (
                    raw.put_verified(response.body)
                ),
            )
        elif live_opt_in:
            source = DailyFredSource.live(
                series=selected,
                credential_loader=credential_loader,
                request_control_path=DEFAULT_CONTROL,
                live_opt_in=True,
                raw_sink=lambda _request, response, _observed, _attempts: (
                    raw.put_verified(response.body)
                ),
            )
        return FinancialDailyService(
            PostgresDailyStore(connection, raw, schema=schema), source
        )
    except FinancialError:
        if connection:
            connection.close()
        raise
    except Exception:
        if connection:
            connection.close()
        raise FinancialError("financial_dev_binding_unavailable") from None
