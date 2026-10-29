"""Explicit DEV-only CPI snapshot binding; no credentials or ambient clients.

The Host owns admission and supplies a fresh, exclusive psycopg connector.
This module does not qualify the database's provenance or authorize live IO.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from psycopg import sql
from psycopg.pq import TransactionStatus

from market_data_foundation.canonical.codec import loads
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
)
from market_data_foundation.canonical.model import (
    Key,
    Kind,
    Publication,
    digest,
    require,
)
from market_data_foundation.canonical.operator_reads import (
    MemoryOperatorReads,
    _Admission,
)
from market_data_foundation.historical.cpi_local_read import CpiReadRejected
from market_data_foundation.historical.cpi_pg_binding import CpiPgTarget
from market_data_foundation.storage.postgres_runtime_config import (
    DEV_DATABASE,
    DEV_HOST,
    PgConfig,
)
from market_data_foundation.web.cpi_api import ReadScope, ScopeFactory, create_app

DEV_SCHEMA = "mdf_local_first_cpi_operable_r1"
SERVER_VERSION = 180006
MAX_RECORDS = 128
MAX_PUBLICATIONS = 128
MAX_CONTENTS = 32
MAX_ROW_BYTES = 1_048_576
MAX_TOTAL_BYTES = 8_388_608
STATEMENT_TIMEOUT_MS = 3000


@dataclass(frozen=True)
class DevReadIdentity:
    """Non-secret pins obtained independently by the Host, never auto-discovered."""

    role: str
    schema_owner: str
    schema_oid: int

    def __post_init__(self):
        if (
            not self.role
            or not self.schema_owner
            or type(self.schema_oid) is not int
            or self.schema_oid <= 0
        ):
            raise ValueError("dev_read_identity_required")


def _fail() -> None:
    try:
        raise ReadContractError(ReadFailure.STORAGE_UNAVAILABLE) from None
    except ReadContractError as error:
        # contextlib.throw keeps the caller exception active in a generator.
        # Suppress both display and introspection of private driver/body details.
        error.__context__ = None
        raise


def _idle(connection) -> bool:
    return (
        connection.closed is False
        and connection.info.transaction_status is TransactionStatus.IDLE
        and type(connection._num_transactions) is int
        and connection._num_transactions == 0
        and connection._tpc is None
        and connection._pipeline is None
        and getattr(connection, "_pool", None) is None
    )


def _query(connection, statement, parameters=(), *, maximum=1):
    # Explicit cursor ownership covers execute/fetch/decode failures alike.
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        if cursor.description is None:
            return []
        rows = cursor.fetchmany(maximum + 1)
        if len(rows) > maximum:
            _fail()
        return rows


def _identity(
    connection, expected: DevReadIdentity, profile: PgConfig, schema=DEV_SCHEMA
) -> None:
    info = connection.info
    if (
        info.host != DEV_HOST
        or info.hostaddr != DEV_HOST
        or info.port != 5432
        or info.dbname != DEV_DATABASE
        or info.user != expected.role
        or info.server_version != SERVER_VERSION
        or connection.pgconn.ssl_in_use is not True
        or info.get_parameters().get("sslmode") != profile.sslmode
        or (
            profile.sslrootcert is not None
            and info.get_parameters().get("sslrootcert") != str(profile.sslrootcert)
        )
    ):
        _fail()
    rows = _query(
        connection,
        "SELECT host(inet_server_addr()), inet_server_port(), current_database(), "
        "current_user, session_user, current_setting('server_version_num'), "
        "current_setting('transaction_isolation'), "
        "current_setting('transaction_read_only'), "
        "current_setting('statement_timeout'), s.ssl, s.version "
        "FROM pg_catalog.pg_stat_ssl s WHERE s.pid = pg_backend_pid()",
    )
    if (
        len(rows) != 1
        or rows[0][:-1]
        != (
            DEV_HOST,
            5432,
            DEV_DATABASE,
            expected.role,
            expected.role,
            str(SERVER_VERSION),
            "repeatable read",
            "on",
            "3s",
            True,
        )
        or rows[0][-1] not in {"TLSv1.2", "TLSv1.3"}
    ):
        _fail()
    rows = _query(
        connection,
        "SELECT n.oid, pg_catalog.pg_get_userbyid(n.nspowner), c.relname, "
        "c.relkind, c.relpersistence, c.relrowsecurity, c.relforcerowsecurity, "
        "pg_catalog.pg_get_userbyid(c.relowner), "
        "pg_catalog.has_schema_privilege(n.oid, 'USAGE'), "
        "pg_catalog.has_table_privilege(c.oid, 'SELECT') "
        "FROM pg_catalog.pg_namespace n JOIN pg_catalog.pg_class c "
        "ON c.relnamespace = n.oid WHERE n.nspname = %s "
        "AND c.relname IN ('records', 'contents', 'publications') "
        "ORDER BY c.relname LIMIT 4",
        (schema,),
        maximum=3,
    )
    if rows != [
        (
            expected.schema_oid,
            expected.schema_owner,
            table,
            "r",
            "p",
            False,
            False,
            expected.schema_owner,
            True,
            True,
        )
        for table in ("contents", "publications", "records")
    ]:
        _fail()


def _batch(connection, table, columns, maximum, remaining, schema=DEV_SCHEMA):
    # Whole-table admission is intentionally conservative for this tiny schema.
    # A limit/byte overflow returns markers, never a partial successful snapshot.
    # SQL bounds returned data; statement_timeout bounds scans/TOAST/sorting time.
    fields = [sql.Identifier(column) for column in columns]
    size = sql.SQL(" + ").join(
        sql.SQL("coalesce(octet_length({}::text), 0)").format(field) for field in fields
    )
    projection = sql.SQL(", ").join(
        sql.SQL("CASE WHEN admitted THEN {} ELSE NULL END").format(f) for f in fields
    )
    statement = sql.SQL("""
        WITH bounded AS MATERIALIZED (
            SELECT *, {} AS bytes FROM {}.{} LIMIT %s
        ), admission AS (
            SELECT count(*) <= %s AND coalesce(max(bytes), 0) <= %s
                AND coalesce(sum(bytes), 0) <= %s AS admitted FROM bounded
        )
        SELECT {}, bytes, admitted FROM bounded CROSS JOIN admission
    """).format(size, sql.Identifier(schema), sql.Identifier(table), projection)
    rows = _query(
        connection,
        statement,
        (maximum + 1, maximum, MAX_ROW_BYTES, remaining),
        maximum=maximum,
    )
    if any(row[-1] is not True or not 0 <= row[-2] <= MAX_ROW_BYTES for row in rows):
        _fail()
    used = sum(row[-2] for row in rows)
    if used > remaining:
        _fail()
    return [row[:-2] for row in rows], remaining - used


class _ReadSnapshot:
    """Only read ports; immutable serialized values, no ledger or connection."""

    def __init__(self, records, contents):
        self.__records = MappingProxyType(records)
        self.__contents = MappingProxyType(contents)
        self.__active = True

    def _expire(self):
        self.__active = False

    def _check(self):
        if not self.__active:
            _fail()

    def get(self, key):
        self._check()
        return MemoryOperatorReads(self.__records).get(key)

    def get_content(self, version):
        self._check()
        require(version.kind == Kind.VERSION, "content_key_kind")
        stored = self.__contents.get(version.identity)
        if stored is None:
            return None
        sha, content = stored
        require(digest(content) == sha, "stored_content_mismatch")
        return content

    def publications_for_version(self, version, maximum=MAX_PUBLICATIONS):
        self._check()
        return MemoryOperatorReads(self.__records).publications_for_version(
            version, min(maximum, MAX_PUBLICATIONS)
        )

    def versions_for_definition(self, definition, maximum=MAX_RECORDS):
        self._check()
        return MemoryOperatorReads(self.__records).versions_for_definition(
            definition, min(maximum, MAX_RECORDS)
        )

    def binding_candidates(self, maximum):
        self._check()
        return MemoryOperatorReads(self.__records).binding_candidates(
            min(maximum, MAX_RECORDS)
        )

    def operator_reads(self):
        self._check()
        return self


def _snapshot(connection, schema=DEV_SCHEMA):
    records, contents = {}, {}
    remaining = MAX_TOTAL_BYTES
    admission = _Admission()
    rows, remaining = _batch(
        connection,
        "records",
        ("kind", "identity", "revision", "payload"),
        MAX_RECORDS,
        remaining,
        schema,
    )
    for kind, identity, revision, payload in rows:
        admission.batch([payload])
        key = Key(Kind(kind), identity, revision)
        value = loads(payload)
        if key.kind == Kind.PUBLICATION or getattr(value, "key", None) != key:
            _fail()
        slot = (kind, identity, revision)
        if slot in records:
            _fail()
        records[slot] = payload
    rows, remaining = _batch(
        connection,
        "publications",
        ("identity", "occurrence", "version_identity", "decision_at", "payload"),
        MAX_PUBLICATIONS,
        remaining,
        schema,
    )
    occurrences = set()
    for identity, occurrence, version, decision_at, payload in rows:
        admission.batch([payload])
        value = loads(payload)
        if not isinstance(value, Publication) or (
            value.key.identity,
            value.request.occurrence,
            value.request.version.identity,
            value.decision_at,
        ) != (identity, occurrence, version, decision_at):
            _fail()
        slot = (Kind.PUBLICATION.value, identity, "")
        if slot in records or occurrence in occurrences:
            _fail()
        occurrences.add(occurrence)
        records[slot] = payload
    rows, remaining = _batch(
        connection,
        "contents",
        ("version_identity", "digest", "payload"),
        MAX_CONTENTS,
        remaining,
        schema,
    )
    for identity, sha, payload in rows:
        if identity in contents or type(payload) is not bytes:
            _fail()
        contents[identity] = (sha, payload)
    return _ReadSnapshot(records, contents)


def dev_read_scope_factory(
    connector: Callable[..., psycopg.Connection],
    identity: DevReadIdentity,
    *,
    enabled: bool = False,
    pg_profile: PgConfig | None = None,
    target: CpiPgTarget | None = None,
) -> ScopeFactory:
    """Build an inert factory. Each entered scope opens and discards one session.

    Connector accepts the enforced libpq options below, adds caller-owned auth,
    and returns a new exclusive connection (never a pool/ambient transaction).
    It must clean up its own partial connection on connection-establishment error.
    No automatic retry, role changes, namespace creation or secret loading.
    Host supplies pg_profile from load_pg_config(global_runtime_path). A missing
    profile fails closed, even when enabled. Explicit require pins the endpoint
    and roles but does not establish a CA-authenticated server identity.
    """

    if target is not None and type(target) is not CpiPgTarget:
        raise ValueError("cpi_pg_target_not_admitted")
    schema = DEV_SCHEMA if target is None else target.schema
    return _readonly_scope_factory(
        connector, identity, enabled=enabled, pg_profile=pg_profile, schema=schema
    )


def _readonly_scope_factory(connector, identity, *, enabled, pg_profile, schema):
    """Shared bounded transaction machinery for explicitly pinned CPI/PCE Hosts."""

    @contextmanager
    def scope() -> Iterator[ReadScope]:
        connection = snapshot = None
        failed = False
        owned = False
        denial = None
        try:
            if (
                enabled is not True
                or not isinstance(pg_profile, PgConfig)
                or identity.role != pg_profile.user
            ):
                _fail()
            connection = connector(
                **pg_profile.connection_kwargs(),
                connect_timeout=5,
                autocommit=True,
                options="-c statement_timeout=3000",
            )
            if not _idle(connection) or connection.autocommit is not True:
                _fail()
            owned = True
            _query(connection, "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            _query(connection, "SET LOCAL statement_timeout = '3000ms'")
            _query(
                connection, "SET LOCAL idle_in_transaction_session_timeout = '5000ms'"
            )
            _query(connection, "SET LOCAL search_path = pg_catalog")
            _identity(connection, identity, pg_profile, schema)
            snapshot = _snapshot(connection, schema)
            if connection.info.transaction_status is not TransactionStatus.INTRANS:
                _fail()
            yield snapshot
        except (ReadContractError, CpiReadRejected) as error:
            denial = (type(error), error.classification)
        except Exception:
            failed = True
        finally:
            if snapshot is not None:
                snapshot._expire()
            if connection is not None:
                try:
                    if owned:
                        connection.rollback()
                        if not _idle(connection):
                            failed = True
                except BaseException:
                    failed = True
                finally:
                    try:
                        # Discard even a rejected pool connection, never return it.
                        if getattr(connection, "_pool", None) is not None:
                            connection.pgconn.finish()
                        else:
                            connection.close()
                        if connection.closed is not True:
                            failed = True
                    except BaseException:
                        failed = True
                        try:
                            connection.pgconn.finish()
                        except BaseException:
                            pass
        # Outside except: raw driver/body exceptions are not chained or retained.
        if failed:
            _fail()
        if denial is not None:
            error_type, classification = denial
            try:
                raise error_type(classification) from None
            except (ReadContractError, CpiReadRejected) as error:
                error.__context__ = None
                raise

    return scope


def create_dev_readonly_app(
    connector: Callable[..., psycopg.Connection] | None = None,
    identity: DevReadIdentity | None = None,
    *,
    enabled: bool = False,
    pg_profile: PgConfig | None = None,
    ui_directory: Path | None = None,
) -> FastAPI:
    """Explicit Host wiring, disabled by default, with only GET/HEAD routes.

    No jobs/schedules/management/observability Host is accepted or inferred.
    The existing create_app default and its frozen OpenAPI remain unchanged.
    """
    factory = None
    if enabled is True:
        if connector is None or identity is None:
            raise ValueError("dev_read_identity_and_connector_required")
        if not isinstance(pg_profile, PgConfig) or identity.role != pg_profile.user:
            raise ValueError("dev_read_pg_profile_required")
        factory = dev_read_scope_factory(
            connector, identity, enabled=True, pg_profile=pg_profile
        )
    app = create_app(factory, ui_directory=ui_directory)

    def only_reads(application):
        router = getattr(application, "router", None)
        if router is None:
            return
        router.routes[:] = [
            route
            for route in router.routes
            if getattr(route, "methods", None) is None
            or route.methods <= {"GET", "HEAD"}
        ]
        for route in router.routes:
            only_reads(getattr(route, "app", None))

    only_reads(app)

    @app.middleware("http")
    async def read_methods_only(request, call_next):
        if request.method not in {"GET", "HEAD"}:
            return JSONResponse(
                status_code=405,
                headers={"Allow": "GET, HEAD", "Cache-Control": "no-store"},
                content={
                    "status": "NOT_AVAILABLE",
                    "classification": "method_not_allowed",
                },
            )
        return await call_next(request)

    return app
