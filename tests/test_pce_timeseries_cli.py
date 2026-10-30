"""PCE CLI routing with injected synthetic services."""

import json
from datetime import UTC, datetime

import pytest
from test_pce_timeseries_source import source

from market_data_foundation.historical.bls_timeseries_profile import PCE_PROFILE
from market_data_foundation.historical.cpi_timeseries_cli import (
    InterfaceError,
    selected_series,
)
from market_data_foundation.historical.pce_timeseries import PceTimeSeriesService
from market_data_foundation.historical.pce_timeseries_cli import build_parser, main
from market_data_foundation.historical.pce_timeseries_store import (
    InMemoryTimeSeriesStore,
)


@pytest.mark.parametrize(
    "basket,expected",
    [("headline", ("DPCERG",)), ("core", ("DPCCRG",)), ("all", PCE_PROFILE.series_ids)],
)
def test_explicit_metadata_selects_two_sa_series(basket, expected):
    assert (
        selected_series({"basket": basket, "adjustment": "sa"}, profile=PCE_PROFILE)
        == expected
    )
    with pytest.raises(InterfaceError, match="unsupported_series_selection"):
        selected_series({"adjustment": "nsa"}, profile=PCE_PROFILE)


@pytest.mark.parametrize(
    "argv",
    [
        ["serve"],
        ["schedule", "status"],
        ["signals", "status"],
        ["update"],
        ["confirm-availability", "--start", "2026-09", "--end", "2026-09"],
    ],
)
def test_unimplemented_operations_rejected_before_service(argv, capsys):
    assert (
        main(
            argv, service_factory=lambda **kw: pytest.fail("unsupported opened service")
        )
        == 2
    )
    assert (
        json.loads(capsys.readouterr().err)["classification"]
        == "pce_command_unsupported"
    )


@pytest.mark.parametrize("action", ["init", "update", "query", "status"])
def test_shared_dispatcher_operations(action, capsys):
    calls = []

    def factory(**options):
        calls.append(options)
        return PceTimeSeriesService(InMemoryTimeSeriesStore(), source())

    argv = [action] + (
        [] if action == "status" else ["--start", "2026-09", "--end", "2026-09"]
    )
    assert main(argv, service_factory=factory) == 0
    assert calls[0]["profile"] is PCE_PROFILE
    result = json.loads(capsys.readouterr().out)
    if action != "status":
        assert result["complete"]


def test_editable_240_month_default_and_cross_profile_rejection(capsys):
    args = build_parser().parse_args(["init"])
    year, month = map(int, args.history_start.split("-"))
    from market_data_foundation.historical.pce_timeseries_cli import (
        previous_calendar_month,
    )

    end_year, end_month = map(int, previous_calendar_month().split("-"))
    assert end_year * 12 + end_month - year * 12 - month == 239
    assert (
        main(
            ["query", "--start", "2026-09", "--end", "2026-09", "--series", "WPSFD4"],
            service_factory=lambda **kw: pytest.fail("bad series opened service"),
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["classification"] == "unsupported_series"


def test_pce_package_credentials_select_bea_only(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from market_data_foundation.historical.cpi_timeseries_cli import (
        package_credential_loader,
    )
    from market_data_foundation.web import package_store, packages

    record = SimpleNamespace(enabled_capability_ids=("pce",))
    store = SimpleNamespace(read=lambda *args: record)
    monkeypatch.setattr(package_store, "LocalPackageStore", lambda *args: store)
    calls = []

    def host(store, *args, **options):
        def load(package, source, **kwargs):
            calls.append(source)
            return "synthetic-bea-value"

        return SimpleNamespace(store=store, load_credential=load)

    monkeypatch.setattr(packages, "PackageHost", host)
    loader = package_credential_loader(tmp_path, "synthetic", profile=PCE_PROFILE)
    assert loader() == "synthetic-bea-value" and calls == ["BEA"]


def test_shared_cli_entry_routes_to_pce_without_bls_scheduler(capsys):
    from market_data_foundation.historical.cpi_timeseries_cli import main as shared_main

    assert (
        shared_main(
            ["status"],
            profile=PCE_PROFILE,
            service_factory=lambda **kw: PceTimeSeriesService(
                InMemoryTimeSeriesStore(), source()
            ),
            scheduler_factory=lambda *a, **kw: pytest.fail("PCE invoked BLS scheduler"),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["schema"] == PCE_PROFILE.schema


@pytest.mark.parametrize(
    "now,expected",
    [
        (datetime(2026, 10, 30, tzinfo=UTC), "2026-09"),
        (datetime(2026, 12, 31, 16, tzinfo=UTC), "2026-12"),
        (datetime(2026, 1, 1, tzinfo=UTC), "2025-12"),
    ],
)
def test_previous_calendar_month_uses_beijing(now, expected):
    from market_data_foundation.historical.pce_timeseries_cli import (
        previous_calendar_month,
    )

    assert previous_calendar_month(now) == expected


def test_init_default_end_and_explicit_override(monkeypatch, capsys):
    from market_data_foundation.historical import pce_timeseries_cli as cli

    monkeypatch.setattr(cli, "previous_calendar_month", lambda: "2026-09")
    calls = []
    monkeypatch.setattr(
        cli, "execute", lambda service, action, params: calls.append(params) or {}
    )

    def factory(**kw):
        return PceTimeSeriesService(InMemoryTimeSeriesStore(), source())

    assert main(["init"], service_factory=factory) == 0
    assert calls[-1]["end"] == "2026-09" and calls[-1]["start"] == "2006-10"
    assert main(["init", "--end", "2026-08"], service_factory=factory) == 0
    assert calls[-1]["end"] == "2026-08"
    capsys.readouterr()


@pytest.mark.parametrize("enabled", [True, False])
def test_inherited_bea_secret_still_requires_enabled_pce(tmp_path, enabled):
    from market_data_foundation.historical.cpi_timeseries_cli import (
        package_credential_loader,
    )
    from market_data_foundation.web.package_catalog import PackageCatalog
    from market_data_foundation.web.package_store import (
        ConfigRecord,
        LocalPackageStore,
        Snapshot,
    )

    directory = tmp_path / "packages"
    store = LocalPackageStore.initialize(directory, PackageCatalog())
    store._commit(
        Snapshot(
            records=[
                ConfigRecord(
                    environment_id="synthetic",
                    package_id="macro",
                    revision="r1",
                    enabled_capability_ids=["pce"] if enabled else ["cpi"],
                )
            ]
        )
    )
    secret = tmp_path / "secret.toml"
    secret.write_text(
        '[bea]\napi_key = "synthetic-bea-key"\n'
        '[bls]\nregistration_key = "wrong-provider"\n'
    )
    loader = package_credential_loader(
        directory, "synthetic", secret_path=secret, profile=PCE_PROFILE
    )
    if enabled:
        assert loader() == "synthetic-bea-key"
    else:
        with pytest.raises(InterfaceError, match="pce_capability_disabled"):
            loader()


@pytest.mark.parametrize("explicit_ref", [True, False])
def test_bea_credential_failure_never_falls_back_to_inherited_or_bls(
    tmp_path, explicit_ref
):
    from market_data_foundation.historical.cpi_timeseries_cli import (
        package_credential_loader,
    )
    from market_data_foundation.web.package_catalog import PackageCatalog, PackageError
    from market_data_foundation.web.package_store import (
        ConfigRecord,
        LocalPackageStore,
        Snapshot,
    )

    directory = tmp_path / "packages"
    store = LocalPackageStore.initialize(directory, PackageCatalog())
    store._commit(
        Snapshot(
            records=[
                ConfigRecord(
                    environment_id="synthetic",
                    package_id="macro",
                    revision="r1",
                    enabled_capability_ids=["pce"],
                    credential_refs={"BEA": "a" * 32} if explicit_ref else {},
                )
            ]
        )
    )
    secret = tmp_path / "secret.toml"
    secret.write_text(
        '[bls]\nregistration_key = "wrong-provider"\n'
        + ('[bea]\napi_key = "valid-inherited-bea-key"\n' if explicit_ref else "")
    )
    loader = package_credential_loader(
        directory, "synthetic", secret_path=secret, profile=PCE_PROFILE
    )
    with pytest.raises(PackageError, match="credential_storage_unavailable"):
        loader()


def test_existing_trusted_bea_loader_remains_supported(tmp_path):
    from market_data_foundation.historical.cpi_timeseries_cli import (
        package_credential_loader,
    )
    from market_data_foundation.web.package_catalog import PackageCatalog
    from market_data_foundation.web.package_store import (
        ConfigRecord,
        LocalPackageStore,
        Snapshot,
    )

    directory = tmp_path / "packages"
    store = LocalPackageStore.initialize(directory, PackageCatalog())
    store._commit(
        Snapshot(
            records=[
                ConfigRecord(
                    environment_id="synthetic",
                    package_id="macro",
                    revision="r1",
                    enabled_capability_ids=["pce"],
                )
            ]
        )
    )
    calls = []

    def trusted(*args):
        calls.append(args)
        return "synthetic-trusted-bea-key"

    loader = package_credential_loader(
        directory, "synthetic", profile=PCE_PROFILE, credential_loader=trusted
    )
    assert loader() == "synthetic-trusted-bea-key"
    assert calls == [("synthetic", "macro", "BEA")]


@pytest.mark.parametrize("action", ["status", "query"])
@pytest.mark.parametrize("override", [False, True])
def test_production_control_binding_is_cwd_independent_and_local_reads_are_pure(
    tmp_path, monkeypatch, capsys, action, override
):
    from market_data_foundation.historical import pce_timeseries as pce
    from market_data_foundation.historical import pce_timeseries_cli as cli
    from market_data_foundation.historical.pce_timeseries_source import BeaRangeSource

    runtime = tmp_path / "config" / "runtime.toml"
    explicit = tmp_path / "shared" / "bea-control.json"
    expected = (
        explicit if override else runtime.parent / "bea-timeseries-request-control.json"
    )
    paths = []

    def forbidden(*args, **kwargs):
        pytest.fail("local read touched credentials or upstream")

    monkeypatch.setattr(cli, "package_credential_loader", forbidden)

    def build(**options):
        paths.append(options["request_control_path"])
        upstream = BeaRangeSource(
            credential_loader=options["credential_loader"],
            request_control_path=options["request_control_path"],
        )
        monkeypatch.setattr(upstream, "fetch", forbidden)
        return PceTimeSeriesService(InMemoryTimeSeriesStore(), upstream)

    monkeypatch.setattr(pce, "build_service", build)
    for name in ("cwd-one", "cwd-two"):
        cwd = tmp_path / name
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        argv = ["--runtime", str(runtime)]
        if override:
            argv += ["--bea-request-control-path", str(explicit)]
        argv += [action]
        if action == "query":
            argv += ["--start", "2026-09", "--end", "2026-09", "--local-only"]
        assert main(argv) == 0
        assert json.loads(capsys.readouterr().out)
        assert list(cwd.iterdir()) == []
    assert paths == [expected, expected]
    assert expected.is_absolute()
    assert not runtime.parent.exists() and not explicit.parent.exists()


@pytest.mark.parametrize(
    "path", ["relative.json", "../control.json", "/tmp/a/../control.json"]
)
def test_invalid_control_override_rejected_before_service(path, capsys):
    assert (
        main(
            ["--bea-request-control-path", path, "status"],
            service_factory=lambda **kw: pytest.fail("invalid path opened service"),
        )
        == 2
    )
    result = json.loads(capsys.readouterr().err)
    assert result["classification"] == "bea_absolute_request_control_path_required"
    assert result["provider_attempts"] == 0


def test_runtime_file_symlink_keeps_config_directory_control_path(
    tmp_path, monkeypatch, capsys
):
    from market_data_foundation.historical import pce_timeseries as pce

    config = tmp_path / "config"
    config.mkdir()
    runtime = config / "runtime.toml"
    runtime.symlink_to(tmp_path / "elsewhere" / "runtime.toml")
    paths = []

    def build(**options):
        paths.append(options["request_control_path"])
        return PceTimeSeriesService(InMemoryTimeSeriesStore(), source())

    monkeypatch.setattr(pce, "build_service", build)
    assert main(["--runtime", str(runtime), "status"]) == 0
    assert paths == [config / "bea-timeseries-request-control.json"]
    capsys.readouterr()


def test_legacy_strict_service_factory_signature_is_preserved(capsys):
    def factory(*, runtime_path, secret_path, profile):
        assert profile is PCE_PROFILE
        return PceTimeSeriesService(InMemoryTimeSeriesStore(), source())

    assert main(["status"], service_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == PCE_PROFILE.schema


def test_cli_obeys_other_source_429_cooldown_across_cwd(tmp_path, monkeypatch, capsys):
    from market_data_foundation.historical import pce_timeseries as pce
    from market_data_foundation.historical import pce_timeseries_cli as cli
    from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
    from market_data_foundation.historical.pce_timeseries_source import (
        BeaRangeSource,
        PceSourceError,
    )

    now = datetime(2026, 10, 30, tzinfo=UTC)
    runtime = tmp_path / "config" / "runtime.toml"
    shared_path = runtime.parent / "bea-timeseries-request-control.json"

    class Throttled:
        calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            return HttpResult(429, b"", {"Retry-After": "600"})

    wire = Throttled()

    def make_source(path, loader):
        return BeaRangeSource(
            credential_loader=loader,
            request_control_path=path,
            transport=wire,
            clock=lambda: now,
            sleep=lambda _: pytest.fail("cooldown should not sleep"),
            max_transport_attempts=1,
        )

    # Model the Web/worker's config-directory binding with an independent source.
    other = make_source(shared_path, lambda: "synthetic-bea-key")
    with pytest.raises(PceSourceError) as throttled:
        other.fetch(("DPCERG",), "2026-09", "2026-09")
    assert throttled.value.attempts == 1

    monkeypatch.setattr(
        cli, "package_credential_loader", lambda *a, **kw: lambda: "synthetic-bea-key"
    )

    def build(**options):
        return PceTimeSeriesService(
            InMemoryTimeSeriesStore(),
            make_source(options["request_control_path"], options["credential_loader"]),
        )

    monkeypatch.setattr(pce, "build_service", build)
    cwd = tmp_path / "unrelated-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    assert (
        main(
            [
                "--runtime",
                str(runtime),
                "update",
                "--start",
                "2026-09",
                "--end",
                "2026-09",
            ]
        )
        == 2
    )
    result = json.loads(capsys.readouterr().err)
    assert result["classification"] == "pce_timeseries_request_cooldown"
    assert result["provider_attempts"] == 0
    assert wire.calls == 1
    assert list(cwd.iterdir()) == []
