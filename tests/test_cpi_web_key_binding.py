"""Offline checks for the Web host's effective BLS credential binding."""

import json
from datetime import UTC, datetime

import pytest

from market_data_foundation.historical import cpi_timeseries_source as module
from market_data_foundation.historical.bls_cpi_direct_live import HttpResult

KEY = "b" * 32


class Transport:
    def __init__(self, echo=False):
        self.requests = []
        self.echo = echo

    def post(self, endpoint, body, headers, **kwargs):
        request = json.loads(body)
        self.requests.append(request)
        document = {
            "status": "REQUEST_SUCCEEDED",
            "Results": {
                "series": [
                    {
                        "seriesID": series,
                        "data": [
                            {
                                "year": "2026",
                                "period": "M09",
                                "value": "100",
                                "footnotes": [],
                            }
                        ],
                    }
                    for series in request["seriesid"]
                ]
            },
        }
        if self.echo:
            document["message"] = KEY
        return HttpResult(200, json.dumps(document).encode(), {})


@pytest.fixture(autouse=True)
def no_default_credential(monkeypatch):
    def reject(_path):
        raise AssertionError("explicit loader must not fall back to a secret file")

    monkeypatch.setattr(module, "load_registration_key", reject)


def source(loader, **kwargs):
    return module.BlsRangeSource(
        credential_loader=loader,
        clock=lambda: datetime(2026, 10, 29, tzinfo=UTC),
        **kwargs,
    )


def fetch(client):
    return client.fetch(module.SERIES_IDS, "2026-09", "2026-09")


def test_effective_key_is_loaded_only_on_fetch_and_excluded_from_evidence():
    calls = []
    transport = Transport()

    def load():
        calls.append(True)
        return KEY

    client = source(load, transport=transport)
    assert calls == []
    batches = fetch(client)
    assert len(calls) == 1
    assert transport.requests[0]["registrationkey"] == KEY
    assert "registrationkey" not in batches[0].request
    assert KEY.encode() not in batches[0].raw


def test_next_fetch_uses_current_effective_key():
    keys = iter([KEY, "c" * 32])
    transport = Transport()
    client = source(lambda: next(keys), transport=transport)
    fetch(client)
    fetch(client)
    assert [r["registrationkey"] for r in transport.requests] == [KEY, "c" * 32]


@pytest.mark.parametrize("value", [None, "", "x" * 31, "?" * 32, 123, True])
def test_invalid_effective_key_fails_before_wire_without_fallback(value):
    transport = Transport()
    with pytest.raises(module.CpiSourceError, match="bls_live_credential_invalid"):
        fetch(source(lambda: value, transport=transport))
    assert transport.requests == []


def test_loader_failure_does_not_expose_error_or_fall_back():
    transport = Transport()

    def load():
        raise RuntimeError(KEY)

    with pytest.raises(module.CpiSourceError) as error:
        fetch(source(load, transport=transport))
    assert error.value.classification == "bls_live_credential_invalid"
    assert error.value.attempts == 0
    assert KEY not in str(error.value)
    assert transport.requests == []


def test_invalid_range_precedes_credential_access():
    calls = []
    client = source(lambda: calls.append(True), transport=Transport())
    with pytest.raises(module.CpiSourceError, match="range_invalid"):
        client.fetch(module.SERIES_IDS, "2026-09", "2026-01")
    assert calls == []


def test_unregistered_source_never_loads_key():
    def load():
        raise AssertionError("unregistered source must not request a credential")

    transport = Transport()
    fetch(source(load, registered=False, transport=transport))
    assert "registrationkey" not in transport.requests[0]


def test_injected_key_echo_is_still_rejected():
    transport = Transport(echo=True)
    with pytest.raises(
        module.CpiSourceError, match="credential_echo_forbidden"
    ) as error:
        fetch(source(lambda: KEY, transport=transport))
    assert len(transport.requests) == 1
    assert KEY not in str(error.value)


@pytest.fixture(params=["web", "cli"])
def package_binding(request, tmp_path, monkeypatch):
    from market_data_foundation.historical.bls_cpi_direct_live import (
        load_registration_key,
    )
    from market_data_foundation.historical.cpi_timeseries_cli import (
        package_credential_loader,
    )
    from market_data_foundation.web.package_catalog import PackageCatalog, SaveCommand
    from market_data_foundation.web.package_store import LocalPackageStore
    from tests.test_cpi_timeseries_web_host import setup

    calls = []
    if request.param == "web":
        _, runtime, _, _, _, _, _ = setup(tmp_path, monkeypatch, allow_collect=True)
        store = runtime.packages.store
        environment = runtime.settings.environment_id
        secret = runtime.settings.config_directory / "secret.toml"
        original = runtime.packages.credential_loader
        load = runtime.timeseries.effective_credential
    else:
        store = LocalPackageStore.initialize(tmp_path / "packages", PackageCatalog())
        environment = "offline"
        store.save(
            environment,
            "macro",
            SaveCommand(expected_revision=None, enabled_capability_ids=["cpi"]),
        )
        secret = tmp_path / "secret.toml"

        def original(environment_id, package_id, source_id):
            assert (environment_id, package_id, source_id) == (
                environment,
                "macro",
                "BLS",
            )
            return load_registration_key(secret)

    secret.write_text(f'[bls]\napi_key="{KEY}"\n')

    def inherited(*args):
        calls.append(args)
        return original(*args)

    if request.param == "web":
        runtime.packages.credential_loader = inherited
    else:
        load = package_credential_loader(
            store.root,
            environment,
            secret_path=secret,
            credential_loader=inherited,
        )
    return load, store, environment, secret, calls


def replace_binding(store, environment, key):
    from market_data_foundation.web.package_catalog import SaveCommand

    current = store.read(environment, "macro")
    return store.save(
        environment,
        "macro",
        SaveCommand.model_validate(
            {
                "expected_revision": current.revision,
                "enabled_capability_ids": ["cpi"],
                "source_changes": [
                    {
                        "source_id": "BLS",
                        "credential_action": "replace",
                        "credential_value": key,
                    }
                ],
            }
        ),
        allow_replace=True,
    )


def test_package_without_ref_uses_current_inherited_key(package_binding):
    load, _, _, secret, calls = package_binding
    transport = Transport()
    client = source(load, transport=transport)
    fetch(client)
    secret.write_text(f'[bls]\napi_key="{"c" * 32}"\n')
    fetch(client)
    assert [item["registrationkey"] for item in transport.requests] == [KEY, "c" * 32]
    assert len(calls) == 2


def test_explicit_ref_precedes_inheritance_and_reloads_each_fetch(package_binding):
    load, store, environment, _, calls = package_binding
    replace_binding(store, environment, "d" * 32)
    transport = Transport()
    client = source(load, transport=transport)
    fetch(client)
    replace_binding(store, environment, "e" * 32)
    fetch(client)
    assert [item["registrationkey"] for item in transport.requests] == [
        "d" * 32,
        "e" * 32,
    ]
    assert calls == []


def test_broken_explicit_ref_never_uses_valid_inherited_key(package_binding):
    load, store, environment, _, calls = package_binding
    replace_binding(store, environment, "d" * 32)
    record = store.read(environment, "macro")
    (store.root / "credentials" / record.credential_refs["BLS"]).unlink()
    transport = Transport()
    with pytest.raises(module.CpiSourceError) as raised:
        fetch(source(load, transport=transport))
    assert raised.value.classification == "bls_live_credential_invalid"
    assert raised.value.attempts == 0
    assert calls == [] and transport.requests == []


@pytest.mark.parametrize("key", [None, "", "invalid"])
def test_missing_or_invalid_inherited_key_rejects_before_http(package_binding, key):
    load, _, _, secret, _ = package_binding
    secret.write_text("[bls]\n" if key is None else f'[bls]\napi_key="{key}"\n')
    transport = Transport()
    with pytest.raises(module.CpiSourceError) as raised:
        fetch(source(load, transport=transport))
    assert raised.value.attempts == 0 and transport.requests == []


def test_disabled_binding_rejects_valid_inherited_key_before_http(package_binding):
    from market_data_foundation.web.package_catalog import SaveCommand

    load, store, environment, _, calls = package_binding
    record = store.read(environment, "macro")
    store.save(
        environment,
        "macro",
        SaveCommand(expected_revision=record.revision, enabled_capability_ids=[]),
    )
    transport = Transport()
    with pytest.raises(module.CpiSourceError) as raised:
        fetch(source(load, transport=transport))
    assert raised.value.attempts == 0
    assert calls == [] and transport.requests == []


def test_cli_native_inherited_loader_uses_supplied_secret_path(tmp_path):
    from market_data_foundation.historical.cpi_timeseries_cli import (
        package_credential_loader,
    )
    from market_data_foundation.web.package_catalog import PackageCatalog, SaveCommand
    from market_data_foundation.web.package_store import LocalPackageStore

    store = LocalPackageStore.initialize(tmp_path / "packages", PackageCatalog())
    store.save(
        "offline",
        "macro",
        SaveCommand(expected_revision=None, enabled_capability_ids=["cpi"]),
    )
    secret = tmp_path / "custom-secret.toml"
    secret.write_text(f'[bls]\napi_key="{KEY}"\n')
    load = package_credential_loader(store.root, "offline", secret_path=secret)
    assert load() == KEY
    secret.write_text(f'[bls]\napi_key="{"c" * 32}"\n')
    assert load() == "c" * 32


def test_cli_missing_package_binding_rejects_before_http(tmp_path):
    from market_data_foundation.historical.cpi_timeseries_cli import (
        package_credential_loader,
    )

    calls = []
    load = package_credential_loader(
        tmp_path / "absent", "offline", credential_loader=lambda *_: calls.append(True)
    )
    transport = Transport()
    with pytest.raises(module.CpiSourceError) as raised:
        fetch(source(load, transport=transport))
    assert raised.value.attempts == 0
    assert calls == [] and transport.requests == []
