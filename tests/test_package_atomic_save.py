"""Atomic package/credential behavior against real POSIX store and ASGI routes."""

import json
import os
import socket
import stat
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import Mock

import psycopg
import pytest
from fastapi.testclient import TestClient

from market_data_foundation.web.package_catalog import PackageError, SaveCommand
from market_data_foundation.web.package_store import LocalPackageStore
from market_data_foundation.web.packages import PackageHost, create_package_app
from tests.d01_package_fixture import fixture_host

PATH = "/api/r5/packages/macro"
FIRST = "synthetic-bls-first"
SECOND = "synthetic-bls-second"
BEA = "synthetic-bea-value"


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external_io_forbidden")

    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


def replacement(source="BLS", value=FIRST):
    return {
        "source_id": source,
        "credential_action": "replace",
        "credential_value": value,
    }


def command(caps=None, revision=None, changes=None):
    return {
        "expected_revision": revision,
        "enabled_capability_ids": caps or ["cpi"],
        "source_changes": [replacement()] if changes is None else changes,
    }


def submit(host, payload):
    with TestClient(create_package_app(host)) as client:
        return client.put(PATH, json=payload)


def seed(tmp_path):
    host = fixture_host(tmp_path / "configuration")
    response = submit(host, command())
    assert response.status_code == 200, response.text
    return host


def record(host):
    return host.store.read(host.environment_id, "macro")


def assert_redacted(response):
    for value in (
        FIRST,
        SECOND,
        BEA,
        "credential_refs",
        "credential_value",
        "/credentials/",
    ):
        assert value not in response.text


def test_one_snapshot_publishes_capabilities_and_private_reference_and_reopens(
    tmp_path,
):
    host = fixture_host(tmp_path / "configuration")
    response = submit(
        host, command(["cpi", "pce"], changes=[replacement(), replacement("BEA", BEA)])
    )
    assert response.status_code == 200, response.text
    assert response.json()["config_revision"] == "r1"
    assert response.json()["enabled_capability_ids"] == ["cpi", "pce"]
    assert_redacted(response)
    saved = record(host)
    snapshot = host.store.path.read_text()
    assert FIRST not in snapshot and BEA not in snapshot
    assert set(saved.credential_refs) == {"BLS", "BEA"}
    assert host.load_credential("macro", "BLS", record=saved) == FIRST
    assert host.load_credential("macro", "BEA", record=saved) == BEA
    for ref in saved.credential_refs.values():
        assert (
            stat.S_IMODE((host.store.root / "credentials" / ref).stat().st_mode)
            == 0o600
        )
        assert ref not in response.text
    assert stat.S_IMODE((host.store.root / "credentials").stat().st_mode) == 0o700
    worker = fixture_host(host.store.root, initialize=False)
    worker.credentials = Mock(side_effect=RuntimeError("legacy_unavailable"))
    assert worker.load_credential("macro", "BLS") == FIRST
    assert worker.load_credential("macro", "BEA") == BEA
    with TestClient(create_package_app(worker)) as client:
        response = client.get(PATH)
        assert response.status_code == 200
        states = {
            s["source_id"]: s["credential_state"]
            for s in response.json()["credential_states"]
        }
        assert states == {
            "BLS": "configured",
            "BEA": "configured",
            "Census": "unknown",
            "FRED": "unknown",
        }
        assert all(
            s["authentication"] == "unknown"
            for s in response.json()["credential_states"]
        )
        assert_redacted(response)
    worker.credentials.assert_not_called()


def test_keep_reuses_trusted_existing_loader_without_copy_or_new_secret_file(tmp_path):
    host = fixture_host(tmp_path / "configuration")
    host.credential_loader = Mock(return_value=FIRST)
    response = submit(
        host, command(changes=[{"source_id": "BLS", "credential_action": "keep"}])
    )
    assert response.status_code == 200
    assert host.load_credential("macro", "BLS") == FIRST
    host.credential_loader.assert_called_once_with(host.environment_id, "macro", "BLS")
    assert record(host).credential_refs == {}
    assert not (host.store.root / "credentials").exists()
    assert_redacted(response)


def test_unassembled_host_rejects_credential_writes_and_does_not_announce_support(
    tmp_path,
):
    original = fixture_host(tmp_path / "configuration")
    host = PackageHost(
        original.store,
        original.environment_id,
        authorize=original.authorize,
        credentials=original.credentials,
    )
    before = host.store.path.read_bytes()
    response = submit(host, command())
    assert response.status_code == 422
    assert response.json()["classification"] == "credential_replace_not_supported"
    assert response.json()["effect"] == "NONE"
    assert host.store.path.read_bytes() == before
    with TestClient(create_package_app(host)) as client:
        assert not any(
            s["replace_supported"] for s in client.get(PATH).json()["credential_states"]
        )
    assert not (host.store.root / "credentials").exists()


@pytest.mark.parametrize(
    "changes",
    [
        [replacement("BEA", BEA)],
        [replacement("TDX")],
        [replacement(), replacement()],
        [replacement(value="")],
        [replacement(value="   ")],
        [replacement(value="a\nb")],
        [replacement(value="x" * 4097)],
        [{"source_id": "BLS", "credential_action": "keep", "credential_value": FIRST}],
        [replacement() | {"credential_ref": "../../private"}],
        [replacement() | {"credential_path": "/etc/passwd"}],
    ],
)
def test_illegal_commands_never_change_configuration_or_echo_input(tmp_path, changes):
    host = fixture_host(tmp_path / "configuration")
    before = host.store.path.read_bytes()
    response = submit(host, command(changes=changes))
    assert response.status_code == 422
    assert response.json()["effect"] == "NONE"
    assert host.store.path.read_bytes() == before
    assert not (host.store.root / "credentials").exists()
    assert_redacted(response)


@pytest.mark.parametrize("field", ["environment_id", "store_dir", "credential_refs"])
def test_browser_cannot_choose_environment_store_or_reference(tmp_path, field):
    host = fixture_host(tmp_path / "configuration")
    response = submit(host, command() | {field: FIRST})
    assert response.status_code == 422
    assert record(host) is None
    assert_redacted(response)


def test_replace_retains_old_value_and_disabled_capability_history(
    tmp_path,
):
    host = seed(tmp_path)
    first = record(host)
    response = submit(host, command(["ppi"], "r1", [replacement(value=SECOND)]))
    assert response.status_code == 200
    second = record(host)
    assert first.credential_refs != second.credential_refs
    assert host.load_credential("macro", "BLS", record=first) == FIRST
    assert host.load_credential("macro", "BLS", record=second) == SECOND
    response = submit(
        host,
        {"expected_revision": "r2", "enabled_capability_ids": [], "source_changes": []},
    )
    assert response.status_code == 200
    assert record(host).credential_refs == second.credential_refs
    assert response.json()["assets"][0]["revisions"][0]["visibility_basis"] == [
        "RETAINED_HISTORY"
    ]
    assert host.load_credential("macro", "BLS") == SECOND
    assert submit(host, command(["cpi"], "r3", [])).status_code == 200
    assert record(host).credential_refs == second.credential_refs
    assert len(list((host.store.root / "credentials").iterdir())) == 2


def test_environment_and_package_isolation_includes_private_references(tmp_path):
    host = seed(tmp_path)
    other = PackageHost(
        host.store,
        "other-environment",
        authorize=host.authorize,
        credential_replace_enabled=True,
    )
    assert other.load_credential("macro", "BLS") is None
    with pytest.raises(PackageError, match="credential_context_mismatch"):
        other.load_credential("macro", "BLS", record=record(host))
    with pytest.raises(PackageError, match="illegal_source_change"):
        host.load_credential("tdx", "BLS")
    assert (
        submit(other, command(changes=[replacement(value=SECOND)])).status_code == 200
    )
    assert other.load_credential("macro", "BLS") == SECOND
    assert host.load_credential("macro", "BLS") == FIRST


def test_failed_second_credential_preparation_cannot_publish_first_or_capabilities(
    tmp_path, monkeypatch
):
    host = seed(tmp_path)
    old = record(host)
    before = host.store.path.read_bytes()
    original = host.store._stage_credential
    calls = []

    def prepare(secret):
        calls.append(1)
        if len(calls) == 2:
            raise PackageError("credential_prepare_failed")
        return original(secret)

    monkeypatch.setattr(host.store, "_stage_credential", prepare)
    response = submit(
        host,
        command(
            ["cpi", "pce"], "r1", [replacement(value=SECOND), replacement("BEA", BEA)]
        ),
    )
    assert response.status_code == 503 and response.json()["effect"] == "NONE"
    assert host.store.path.read_bytes() == before
    assert record(host) == old
    assert host.load_credential("macro", "BLS") == FIRST
    assert host.load_credential("macro", "BEA") is None
    assert_redacted(response)


def test_pre_replace_failure_keeps_old_pair_even_after_new_file_is_prepared(
    tmp_path, monkeypatch
):
    host = seed(tmp_path)
    old = record(host)
    before = host.store.path.read_bytes()

    def fail(*args):
        raise OSError(SECOND)

    monkeypatch.setattr("market_data_foundation.web.package_store.os.replace", fail)
    response = submit(host, command(["ppi"], "r1", [replacement(value=SECOND)]))
    assert response.status_code == 503
    assert response.json() == {
        "status": "UNAVAILABLE",
        "classification": "config_save_failed",
        "effect": "NONE",
    }
    assert host.store.path.read_bytes() == before
    assert record(host) == old and host.load_credential("macro", "BLS") == FIRST
    assert_redacted(response)


def test_post_publish_fsync_failure_is_unknown_and_old_cas_cannot_replay(
    tmp_path, monkeypatch
):
    host = seed(tmp_path)
    original_replace, original_fsync = os.replace, os.fsync
    published = False

    def publish(*args, **kwargs):
        nonlocal published
        original_replace(*args, **kwargs)
        published = True

    def fsync(fd):
        if published:
            raise OSError(SECOND)
        return original_fsync(fd)

    monkeypatch.setattr("market_data_foundation.web.package_store.os.replace", publish)
    monkeypatch.setattr("market_data_foundation.web.package_store.os.fsync", fsync)
    payload = command(["ppi"], "r1", [replacement(value=SECOND)])
    response = submit(host, payload)
    assert response.status_code == 503
    assert response.json() == {
        "status": "UNKNOWN",
        "classification": "config_commit_outcome_unknown",
        "effect": "UNKNOWN",
    }
    assert record(host).revision == "r2" and record(host).enabled_capability_ids == [
        "ppi"
    ]
    assert host.load_credential("macro", "BLS") == SECOND
    count = len(list((host.store.root / "credentials").iterdir()))
    replay = submit(host, payload)
    assert replay.status_code == 409 and replay.json()["effect"] == "NONE"
    assert len(list((host.store.root / "credentials").iterdir())) == count
    assert_redacted(response)


def test_prepared_reference_is_invisible_until_single_snapshot_publication(
    tmp_path, monkeypatch
):
    host = seed(tmp_path)
    old = record(host)
    prepared, release, reader_started = Event(), Event(), Event()
    original = host.store._commit

    def commit(snapshot):
        prepared.set()
        assert release.wait(10)
        return original(snapshot)

    def read_other():
        reader_started.set()
        other = LocalPackageStore(host.store.root, host.catalog)
        current = other.read(host.environment_id, "macro")
        return current, other.resolve_credential(current, "BLS")

    monkeypatch.setattr(host.store, "_commit", commit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        write = pool.submit(
            submit, host, command(["ppi"], "r1", [replacement(value=SECOND)])
        )
        try:
            assert prepared.wait(10)
            persisted = json.loads(host.store.path.read_text())["records"][0]
            assert persisted["enabled_capability_ids"] == ["cpi"]
            assert persisted["credential_refs"] == old.credential_refs
            assert host.store.resolve_credential(old, "BLS") == FIRST
            read = pool.submit(read_other)
            assert reader_started.wait(10)
            assert not read.done()
        finally:
            release.set()
        assert write.result(timeout=10).status_code == 200
        current, value = read.result(timeout=10)
        assert current.enabled_capability_ids == ["ppi"] and value == SECOND


def test_independent_writers_cas_winner_publishes_its_whole_pair_only(tmp_path):
    first = fixture_host(tmp_path / "configuration")
    second = fixture_host(first.store.root, initialize=False)
    barrier = Barrier(2)

    def write(host, cap, secret):
        barrier.wait(5)
        try:
            host.store.save(
                host.environment_id,
                "macro",
                SaveCommand(**command([cap], changes=[replacement(value=secret)])),
                allow_replace=True,
            )
            return cap, secret, "won"
        except PackageError as error:
            return cap, secret, error.failure.classification

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(write, first, "cpi", FIRST)
        b = pool.submit(write, second, "ppi", SECOND)
        results = [a.result(timeout=10), b.result(timeout=10)]
    winner = next(r for r in results if r[2] == "won")
    assert sorted(r[2] for r in results) == ["config_revision_conflict", "won"]
    assert record(first).enabled_capability_ids == [winner[0]]
    assert first.load_credential("macro", "BLS") == winner[1]
    assert len(list((first.store.root / "credentials").iterdir())) == 1


@pytest.mark.parametrize(
    "damage", ["missing", "symlink", "public_file", "public_directory", "empty", "fifo"]
)
def test_current_credential_failure_never_falls_back_to_old_loader(tmp_path, damage):
    host = seed(tmp_path)
    host.credential_loader = Mock(return_value="stale-synthetic-credential")
    p = host.store.root / "credentials" / record(host).credential_refs["BLS"]
    if damage in ("missing", "symlink", "fifo"):
        p.unlink()
        if damage == "symlink":
            target = tmp_path / "outside-secret"
            target.write_text(SECOND)
            p.symlink_to(target)
        elif damage == "fifo":
            os.mkfifo(p, mode=0o600)
    elif damage == "public_file":
        p.chmod(0o644)
    elif damage == "public_directory":
        p.parent.chmod(0o755)
    else:
        p.write_text("")
    with pytest.raises(PackageError, match="credential_storage_unavailable"):
        host.load_credential("macro", "BLS")
    states = host.credential_states("macro", record(host), source_ids={"BLS"})
    assert states[0].credential_state == "unknown"
    host.credential_loader.assert_not_called()


def test_credential_prepare_requires_private_root_and_does_not_chmod_existing_host(
    tmp_path,
):
    host = fixture_host(tmp_path / "configuration")
    host.store.root.chmod(0o755)
    response = submit(host, command())
    assert response.status_code == 503 and response.json()["effect"] == "NONE"
    assert record(host) is None
    assert stat.S_IMODE(host.store.root.stat().st_mode) == 0o755


def test_permissions_checked_before_prepare_and_invalid_secret_not_logged(
    tmp_path, caplog
):
    host = fixture_host(tmp_path / "configuration", scenario="read_only")
    response = submit(host, command())
    assert response.status_code == 403 and record(host) is None
    assert not (host.store.root / "credentials").exists()
    assert_redacted(response)
    assert FIRST not in caplog.text


def test_invalid_utf8_secret_is_rejected_before_preparation(tmp_path):
    host = fixture_host(tmp_path / "configuration")
    payload = command(changes=[replacement(value="\ud800")])
    with TestClient(create_package_app(host)) as client:
        response = client.put(
            PATH,
            content=json.dumps(payload),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 422 and response.json()["effect"] == "NONE"
    assert record(host) is None
    assert not (host.store.root / "credentials").exists()


def test_cpi_query_uses_published_bls_reference_and_ignores_broken_unneeded_bea(
    tmp_path,
):
    from tests.d02_query_fixture import fixture

    app, state = fixture(tmp_path, "miss")
    host = state.host.packages
    response = submit(
        host, command(["cpi", "pce"], "r1", [replacement(), replacement("BEA", BEA)])
    )
    assert response.status_code == 200
    current = record(host)
    (host.store.root / "credentials" / current.credential_refs["BEA"]).unlink()
    host.credentials = Mock(side_effect=RuntimeError("inherited_loader_unavailable"))
    with TestClient(app) as client:
        response = client.get(
            "/api/r5/cpi/query",
            params={
                "reference": "2026-08",
                "operation_id": "mdf.source-candidate.bls-v2.cpi.direct@p4-direct-r1",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["channel"] == "upstream"
        assert state.source == 1
        (host.store.root / "credentials" / current.credential_refs["BLS"]).unlink()
        unavailable = client.get(
            "/api/r5/cpi/query",
            params={
                "reference": "2026-08",
                "operation_id": "mdf.source-candidate.bls-v2.cpi.direct@p4-direct-r1",
            },
        )
        assert unavailable.status_code == 409
        assert unavailable.json()["classification"] == "credential_unknown"
        assert state.source == 1
    host.credentials.assert_not_called()


def test_local_cpi_read_does_not_require_current_credentials(
    tmp_path,
):
    from tests.d02_query_fixture import fixture

    app, state = fixture(tmp_path, "local_hit")
    host = state.host.packages
    assert submit(host, command(revision="r1")).status_code == 200
    (host.store.root / "credentials" / record(host).credential_refs["BLS"]).unlink()
    with TestClient(app) as client:
        response = client.get("/api/r5/cpi/query", params={"reference": "2026-08"})
        assert response.status_code == 200 and response.json()["channel"] == "local"
        assert state.source == 0
