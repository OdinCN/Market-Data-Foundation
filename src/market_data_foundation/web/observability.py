"""Bounded read-only projections. Only an explicitly admitted Host can supply facts.

No provider, process discovery, environment lookup, socket, log reader or writer.
The local sampler observes its Linux namespace, not an inferred physical host.
"""

import math
import os
import re
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from market_data_foundation.web.dto import DTO
from market_data_foundation.web.source_catalog import DirectoryQuery, directory

MAX_BYTES = 65536
MAX_AGE_SECONDS = 60
MAX_SAMPLE_SECONDS = 2
MAX_INTEGER = 2**63 - 1
PublicKey = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:@-]{0,255}$")]
Nonnegative = Annotated[int, Field(ge=0, le=MAX_INTEGER)]
Reason = Literal[
    "not_configured",
    "permission_denied",
    "source_unavailable",
    "invalid_sample",
    "limit_exceeded",
    "stale",
    "clock_invalid",
    "host_contract_invalid",
    "observed",
]
State = Literal["OK", "UNKNOWN"]


class ProjectionDTO(DTO):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class Observation(ProjectionDTO):
    status: State = "UNKNOWN"
    classification: Reason = "not_configured"
    sampled_at: str | None = None
    source: Literal["LINUX_NAMESPACE", "TRUSTED_HOST", "OFFLINE_FIXTURE"] | None = None
    clock: Literal["UTC_REALTIME"] = "UTC_REALTIME"

    @field_validator("sampled_at")
    @classmethod
    def instant(cls, value):
        if value is not None:
            parse_time(value)
        return value

    @model_validator(mode="after")
    def evidence_required(self):
        if self.status == "OK" and (
            self.sampled_at is None
            or self.source is None
            or self.classification != "observed"
        ):
            raise ValueError("evidence_required")
        return self


def parse_time(value: str) -> datetime:
    if len(value) > 40:
        raise ValueError("invalid_time")
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("invalid_time")
    return instant.astimezone(UTC)


class LoadDTO(ProjectionDTO):
    observation: Observation = Field(default_factory=Observation)
    unit: Literal["runnable_tasks_1m_5m_15m"] = "runnable_tasks_1m_5m_15m"
    values: (
        list[Annotated[float, Field(ge=0, le=1000000, allow_inf_nan=False)]] | None
    ) = Field(default=None, min_length=3, max_length=3)

    @model_validator(mode="after")
    def complete(self):
        if (self.observation.status == "OK") != (self.values is not None):
            raise ValueError("invalid_load")
        return self


class MemoryDTO(ProjectionDTO):
    observation: Observation = Field(default_factory=Observation)
    unit: Literal["bytes"] = "bytes"
    total: Nonnegative | None = None
    available: Nonnegative | None = None

    @model_validator(mode="after")
    def bounds(self):
        if (self.observation.status == "OK") != (
            self.total is not None and self.available is not None
        ) or (
            self.observation.status != "OK"
            and (self.total is not None or self.available is not None)
        ):
            raise ValueError("invalid_memory")
        if self.total is not None and (
            self.total == 0 or self.available is None or self.available > self.total
        ):
            raise ValueError("invalid_memory")
        return self


class DiskDTO(MemoryDTO):
    mount: Literal["root"] = "root"


class NetworkDTO(ProjectionDTO):
    interface: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,15}$")
    observation: Observation = Field(default_factory=Observation)
    unit: Literal["cumulative_bytes"] = "cumulative_bytes"
    received: Nonnegative | None = None
    transmitted: Nonnegative | None = None

    @model_validator(mode="after")
    def complete(self):
        if (self.observation.status == "OK") != (
            self.received is not None and self.transmitted is not None
        ) or (
            self.observation.status != "OK"
            and (self.received is not None or self.transmitted is not None)
        ):
            raise ValueError("invalid_network")
        return self


class ResourcesDTO(ProjectionDTO):
    host_identity: PublicKey | None = None
    cpu_load: LoadDTO = Field(default_factory=LoadDTO)
    memory: MemoryDTO = Field(default_factory=MemoryDTO)
    disks: list[DiskDTO] = Field(default_factory=lambda: [DiskDTO()], max_length=1)
    networks: list[NetworkDTO] = Field(default_factory=list, max_length=8)
    sample_duration_seconds: (
        Annotated[float, Field(ge=0, le=2, allow_inf_nan=False)] | None
    ) = None
    duration_clock: Literal["MONOTONIC"] = "MONOTONIC"

    @model_validator(mode="after")
    def named(self):
        observations = [
            self.cpu_load.observation,
            self.memory.observation,
            *[d.observation for d in self.disks],
            *[n.observation for n in self.networks],
        ]
        if any(o.status == "OK" for o in observations) and (
            self.host_identity is None or self.sample_duration_seconds is None
        ):
            raise ValueError("host_and_duration_required")
        names = [n.interface for n in self.networks]
        if len(names) != len(set(names)):
            raise ValueError("duplicate_interface")
        return self


class ObjectRef(ProjectionDTO):
    # Public, already-redacted identities only. Never URLs, DSNs, paths or messages.
    operation_identity: PublicKey | None = None
    asset_identity: PublicKey | None = None
    asset_revision: PublicKey | None = None
    version_identity: PublicKey | None = None
    publication_identity: PublicKey | None = None
    job_identity: PublicKey | None = None
    attempt_identity: PublicKey | None = None

    @model_validator(mode="after")
    def known_identity(self):
        entries = directory(DirectoryQuery()).entries
        if self.operation_identity is not None:
            entry = next(
                (e for e in entries if e.identity == self.operation_identity), None
            )
            if entry is None or (
                self.asset_identity is not None
                and (
                    entry.asset is None
                    or entry.asset.identity != self.asset_identity
                    or entry.asset.revision != self.asset_revision
                )
            ):
                raise ValueError("unknown_operation")
        if self.asset_identity is not None and not any(
            e.asset
            and e.asset.identity == self.asset_identity
            and e.asset.revision == self.asset_revision
            for e in entries
        ):
            raise ValueError("unknown_asset")
        if self.asset_revision is not None and self.asset_identity is None:
            raise ValueError("asset_required")
        if (
            self.version_identity or self.publication_identity
        ) and not self.asset_identity:
            raise ValueError("asset_required")
        if self.attempt_identity and not self.job_identity:
            raise ValueError("job_required")
        return self


class InstanceDTO(ProjectionDTO):
    component_identity: PublicKey
    instance_identity: PublicKey
    observation: Observation = Field(default_factory=Observation)
    state: Literal["RUNNING", "STOPPED", "DEGRADED", "UNKNOWN"] = "UNKNOWN"
    started_at: str | None = None
    heartbeat_at: str | None = None
    observe_only: Literal[True] = True
    related: ObjectRef = Field(default_factory=ObjectRef)

    @model_validator(mode="after")
    def admitted_component(self):
        components = {e.component_identity for e in directory(DirectoryQuery()).entries}
        if self.component_identity not in components | {"market_data_foundation.web"}:
            raise ValueError("unknown_component")
        for value in (self.started_at, self.heartbeat_at):
            if value is not None and (
                self.observation.sampled_at is None
                or parse_time(value) > parse_time(self.observation.sampled_at)
            ):
                raise ValueError("clock_invalid")
        return self


class StorageDTO(ProjectionDTO):
    identity: Literal["postgresql", "garage", "questdb"]
    instance_identity: PublicKey | None = None
    qualification_identity: PublicKey | None = None
    observation: Observation = Field(default_factory=Observation)
    state: Literal["AVAILABLE", "UNAVAILABLE", "UNKNOWN", "DEFER"] = "UNKNOWN"
    qualification: Literal["QUALIFIED_READ_ONLY", "UNKNOWN", "DEFER"] = "UNKNOWN"

    @model_validator(mode="after")
    def qualified_identity(self):
        if self.qualification == "QUALIFIED_READ_ONLY" and (
            self.instance_identity is None or self.qualification_identity is None
        ):
            raise ValueError("qualification_identity_required")
        if (self.state == "DEFER" or self.qualification == "DEFER") and (
            self.identity != "questdb"
        ):
            raise ValueError("invalid_storage_state")
        return self


class DiagnosticDTO(ProjectionDTO):
    identity: PublicKey
    observation: Observation
    severity: Literal["INFO", "WARNING", "ERROR"]
    module: Literal["runtime", "source", "asset", "service", "storage", "job"]
    classification: Literal[
        "runtime_unavailable",
        "read_failed",
        "permission_denied",
        "budget_exceeded",
        "material_missing",
        "contract_invalid",
        "job_failed",
        "recovered",
    ]
    related: ObjectRef = Field(default_factory=ObjectRef)


class TrustedEvidence(ProjectionDTO):
    """Host-admitted, redacted structured records, never raw logs or credentials."""

    observation: Observation
    instances: list[InstanceDTO] = Field(default_factory=list, max_length=32)
    storage: list[StorageDTO] = Field(default_factory=list, max_length=2)
    diagnostics: list[DiagnosticDTO] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique(self):
        observations = [
            self.observation,
            *[i.observation for i in self.instances],
            *[s.observation for s in self.storage],
            *[d.observation for d in self.diagnostics],
        ]
        if any(o.source == "LINUX_NAMESPACE" for o in observations):
            raise ValueError("trusted_structured_evidence_required")
        if self.observation.sampled_at is not None and any(
            o.sampled_at is not None
            and parse_time(o.sampled_at) > parse_time(self.observation.sampled_at)
            for o in observations[1:]
        ):
            raise ValueError("record_clock_after_envelope")
        for keys in (
            [i.instance_identity for i in self.instances],
            [s.identity for s in self.storage],
            [d.identity for d in self.diagnostics],
        ):
            if len(keys) != len(set(keys)):
                raise ValueError("duplicate_identity")
        if any(s.identity == "questdb" for s in self.storage):
            raise ValueError("questdb_deferred")
        return self


class ObservabilityQuery(DTO):
    # Reject all client-supplied paths, hosts, providers, log requests or clocks.
    pass


class ObservabilityDTO(ProjectionDTO):
    status: State
    generated_at: str
    max_age_seconds: Literal[60] = MAX_AGE_SECONDS
    evidence_mode: Literal["UNCONFIGURED", "AUTHORIZED_HOST", "OFFLINE_FIXTURE"]
    resources: ResourcesDTO
    evidence_observation: Observation
    instances: list[InstanceDTO] = Field(max_length=32)
    storage: list[StorageDTO] = Field(min_length=3, max_length=3)
    diagnostics: list[DiagnosticDTO] = Field(max_length=100)


def _failure(error: Exception) -> Reason:
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, OSError):
        return "source_unavailable"
    if isinstance(error, OverflowError):
        return "limit_exceeded"
    return "invalid_sample"


def _read_fixed(path: str) -> str:
    if path not in {"/proc/loadavg", "/proc/meminfo", "/proc/net/dev"}:
        raise ValueError("source_not_allowed")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("source_not_regular")
        raw = b""
        while len(raw) <= MAX_BYTES:
            chunk = os.read(descriptor, MAX_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
        if len(raw) > MAX_BYTES:
            raise OverflowError("source_too_large")
        return raw.decode("ascii", errors="strict")
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class LinuxSampler:
    """Explicit opt-in only. Fixed proc sources/root mount; no path parameters.

    Host must admit the namespace and local root filesystem. Interfaces are names
    selected from the fixed /proc/net/dev snapshot, never interpolated into paths.
    """

    host_identity: str
    interfaces: tuple[str, ...] = ()

    def __post_init__(self):
        ResourcesDTO(host_identity=self.host_identity)
        if len(self.interfaces) > 8 or len(set(self.interfaces)) != len(
            self.interfaces
        ):
            raise ValueError("invalid_interfaces")
        for name in self.interfaces:
            NetworkDTO(interface=name)

    def __call__(self) -> ResourcesDTO:
        if sys.platform != "linux":
            return unknown_resources(
                self.host_identity, self.interfaces, "source_unavailable"
            )
        wall_start, mono_start = datetime.now(UTC), time.monotonic()
        stamp = Observation(
            status="OK",
            classification="observed",
            sampled_at=wall_start.isoformat(),
            source="LINUX_NAMESPACE",
        )

        def capture(builder, **identity):
            try:
                return builder(stamp, **identity)
            except Exception as error:
                return Observation(
                    classification=_failure(error),
                    sampled_at=stamp.sampled_at,
                    source=stamp.source,
                )

        def load(observation):
            tokens = _read_fixed("/proc/loadavg").split()
            if len(tokens) != 5:
                raise ValueError("invalid_load")
            return LoadDTO(
                observation=observation, values=[float(v) for v in tokens[:3]]
            )

        def memory(observation):
            fields = {}
            for line in _read_fixed("/proc/meminfo").splitlines():
                key, _, value = line.partition(":")
                if key in {"MemTotal", "MemAvailable"}:
                    if key in fields or not re.fullmatch(r"\s*\d+ kB\s*", value):
                        raise ValueError("invalid_memory")
                    fields[key] = int(value.split()[0]) * 1024
            return MemoryDTO(
                observation=observation,
                total=fields["MemTotal"],
                available=fields["MemAvailable"],
            )

        def disk(observation):
            value = os.statvfs("/")
            if value.f_frsize <= 0 or not 0 <= value.f_bavail <= value.f_blocks:
                raise ValueError("invalid_disk")
            return DiskDTO(
                observation=observation,
                total=value.f_blocks * value.f_frsize,
                available=value.f_bavail * value.f_frsize,
            )

        def networks(observation):
            rows = {}
            for line in _read_fixed("/proc/net/dev").splitlines()[2:]:
                name, sep, values = line.partition(":")
                if not sep:
                    raise ValueError("invalid_network")
                name = name.strip()
                if name in rows:
                    raise ValueError("duplicate_interface")
                rows[name] = values.split()
            result = []
            for name in self.interfaces:
                values = rows[name]
                if len(values) != 16 or not all(v.isdecimal() for v in values):
                    raise ValueError("invalid_network")
                result.append(
                    NetworkDTO(
                        interface=name,
                        observation=observation,
                        received=int(values[0]),
                        transmitted=int(values[8]),
                    )
                )
            return result

        cpu, mem, root, net = (
            capture(load),
            capture(memory),
            capture(disk),
            capture(networks) if self.interfaces else [],
        )
        elapsed = time.monotonic() - mono_start
        wall_elapsed = (datetime.now(UTC) - wall_start).total_seconds()
        if (
            not math.isfinite(elapsed)
            or not 0 <= elapsed <= MAX_SAMPLE_SECONDS
            or abs(wall_elapsed - elapsed) > 5
        ):
            reason: Reason = (
                "clock_invalid"
                if elapsed < 0 or abs(wall_elapsed - elapsed) > 5
                else "stale"
            )
            return unknown_resources(self.host_identity, self.interfaces, reason)
        return ResourcesDTO(
            host_identity=self.host_identity,
            cpu_load=LoadDTO(observation=cpu) if isinstance(cpu, Observation) else cpu,
            memory=MemoryDTO(observation=mem) if isinstance(mem, Observation) else mem,
            disks=[
                DiskDTO(observation=root) if isinstance(root, Observation) else root
            ],
            networks=[NetworkDTO(interface=n, observation=net) for n in self.interfaces]
            if isinstance(net, Observation)
            else net,
            sample_duration_seconds=elapsed,
        )


def unknown_resources(host=None, interfaces=(), reason: Reason = "not_configured"):
    stamp = Observation(classification=reason)
    return ResourcesDTO(
        host_identity=host,
        cpu_load=LoadDTO(observation=stamp),
        memory=MemoryDTO(observation=stamp),
        disks=[DiskDTO(observation=stamp)],
        networks=[NetworkDTO(interface=n, observation=stamp) for n in interfaces],
    )


@dataclass(frozen=True)
class ObservabilityHost:
    """Only trusted application composition may inject these finite local readers.

    No production binding is installed by create_app. Fixture mode is explicit.
    Callbacks must be bounded, read-only and never probe a remote service here.
    """

    sampler: Callable[[], ResourcesDTO] | None = None
    evidence: Callable[[], TrustedEvidence] | None = None
    evidence_mode: Literal["AUTHORIZED_HOST", "OFFLINE_FIXTURE"] = "AUTHORIZED_HOST"

    def __post_init__(self):
        if self.evidence_mode not in {"AUTHORIZED_HOST", "OFFLINE_FIXTURE"}:
            raise ValueError("invalid_evidence_mode")


def fresh(stamp: Observation, now: datetime) -> Observation:
    if stamp.status != "OK":
        return stamp
    age = (now - parse_time(stamp.sampled_at)).total_seconds()
    if age < 0 or age > MAX_AGE_SECONDS:
        return Observation(
            classification="clock_invalid" if age < 0 else "stale",
            sampled_at=stamp.sampled_at,
            source=stamp.source,
        )
    return stamp


def _check_mode(host: ObservabilityHost, observations: list[Observation]) -> None:
    for stamp in observations:
        if stamp.status == "OK" and (
            (stamp.source == "OFFLINE_FIXTURE")
            != (host.evidence_mode == "OFFLINE_FIXTURE")
        ):
            raise ValueError("evidence_mode_mismatch")


def project(
    host: ObservabilityHost | None, *, now: datetime | None = None
) -> ObservabilityDTO:
    resources = ResourcesDTO()
    evidence = None
    stamp = Observation()
    if host is not None and host.sampler is not None:
        try:
            # Revalidate even preconstructed/model_copy DTOs at the trust boundary.
            resources = ResourcesDTO.model_validate(host.sampler().model_dump())
            _check_mode(
                host,
                [
                    resources.cpu_load.observation,
                    resources.memory.observation,
                    *[d.observation for d in resources.disks],
                    *[n.observation for n in resources.networks],
                ],
            )
        except Exception as error:
            resources = unknown_resources(reason=_failure(error))
    if host is not None and host.evidence is not None:
        try:
            evidence = TrustedEvidence.model_validate(host.evidence().model_dump())
            _check_mode(
                host,
                [
                    evidence.observation,
                    *[i.observation for i in evidence.instances],
                    *[s.observation for s in evidence.storage],
                    *[d.observation for d in evidence.diagnostics],
                ],
            )
            stamp = evidence.observation
        except Exception:
            stamp = Observation(classification="host_contract_invalid")
    # Completion clock, never the CPI as-of or a browser-supplied timestamp.
    instant = datetime.now(UTC) if now is None else now
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("aware_clock_required")
    instant = instant.astimezone(UTC)
    for item in [
        resources.cpu_load,
        resources.memory,
        *resources.disks,
        *resources.networks,
    ]:
        observation = fresh(item.observation, instant)
        if observation.status != "OK":
            for name in ("values", "total", "available", "received", "transmitted"):
                if name in type(item).model_fields:
                    item = item.model_copy(update={name: None})
        item_data = item.model_dump()
        item_data["observation"] = observation.model_dump()
        if isinstance(item, LoadDTO):
            resources = resources.model_copy(update={"cpu_load": LoadDTO(**item_data)})
        elif isinstance(item, DiskDTO):
            resources = resources.model_copy(update={"disks": [DiskDTO(**item_data)]})
        elif isinstance(item, NetworkDTO):
            resources = resources.model_copy(
                update={
                    "networks": [
                        NetworkDTO(**item_data) if n.interface == item.interface else n
                        for n in resources.networks
                    ]
                }
            )
        else:
            resources = resources.model_copy(update={"memory": MemoryDTO(**item_data)})
    stamp = fresh(stamp, instant)
    instances, diagnostics = [], []
    storage = {name: StorageDTO(identity=name) for name in ("postgresql", "garage")}
    if evidence is not None and stamp.status == "OK":
        for item in evidence.instances:
            observation = fresh(item.observation, instant)
            instances.append(
                item.model_copy(
                    update={
                        "observation": observation,
                        **(
                            {
                                "state": "UNKNOWN",
                                "started_at": None,
                                "heartbeat_at": None,
                            }
                            if observation.status != "OK"
                            else {}
                        ),
                    }
                )
            )
        for item in evidence.storage:
            observation = fresh(item.observation, instant)
            verified = (
                observation.status == "OK"
                and item.qualification == "QUALIFIED_READ_ONLY"
            )
            storage[item.identity] = item.model_copy(
                update={
                    "observation": observation,
                    **({"state": "UNKNOWN"} if not verified else {}),
                }
            )
        # Records retain old observation clocks/classifications but never active alerts.
        diagnostics = [
            d.model_copy(update={"observation": fresh(d.observation, instant)})
            for d in evidence.diagnostics
        ]
    return ObservabilityDTO(
        status="OK" if stamp.status == "OK" else "UNKNOWN",
        generated_at=instant.isoformat(),
        evidence_mode="UNCONFIGURED" if host is None else host.evidence_mode,
        resources=resources,
        evidence_observation=stamp,
        instances=instances,
        storage=[
            *storage.values(),
            StorageDTO(identity="questdb", state="DEFER", qualification="DEFER"),
        ],
        diagnostics=diagnostics,
    )
