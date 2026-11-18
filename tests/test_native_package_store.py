"""A new TDX opt-in must not make the running legacy reader lose all packages."""

from dataclasses import replace
from unittest.mock import patch

import pytest

from market_data_foundation.web.native_package_store import NativePackageStore
from market_data_foundation.web.package_catalog import (
    PackageCatalog,
    PackageError,
    SaveCommand,
)
from market_data_foundation.web.package_store import LocalPackageStore


def command(ids, revision=None):
    return SaveCommand(expected_revision=revision, enabled_capability_ids=ids)


def stores(tmp_path):
    catalog = PackageCatalog()
    legacy_catalog = PackageCatalog()
    legacy_catalog.packages["tdx"] = replace(
        catalog.package("tdx"),
        capabilities=tuple(
            c
            for c in catalog.package("tdx").capabilities
            if c.capability_id == "daily_raw"
        ),
    )
    legacy = LocalPackageStore.initialize(tmp_path / "configuration", legacy_catalog)
    modern = NativePackageStore(legacy.root, catalog)
    return legacy, modern


def test_old_reader_and_other_packages_secrets_survive_native_roundtrip(tmp_path):
    old, new = stores(tmp_path)
    old.save(
        "dev",
        "macro",
        SaveCommand.model_validate(
            dict(
                expected_revision=None,
                enabled_capability_ids=["claims"],
                source_changes=[
                    dict(
                        source_id="FRED",
                        credential_action="replace",
                        credential_value="isolated-dummy-value",
                    )
                ],
            )
        ),
        allow_replace=True,
    )
    old.save("dev", "tdx", command(["daily_raw"]))
    before = old.path.read_bytes()
    macro_before = old.read("dev", "macro")
    saved = new.save(
        "dev", "tdx", command(["daily_raw", "daily_native", "stock_roster"], "r1")
    )
    assert set(saved.enabled_capability_ids) == {
        "daily_raw",
        "daily_native",
        "stock_roster",
    }
    assert old.path.read_bytes() == before
    assert old.read("dev", "tdx").enabled_capability_ids == ["daily_raw"]
    assert old.read("dev", "macro") == macro_before
    assert (
        old.resolve_credential(old.read("dev", "macro"), "FRED")
        == "isolated-dummy-value"
    )
    reopened = NativePackageStore(old.root, new.catalog)
    assert reopened.read("dev", "tdx") == saved
    # A normal Macro save still uses the one legacy snapshot and preserves extension.
    new.save("dev", "macro", command(["claims", "cpi"], macro_before.revision))
    assert new.read("dev", "tdx") == saved
    assert old.read("dev", "macro").enabled_capability_ids == ["claims", "cpi"]
    assert (
        old.resolve_credential(old.read("dev", "macro"), "FRED")
        == "isolated-dummy-value"
    )
    assert new.save(
        "dev", "tdx", command(["daily_raw"], saved.revision)
    ).enabled_capability_ids == ["daily_raw"]


def test_legacy_change_invalidates_modern_cas_and_cannot_be_overwritten(tmp_path):
    old, new = stores(tmp_path)
    first = new.save("dev", "tdx", command(["daily_native"]))
    assert old.read("dev", "tdx") is None
    old.save("dev", "tdx", command(["daily_raw"]))
    with pytest.raises(PackageError, match="config_revision_conflict"):
        new.save("dev", "tdx", command(["daily_native"], first.revision))
    current = new.read("dev", "tdx")
    with pytest.raises(
        PackageError, match="legacy_capability_managed_by_reference_host"
    ):
        new.save("dev", "tdx", command(["daily_native"], current.revision))
    assert new.read("dev", "tdx") == current
    assert set(current.enabled_capability_ids) == {"daily_raw", "daily_native"}


def test_extension_commit_failure_cannot_enable_capability_or_damage_primary(tmp_path):
    old, new = stores(tmp_path)
    before = old.path.read_bytes()
    with patch(
        "market_data_foundation.web.package_store.os.replace", side_effect=OSError
    ):
        with pytest.raises(PackageError, match="config_save_failed"):
            new.save("dev", "tdx", command(["daily_native"]))
    assert new.read("dev", "tdx") is None
    assert old.path.read_bytes() == before
    assert old.snapshot().records == []


@pytest.mark.parametrize("composition", [True, False])
def test_real_unified_host_http_save_keeps_legacy_snapshot_readable(
    tmp_path, monkeypatch, composition
):
    from fastapi.testclient import TestClient

    from market_data_foundation.web.cpi_timeseries_host import create_timeseries_preview
    from market_data_foundation.web.tdx_daily_api import DailyRuntime, TdxOptions
    from tests import test_readonly_host as old_fixture
    from tests.test_tdx_p0 import Store
    from tests.test_unified_dev_operations import handler_for, request

    _, previous, *_ = old_fixture.setup(
        tmp_path, monkeypatch, enabled_caps=("cpi",), package_config_save=True
    )
    tdx = DailyRuntime(
        TdxOptions(tmp_path / "tdx", tmp_path / "unused-driver", "unused:1"),
        store=Store(),
    )
    app = create_timeseries_preview(
        previous.settings,
        enabled=True,
        connector=previous.connector,
        cpi_access=old_fixture.ALLOW,
        pce_access=old_fixture.ALLOW,
        clock=previous.clock,
        schedule_path=tmp_path / "schedule.json",
        job_directory=tmp_path / "jobs",
        tdx_runtime=tdx,
        dev_origin="http://192.168.10.20:18766",
        allow_unified_dev_macro_operations=composition,
    )
    host = app.state.timeseries_runtime.packages
    before = (host.store.root / "packages.json").read_bytes()
    with TestClient(
        app, base_url="http://192.168.10.20:18766", client=("192.168.10.25", 1234)
    ) as client:
        path = "/api/r5/packages/tdx"
        if not composition:
            assert client.get(path).json()["write_eligible"] is False
            assert (
                client.put(
                    path,
                    json=dict(
                        expected_revision=None,
                        enabled_capability_ids=["daily_native"],
                        source_changes=[],
                    ),
                    headers={"Origin": "http://192.168.10.20:18766"},
                ).status_code
                == 405
            )
            assert (host.store.root / "packages.json").read_bytes() == before
            return
        assert client.get(path).json()["write_eligible"] is True
        assert handler_for(monkeypatch, app).request_admitted(
            request(path, method="PUT")
        )
        saved = client.put(
            path,
            json=dict(
                expected_revision=None,
                enabled_capability_ids=["daily_native", "stock_roster"],
                source_changes=[{"source_id": "TDX", "credential_action": "keep"}],
            ),
            headers={"Origin": "http://192.168.10.20:18766"},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["configuration_readonly_capability_ids"] == []
        assert {c["capability_id"] for c in saved.json()["capabilities"]} == {
            "daily_native",
            "stock_roster",
        }
        assert set(client.get(path).json()["enabled_capability_ids"]) == {
            "daily_native",
            "stock_roster",
        }
        assert (host.store.root / "packages.json").read_bytes() == before
        assert client.get("/api/r5/packages/macro").json()[
            "enabled_capability_ids"
        ] == ["cpi"]


def test_native_browser_keep_does_not_allow_credential_replacement(tmp_path):
    old, new = stores(tmp_path)
    first = new.save(
        "dev",
        "tdx",
        SaveCommand.model_validate(
            dict(
                expected_revision=None,
                enabled_capability_ids=["daily_native"],
                source_changes=[dict(source_id="TDX", credential_action="keep")],
            )
        ),
    )
    before = old.path.read_bytes()
    with pytest.raises(PackageError, match="source_credentials_not_applicable"):
        new.save(
            "dev",
            "tdx",
            SaveCommand.model_validate(
                dict(
                    expected_revision=first.revision,
                    enabled_capability_ids=["daily_native"],
                    source_changes=[
                        dict(
                            source_id="TDX",
                            credential_action="replace",
                            credential_value="isolated-dummy-value",
                        )
                    ],
                )
            ),
        )
    assert old.path.read_bytes() == before
    assert new.read("dev", "tdx") == first
