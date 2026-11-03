"""JOLTS manual Host over shared permissions, journal, query and HTTP routes."""

from pathlib import Path
from zoneinfo import ZoneInfo

from market_data_foundation.historical.bls_timeseries_profile import JOLTS_PROFILE
from market_data_foundation.historical.cpi_timeseries import month_at, month_number
from market_data_foundation.historical.jolts_auto_strategy import JoltsAutoStrategy
from market_data_foundation.historical.jolts_contract import JOLTS_DEFINITION
from market_data_foundation.historical.jolts_release_schedule import (
    POLICY,
    JoltsReleaseScheduler,
)
from market_data_foundation.web.cpi_timeseries_api import TimeSeriesHost


class JoltsTimeSeriesHost(TimeSeriesHost):
    def __init__(
        self,
        *,
        job_directory,
        state_path,
        calendar_fetcher=None,
        reference_fetcher=None,
        fallback_fetcher=None,
        **options,
    ):
        paths = [Path(job_directory), Path(state_path)]
        if any(not p.is_absolute() or ".." in p.parts for p in paths):
            raise ValueError("jolts_independent_absolute_paths_required")
        a, b = (p.resolve() for p in paths)
        if a == b or a in b.parents or b in a.parents:
            raise ValueError("jolts_independent_paths_required")
        scheduler_options = {
            k: v
            for k, v in dict(
                calendar_fetcher=calendar_fetcher,
                reference_fetcher=reference_fetcher,
                fallback_fetcher=fallback_fetcher,
            ).items()
            if v is not None
        }
        if "clock" in options:
            scheduler_options["clock"] = options["clock"]
        scheduler = JoltsReleaseScheduler(paths[1], **scheduler_options)
        super().__init__(
            profile=JOLTS_PROFILE,
            scheduler=scheduler,
            job_directory=paths[0],
            **options,
        )
        self.auto_strategy = JoltsAutoStrategy(
            scheduler,
            scheduler.state_path.with_name("jolts-strategy.json"),
            clock=self.clock,
        )
        scheduler.submit = self.submit

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
            "operations": [
                "status",
                "query",
                "jobs",
                "executions",
                "api-help",
                "releases",
                "strategy",
                "schedule",
            ],
            "release_records": {
                "supported": True,
                "local_only": True,
                "material_scope": "national JOLTS archive; partial narrative claims",
            },
            "periodic_policy": POLICY,
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
                "publication_event": (
                    "official calendar plan; never binds current revision"
                ),
                "source_version/surprise": (
                    "null unless separately proven; no consensus means no surprise"
                ),
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
                "completion": (
                    "running/complete/failed/unknown; complete may have source gaps; "
                    "release scheduler requires this attempt's eight numeric targets"
                ),
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

    def publication_metadata(self, result, **options):
        return super().publication_metadata(result, **options)

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
                "available": True,
                "preset_id": "jolts_previous_calendar_month_240m",
                "reason": None,
                "anchor_basis": "request_calendar_month_no_release_claim",
            },
            "periodic_enabled": self.auto_strategy.get()["enabled"],
            "calendar_status": self.scheduler.status()["calendar_status"],
        }

    def executions(self):
        work = self.scheduler.status().get("work", {})
        automatic = {
            request_id: record
            for record in work.values()
            for request_id in record.get("request_ids", [record.get("request_id")])
            if request_id
        }
        return {
            "executions": [
                dict(
                    j,
                    id=j["job_id"],
                    origin="automatic" if j["job_id"] in automatic else "manual",
                    execution_kind=automatic.get(j["job_id"], {}).get("kind"),
                )
                for j in self.journal.list()
            ]
        }

    def strategy(self):
        return {**self.auto_strategy.get(), "policy": POLICY}

    def schedule(self):
        value = self.auto_strategy.get()
        return {
            **self.scheduler.status(),
            "worker": {**value["worker"], "enabled": value["enabled"]},
        }
