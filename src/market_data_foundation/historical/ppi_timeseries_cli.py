"""PPI CLI entry point using the shared, profile-bound time-series operations."""

from market_data_foundation.historical.bls_timeseries_profile import PPI_PROFILE
from market_data_foundation.historical.cpi_timeseries_cli import main as shared_main


def main(argv=None, *, service_factory=None, scheduler_factory=None):
    return shared_main(
        argv,
        service_factory=service_factory,
        scheduler_factory=scheduler_factory,
        profile=PPI_PROFILE,
    )


if __name__ == "__main__":
    raise SystemExit(main())
