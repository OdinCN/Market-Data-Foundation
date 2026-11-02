"""Weekly collection using the shared durable JobJournal and execution lock."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from market_data_foundation.historical.calendar_resilience import failure
from market_data_foundation.historical.claims_collection import readback_digest
from market_data_foundation.historical.claims_contract import SERIES_IDS, day, weeks
from market_data_foundation.web.cpi_timeseries_jobs import JobJournal, WebError

CLAIMS_PROFILE = SimpleNamespace(
    key="claims", label="初请与续请失业金", earliest_month=None
)


def validate_command(command, now):
    if (
        not isinstance(command, dict)
        or set(command) != {"request_id", "action", "start", "end"}
        or any(type(v) is not str for v in command.values())
    ):
        raise WebError("claims_invalid_job_request")
    try:
        if str(UUID(command["request_id"])) != command["request_id"]:
            raise ValueError
    except ValueError:
        raise WebError("claims_invalid_request_id") from None
    if command["action"] not in {"init", "update"}:
        raise WebError("claims_invalid_job_action")
    if not weeks(command["start"], command["end"]):
        raise WebError("claims_empty_collection_range")
    if day(command["end"]) > now.date():
        raise WebError("claims_collection_future_range")


def verify_readback(written, readback):
    expected = {
        (s, w)
        for s in SERIES_IDS
        for w in weeks(written["start_date"], written["end_date"])
    }
    rows = readback["rows"]
    return (
        {(p["series_id"], p["week_ending"]) for p in rows} == expected
        and len(rows) == len(expected)
        and readback["coverage"] == written["coverage"]
        and readback_digest(readback) == written["readback_digest"]
        and all(
            p["provenance"] is None
            or (
                p["provenance"]["origin"] == "local" and p["provenance"]["available_at"]
            )
            for p in rows
        )
        and readback["provider_requests"] == readback["provider_attempts"] == 0
    )


class ClaimsJobRunner:
    def __init__(
        self,
        service,
        directory,
        *,
        clock=lambda: datetime.now(UTC),
        capture_material=None,
    ):
        self.service, self.clock, self.capture_material = (
            service,
            clock,
            capture_material,
        )
        self.journal = JobJournal(directory, profile=CLAIMS_PROFILE)

    def submit(self, command, *, origin="manual", stage=None, on_started=None):
        validate_command(command, self.clock())
        if origin not in {"manual", "schedule"}:
            raise WebError("claims_invalid_job_origin")
        with self.journal.execution():
            existing = self.journal.read(command["request_id"])
            if existing:
                if any(existing.get(k) != v for k, v in command.items()):
                    raise WebError("request_id_conflict", 409)
                return existing
            at = self.clock().isoformat()
            job = dict(
                command,
                asset="claims",
                job_id=command["request_id"],
                origin=origin,
                stage=stage,
                state="running",
                created_at=at,
                started_at=at,
                finished_at=None,
                classification=None,
                result=None,
            )
            self.journal.save(job)
            try:
                if on_started:
                    on_started(dict(job))
                method = (
                    self.service.init_range
                    if command["action"] == "init"
                    else self.service.update_range
                )
                written = method(command["start"], command["end"])
                readback = self.service.query_range(
                    command["start"], command["end"], allow_upstream=False
                )
                if not verify_readback(written, readback):
                    raise WebError("claims_job_readback_mismatch", 503)
                material = (
                    self.capture_material(self.service)
                    if self.capture_material
                    else dict(status="unbound")
                )
                job.update(
                    state="complete",
                    result={
                        **written,
                        "readback": dict(verified=True, as_of_basis="available"),
                        "material": material,
                        "event_binding_status": "scope_only",
                        "completion": "local_readback_verified",
                    },
                )
            except Exception as error:
                code = failure(error, "claims_execution_unknown")["classification"]
                failed = any(
                    t in code
                    for t in (
                        "http",
                        "timeout",
                        "connect",
                        "request_",
                        "cooldown",
                        "network",
                    )
                ) and not any(
                    t in code for t in ("storage", "readback", "raw_", "commit")
                )
                job.update(
                    state="failed" if failed else "unknown",
                    classification=code,
                    result=dict(
                        provider_requests=getattr(error, "request_count", 0),
                        provider_attempts=getattr(error, "attempts", 0),
                        retry_at=str(getattr(error, "retry_at", "") or "") or None,
                    ),
                )
            job["finished_at"] = self.clock().isoformat()
            self.journal.save(job)
            return job
