"""Opt-in M3 product host using the shared permission and durable job contracts."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from market_data_foundation.historical.cpi_timeseries import month_at, month_number
from market_data_foundation.historical.m3_schedule import M3AutoStrategy, M3Scheduler
from market_data_foundation.historical.macro_release_records import ReleaseRecords
from market_data_foundation.web.cpi_timeseries_api import TimeSeriesHost
from market_data_foundation.web.cpi_timeseries_jobs import (
    JobJournal,
    WebError,
    validate_command,
)
from market_data_foundation.web.m3_query_api import M3QueryHost

M3_PROFILE = SimpleNamespace(
    key="m3", label="美国耐用品与核心资本品", earliest_month=None
)


class M3ProductHost(M3QueryHost):
    profile = M3_PROFILE
    source_id = "Census"
    source = TimeSeriesHost.source
    capabilities = TimeSeriesHost.capabilities
    effective_credential = TimeSeriesHost.effective_credential
    asset_metadata = TimeSeriesHost.asset_metadata

    def __init__(
        self,
        *,
        packages,
        read_factory,
        query_factory=None,
        write_factory=None,
        job_directory,
        allow_read=False,
        allow_source=False,
        allow_collect=False,
        clock=lambda: datetime.now(UTC),
    ):
        super().__init__(
            read_factory=read_factory,
            query_factory=query_factory,
            allow_read=allow_read,
            allow_source=allow_source,
            upstream_access=lambda: self.upstream_access(self.source())["allowed"],
        )
        if type(allow_collect) is not bool:
            raise ValueError("explicit_timeseries_permissions_required")
        self.packages, self.query_allowed, self.write_factory = (
            packages,
            allow_read,
            write_factory,
        )
        self.allow_collect, self.clock = allow_collect, clock
        self.journal = JobJournal(Path(job_directory), profile=M3_PROFILE)
        end = month_at(self.clock().year * 12 + self.clock().month - 2)
        self.scheduler = M3Scheduler(
            Path(job_directory).parent / "m3-schedule.json",
            history_start=month_at(month_number(end) - 239),
            clock=self.clock,
            submit=self.submit,
        )
        self.auto_strategy = M3AutoStrategy(
            self.scheduler,
            Path(job_directory).parent / "m3-strategy.json",
            clock=self.clock,
        )

    def upstream_access(self, source):
        if not self.allow_source:
            return {"allowed": False, "reason": "m3_source_not_enabled"}
        return TimeSeriesHost.upstream_access(self, source)

    def status(self):
        if not self.allow_read:
            raise WebError("local_read_not_authorized", 403)
        with self.service() as service:
            result = service.store.status()
        source = self.source()
        end = month_at(self.clock().year * 12 + self.clock().month - 2)
        start = month_at(max(1901 * 12, month_number(end) - 239))
        return {
            **result,
            **self.asset_metadata(),
            "source": source,
            "source_allowed": self.upstream_access(source)["allowed"],
            "web_capabilities": self.capabilities(source),
            "initialization": {
                "preset_id": "m3_latest_20y_v1",
                "months": 240,
                "start": start,
                "end": end,
                "available": True,
                "editable": True,
                "reason": None,
                "anchor_basis": "previous_utc_calendar_month_not_release_readiness",
            },
            "collection_scope": {"max_months": 2400, "series": "three_m3_inputs"},
        }

    def releases(self, params):
        if not self.allow_read:
            raise WebError("local_read_not_authorized", 403)
        if set(params) - {
            "event_id",
            "as_of",
            "read_mode",
            "source_version",
            "reference_month",
        }:
            raise WebError("unsupported_parameters")
        with self.service() as service:
            return ReleaseRecords(service.store).read(**params)

    def strategy(self):
        if not self.allow_read:
            raise WebError("local_read_not_authorized", 403)
        value = self.auto_strategy.get()
        return {
            **value,
            "calendar_status": self.scheduler.status()["calendar_status"],
            "next_release_at": self.scheduler.status()["next_due"],
            "execution_state": value["worker"]["state"],
        }

    def update_strategy(self, command):
        if not self.allow_read:
            raise WebError("local_read_not_authorized", 403)
        if (
            type(command) is not dict
            or set(command) not in ({"enabled"}, {"enabled", "history_start"})
            or type(command.get("enabled")) is not bool
        ):
            raise WebError("invalid_strategy_parameters")
        if command["enabled"]:
            access = self.capabilities(self.source())
            if not access["collect"]:
                raise WebError(access["reason"], 403)
        self.auto_strategy.update(
            enabled=command["enabled"],
            history_start=command.get(
                "history_start", self.auto_strategy.get()["history_start"]
            ),
        )
        return self.strategy()

    def schedule(self):
        value = self.auto_strategy.get()
        return {
            **self.scheduler.status(),
            "worker": {**value["worker"], "enabled": value["enabled"]},
        }

    def jobs(self):
        if not self.allow_read:
            raise WebError("local_read_not_authorized", 403)
        return {"jobs": self.journal.list()}

    def executions(self):
        return {
            "executions": [
                dict(j, id=j["job_id"], origin="manual") for j in self.jobs()["jobs"]
            ]
        }

    def submit(self, command, *, on_started=None, resume=False):
        if not self.allow_read:
            raise WebError("local_read_not_authorized", 403)
        validate_command(command, 2400, profile=M3_PROFILE)
        old = self.journal.read(command["request_id"])
        if old:
            if any(old.get(k) != v for k, v in command.items()):
                raise WebError("request_id_conflict", 409)
            if not resume or old["state"] in {"running", "complete"}:
                return old
        elif resume:
            raise WebError("m3_job_not_found", 404)
        record = self.packages.store.read(self.packages.environment_id, "macro")
        access = self.capabilities(self.source(record))
        if not access["collect"]:
            raise WebError(access["reason"], 403)

        def credential_loader():
            current = self.packages.store.read(self.packages.environment_id, "macro")
            if current != record:
                raise WebError("package_configuration_changed", 409)
            value = self.packages.load_credential(
                "macro", self.source_id, record=record
            )
            if value is None:
                raise WebError("census_credential_missing", 403)
            return value

        with self.journal.execution():
            current = self.journal.read(command["request_id"])
            if current and (not resume or current["state"] == "complete"):
                return current
            now = self.clock().isoformat()
            job = dict(
                command,
                job_id=command["request_id"],
                collection_id=command["request_id"],
                asset="m3",
                state="running",
                created_at=old["created_at"] if old else now,
                started_at=now,
                finished_at=None,
                classification=None,
                result=None,
            )
            self.journal.save(job)
            try:
                if on_started:
                    on_started(dict(job))
                service = self.write_factory(credential_loader=credential_loader)
                try:
                    written = service.collect_range(
                        command["start"],
                        command["end"],
                        dataset="m3",
                        confirm_write=True,
                        resume_id=job["collection_id"],
                    )
                    readback = service.query_range(
                        command["start"], command["end"], dataset="m3"
                    )
                finally:
                    connection = getattr(service.store, "connection", None)
                    if connection is not None:
                        connection.close()
                done = (
                    written["remaining_requests"] == 0
                    and not written["storage_failure"]
                    and not written["source_failure"]
                )
                verified = (
                    written["persisted"] is True
                    and written["readback_status"] == "confirmed"
                    and readback["provider_requests"] == 0
                )
                missing = written["missing"]
                job.update(
                    state="complete" if done and verified else "failed",
                    classification=None
                    if done and verified
                    else "m3_storage_outcome_unknown"
                    if written["storage_failure"]
                    else "m3_resume_required",
                    result={
                        **written,
                        "coverage_complete": not missing,
                        "coverage": {
                            "expected": readback["coverage"]["expected"],
                            "available": readback["coverage"]["available"],
                            "official_null": 0,
                            "missing": len(missing),
                        },
                        "completion": "source_coverage_partial"
                        if missing
                        else "local_readback_verified",
                        "event_binding_status": "unconfirmed",
                        "readback": {
                            "verified": verified,
                            "point_count": written["readback_points"],
                            "expected_points": readback["coverage"]["expected"],
                            "missing": missing,
                            "source_missing": missing,
                            "source_unavailable": [],
                            "source_unavailable_count": 0,
                        },
                    },
                )
            except Exception:
                job.update(state="unknown", classification="m3_execution_unknown")
            job["finished_at"] = self.clock().isoformat()
            self.journal.save(job)
            return job
