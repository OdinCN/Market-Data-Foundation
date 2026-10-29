"""Native R5 composition for the bounded BEA candidate. No implicit live IO."""

from market_data_foundation.canonical.model import require
from market_data_foundation.web.cpi_dev_host import BINDING as CPI_BINDING
from market_data_foundation.web.cpi_dev_host import OPERATOR, candidate_spec
from market_data_foundation.web.cpi_jobs import CpiPgJobsHost, Operator
from market_data_foundation.web.pce_api import create_preview
from market_data_foundation.web.pce_dev_host import (
    CANDIDATE_ORIGIN,
    ORIGIN,
    admitted,
    clock,
)


def create_native_preview(
    pce_host,
    cpi_host,
    *,
    ui_directory,
    enabled=False,
    observability_host=None,
    management_host=None,
    package_host=None,
    query_host=None,
    origin=CANDIDATE_ORIGIN,
):
    """Host supplies independent schema identities; never reuses the PCE scope for CPI.

    Both adapters are the existing nonproduction PG implementations. The outer
    admission gate permits R5 reads and, on the explicit DEV origin, the exact
    PCE submissions and Owner-verified CPI failed-INIT successor.
    Construction invokes no worker, DDL or preflight.
    """
    require(enabled is True, "pce_native_preview_opt_in_required")
    journal = cpi_host.journal()
    require(journal.binding == CPI_BINDING, "cpi_pg_binding_mismatch")

    def authorize(request):
        if request.method in {"GET", "HEAD"} and admitted(request, origin=origin):
            return Operator(OPERATOR, False)
        return None

    def authorize_backfill(request):
        if (
            origin == ORIGIN
            and admitted(request, origin=origin)
            and (
                request.method in {"GET", "HEAD"}
                or request.url.path == "/api/r5/cpi/jobs/backfill"
            )
        ):
            return Operator(OPERATOR, True)
        return None

    jobs = CpiPgJobsHost(
        journal,
        candidate_spec(),
        clock,
        authorize,
        authorize_backfill=authorize_backfill,
    )
    return create_preview(
        pce_host.journal(),
        pce_host.read_scope,
        clock,
        cpi_read_scope=cpi_host.read_scope(),
        cpi_jobs_host=jobs,
        observability_host=observability_host,
        management_host=management_host,
        package_host=package_host,
        query_host=query_host,
        ui_directory=ui_directory,
        enabled=True,
        origin=origin,
    )
