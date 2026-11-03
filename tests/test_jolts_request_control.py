"""Offline JOLTS composition uses shared BLS admission, with no real state I/O."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from market_data_foundation.historical import cpi_timeseries as core
from market_data_foundation.historical import jolts_timeseries as jolts
from market_data_foundation.historical.bls_timeseries_profile import JOLTS_PROFILE
from market_data_foundation.historical.cpi_request_control import CpiRequestControl
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
)
from market_data_foundation.web import jolts_runtime as runtime
from tests.test_cpi_timeseries_web import no_network  # noqa: F401
from tests.test_jolts_timeseries import AT, OPENINGS


@pytest.fixture
def composition(tmp_path, monkeypatch):
    def memory_service(**_options):
        return jolts.JoltsTimeSeriesService(
            InMemoryTimeSeriesStore(profile=JOLTS_PROFILE, clock=lambda: AT),
            jolts.BlsJoltsRangeSource(),
        )

    # Replace only storage ports; exercise real JOLTS query/write factories.
    monkeypatch.setattr(core, "build_service", memory_service)
    monkeypatch.setattr(runtime, "build_read_service", memory_service)

    def make(**options):
        return runtime.create_jolts_app(
            packages=SimpleNamespace(),
            runtime_path=tmp_path / "unused-runtime.toml",
            secret_path=tmp_path / "unused-secret.toml",
            state_directory=tmp_path / "jolts-state",
            allow_read=True,
            allow_collect=True,
            allow_source=True,
            **options,
        ).state.jolts_host

    return make


def test_default_query_collection_and_writer_share_existing_bls_path(composition):
    host = composition()
    with (
        host.service(upstream=True, credential_loader=lambda: "d" * 32) as query,
        host.service(write=True, credential_loader=lambda: "d" * 32) as collection,
    ):
        writer = jolts.build_service(credential_loader=lambda: "d" * 32)
        try:
            controls = [
                service.source._request_control
                for service in (query, collection, writer)
            ]
            assert all(
                c.state_path == core.DEFAULT_REQUEST_CONTROL_PATH for c in controls
            )
            assert all(c.lock_path == controls[0].lock_path for c in controls)
        finally:
            writer.close()


def test_explicit_control_path_is_used_by_both_app_factories_and_writer(
    composition, tmp_path
):
    path = tmp_path / "controlled-test.json"
    host = composition(request_control_path=path)
    with (
        host.service(upstream=True, credential_loader=lambda: "d" * 32) as query,
        host.service(write=True, credential_loader=lambda: "d" * 32) as collection,
    ):
        writer = jolts.build_service(request_control_path=path)
        try:
            assert all(
                service.source._request_control.state_path == path
                for service in (query, collection, writer)
            )
            assert not path.exists()
            assert not path.with_name(path.name + ".lock").exists()
        finally:
            writer.close()


@pytest.mark.parametrize("operation", ["query", "collection"])
@pytest.mark.parametrize("block", ["cooldown", "budget_exhausted", "in_flight"])
def test_shared_admission_blocks_default_jolts_before_transport(
    composition, tmp_path, monkeypatch, operation, block
):
    # Redirect the existing default itself, never access the real shared state.
    path = tmp_path / "shared-bls.json"
    monkeypatch.setattr(core, "DEFAULT_REQUEST_CONTROL_PATH", path)
    shared = CpiRequestControl(path, clock=lambda: AT, min_interval_seconds=0)
    if block == "cooldown":
        with shared.attempt() as ticket:
            ticket.record_response(429, {"Retry-After": "900"})
    elif block == "budget_exhausted":
        for _ in range(400):
            with shared.attempt() as ticket:
                ticket.record_response(200, {})

    host = composition()
    calls = []

    def forbidden_transport(*args, **kwargs):
        calls.append(True)
        raise AssertionError("transport_must_not_run")

    with host.service(
        upstream=operation == "query",
        write=operation == "collection",
        credential_loader=lambda: "d" * 32,
    ) as service:
        control = service.source._request_control
        assert control.state_path == shared.state_path
        assert control.lock_path == shared.lock_path
        control._clock = service.source._clock = lambda: AT
        monkeypatch.setattr(service.source._transport, "post", forbidden_transport)
        with shared.attempt() if block == "in_flight" else nullcontext():
            before = path.read_bytes()
            with pytest.raises(jolts.JoltsTimeSeriesError) as error:
                if operation == "query":
                    service.query_range(
                        "2026-08",
                        "2026-08",
                        series_ids=(OPENINGS,),
                        allow_upstream=True,
                    )
                else:
                    service.update_range("2026-08", "2026-08", series_ids=(OPENINGS,))
            assert error.value.classification == f"cpi_timeseries_request_{block}"
            assert error.value.attempts == 0
            assert not calls
            assert path.read_bytes() == before
        assert service.store.status()["observation_count"] == 0
    assert not (tmp_path / "jolts-state").exists()
