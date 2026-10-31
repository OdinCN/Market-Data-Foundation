"""BEA-specific public contract over the shared time-series Web operations."""

from market_data_foundation.historical.bls_timeseries_profile import PCE_PROFILE
from market_data_foundation.historical.pce_auto_strategy import PceAutoStrategy
from market_data_foundation.web.cpi_timeseries_api import TimeSeriesHost
from market_data_foundation.web.cpi_timeseries_jobs import WebError


class PceTimeSeriesHost(TimeSeriesHost):
    def __init__(self, *, scheduler, clock, **options):
        super().__init__(
            profile=PCE_PROFILE,
            scheduler=scheduler,
            clock=clock,
            strategy=PceAutoStrategy(
                scheduler,
                scheduler.state_path.with_name("pce-strategy.json"),
                clock=clock,
            ),
            **options,
        )

    def api_help(self):
        return {
            **super().api_help(),
            "provider": "BEA",
            "source_id": "BEA",
            "dataset": "NIPA",
            "table_name": "T20804",
            "frequency": "M",
            "series": PCE_PROFILE.series_metadata,
            "baskets": ["headline", "core", "all"],
            "adjustments": ["sa", "all"],
            "seasonal_adjustment": "SA",
            "year_parameter": "comma_separated_years_or_ALL; bounded_batches",
            "measures": [
                "index",
                "change_1m_ratio",
                "change_3m_ratio",
                "change_6m_ratio",
                "change_12m_ratio",
                "mom",
                "yoy",
            ],
            "ratio_semantics": (
                "index[t]/index[t-k]-1; same_cutoff; not_annualized; 0.0312=3.12%"
            ),
            "buffer_months": 12,
            "history_availability_semantics": (
                "mdf_post_commit_confirmation_available_at"
            ),
            "official_publication_time": "explicit_official_calendar_event_only",
            "last_revised_is_publication_time": False,
            "official_mom_table_is_index": False,
            "credential_binding": "macro/pce:BEA; no_provider_fallback",
        }

    def query(self, params):
        if (
            isinstance(params, dict)
            and str(params.get("adjustment", "sa")).lower() == "nsa"
        ):
            raise WebError("pce_seasonal_adjustment_unsupported")
        return super().query(params)
