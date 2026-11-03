"""Claims routes share the macro operator boundary and strict request parsing."""

from fastapi.responses import JSONResponse

from market_data_foundation.historical.calendar_resilience import (
    failure as safe_failure,
)
from market_data_foundation.historical.claims_contract import ClaimsError
from market_data_foundation.historical.cpi_release_schedule import CpiScheduleError
from market_data_foundation.web.claims_host import PREFIX
from market_data_foundation.web.cpi_timeseries_jobs import WebError
from market_data_foundation.web.gdp_api import timeseries_router as macro_router
from market_data_foundation.web.package_catalog import PackageError


def failure(error):
    if isinstance(error, PackageError):
        return JSONResponse(error.failure.model_dump(), error.http_status)
    code = safe_failure(error, "claims_local_failure")["classification"]
    status = (
        error.status
        if isinstance(error, WebError)
        else (
            400
            if isinstance(error, (ClaimsError, CpiScheduleError))
            and not any(
                t in code
                for t in ("storage", "schema", "snapshot", "configuration", "commit")
            )
            else 503
        )
    )
    return JSONResponse(dict(status="ERROR", classification=code), status)


def timeseries_router(host):
    return macro_router(
        host,
        prefix=PREFIX,
        family="claims",
        period_schema=dict(
            type="string", format="date", pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
        ),
        period_description="YYYY-MM-DD inclusive natural day",
        series_description="six native FRED IDs CSV; all six by default",
        basis_description="available default / observed; one cutoff for every operand",
        release_fields=(
            "as_of",
            "read_mode",
            "event_id",
            "reference_week",
            "source_version",
        ),
        error_handler=failure,
    )
