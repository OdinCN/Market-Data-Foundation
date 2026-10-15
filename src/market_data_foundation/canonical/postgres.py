"""Bounded PostgreSQL ledger for MDF P3 canonical-kernel integration."""

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus

from .codec import dumps, loads
from .model import (
    DatasetVersion,
    Key,
    Kind,
    Publication,
    PublicationRequest,
    Record,
    digest,
    require,
)
from .repository import prepare_publication
from .retained_restore import (
    RestoreBinding,
    RetainedExactGraph,
    check_restore_target,
    validate_exact_graph,
)

_SCHEMA = re.compile(
    r"^(?:mdf_p3_canonical_kernel_r1_[a-z0-9_]+|"
    r"mdf_p4_raw_daily_bars_replay_r1|"
    r"mdf_p4_tdx_daily_bars_live_admission_r1|"
    r"mdf_p4_tdx_daily_bars_paged_backfill_r1|"
    r"mdf_p4_macro_bls_cpi_direct_admission_r1|"
    r"mdf_p4_macro_bls_cpi_direct_live_r1|"
    r"mdf_p4_bls_cpi_garage_material_r1|"
    r"mdf_p4_macro_bls_cpi_retained_handoff_r1|"
    r"mdf_local_first_cpi_operable_r1|"
    r"mdf_p6_consumer_read_r1|"
    r"mdf_p6_real_cognition_data_readiness_r1|"
    r"mdf_macro_bls_ppi_vertical_r1)$"
)


def validate_schema(schema: str) -> str:
    require(bool(_SCHEMA.fullmatch(schema)), "unsafe_postgres_namespace")
    return schema


class PostgresLedger:
    """Caller owns ordinary writes; publication owns an idle connection's commit."""

    def __init__(self, connection: psycopg.Connection, schema: str) -> None:
        self.connection = connection
        self.schema = validate_schema(schema)

    def operator_reads(self):
        """Create an operator read session; caller supplies snapshot consistency."""
        from .operator_reads import PostgresOperatorReads

        return PostgresOperatorReads(self.connection, self.schema)

    def operator_snapshot(self):
        """Own a read-only repeatable-read scope on an exclusively held connection."""
        from .operator_snapshot import operator_snapshot

        return operator_snapshot(self.connection, self.schema)

    def namespace_exists(self) -> bool:
        row = self.connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)",
            (self.schema,),
        ).fetchone()
        return bool(row[0])

    def create_namespace(self) -> None:
        require(not self.namespace_exists(), "postgres_namespace_exists")
        schema = sql.Identifier(self.schema)
        self.connection.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        self.connection.execute(
            sql.SQL(
                """
                CREATE TABLE {}.records (
                    kind text NOT NULL,
                    identity text NOT NULL,
                    revision text NOT NULL,
                    payload text NOT NULL,
                    PRIMARY KEY (kind, identity, revision)
                )
                """
            ).format(schema)
        )
        self.connection.execute(
            sql.SQL(
                """
                CREATE TABLE {}.contents (
                    version_identity text PRIMARY KEY,
                    digest text NOT NULL,
                    payload bytea NOT NULL
                )
                """
            ).format(schema)
        )
        self.connection.execute(
            sql.SQL(
                """
                CREATE TABLE {}.publications (
                    identity text PRIMARY KEY,
                    occurrence text NOT NULL UNIQUE,
                    version_identity text NOT NULL,
                    decision_at timestamptz NOT NULL,
                    payload text NOT NULL
                )
                """
            ).format(schema)
        )

    def drop_namespace(self) -> None:
        require(self.namespace_exists(), "postgres_namespace_missing")
        self.connection.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema))
        )

    def server_now(self) -> datetime:
        row = self.connection.execute("SELECT clock_timestamp()").fetchone()
        return row[0]

    def get(self, key: Key) -> Record | None:
        if key.kind == Kind.PUBLICATION:
            row = self.connection.execute(
                sql.SQL(
                    "SELECT payload FROM {}.publications WHERE identity = %s"
                ).format(sql.Identifier(self.schema)),
                (key.identity,),
            ).fetchone()
        else:
            row = self.connection.execute(
                sql.SQL(
                    """
                    SELECT payload
                    FROM {}.records
                    WHERE kind = %s AND identity = %s AND revision = %s
                    """
                ).format(sql.Identifier(self.schema)),
                (key.kind.value, key.identity, key.revision),
            ).fetchone()
        if row is None:
            return None
        value = loads(row[0])
        require(hasattr(value, "key"), "stored_record_invalid")
        return value

    def put(self, record: Record) -> Record:
        require(not isinstance(record, Publication), "publication_requires_publish_api")
        payload = dumps(record)
        key = record.key
        self.connection.execute(
            sql.SQL(
                """
                INSERT INTO {}.records (kind, identity, revision, payload)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """
            ).format(sql.Identifier(self.schema)),
            (key.kind.value, key.identity, key.revision, payload),
        )
        stored = self.get(key)
        require(stored is not None and stored == record, "canonical_identity_conflict")
        return stored

    def publish(
        self, request: PublicationRequest, clock: Callable[[], datetime]
    ) -> Publication:
        """Commit a server-timed decision before returning; never join caller work.

        A commit/transport error returns no Publication. Reconcile the same
        occurrence on a fresh connection; do not invent a replacement identity.
        """
        require(
            self.connection.info.transaction_status == TransactionStatus.IDLE,
            "publication_connection_not_idle",
        )
        with self.connection.transaction():
            # This bounded ledger serializes decision chains and occurrence retries.
            # Acquire protection before sampling time, including after lock waits.
            self.connection.execute(
                sql.SQL(
                    "LOCK TABLE {}.publications IN SHARE ROW EXCLUSIVE MODE"
                ).format(sql.Identifier(self.schema))
            )
            existing = self.publication_by_occurrence(request.occurrence)
            if existing is not None:
                require(existing.request == request, "occurrence_identity_conflict")
                publication = existing
            else:
                publication = prepare_publication(self, request, self.server_now)
                # All lineage/evidence checks precede the final server timestamp.
                decision_at = self.server_now()
                usable_at = request.usable_at or decision_at
                require(usable_at >= decision_at, "usable_before_decision")
                publication = replace(
                    publication, decision_at=decision_at, usable_at=usable_at
                )
                self.connection.execute(
                    sql.SQL("""
                        INSERT INTO {}.publications
                            (identity, occurrence, version_identity,
                             decision_at, payload)
                        VALUES (%s, %s, %s, %s, %s)
                    """).format(sql.Identifier(self.schema)),
                    (
                        request.identity,
                        request.occurrence,
                        request.version.identity,
                        decision_at,
                        dumps(publication),
                    ),
                )
        # Exiting the outermost transaction confirms COMMIT. Nothing is returned
        # from inside it, and a caller commit/rollback cannot affect this decision.
        return publication

    def restore_exact_graph(
        self,
        graph: RetainedExactGraph,
        binding: RestoreBinding,
        readback: bytes,
    ) -> Publication:
        """Atomically import one approved historical graph on an idle connection.

        Caller must first approve the retained proof window and its digests.
        There is no new decision clock or durability/availability claim. A
        commit/transport error returns no Publication; retry the identical
        bound graph on a fresh connection to reconcile the original occurrence.
        """
        require(
            self.connection.info.transaction_status == TransactionStatus.IDLE,
            "restore_connection_not_idle",
        )
        candidate = validate_exact_graph(graph, binding, readback)
        with self.connection.transaction():
            self.connection.execute(
                sql.SQL(
                    "LOCK TABLE {}.records, {}.contents, {}.publications "
                    "IN SHARE ROW EXCLUSIVE MODE"
                ).format(*[sql.Identifier(self.schema)] * 3)
            )
            publication = graph.publication
            expected = (
                publication.request.identity,
                publication.request.occurrence,
                publication.request.version.identity,
                publication.decision_at,
                dumps(publication),
            )
            rows = self.connection.execute(
                sql.SQL("""
                    SELECT identity, occurrence, version_identity, decision_at, payload
                    FROM {}.publications
                    WHERE identity = %s OR occurrence = %s OR version_identity = %s
                """).format(sql.Identifier(self.schema)),
                expected[:3],
            ).fetchall()
            require(
                rows == [] or rows == [expected],
                "restore_publication_index_conflict",
            )
            check_restore_target(self, graph)
            for record in graph.records:
                self.put(record)
            self.put_content(candidate.get(graph.version), graph.content)
            if self.get(graph.publication.key) is None:
                self.connection.execute(
                    sql.SQL("""
                        INSERT INTO {}.publications
                            (identity, occurrence, version_identity,
                             decision_at, payload)
                        VALUES (%s, %s, %s, %s, %s)
                    """).format(sql.Identifier(self.schema)),
                    expected,
                )
        return graph.publication

    def put_content(self, version: DatasetVersion, content: bytes) -> None:
        require(type(content) is bytes, "mutable_content")
        require(digest(content) == version.content_digest, "content_digest_mismatch")
        self.connection.execute(
            sql.SQL(
                """
                INSERT INTO {}.contents (version_identity, digest, payload)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """
            ).format(sql.Identifier(self.schema)),
            (version.identity, version.content_digest, content),
        )
        row = self.connection.execute(
            sql.SQL(
                """
                SELECT digest, payload
                FROM {}.contents
                WHERE version_identity = %s
                """
            ).format(sql.Identifier(self.schema)),
            (version.identity,),
        ).fetchone()
        require(row is not None, "version_content_missing")
        stored = bytes(row[1])
        require(
            row[0] == version.content_digest
            and stored == content
            and digest(stored) == version.content_digest,
            "version_content_conflict",
        )

    def get_content(self, version: Key) -> bytes | None:
        require(version.kind == Kind.VERSION, "content_key_kind")
        row = self.connection.execute(
            sql.SQL(
                """
                SELECT digest, payload
                FROM {}.contents
                WHERE version_identity = %s
                """
            ).format(sql.Identifier(self.schema)),
            (version.identity,),
        ).fetchone()
        if row is None:
            return None
        content = bytes(row[1])
        require(digest(content) == row[0], "stored_content_mismatch")
        return content

    def publication_by_occurrence(self, occurrence: str) -> Publication | None:
        row = self.connection.execute(
            sql.SQL("SELECT payload FROM {}.publications WHERE occurrence = %s").format(
                sql.Identifier(self.schema)
            ),
            (occurrence,),
        ).fetchone()
        if row is None:
            return None
        value = loads(row[0])
        require(isinstance(value, Publication), "stored_publication_invalid")
        return value

    def publications_for_version(self, version: Key) -> tuple[Publication, ...]:
        require(version.kind == Kind.VERSION, "publication_version_kind")
        rows = self.connection.execute(
            sql.SQL(
                """
                SELECT payload
                FROM {}.publications
                WHERE version_identity = %s
                ORDER BY decision_at, identity
                """
            ).format(sql.Identifier(self.schema)),
            (version.identity,),
        ).fetchall()
        values = tuple(loads(row[0]) for row in rows)
        require(
            all(isinstance(value, Publication) for value in values),
            "stored_publication_invalid",
        )
        return values

    def versions_for_definition(self, definition: Key) -> tuple[DatasetVersion, ...]:
        require(definition.kind == Kind.DEFINITION, "version_definition_kind")
        rows = self.connection.execute(
            sql.SQL(
                """
                SELECT payload
                FROM {}.records
                WHERE kind = %s
                ORDER BY identity, revision
                """
            ).format(sql.Identifier(self.schema)),
            (Kind.VERSION.value,),
        ).fetchall()
        candidates = tuple(loads(row[0]) for row in rows)
        require(
            all(isinstance(value, DatasetVersion) for value in candidates),
            "stored_version_invalid",
        )
        return tuple(value for value in candidates if value.definition == definition)
