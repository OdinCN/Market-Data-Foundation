"""Opt-in M3 product host using the shared permission and durable job contracts."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from market_data_foundation.historical.cpi_timeseries import month_at, month_number
from market_data_foundation.historical.m3_release_acquisition import (
    capture_m3_material,
    numeric_ready,
    recoverable_material,
)
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
        joint_collections=True,
        calendar_fetch=None,
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
        self.joint_collections = joint_collections
        self.journal = JobJournal(Path(job_directory), profile=M3_PROFILE)
        end = month_at(self.clock().year * 12 + self.clock().month - 2)
        self.scheduler = M3Scheduler(
            Path(job_directory).parent / "m3-schedule.json",
            history_start=month_at(month_number(end) - 239),
            clock=self.clock,
            submit=self.submit,
            submit_report=self.submit_report,
            calendar_fetch=calendar_fetch,
            maintain_history=self.maintain_history,
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
            reports = {d: service.store.status(d) for d in ("m3", "advm3")}
            result = {
                **reports["m3"],
                "reports": reports,
                "total_observation_count": sum(
                    r["observation_count"] for r in reports.values()
                ),
            }
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

    def strategy(self, *, include_facts=True):
        from market_data_foundation.web.macro_runtime_facts import collection_facts

        if not self.allow_read:
            raise WebError("local_read_not_authorized", 403)
        value = self.auto_strategy.get()
        return {
            **value,
            **(collection_facts(self) if include_facts else {}),
            "calendar_status": self.scheduler.status()["calendar_status"],
            "next_release_at": self.scheduler.status()["next_due"],
            "execution_state": value["worker"]["state"],
        }

    def strategy_policy(self):
        return self.strategy(include_facts=False)

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
                dict(
                    j,
                    id=j["job_id"],
                    origin=j.get(
                        "origin",
                        "automatic"
                        if j["job_id"]
                        in {
                            v.get("request_id")
                            for v in self.scheduler._load()["work"].values()
                        }
                        else "manual",
                    ),
                )
                for j in self.jobs()["jobs"]
            ]
        }

    def maintain_history(self, event, identity, start):
        from market_data_foundation.historical.m3_capital_goods_store import SERIES_IDS
        from market_data_foundation.historical.m3_capital_goods_timeseries import (
            _months,
        )

        dataset, end = event["dataset"], event["reference_month"]
        old = self.journal.read(identity)
        if old and old["state"] == "complete":
            return old
        expected = {(s, m) for s in SERIES_IDS for m in _months(start, end)}
        release = datetime.fromisoformat(event["release_at"])
        with self.service() as service:
            rows = service.store.read_range(dataset, start, end)
        proof = {
            (r["series_id"], r["month"])
            for r in rows
            if r.get("available_at")
            and datetime.fromisoformat(r["observed_at"]) >= release
        }
        command = {
            "request_id": identity,
            "action": "update",
            "start": start,
            "end": end,
        }
        if proof != expected:
            return self.submit_report(
                dataset, command, resume=old is not None, work_stage="history"
            )
        with self.journal.execution():
            existing = self.journal.read(identity)
            if existing and existing["state"] == "complete":
                return existing
            now = self.clock().isoformat()
            available = sum(r["value"] is not None for r in rows)
            job = dict(
                command,
                job_id=identity,
                collection_id=identity,
                asset="m3",
                dataset=dataset,
                origin="automatic",
                stage="history",
                state="complete",
                created_at=now,
                started_at=now,
                finished_at=now,
                classification=None,
                result={
                    "operation": "local_post_release_history_verification",
                    "source_basis": "confirmed_source_snapshots_"
                    "observed_after_planned_release",
                    "release_at": event["release_at"],
                    "source_batches": sorted({r["batch_id"] for r in rows}),
                    "provider_requests": 0,
                    "provider_attempts": 0,
                    "saved_points": 0,
                    "saved_revisions": 0,
                    "persisted": False,
                    "coverage_complete": available == len(expected),
                    "coverage": {
                        "expected": len(expected),
                        "available": available,
                        "official_null": 0,
                        "missing": len(expected) - available,
                    },
                    "completion": "local_readback_verified",
                    "readback": {
                        "verified": True,
                        "point_count": len(rows),
                        "expected_points": len(expected),
                        "missing": [],
                        "source_missing": [],
                        "source_unavailable": [],
                        "source_unavailable_count": 0,
                    },
                },
            )
            self.journal.save(job)
            return job

    def submit_report(self, dataset, command, *, work_stage="target", **kwargs):
        from market_data_foundation.historical.m3_capital_goods import M3Request

        M3Request(dataset, "MDM", command["start"], command["start"])
        if kwargs.get("resume") and self.journal.read(command["request_id"]) is None:
            kwargs["resume"] = False
        return self.submit(
            command, report_stage=dataset, execution_stage=work_stage, **kwargs
        )

    def submit(
        self,
        command,
        *,
        on_started=None,
        resume=False,
        report_stage=None,
        execution_stage=None,
    ):
        if not self.allow_read:
            raise WebError("local_read_not_authorized", 403)
        validate_command(command, 2400, profile=M3_PROFILE)
        # The shared journal distinguishes a live execution lock from an
        # interrupted running record; explicit recovery must use that view.
        old = next(
            (
                j
                for j in self.journal.list()
                if j["request_id"] == command["request_id"]
            ),
            None,
        )
        if old:
            if report_stage is not None and old.get("dataset", "m3") != report_stage:
                raise WebError("request_id_conflict", 409)
            report_stage = (
                old.get("dataset")
                if old.get("dataset") in {"m3", "advm3"}
                else report_stage
            )
            if any(old.get(k) != v for k, v in command.items()):
                raise WebError("request_id_conflict", 409)
            if (
                not resume
                or old["state"] == "running"
                or (old["state"] == "complete" and not recoverable_material(old))
            ):
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
            if current and (
                not resume
                or (
                    current["state"] == "complete" and not recoverable_material(current)
                )
            ):
                return current
            now = self.clock().isoformat()
            job = dict(
                command,
                job_id=command["request_id"],
                collection_id=command["request_id"],
                asset="m3",
                origin="automatic" if report_stage else "manual",
                stage=execution_stage or ("target" if report_stage else "manual"),
                state="running",
                created_at=old["created_at"] if old else now,
                started_at=old["started_at"]
                if old and old["state"] == "complete"
                else now,
                finished_at=None,
                classification=None,
                result=None,
            )
            joint = (
                report_stage is None
                and self.joint_collections
                and (old is None or bool(old.get("report_collections")))
            )
            job["dataset"] = "both" if joint else (report_stage or "m3")
            if joint:
                from market_data_foundation.historical.m3_reports import child_ids

                job["report_collections"] = (
                    old["report_collections"]
                    if old
                    else child_ids(job["collection_id"])
                )
            self.journal.save(job)
            try:
                if on_started:
                    on_started(dict(job))
                service = self.write_factory(credential_loader=credential_loader)
                try:

                    def progress(value):
                        if "report_collections" in value:
                            job["report_collections"] = value["report_collections"]
                        job["result"] = {
                            **value,
                            "completion": "collection_in_progress",
                        }
                        self.journal.save(job)

                    if resume and old and numeric_ready(old.get("numeric_result")):
                        written = old["numeric_result"]
                        readback = old["numeric_readback"]
                    elif joint:
                        from market_data_foundation.historical.m3_reports import (
                            collect_reports,
                        )

                        written = collect_reports(
                            service,
                            command["start"],
                            command["end"],
                            collection_ids=job["report_collections"],
                            reuse_existing=old is None
                            and command["action"] in {"init", "initialize"},
                            on_progress=progress,
                        )
                        readback = {
                            "coverage": written["coverage"],
                            "provider_requests": 0,
                        }
                    else:
                        written = service.collect_range(
                            command["start"],
                            command["end"],
                            dataset=report_stage or "m3",
                            confirm_write=True,
                            resume_id=job["collection_id"],
                            on_progress=progress,
                        )
                        readback = service.query_range(
                            command["start"],
                            command["end"],
                            dataset=report_stage or "m3",
                        )
                    job["numeric_result"] = written
                    job["numeric_readback"] = readback
                    job["numeric_finished_at"] = (old or {}).get(
                        "numeric_finished_at"
                    ) or self.clock().isoformat()
                    self.journal.save(job)
                    material = capture_m3_material(
                        self,
                        service,
                        job["dataset"],
                        command["start"],
                        command["end"],
                        written,
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
                waiting = bool(
                    written["source_failure"]
                    and written["source_failure"].get("retry_at")
                )
                job.update(
                    state="complete"
                    if done and verified
                    else "waiting"
                    if waiting
                    else "failed",
                    classification=None
                    if done and verified
                    else "m3_storage_outcome_unknown"
                    if written["storage_failure"]
                    else "m3_source_wait"
                    if waiting
                    else "m3_source_failed"
                    if written["source_failure"]
                    else "m3_resume_required",
                    result={
                        **written,
                        "release_material": material,
                        "coverage_complete": not missing,
                        "coverage": {
                            "expected": readback["coverage"]["expected"],
                            "available": readback["coverage"]["available"],
                            "official_null": 0,
                            "missing": len(missing),
                        },
                        "completion": "collection_pending"
                        if written["remaining_requests"]
                        else "source_coverage_partial"
                        if missing
                        else "local_readback_verified",
                        "event_binding_status": "unconfirmed",
                        "readback": {
                            "verified": verified,
                            "point_count": written["readback_points"],
                            "expected_points": readback["coverage"]["expected"],
                            "missing": missing,
                            "source_missing": written["source_missing"],
                            "source_unavailable": [],
                            "source_unavailable_count": 0,
                        },
                    },
                )
            except Exception:
                job.update(state="unknown", classification="m3_execution_unknown")
            job["finished_at"] = (
                old["finished_at"]
                if old and old["state"] == "complete"
                else self.clock().isoformat()
            )
            if old and old["state"] == "complete":
                job["material_last_attempt_at"] = self.clock().isoformat()
                if (job.get("result") or {}).get("release_material", {}).get(
                    "state"
                ) == "retained":
                    job["material_recovered_at"] = self.clock().isoformat()
            self.journal.save(job)
            return job
