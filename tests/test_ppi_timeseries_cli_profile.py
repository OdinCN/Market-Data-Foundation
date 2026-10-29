"""Offline CLI profile selection and shared dispatcher compatibility checks."""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
from market_data_foundation.historical.cpi_timeseries_cli import (
    DEFAULT_STATE,
    InterfaceError,
    WorkerService,
    build_parser,
    error_document,
    execute,
    main,
    package_credential_loader,
    selected_series,
)
from market_data_foundation.historical.ppi_timeseries_cli import main as ppi_main


class RecordingService:
    def __init__(self, profile=None, *, complete=True):
        if profile is not None:
            self.profile = profile
        self.calls = []
        self.closed = False
        self.complete = complete

    def query_range(self, start, end, **options):
        self.calls.append((start, end, options))
        return {
            "complete": self.complete,
            "points": [
                {"series_id": series, "index_value": "100", "yoy_pct": None}
                for series in options["series_ids"]
            ],
        }

    init_range = query_range
    update_range = query_range
    confirm_current_availability = query_range

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "basket,adjustment,expected",
    [
        ("headline", "SA", ("WPSFD4",)),
        ("overall", "NSA", ("WPUFD4",)),
        ("core", "SA", ("WPSFD49104",)),
        ("core", "NSA", ("WPUFD49104",)),
        ("all", "SA", ("WPSFD4", "WPSFD49104")),
        ("all", "NSA", ("WPUFD4", "WPUFD49104")),
        ("core", "all", ("WPSFD49104", "WPUFD49104")),
    ],
)
def test_ppi_basket_adjustment_mapping(basket, adjustment, expected):
    params = {"basket": basket, "adjustment": adjustment}
    assert selected_series(params, profile=PPI_PROFILE) == expected
    service = RecordingService(PPI_PROFILE)
    result = execute(service, "query", {"start": "2026-09", "end": "2026-09", **params})
    assert service.calls[0][2]["series_ids"] == expected
    assert tuple(point["series_id"] for point in result["points"]) == expected
    assert result["measure_complete"]


@pytest.mark.parametrize("profile", [CPI_PROFILE, PPI_PROFILE])
@pytest.mark.parametrize("action", ["init", "update", "confirm-availability", "query"])
def test_execute_uses_bound_profile_defaults(profile, action):
    service = RecordingService(profile)
    execute(service, action, {"start": "2026-09", "end": "2026-09"})
    expected = profile.series_ids[:1] if action == "query" else profile.series_ids
    assert service.calls[0][2]["series_ids"] == expected


def test_cpi_defaults_and_existing_cli_state_path_are_unchanged():
    assert selected_series({}) == CPI_PROFILE.series_ids
    assert selected_series({}, query=True) == ("CUSR0000SA0",)
    service = RecordingService()
    execute(service, "query", {"start": "2026-09", "end": "2026-09"})
    assert service.calls[0][2]["series_ids"] == ("CUSR0000SA0",)
    assert str(DEFAULT_STATE) == "artifacts/cpi-timeseries/schedule.json"
    assert build_parser().parse_args(["status"]).schedule_state == DEFAULT_STATE


@pytest.mark.parametrize(
    "profile,other", [(PPI_PROFILE, CPI_PROFILE), (CPI_PROFILE, PPI_PROFILE)]
)
def test_cross_profile_series_rejected_before_service(profile, other):
    service = RecordingService(profile)
    with pytest.raises(InterfaceError) as caught:
        execute(
            service,
            "query",
            {"start": "2026-09", "end": "2026-09", "series": other.series_ids[0]},
        )
    assert error_document(caught.value)["classification"] == "unsupported_series"
    assert not service.calls


def test_ppi_explicit_series_deduplicates_and_keeps_order():
    assert selected_series(
        {"series": [" WPUFD49104,WPSFD4 ", "WPUFD49104"]}, profile=PPI_PROFILE
    ) == ("WPUFD49104", "WPSFD4")
    assert selected_series({}, profile=PPI_PROFILE) == PPI_PROFILE.series_ids
    assert selected_series({}, query=True, profile=PPI_PROFILE) == ("WPSFD4",)


@pytest.mark.parametrize(
    "params,classification",
    [
        (
            {"series": "WPSFD4", "basket": "core"},
            "series_and_basket_are_mutually_exclusive",
        ),
        ({"series": 1}, "invalid_series"),
        ({"series": []}, "unsupported_series"),
        ({"basket": []}, "invalid_series_selection"),
        ({"basket": "other"}, "invalid_basket"),
        ({"adjustment": "other"}, "invalid_adjustment"),
    ],
)
def test_ppi_retains_interface_error_classifications(params, classification):
    with pytest.raises(InterfaceError) as caught:
        selected_series(params, profile=PPI_PROFILE)
    assert caught.value.classification == classification


@pytest.mark.parametrize("complete,measure", [(False, "index"), (True, "yoy")])
def test_ppi_worker_preserves_service_and_measure_completeness(complete, measure):
    opened = []

    def factory():
        service = RecordingService(PPI_PROFILE, complete=complete)
        opened.append(service)
        return service

    worker = WorkerService(factory, profile=PPI_PROFILE)
    assert not opened
    result = execute(
        worker,
        "query",
        {
            "start": "2026-09",
            "end": "2026-09",
            "basket": "all",
            "adjustment": "all",
            "measure": measure,
            "local_only": True,
        },
    )
    assert opened[0].calls[0][2]["series_ids"] == PPI_PROFILE.series_ids
    assert opened[0].calls[0][2]["allow_upstream"] is False
    assert opened[0].closed
    assert result["complete"] is complete
    assert result["measure_complete"] is False


def test_worker_rejects_mismatched_factory_profile_and_closes_service():
    service = RecordingService(CPI_PROFILE)
    worker = WorkerService(lambda: service, profile=PPI_PROFILE)
    with pytest.raises(CpiTimeSeriesError) as caught:
        execute(worker, "query", {"start": "2026-09", "end": "2026-09"})
    assert (
        error_document(caught.value)["classification"]
        == "bls_timeseries_profile_mismatch"
    )
    assert not service.calls
    assert service.closed


def test_custom_profiles_are_rejected_without_opening_service():
    custom = replace(PPI_PROFILE, key="custom")
    with pytest.raises(ValueError, match="bls_timeseries_profile_invalid"):
        selected_series({}, profile=custom)
    with pytest.raises(ValueError, match="bls_timeseries_profile_invalid"):
        WorkerService(lambda: pytest.fail("unexpected service startup"), profile=custom)


@pytest.mark.parametrize("entrypoint", [ppi_main, main])
@pytest.mark.parametrize("action", ["query", "init", "update", "confirm-availability"])
def test_ppi_main_explicit_series_and_factory_profile(
    entrypoint, action, tmp_path, capsys
):
    service = RecordingService(PPI_PROFILE)
    factories, schedulers = [], []

    def factory(**options):
        factories.append(options)
        return service

    def scheduler(*args, **options):
        schedulers.append((args, options))

    argv = [action, "--start", "2026-09", "--end", "2026-09", "--series", "WPSFD4"]
    options = {"profile": PPI_PROFILE} if entrypoint is main else {}
    assert (
        entrypoint(
            argv, service_factory=factory, scheduler_factory=scheduler, **options
        )
        == 0
    )
    assert factories[0]["profile"] is PPI_PROFILE
    assert service.calls[0][2]["series_ids"] == ("WPSFD4",)
    assert schedulers[0][0][1] == Path("artifacts/ppi-timeseries/schedule.json")
    assert schedulers[0][1]["profile"] is PPI_PROFILE
    assert service.closed
    assert json.loads(capsys.readouterr().out)["complete"]


@pytest.mark.parametrize("action", ["query", "init", "update", "confirm-availability"])
def test_ppi_main_cpi_series_rejected_before_factory(action, capsys):
    def forbidden(**options):
        pytest.fail("invalid series opened a service")

    assert (
        ppi_main(
            [
                action,
                "--start",
                "2026-09",
                "--end",
                "2026-09",
                "--series",
                "CUSR0000SA0",
            ],
            service_factory=forbidden,
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["classification"] == "unsupported_series"


@pytest.mark.parametrize(
    "argv", [["serve"], ["schedule", "status"], ["signals", "status"], ["update"]]
)
def test_ppi_unsupported_commands_never_open_service_or_state(
    argv, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)

    def forbidden(**options):
        pytest.fail("unsupported command opened service")

    assert ppi_main(argv, service_factory=forbidden) == 2
    assert (
        json.loads(capsys.readouterr().err)["classification"]
        == "ppi_command_unsupported"
    )
    assert not (tmp_path / "artifacts").exists()


def test_ppi_parser_help_and_240_month_request_default():
    parser = build_parser(profile=PPI_PROFILE)
    args = parser.parse_args(["status"])
    now = datetime.now()
    year, month = map(int, args.history_start.split("-"))
    assert now.year * 12 + now.month - (year * 12 + month) == 239
    assert args.schedule_state == Path("artifacts/ppi-timeseries/schedule.json")
    assert "PPI time series" in parser.format_help()


def test_ppi_rejects_explicit_cpi_schedule_path_before_factory(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    assert (
        ppi_main(
            ["--schedule-state", str(DEFAULT_STATE), "status"],
            service_factory=lambda **_: pytest.fail("opened service"),
        )
        == 2
    )
    assert (
        json.loads(capsys.readouterr().err)["classification"]
        == "ppi_schedule_state_conflicts_with_cpi"
    )
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.parametrize("bound_profile", [None, CPI_PROFILE])
def test_ppi_main_rejects_unbound_or_cpi_factory_service(bound_profile, capsys):
    service = RecordingService(bound_profile)
    assert ppi_main(["status"], service_factory=lambda **_: service) == 2
    assert (
        json.loads(capsys.readouterr().err)["classification"]
        == "bls_timeseries_profile_mismatch"
    )
    assert service.closed and not service.calls


def test_ppi_default_factories_receive_profile_without_io(
    monkeypatch, tmp_path, capsys
):
    from market_data_foundation.historical import (
        cpi_release_schedule,
        cpi_timeseries,
        cpi_timeseries_cli,
    )

    monkeypatch.chdir(tmp_path)
    service = RecordingService(PPI_PROFILE)
    built, scheduled, credentials = [], [], []

    def build(**options):
        built.append(options)
        assert options["credential_loader"]() == "offline-test-key"
        return service

    def credential_loader(*args, **options):
        credentials.append(options)
        return lambda: "offline-test-key"

    def scheduler(*args, **options):
        scheduled.append((args, options))

    monkeypatch.setattr(cpi_timeseries, "build_service", build)
    monkeypatch.setattr(
        cpi_timeseries_cli, "package_credential_loader", credential_loader
    )
    monkeypatch.setattr(cpi_release_schedule, "CpiReleaseScheduler", scheduler)
    monkeypatch.setattr(
        cpi_timeseries_cli,
        "persisted_history_start",
        lambda *args: pytest.fail("PPI read scheduler state"),
    )
    assert (
        ppi_main(
            [
                "query",
                "--start",
                "2026-09",
                "--end",
                "2026-09",
                "--series",
                "WPSFD4",
                "--local-only",
            ]
        )
        == 0
    )
    assert built[0]["profile"] is PPI_PROFILE
    assert built[0]["max_transport_attempts"] == 1
    assert credentials[0]["profile"] is PPI_PROFILE
    assert scheduled[0][1]["profile"] is PPI_PROFILE
    assert service.calls[0][2]["allow_upstream"] is False
    assert not (tmp_path / "artifacts").exists()
    assert json.loads(capsys.readouterr().out)["complete"]


@pytest.mark.parametrize("enabled", [("ppi",), ("cpi",), ()])
def test_ppi_package_credentials_require_ppi_capability(monkeypatch, tmp_path, enabled):
    from market_data_foundation.web import package_store, packages

    calls = []
    store = SimpleNamespace(
        read=lambda *args: SimpleNamespace(enabled_capability_ids=enabled)
    )
    monkeypatch.setattr(package_store, "LocalPackageStore", lambda *args: store)

    def host(store, *args, **options):
        def load(*args, **kwargs):
            calls.append(True)
            return "offline-test-key"

        return SimpleNamespace(store=store, load_credential=load)

    monkeypatch.setattr(packages, "PackageHost", host)
    load = package_credential_loader(tmp_path, "offline", profile=PPI_PROFILE)
    if "ppi" in enabled:
        assert load() == "offline-test-key"
        assert calls == [True]
    else:
        with pytest.raises(InterfaceError, match="ppi_capability_disabled"):
            load()
        assert not calls
