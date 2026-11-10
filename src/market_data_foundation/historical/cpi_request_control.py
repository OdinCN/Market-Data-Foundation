"""Secret-free, cross-process BLS admission and actual HTTP accounting.

Callers must explicitly share a state_path. Admission never waits: hold the
returned attempt across exactly one HTTP exchange and record its response before
leaving. The stable lock file must be retained alongside the atomic JSON state.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

WINDOW = timedelta(hours=24)
REQUEST_LIMIT = 400
INTERRUPTED_BACKOFF = timedelta(seconds=30)


class CpiRequestControlError(RuntimeError):
    """Stable admission facts, without underlying filesystem/provider text."""

    def __init__(
        self,
        classification: str,
        *,
        retry_at: datetime | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(classification)
        self.classification = classification
        self.retry_at = retry_at
        self.http_status = http_status


def retry_after_at(
    headers: Mapping[str, str], now: datetime, *, minimum_seconds: float = 300
) -> datetime:
    """Honor delta seconds and HTTP dates, with a conservative minimum."""
    result = now + timedelta(seconds=minimum_seconds)
    value = next(
        (value for key, value in headers.items() if key.lower() == "retry-after"),
        None,
    )
    if not isinstance(value, str):
        return result
    try:
        if value.strip().isdigit():
            candidate = now + timedelta(seconds=int(value.strip()))
        else:
            candidate = parsedate_to_datetime(value)
            if candidate.tzinfo is None or candidate.utcoffset() is None:
                return result
        return max(result, candidate.astimezone(UTC))
    except ValueError, TypeError, OverflowError:
        return result


def _instant(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError
    return result.astimezone(UTC)


def _state_unknown() -> CpiRequestControlError:
    return CpiRequestControlError("cpi_timeseries_request_state_unknown")


class _Attempt:
    def __init__(self, control: CpiRequestControl, state: dict) -> None:
        self.control = control
        self.state = state
        self.failed_write = False
        self.in_flight_unknown = False

    def record_response(
        self,
        status: int,
        headers: Mapping[str, str],
        *,
        provider_daily_limit: bool = False,
    ) -> datetime | None:
        now = self.control._now()
        if now < _instant(self.state["checked_at"]):
            raise _state_unknown()
        self.state["checked_at"] = now.isoformat()
        retry_at = None
        if status == 429:
            self.state["consecutive_429"] = min(2, self.state["consecutive_429"] + 1)
            retry_at = retry_after_at(
                headers,
                now,
                minimum_seconds=1800 if self.state["consecutive_429"] >= 2 else 300,
            )
            self.state["cooldown_until"] = retry_at.isoformat()
        else:
            self.state["consecutive_429"] = 0
        if provider_daily_limit:
            retry_at = now + WINDOW
            self.state["quota_until"] = retry_at.isoformat()
        self._save()
        return retry_at

    def _save(self) -> None:
        try:
            self.control._save(self.state)
        except CpiRequestControlError:
            self.failed_write = True
            raise


class CpiRequestControl:
    """400 actual requests per rolling 24h, single flight and shared cooldown."""

    def __init__(
        self,
        state_path: str | Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        min_interval_seconds: float = 1.0,
    ) -> None:
        if (
            isinstance(min_interval_seconds, bool)
            or not isinstance(min_interval_seconds, (int, float))
            or not math.isfinite(min_interval_seconds)
            or min_interval_seconds < 0
        ):
            raise ValueError("cpi_timeseries_request_interval_invalid")
        self.state_path = Path(state_path)
        self.lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        self._clock = clock
        self._interval = timedelta(seconds=min_interval_seconds)

    def _now(self) -> datetime:
        try:
            now = self._clock()
            if not isinstance(now, datetime) or now.utcoffset() is None:
                raise ValueError
            return now.astimezone(UTC)
        except Exception:
            raise _state_unknown() from None

    def _load(self, *, initializable: bool, now: datetime) -> dict:
        def pairs(items):
            if len(dict(items)) != len(items):
                raise ValueError
            return dict(items)

        try:
            try:
                # The file has at most 400 timestamps, never response material.
                with self.state_path.open("rb") as stream:
                    raw = stream.read(128 * 1024 + 1)
                if len(raw) > 128 * 1024:
                    raise ValueError
                state = json.loads(raw, object_pairs_hook=pairs)
            except FileNotFoundError:
                if not initializable:
                    raise ValueError
                return {
                    "version": 1,
                    "requests": [],
                    "checked_at": now.isoformat(),
                    "last_request_at": None,
                    "in_flight_at": None,
                    "cooldown_until": None,
                    "quota_until": None,
                    "consecutive_429": 0,
                    "recovery_until": None,
                }
            if type(state) is not dict or set(state) - {"recovery_until"} != {
                "version",
                "requests",
                "checked_at",
                "last_request_at",
                "in_flight_at",
                "cooldown_until",
                "quota_until",
                "consecutive_429",
                "checksum",
            }:
                raise ValueError
            payload = {key: value for key, value in state.items() if key != "checksum"}
            checksum = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if state["checksum"] != checksum:
                raise ValueError
            state.setdefault("recovery_until", None)
            if type(state["version"]) is not int or state["version"] != 1:
                raise ValueError
            if (
                type(state["requests"]) is not list
                or len(state["requests"]) > REQUEST_LIMIT
                or type(state["consecutive_429"]) is not int
                or not 0 <= state["consecutive_429"] <= REQUEST_LIMIT
            ):
                raise ValueError
            checked = _instant(state["checked_at"])
            requests = [_instant(value) for value in state["requests"]]
            if checked > now or requests != sorted(requests):
                raise ValueError
            if any(value > checked for value in requests):
                raise ValueError
            for name in (
                "last_request_at",
                "in_flight_at",
                "cooldown_until",
                "quota_until",
                "recovery_until",
            ):
                if state[name] is not None:
                    _instant(state[name])
            last = state["last_request_at"]
            if (requests and (last is None or _instant(last) != requests[-1])) or (
                last is not None and _instant(last) > checked
            ):
                raise ValueError
            if state["in_flight_at"] is not None and (
                last is None or _instant(state["in_flight_at"]) != _instant(last)
            ):
                raise ValueError
            return state
        except OSError, ValueError, TypeError, KeyError, RecursionError:
            raise _state_unknown() from None

    def _save(self, state: dict) -> None:
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=self.state_path.name + ".", dir=self.state_path.parent
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                payload = {
                    key: value for key, value in state.items() if key != "checksum"
                }
                payload["checksum"] = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            directory = os.open(self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError, ValueError, TypeError:
            raise _state_unknown() from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    @contextmanager
    def attempt(self) -> Iterator[_Attempt]:
        """Admit and durably count one exchange, or fail promptly with retry_at."""
        now = self._now()
        descriptor = None
        yielded = False
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(
                    self.lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
                )
            except FileExistsError:
                descriptor = os.open(self.lock_path, os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CpiRequestControlError(
                    "cpi_timeseries_request_in_flight",
                    retry_at=now + max(self._interval, timedelta(seconds=1)),
                ) from None
            # Another holder can finish after our initial clock sample and
            # immediately before flock succeeds. Validate against fresh time.
            now = self._now()
            marker = os.read(descriptor, 2)
            if marker not in {b"", b"1"}:
                raise _state_unknown()
            state = self._load(initializable=marker == b"", now=now)
            if state["in_flight_at"] is not None:
                # Owning the same flock proves the prior process is no longer
                # in flight. Retain its prebooked count and persist a fixed
                # recovery deadline; repeated restarts must not extend it.
                until = _instant(state["in_flight_at"]) + INTERRUPTED_BACKOFF
                if state["recovery_until"] is not None:
                    until = max(until, _instant(state["recovery_until"]))
                state["recovery_until"] = until.isoformat()
                state["in_flight_at"] = None
                state["checked_at"] = now.isoformat()
                self._save(state)
            for name, classification, status in (
                ("quota_until", "cpi_timeseries_provider_daily_limit", 200),
                ("cooldown_until", "cpi_timeseries_request_cooldown", 429),
                ("recovery_until", "cpi_timeseries_request_interrupted", None),
            ):
                if state[name] is not None and _instant(state[name]) > now:
                    raise CpiRequestControlError(
                        classification,
                        retry_at=_instant(state[name]),
                        http_status=status,
                    )
            state["requests"] = [
                value for value in state["requests"] if _instant(value) > now - WINDOW
            ]
            if len(state["requests"]) >= REQUEST_LIMIT:
                raise CpiRequestControlError(
                    "cpi_timeseries_request_budget_exhausted",
                    retry_at=_instant(state["requests"][0]) + WINDOW,
                )
            if state["last_request_at"] is not None:
                until = _instant(state["last_request_at"]) + self._interval
                if until > now:
                    raise CpiRequestControlError(
                        "cpi_timeseries_request_spacing", retry_at=until
                    )
            state["checked_at"] = state["last_request_at"] = now.isoformat()
            state["recovery_until"] = None
            state["in_flight_at"] = now.isoformat()
            state["requests"].append(now.isoformat())
            self._save(state)  # Complete before the caller may start actual HTTP.
            if not marker:
                os.write(descriptor, b"1")
                os.fsync(descriptor)
            attempt = _Attempt(self, state)
            try:
                yielded = True
                yield attempt
            except BaseException as error:
                # A supervised exchange that could not be reaped must not be
                # converted to a known finished request by context cleanup.
                attempt.in_flight_unknown = bool(
                    getattr(error, "request_may_be_in_flight", False)
                )
                raise
            finally:
                if not attempt.failed_write and not attempt.in_flight_unknown:
                    state["in_flight_at"] = None
                    attempt._save()
        except OSError:
            if yielded:
                raise
            raise _state_unknown() from None
        finally:
            if descriptor is not None:
                # Closing releases flock, including all exceptional paths.
                os.close(descriptor)
