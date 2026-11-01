"""CPI Web adapter: read-only current pass-through and strictly local history."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    validate_profile,
)
from market_data_foundation.historical.calendar_publications import publication_events
from market_data_foundation.historical.cpi_auto_strategy import CpiAutoStrategy
from market_data_foundation.historical.cpi_timeseries_cli import (
    error_document,
    reject_json_constant,
    require_parameters,
    selected_series,
    unique_json_object,
    validate_query,
    validate_range,
)
from market_data_foundation.historical.cpi_timeseries_http import error_status
from market_data_foundation.historical.macro_release_records import ReleaseRecords
from market_data_foundation.web.cpi_timeseries_dispatch import dispatch_job
from market_data_foundation.web.cpi_timeseries_jobs import (
    JobJournal,
    WebError,
    validate_command,
    verify_readback,
)
from market_data_foundation.web.cpi_timeseries_policy import (
    PRESET_MONTHS,
    automatic_executions,
    initialization_preset,
    strategy_projection,
)
from market_data_foundation.web.package_catalog import PackageError
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN, admitted
from market_data_foundation.web.ppi_timeseries import execute_profile

PREFIX = "/api/r5/cpi/timeseries"
PREFIXES = (PREFIX, "/api/r5/ppi/timeseries", "/api/r5/employment/timeseries")


def admitted_job(request, *, prefix=None):
    prefixes = PREFIXES if prefix is None else (prefix,)
    return (
        request.method == "POST"
        and request.url.path in {value + "/jobs" for value in prefixes}
        and not request.url.query
        and request.headers.getlist("origin") == [CANDIDATE_ORIGIN]
        and request.headers.getlist("content-type") == ["application/json"]
        and "transfer-encoding" not in request.headers
        and admitted(
            Request(dict(request.scope, method="GET")), origin=CANDIDATE_ORIGIN
        )
    )


def admitted_strategy(request, *, prefix=None):
    if prefix is None:
        prefix = next(
            (value for value in PREFIXES if request.url.path == value + "/strategy"),
            PREFIX,
        )
    return (
        request.method == "PUT"
        and request.url.path == prefix + "/strategy"
        and admitted_job(
            Request(dict(request.scope, method="POST", path=prefix + "/jobs")),
            prefix=prefix,
        )
    )


def failure(error):
    if isinstance(error, WebError):
        return JSONResponse(
            {"status": "ERROR", "classification": error.classification}, error.status
        )
    if isinstance(error, PackageError):
        return JSONResponse(error.failure.model_dump(), error.http_status)
    return JSONResponse(error_document(error), error_status(error))


class TimeSeriesHost:
    def __init__(
        self,
        *,
        packages,
        read_service_factory,
        scheduler,
        job_directory,
        write_service_factory=None,
        allow_collect=False,
        query_allowed=True,
        max_collect_months=None,
        strategy=None,
        query_service_factory=None,
        clock=lambda: datetime.now(UTC),
        profile=CPI_PROFILE,
    ):
        if type(allow_collect) is not bool or type(query_allowed) is not bool:
            raise ValueError("explicit_timeseries_permissions_required")
        if max_collect_months is not None and (
            type(max_collect_months) is not int or not 1 <= max_collect_months <= 2400
        ):
            raise ValueError("invalid_collection_limit")
        self.profile = validate_profile(profile)
        self.source_id = "BEA" if profile.key == "pce" else "BLS"
        self.official_origin = (
            "https://www.bea.gov/" if profile.key == "pce" else "https://www.bls.gov/"
        )
        if getattr(scheduler, "profile", CPI_PROFILE) is not profile:
            raise ValueError("bls_timeseries_profile_mismatch")
        self.prefix = f"/api/r5/{profile.key}/timeseries"
        self.packages, self.read_factory = packages, read_service_factory
        self.write_factory, self.scheduler = write_service_factory, scheduler
        self.allow_collect, self.query_allowed = allow_collect, query_allowed
        self.max_collect_months, self.clock = max_collect_months, clock
        self.journal = JobJournal(job_directory, profile=profile)
        self.query_factory = query_service_factory
        self.auto_strategy = strategy or CpiAutoStrategy(
            scheduler,
            scheduler.state_path.with_name(
                "strategy.json"
                if profile is CPI_PROFILE
                else f"{profile.key}-strategy.json"
            ),
            clock=clock,
        )

    @contextmanager
    def service(self, *, write=False, credential_loader=None, upstream=False):
        service = (
            self.query_factory(credential_loader=credential_loader)
            if upstream and self.query_factory is not None
            else self.write_factory(credential_loader=credential_loader)
            if write
            else self.read_factory()
        )
        try:
            if getattr(service, "profile", CPI_PROFILE) is not self.profile:
                raise WebError("bls_timeseries_profile_mismatch", 503)
            yield service
        finally:
            service.close()

    def asset_metadata(self):
        capability = next(
            c
            for c in self.packages.catalog.package("macro").capabilities
            if c.capability_id == self.profile.key
        )
        return {
            "asset_id": capability.definition_ref.identity,
            "definition_ref": capability.definition_ref.model_dump(),
            "profile": self.profile.key,
            "label": self.profile.label,
        }

    def api_help(self):
        return {
            **self.asset_metadata(),
            "prefix": self.prefix,
            "operations": [
                "status",
                "query",
                "jobs",
                "strategy",
                "executions",
                "schedule",
                "api-help",
                "releases",
            ],
            "release_records": {
                "path": self.prefix + "/releases",
                "parameters": {
                    "event_id": "optional exact independent official event identity",
                    "as_of": "optional aware MDF availability cutoff; inclusive",
                    "read_mode": "event (default) / source_history",
                    "source_version": "optional exact YYYY-MM-DD archive version date",
                },
                "read_only": True,
                "provider_requests": 0,
                "schema_preparation": "explicit prepare-release-schema CLI only",
                "source_history": "named archive evidence; no latest fallback",
                "time_precision": "date stays date; unknown actual_at stays null",
                "roles": {
                    "actual": "official current statistic in this material",
                    "official_previous": (
                        "previously published value reported by this material"
                    ),
                    "prior_revised": "上期值（本次修订后）",
                },
                "continuous_version": (
                    "null until explicitly evidence-bound; "
                    "local revision=1 is not first release"
                ),
                "surprise": "null: no qualified stable pre-release consensus source",
            },
            "series_ids": list(self.profile.series_ids),
            "query_defaults": {
                "local_only": self.profile is CPI_PROFILE,
                "query_mode": "current",
                "as_of_basis": "observed",
                "history_as_of_basis": "available",
            },
            "history_local_only": True,
            "query_persists": False,
            "as_of_basis_semantics": {
                "observed": (
                    "source_response_observed_at_not_mdf_commit_or_historical_vintage"
                ),
                "available": "mdf_post_commit_confirmation_available_at",
            },
            "availability_is_query_parameter": False,
            "point_availability_values": [
                "available",
                "unpersisted",
                "source_unavailable",
                "withdrawn",
                "unknown",
            ],
        }

    def source(self, record=None):
        if record is None:
            record = self.packages.store.read(self.packages.environment_id, "macro")
        states = self.packages.credential_states(
            "macro", record, source_ids={self.source_id}
        )
        state = next(
            (s.credential_state for s in states if s.source_id == self.source_id),
            "unknown",
        )
        return {
            "source_id": self.source_id,
            "capability_enabled": bool(
                record and self.profile.key in record.enabled_capability_ids
            ),
            "credential_state": state,
        }

    def capabilities(self, source):
        reason = (
            "collection_not_enabled"
            if not self.allow_collect
            else self.profile.key + "_package_credential_binding_unavailable"
            if self.write_factory is None
            else self.profile.key + "_capability_disabled"
            if not source["capability_enabled"]
            else self.source_id.lower() + "_credential_" + source["credential_state"]
            if source["credential_state"] != "configured"
            else "local_read_not_authorized"
            if not self.query_allowed
            else None
        )
        return {
            "query": self.query_allowed,
            "collect": reason is None,
            "reason": reason,
        }

    def upstream_access(self, source):
        reason = (
            "local_read_not_authorized"
            if not self.query_allowed
            else self.profile.key + "_passthrough_binding_unavailable"
            if self.query_factory is None
            else self.profile.key + "_capability_disabled"
            if not source["capability_enabled"]
            else self.source_id.lower() + "_credential_" + source["credential_state"]
            if source["credential_state"] != "configured"
            else None
        )
        return {"allowed": reason is None, "reason": reason}

    def status(self):
        source = self.source()
        self.auto_strategy.get()  # Bind the persisted baseline in memory only.
        snapshot = self.scheduler.status()
        with self.service() as service:
            result = execute_profile(service, "status", profile=self.profile)
            preset = initialization_preset(
                service, result, snapshot, self.clock(), profile=self.profile
            )
        return {
            **result,
            **self.asset_metadata(),
            "initialization": preset,
            "web_capabilities": self.capabilities(source),
            "source": source,
            "collection_scope": {
                "max_months": self.max_collect_months,
                "series": "all",
                "initialization_preset_months": PRESET_MONTHS,
            },
        }

    def releases(self, params):
        require_parameters(params, {"event_id", "as_of", "read_mode", "source_version"})
        if not self.query_allowed:
            raise WebError("local_read_not_authorized", 403)
        with self.service() as service:
            return ReleaseRecords(service.store).read(**params)

    def query(self, params):
        require_parameters(
            params,
            {
                "start",
                "end",
                "basket",
                "adjustment",
                "measure",
                "vintage_mode",
                "as_of",
                "local_only",
                "query_mode",
                "as_of_basis",
            }
            | ({"series"} if self.profile.key == "employment" else set()),
        )
        if not self.query_allowed:
            raise WebError("local_read_not_authorized", 403)
        if self.profile.key == "employment" and (
            "basket" in params
            or "adjustment" in params
            or params.get("measure", "index") != "index"
        ):
            raise WebError("employment_series_filter_required")
        local_only = params.get(
            "local_only", "true" if self.profile is CPI_PROFILE else "false"
        )
        if local_only not in {"true", "false"}:
            raise WebError("invalid_local_only")
        params = dict(params, local_only=local_only == "true")
        params.setdefault(
            "query_mode",
            "history"
            if params.get("as_of") is not None
            or params.get("vintage_mode") == "available"
            or params.get("as_of_basis") == "available"
            else "current",
        )
        validate_range(params.get("start"), params.get("end"))
        validate_query(params)
        selected_series(params, query=True, profile=self.profile)
        history = (
            params.get("query_mode") == "history" or params.get("as_of") is not None
        )
        upstream = not params["local_only"] and not history
        if upstream and self.query_factory is None:
            raise WebError(self.profile.key + "_passthrough_binding_unavailable")
        with self.service(
            upstream=upstream, credential_loader=self.effective_credential
        ) as service:
            result = execute_profile(service, "query", params, profile=self.profile)
        return self.publication_metadata(result)

    def effective_credential(self):
        record = self.packages.store.read(self.packages.environment_id, "macro")
        source = self.source(record)
        if not source["capability_enabled"]:
            raise WebError(self.profile.key + "_capability_disabled", 403)
        value = self.packages.load_credential("macro", self.source_id, record=record)
        if value is None:
            raise WebError(self.source_id.lower() + "_credential_missing", 403)
        return value

    def publication_metadata(self, result, *, snapshot=None):
        events = publication_events(
            self.scheduler.status() if snapshot is None else snapshot,
            self.official_origin,
            as_of=result.get("as_of"),
        )
        events = {
            month: event
            for month, event in events.items()
            if result["start_month"] <= month <= result["end_month"]
        }
        result["points"] = [
            dict(point, publication_event=events.get(point["month"]))
            for point in result["points"]
        ]
        result["publication_events"] = list(events.values())
        result["metadata"] = {
            "publication_event": {
                "label": "官方日历时间",
                "description": (
                    "来自官方日历，不代表当前修订值当时已知；"
                    "历史默认按入库可用时间，显式 observed 按来源响应观察时间。"
                    "as_of 仅展示截止前取得的日历及月份证据；缺失时不推算。"
                ),
                "semantics": "official_month_event_not_current_value_revision_time",
                "binds_current_revision": False,
            },
            "as_of_default_basis": "available",
            "query_mode_default": "current",
        }
        return result

    def strategy(self):
        value = self.auto_strategy.get()
        return strategy_projection(
            value, self.scheduler.status(), self.clock(), profile=self.profile
        )

    def update_strategy(self, command):
        require_parameters(command, {"enabled", "history_start"})
        if (
            set(command) not in ({"enabled"}, {"enabled", "history_start"})
            or type(command["enabled"]) is not bool
            or (
                "history_start" in command
                and not isinstance(command["history_start"], str)
            )
        ):
            raise WebError("invalid_strategy_parameters")
        command = dict(command)
        command.setdefault("history_start", self.auto_strategy.get()["history_start"])
        validate_range(command["history_start"], command["history_start"])
        if command["enabled"]:
            capabilities = self.capabilities(self.source())
            if not capabilities["collect"]:
                raise WebError(capabilities["reason"], 403)
        value = self.auto_strategy.update(**command)
        return strategy_projection(
            value, self.scheduler.status(), self.clock(), profile=self.profile
        )

    def schedule(self):
        strategy = self.auto_strategy.get()
        return {
            **self.scheduler.status(),
            "worker": {**strategy["worker"], "enabled": strategy["enabled"]},
        }

    def executions(self):
        self.auto_strategy.get()
        manual = [
            {
                **job,
                "id": job["job_id"],
                "origin": "manual",
            }
            for job in self.journal.list()
        ]
        records = manual + automatic_executions(
            self.scheduler.status(), profile=self.profile
        )
        return {
            "executions": sorted(
                records,
                key=lambda r: (
                    r.get("started_at") or r.get("created_at") or "",
                    r["id"],
                ),
                reverse=True,
            ),
            "automatic_history_scope": "latest_attempt_per_stage",
        }

    def submit(self, command, *, on_started=None):
        validate_command(command, None, profile=self.profile)
        # Replay is read-only and remains available if source access is revoked.
        existing = self.journal.read(command["request_id"])
        if existing is not None:
            return self.replay(existing, command)
        # Initialization is a suggested range, not an execution constraint.
        validate_command(command, self.max_collect_months, profile=self.profile)
        record = self.packages.store.read(self.packages.environment_id, "macro")
        capabilities = self.capabilities(self.source(record))
        if not capabilities["collect"]:
            raise WebError(capabilities["reason"], 403)

        def credential_loader():
            current = self.packages.store.read(self.packages.environment_id, "macro")
            if current != record:
                raise WebError("package_configuration_changed", 409)
            value = self.packages.load_credential(
                "macro", self.source_id, record=record
            )
            if value is None:
                raise WebError(self.source_id.lower() + "_credential_missing", 403)
            return value

        with self.journal.execution():
            existing = self.journal.read(command["request_id"])
            if existing is not None:
                return self.replay(existing, command)
            now = self.clock().isoformat()
            job = dict(
                command,
                job_id=command["request_id"],
                state="running",
                created_at=now,
                started_at=now,
                finished_at=None,
                classification=None,
                result=None,
            )
            job["asset"] = self.profile.key
            self.journal.save(job)
            try:
                if on_started is not None:
                    on_started(dict(job))
                with self.service(
                    write=True, credential_loader=credential_loader
                ) as service:
                    written = execute_profile(
                        service,
                        "update"
                        if command["action"] == "initialize"
                        else command["action"],
                        {
                            "start": command["start"],
                            "end": command["end"],
                            **(
                                {"series": command["series"]}
                                if "series" in command
                                else {}
                            ),
                        },
                        profile=self.profile,
                    )
                read_start, read_end = command["start"], command["end"]
                if self.profile.key in {"ppi", "pce", "employment"}:
                    months = [
                        p["month"]
                        for p in written.get("source_returned_series_months", [])
                    ]
                    read_start = min([read_start, *months])
                    read_end = max([read_end, *months])
                with self.service() as service:
                    readback = execute_profile(
                        service,
                        "query",
                        {
                            "start": read_start,
                            "end": read_end,
                            "basket": "all",
                            "adjustment": "all",
                            "local_only": True,
                            "vintage_mode": "available",
                            **(
                                {
                                    "series": command["series"],
                                    "basket": None,
                                    "adjustment": None,
                                }
                                if "series" in command
                                else {}
                            ),
                        },
                        profile=self.profile,
                    )
                verified, result = verify_readback(
                    written, readback, command, profile=self.profile
                )
                job.update(
                    state="complete" if verified else "failed",
                    result=result,
                    classification=(
                        None
                        if verified
                        else "source_range_incomplete"
                        if not result["readback"]["refresh_coverage_complete"]
                        else "local_readback_incomplete"
                    ),
                )
            except Exception as error:
                document = error_document(error)
                classification = (
                    error.classification
                    if isinstance(error, WebError)
                    else document["classification"]
                )
                # A failure after a write began cannot prove rollback or no effect.
                job.update(
                    state="unknown",
                    classification=classification,
                    result={"provider_attempts": document["provider_attempts"]},
                )
            job["finished_at"] = self.clock().isoformat()
            self.journal.save(job)
            return job

    def replay(self, record, command):
        if record.get("series") != command.get("series") or any(
            record.get(key) != value for key, value in command.items()
        ):
            raise WebError("request_id_conflict", 409)
        if record["state"] == "running":
            return next(
                r
                for r in self.journal.list()
                if r["request_id"] == command["request_id"]
            )
        return record


def timeseries_router(host, *, profile=CPI_PROFILE):
    prefix = host.prefix if host is not None else f"/api/r5/{profile.key}/timeseries"
    router = APIRouter(prefix=prefix)

    def enabled_host():
        if host is None:
            raise WebError(profile.key + "_timeseries_not_enabled", 503)

    def read(request, operation):
        if not admitted(request, origin=CANDIDATE_ORIGIN):
            raise WebError("operator_not_authorized", 403)
        enabled_host()
        if (
            request.headers.get("content-length", "0") != "0"
            or "transfer-encoding" in request.headers
        ):
            raise WebError("get_body_not_supported")
        pairs = request.query_params.multi_items()
        if len(dict(pairs)) != len(pairs):
            raise WebError("duplicate_parameter")
        if operation not in {"query", "releases"} and pairs:
            raise WebError("unsupported_parameters")
        if operation == "jobs":
            return {"jobs": host.journal.list()}
        return (
            getattr(host, operation)(dict(pairs))
            if operation in {"query", "releases"}
            else getattr(host, operation.replace("-", "_"))()
        )

    def endpoint(operation):
        def get(request: Request):
            try:
                return read(request, operation)
            except Exception as error:
                return failure(error)

        return get

    for operation in (
        "status",
        "query",
        "schedule",
        "strategy",
        "jobs",
        "executions",
        "api-help",
        "releases",
    ):
        router.add_api_route(
            "/" + operation,
            endpoint(operation),
            methods=["GET"],
            name="timeseries_" + operation,
        )

    @router.put("/strategy")
    async def update_strategy(request: Request):
        try:
            if not admitted_strategy(request, prefix=prefix):
                raise WebError("operator_not_authorized", 403)
            enabled_host()
            body = bytearray()
            async for part in request.stream():
                body.extend(part)
                if len(body) > 4096:
                    raise WebError("request_body_too_large", 413)
            try:
                command = json.loads(
                    body,
                    object_pairs_hook=unique_json_object,
                    parse_constant=reject_json_constant,
                )
            except ValueError, UnicodeError, RecursionError:
                raise WebError("invalid_strategy_json") from None
            return await run_in_threadpool(host.update_strategy, command)
        except Exception as error:
            return failure(error)

    @router.post("/jobs")
    async def submit(request: Request):
        try:
            if not admitted_job(request, prefix=prefix):
                raise WebError("operator_not_authorized", 403)
            enabled_host()
            body = bytearray()
            async for part in request.stream():
                body.extend(part)
                if len(body) > 4096:
                    raise WebError("request_body_too_large", 413)
            try:
                command = json.loads(
                    body,
                    object_pairs_hook=unique_json_object,
                    parse_constant=reject_json_constant,
                )
            except ValueError, UnicodeError, RecursionError:
                raise WebError("invalid_job_json") from None
            job = await run_in_threadpool(dispatch_job, host.submit, command)
            return JSONResponse(
                job, status_code=202 if job["state"] == "running" else 200
            )
        except Exception as error:
            return failure(error)

    return router
