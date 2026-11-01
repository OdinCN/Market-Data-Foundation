"""Explicit PCE Host wiring. Construction never performs network or secret IO."""

import ipaddress
import os
import tomllib
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from fastapi import Request

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import require
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.historical.bls_cpi_material_store import (
    BUCKET,
    GARAGE_ENDPOINT,
    GarageCredentials,
    GarageMaterialStore,
    PostgresAdvisoryWriterProtection,
)
from market_data_foundation.historical.pce_dev_acquire import LivePceAcquirer
from market_data_foundation.historical.pce_local_job import PceLocalJob
from market_data_foundation.historical.pce_pg_binding import (
    BINDING,
    SCHEMA,
    backfill_identity,
    job_identity,
)
from market_data_foundation.historical.pce_pg_journal import (
    PgJournalIdentity,
    PostgresJobJournal,
)
from market_data_foundation.storage.postgres_runtime_config import load_pg_config
from market_data_foundation.web.cpi_pg_readonly_scope import (
    DevReadIdentity,
    _identity,
    _snapshot,
)

CONFIG = Path("/srv/market-data-foundation/config/local")
ORIGIN = "http://192.168.10.20:18760"
LISTENER = ("192.168.10.20", 18760)
CANDIDATE_ORIGIN = "http://192.168.10.20:18761"
CANDIDATE_LISTENER = ("192.168.10.20", 18761)
LAN = ipaddress.ip_network("192.168.10.0/24")


def clock():
    return datetime.now(UTC)


def _readonly_review_origin():
    """An explicitly configured public origin grants reads on the DEV candidate only."""
    value = os.environ.get("MDF_DEV_READONLY_ORIGIN", "")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme == "http"
            and parsed.hostname
            and parsed.port == CANDIDATE_LISTENER[1]
            and parsed.username is None
            and parsed.password is None
            and value == "http://" + parsed.netloc
            and value != CANDIDATE_ORIGIN
        ):
            return value
    except ValueError:
        pass
    return None


def admitted(request: Request, *, origin=ORIGIN):
    try:
        if origin not in {ORIGIN, CANDIDATE_ORIGIN}:
            return False
        peer = ipaddress.ip_address(request.client.host)
        review_origin = _readonly_review_origin()
        public_read = (
            origin == CANDIDATE_ORIGIN
            and review_origin is not None
            and request.headers.getlist("host")
            == [review_origin.removeprefix("http://")]
        )
        if public_read:
            if request.method not in {"GET", "HEAD"}:
                return False
            origin = review_origin
        elif not (peer.is_loopback or peer in LAN):
            return False
        if request.headers.getlist("host") != [origin.removeprefix("http://")]:
            return False
        if any(
            key in request.headers
            for key in (
                "forwarded",
                "x-forwarded-for",
                "x-forwarded-host",
                "x-forwarded-proto",
                "x-operator",
                "x-operator-identity",
                "x-user",
                "x-role",
                "authorization",
            )
        ):
            return False
        origins = request.headers.getlist("origin")
        if origins and origins != [origin]:
            return False
        if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
            return False
        if request.method not in {"GET", "HEAD"}:
            return (
                request.method == "POST"
                and request.url.path
                in {
                    "/api/pce/jobs",
                    "/api/pce/jobs/backfill",
                    "/api/r5/cpi/jobs/backfill",
                }
                and not request.url.query
                and origins == [origin]
                and request.headers.getlist("content-type") == ["application/json"]
            )
        return True
    except ValueError, AttributeError:
        return False


class DevHost:
    def __init__(self, identity: PgJournalIdentity, *, enabled=False):
        require(enabled is True, "pce_dev_host_opt_in_required")
        self.profile = load_pg_config(CONFIG / "runtime.toml")
        require(self.profile.sslmode == "verify-full", "pce_verify_full_required")
        self.identity = identity
        self.read_identity = DevReadIdentity(
            self.profile.user, identity.schema_owner, identity.schema_oid
        )

    def connect(self, **kwargs):
        require(
            load_pg_config(CONFIG / "runtime.toml") == self.profile,
            "pce_dev_profile_changed",
        )
        expected = dict(
            self.profile.connection_kwargs(),
            autocommit=True,
            connect_timeout=5,
            options="-c statement_timeout=3000",
        )
        require(kwargs == expected, "pce_dev_connection_options_mismatch")
        secret = tomllib.loads((CONFIG / "secret.toml").read_text())
        password = secret["postgres"]["password"]
        require(type(password) is str and bool(password), "pce_dev_credential_missing")
        return psycopg.connect(**kwargs, password=password)

    def connection(self):
        return self.connect(
            **self.profile.connection_kwargs(),
            autocommit=True,
            connect_timeout=5,
            options="-c statement_timeout=3000",
        )

    def journal(self):
        return PostgresJobJournal(
            self.connect, self.profile, self.identity, BINDING, enabled=True
        )

    @contextmanager
    def read_scope(self):
        snapshot = None
        with self.connection() as connection:
            try:
                connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                connection.execute("SET LOCAL statement_timeout = '3000ms'")
                connection.execute("SET LOCAL search_path = pg_catalog")
                _identity(connection, self.read_identity, self.profile, SCHEMA)
                snapshot = _snapshot(connection, SCHEMA)
                yield snapshot
            finally:
                if snapshot is not None:
                    snapshot._expire()
                connection.rollback()

    @contextmanager
    def ledger_readback(self):
        with self.connection() as connection:
            try:
                connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                connection.execute("SET LOCAL statement_timeout = '3000ms'")
                connection.execute("SET LOCAL search_path = pg_catalog")
                _identity(connection, self.read_identity, self.profile, SCHEMA)
                yield PostgresLedger(connection, SCHEMA)
            finally:
                connection.rollback()

    def preflight(self, spec):
        BINDING.check_spec(spec.document(), spec.scope)
        with self.read_scope():
            pass
        journal = self.journal()
        with journal._transaction(write=False) as connection:
            journal._check_schema(connection, True)
            row = connection.execute(
                "SELECT bool_and(has_table_privilege(current_user, "
                "quote_ident(%s) || '.' || quote_ident(t), 'INSERT')) "
                "FROM unnest(ARRAY['records','contents','publications']) t",
                (SCHEMA,),
            ).fetchone()
            require(row == (True,), "pce_dev_canonical_write_denied")

    def worker_once(self, execution_identity, *, enabled=False):
        require(enabled is True, "pce_dev_worker_opt_in_required")
        execution_identity.validate()
        identity = execution_identity.producer_execution_identity
        require(
            identity in {job_identity(), backfill_identity()}
            and execution_identity.attempt_identity == identity + "-a1",
            "pce_execution_identity_mismatch",
        )
        journal = self.journal()
        document = journal.get(identity)
        if document["state"] == "running":
            # Only a free executor lock proves there is no active owned Worker.
            # Record interruption, never replay the source or material effects.
            with journal.executor(identity):
                document = journal.get(identity)
                if document["state"] == "running":
                    at = clock()
                    document = journal._update(
                        identity,
                        at,
                        state="outcome_unknown",
                        stage="restart_detected",
                        classification="interrupted_attempt",
                        finished_at=at.isoformat(),
                    )
            return document
        if document["state"] != "pending":
            return document
        with self.connection() as writer:
            writer.autocommit = False
            # Configuration and Garage credentials are loaded inside preflight,
            # after the PG Attempt has been accepted. No Garage call occurs here.
            engine = PceLocalJob(
                journal,
                clock,
                CanonicalKernel(PostgresLedger(writer, SCHEMA), clock),
                None,
                PostgresAdvisoryWriterProtection(writer),
                self.ledger_readback,
                writer.commit,
            )

            def prepare():
                runtime = tomllib.loads((CONFIG / "runtime.toml").read_text())
                s3 = runtime["s3"]
                require(
                    runtime["storage"]["backend"] == "s3"
                    and s3["endpoint_url"] == GARAGE_ENDPOINT
                    and s3["bucket"] == BUCKET
                    and s3["region"] == "garage"
                    and s3["key_prefix"] == ""
                    and s3["addressing_style"] == "path",
                    "pce_dev_garage_binding_mismatch",
                )
                secret = tomllib.loads((CONFIG / "secret.toml").read_text())
                engine.store = GarageMaterialStore(
                    GarageCredentials(
                        secret["s3"]["access_key_id"], secret["s3"]["secret_access_key"]
                    )
                )

            try:
                return engine.run(
                    identity,
                    enabled=True,
                    preflight=self.preflight,
                    prepare=prepare,
                    acquire=LivePceAcquirer(
                        execution_identity,
                        Path("artifacts/b3-bea-pce-r1") / (identity + "-a1"),
                        clock,
                    ),
                )
            finally:
                writer.rollback()
