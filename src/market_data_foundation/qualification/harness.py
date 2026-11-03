"""Run one explicit capability binding and emit a normalized observation."""

import math
import platform
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from time import perf_counter
from typing import Any, Literal

from .contracts import Capability, Fixture, MacroCapability


@dataclass(frozen=True)
class Endpoint:
    host: str | None = None
    port: int | None = None
    server_identity: str | None = None
    unobservable_reason: str | None = "not reported"

    def __post_init__(self) -> None:
        if self.host and not re.fullmatch(r"[a-zA-Z0-9_.:-]+", self.host):
            raise ValueError("endpoint host must not contain URLs or authentication")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("invalid endpoint port")
        if not self.host and not self.server_identity and not self.unobservable_reason:
            raise ValueError("unobservable endpoint requires a reason")


@dataclass(frozen=True)
class Binding:
    provider: str
    implementation: str
    version: str
    capability: Capability | MacroCapability
    mode: Literal["offline", "live"]
    endpoint: Endpoint = field(default_factory=Endpoint)

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.provider, self.implementation, self.version)
        ):
            raise ValueError(
                "explicit provider, implementation and version are required"
            )
        if self.mode not in ("offline", "live"):
            raise ValueError("binding mode must be offline or live")
        if not isinstance(self.capability, (Capability, MacroCapability)):
            raise ValueError("binding requires a canonical capability")


@dataclass(frozen=True)
class Evidence:
    success: bool
    returned_symbols: tuple[str, ...] = ()
    row_count: int = 0
    complete: bool | None = None
    missing_fields: tuple[str, ...] = ()
    observed_start: date | None = None
    observed_end: date | None = None
    pages: int = 0
    exhausted: bool | None = None
    units: dict[str, str] = field(default_factory=dict)
    semantics: dict[str, str] = field(default_factory=dict)
    endpoint: Endpoint = field(default_factory=Endpoint)

    def __post_init__(self) -> None:
        if type(self.success) is not bool:
            raise TypeError("success must be a boolean")
        if type(self.row_count) is not int or type(self.pages) is not int:
            raise TypeError("observation counts must be integers")
        if self.row_count < 0 or self.pages < 0:
            raise ValueError("observation counts must not be negative")
        if any(
            value is not None and type(value) is not bool
            for value in (self.complete, self.exhausted)
        ):
            raise TypeError("coverage and pagination flags must be boolean or unknown")
        for values in (self.returned_symbols, self.missing_fields):
            if any(not isinstance(value, str) or not value for value in values) or len(
                set(values)
            ) != len(values):
                raise ValueError("coverage identifiers must be unique nonempty strings")
        for mapping in (self.units, self.semantics):
            if any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in mapping.items()
            ):
                raise TypeError("unit and semantic declarations must be strings")
        if not isinstance(self.endpoint, Endpoint):
            raise TypeError("evidence requires a normalized Endpoint")
        if any(
            type(value) is not date
            for value in (self.observed_start, self.observed_end)
            if value is not None
        ):
            raise ValueError("observed bounds must be dates")
        if self.observed_start and self.observed_end:
            if self.observed_start > self.observed_end:
                raise ValueError("observed date range is reversed")


def classify_exception(error: Exception) -> dict[str, str]:
    """Preserve the original exception type without retaining its message."""
    name = type(error).__name__.lower()
    if isinstance(error, TimeoutError) or "timeout" in name:
        category = "timeout"
    elif isinstance(error, PermissionError) or "auth" in name:
        category = "authentication"
    elif isinstance(error, ImportError):
        category = "dependency"
    elif isinstance(error, ConnectionError) or isinstance(error, OSError):
        category = "network"
    elif isinstance(error, (ValueError, TypeError)):
        category = "validation"
    else:
        category = "other"
    return {
        "category": category,
        "module": type(error).__module__,
        "type": type(error).__qualname__,
    }


def observe(
    binding: Binding,
    fixture: Fixture,
    execute: Callable[[Fixture], Evidence],
    *,
    allow_live: bool = False,
    timestamp: datetime | None = None,
    timer: Callable[[], float] = perf_counter,
) -> dict[str, Any]:
    if binding.capability != fixture.capability:
        raise ValueError("binding capability does not match fixture")
    if binding.mode == "live" and not allow_live:
        raise PermissionError("live provider execution requires explicit opt-in")
    observed_at = timestamp if timestamp is not None else datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observation timestamp must include a timezone")
    started = timer()
    exception = None
    try:
        evidence = execute(fixture)
        if not isinstance(evidence, Evidence):
            raise TypeError("executor must return normalized Evidence")
    except Exception as error:
        evidence = Evidence(
            success=False,
            endpoint=Endpoint(
                unobservable_reason="execution failed; connection not observed"
            ),
        )
        exception = classify_exception(error)
    latency = (timer() - started) * 1000
    if not math.isfinite(latency) or latency < 0:
        raise ValueError("latency timer must be finite and monotonic")

    def iso(value: date | None) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "schema_version": 3 if isinstance(fixture.capability, MacroCapability) else 2,
        **(
            {"capability_scope": "macro"}
            if isinstance(fixture.capability, MacroCapability)
            else {}
        ),
        "provider": binding.provider,
        "implementation": binding.implementation,
        "implementation_version": binding.version,
        "python": platform.python_version(),
        "mode": binding.mode,
        "capability": fixture.capability.value,
        "fixture_id": fixture.fixture_id,
        "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
        "success": evidence.success,
        "latency_ms": latency,
        "endpoint": asdict(evidence.endpoint),
        "attempted_endpoint": asdict(binding.endpoint),
        "coverage": {
            "requested_symbols": list(fixture.symbols),
            "returned_symbols": list(evidence.returned_symbols),
            "row_count": evidence.row_count,
            "complete": evidence.complete,
            "missing_fields": list(evidence.missing_fields),
        },
        "date_range": {
            "requested_start": iso(fixture.start),
            "requested_end": iso(fixture.end),
            "observed_start": iso(evidence.observed_start),
            "observed_end": iso(evidence.observed_end),
        },
        "pagination": {
            "pages": evidence.pages,
            "page_size": fixture.page_size,
            "exhausted": evidence.exhausted,
        },
        "units": dict(evidence.units),
        "semantics": dict(evidence.semantics),
        "exception": exception,
    }
