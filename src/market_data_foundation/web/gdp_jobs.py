"""Durable GDP collection receipts; uncertain executions are never replayed silently."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

from market_data_foundation.historical.gdp_contract import SERIES_IDS
from market_data_foundation.historical.gdp_timeseries import quarters
from market_data_foundation.web.cpi_timeseries_jobs import JobJournal, WebError

GDP_PROFILE = SimpleNamespace(key="gdp", label="GDP 与增长", earliest_month=None)


def validate_command(command, now):
    if not isinstance(command, dict) or set(command) != {
        "request_id",
        "action",
        "start",
        "end",
    }:
        raise WebError("gdp_invalid_job_request")
    if any(type(v) is not str for v in command.values()):
        raise WebError("gdp_invalid_job_request")
    try:
        if str(UUID(command["request_id"])) != command["request_id"]:
            raise ValueError
    except ValueError:
        raise WebError("gdp_invalid_request_id") from None
    if command["action"] not in {"init", "update"}:
        raise WebError("gdp_invalid_job_action")
    quarters(command["start"], command["end"])
    current = f"{now.year:04d}-Q{(now.month - 1) // 3 + 1}"
    if command["end"] >= current:
        raise WebError("gdp_collection_requires_closed_quarter")


def verify_readback(written, readback):
    expected = {
        (s, q)
        for s in SERIES_IDS
        for q in quarters(written["start_quarter"], written["end_quarter"])
    }

    def indexed(payload):
        return {(p["series_id"], p["quarter"]): p for p in payload["points"]}

    actual, produced = indexed(readback), indexed(written)
    if set(actual) != expected or set(produced) != expected:
        return False
    for key, point in produced.items():
        saved = actual[key]
        if point.get("origin") != "local":
            if saved.get("origin") == "local":
                return False
            continue
        if saved.get("origin") != "local" or saved.get("available_at") is None:
            return False
        if any(
            saved.get(k) != point.get(k) for k in ("revision", "raw_sha256", "batch_id")
        ):
            return False
        a, b = saved["value"], point["value"]
        if (a is None) != (b is None) or (a is not None and Decimal(a) != Decimal(b)):
            return False
    return (
        readback.get("provider_attempts") == 0
        and readback.get("provider_requests") == 0
    )


class GdpJobRunner:
    def __init__(self, service, directory, *, clock=lambda: datetime.now(UTC)):
        self.service, self.clock = service, clock
        self.journal = JobJournal(directory, profile=GDP_PROFILE)

    def executions(self):
        return {"executions": [dict(j, id=j["job_id"]) for j in self.journal.list()]}

    def submit(self, command, *, origin="manual", stage=None, on_started=None):
        validate_command(command, self.clock())
        if origin not in {"manual", "schedule"}:
            raise WebError("gdp_invalid_job_origin")
        with self.journal.execution():
            existing = self.journal.read(command["request_id"])
            if existing:
                if any(existing.get(k) != v for k, v in command.items()):
                    raise WebError("request_id_conflict", 409)
                return existing
            at = self.clock().isoformat()
            job = dict(
                command,
                asset="gdp",
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
                if on_started is not None:
                    on_started(dict(job))
                method = (
                    self.service.init_range
                    if command["action"] == "init"
                    else self.service.update_range
                )
                written = method(command["start"], command["end"])
                readback = self.service.query_range(
                    command["start"],
                    command["end"],
                    allow_upstream=False,
                    as_of_basis="available",
                )
                verified = verify_readback(written, readback)
                if not verified:
                    raise WebError("gdp_job_readback_mismatch", 503)
                job.update(
                    state="complete",
                    result={
                        **{
                            k: written[k]
                            for k in (
                                "saved_points",
                                "saved_revisions",
                                "provider_requests",
                                "provider_attempts",
                                "coverage",
                                "source_missing",
                            )
                        },
                        "readback": {"verified": True, "as_of_basis": "available"},
                        "completion": "source_coverage_partial"
                        if written["source_missing"]
                        else "local_readback_verified",
                        "event_binding_status": "unconfirmed",
                    },
                )
            except Exception as error:
                code = getattr(error, "classification", "gdp_execution_unknown")
                # A storage/readback failure may follow a committed write.
                unknown = not (
                    getattr(error, "provider_requests", None) is not None
                    and any(
                        token in code
                        for token in (
                            "http_",
                            "request_",
                            "cooldown",
                            "quota",
                            "budget",
                            "source_",
                        )
                    )
                    and not any(
                        token in code
                        for token in ("storage", "readback", "availability", "raw_")
                    )
                )
                job.update(
                    state="unknown" if unknown else "failed",
                    classification=code,
                    result={
                        "provider_requests": getattr(error, "provider_requests", 0),
                        "provider_attempts": getattr(error, "provider_attempts", 0),
                        "retry_at": str(getattr(error, "retry_at", "") or "") or None,
                    },
                )
            job["finished_at"] = self.clock().isoformat()
            self.journal.save(job)
            return job
