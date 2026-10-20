"""Host-only deterministic QA. Never execute pytest/TestClient in Codex sandbox."""

import json
import os
import socket
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from market_data_foundation.web import observability as obs
from market_data_foundation.web.cpi_api import create_app
from tests.r5_observability_fixture import fixture_host

AT = datetime(2026, 10, 20, tzinfo=UTC)


@pytest.fixture(autouse=True)
def forbid_external(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network_or_listener_forbidden")

    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)
    for name in ("connect", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, forbidden)


def test_no_host_no_reads_no_provider_or_environment(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("implicit_capability_forbidden")

    monkeypatch.setattr(obs, "_read_fixed", forbidden)
    monkeypatch.setattr(os, "statvfs", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    view = obs.project(None, now=AT)
    assert view.status == "UNKNOWN"
    assert view.evidence_mode == "UNCONFIGURED"
    assert view.resources.cpu_load.values is None
    assert view.resources.memory.total is None
    assert view.resources.disks[0].total is None
    assert view.resources.networks == view.instances == view.diagnostics == []
    assert [(s.identity, s.state) for s in view.storage] == [
        ("postgresql", "UNKNOWN"),
        ("garage", "UNKNOWN"),
        ("questdb", "DEFER"),
    ]


def synthetic_linux(monkeypatch, *, read_error=None, change=None, elapsed=0.1):
    files = {
        "/proc/loadavg": "0.25 0.50 0.75 1/12 42\n",
        "/proc/meminfo": "MemTotal: 1024 kB\nMemAvailable: 512 kB\n",
        "/proc/net/dev": (
            "header\nheader\n fixture0: 123 0 0 0 0 0 0 0 456 0 0 0 0 0 0 0\n"
        ),
    }
    if change:
        files.update(change)
    calls = []

    def read(path):
        calls.append(path)
        if read_error:
            raise read_error("private-sensitive-response")
        return files[path]

    monkeypatch.setattr(obs, "_read_fixed", read)
    mounts = []

    def disk(path):
        mounts.append(path)
        return SimpleNamespace(f_frsize=4096, f_blocks=100, f_bavail=40)

    monkeypatch.setattr(os, "statvfs", disk)
    ticks = iter([0.0, elapsed])
    monkeypatch.setattr(obs.time, "monotonic", lambda: next(ticks))
    return obs.LinuxSampler("fixture-linux", ("fixture0",)), calls, mounts


def test_fixed_sources_units_counter_semantics_and_named_mount(monkeypatch):
    sampler, files, mounts = synthetic_linux(monkeypatch)
    view = sampler()
    assert files == ["/proc/loadavg", "/proc/meminfo", "/proc/net/dev"]
    assert mounts == ["/"]
    assert view.cpu_load.values == [0.25, 0.5, 0.75]
    assert view.cpu_load.unit == "runnable_tasks_1m_5m_15m"
    assert view.memory.total == 1024 * 1024
    assert view.memory.available == 512 * 1024
    assert view.disks[0].total == 100 * 4096
    assert view.disks[0].mount == "root"
    assert view.networks[0].received == 123
    assert view.networks[0].transmitted == 456
    assert view.networks[0].unit == "cumulative_bytes"
    assert view.sample_duration_seconds == 0.1
    assert "percent" not in view.model_dump_json()
    assert "throughput" not in view.model_dump_json()


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (PermissionError, "permission_denied"),
        (OSError, "source_unavailable"),
        (OverflowError, "limit_exceeded"),
        (ValueError, "invalid_sample"),
    ],
)
def test_read_failure_redacted_per_resource(monkeypatch, error, reason):
    sampler, _, _ = synthetic_linux(monkeypatch, read_error=error)
    view = sampler()
    assert view.cpu_load.observation.classification == reason
    assert view.memory.observation.classification == reason
    assert view.networks[0].observation.classification == reason
    assert (
        view.cpu_load.values is view.memory.total is view.networks[0].received is None
    )
    assert view.disks[0].observation.status == "OK"
    assert "private-sensitive" not in view.model_dump_json()


@pytest.mark.parametrize(
    ("path", "content", "field"),
    [
        ("/proc/loadavg", "nan 0.1 0.2 1/2 2", "cpu_load"),
        ("/proc/loadavg", "-1 0.1 0.2 1/2 2", "cpu_load"),
        ("/proc/loadavg", "1e100 0 0 1/2 2", "cpu_load"),
        ("/proc/loadavg", "0 0", "cpu_load"),
        ("/proc/meminfo", "MemTotal: 1 MB\nMemAvailable: 1 MB", "memory"),
        ("/proc/meminfo", "MemTotal: 1 kB\nMemAvailable: 2 kB", "memory"),
        ("/proc/meminfo", "MemTotal: 0 kB\nMemAvailable: 0 kB", "memory"),
        ("/proc/meminfo", "MemTotal: 1 kB\nMemTotal: 1 kB", "memory"),
        ("/proc/net/dev", "a\nb\nfixture0: -1", "networks"),
        ("/proc/net/dev", "a\nb\nother0: 0", "networks"),
    ],
)
def test_malformed_or_inconsistent_sample(monkeypatch, path, content, field):
    sampler, _, _ = synthetic_linux(monkeypatch, change={path: content})
    item = getattr(sampler(), field)
    if isinstance(item, list):
        item = item[0]
    assert item.observation.status == "UNKNOWN"
    assert item.observation.classification == "invalid_sample"


@pytest.mark.parametrize("elapsed", [-1.0, 2.01, float("nan")])
def test_bounded_sample_clock(monkeypatch, elapsed):
    sampler, _, _ = synthetic_linux(monkeypatch, elapsed=elapsed)
    view = sampler()
    assert view.cpu_load.values is view.memory.total is None
    assert view.disks[0].observation.status == "UNKNOWN"
    assert view.sample_duration_seconds is None


def test_reader_allowlist_limit_regular_file_and_no_follow(monkeypatch):
    opened, closed, remaining = [], [], bytearray(b"x" * (obs.MAX_BYTES + 1))

    def open_file(path, flags):
        opened.append((path, flags))
        return 9

    def read_file(fd, length):
        assert fd == 9
        chunk = bytes(remaining[: min(1000, length)])
        del remaining[: len(chunk)]
        return chunk

    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFREG))
    monkeypatch.setattr(os, "read", read_file)
    monkeypatch.setattr(os, "close", closed.append)
    with pytest.raises(ValueError):
        obs._read_fixed("/etc/passwd")
    assert opened == []
    with pytest.raises(OverflowError):
        obs._read_fixed("/proc/meminfo")
    assert closed == [9]
    assert opened[0][1] & os.O_NOFOLLOW
    assert opened[0][1] & os.O_NONBLOCK
    monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFIFO))
    with pytest.raises(ValueError):
        obs._read_fixed("/proc/loadavg")
    assert closed == [9, 9]


@pytest.mark.parametrize(
    "interfaces", [("../private",), ("a", "a"), tuple(f"n{i}" for i in range(9))]
)
def test_invalid_interface_no_path_injection(interfaces):
    with pytest.raises((ValidationError, ValueError)):
        obs.LinuxSampler("fixture-host", interfaces)


def test_trusted_fixture_identity_times_and_unqualified_storage():
    host = fixture_host(AT)
    view = obs.project(host, now=AT)
    assert view.status == "OK"
    assert view.instances[0].state == "DEGRADED"
    assert view.instances[0].observe_only is True
    assert view.instances[0].related == view.diagnostics[0].related
    assert view.storage[0].state == "UNAVAILABLE"
    assert view.storage[1].state == "UNKNOWN"
    data = host.evidence()
    unqualified = data.storage[0].model_copy(
        update={"qualification": "UNKNOWN", "state": "AVAILABLE"}
    )
    host = replace(
        host, evidence=lambda: data.model_copy(update={"storage": [unqualified]})
    )
    assert obs.project(host, now=AT).storage[0].state == "UNKNOWN"


@pytest.mark.parametrize(("delta", "reason"), [(61, "stale"), (-1, "clock_invalid")])
def test_expiry_clears_values_states_and_active_diagnostics(delta, reason):
    view = obs.project(fixture_host(AT), now=AT + timedelta(seconds=delta))
    assert view.status == "UNKNOWN"
    assert view.resources.cpu_load.values is None
    assert view.resources.memory.total is None
    assert view.resources.networks[0].received is None
    assert view.resources.cpu_load.observation.classification == reason
    assert view.instances == view.diagnostics == []
    assert view.storage[0].state == "UNKNOWN"


def test_independent_record_clock_and_timezone_semantics():
    host = fixture_host(AT)
    data = host.evidence()
    earlier = data.instances[0].observation.model_copy(
        update={"sampled_at": (AT - timedelta(seconds=61)).isoformat()}
    )
    old = data.instances[0].model_copy(
        update={"observation": earlier, "started_at": None, "heartbeat_at": None}
    )
    diag = data.diagnostics[0].model_copy(update={"observation": earlier})
    host = replace(
        host,
        evidence=lambda: data.model_copy(
            update={"instances": [old], "diagnostics": [diag]}
        ),
    )
    view = obs.project(host, now=AT)
    assert view.instances[0].state == "UNKNOWN"
    assert view.instances[0].started_at is view.instances[0].heartbeat_at is None
    assert view.diagnostics[0].observation.classification == "stale"
    assert obs.parse_time("2026-10-20T08:00:00+08:00") == AT
    with pytest.raises(ValueError):
        obs.parse_time("2026-10-20T00:00:00")


def test_failing_host_no_exception_leak_or_implicit_fallback():
    def broken():
        raise RuntimeError("private-sensitive-dsn")

    view = obs.project(obs.ObservabilityHost(sampler=broken, evidence=broken), now=AT)
    assert view.status == "UNKNOWN"
    assert view.evidence_observation.classification == "host_contract_invalid"
    assert view.resources.cpu_load.values is None
    assert "private-sensitive" not in view.model_dump_json()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(diagnostics=data["diagnostics"] * 51),
        lambda data: data["diagnostics"][0].update(
            message="private-sensitive-response"
        ),
        lambda data: data["diagnostics"][0].update(
            classification="private-sensitive-response"
        ),
        lambda data: data["diagnostics"][0]["related"].update(
            asset_identity="invented.asset"
        ),
        lambda data: data["diagnostics"][0]["related"].update(
            job_identity="postgres://private"
        ),
        lambda data: data["instances"][0].update(
            component_identity="invented.component"
        ),
        lambda data: data["instances"][0].update(started_at="2099-01-01T00:00:00Z"),
        lambda data: data["storage"][0].update(identity="questdb"),
    ],
)
def test_untrusted_fields_limits_and_identities_fail_closed(mutation):
    host = fixture_host(AT)
    payload = host.evidence().model_dump()
    mutation(payload)
    # Deliberately bypass construction; projection must revalidate all nested DTOs.
    forged = obs.TrustedEvidence.model_construct(**payload)
    view = obs.project(replace(host, evidence=lambda: forged), now=AT)
    assert view.evidence_observation.classification == "host_contract_invalid"
    assert view.instances == view.diagnostics == []
    assert "private-sensitive" not in view.model_dump_json()


def test_get_contract_no_queries_mutations_and_no_caching():
    with TestClient(create_app()) as client:
        for _ in range(2):
            response = client.get("/api/r5/observability")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["status"] == "UNKNOWN"
        for params in (
            {"path": "/etc/passwd"},
            {"host": "private"},
            [("path", "a"), ("path", "b")],
            {"as_of": "private"},
        ):
            response = client.get("/api/r5/observability", params=params)
            assert response.status_code == 422
            assert response.json() == {
                "status": "NOT_AVAILABLE",
                "classification": "invalid_request",
            }
            assert response.headers["cache-control"] == "no-store"
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert client.request(method, "/api/r5/observability").status_code == 405
    with TestClient(create_app(observability_host=fixture_host())) as client:
        view = client.get("/api/r5/observability").json()
        assert view["evidence_mode"] == "OFFLINE_FIXTURE"
        assert view["diagnostics"][0]["related"]["job_identity"] == "fixture.job.1"
    schema = create_app().openapi()
    assert json.loads(Path("web/openapi.json").read_text()) == schema
    assert set(schema["paths"]["/api/r5/observability"]) == {"get"}
    for name, model in schema["components"]["schemas"].items():
        if "properties" in model:
            assert model["additionalProperties"] is False, name


def test_non_linux_and_no_named_interface_never_guess(monkeypatch):
    sampler, files, _ = synthetic_linux(monkeypatch)
    monkeypatch.setattr(obs.sys, "platform", "unsupported")
    view = sampler()
    assert view.cpu_load.values is None
    assert files == []
    monkeypatch.setattr(obs.sys, "platform", "linux")
    sampler, files, _ = synthetic_linux(monkeypatch)
    view = replace(sampler, interfaces=())()
    assert "/proc/net/dev" not in files
    assert view.networks == []


@pytest.mark.parametrize("jump", [-10, 10])
def test_wall_clock_discontinuity_invalidates_whole_sample(monkeypatch, jump):
    sampler, _, _ = synthetic_linux(monkeypatch)
    instants = iter([AT, AT + timedelta(seconds=jump)])

    class Clock(datetime):
        @staticmethod
        def now(zone):
            return next(instants)

    monkeypatch.setattr(obs, "datetime", Clock)
    view = sampler()
    assert view.cpu_load.observation.classification == "clock_invalid"
    assert view.cpu_load.values is view.memory.total is None


def test_no_qualified_storage_without_named_current_qualification():
    with pytest.raises(ValidationError):
        obs.StorageDTO(identity="postgresql", qualification="QUALIFIED_READ_ONLY")


def test_invalid_resource_host_cannot_leak_values():
    host = fixture_host(AT)
    data = host.sampler().model_dump()
    data["memory"]["available"] = data["memory"]["total"] + 1
    forged = obs.ResourcesDTO.model_construct(**data)
    view = obs.project(replace(host, sampler=lambda: forged), now=AT)
    assert view.resources.memory.total is None
    assert view.resources.cpu_load.values is None
    assert view.resources.cpu_load.observation.classification == "invalid_sample"


def test_synthetic_source_cannot_claim_authorized_host_mode():
    view = obs.project(
        replace(fixture_host(AT), evidence_mode="AUTHORIZED_HOST"), now=AT
    )
    assert view.status == "UNKNOWN"
    assert view.evidence_observation.classification == "host_contract_invalid"
    assert view.resources.cpu_load.values is None
    assert view.instances == view.diagnostics == []


def test_record_cannot_be_newer_than_its_trusted_envelope():
    host = fixture_host(AT)
    evidence = host.evidence()
    stamp = evidence.observation.model_copy(
        update={"sampled_at": (AT - timedelta(seconds=1)).isoformat()}
    )
    forged = evidence.model_copy(update={"observation": stamp})
    view = obs.project(replace(host, evidence=lambda: forged), now=AT)
    assert view.evidence_observation.classification == "host_contract_invalid"
    assert view.instances == view.diagnostics == []
