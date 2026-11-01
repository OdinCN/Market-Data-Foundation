"""Run the fail-closed retail CLI; embedding host must inject a service factory."""

from market_data_foundation.historical.retail_timeseries_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
