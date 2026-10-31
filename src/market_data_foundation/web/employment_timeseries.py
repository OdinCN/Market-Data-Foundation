"""Explicit continuous employment binding; retained T03 is not runtime evidence."""

from market_data_foundation.historical.bls_timeseries_profile import EMPLOYMENT_PROFILE
from market_data_foundation.web.cpi_timeseries_api import TimeSeriesHost


class EmploymentTimeSeriesHost(TimeSeriesHost):
    def __init__(self, **options):
        super().__init__(profile=EMPLOYMENT_PROFILE, **options)

    def asset_metadata(self):
        return {
            **super().asset_metadata(),
            "host_binding": "employment-continuous-dev-r1",
            "legacy_snapshot_is_runtime_evidence": False,
        }

    def api_help(self):
        return {
            **super().api_help(),
            "parameters": {
                "start/end": "required YYYY-MM, at most 2400 months",
                "series": "comma-separated allowlisted series; default all three",
                "query_mode": "current: local-first, unsaved; history: local only",
                "as_of": "aware cutoff; history uses local commit availability",
                "local_only": "true forbids source; history always local",
                "measure": "index only; basket/adjustment are not employment filters",
            },
            "series": EMPLOYMENT_PROFILE.series_metadata,
            "fields": {
                "value": "CES thousand jobs; CPS percent; CES USD/hour",
                "origin/availability": "local/upstream; persisted/temporary/gaps",
                "calculation_inputs": "same-cutoff comparison value and provenance",
                "raw_sha256/batch_id": "retained artifact and local batch",
                "change_1m_ratio/change_12m_ratio": "AHE ratios; 0.01 means 1%",
                "net_change_1m_thousand_jobs": "CES month difference; thousand jobs",
                "change_1m_percentage_points": "CPS difference; percentage points",
                "mom_pct/yoy_pct": "AHE same-cutoff endpoint derived percentages only",
                "provider_calculations": "official net_changes/pct_changes, separate",
                "footnotes": "official preliminary and empty objects retained",
                "revision": "local sequence; 1 is not official first release",
                "available_at": "local commit time; not official release",
                "publication_event": "official month event; not revision release time",
                "null": "missing source or comparison endpoint; never filled with zero",
            },
            "examples": [
                self.prefix + "/query?start=2025-01&end=2026-09"
                "&series=CES0000000001,LNS14000000,CES0500000003"
                "&query_mode=current&local_only=false&measure=index",
                self.prefix + "/query?start=2025-01&end=2026-09&series=LNS14000000"
                "&query_mode=history&local_only=true&as_of_basis=available"
                "&as_of=2026-10-31T00%3A00%3A00Z",
            ],
        }
