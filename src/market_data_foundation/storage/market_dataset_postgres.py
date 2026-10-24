"""Dedicated DEV SQL writer: atomic facts/projections, no IO on import.

The existing canonical PostgresLedger and its allowlist are deliberately untouched.
This candidate has no production admission path. Reader transactions are explicit.
"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC
from pathlib import Path

import psycopg
from psycopg.pq import TransactionStatus

from market_data_foundation.canonical.market_material import (
    MaterialReceipt,
    byte_digest,
    read_material,
)
from market_data_foundation.canonical.market_persistence import (
    CommitReceipt,
    CommitUnknown,
    Fault,
    MemoryMarketLedger,
    Verification,
    inject,
)
from market_data_foundation.canonical.market_projection import (
    AdmissionGuard,
    IndexGeneration,
    VersionGuard,
    candidate,
    version_metadata,
    version_slots,
)
from market_data_foundation.canonical.market_publication import (
    MarketPublication,
    publication_head,
    validate_history,
)
from market_data_foundation.canonical.model import ContractError, Decision, require
from market_data_foundation.domain_contracts.market_universe import (
    canonical_bytes,
    content_id,
)

from .market_dataset_codec import dumps, loads
from .market_dataset_garage import RUN
from .postgres_runtime_config import DEFAULT_RUNTIME_PATH, load_pg_config

SCHEMA = "mdf_market_dataset_dev_r1"
SCHEMA_REVISION = "market.dataset.physical.r1"
BINDING = "mdf.market.dataset.dev.r1"
DDL_PATH = (
    Path(__file__).resolve().parents[3] / "db/market_dataset/001_market_dataset.sql"
)
LOCK_ID = 7163301503


def utc(value):
    return value.astimezone(UTC).isoformat() if value is not None else None


def slot_args(slot):
    return (
        content_id(slot.definition),
        slot.instrument.namespace,
        slot.instrument.instrument_id,
        slot.day.isoformat(),
    )


SLOT_WHERE = (
    "definition_id=%s AND instrument_namespace=%s "
    "AND instrument_id=%s AND local_date=%s"
)


def connect_dev(password: str, *, runtime_path: Path = DEFAULT_RUNTIME_PATH):
    """Exact server/db/role/TLS binding; no DSN, env target or service fallback."""
    require(bool(password), "postgres_credential_missing")
    profile = load_pg_config(runtime_path)
    return psycopg.connect(
        **profile.connection_kwargs(),
        password=password,
        connect_timeout=5,
        autocommit=True,
        options="-c statement_timeout=5000 -c lock_timeout=1000 -c timezone=UTC",
    )


def preflight(connection):
    """Read-only identity/ACL intake, before any namespace or object writes."""
    require(
        connection.info.transaction_status == TransactionStatus.IDLE,
        "idle_connection_required",
    )
    with connection.transaction():
        connection.execute("SET TRANSACTION READ ONLY")
        row = connection.execute(
            "SELECT current_database(), current_user, session_user, "
            "host(inet_server_addr()), inet_server_port(), "
            "(SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()), "
            "has_database_privilege(current_user,current_database(),'CREATE'), "
            "(SELECT rolsuper OR rolcreaterole FROM pg_roles "
            "WHERE rolname=current_user)"
        ).fetchone()
        require(
            row[:6]
            == (
                "market_data_foundation_dev",
                "market_data_foundation_dev",
                "market_data_foundation_dev",
                "192.168.10.4",
                5432,
                True,
            ),
            "postgres_identity_or_tls_mismatch",
        )
        require(row[6] and not row[7], "postgres_role_privilege_mismatch")
        inherited_grants = connection.execute(
            "SELECT count(*) FROM pg_default_acl d, "
            "LATERAL aclexplode(d.defaclacl) a "
            "WHERE d.defaclrole=(SELECT oid FROM pg_roles WHERE rolname=current_user) "
            "AND d.defaclnamespace=0 "
            "AND d.defaclobjtype IN ('r','n') AND a.grantee<>d.defaclrole"
        ).fetchone()[0]
        require(inherited_grants == 0, "postgres_default_acl_not_isolated")
        schemas = connection.execute(
            "SELECT nspname, pg_get_userbyid(nspowner), nspacl::text "
            "FROM pg_namespace WHERE nspname=%s",
            (SCHEMA,),
        ).fetchall()
        return {
            "identity_tls": "VERIFIED",
            "create_schema": True,
            "namespace_exists": bool(schemas),
            "namespace_owner": schemas[0][1] if schemas else None,
            "namespace_acl": schemas[0][2] if schemas else None,
            "backup_pitr": "UNKNOWN",
            "default_acl_isolated": True,
        }


def migrate(connection, run_id: str):
    require(RUN.fullmatch(run_id) is not None, "invalid_probe_run")
    intake = preflight(connection)
    require(not intake["namespace_exists"], "postgres_namespace_conflict")
    ddl = DDL_PATH.read_text()
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        connection.execute(f"CREATE SCHEMA {SCHEMA}")
        connection.execute(f"REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC")
        for statement in ddl.split(";"):
            if statement.strip():
                connection.execute(statement.replace("{s}", SCHEMA))
        connection.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM PUBLIC")
        connection.execute(
            f"INSERT INTO {SCHEMA}.schema_metadata "
            "VALUES (1,%s,%s,%s,%s,clock_timestamp())",
            (SCHEMA_REVISION, byte_digest(ddl.encode()), BINDING, run_id),
        )
        connection.execute(
            f"INSERT INTO {SCHEMA}.projection_generations VALUES (%s,%s,true)",
            ("initial", dumps(IndexGeneration(BINDING, "initial", False))),
        )
    return {
        "schema_revision": SCHEMA_REVISION,
        "ddl_sha256": byte_digest(ddl.encode()),
        "owner_run": run_id,
    }


class _ReadbackOnlyStore:
    def __init__(self, store):
        self.store = store

    def put(self, locator, raw):
        require(self.store.read(locator, len(raw)) == raw, "material_readback_mismatch")

    def read(self, locator, maximum):
        return self.store.read(locator, maximum)


class PostgresMarketDataset:
    """One dedicated connection; caller may not nest transactions or share it.

    A schema-wide writer lock is intentional for the bounded DEV candidate.
    It also serializes generation changes with every Version/Publication commit.
    """

    def __init__(self, connection, store, decoders=(), *, owner_run):
        require(RUN.fullmatch(owner_run) is not None, "invalid_probe_run")
        self.connection, self.store = connection, store
        self.decoders, self.owner_run = tuple(decoders), owner_run

    def _sql(self, query, args=()):
        try:
            return self.connection.execute(query.replace("{s}", SCHEMA), args)
        except psycopg.Error:
            raise ContractError("market_postgres_statement_failed") from None

    def _identity(self):
        row = self._sql(
            "SELECT schema_revision, ddl_digest, ledger_binding, owner_run "
            "FROM {s}.schema_metadata WHERE singleton=1"
        ).fetchone()
        require(
            row
            == (
                SCHEMA_REVISION,
                byte_digest(DDL_PATH.read_bytes()),
                BINDING,
                self.owner_run,
            ),
            "physical_ledger_binding_mismatch",
        )

    def _idle(self):
        require(
            self.connection.info.transaction_status == TransactionStatus.IDLE,
            "idle_connection_required",
        )

    @contextmanager
    def _write(self):
        self._idle()
        with self.connection.transaction():
            self._sql("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
            self._identity()
            yield

    def now(self):
        return self._sql("SELECT clock_timestamp()").fetchone()[0]

    def _record(self, table, identity, *, missing=False):
        require(
            table in ("materials", "versions", "publication_events"), "record_table"
        )
        row = self._sql(
            f"SELECT payload,payload_digest FROM {{s}}.{table} WHERE id=%s", (identity,)
        ).fetchone()
        if row is None and missing:
            return None
        require(row is not None, "canonical_record_missing")
        value = loads(row[0])
        require(
            content_id(value) == row[1] and content_id(value.key) == identity,
            "canonical_record_digest_mismatch",
        )
        return value

    def _receipt(self, operation, identity):
        row = self._sql(
            "SELECT payload_digest,result_digest FROM {s}.commit_receipts "
            "WHERE operation=%s AND identity=%s",
            (operation, identity),
        ).fetchone()
        return CommitReceipt(operation, identity, *row) if row else None

    def _transaction(self, operation, identity, payload, mutate, fault=None):
        digest = content_id(payload)
        entering_commit = False
        try:
            with self._write():
                old = self._receipt(operation, identity)
                if old:
                    require(
                        old.payload_digest == digest, "same_identity_different_content"
                    )
                    return old
                result = mutate()
                inject(fault, Fault.BEFORE_COMMIT)
                receipt = CommitReceipt(operation, identity, digest, content_id(result))
                self._sql(
                    "INSERT INTO {s}.commit_receipts VALUES (%s,%s,%s,%s)",
                    (operation, identity, digest, receipt.result_digest),
                )
                entering_commit = True
        except Exception:
            if entering_commit:
                raise CommitUnknown("postgres_commit_acknowledgement_unknown") from None
            raise
        if fault == Fault.AFTER_COMMIT:
            raise CommitUnknown("postgres_commit_acknowledgement_unknown")
        return receipt

    def verify(self, operation, identity, payload_digest):
        try:
            self._idle()
            with self.connection.transaction():
                self._sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                self._identity()
                receipt = self._receipt(operation, identity)
                if receipt is None:
                    return Verification.ABSENT
                return (
                    Verification.COMMITTED
                    if receipt.payload_digest == payload_digest
                    else Verification.CONFLICT
                )
        except Exception:
            return Verification.UNKNOWN

    @staticmethod
    def material_intent(
        receipt: MaterialReceipt, *, level: str
    ) -> tuple[MaterialReceipt, str]:
        """Freeze the physical payload before retain; no storage or ACK needed.

        Save this tuple, call retain(intent[0], raw, level=intent[1]), then use
        I1 verify_original(writer, intent) even if retain raises CommitUnknown.
        """
        require(isinstance(receipt, MaterialReceipt), "material_receipt_required")
        require(
            type(level) is str
            and level in ("raw", "source-projection", "status", "metadata"),
            "material_level",
        )
        return receipt, level

    def _prepare_material(self, receipt, raw):
        self.store.put(receipt.locator, raw)
        read_material(self.store, receipt)

    def retain(self, receipt, raw, *, level="raw", fault=None):
        intent = self.material_intent(receipt, level=level)
        require(
            type(raw) is bytes
            and len(raw) == receipt.byte_length <= 8 * 1024 * 1024
            and byte_digest(raw) == receipt.digest,
            "material_bytes_mismatch",
        )
        if receipt.locator.startswith("s3://"):
            semantic = content_id(
                (
                    receipt.mapping_revision,
                    receipt.upstream_revision,
                    receipt.decode_revision,
                )
            )
            suffix = (
                f"/{level}/{content_id(receipt.source)}/"
                f"{content_id(receipt.binding)}/{semantic}/sha256/{receipt.digest}"
            )
            require(
                receipt.locator.endswith(suffix), "material_semantic_address_mismatch"
            )

        def mutate():
            self._prepare_material(receipt, raw)
            self._sql(
                "INSERT INTO {s}.materials VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    content_id(receipt.key),
                    dumps(receipt),
                    content_id(receipt),
                    receipt.job,
                    receipt.shard,
                    receipt.attempt,
                    receipt.request,
                    content_id(receipt.source),
                    content_id(receipt.binding),
                    receipt.mapping_revision,
                    level,
                    receipt.locator,
                    receipt.digest,
                    receipt.byte_length,
                    utc(receipt.requested_at),
                    utc(receipt.obtained_at),
                    utc(receipt.received_at),
                    utc(receipt.known_at),
                ),
            )
            inject(fault, Fault.AFTER_CANONICAL)
            return receipt

        # Level is additional physical metadata, bound into this port's receipt.
        return self._transaction(
            "material", content_id(receipt.key), intent, mutate, fault
        )

    def _validate_version(self, version):
        # Reuse D3's complete decoder/frame/clock/admission-subject validation.
        ledger = MemoryMarketLedger(
            BINDING, _ReadbackOnlyStore(self.store), self.decoders, self.now
        )
        for receipt in version.assessment.materials:
            actual = self._record("materials", content_id(receipt.key))
            require(
                canonical_bytes(actual) == canonical_bytes(receipt),
                "canonical_material_missing",
            )
            level = self._sql(
                "SELECT material_level FROM {s}.materials WHERE id=%s",
                (content_id(receipt.key),),
            ).fetchone()[0]
            committed = self._receipt("material", content_id(receipt.key))
            require(
                committed is not None
                and committed.payload_digest == content_id((receipt, level))
                and committed.result_digest == content_id(receipt),
                "canonical_material_receipt_mismatch",
            )
            ledger.retain(receipt, read_material(self.store, receipt))
        ledger.commit_version(version)

    def _refresh_slot(self, slot):
        args = slot_args(slot)
        count = self._sql(
            "SELECT count(*) FROM {s}.projection_rows WHERE " + SLOT_WHERE, args
        ).fetchone()[0]
        digest = None
        if count <= 64:
            rows = self._sql(
                "SELECT payload FROM {s}.projection_rows WHERE " + SLOT_WHERE, args
            ).fetchall()
            records = tuple(
                sorted(
                    (loads(row[0]) for row in rows),
                    key=lambda r: canonical_bytes(r.version),
                )
            )
            digest = content_id(records)
        self._sql(
            "INSERT INTO {s}.projection_slots VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (definition_id,instrument_namespace,instrument_id,local_date) "
            "DO UPDATE SET candidate_count=excluded.candidate_count, "
            "candidate_digest=excluded.candidate_digest,overflow=excluded.overflow",
            (*args, count, digest, count > 64),
        )

    def _project_version(self, version):
        identity = content_id(version.key)
        self._sql(
            "INSERT INTO {s}.projection_versions VALUES (%s,%s)",
            (identity, dumps(version_metadata(version))),
        )
        for slot in version_slots(version):
            old = self._sql(
                "SELECT candidate_count,candidate_digest,overflow FROM "
                "{s}.projection_slots WHERE " + SLOT_WHERE,
                slot_args(slot),
            ).fetchone()
            count = self._sql(
                "SELECT count(*) FROM {s}.projection_rows WHERE " + SLOT_WHERE,
                slot_args(slot),
            ).fetchone()[0]
            require(
                (old is None and count == 0)
                or (
                    old is not None and old[0] == count and bool(old[2]) == (count > 64)
                ),
                "candidate_directory_inconsistent",
            )
            if old is not None and count <= 64:
                rows = self._sql(
                    "SELECT payload FROM {s}.projection_rows WHERE " + SLOT_WHERE,
                    slot_args(slot),
                ).fetchall()
                previous = tuple(
                    sorted(
                        (loads(row[0]) for row in rows),
                        key=lambda item: canonical_bytes(item.version),
                    )
                )
                require(
                    old[1] == content_id(previous), "candidate_directory_inconsistent"
                )
            self._sql(
                "INSERT INTO {s}.projection_rows VALUES (%s,%s,%s,%s,%s,%s)",
                (*slot_args(slot), identity, dumps(candidate(version))),
            )
            self._refresh_slot(slot)

    def commit_version(self, version, fault=None):
        def mutate():
            self._validate_version(version)
            identity = content_id(version.key)
            self._sql(
                "INSERT INTO {s}.versions VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    identity,
                    dumps(version),
                    content_id(version),
                    utc(version.frozen_at),
                    version.content_digest,
                    len(version.assessment.content),
                ),
            )
            for receipt in version.assessment.materials:
                self._sql(
                    "INSERT INTO {s}.version_materials VALUES (%s,%s)",
                    (identity, content_id(receipt.key)),
                )
            self._sql(
                "INSERT INTO {s}.publication_heads VALUES (%s,0,NULL,%s)",
                (identity, content_id(())),
            )
            inject(fault, Fault.AFTER_CANONICAL)
            self._project_version(version)
            self._sql(
                "INSERT INTO {s}.projection_guards VALUES (%s,%s)",
                (identity, dumps(VersionGuard(version.key, 0, content_id(()), 0))),
            )
            inject(fault, Fault.AFTER_PROJECTION)
            return version

        return self._transaction(
            "version", content_id(version.key), version, mutate, fault
        )

    def set_admission(self, subject, revision, allowed, fault=None):
        """DEV explicit fact only; no Source/production qualification is granted."""
        require(
            type(allowed) is bool and bool(subject) and bool(revision), "admission_fact"
        )

        def mutate():
            row = self._sql(
                "SELECT payload FROM {s}.admission_guards WHERE subject=%s", (subject,)
            ).fetchone()
            old = loads(row[0]) if row else None
            guard = AdmissionGuard(
                subject, revision, allowed, old.change + 1 if old else 1
            )
            self._sql(
                "INSERT INTO {s}.admission_guards VALUES (%s,%s) ON CONFLICT (subject) "
                "DO UPDATE SET payload=excluded.payload",
                (subject, dumps(guard)),
            )
            return guard

        return self._transaction(
            "admission",
            content_id((subject, revision)),
            (subject, revision, allowed),
            mutate,
            fault,
        )

    def _history(self, key, maximum=4096):
        identity = content_id(key)
        anchor = self._sql(
            "SELECT event_count,head_id,history_digest FROM "
            "{s}.publication_heads WHERE version_id=%s",
            (identity,),
        ).fetchone()
        guard = self._sql(
            "SELECT payload FROM {s}.projection_guards WHERE version_id=%s", (identity,)
        ).fetchone()
        require(anchor is not None and guard is not None, "publication_head_missing")
        require(0 <= anchor[0] <= maximum, "publication_history_capacity")
        guard = loads(guard[0])
        require(
            guard == VersionGuard(key, anchor[0], anchor[2], anchor[0]),
            "publication_head_mismatch",
        )
        rows = self._sql(
            "SELECT id FROM {s}.publication_events WHERE version_id=%s "
            "ORDER BY decision_at,id LIMIT %s",
            (identity, maximum + 1),
        ).fetchall()
        require(len(rows) == anchor[0], "publication_history_inconsistent")
        history = tuple(self._record("publication_events", row[0]) for row in rows)
        expected = publication_head(key, history)
        require(
            (
                expected.count,
                content_id(expected.head) if expected.head else None,
                expected.history_digest,
            )
            == anchor,
            "publication_history_inconsistent",
        )
        projected = self._sql(
            "SELECT payload FROM {s}.projection_publications "
            "WHERE version_id=%s ORDER BY decision_at,event_id LIMIT %s",
            (identity, maximum + 1),
        ).fetchall()
        require(
            tuple(loads(row[0]) for row in projected) == history,
            "publication_projection_inconsistent",
        )
        validate_history(key, history)
        return history

    def publish(self, request, fault=None):
        def mutate():
            version = self._record("versions", content_id(request.version))
            if request.decision in (Decision.PUBLISH, Decision.RESTORE):
                self._validate_version(version)
                for subject in version.admission_subjects:
                    row = self._sql(
                        "SELECT payload FROM {s}.admission_guards WHERE subject=%s",
                        (subject,),
                    ).fetchone()
                    require(row is not None, "publication_admission_denied")
                    guard = loads(row[0])
                    committed = self._receipt(
                        "admission", content_id((subject, guard.revision))
                    )
                    require(
                        guard.subject == subject
                        and guard.allowed
                        and guard.change > 0
                        and committed is not None
                        and committed.payload_digest
                        == content_id((subject, guard.revision, True))
                        and committed.result_digest == content_id(guard),
                        "publication_admission_denied",
                    )
            history = self._history(request.version)
            now = self.now()
            require(now >= version.frozen_at, "publication_before_version")
            event = MarketPublication(request, now, request.usable_at or now)
            history = (*history, event)
            validate_history(request.version, history)
            identity, version_id = content_id(event.key), content_id(request.version)
            self._sql(
                "INSERT INTO {s}.publication_events VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    identity,
                    request.occurrence,
                    version_id,
                    utc(now),
                    utc(event.usable_at),
                    utc(request.effective_at),
                    dumps(event),
                    content_id(event),
                ),
            )
            head = publication_head(request.version, history)
            self._sql(
                "UPDATE {s}.publication_heads SET event_count=%s,head_id=%s,"
                "history_digest=%s WHERE version_id=%s",
                (head.count, identity, head.history_digest, version_id),
            )
            inject(fault, Fault.AFTER_CANONICAL)
            self._sql(
                "INSERT INTO {s}.projection_publications VALUES (%s,%s,%s,%s)",
                (identity, version_id, utc(now), dumps(event)),
            )
            self._sql(
                "UPDATE {s}.projection_guards SET payload=%s WHERE version_id=%s",
                (
                    dumps(
                        VersionGuard(
                            request.version, head.count, head.history_digest, head.count
                        )
                    ),
                    version_id,
                ),
            )
            inject(fault, Fault.AFTER_PROJECTION)
            return event

        return self._transaction(
            "publication", content_id(request.key), request, mutate, fault
        )

    @contextmanager
    def read_snapshot(self, expected_generation=None):
        from .market_dataset_reader import PostgresMarketView

        self._idle()
        with self.connection.transaction():
            self._sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            self._identity()
            view = PostgresMarketView(self, expected_generation)
            try:
                yield view
            finally:
                view.closed = True

    def rebuild(self, generation, covered_months, fault=None):
        """Explicit bounded stop-writes repair; never called by a Reader.

        Development capacity: 256 Versions, 4096 Publications, 448 months.
        All original facts and receipts remain, including unused generations.
        """
        require(bool(generation) and len(generation) <= 128, "generation_required")
        require(
            len(covered_months) <= 448
            and len(set(covered_months)) == len(covered_months),
            "rebuild_month_capacity",
        )

        def mutate():
            require(
                self._sql(
                    "SELECT 1 FROM {s}.projection_generations WHERE generation=%s",
                    (generation,),
                ).fetchone()
                is None,
                "new_generation_required",
            )
            ids = self._sql(
                "SELECT id FROM {s}.versions ORDER BY id LIMIT 257"
            ).fetchall()
            require(len(ids) <= 256, "rebuild_version_capacity")
            versions = tuple(self._record("versions", row[0]) for row in ids)
            events = self._sql(
                "SELECT id FROM {s}.publication_events "
                "ORDER BY decision_at,id LIMIT 4097"
            ).fetchall()
            require(len(events) <= 4096, "rebuild_publication_capacity")
            events = tuple(self._record("publication_events", row[0]) for row in events)
            # Bidirectional receipt checks stop a missing tail being repaired away.
            receipts = self._sql(
                "SELECT operation,identity,payload_digest,result_digest FROM "
                "{s}.commit_receipts WHERE operation IN ('version','publication') "
                "LIMIT 4353"
            ).fetchall()
            expected = {
                ("version", content_id(v.key), content_id(v), content_id(v))
                for v in versions
            }
            expected.update(
                ("publication", content_id(e.key), content_id(e.request), content_id(e))
                for e in events
            )
            require(set(receipts) == expected, "canonical_receipt_graph_mismatch")
            for version in versions:
                self._validate_version(version)
                references = self._sql(
                    "SELECT material_id FROM {s}.version_materials WHERE version_id=%s",
                    (content_id(version.key),),
                ).fetchall()
                require(
                    {row[0] for row in references}
                    == {content_id(r.key) for r in version.assessment.materials},
                    "canonical_material_reference_mismatch",
                )
                history = tuple(e for e in events if e.request.version == version.key)
                validate_history(version.key, history)
                require(
                    all(
                        version.frozen_at <= e.decision_at <= self.now()
                        for e in history
                    ),
                    "canonical_publication_clock_mismatch",
                )
            for table in (
                "projection_rows",
                "projection_slots",
                "projection_months",
                "projection_versions",
                "projection_publications",
                "projection_guards",
            ):
                self._sql(f"DELETE FROM {{s}}.{table}")
            for month in covered_months:
                self._sql(
                    "INSERT INTO {s}.projection_months VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        content_id(month.definition),
                        month.instrument.namespace,
                        month.instrument.instrument_id,
                        month.year,
                        month.month,
                        dumps(month),
                    ),
                )
            for version in versions:
                self._project_version(version)
                history = tuple(e for e in events if e.request.version == version.key)
                validate_history(version.key, history)
                head = publication_head(version.key, history)
                self._sql(
                    "INSERT INTO {s}.publication_heads VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (version_id) DO UPDATE SET "
                    "event_count=excluded.event_count,"
                    "head_id=excluded.head_id,history_digest=excluded.history_digest",
                    (
                        content_id(version.key),
                        head.count,
                        content_id(head.head) if head.head else None,
                        head.history_digest,
                    ),
                )
                self._sql(
                    "INSERT INTO {s}.projection_guards VALUES (%s,%s)",
                    (
                        content_id(version.key),
                        dumps(
                            VersionGuard(
                                version.key, head.count, head.history_digest, head.count
                            )
                        ),
                    ),
                )
            for event in events:
                self._sql(
                    "INSERT INTO {s}.projection_publications VALUES (%s,%s,%s,%s)",
                    (
                        content_id(event.key),
                        content_id(event.request.version),
                        utc(event.decision_at),
                        dumps(event),
                    ),
                )
            inject(fault, Fault.AFTER_PROJECTION)
            self._sql("UPDATE {s}.projection_generations SET active=false WHERE active")
            value = IndexGeneration(BINDING, generation, True)
            self._sql(
                "INSERT INTO {s}.projection_generations VALUES (%s,%s,true)",
                (generation, dumps(value)),
            )
            return value

        return self._transaction(
            "rebuild", generation, (generation, covered_months), mutate, fault
        )

    def suspend(self):
        with self._write():
            row = self._sql(
                "SELECT generation,payload FROM {s}.projection_generations WHERE active"
            ).fetchone()
            self._sql(
                "UPDATE {s}.projection_generations SET payload=%s WHERE generation=%s",
                (dumps(replace(loads(row[1]), ready=False)), row[0]),
            )

    def cleanup_probe_schema(self):
        """No CASCADE. Exact owned tables only; unexpected dependencies stop cleanup."""
        with self._write():
            tables = (
                "projection_guards",
                "projection_publications",
                "projection_rows",
                "projection_slots",
                "projection_months",
                "projection_versions",
                "projection_generations",
                "admission_guards",
                "commit_receipts",
                "publication_heads",
                "publication_events",
                "version_materials",
                "versions",
                "materials",
                "schema_metadata",
            )
            for table in tables:
                self._sql(f"DROP TABLE {{s}}.{table}")
            self._sql("DROP SCHEMA {s}")
