"""Importable operator entry point; caller supplies connection and transaction."""

from market_data_foundation.historical.pce_timeseries_migration import (
    migrate_pce_percent_schema,
)

__all__ = ["migrate_pce_percent_schema"]
