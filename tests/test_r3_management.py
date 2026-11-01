"""Offline A1 API/PG port tests. No server, DB or provider is involved."""

import json
import re
import socket
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.management import (
    FAMILIES,
    InstanceRecord,
    ManagementHost,
    create_management_app,
    project_management,
)
from market_data_foundation.web.management_pg import (
    ManagementStorageError,
    PgManagementRecords,
)
from market_data_foundation.web.source_catalog import DirectoryQuery, directory

NOW = datetime(2026, 10, 24, tzinfo=UTC)
DDL = Path("db/management/r3_a1_r1.sql")
IDENTIFIER_COLUMNS = ("identity", "catalog_identity", "endpoint_ref")


def identifier_check(column):
    """Extract only the three fixed candidate columns, never arbitrary SQL."""
    assert column in IDENTIFIER_COLUMNS
    ddl = DDL.read_text()
    assert "{0,511}" not in ddl
    match = re.search(
        rf"^    {column} text(?P<declaration>.*?) CHECK \(\n"
        r"(?P<expression>.*?)\)(?=,\n|\n\);)",
        ddl,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    expression = " ".join(match["expression"].split())
    # Full expression match pins quoting, operands and explicit grouping. The
    # BETWEEN's internal AND cannot escape its own parenthesized predicate.
    assert expression == (
        f"({column} ~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]*$') "
        f"AND (char_length({column}) BETWEEN 1 AND 512)"
    )
    return match["declaration"].strip(), expression


def offline_identifier_check(column, value):
    """Restricted PG-compatible expression oracle, NOT PostgreSQL execution.

    SQLite evaluates the shipped parentheses, AND, BETWEEN and SQL NULL logic
    in memory. Only PG's ~ is translated; char_length and this anchored ASCII
    regex subset use Python functions. fullmatch gives PG's default strict end
    anchor behavior, including rejection of a trailing newline. This cannot
    qualify PG regex compilation, locale behavior, DDL or persistence.
    """
    declaration, expression = identifier_check(column)
    expression = expression.replace(
        f"{column} ~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]*$'",
        f"regexp('^[A-Za-z0-9][A-Za-z0-9_.:@-]*$', {column})",
    )
    with sqlite3.connect(":memory:") as oracle:
        oracle.create_function(
            "regexp",
            2,
            lambda pattern, text: (
                None if text is None else re.fullmatch(pattern, text) is not None
            ),
        )
        oracle.create_function(
            "char_length", 1, lambda text: None if text is None else len(text)
        )
        result = oracle.execute(
            f"WITH candidate({column}) AS (VALUES (?)) "
            f"SELECT {expression} FROM candidate",
            (value,),
        ).fetchone()[0]
    # PG CHECK rejects FALSE only; PRIMARY KEY also implies NOT NULL.
    required = declaration in {"PRIMARY KEY", "NOT NULL"}
    return result, result != 0 and not (value is None and required)


@pytest.mark.parametrize(
    "column,declaration,null_allowed",
    [
        ("identity", "PRIMARY KEY", False),
        ("catalog_identity", "NOT NULL", False),
        ("endpoint_ref", "", True),
    ],
)
def test_pg_ddl_identifier_structure_and_nullability(column, declaration, null_allowed):
    assert identifier_check(column)[0] == declaration
    # CHECK itself yields UNKNOWN for NULL in all three cases; only the two
    # required columns reject NULL through the separate column declaration.
    assert offline_identifier_check(column, None) == (None, null_allowed)


@pytest.mark.parametrize("column", IDENTIFIER_COLUMNS)
@pytest.mark.parametrize(
    "value,accepted",
    [
        ("A", True),
        ("z", True),
        ("0", True),
        ("9", True),
        ("A" * 255, True),
        ("A" * 256, True),
        ("A" * 511, True),
        ("A" * 512, True),
        ("A" * 513, False),
        ("", False),
        ("aZ09_.:@-", True),
        *[(char + "a", False) for char in "_.:@-"],
        *[("a" + char, False) for char in " /?=#%+;'\"\\\t\r\n"],
        ("a\nb", False),
        ("中", False),
        ("a中", False),
        ("é", False),
        ("aé", False),
        ("Ａ", False),
        ("aＡ", False),
        ("١", False),
        ("a١", False),
        ("a😀", False),
    ],
)
def test_pg_ddl_identifier_boundaries_offline_oracle(column, value, accepted):
    assert offline_identifier_check(column, value) == (int(accepted), accepted)


def registration(**changes):
    data = dict(
        identity="fixture.r3.instance.1",
        catalog_identity=next(
            e.identity
            for e in directory(DirectoryQuery()).entries
            if e.domain == "Macro"
        ),
        ownership="OBSERVE_ONLY",
        endpoint_ref="fixture.endpoint.1",
    )
    return InstanceRecord(**(data | changes))


def host(record):
    return ManagementHost(lambda: (record,), "OFFLINE_FIXTURE", lambda: NOW)


def test_default_is_actual_catalog_without_host_scope_or_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("network_or_scope_forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)
    with TestClient(create_app(forbidden)) as client:
        first = client.get("/api/r3/management")
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        assert first.content == client.get("/api/r3/management").content
        data = first.json()
        assert data["entries"] == client.get("/api/r5/sources").json()["entries"]
        assert data["evidence_mode"] == "CODE_CONTRACTS_ONLY"
        assert data["instance_scope"] == "UNCONFIGURED"
        assert data["instances"] == []
        assert data["deferred"] == ["QMT"]
        assert data["persistence"] == "DEV_PG_GATE_PENDING"
        assert {f["identity"] for f in data["families"]} == {f[0] for f in FAMILIES}
        assert len(data["families"]) == len(FAMILIES)
        consumption = next(
            f for f in data["families"] if f["identity"] == "CONSUMPTION"
        )
        assert consumption["catalog_identities"] == [
            "mdf.source.bea.nipa.consumption-income@r1"
        ]
        macro = {e["identity"] for e in data["entries"] if e["domain"] == "Macro"}
        linked = [
            identity for f in data["families"] for identity in f["catalog_identities"]
        ]
        assert set(linked) == macro
        assert len(linked) == len(set(linked))
        assert all(e["source_qualification"] == "UNKNOWN" for e in data["entries"])
        for method in ("post", "put", "patch", "delete"):
            assert getattr(client, method)("/api/r3/management").status_code == 405


@pytest.mark.parametrize(
    "query",
    [
        "rank=active",
        "sql=private-sensitive",
        "credentials=private-sensitive",
        "ownership=MDF_MANAGED",
        "source=x&source=y",
    ],
)
def test_no_client_scope_or_echo(query):
    with TestClient(create_app()) as client:
        response = client.get("/api/r3/management?" + query)
        assert response.status_code == 422
        assert response.json() == {
            "status": "NOT_AVAILABLE",
            "classification": "invalid_request",
        }
        assert "private-sensitive" not in response.text


@pytest.mark.parametrize("ownership", ["MDF_MANAGED", "OWNER_MANAGED", "OBSERVE_ONLY"])
@pytest.mark.parametrize(
    "age,ttl,state,classification",
    [
        (10, 60, "AVAILABLE", "observed"),
        (60, 60, "UNKNOWN", "expired"),
        (90, 60, "UNKNOWN", "expired"),
        (-1, 60, "UNKNOWN", "clock_invalid"),
        (10, 0, "UNKNOWN", "clock_invalid"),
        (10, 61, "UNKNOWN", "clock_invalid"),
    ],
)
def test_runtime_freshness_never_promotes_qualification(
    ownership, age, ttl, state, classification
):
    at = NOW - timedelta(seconds=age)
    record = registration(
        ownership=ownership,
        state="AVAILABLE",
        observed_at=at.isoformat(),
        expires_at=(at + timedelta(seconds=ttl)).isoformat(),
    )
    data = project_management(host(record))
    instance = data.instances[0]
    assert instance.effective_state == state
    assert instance.classification == classification
    assert instance.record.observed_at == at.isoformat()
    assert instance.commands == []
    assert all(
        e.source_qualification == e.current_binding == "UNKNOWN" for e in data.entries
    )
    assert data.evidence_mode == "OFFLINE_FIXTURE"


def test_missing_observation_is_unknown_even_with_reported_available():
    assert (
        project_management(host(registration(state="AVAILABLE")))
        .instances[0]
        .effective_state
        == "UNKNOWN"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"endpoint_ref": "postgresql://user:password@private/db"},
        {"identity": "https://private?token=private"},
        {"observed_at": "2026-10-24T00:00:00"},
        {"credentials": "private-sensitive"},
        {"ownership": "ROOT"},
    ],
)
def test_instance_public_allowlist(changes):
    with pytest.raises(ValidationError):
        registration(**changes)


@pytest.mark.parametrize("scenario", ["exception", "duplicate", "orphan"])
def test_host_failure_is_all_or_nothing_and_redacted(scenario):
    def read():
        if scenario == "exception":
            raise RuntimeError("private-sensitive-password")
        if scenario == "duplicate":
            return (registration(), registration())
        return (registration(catalog_identity="missing"),)

    with TestClient(
        create_app(management_host=replace(host(registration()), read=read))
    ) as client:
        response = client.get("/api/r3/management")
        assert response.status_code == 503
        assert response.json() == {
            "status": "UNKNOWN",
            "classification": "management_read_unavailable",
        }
        assert "private-sensitive" not in response.text


def test_separate_openapi_contracts_are_frozen_and_get_only():
    schema = create_management_app().openapi()
    assert schema == json.loads(Path("web/src/management.openapi.json").read_text())
    assert create_app().openapi() == json.loads(Path("web/openapi.json").read_text())
    assert set(schema["paths"]["/management"]) == {"get"}
    for name in ("ManagementDTO", "InstanceRecord", "InstanceDTO", "FamilyDTO"):
        assert schema["components"]["schemas"][name]["additionalProperties"] is False


class MockConnection:
    """Port mock only: neither SQL semantics nor real PG durability evidence."""

    def __init__(self):
        self.rows = {}
        self.calls = []
        self.fail = False
        self.row = None

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, parameters=()):
        self.calls.append((statement, parameters))
        if self.fail:
            raise OSError("private-sensitive-dsn")
        if statement.startswith("INSERT"):
            self.rows.setdefault(parameters[0], parameters)
        elif parameters:
            self.row = self.rows.get(parameters[0])

    def fetchone(self):
        return self.row

    def fetchall(self):
        return sorted(self.rows.values())[:65]


@pytest.mark.parametrize("ownership", ["OWNER_MANAGED", "OBSERVE_ONLY"])
@pytest.mark.parametrize("endpoint_ref", [None, "fixture.endpoint.1"])
@pytest.mark.parametrize(
    "changed_field", ["ownership", "catalog_identity", "endpoint_ref"]
)
def test_pg_port_exact_replay_readback_and_conflict_without_transaction_effects(
    ownership, endpoint_ref, changed_field
):
    connection = MockConnection()
    adapter = PgManagementRecords(connection)
    record = registration(ownership=ownership, endpoint_ref=endpoint_ref)
    adapter.register(record)
    adapter.register(record)
    # Reconstruct adapter around the same mock store, not a PG restart test.
    assert PgManagementRecords(connection).read() == (record,)
    original_rows = connection.rows.copy()
    changed = {
        "ownership": "MDF_MANAGED",
        "catalog_identity": next(
            e.identity
            for e in directory(DirectoryQuery()).entries
            if e.domain == "Macro" and e.identity != record.catalog_identity
        ),
        "endpoint_ref": "fixture.endpoint.changed" if endpoint_ref is None else None,
    }
    with pytest.raises(ManagementStorageError, match="registration_identity_conflict"):
        adapter.register(
            record.model_copy(update={changed_field: changed[changed_field]})
        )
    assert connection.rows == original_rows
    adapter.register(record)
    assert adapter.read() == (record,)
    assert all(
        "CREATE" not in sql and "UPDATE" not in sql for sql, _ in connection.calls
    )
    assert all(record.identity not in sql for sql, _ in connection.calls)


def test_pg_port_does_not_retry_or_expose_raw_failure():
    connection = MockConnection()
    connection.fail = True
    with pytest.raises(ManagementStorageError, match="^storage_outcome_unknown$"):
        PgManagementRecords(connection).register(registration())
    assert len(connection.calls) == 1
    with pytest.raises(ManagementStorageError, match="^management_read_unavailable$"):
        PgManagementRecords(connection).read()


def test_pg_port_rejects_orphans_observations_and_overflow():
    connection = MockConnection()
    adapter = PgManagementRecords(connection)
    for record in (
        registration(catalog_identity="missing"),
        registration(state="AVAILABLE"),
    ):
        with pytest.raises(
            ManagementStorageError, match="registration_contract_invalid"
        ):
            adapter.register(record)
    assert connection.calls == []
    for index in range(65):
        adapter.register(registration(identity=f"fixture.instance.{index:02}"))
    with pytest.raises(ManagementStorageError, match="management_limit_exceeded"):
        adapter.read()


def test_kd_ownership_cannot_be_promoted_to_mdf_control():
    kd = next(e for e in directory(DirectoryQuery()).entries if e.domain == "KD")
    record = registration(catalog_identity=kd.identity, ownership="MDF_MANAGED")
    with pytest.raises(ValueError, match="management_identity_invalid"):
        project_management(host(record))
    connection = MockConnection()
    with pytest.raises(ManagementStorageError, match="registration_contract_invalid"):
        PgManagementRecords(connection).register(record)
    assert connection.calls == []
