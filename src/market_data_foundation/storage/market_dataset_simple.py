"""I3 fixed-payload ingest over existing D1-D4 facts and one pending table.

Use one dedicated connection per worker. Pending bytes survive process loss in PG.
Object IO precedes the facts transaction; failed/late commits never delete objects.
"""

import re

import psycopg

from market_data_foundation.canonical.market_material import byte_digest, read_material
from market_data_foundation.canonical.market_persistence import CommitUnknown
from market_data_foundation.canonical.market_projection import IndexGeneration
from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.domain_contracts.market_universe import content_id

from .market_dataset_codec import dumps, loads
from .market_dataset_garage import RUN
from .market_dataset_postgres import (
    BINDING,
    DDL_PATH,
    PostgresMarketDataset,
    preflight,
)

PENDING_DDL = DDL_PATH.with_name("002_simple_pending.sql")
REVISION = "market.dataset.simple.r1"
SCHEMA_PATTERN = re.compile(r"mdf_i3_simple_i3_[a-f0-9]{32}")
MAX_STAGED_BYTES = 64 * 1024 * 1024


def schema_for(run_id):
    require(RUN.fullmatch(run_id) is not None, "invalid_probe_run")
    return "mdf_i3_simple_" + run_id.replace("-", "_")


def ddl_digest():
    return byte_digest(DDL_PATH.read_bytes() + PENDING_DDL.read_bytes())


def migrate_simple(connection, run_id):
    schema = schema_for(run_id)
    preflight(connection)
    with connection.transaction():
        require(
            connection.execute("SELECT to_regnamespace(%s)", (schema,)).fetchone()[0]
            is None,
            "postgres_namespace_conflict",
        )
        connection.execute(f"CREATE SCHEMA {schema}")
        connection.execute(f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC")
        for path in (DDL_PATH, PENDING_DDL):
            for statement in path.read_text().split(";"):
                if statement.strip():
                    connection.execute(statement.replace("{s}", schema))
        connection.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM PUBLIC")
        connection.execute(
            f"INSERT INTO {schema}.schema_metadata VALUES "
            "(1,%s,%s,%s,%s,clock_timestamp())",
            (REVISION, ddl_digest(), BINDING, run_id),
        )
        connection.execute(
            f"INSERT INTO {schema}.projection_generations VALUES (%s,%s,true)",
            ("initial", dumps(IndexGeneration(BINDING, "initial", False))),
        )
    return schema


class SimplePostgresDataset(PostgresMarketDataset):
    def __init__(self, connection, store, decoders=(), *, owner_run):
        super().__init__(connection, store, decoders, owner_run=owner_run)
        self.schema = schema_for(owner_run)

    def _sql(self, query, args=()):
        require(SCHEMA_PATTERN.fullmatch(self.schema) is not None, "schema_scope")
        try:
            return self.connection.execute(query.replace("{s}", self.schema), args)
        except psycopg.Error:
            raise ContractError("market_postgres_statement_failed") from None

    def _identity(self):
        row = self._sql(
            "SELECT schema_revision, ddl_digest, ledger_binding, owner_run "
            "FROM {s}.schema_metadata WHERE singleton=1"
        ).fetchone()
        require(
            row == (REVISION, ddl_digest(), BINDING, self.owner_run),
            "physical_ledger_binding_mismatch",
        )

    def cleanup_probe_schema(self):
        raise ContractError("simple_storage_no_schema_drop")

    def _transaction(self, operation, identity, payload, mutate, fault=None):
        def complete():
            result = mutate()
            if operation == "material":
                self._sql(
                    "UPDATE {s}.ingest_pending SET state='complete' WHERE identity=%s",
                    (identity,),
                )
            return result

        return super()._transaction(operation, identity, payload, complete, fault)

    def retain(self, receipt, raw, *, level="raw", fault=None):
        intent = self.material_intent(receipt, level=level)
        require(
            type(raw) is bytes
            and 0 < len(raw) == receipt.byte_length <= 8388608
            and byte_digest(raw) == receipt.digest,
            "material_bytes_mismatch",
        )
        # Bind the complete physical address before even creating a pending intent.
        if receipt.locator.startswith("s3://"):
            expected = self.store.locator(
                level,
                content_id(receipt.source),
                content_id(receipt.binding),
                content_id(
                    (
                        receipt.mapping_revision,
                        receipt.upstream_revision,
                        receipt.decode_revision,
                    )
                ),
                receipt.digest,
            )
            require(receipt.locator == expected, "material_semantic_address_mismatch")
        identity = content_id(receipt.key)
        attempt_identity = content_id(
            (
                "market.dataset.simple.original-attempt.r1",
                receipt.job,
                receipt.shard,
                receipt.attempt,
                receipt.request,
                level,
            )
        )
        entering_commit = False
        try:
            with self._write():
                row = self._sql(
                    "SELECT intent,intent_digest,raw FROM {s}.ingest_pending "
                    "WHERE identity=%s OR attempt_identity=%s",
                    (identity, attempt_identity),
                ).fetchone()
                if row:
                    require(
                        row[0] == dumps(intent)
                        and row[1] == content_id(intent)
                        and bytes(row[2]) == raw,
                        "same_identity_different_content",
                    )
                else:
                    total = self._sql(
                        "SELECT coalesce(sum(length(raw)),0) FROM {s}.ingest_pending"
                    ).fetchone()[0]
                    require(total + len(raw) <= MAX_STAGED_BYTES, "staging_capacity")
                    self._sql(
                        "INSERT INTO {s}.ingest_pending VALUES "
                        "(%s,%s,%s,%s,%s,'pending')",
                        (
                            identity,
                            attempt_identity,
                            dumps(intent),
                            content_id(intent),
                            raw,
                        ),
                    )
                entering_commit = True
        except Exception:
            if entering_commit:
                raise CommitUnknown("pending_commit_acknowledgement_unknown") from None
            raise
        # No DB lock or transaction held while waiting for Garage.
        self.store.put(receipt.locator, raw)
        return super().retain(receipt, raw, level=level, fault=fault)

    def _prepare_material(self, receipt, raw):
        read_material(self.store, receipt)

    def recover(self, identity):
        self._idle()
        with self.connection.transaction():
            self._sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            self._identity()
            row = self._sql(
                "SELECT intent,intent_digest,raw FROM {s}.ingest_pending "
                "WHERE identity=%s",
                (identity,),
            ).fetchone()
            require(row is not None, "pending_original_missing")
            receipt, level = loads(row[0])
            raw = bytes(row[2])
            require(
                content_id(receipt.key) == identity
                and content_id((receipt, level)) == row[1],
                "pending_original_corrupt",
            )
        return self.retain(receipt, raw, level=level)

    def pending_inventory(self):
        self._idle()
        with self.connection.transaction():
            self._sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            self._identity()
            return self._sql(
                "SELECT identity,intent_digest,length(raw),state "
                "FROM {s}.ingest_pending ORDER BY identity"
            ).fetchall()


def simple_preflight(connection, run_id):
    """Read-only durability, role/TLS and observable backup facts; no config edits."""
    report = preflight(connection)
    schema = schema_for(run_id)
    with connection.transaction():
        connection.execute("SET TRANSACTION READ ONLY")
        row = connection.execute(
            "SELECT current_setting('fsync'), current_setting('synchronous_commit'), "
            "current_setting('full_page_writes'), current_setting('archive_mode'), "
            "current_setting('wal_level'), pg_is_in_recovery(), "
            "to_regnamespace(%s)::text",
            (schema,),
        ).fetchone()
        require(
            row[:3] == ("on", "on", "on") and not row[5], "postgres_durability_mismatch"
        )
        backup = connection.execute(
            "SELECT archived_count,last_archived_time,failed_count,last_failed_time "
            "FROM pg_stat_archiver"
        ).fetchone()
        role = connection.execute(
            "SELECT rolcreatedb,rolreplication,rolbypassrls "
            "FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        namespace = connection.execute(
            "SELECT pg_get_userbyid(nspowner),nspacl::text "
            "FROM pg_namespace WHERE nspname=%s",
            (schema,),
        ).fetchone()
        drift = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM pg_namespace n WHERE nspname=%s AND "
            "(nspowner<>(SELECT oid FROM pg_roles WHERE rolname=current_user) OR "
            "EXISTS (SELECT 1 FROM aclexplode("
            "coalesce(nspacl,acldefault('n',nspowner))) "
            "a WHERE a.grantee<>nspowner))), "
            "(SELECT count(*) FROM pg_class c JOIN pg_namespace n "
            "ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relkind IN ('r','p') AND "
            "(c.relowner<>(SELECT oid FROM pg_roles WHERE rolname=current_user) OR "
            "EXISTS (SELECT 1 FROM aclexplode("
            "coalesce(c.relacl,acldefault('r',c.relowner))) "
            "a WHERE a.grantee<>c.relowner))), "
            "(SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s "
            "AND a.attacl IS NOT NULL AND EXISTS (SELECT 1 FROM aclexplode(a.attacl) x "
            "WHERE x.grantee<>c.relowner))",
            (schema, schema, schema),
        ).fetchone()
        require(not any(role) and drift == (0, 0, 0), "postgres_permission_drift")
        tls = connection.execute(
            "SELECT version,cipher FROM pg_stat_ssl WHERE pid=pg_backend_pid()"
        ).fetchone()
    report.update(
        schema=schema,
        namespace_exists=row[6] is not None,
        namespace_owner=namespace[0] if namespace else None,
        namespace_acl=namespace[1] if namespace else None,
        fsync=row[0],
        synchronous_commit=row[1],
        full_page_writes=row[2],
        archive_mode=row[3],
        wal_level=row[4],
        primary=not row[5],
        archive_statistics={
            "archived_count": backup[0],
            "last_archived_at": str(backup[1]),
            "failed_count": backup[2],
            "last_failed_at": str(backup[3]),
        },
        role_attributes=dict(zip(("createdb", "replication", "bypassrls"), role)),
        namespace_table_column_acl="ISOLATED" if namespace else "NOT_CREATED",
        tls_protocol=tls[0],
        tls_cipher=tls[1],
        backup_pitr="UNKNOWN_NO_RESTORE_PROOF",
    )
    return report
