"""Explicit DEV retail composition and durable manual execution; no scheduler."""

from contextlib import contextmanager
from datetime import UTC, datetime

from market_data_foundation.historical.census_retail_release_schedule import (
    CALENDAR_URL,
)
from market_data_foundation.historical.cpi_timeseries import month_at, month_number
from market_data_foundation.historical.retail_release_scheduler import (
    RETAIL_PROFILE,
    RetailAutoStrategy,
    RetailReleaseScheduler,
)
from market_data_foundation.historical.retail_timeseries import (
    RetailTimeSeriesService,
)
from market_data_foundation.historical.retail_timeseries_store import (
    PostgresRetailStore,
)
from market_data_foundation.web.cpi_timeseries_api import TimeSeriesHost
from market_data_foundation.web.cpi_timeseries_jobs import (
    JobJournal,
    WebError,
    validate_command,
)
from market_data_foundation.web.retail_timeseries import (
    RetailTimeSeriesHost,
    json_value,
)

RETAIL_SCHEMA = "mdf_retail_timeseries_dev_r1"


class RetailContinuousHost:
    prefix = RetailTimeSeriesHost.prefix
    profile = RETAIL_PROFILE
    source_id = "Census"
    official_origin = CALENDAR_URL
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
        write_service_factory=None,
        query_service_factory=None,
        query_allowed=False,
        allow_collect=False,
        allow_source=False,
        clock=lambda: datetime.now(UTC),
        schedule_path=None,
        calendar_fetcher=None,
        history_calendar_fetcher=None,
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
        self.journal = JobJournal(job_directory, profile=self.profile)
        self.scheduler = RetailReleaseScheduler(
            self,
            schedule_path or self.journal.directory.parent / "retail-schedule.json",
            clock=clock,
            calendar_fetcher=calendar_fetcher,
            history_calendar_fetcher=history_calendar_fetcher,
        )
        self.auto_strategy = RetailAutoStrategy(
            self.scheduler,
            self.scheduler.state_path.with_name("retail-strategy.json"),
            clock=clock,
        )

    def upstream_access(self, source):
        allowed = (
            self.query_allowed
            and self.allow_source
            and self.query_factory is not None
            and source["capability_enabled"]
            and source["credential_state"] == "configured"
        )
        return {
            "allowed": allowed,
            "reason": None if allowed else "retail_source_not_bound",
        }

    @contextmanager
    def service(self, *, write=False, upstream=False):
        factory = (
            self.write_factory
            if write
            else self.query_factory
            if upstream
            else self.read_factory
        )
        if factory is None:
            raise WebError("retail_source_not_bound", 403)
        service = (
            factory(credential_loader=self.effective_credential)
            if write or upstream
            else factory()
        )
        try:
            if not isinstance(service, RetailTimeSeriesService) or (
                isinstance(service.store, PostgresRetailStore)
                and service.store.schema != RETAIL_SCHEMA
            ):
                raise WebError("retail_service_binding_mismatch", 503)
            yield service
        finally:
            connection = getattr(service.store, "connection", None)
            if connection is not None:
                connection.close()

    def status(self):
        source = self.source()
        with self.service() as service:
            result = service.status()
            start, end = service.default_range()
        return {
            **result,
            **self.asset_metadata(),
            "source_allowed": self.upstream_access(source)["allowed"],
            "host_binding": "retail-continuous-dev-r1",
            "legacy_snapshot_is_runtime_evidence": False,
            "source_qualified": False,
            "latest_months": {
                s: result["latest_month"] for s in ("headline", "control")
            },
            "initialization": {
                "preset_id": "retail_previous_month_240",
                "months": 240,
                "start": start,
                "end": end,
                "available": True,
                "reason": None,
                "anchor_basis": "previous_utc_calendar_month_not_release_readiness",
            },
            "web_capabilities": self.capabilities(source),
            "source": source,
            "collection_scope": {"max_months": 2400, "series": "all_six_raw_inputs"},
            "periodic": self.strategy(),
        }

    def query(self, params):
        if not self.query_allowed:
            raise WebError("local_read_not_authorized", 403)
        # Historical and local-only reads never load a source credential.
        historical = (
            params.get("query_mode") == "history"
            or params.get("as_of")
            or params.get("as_of_basis") == "available"
            or params.get("vintage_mode") == "available"
        )
        upstream = (
            not historical
            and params.get("local_only") != "true"
            and self.upstream_access(self.source())["allowed"]
        )
        with self.service(upstream=upstream) as service:
            result = RetailTimeSeriesHost(
                service, query_allowed=True, allow_source=upstream
            ).query(params)
        return self.publication_metadata(result)

    def publication_metadata(self, result):
        return TimeSeriesHost.publication_metadata(self, result)

    def api_help(self):
        result = RetailTimeSeriesHost(object(), query_allowed=True).api_help()
        return {
            **result,
            "fields": {
                **result["fields"],
                "publication_events/points.publication_event": (
                    "retained official Census Data Month / annual "
                    "Period Covered calendar; "
                    "worker maintains current and previous release years, named years "
                    "can be imported; only evidenced months, not twenty-year coverage. "
                    "scheduled is a plan, never actual publication or revision "
                    "availability. Includes months with null levels. Missing month "
                    "has null event; TBA/unknown has unknown status and null time. "
                    "Calendar acquisition after as_of is withheld; no calendar "
                    "refresh or schedule/database writes on GET"
                ),
            },
            "boundary": (
                "DEV manual and official-calendar periodic collection; "
                "T05 is not continuous qualification"
            ),
            "collection": (
                "POST jobs: request_id/action/start/end; all six raw inputs. "
                "Same job_id=collection_id. Explicit POST jobs/resume "
                "retains range and id."
            ),
        }

    strategy = TimeSeriesHost.strategy

    def update_strategy(self, command):
        with self.scheduler._locked():
            return TimeSeriesHost.update_strategy(self, command)

    schedule = TimeSeriesHost.schedule

    def executions(self):
        return {
            "executions": [
                dict(j, id=j["job_id"], origin=j.get("origin", "manual"))
                for j in self.journal.list()
            ]
        }

    def submit(
        self, command, *, on_started=None, resume=False, origin="manual", stage=None
    ):
        validate_command(command, 2400)
        existing = self.journal.read(command["request_id"])
        if existing is not None:
            if any(existing.get(k) != v for k, v in command.items()):
                raise WebError("request_id_conflict", 409)
            existing = next(
                j
                for j in self.journal.list()
                if j["request_id"] == command["request_id"]
            )
            if not resume or existing["state"] in {"running", "complete"}:
                return existing
            retry = (existing.get("result") or {}).get("retry_at")
            if retry and datetime.fromisoformat(retry) > self.clock():
                raise WebError("retail_retry_not_due", 409)
        elif resume:
            raise WebError("retail_job_not_found", 404)
        permitted = self.capabilities(self.source())
        if not permitted["collect"]:
            raise WebError(permitted["reason"], 403)
        with self.journal.execution():
            current = self.journal.read(command["request_id"])
            if current and (not resume or current["state"] == "complete"):
                if any(current.get(k) != v for k, v in command.items()):
                    raise WebError("request_id_conflict", 409)
                return current
            now = self.clock().isoformat()
            job = dict(
                command,
                origin=existing.get("origin", origin) if existing else origin,
                stage=existing.get("stage", stage) if existing else stage,
                job_id=command["request_id"],
                collection_id=command["request_id"],
                state="running",
                created_at=existing["created_at"] if existing else now,
                started_at=now,
                finished_at=None,
                classification=None,
                result=None,
            )
            self.journal.save(job)
            if on_started:
                on_started(dict(job))
            try:
                with self.service(write=True) as service:
                    written = getattr(
                        service,
                        "update" if command["action"] == "update" else "initialize",
                    )(command["start"], command["end"], collection_id=job["job_id"])
                job["result"] = json_value(written)
                with self.service() as service:
                    readback = service.query_range(
                        month_at(month_number(command["start"]) - 12),
                        command["end"],
                        local_only=True,
                        as_of_basis="available",
                    )
                cells = [c for p in readback["points"] for c in p["source_cells"]]
                gaps = [
                    {
                        "series_id": c["category_code"] + ":" + c["data_type_code"],
                        "month": c["month"],
                        "reason": c["reason"],
                    }
                    for c in cells
                    if c["status"] in {"missing", "not_provided"}
                ]
                source_gaps = [
                    dict(
                        series_id=c["category_code"] + ":" + c["data_type_code"],
                        month=c["month"],
                        reason=c["reason"],
                    )
                    for c in cells
                    if c["status"] == "missing"
                ]
                unavailable = [
                    dict(
                        series_id=c["category_code"] + ":" + c["data_type_code"],
                        month=c["month"],
                    )
                    for c in cells
                    if c["value"] is None
                    and c["status"] not in {"missing", "not_provided"}
                ]
                expected = (
                    month_number(command["end"]) - month_number(command["start"]) + 1
                ) * 6
                local_missing = [c for c in cells if c["status"] == "not_provided"]
                confirmed = all(
                    (
                        version := point["input_versions"].get(
                            "/".join(
                                (
                                    cell["month"],
                                    cell["category_code"],
                                    cell["data_type_code"],
                                )
                            ),
                            {},
                        )
                    ).get("origin")
                    == "local"
                    and version.get("available_at") is not None
                    for point in readback["points"]
                    for cell in point["source_cells"]
                )
                verified = (
                    written["status"] == "COMPLETED"
                    and len(cells) == expected + 72
                    and not local_missing
                    and confirmed
                )
                result = dict(
                    written,
                    saved_points=written["saved_observations"],
                    coverage_complete=verified and not gaps,
                    completion="source_coverage_partial"
                    if gaps
                    else "local_read_verified"
                    if verified
                    else "collection_incomplete",
                    readback={
                        "verified": verified,
                        "point_count": sum(
                            command["start"] <= c["month"] <= command["end"]
                            for c in cells
                        ),
                        "buffer_point_count": sum(
                            c["month"] < command["start"] for c in cells
                        ),
                        "expected_points": expected,
                        "source_unavailable_count": len(unavailable),
                        "missing": gaps,
                        "source_missing": source_gaps,
                        "source_unavailable": unavailable,
                    },
                )
                job.update(
                    state="complete" if verified else "failed",
                    result=json_value(result),
                    classification=written["classification"]
                    or ("source_range_incomplete" if not verified else None),
                )
            except Exception as error:
                job.update(
                    state="unknown",
                    classification=getattr(
                        error, "classification", "retail_local_failure"
                    ),
                )
            job["finished_at"] = self.clock().isoformat()
            self.journal.save(job)
            return job
