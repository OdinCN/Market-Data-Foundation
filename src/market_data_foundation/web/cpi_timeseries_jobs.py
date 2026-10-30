"""Small durable single-host execution journal, separate from business storage."""

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from market_data_foundation.historical.bls_timeseries_profile import CPI_PROFILE
from market_data_foundation.historical.cpi_timeseries import month_number, month_range
from market_data_foundation.historical.cpi_timeseries_cli import validate_range


class WebError(ValueError):
    def __init__(self, classification, status=400):
        self.classification, self.status = classification, status
        super().__init__(classification)


def validate_command(command, max_months):
    if not isinstance(command, dict) or set(command) != {
        "request_id",
        "action",
        "start",
        "end",
    }:
        raise WebError("invalid_job_request")
    if any(type(v) is not str for v in command.values()):
        raise WebError("invalid_job_request")
    try:
        request_id = str(UUID(command["request_id"]))
    except ValueError:
        raise WebError("invalid_request_id") from None
    if request_id != command["request_id"]:
        raise WebError("canonical_request_id_required")
    if command["action"] not in {"init", "update", "initialize"}:
        raise WebError("invalid_job_action")
    validate_range(command["start"], command["end"])
    if max_months is not None and (
        month_number(command["end"]) - month_number(command["start"]) >= max_months
    ):
        raise WebError("collection_range_exceeds_host_limit")


def verify_readback(written, readback, command, *, profile=CPI_PROFILE):
    """Coverage includes explicit source nulls; an absent row is a different gap."""
    if profile.key in {"ppi", "pce"}:
        return verify_ppi_readback(written, readback, command, profile=profile)
    expected = {
        (s, m)
        for s in profile.series_ids
        for m in month_range(command["start"], command["end"])
    }

    def index(result):
        rows = result["points"] + result.get("source_unavailable", [])
        return {(p["series_id"], p["month"]): p for p in rows}

    actual, produced = index(readback), index(written)
    missing = sorted(expected - actual.keys())
    matched = all(
        key in actual
        and all(
            actual[key].get(field) == point.get(field)
            for field in ("index_value", "revision", "raw_sha256")
        )
        for key, point in produced.items()
    )
    local = all(p.get("origin") == "local" for p in actual.values())
    coverage = not missing and bool(readback.get("coverage_complete"))
    refresh = (
        command["action"] not in {"update", "initialize"}
        or written.get("refresh_coverage_complete") is True
    )
    verified = (
        written.get("persisted") is True
        and matched
        and local
        and coverage
        and readback.get("provider_requests") == 0
        and readback.get("provider_attempts") == 0
        and refresh
        and expected <= produced.keys()
    )
    unavailable = sorted(k for k, p in actual.items() if p["index_value"] is None)
    source_nulls = {
        (p["series_id"], p["month"]) for p in written.get("source_unavailable", [])
    }
    source_gaps = [
        p
        for p in written.get("source_missing", [])
        if (p["series_id"], p["month"]) not in source_nulls
    ]
    result = {
        key: written.get(key, 0)
        for key in (
            "provider_requests",
            "provider_attempts",
            "saved_points",
            "saved_revisions",
        )
    }
    result.update(
        coverage_complete=coverage and refresh,
        readback={
            "verified": verified,
            "vintage_mode": "available",
            "point_count": len(actual),
            "expected_points": len(expected),
            "local_coverage_complete": coverage,
            "refresh_coverage_complete": refresh,
            "source_missing": source_gaps,
            "source_unavailable_count": len(unavailable),
            "missing": [{"series_id": s, "month": m} for s, m in missing],
            "source_unavailable": [
                {"series_id": s, "month": m} for s, m in unavailable
            ],
        },
    )
    return verified, result


def verify_ppi_readback(written, readback, command, *, profile):
    def keys(rows):
        return {(p["series_id"], p["month"]) for p in rows}

    def index(result):
        return {
            (p["series_id"], p["month"]): p
            for p in result["points"] + result.get("source_unavailable", [])
        }

    expected = {
        (s, m)
        for s in profile.series_ids
        for m in month_range(command["start"], command["end"])
    }
    returned = keys(written.get("source_returned_series_months", []))
    actual, produced = index(readback), index(written)
    available = {
        k
        for k, p in actual.items()
        if p.get("origin") == "local"
        and p.get("availability") in {"available", "source_unavailable"}
        and p.get("available_at") is not None
    }
    source_gaps = keys(written.get("source_missing", [])) - returned
    missing = expected - available
    matched = all(
        key in available
        and all(
            actual[key].get(field) == point.get(field)
            for field in ("index_value", "revision", "raw_sha256")
        )
        for key, point in produced.items()
    )
    verified = (
        written.get("persisted") is True
        and bool(returned)
        and returned <= available
        and matched
        and missing <= source_gaps
        and readback.get("provider_requests") == 0
        and readback.get("provider_attempts") == 0
    )

    def rows(values):
        return [{"series_id": s, "month": m} for s, m in sorted(values)]

    partial = bool(source_gaps & expected)
    result = {
        key: written.get(key, 0)
        for key in (
            "provider_requests",
            "provider_attempts",
            "saved_points",
            "saved_revisions",
        )
    }
    nulls = {k for k, p in actual.items() if p["index_value"] is None}
    result.update(
        coverage_complete=not missing and not partial,
        source_missing=rows(source_gaps),
        readback={
            "verified": verified,
            "vintage_mode": "available",
            "point_count": len(actual),
            "expected_points": len(expected),
            "local_coverage_complete": not missing,
            "refresh_coverage_complete": not partial,
            "source_missing": rows(source_gaps),
            "missing": rows(missing),
            "source_unavailable": rows(nulls),
            "source_unavailable_count": len(nulls),
        },
    )
    if verified and partial:
        result["completion"] = "source_coverage_partial"
    return verified, result


class JobJournal:
    def __init__(self, directory, *, profile=CPI_PROFILE):
        self.profile = profile
        self.directory = Path(directory)
        if not self.directory.is_absolute() or ".." in self.directory.parts:
            raise ValueError("absolute_job_directory_required")

    @contextmanager
    def execution(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            self.directory / "execution.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WebError(self.profile.key + "_jobs_busy", 409) from None
            # With the exclusive lock acquired no earlier execution can still
            # be active. Persist its uncertainty before another job starts.
            for path in self.directory.glob("*.json"):
                try:
                    if str(UUID(path.stem)) != path.stem:
                        continue
                except ValueError:
                    continue
                record = self.read(path.stem)
                if record["state"] == "running":
                    self.save(
                        dict(
                            record,
                            state="unknown",
                            classification="execution_interrupted",
                        )
                    )
            yield
        finally:
            os.close(descriptor)

    def read(self, request_id):
        path = self.directory / (request_id + ".json")
        if not path.exists():
            return None
        try:
            result = json.loads(path.read_text())
            if result["request_id"] != request_id:
                raise ValueError
            if result.get("asset", "cpi") != self.profile.key:
                raise WebError(self.profile.key + "_job_journal_asset_mismatch", 503)
            return result
        except WebError:
            raise
        except Exception:
            raise WebError(self.profile.key + "_job_journal_unavailable", 503) from None

    def save(self, record):
        if "asset" in record and record["asset"] != self.profile.key:
            raise WebError(self.profile.key + "_job_journal_asset_mismatch", 503)
        record["asset"] = self.profile.key
        path = self.directory / (record["request_id"] + ".json")
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(record, stream, sort_keys=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def list(self):
        # GET never creates a directory, lock or state file.
        if not self.directory.exists():
            return []
        active = False
        try:
            descriptor = os.open(self.directory / "execution.lock", os.O_RDONLY)
        except FileNotFoundError:
            descriptor = None
        if descriptor is not None:
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    active = True
            finally:
                os.close(descriptor)
        records = []
        for path in self.directory.glob("*.json"):
            try:
                if str(UUID(path.stem)) != path.stem:
                    continue
            except ValueError:
                continue
            record = self.read(path.stem)
            if record["state"] == "running" and not active:
                record = dict(
                    record, state="unknown", classification="execution_interrupted"
                )
            records.append(record)
        return sorted(
            records, key=lambda r: (r["created_at"], r["job_id"]), reverse=True
        )
