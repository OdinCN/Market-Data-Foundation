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
                "series": (
                    "comma-separated allowlisted series; query default original five; "
                    "allowlist includes private/EPOP/eleven major sectors"
                ),
                "query_mode": (
                    "current/history; inferred history with as_of or available"
                ),
                "as_of": (
                    "optional aware cutoff; history defaults to commit availability"
                ),
                "as_of_basis/vintage_mode": (
                    "observed/available; must agree if both supplied; "
                    "current without cutoff uses observed; history defaults available; "
                    "observed is source response time, not official release"
                ),
                "local_only": (
                    "default false; true forbids source; history always local"
                ),
                "columns": (
                    "optional CSV native numeric fields; omitted preserves existing "
                    "fields"
                ),
                "measure": "index only; basket/adjustment are not employment filters",
            },
            "series": EMPLOYMENT_PROFILE.series_metadata,
            "collection_parameters": {
                "series": (
                    "comma-separated allowlisted subset; collection default all "
                    "profile series"
                ),
                "scope": (
                    "query/CLI/jobs subsets; automatic strategy always all profile "
                    "series"
                ),
            },
            "fields": {
                "value": (
                    "CES thousand jobs; CPS unemployment/participation/EPOP percent; "
                    "CES USD/hour or hours"
                ),
                "origin/availability": (
                    "local/upstream; available/unpersisted; source_unavailable, "
                    "withdrawn and unknown appear in missing-evidence arrays"
                ),
                "calculation_inputs": "same-cutoff comparison value and provenance",
                "raw_sha256/raw_locator": ("material digest/nullable retained locator"),
                "batch_id": (
                    "persisted numeric capture batch identity; "
                    "not an official release ID"
                ),
                "change_1m_ratio/change_12m_ratio": "AHE ratios; 0.01 means 1%",
                "net_change_1m_thousand_jobs": "CES month difference; thousand jobs",
                "change_1m_percentage_points": (
                    "CPS unemployment/participation/EPOP difference; percentage points"
                ),
                "change_1m_hours": "CES weekly paid hours month difference; hours",
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
                "&series=CES0000000001,LNS14000000,CES0500000003,LNS11300000,CES0500000002"
                "&query_mode=current&local_only=false&measure=index",
                self.prefix + "/query?start=2025-01&end=2026-09&series=LNS14000000"
                "&query_mode=history&local_only=true&as_of_basis=available"
                "&as_of=2026-10-31T00%3A00%3A00Z",
            ],
        }
