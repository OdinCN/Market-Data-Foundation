"""Opt-in Host wiring for exactly one DEV CPI release, with no import-time IO."""

import ipaddress
import json
import tomllib
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from fastapi import Request
from fastapi.responses import JSONResponse

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import digest, require
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.historical.bls_cpi_direct import (
    MAX_RESPONSE_BYTES,
    SERIES_IDS,
)
from market_data_foundation.historical.bls_cpi_direct_live import (
    TIMEOUT_SECONDS,
    ExactOneHttpsTransport,
    load_registration_key,
)
from market_data_foundation.historical.bls_cpi_material_store import (
    BUCKET,
    GARAGE_ENDPOINT,
    GarageCredentials,
    GarageMaterialStore,
    PostgresAdvisoryWriterProtection,
)
from market_data_foundation.historical.cpi_local_job import (
    CONTRACT,
    QUALITY,
    CpiJobSpec,
    CpiLocalJob,
    DirectCpiAcquirer,
)
from market_data_foundation.historical.cpi_pg_binding import (
    BACKFILL_JOB,
    INITIAL_JOB,
    SCHEMA,
    CpiPgBinding,
)
from market_data_foundation.historical.cpi_pg_journal import (
    PgJournalIdentity,
    PostgresJobJournal,
)
from market_data_foundation.storage.postgres_runtime_config import load_pg_config
from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.cpi_jobs import CpiManualWorker, CpiPgJobsHost, Operator
from market_data_foundation.web.cpi_pg_readonly_scope import (
    DevReadIdentity,
    _identity,
    dev_read_scope_factory,
)

CONFIG = Path("/srv/market-data-foundation/config/local")
ORIGIN = "http://192.168.10.20:18759"
LISTENER = ("192.168.10.20", 18759)
LAN = ipaddress.ip_network("192.168.10.0/24")
OPERATOR = "ao-dev-cpi-05"
WORKER = "ao-dev-cpi-05-worker"
BINDING = CpiPgBinding(OPERATOR)


def clock():
    return datetime.now(UTC)


def candidate_spec():
    return CpiJobSpec(
        definition=BINDING.definition,
        definition_revision=BINDING.definition_revision,
        series=SERIES_IDS,
        reference=BINDING.reference,
        source_identity=BINDING.source,
        source_revision=BINDING.source_revision,
        provider_revision=BINDING.provider_revision,
        completeness=CONTRACT.completeness,
        quality=QUALITY,
        release_slot=BINDING.release_slot,
        mode="acquire",
        max_provider_requests=1,
        max_response_bytes=MAX_RESPONSE_BYTES,
        timeout_seconds=TIMEOUT_SECONDS,
        effect_domain="owner_nonproduction",
    )


def job_identity():
    return (
        "cpi-init-"
        + digest(json.dumps(candidate_spec().document(), sort_keys=True).encode())[:40]
    )


def admitted(request: Request):
    """Direct TCP peer and exact Host/Origin; no proxy or browser identity trust."""
    try:
        peer = ipaddress.ip_address(request.client.host)
        if not (peer.is_loopback or peer in LAN):
            return False
        if request.headers.getlist("host") != [ORIGIN.removeprefix("http://")]:
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
        if origins and origins != [ORIGIN]:
            return False
        if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
            return False
        if request.method not in {"GET", "HEAD"}:
            return (
                request.method == "POST"
                and request.url.path == "/api/r5/cpi/jobs"
                and not request.url.query
                and origins == [ORIGIN]
                and request.headers.get("content-type", "").split(";")[0]
                == "application/json"
            )
        return True
    except ValueError, AttributeError:
        return False


def authorize(request):
    return Operator(OPERATOR, True) if admitted(request) else None


def create_preview(journal, read_scope, *, ui_directory=None, enabled=False):
    require(enabled is True, "cpi_dev_preview_opt_in_required")
    require(journal.binding == BINDING, "cpi_pg_binding_mismatch")
    host = CpiPgJobsHost(journal, candidate_spec(), clock, authorize)
    app = create_app(read_scope, jobs_host=host, ui_directory=ui_directory)

    @app.middleware("http")
    async def boundary(request, call_next):
        if not admitted(request):
            return JSONResponse(
                status_code=403,
                content={
                    "status": "NOT_AVAILABLE",
                    "classification": "operator_not_authorized",
                },
                headers={"Cache-Control": "no-store"},
            )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return app


class DevHost:
    """Reads existing configuration only after Host opt-in. Never creates config."""

    def __init__(self, schema_identity: PgJournalIdentity, *, enabled=False):
        require(enabled is True, "cpi_dev_host_opt_in_required")
        self.profile = load_pg_config(CONFIG / "runtime.toml")
        self.identity = schema_identity
        require(
            schema_identity.schema_owner == self.profile.user,
            "cpi_dev_schema_owner_mismatch",
        )
        self.read_identity = DevReadIdentity(
            self.profile.user, schema_identity.schema_owner, schema_identity.schema_oid
        )

    def connect(self, **kwargs):
        require(
            load_pg_config(CONFIG / "runtime.toml") == self.profile,
            "cpi_dev_profile_changed",
        )
        expected = dict(
            self.profile.connection_kwargs(),
            autocommit=True,
            connect_timeout=5,
            options="-c statement_timeout=3000",
        )
        require(kwargs == expected, "cpi_dev_connection_options_mismatch")
        secret = tomllib.loads((CONFIG / "secret.toml").read_text())
        password = secret["postgres"]["password"]
        require(type(password) is str and bool(password), "cpi_dev_credential_missing")
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

    def read_scope(self):
        return dev_read_scope_factory(
            self.connect,
            self.read_identity,
            enabled=True,
            pg_profile=self.profile,
            target=BINDING.target,
        )

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
        # Check canonical schema/owner/TLS and bounded content through the exact
        # reader before Provider/Garage. No automatic DDL or fallback.
        with self.read_scope()():
            pass
        journal = self.journal()
        with journal._transaction(write=False) as connection:
            journal._check_schema(connection, True)
            rows = connection.execute(
                "SELECT bool_and(has_table_privilege(current_user, "
                "quote_ident(%s) || '.' || quote_ident(t), 'INSERT')) "
                "FROM unnest(ARRAY['records','contents','publications']) t",
                (SCHEMA,),
            ).fetchone()
            require(rows == (True,), "cpi_dev_canonical_write_denied")

    def worker_once(self, artifact_root: Path, *, enabled=False, job_id=None):
        require(enabled is True, "cpi_dev_worker_opt_in_required")
        require(
            artifact_root.resolve().is_relative_to(Path("artifacts").resolve()),
            "cpi_dev_artifact_scope_required",
        )
        selected = job_identity() if job_id is None else job_id
        require(selected in {INITIAL_JOB, BACKFILL_JOB}, "job_target_unsupported")
        spec = candidate_spec()
        journal = self.journal()
        document = journal.get(selected)
        if selected == BACKFILL_JOB:
            with journal._transaction() as c:
                journal._backfill_storage(c)
        if document["state"] not in {"pending", "running", "outcome_unknown"}:
            return document
        if document["state"] in {"running", "outcome_unknown"}:
            # Recovery is observation-only, but must qualify the same namespace
            # before material/canonical reads, just as a pending attempt does.
            self.preflight(spec)
        runtime = tomllib.loads((CONFIG / "runtime.toml").read_text())
        s3 = runtime["s3"]
        require(
            runtime["storage"]["backend"] == "s3"
            and s3["endpoint_url"] == GARAGE_ENDPOINT
            and s3["bucket"] == BUCKET
            and s3["region"] == "garage"
            and s3["key_prefix"] == ""
            and s3["addressing_style"] == "path",
            "cpi_dev_garage_binding_mismatch",
        )
        secret = tomllib.loads((CONFIG / "secret.toml").read_text())
        credentials = GarageCredentials(
            secret["s3"]["access_key_id"], secret["s3"]["secret_access_key"]
        )
        if document["state"] == "pending":
            load_registration_key(CONFIG / "secret.toml")
        with self.connection() as writer:
            # The canonical kernel owns explicit commit boundaries. No implicit
            # autocommit writes; the journal always has separate connections.
            writer.autocommit = False
            engine = CpiLocalJob(
                journal,
                clock,
                CanonicalKernel(PostgresLedger(writer, SCHEMA), clock),
                GarageMaterialStore(credentials),
                PostgresAdvisoryWriterProtection(writer),
                self.ledger_readback,
                writer.commit,
                target_binding=BINDING,
            )
            try:
                return CpiManualWorker(
                    WORKER,
                    engine,
                    spec,
                    self.preflight,
                    DirectCpiAcquirer(
                        ExactOneHttpsTransport(),
                        CONFIG / "secret.toml",
                        artifact_root,
                        clock,
                    ),
                ).process(selected, enabled=True)
            finally:
                # Never let connection.__exit__ commit after an UNKNOWN return.
                writer.rollback()
