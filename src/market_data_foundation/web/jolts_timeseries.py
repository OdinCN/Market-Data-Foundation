"""JOLTS manual Host over shared permissions, journal, query and HTTP routes."""

from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from market_data_foundation.historical.bls_timeseries_profile import JOLTS_PROFILE
from market_data_foundation.historical.cpi_timeseries import month_at, month_number
from market_data_foundation.historical.jolts_contract import JOLTS_DEFINITION
from market_data_foundation.web.cpi_timeseries_api import TimeSeriesHost
from market_data_foundation.web.cpi_timeseries_jobs import WebError


class JoltsTimeSeriesHost(TimeSeriesHost):
    def __init__(self, *, job_directory, state_path, **options):
        paths = [Path(job_directory), Path(state_path)]
        if any(not p.is_absolute() or ".." in p.parts for p in paths):
            raise ValueError("jolts_independent_absolute_paths_required")
        a, b = (p.resolve() for p in paths)
        if a == b or a in b.parents or b in a.parents:
            raise ValueError("jolts_independent_paths_required")
        # No calendar, policy or worker is constructed in the manual segment.
        super().__init__(
            profile=JOLTS_PROFILE,
            scheduler=SimpleNamespace(profile=JOLTS_PROFILE, state_path=paths[1]),
            strategy=object(),
            job_directory=paths[0],
            **options,
        )

    def asset_metadata(self):
        return {
            "asset_id": JOLTS_DEFINITION.identity,
            "definition_ref": {
                "identity": JOLTS_DEFINITION.identity,
                "revision": JOLTS_DEFINITION.revision,
            },
            "profile": "jolts",
            "label": JOLTS_PROFILE.label,
            "source_qualified": False,
        }

    def api_help(self):
        return {
            **super().api_help(),
            "operations": ["status", "query", "jobs", "executions", "api-help"],
            "release_records": {"supported": False},
            "baskets": [],
            "parameters": {
                "start/end": "required inclusive YYYY-MM; at most 2400 months",
                "series": "optional CSV of eight national SA BLS IDs; default all",
                "measure": "index only; value is native quantity/rate, not index",
                "local_only": "default true; false explicitly permits temporary fill",
                "query_mode": "current/history; history always local",
                "as_of": "aware inclusive cutoff shared by every comparison operand",
                "as_of_basis/vintage_mode": "observed/available; official unsupported",
                "columns": "optional CSV of the documented numeric fields",
            },
            "query_defaults": {
                "local_only": True,
                "query_mode": "current",
                "as_of_basis": "observed",
                "history_as_of_basis": "available",
            },
            "fields": {
                "value": "quantity: thousand jobs/person flows; official rate: percent",
                "change_1m_level": "quantity difference in the series native unit",
                "change_1m_ratio/change_12m_ratio": "quantity growth ratio; 0.01 = 1%",
                "change_1m_percentage_points/change_12m_percentage_points": (
                    "official rate series endpoint differences only"
                ),
                "provider_calculations": "BLS calculations, separate from rate values",
                "available_at": "post-commit MDF confirmation, never official release",
                "observed_at": "actual source response time; never backfilled",
                "revision": "changed-only local sequence, not official vintage",
                "publication_event/source_version/surprise": "null; evidence pending",
                "null": "missing endpoint or zero ratio denominator; never zero fill",
            },
            "provider_status": "P/R only from BLS footnotes; absence means unknown",
            "basket_selection": "unsupported; use native series IDs",
            "research_presentation": "independent quantity and official rate series",
            "collection_parameters": {
                "path": self.prefix + "/jobs",
                "request_id": "canonical UUID; exact replay never re-executes",
                "action": "init/initialize/update; explicit start/end required",
                "series": "optional native CSV subset; omitted collects all eight",
                "completion": "running/complete/failed/unknown; inspect readback",
            },
            "examples": [
                self.prefix + "/query?start=2026-01&end=2026-08",
                self.prefix + "/query?start=2026-01&end=2026-08"
                "&query_mode=history&as_of_basis=available"
                "&as_of=2026-11-03T00%3A00%3A00Z",
                self.prefix + "/query?start=2026-08&end=2026-08&local_only=false",
            ],
        }

    def query(self, params):
        return super().query(dict(params, local_only=params.get("local_only", "true")))

    def publication_metadata(self, result, **_options):
        # The numeric service already reports the independent evidence gaps.
        result["publication_events"] = []
        return result

    def capture_material(self, *_args):
        return {"state": "not_attempted", "reason": "jolts_release_material_pending"}

    def status(self):
        with self.service() as service:
            result = service.status()
        now = self.clock().astimezone(ZoneInfo("Asia/Shanghai"))
        end = month_at(month_number(now.strftime("%Y-%m")) - 1)
        return {
            **result,
            **self.asset_metadata(),
            "web_capabilities": self.capabilities(self.source()),
            "initialization": {
                "start": month_at(month_number(end) - 239),
                "end": end,
                "months": 240,
                "anchor_basis": "request_calendar_month_no_release_claim",
            },
            "periodic_enabled": False,
        }

    def executions(self):
        return {
            "executions": [
                dict(j, id=j["job_id"], origin="manual") for j in self.journal.list()
            ]
        }

    def strategy(self):
        return {"enabled": False, "reason": "jolts_periodic_not_implemented"}

    def schedule(self):
        return {"events": [], "worker": {"enabled": False, "state": "not_started"}}

    def update_strategy(self, _command):
        raise WebError("jolts_periodic_not_implemented", 409)

    def releases(self, _params):
        raise WebError("jolts_release_records_not_implemented", 409)
