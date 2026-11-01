"""Public consumption contract over the shared monthly workbench operations."""

from market_data_foundation.historical.bls_timeseries_profile import CONSUMPTION_PROFILE
from market_data_foundation.historical.consumption_schedule import (
    ConsumptionAutoStrategy,
)
from market_data_foundation.web.cpi_timeseries_api import TimeSeriesHost
from market_data_foundation.web.cpi_timeseries_jobs import WebError


class ConsumptionTimeSeriesHost(TimeSeriesHost):
    def __init__(self, *, scheduler, clock, **options):
        super().__init__(
            profile=CONSUMPTION_PROFILE,
            scheduler=scheduler,
            clock=clock,
            strategy=ConsumptionAutoStrategy(
                scheduler,
                scheduler.state_path.with_name("consumption-strategy.json"),
                clock=clock,
            ),
            **options,
        )

    def api_help(self):
        common = super().api_help()
        return {
            **common,
            "release_records": {
                **common["release_records"],
                "schema_preparation": "explicit consumption CLI prepare-schema only",
            },
            "column_selection": (
                "columns CSV selects native numeric fields; metadata remains; "
                "omitted returns all applicable fields"
            ),
            "provider": "BEA",
            "source_id": "BEA",
            "dataset": "NIPA",
            "tables": ["T20600", "T20806"],
            "frequency": "M",
            "last_revised_is_publication_time": False,
            "series": CONSUMPTION_PROFILE.series_metadata,
            "measures": ["native"],
            "baskets": [],
            "examples": {
                "current": self.prefix
                + (
                    "/query?start=2026-01&end=2026-09&series=DPCERC,A072RC"
                    "&measure=native&local_only=false&query_mode=current"
                    "&as_of_basis=observed"
                ),
                "history": self.prefix
                + (
                    "/query?start=2026-01&end=2026-09&series=DPCERC,A072RC"
                    "&measure=native&local_only=true&query_mode=history"
                    "&as_of_basis=available&as_of=2026-11-01T20%3A00%3A00%2B08%3A00"
                ),
                "source_history": self.prefix
                + (
                    "/releases?read_mode=source_history&source_version=2026-10-29"
                    "&reference_month=2026-09"
                ),
            },
            "measure_semantics": (
                "native returns original amount/rate in value with per-series units"
            ),
            "columns": [
                "value",
                "mom_pct",
                "yoy_pct",
                "change_1m_ratio",
                "change_3m_ratio",
                "change_6m_ratio",
                "change_12m_ratio",
                "change_1m_percentage_points",
                "change_12m_percentage_points",
            ],
            "units": (
                "money: millions current/chained-2017 USD, SAAR levels; "
                "saving rate: percent, SA, not annualized"
            ),
            "calculation_method": (
                "Same-cutoff Decimal endpoints; final ratio 4 places, percent 2; "
                "saving rate only "
                "percentage-point changes; chained components nonadditive; "
                "no CPI deflation"
            ),
            "local_only_default": False,
            "basket_selection": "use series CSV; omitted all seven",
            "research_presentation": (
                "native money levels and saving rate, independent from PCE prices"
            ),
            "provider_status": "BEA notes retained; no inferred finality",
            "buffer_months": 12,
            "credential_binding": "macro/consumption:BEA; no provider fallback",
            "snapshot_semantics": (
                "two separately completed table requests, never atomic official vintage"
            ),
        }

    def query(self, params):
        if isinstance(params, dict) and params.get("measure", "native") != "native":
            raise WebError("consumption_measure_not_supported")
        return super().query(params)
