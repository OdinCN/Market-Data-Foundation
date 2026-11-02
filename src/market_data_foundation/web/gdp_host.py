"""GDP Web host: independent quarterly contracts over shared package permissions."""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from market_data_foundation.historical.gdp_contract import (
    METRICS,
    SERIES_IDS,
    shift_quarter,
    validate_series,
)
from market_data_foundation.historical.gdp_release_records import GdpReleaseRecords
from market_data_foundation.historical.gdp_schedule import (
    GdpAutoStrategy,
    GdpReleaseScheduler,
)
from market_data_foundation.historical.gdp_store import SCHEMA, PostgresGdpStore
from market_data_foundation.historical.gdp_timeseries import (
    GdpTimeSeriesService,
    _cutoff,
    quarters,
)
from market_data_foundation.web.cpi_timeseries_api import TimeSeriesHost
from market_data_foundation.web.cpi_timeseries_jobs import JobJournal, WebError
from market_data_foundation.web.gdp_jobs import (
    GDP_PROFILE,
    GdpJobRunner,
    validate_command,
)

PREFIX = "/api/r5/gdp/timeseries"


class GdpTimeSeriesHost:
    profile, source_id = GDP_PROFILE, "BEA"
    prefix = PREFIX
    source = TimeSeriesHost.source
    capabilities = TimeSeriesHost.capabilities
    effective_credential = TimeSeriesHost.effective_credential
    asset_metadata = TimeSeriesHost.asset_metadata

    def __init__(
        self,
        *,
        packages,
        read_service_factory,
        job_directory,
        schedule_path,
        write_service_factory=None,
        query_service_factory=None,
        query_allowed=False,
        allow_collect=False,
        allow_source=False,
        history_start=None,
        clock=lambda: datetime.now(UTC),
        calendar_fetcher=None,
    ):
        if any(
            type(v) is not bool for v in (query_allowed, allow_collect, allow_source)
        ):
            raise ValueError("explicit_timeseries_permissions_required")
        self.packages, self.read_factory = packages, read_service_factory
        self.write_factory, self.query_factory = (
            write_service_factory,
            query_service_factory,
        )
        self.query_allowed, self.allow_collect, self.allow_source = (
            query_allowed,
            allow_collect,
            allow_source,
        )
        self.clock = clock
        self.journal = JobJournal(job_directory, profile=GDP_PROFILE)
        self.scheduler = GdpReleaseScheduler(
            self,
            schedule_path,
            clock=clock,
            history_start=history_start,
            **(
                {"calendar_fetcher": calendar_fetcher}
                if calendar_fetcher is not None
                else {}
            ),
        )
        self.auto_strategy = GdpAutoStrategy(
            self.scheduler,
            Path(schedule_path).with_name("gdp-strategy.json"),
            clock=clock,
        )

    def upstream_access(self, source):
        result = TimeSeriesHost.upstream_access(self, source)
        if not self.allow_source:
            return {"allowed": False, "reason": "gdp_source_query_not_enabled"}
        return result

    @contextmanager
    def service(self, *, write=False, upstream=False, credential_loader=None):
        factory = (
            self.write_factory
            if write
            else self.query_factory
            if upstream
            else self.read_factory
        )
        if factory is None:
            raise WebError("gdp_service_not_bound", 503)
        service = (
            factory(credential_loader=credential_loader)
            if write or upstream
            else factory()
        )
        try:
            if not isinstance(service, GdpTimeSeriesService) or (
                isinstance(service.store, PostgresGdpStore)
                and service.store.schema != SCHEMA
            ):
                raise WebError("gdp_service_binding_mismatch", 503)
            yield service
        finally:
            service.close()

    def _read_allowed(self):
        if not self.query_allowed:
            raise WebError("local_read_not_authorized", 403)

    def status(self):
        self._read_allowed()
        with self.service() as service:
            result = service.status()
        now = self.clock()
        current = f"{now.year:04d}-Q{(now.month - 1) // 3 + 1}"
        end = shift_quarter(current, -1)
        source = self.source()
        return {
            **result,
            **self.asset_metadata(),
            "source": source,
            "web_capabilities": self.capabilities(source),
            "source_allowed": self.upstream_access(source)["allowed"],
            "initialization": {
                "preset_id": "gdp_previous_quarter_80",
                "quarters": 80,
                "start": shift_quarter(end, -79),
                "end": end,
                "editable": True,
                "available": True,
                "anchor_basis": "previous_utc_quarter_not_release_readiness",
            },
            "collection_scope": {
                "series": "all_eight_native_inputs",
                "max_quarters": 800,
            },
        }

    def query(self, params):
        self._read_allowed()
        if set(params) - {
            "start",
            "end",
            "series",
            "local_only",
            "query_mode",
            "as_of",
            "as_of_basis",
        }:
            raise WebError("unsupported_parameters")
        quarters(params.get("start"), params.get("end"))
        series = (
            validate_series(tuple(params["series"].split(",")))
            if "series" in params
            else SERIES_IDS
        )
        local = params.get("local_only", "true")
        if local not in {"true", "false"}:
            raise WebError("invalid_local_only")
        cutoff = _cutoff(params.get("as_of"))
        mode = params.get("query_mode", "history" if cutoff else "current")
        if mode not in {"history", "current"} or (mode == "history") != (
            cutoff is not None
        ):
            raise WebError("gdp_timeseries_history_cutoff_required")
        basis = params.get(
            "as_of_basis", "available" if mode == "history" else "observed"
        )
        if basis not in {"observed", "available"}:
            raise WebError("gdp_timeseries_cutoff_basis_invalid")
        upstream = local == "false" and mode == "current"
        if upstream:
            permission = self.upstream_access(self.source())
            if not permission["allowed"]:
                raise WebError(permission["reason"], 403)
        with self.service(
            upstream=upstream, credential_loader=self.effective_credential
        ) as service:
            result = service.query_range(
                params["start"],
                params["end"],
                series_ids=series,
                allow_upstream=upstream,
                as_of=cutoff,
                query_mode=mode,
                as_of_basis=basis,
            )
            result = GdpReleaseRecords(service.store).link_result(result)
        snapshot = self.scheduler.status()
        acquired = snapshot["calendar"].get("acquired_at")
        known = acquired and (
            cutoff is None or datetime.fromisoformat(acquired) <= cutoff
        )
        events = [
            e
            for e in snapshot["calendar_publications"]
            if known
            and e.get("reference_quarter")
            and params["start"] <= e["reference_quarter"] <= params["end"]
        ]
        result["points"] = [
            dict(
                p,
                publication_events=[
                    e for e in events if e["reference_quarter"] == p["quarter"]
                ],
            )
            for p in result["points"]
        ]
        return {
            **result,
            "publication_events": events,
            "metadata": {
                "calendar_binds_current_revision": False,
                "calendar_semantics": (
                    "planned_official_quarter_estimate_not_value_timestamp"
                ),
                "historical_calendar": (
                    "only_snapshot_acquired_by_cutoff; no reconstruction"
                ),
            },
        }

    def releases(self, params):
        self._read_allowed()
        if set(params) - {
            "event_id",
            "reference_quarter",
            "source_version",
            "read_mode",
            "as_of",
        }:
            raise WebError("unsupported_parameters")
        with self.service() as service:
            return GdpReleaseRecords(service.store).read(**params)

    def strategy(self):
        self._read_allowed()
        value, snapshot = self.auto_strategy.get(), self.scheduler.status()
        future = [
            e["release_at"]
            for e in snapshot["calendar_publications"]
            if e["status"] == "CONFIRMED"
            and not e.get("classification")
            and datetime.fromisoformat(e["release_at"]) > self.clock()
        ]
        return {
            **value,
            "calendar_status": snapshot["calendar_status"],
            "calendar": snapshot["calendar"],
            "next_release_at": min(future) if future else None,
            "runtime_state": value["worker"]["state"],
        }

    def update_strategy(self, command):
        self._read_allowed()
        if (
            not isinstance(command, dict)
            or set(command) not in ({"enabled"}, {"enabled", "history_start"})
            or type(command.get("enabled")) is not bool
        ):
            raise WebError("invalid_strategy_parameters")
        if command["enabled"]:
            access = self.capabilities(self.source())
            if not access["collect"]:
                raise WebError(access["reason"], 403)
        with self.scheduler._locked():
            self.auto_strategy.update(
                enabled=command["enabled"],
                history_start=command.get(
                    "history_start", self.auto_strategy.get()["history_start"]
                ),
            )
        return self.strategy()

    def schedule(self):
        self._read_allowed()
        strategy = self.auto_strategy.get()
        return {
            **self.scheduler.status(),
            "worker": {**strategy["worker"], "enabled": strategy["enabled"]},
        }

    def executions(self):
        self._read_allowed()
        return {"executions": [dict(j, id=j["job_id"]) for j in self.journal.list()]}

    def submit(self, command, *, on_started=None, origin="manual", stage=None):
        self._read_allowed()
        validate_command(command, self.clock())
        existing = next(
            (
                j
                for j in self.journal.list()
                if j["request_id"] == command["request_id"]
            ),
            None,
        )
        if existing:
            if any(existing.get(k) != v for k, v in command.items()):
                raise WebError("request_id_conflict", 409)
            return existing
        record = self.packages.store.read(self.packages.environment_id, "macro")
        access = self.capabilities(self.source(record))
        if not access["collect"]:
            raise WebError(access["reason"], 403)

        def credential_loader():
            current = self.packages.store.read(self.packages.environment_id, "macro")
            if current != record:
                raise WebError("package_configuration_changed", 409)
            return self.effective_credential()

        with self.service(write=True, credential_loader=credential_loader) as service:
            return GdpJobRunner(
                service, self.journal.directory, clock=self.clock
            ).submit(
                command,
                origin=origin,
                stage=stage,
                on_started=on_started,
            )

    def api_help(self):
        return {
            **self.asset_metadata(),
            "prefix": PREFIX,
            "provider": "BEA",
            "operations": [
                "status",
                "query",
                "api-help",
                "releases",
                "jobs",
                "executions",
                "strategy",
                "schedule",
            ],
            "frequency": "quarterly",
            "series_ids": list(SERIES_IDS),
            "series": [asdict(m) for m in METRICS],
            "parameters": {
                "start": "required YYYY-Qn, inclusive",
                "end": "required YYYY-Qn, inclusive",
                "series": "optional native SeriesCode CSV; default all eight",
                "local_only": (
                    "true by default; false permits current-query gap fetch "
                    "only, never persists"
                ),
                "query_mode": "current by default; history when as_of supplied",
                "as_of": "aware ISO8601 MDF cutoff required for history",
                "as_of_basis": (
                    "observed/available; history defaults available; no offi"
                    "cial vintage inference"
                ),
            },
            "response_fields": {
                "points": "quarter descending, then registered metric order",
                "value": "decimal string; unit and statistic given per point",
                "value_unit": "million_chained_2017_USD / percent / percentage_points",
                "change_1q_ratio": "nonannualized level change; 0.01 means 1%",
                "change_4q_ratio": (
                    "level change over four quarters; same cutoff endpoints"
                ),
                "estimate_kind": "unknown for continuous NIPA values",
                "publication_events": (
                    "all known estimates for quarter; planned time, not value proof"
                ),
                "related_releases": "scope relation only; continuous version unbound",
                "coverage": "expected / available / official_null / missing",
                "source_failure": "gap-fetch failure retains existing data and reasons",
            },
            "release_records": {
                "path": PREFIX + "/releases",
                "parameters": [
                    "reference_quarter",
                    "event_id",
                    "source_version",
                    "as_of",
                    "read_mode",
                ],
                "read_modes": ["event", "source_history"],
                "coverage": (
                    "official summary A191RL only; other seven not yet archi"
                    "ve-qualified"
                ),
                "precision": (
                    "source date remains date; late material never backdates"
                    " MDF availability"
                ),
                "surprise": (
                    "null without qualified event actual and named pre-relea"
                    "se consensus"
                ),
            },
            "collection": {
                "action": "init missing only / update refresh",
                "default_quarters": 80,
                "closed_quarters_only": True,
                "series": "all eight",
                "buffer_quarters": 4,
                "job_ack": (
                    "202 after durable running receipt; GET jobs/executions "
                    "for terminal state"
                ),
                "interrupted": "unknown; no silent replay",
            },
            "examples": {
                "current_local": PREFIX
                + "/query?start=2026-Q2&end=2026-Q3&local_only=true",
                "current_gap_fetch": PREFIX
                + "/query?start=2026-Q2&end=2026-Q3&local_only=false",
                "history": PREFIX
                + (
                    "/query?start=2026-Q2&end=2026-Q3&query_mode=history&loc"
                    "al_only=true&as_of_basis=available&as_of=2026-11-02T12%"
                    "3A00%3A00Z"
                ),
                "releases": PREFIX + "/releases?reference_quarter=2026-Q3",
            },
        }
