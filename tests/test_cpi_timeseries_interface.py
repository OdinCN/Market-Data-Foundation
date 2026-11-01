"""Offline interface checks: no listener, credentials, PG, Garage, or BLS."""

import json
from pathlib import Path

import pytest

from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
from market_data_foundation.historical.cpi_timeseries_cli import (
    InterfaceError,
    execute,
    main,
    run_worker,
    selected_series,
)
from market_data_foundation.historical.cpi_timeseries_http import (
    admitted,
    handle_request,
)
from market_data_foundation.historical.cpi_timeseries_store import SERIES_IDS


class Service:
    def __init__(self):
        self.calls = []
        self.closed = False

    def _result(self, name, start, end, **kwargs):
        self.calls.append((name, start, end, kwargs))
        return {
            "points": [
                {
                    "series_id": kwargs["series_ids"][0],
                    "month": start,
                    "index_value": "300.1",
                    "mom_pct": "0.20",
                    "yoy_pct": None,
                    "origin": "local",
                    "raw_sha256": "a" * 64,
                }
            ],
            "complete": True,
            "provider_requests": 0,
            "persisted": name != "query",
        }

    def init_range(self, start, end, **kwargs):
        return self._result("init", start, end, **kwargs)

    def update_range(self, start, end, **kwargs):
        return self._result("update", start, end, **kwargs)

    def query_range(self, start, end, **kwargs):
        return self._result("query", start, end, **kwargs)

    def status(self):
        self.calls.append(("status",))
        return {"observation_count": 1}

    def close(self):
        self.closed = True


class Scheduler:
    def __init__(self, service=None, state_path=None, **options):
        self.service = service
        self.state_path = state_path
        self.options = options
        self.calls = []

    def status(self):
        return {"status": "not_configured"}

    def refresh_calendar(self):
        self.calls.append("refresh")
        return {"status": "calendar_refreshed"}

    def due_check(self):
        self.calls.append("due")
        return {"status": "not_due"}


def test_init_defaults_to_four_series_query_defaults_headline_sa():
    service = Service()
    execute(service, "init", {"start": "2010-01", "end": "2026-09"})
    from market_data_foundation.historical.bls_timeseries_profile import CPI_PROFILE

    assert set(service.calls[-1][3]["series_ids"]) == set(CPI_PROFILE.series_ids)
    result = execute(service, "query", {"start": "2026-09", "end": "2026-09"})
    assert service.calls[-1][3]["series_ids"] == ("CUSR0000SA0",)
    assert result["persisted"] is False
    assert result["points"][0]["value"] == "300.1"
    assert result["points"][0]["raw_sha256"] == "a" * 64


def test_query_measure_completeness_does_not_hide_calculation_gap():
    service = Service()
    result = execute(
        service,
        "query",
        {
            "start": "2026-09",
            "end": "2026-09",
            "measure": "yoy",
            "basket": "core",
            "adjustment": "NSA",
        },
    )
    assert result["complete"] is True
    assert result["measure_complete"] is False
    assert result["unit"] == "percent"
    assert service.calls[-1][3]["series_ids"] == ("CUUR0000SA0L1E",)


@pytest.mark.parametrize(
    "parameters",
    [
        {"local_only": True},
        {"as_of": "2026-10-01T00:00:00+00:00"},
    ],
)
def test_local_only_and_as_of_never_enable_upstream(parameters):
    service = Service()
    execute(service, "query", {"start": "2026-01", "end": "2026-09", **parameters})
    assert service.calls[-1][3]["allow_upstream"] is False


@pytest.mark.parametrize(
    "parameters",
    [
        {"series": "CUSR0000SA0", "basket": "core"},
        {"series": "not-a-supported-series"},
        {"series": []},
        {"adjustment": "surprise"},
    ],
)
def test_invalid_selection_rejected_before_service(parameters):
    service = Service()
    with pytest.raises(InterfaceError):
        execute(service, "query", {"start": "2026-01", "end": "2026-09", **parameters})
    assert service.calls == []


def test_series_list_and_repeat_are_supported_without_duplicate_work():
    assert selected_series({"series": ["CUSR0000SA0,CUUR0000SA0", "CUSR0000SA0"]}) == (
        "CUSR0000SA0",
        "CUUR0000SA0",
    )


def test_update_without_range_uses_due_target_not_fixed_end():
    service, scheduler = Service(), Scheduler()
    assert execute(service, "update", scheduler=scheduler)["status"] == "not_due"
    assert scheduler.calls == ["due"]
    assert service.calls == []
    with pytest.raises(InterfaceError):
        execute(service, "update", {"start": "2026-01"}, scheduler=scheduler)


@pytest.mark.parametrize(
    "peer,headers",
    [
        ("192.168.10.20", {"Host": "127.0.0.1:18765"}),
        ("127.0.0.1", {"Host": "evil.example:18765"}),
        ("127.0.0.1", {"Host": "127.0.0.1:18765", "Origin": "http://evil.example"}),
        ("127.0.0.1", {"Host": "127.0.0.1:18765", "X-Forwarded-For": "127.0.0.1"}),
        ("127.0.0.1", [("Host", "127.0.0.1:18765"), ("Host", "127.0.0.1:18765")]),
    ],
)
def test_http_boundary_blocks_before_service(peer, headers):
    service = Service()
    code, result = handle_request(service, "GET", "/api/cpi/status", headers, peer=peer)
    assert code == 403
    assert result["classification"] == "loopback_request_required"
    assert service.calls == []


def test_http_query_same_service_semantics_and_no_web():
    service = Service()
    code, result = handle_request(
        service,
        "GET",
        "/api/cpi/query?series=CUUR0000SA0&measure=mom&start=2020-01&end=2020-01&local_only=true",
        {"Host": "127.0.0.1:18765"},
    )
    assert code == 200
    assert result["points"][0]["value"] == "0.20"
    assert service.calls[-1][3]["allow_upstream"] is False
    assert handle_request(service, "GET", "/", {"Host": "127.0.0.1:18765"})[0] == 404


@pytest.mark.parametrize(
    "target,body,headers",
    [
        ("/api/cpi/init", b"[]", {"Content-Type": "application/json"}),
        ("/api/cpi/init", b"{}", {"Content-Type": "text/plain"}),
        ("/api/cpi/init?start=2020-01", b"{}", {"Content-Type": "application/json"}),
        ("/api/cpi/init", b"x" * 65537, {"Content-Type": "application/json"}),
    ],
)
def test_http_mutations_require_bounded_unambiguous_json(target, body, headers):
    service = Service()
    code, result = handle_request(
        service,
        "POST",
        target,
        {"Host": "127.0.0.1:18765", **headers},
        body,
    )
    assert code == 400
    assert result["status"] == "ERROR"
    assert service.calls == []


def test_http_init_calls_persistent_init_not_query():
    service = Service()
    code, result = handle_request(
        service,
        "POST",
        "/api/cpi/init",
        {"Host": "127.0.0.1:18765", "Content-Type": "application/json"},
        json.dumps({"start": "2015-01", "end": "2026-09"}).encode(),
    )
    assert code == 200 and result["persisted"] is True
    assert service.calls[-1][0] == "init"


def test_http_duplicate_query_rejected():
    service = Service()
    code, result = handle_request(
        service,
        "GET",
        "/api/cpi/query?start=2020-01&start=2021-01",
        {"Host": "127.0.0.1:18765"},
    )
    assert code == 400 and result["classification"] == "duplicate_parameter"
    assert service.calls == []


def test_unknown_errors_do_not_leak_credentials(capsys):
    def broken_factory(**kwargs):
        raise RuntimeError("password=must-never-be-printed")

    assert (
        main(["status"], service_factory=broken_factory, scheduler_factory=Scheduler)
        == 2
    )
    captured = capsys.readouterr()
    assert "must-never" not in captured.err
    assert json.loads(captured.err)["classification"] == "internal_error"


def test_cli_routes_candidate_factory_and_closes_service(capsys):
    service, calls = Service(), []

    def factory(**kwargs):
        calls.append(kwargs)
        return service

    result = main(
        [
            "--runtime",
            "/tmp/runtime.toml",
            "--secret",
            "/tmp/secret.toml",
            "query",
            "--start",
            "2026-01",
            "--end",
            "2026-09",
            "--local-only",
        ],
        service_factory=factory,
        scheduler_factory=Scheduler,
    )
    assert result == 0
    assert calls == [
        {
            "runtime_path": Path("/tmp/runtime.toml"),
            "secret_path": Path("/tmp/secret.toml"),
        }
    ]
    assert service.closed
    assert json.loads(capsys.readouterr().out)["provider_requests"] == 0


def test_invalid_arguments_do_not_open_database(capsys):
    def forbidden(**kwargs):
        pytest.fail("must not construct service")

    assert main(["serve", "--port", "80"], service_factory=forbidden) == 2
    assert json.loads(capsys.readouterr().err)["classification"] == "port_out_of_range"


def test_calendar_status_does_not_need_database_or_credentials(capsys):
    def forbidden(**kwargs):
        pytest.fail("calendar-only command opened service")

    assert (
        main(
            ["schedule", "status"],
            service_factory=forbidden,
            scheduler_factory=Scheduler,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "not_configured"


def test_worker_continues_beyond_twelve_ticks():
    scheduler, emissions = Scheduler(), []
    sleeps = []

    def stop_after_14(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 14:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_worker(
            scheduler, poll_seconds=5, sleep=stop_after_14, emit_result=emissions.append
        )
    assert scheduler.calls == ["due"] * 14
    assert len(emissions) == 14


def test_loopback_header_admission_is_strict():
    assert admitted("127.0.0.1", {"Host": "localhost:18765"}, 18765)
    assert not admitted(
        "127.0.0.1", {"Host": "127.0.0.1:18765", "Transfer-Encoding": "chunked"}, 18765
    )


@pytest.mark.parametrize(
    "target,body,method,extra",
    [
        (
            "/api/cpi/init",
            b'{"start":"2020-01","start":"2021-01","end":"2021-01"}',
            "POST",
            {"Content-Type": "application/json"},
        ),
        ("/api/cpi/query?start", b"", "GET", {}),
    ],
)
def test_ambiguous_or_malformed_input_is_json_client_error(target, body, method, extra):
    service = Service()
    code, result = handle_request(
        service, method, target, {"Host": "127.0.0.1:18765", **extra}, body
    )
    assert code == 400
    assert result["status"] == "ERROR"
    assert service.calls == []


def test_calendar_import_is_explicit_local_file_and_needs_no_pg(tmp_path, capsys):
    path = tmp_path / "official.html"
    path.write_bytes(b"<table>official snapshot</table>")
    imports = []

    class ImportScheduler(Scheduler):
        def import_calendar(self, raw, *, acquired_at):
            imports.append((raw, acquired_at.isoformat()))
            return {"action": "calendar_imported"}

    def forbidden(**kwargs):
        pytest.fail("calendar import opened data service")

    assert (
        main(
            ["schedule", "import", str(path), "--acquired-at", "2026-10-28T04:00:00Z"],
            service_factory=forbidden,
            scheduler_factory=ImportScheduler,
        )
        == 0
    )
    assert imports == [
        (b"<table>official snapshot</table>", "2026-10-28T04:00:00+00:00")
    ]
    assert json.loads(capsys.readouterr().out)["action"] == "calendar_imported"


def test_calendar_import_rejects_naive_acquisition_time(tmp_path, capsys):
    def forbidden(**kwargs):
        pytest.fail("calendar import opened data service")

    assert (
        main(
            [
                "schedule",
                "import",
                str(tmp_path / "absent"),
                "--acquired-at",
                "2026-10-28",
            ],
            service_factory=forbidden,
            scheduler_factory=Scheduler,
        )
        == 2
    )
    assert (
        json.loads(capsys.readouterr().err)["classification"]
        == "calendar_acquired_at_aware_timestamp_required"
    )


def test_worker_reopens_service_for_each_actual_operation():
    from market_data_foundation.historical.cpi_timeseries_cli import WorkerService

    services = []

    def factory():
        service = Service()
        services.append(service)
        return service

    proxy = WorkerService(factory)
    assert services == []
    proxy.update_range("2026-07", "2026-09", series_ids=SERIES_IDS)
    proxy.query_range("2026-09", "2026-09", series_ids=SERIES_IDS, allow_upstream=False)
    assert len(services) == 2
    assert all(service.closed for service in services)
    assert services[0].calls[0][0] == "update"
    assert services[1].calls[0][0] == "query"


def test_worker_releases_broken_session_and_next_call_can_recover():
    from market_data_foundation.historical.cpi_timeseries_cli import WorkerService

    broken, healthy = Service(), Service()

    def fail(*args, **kwargs):
        raise RuntimeError("connection lost")

    broken.update_range = fail
    services = iter([broken, healthy])
    proxy = WorkerService(lambda: next(services))
    with pytest.raises(RuntimeError):
        proxy.update_range("2026-09", "2026-09", series_ids=SERIES_IDS)
    assert broken.closed
    assert proxy.update_range("2026-09", "2026-09", series_ids=SERIES_IDS)["persisted"]
    assert healthy.closed


@pytest.mark.parametrize(
    "parameters",
    [
        {"start": "2026-1"},
        {"end": "2025-12"},
        {"start": "1900-01"},
        {"start": "1901-01", "end": "2101-01"},
        {"as_of": "2026-10-28"},
        {"as_of": 1},
        {"as_of": "invalid"},
        {"vintage_mode": []},
        {"vintage_mode": "unknown"},
        {"measure": "ratio"},
        {"measure": []},
        {"local_only": 1},
        {"basket": ""},
        {"adjustment": ""},
        {"basket": False},
    ],
)
def test_new_query_validation_precedes_service(parameters):
    service = Service()
    with pytest.raises((InterfaceError, CpiTimeSeriesError)):
        execute(service, "query", {"start": "2026-01", "end": "2026-09", **parameters})
    assert not service.calls


@pytest.mark.parametrize("mode", ["available", "official"])
def test_vintage_never_silently_uses_current_upstream(mode):
    service = Service()
    params = {"start": "2026-01", "end": "2026-09", "vintage_mode": mode}
    if mode == "official":
        with pytest.raises(InterfaceError, match="official_vintage_unsupported"):
            execute(service, "query", params)
        assert not service.calls
    else:
        execute(service, "query", params)
        assert service.calls[-1][3]["allow_upstream"] is False
        assert service.calls[-1][3]["vintage_mode"] == "available"


@pytest.mark.parametrize(
    "argv,classification",
    [
        (["--history-start", "2026-13", "status"], "cpi_timeseries_month_invalid"),
        (["--signal-poll-seconds", "0", "status"], "signal_poll_seconds_out_of_range"),
        (
            ["query", "--start", "2026-01", "--end", "2025-01"],
            "cpi_timeseries_range_invalid",
        ),
        (
            [
                "query",
                "--start",
                "2026-01",
                "--end",
                "2026-01",
                "--as-of",
                "2026-01-01",
            ],
            "invalid_as_of",
        ),
        (
            [
                "query",
                "--start",
                "2026-01",
                "--end",
                "2026-01",
                "--vintage-mode",
                "official",
            ],
            "cpi_timeseries_official_vintage_unsupported",
        ),
    ],
)
def test_new_cli_invalid_arguments_never_open_service(argv, classification, capsys):
    def forbidden(**kwargs):
        pytest.fail("invalid CLI input constructed service")

    assert main(argv, service_factory=forbidden) == 2
    assert json.loads(capsys.readouterr().err)["classification"] == classification


def test_schedule_fixed_configuration_is_forwarded_without_credentials(capsys):
    schedules = []

    def factory(*args, **kwargs):
        scheduler = Scheduler(*args, **kwargs)
        schedules.append(scheduler)
        return scheduler

    def forbidden(**kwargs):
        pytest.fail("status constructed service")

    assert (
        main(
            [
                "--history-start",
                "2005-01",
                "--signal-poll-seconds",
                "600",
                "signals",
                "status",
            ],
            service_factory=forbidden,
            scheduler_factory=factory,
        )
        == 0
    )
    assert schedules[0].options == {
        "history_start": "2005-01",
        "signal_poll_seconds": 600,
    }
    assert json.loads(capsys.readouterr().out)["status"] == "not_configured"


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"[]",
        b'{"notices":[]}',
        b'{"notices":[null]}',
        b'{"notices":{},"notices":[]}',
    ],
)
def test_http_signal_import_rejects_invalid_envelope(payload):
    service, scheduler = Service(), Scheduler()
    status, result = handle_request(
        service,
        "POST",
        "/api/cpi/signals/import",
        {"Host": "127.0.0.1:18765", "Content-Type": "application/json"},
        payload,
        scheduler=scheduler,
    )
    assert status == 400
    assert result["status"] == "ERROR"
    assert service.calls == scheduler.calls == []


@pytest.mark.parametrize("mode", ["official", "wrong"])
def test_http_unsupported_vintage_is_client_error_before_source(mode):
    service = Service()
    status, result = handle_request(
        service,
        "GET",
        f"/api/cpi/query?start=2026-01&end=2026-09&vintage_mode={mode}",
        {"Host": "127.0.0.1:18765"},
    )
    assert status == 400 and result["status"] == "ERROR"
    assert service.calls == []


def test_signal_file_size_duplicate_keys_and_no_database(tmp_path, capsys):
    def forbidden(**kwargs):
        pytest.fail("signal import constructed service")

    path = tmp_path / "notices.json"
    for raw, classification in (
        (b"x" * 65537, "signal_payload_too_large"),
        (b'{"notices":[],"notices":[]}', "duplicate_parameter"),
        (b"{", "invalid_json"),
    ):
        path.write_bytes(raw)
        assert (
            main(
                ["signals", "import", str(path)],
                service_factory=forbidden,
                scheduler_factory=Scheduler,
            )
            == 2
        )
        assert json.loads(capsys.readouterr().err)["classification"] == classification


@pytest.mark.parametrize("seconds", [True, 4, 3601, 5.5])
def test_worker_invalid_poll_is_rejected_before_tick(seconds):
    scheduler = Scheduler()
    with pytest.raises(InterfaceError, match="poll_seconds_out_of_range"):
        run_worker(scheduler, poll_seconds=seconds)
    assert scheduler.calls == []


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_http_rejects_nonstandard_json_constants(constant):
    service = Service()
    code, result = handle_request(
        service,
        "POST",
        "/api/cpi/update",
        {"Host": "127.0.0.1:18765", "Content-Type": "application/json"},
        ('{"start":' + constant + ',"end":"2026-09"}').encode(),
    )
    assert code == 400 and result["classification"] == "invalid_json"
    assert service.calls == []


def test_cli_scheduled_update_filter_is_rejected_without_database(capsys):
    def forbidden(**kwargs):
        pytest.fail("invalid scheduled update opened service")

    assert main(["update", "--basket", "core"], service_factory=forbidden) == 2
    assert (
        json.loads(capsys.readouterr().err)["classification"]
        == "scheduled_update_does_not_accept_series_filter"
    )


class ConfirmationService(Service):
    def confirm_current_availability(self, start, end, **kwargs):
        self.calls.append(("confirm", start, end, kwargs))
        return {"confirmed_points": 2, "provider_requests": 0}


def test_cli_confirm_availability_routes_selected_series_and_closes(capsys):
    service = ConfirmationService()
    assert (
        main(
            [
                "confirm-availability",
                "--start",
                "1999-01",
                "--end",
                "2026-09",
                "--basket",
                "core",
                "--adjustment",
                "nsa",
            ],
            service_factory=lambda **kwargs: service,
            scheduler_factory=Scheduler,
        )
        == 0
    )
    assert service.calls == [
        ("confirm", "1999-01", "2026-09", {"series_ids": ("CUUR0000SA0L1E",)})
    ]
    assert service.closed
    assert json.loads(capsys.readouterr().out)["confirmed_points"] == 2


def test_http_confirm_availability_defaults_four_series_and_passes_result():
    service = ConfirmationService()
    code, result = handle_request(
        service,
        "POST",
        "/api/cpi/confirm-availability",
        {"Host": "127.0.0.1:18765", "Content-Type": "application/json"},
        b'{"start":"1999-01","end":"2026-09"}',
    )
    assert code == 200 and result == {"confirmed_points": 2, "provider_requests": 0}
    assert service.calls == [
        ("confirm", "1999-01", "2026-09", {"series_ids": tuple(selected_series({}))})
    ]
    assert set(service.calls[0][3]["series_ids"]) == set(SERIES_IDS)


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"start": "2026-09"},
        {"start": "2026-10", "end": "2026-09"},
        {"start": "2026-9", "end": "2026-09"},
        {"start": "2026-09", "end": "2026-09", "as_of": "2020-01-01T00:00:00Z"},
        {"start": "2026-09", "end": "2026-09", "available_at": "2020-01-01T00:00:00Z"},
        {"start": "2026-09", "end": "2026-09", "vintage_mode": "available"},
        {
            "start": "2026-09",
            "end": "2026-09",
            "series": SERIES_IDS[0],
            "basket": "all",
        },
        {"start": "2026-09", "end": "2026-09", "series": []},
        {"start": "2026-09", "end": "2026-09", "measure": "index"},
    ],
)
def test_http_confirm_invalid_parameters_cannot_confirm_or_backdate(params):
    service = ConfirmationService()
    code, result = handle_request(
        service,
        "POST",
        "/api/cpi/confirm-availability",
        {"Host": "127.0.0.1:18765", "Content-Type": "application/json"},
        json.dumps(params).encode(),
    )
    assert code == 400 and result["status"] == "ERROR"
    assert service.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["confirm-availability"],
        ["confirm-availability", "--start", "2026-09"],
        ["confirm-availability", "--start", "2026-10", "--end", "2026-09"],
        [
            "confirm-availability",
            "--start",
            "2026-09",
            "--end",
            "2026-09",
            "--as-of",
            "2020-01-01T00:00:00Z",
        ],
    ],
)
def test_cli_confirm_invalid_input_never_opens_data_service(argv, capsys):
    def forbidden(**kwargs):
        pytest.fail("invalid confirmation input opened service")

    assert main(argv, service_factory=forbidden) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "ERROR"


@pytest.mark.parametrize(
    "peer,extra",
    [
        ("192.168.10.20", {}),
        ("127.0.0.1", {"Origin": "http://localhost"}),
        ("127.0.0.1", {"X-Forwarded-For": "127.0.0.1"}),
    ],
)
def test_confirmation_retains_loopback_boundary(peer, extra):
    service = ConfirmationService()
    code, _ = handle_request(
        service,
        "POST",
        "/api/cpi/confirm-availability",
        {"Host": "127.0.0.1:18765", "Content-Type": "application/json", **extra},
        b'{"start":"1999-01","end":"2026-09"}',
        peer=peer,
    )
    assert code == 403 and service.calls == []


def test_confirmation_is_post_only():
    service = ConfirmationService()
    code, _ = handle_request(
        service,
        "GET",
        "/api/cpi/confirm-availability?start=1999-01&end=2026-09",
        {"Host": "127.0.0.1:18765"},
    )
    assert code == 404 and service.calls == []


def test_worker_proxy_forwards_confirmation_and_local_status_and_closes():
    from market_data_foundation.historical.cpi_timeseries_cli import WorkerService

    opened = []

    def factory():
        service = ConfirmationService()
        opened.append(service)
        return service

    proxy = WorkerService(factory)
    assert (
        proxy.confirm_current_availability("1999-01", "2026-09", series_ids=SERIES_IDS)[
            "confirmed_points"
        ]
        == 2
    )
    assert proxy.status() == {"observation_count": 1}
    assert len(opened) == 2 and all(service.closed for service in opened)
    assert opened[0].calls == [
        ("confirm", "1999-01", "2026-09", {"series_ids": SERIES_IDS})
    ]
    assert opened[1].calls == [("status",)]


@pytest.mark.parametrize(
    "error,code,classification",
    [
        (
            CpiTimeSeriesError("cpi_timeseries_confirmation_outcome_unknown"),
            503,
            "cpi_timeseries_confirmation_outcome_unknown",
        ),
        (RuntimeError("private raw failure must not escape"), 500, "internal_error"),
    ],
)
def test_http_confirmation_failure_returns_classification_only(
    error, code, classification
):
    class BrokenConfirmation(Service):
        def confirm_current_availability(self, *args, **kwargs):
            raise error

    status, result = handle_request(
        BrokenConfirmation(),
        "POST",
        "/api/cpi/confirm-availability",
        {"Host": "127.0.0.1:18765", "Content-Type": "application/json"},
        b'{"start":"1999-01","end":"2026-09"}',
    )
    assert status == code and result["classification"] == classification
    assert "private raw" not in json.dumps(result)
