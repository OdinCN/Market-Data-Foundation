"""Hermetic transport contract tests; public synthetic CA, no network or secrets."""

import json
import os
import socket
import ssl
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import psycopg
import pytest

from market_data_foundation.storage.market_dataset_postgres import connect_dev
from market_data_foundation.storage.postgres_runtime_config import (
    PgConfigError,
    load_pg_config,
)

# Generated offline solely as a parser fixture; no private key is retained.
TEST_CA = """-----BEGIN CERTIFICATE-----
MIIDHTCCAgWgAwIBAgIUOTpUIADCJSaLuV1UtOWajRdq6gowDQYJKoZIhvcNAQEL
BQAwHjEcMBoGA1UEAwwTTURGIE9mZmxpbmUgVGVzdCBDQTAeFw0yNjEwMjQxMzQ4
NDBaFw0zNjEwMjExMzQ4NDBaMB4xHDAaBgNVBAMME01ERiBPZmZsaW5lIFRlc3Qg
Q0EwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQDBcn7raXMLaBzbwgUa
CILMK3PwWktdtfuR5X5wDclmUbGWulG9JBDOFt9DIsZM/yBag2pfOoMwb4gymcH0
YuWG3HP6yamMR4AoK9UtlnE4q6vNmmjvU1Sxspjxg9mQQPstImpa5mbRxw1K6POW
quY6GjbWCadGyEPBzuyhh2uTm+aEjoJIaZFn8nY0/0uBvLF/ZCi99UC3x2Lu5jk6
rQ+CUA8bFXimWoCsgttZqeBKO8KGioECBR7sy0M1lj5e72H/zdqdimsHb3hiBV52
hUtgKP5zJJ/u6xrLIDdbum0d/C6yDD/gtyOlGVaWBUDk4QOTuMgawzVm4C2JXXDA
VCEZAgMBAAGjUzBRMB0GA1UdDgQWBBR8s/f++O5H7St3IjvaJ5VcExAESzAfBgNV
HSMEGDAWgBR8s/f++O5H7St3IjvaJ5VcExAESzAPBgNVHRMBAf8EBTADAQH/MA0G
CSqGSIb3DQEBCwUAA4IBAQBwDmP2xRFwF4tXGlypepdmo0ijKG4avqLYHCOHz2yc
vTG6T/AzP9HMokau8Y16/XQnJQIL52qCltSPsG6E5werHcf73Wo9qUYDcuL2vseC
g5Zcd9aETaFlfkxsZYV+kqIS4nbzBtIntz5m7IQeSiuv6PperndSZUcVenGob4wU
dFMqc2+c0nIAzmTDmqmU3RosH3n2ZMvwX0tCbFD+C0+NQSjvBpzh0gIs3XojbL+J
KsVFR8u9J+LKZeoPn5NbZHShBiEnKxbV0shodqXBSqWHRNwydP1j9TU/wXw/0vuM
qxXl79ls2iMlQv8RwO/KxuB40t9FNBOqEoqL3Jpqd6kr
-----END CERTIFICATE-----
"""


def runtime_file(tmp_path, mode="require", *, root=None, **changes):
    pg = dict(
        host="192.168.10.4",
        port=5432,
        database="market_data_foundation_dev",
        user="market_data_foundation_dev",
        sslmode=mode,
    )
    if root is not None:
        pg["sslrootcert"] = str(root)
    pg.update(changes)
    path = tmp_path / "runtime.toml"
    path.write_text(
        "[postgres]\n"
        + "\n".join(f"{key} = {json.dumps(value)}" for key, value in pg.items())
        + "\n"
    )
    return path


def ca_file(tmp_path):
    ca = tmp_path / "chosen-ca.crt"
    ca.write_text(TEST_CA)
    return ca


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("network_forbidden")

    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    for name in os.environ:
        if name.startswith("PG"):
            monkeypatch.delenv(name)


@pytest.mark.parametrize(
    "mode,with_root",
    [("require", False), ("require", True), ("verify-ca", True), ("verify-full", True)],
)
def test_transport_options_and_immutable_profile(
    tmp_path, monkeypatch, mode, with_root
):
    ca = ca_file(tmp_path) if with_root else None
    path = runtime_file(tmp_path, mode, root=ca)
    profile = load_pg_config(path)
    with pytest.raises(FrozenInstanceError):
        profile.sslmode = "require"
    calls = []
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: calls.append(kwargs))
    connect_dev("synthetic", runtime_path=path)
    expected = dict(
        host="192.168.10.4",
        hostaddr="192.168.10.4",
        port=5432,
        dbname="market_data_foundation_dev",
        user="market_data_foundation_dev",
        sslmode=mode,
        gssencmode="disable",
        password="synthetic",
        connect_timeout=5,
        autocommit=True,
        options="-c statement_timeout=5000 -c lock_timeout=1000 -c timezone=UTC",
    )
    if ca:
        expected["sslrootcert"] = str(ca)
    assert calls == [expected]
    if not with_root:
        assert "sslrootcert" not in profile.connection_kwargs()


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
@pytest.mark.parametrize(
    "root",
    [
        "",
        " ",
        "relative.crt",
        "system",
        "traversal",
        "absent",
        "empty",
        "invalid",
        "directory",
        "unreadable",
        "broken_symlink",
        "loop_symlink",
        "directory_symlink",
        "invalid_symlink",
        "unreadable_symlink",
        "fifo_symlink",
        "nul",
        "control",
        "del",
        "whitespace",
    ],
)
def test_bad_trust_fails_before_connection(tmp_path, mode, root):
    if root in ("empty", "invalid", "unreadable"):
        ca = tmp_path / "bad.crt"
        ca.write_text(
            TEST_CA if root == "unreadable" else ("" if root == "empty" else "invalid")
        )
        if root == "unreadable":
            ca.chmod(0)
        root = str(ca)
    elif root == "absent":
        root = str(tmp_path / "absent.crt")
    elif root == "directory":
        root = str(tmp_path)
    elif root == "traversal":
        ca = ca_file(tmp_path)
        directory = tmp_path / "nested"
        directory.mkdir()
        root = str(directory / ".." / ca.name)
    elif root.endswith("_symlink"):
        ca = tmp_path / "target.crt"
        link = tmp_path / "link.crt"
        if root == "loop_symlink":
            ca.symlink_to(link)
        elif root == "directory_symlink":
            ca.mkdir()
        elif root == "invalid_symlink":
            ca.write_text("invalid")
        elif root == "unreadable_symlink":
            ca.write_text(TEST_CA)
            ca.chmod(0)
        elif root == "fifo_symlink":
            os.mkfifo(ca)
        link.symlink_to(ca)
        root = str(link)
    elif root == "nul":
        root = "/tmp/private\0.crt"
    elif root == "control":
        root = str(tmp_path / "private\n.crt")
    elif root == "del":
        root = str(tmp_path / "private\x7f.crt")
    elif root == "whitespace":
        root = str(ca_file(tmp_path)) + " "
    path = runtime_file(tmp_path, mode, root=root)
    with pytest.raises(PgConfigError):
        connect_dev("synthetic", runtime_path=path)


@pytest.mark.parametrize("mode", ["verify-ca", "verify-full"])
def test_verification_requires_explicit_root(tmp_path, mode):
    with pytest.raises(PgConfigError, match="postgres_sslrootcert_required"):
        connect_dev("synthetic", runtime_path=runtime_file(tmp_path, mode))


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
@pytest.mark.parametrize("kind", ["absolute", "relative", "chain", "directory"])
def test_ca_alias_compatibility(tmp_path, monkeypatch, mode, kind):
    ca = ca_file(tmp_path)
    alias = tmp_path / "cert.pem"
    if kind == "directory":
        directory = tmp_path / "certs"
        directory.symlink_to(tmp_path, target_is_directory=True)
        alias = directory / ca.name
    elif kind == "chain":
        intermediate = tmp_path / "intermediate.pem"
        intermediate.symlink_to(ca.name)
        alias.symlink_to(intermediate.name)
    else:
        alias.symlink_to(ca if kind == "absolute" else ca.name)
    assert alias.resolve(strict=True) == ca
    path = runtime_file(tmp_path, mode, root=alias)
    profile = load_pg_config(path)
    expected = load_pg_config(runtime_file(tmp_path, mode, root=ca)).connection_kwargs()
    expected["sslrootcert"] = str(alias)
    assert profile.connection_kwargs() == expected
    calls = []
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: calls.append(kwargs))
    connect_dev("synthetic", runtime_path=runtime_file(tmp_path, mode, root=alias))
    assert len(calls) == 1
    assert all(calls[0][key] == value for key, value in expected.items())


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
@pytest.mark.parametrize("change", ["missing", "invalid", "unreadable", "retarget"])
def test_ca_alias_revalidated_before_connection(tmp_path, mode, change):
    ca = ca_file(tmp_path)
    alias = tmp_path / "cert.pem"
    alias.symlink_to(ca.name)
    path = runtime_file(tmp_path, mode, root=alias)
    profile = load_pg_config(path)
    if change == "missing":
        ca.unlink()
    elif change == "invalid":
        ca.write_text("invalid")
    elif change == "unreadable":
        ca.chmod(0)
    else:
        invalid = tmp_path / "invalid.crt"
        invalid.write_text("invalid")
        alias.unlink()
        alias.symlink_to(invalid)
    with pytest.raises(PgConfigError):
        profile.connection_kwargs()
    with pytest.raises(PgConfigError):
        connect_dev("synthetic", runtime_path=path)


@pytest.mark.parametrize("error", [PermissionError, ssl.SSLError])
def test_ca_alias_load_failure_is_sanitized(tmp_path, monkeypatch, error):
    alias = tmp_path / "cert.pem"
    alias.symlink_to(ca_file(tmp_path))
    path = runtime_file(tmp_path, "verify-full", root=alias)

    def refuse(*args, **kwargs):
        raise error("synthetic-private")

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", refuse)
    with pytest.raises(PgConfigError, match="^postgres_sslrootcert_invalid$") as caught:
        connect_dev("synthetic", runtime_path=path)
    assert caught.value.__cause__ is caught.value.__context__ is None


@pytest.mark.parametrize(
    "change",
    [
        {"sslmode": "prefer"},
        {"sslmode": "disable"},
        {"sslmode": "allow"},
        {"sslmode": "VERIFY-FULL"},
        {"sslmode": ""},
        {"sslmode": []},
        {"host": "127.0.0.1"},
        {"hostaddr": "203.0.113.1"},
        {"port": True},
        {"port": "5432"},
        {"port": 5433},
        {"user": "other"},
        {"database": "postgresql://untrusted/db"},
        {"password": "synthetic"},
        {"options": "-c role=other"},
        {"sslrootcert": False},
        {"sslrootcert": ""},
    ],
)
def test_invalid_shape_or_target_fails_before_connection(tmp_path, change):
    path = runtime_file(tmp_path, **change)
    with pytest.raises(PgConfigError):
        connect_dev("synthetic", runtime_path=path)


@pytest.mark.parametrize(
    "content",
    [
        "",
        "[postgres]",
        "[postgres",
        "postgres = 2",
        "[postgres]\npassword = 'synthetic-private'",
    ],
)
def test_missing_or_malformed_section_is_sanitized(tmp_path, content):
    path = tmp_path / "runtime.toml"
    path.write_text(content)
    with pytest.raises(PgConfigError) as caught:
        connect_dev("synthetic", runtime_path=path)
    assert "synthetic-private" not in str(caught.value)
    assert caught.value.__cause__ is caught.value.__context__ is None


@pytest.mark.parametrize(
    "kind", ["missing", "directory", "relative", "traversal", "symlink"]
)
def test_runtime_path_contract(tmp_path, kind):
    path = runtime_file(tmp_path)
    if kind == "missing":
        path.unlink()
    elif kind == "directory":
        path = tmp_path
    elif kind == "relative":
        path = Path("runtime.toml")
    elif kind == "traversal":
        path = tmp_path / ".." / tmp_path.name / "runtime.toml"
    else:
        link = tmp_path / "link.toml"
        link.symlink_to(path)
        path = link
    with pytest.raises(PgConfigError):
        connect_dev("synthetic", runtime_path=path)


@pytest.mark.parametrize(
    "name",
    [
        "PGHOSTADDR",
        "PGHOST",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGSYSCONFDIR",
        "PGTARGETSESSIONATTRS",
        "PGLOADBALANCEHOSTS",
        "PGSSLMODE",
        "PGREQUIRESSL",
        "PGSSLNEGOTIATION",
        "PGSSLCOMPRESSION",
        "PGSSLCERT",
        "PGSSLKEY",
        "PGSSLCERTMODE",
        "PGSSLROOTCERT",
        "PGSSLCRL",
        "PGSSLCRLDIR",
        "PGSSLSNI",
        "PGSSLMINPROTOCOLVERSION",
        "PGSSLMAXPROTOCOLVERSION",
        "PGGSSENCMODE",
        "PGKRBSRVNAME",
        "PGGSSLIB",
        "PGGSSDELEGATION",
        "PGCHANNELBINDING",
        "PGREQUIREAUTH",
        "PGREQUIREPEER",
        "PGOPTIONS",
    ],
)
@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
@pytest.mark.parametrize("value", ["", "synthetic-private"])
def test_environment_rejected_at_load_and_connection(
    tmp_path, monkeypatch, name, mode, value
):
    path = runtime_file(tmp_path, mode, root=ca_file(tmp_path))
    profile = load_pg_config(path)
    monkeypatch.setenv(name, value)
    with pytest.raises(PgConfigError, match="^postgres_environment_not_allowed$"):
        connect_dev("synthetic", runtime_path=path)
    with pytest.raises(PgConfigError, match="^postgres_environment_not_allowed$"):
        profile.connection_kwargs()


@pytest.mark.parametrize(
    "name,value",
    [
        ("PGAPPNAME", "offline-compat"),
        ("PGCONNECT_TIMEOUT", "13"),
        ("PGPASSFILE", "/nonexistent/caller-owned-passfile"),
        ("PGPASSWORD", "synthetic-private"),
        ("PGCLIENTENCODING", "UTF8"),
        ("PGTZ", "UTC"),
        ("PGDATESTYLE", "ISO"),
        ("PG_TEST_UNRELATED", "synthetic"),
    ],
)
@pytest.mark.parametrize(
    "mode,with_root",
    [("require", False), ("require", True), ("verify-ca", True), ("verify-full", True)],
)
def test_compatible_environment_preserves_explicit_options(
    tmp_path, monkeypatch, name, value, mode, with_root
):
    path = runtime_file(tmp_path, mode, root=ca_file(tmp_path) if with_root else None)
    profile = load_pg_config(path)
    expected = profile.connection_kwargs()
    monkeypatch.setenv(name, value)
    assert profile.connection_kwargs() == expected
    assert load_pg_config(path).connection_kwargs() == expected
    calls = []
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: calls.append(kwargs))
    connect_dev("synthetic", runtime_path=path)
    assert len(calls) == 1
    assert all(calls[0][key] == val for key, val in expected.items())
    assert calls[0]["connect_timeout"] == 5
    assert calls[0]["password"] == "synthetic"
    assert os.environ[name] == value


def test_profile_rechecks_ca_and_identity(tmp_path):
    ca = ca_file(tmp_path)
    profile = load_pg_config(runtime_file(tmp_path, "verify-full", root=ca))
    with pytest.raises(PgConfigError):
        replace(profile, host="127.0.0.1")
    ca.unlink()
    with pytest.raises(PgConfigError):
        profile.connection_kwargs()


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_connection_failure_never_retries_or_downgrades(tmp_path, monkeypatch, mode):
    root = None if mode == "require" else ca_file(tmp_path)
    path = runtime_file(tmp_path, mode, root=root)
    calls = []

    def refuse(**kwargs):
        calls.append(kwargs)
        raise OSError("synthetic connection failure")

    monkeypatch.setattr(psycopg, "connect", refuse)
    with pytest.raises(OSError):
        connect_dev("synthetic", runtime_path=path)
    assert len(calls) == 1
    assert calls[0]["sslmode"] == mode


def test_require_rejects_declared_malformed_root(tmp_path):
    root = tmp_path / "bad.crt"
    root.write_text("not a certificate")
    path = runtime_file(tmp_path, "require", root=root)
    with pytest.raises(PgConfigError, match="postgres_sslrootcert_invalid"):
        connect_dev("synthetic", runtime_path=path)
