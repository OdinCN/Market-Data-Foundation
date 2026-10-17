"""T08-only protected raw handoff, publication-last and read-only reconciliation.

No service configuration or connections are created here. Caller injects the
existing Garage/devPG capabilities after the independent gates. Intent is durable
before effects; a consumed provider handoff never implicitly retries storage.
"""

import base64
import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime

from psycopg.pq import TransactionStatus

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import digest, require
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.canonical.repository import InMemoryLedger

from .bls_cpi_material_store import (
    GarageMaterialStore,
    MaterialIdentity,
    MaterialWriteOutcomeUnknown,
    PostgresAdvisoryWriterProtection,
)
from .sdmx_contract import MAX_ENTITY_BYTES, definition_for
from .sdmx_official import (
    SdmxPlan,
    build_plan,
    candidate_from_content,
    validate_plan,
)
from .sdmx_source import exclusive, write_json

SCHEMA = "mdf_macro_sdmx_t08_r1"
DEV_HOST = "192.168.10.4"


@dataclass(frozen=True)
class SdmxMaterialIdentity(MaterialIdentity):
    """Separate T08 object prefix; never addresses existing CPI/FOMC/GDP keys."""

    def __post_init__(self):
        MaterialIdentity(self.sha256, 1, self.bucket)  # Existing digest/bucket gate.
        require(
            type(self.size) is int and 0 < self.size <= MAX_ENTITY_BYTES,
            "sdmx_material_bound",
        )
        expected = f"t08-sdmx/materials/sha256/{self.sha256}/raw-response.bin"
        require(not self.key or self.key == expected, "sdmx_material_key_invalid")
        object.__setattr__(self, "key", expected)


def raw_identities(plan):
    return tuple(
        SdmxMaterialIdentity(digest(r.raw), len(r.raw))
        for r in candidate_from_content(plan.content).entities
    )


def durable_plan(plan: SdmxPlan):
    validate_plan(plan)
    return replace(
        plan,
        raw_materials=tuple(
            replace(m, controlled_ref=i.locator, durable=True, replayable=True)
            for m, i in zip(plan.raw_materials, raw_identities(plan), strict=True)
        ),
    )


def _records(plan):
    return (
        plan.source,
        definition_for(candidate_from_content(plan.content).provider),
        plan.binding_candidate,
        plan.run,
        *plan.acquisitions,
        *plan.raw_materials,
        plan.semantic_material,
    )


def _check_ledger(ledger):
    if isinstance(ledger, PostgresLedger):
        require(
            ledger.schema == SCHEMA and ledger.connection.info.host == DEV_HOST,
            "sdmx_devpg_scope_required",
        )
    else:
        require(isinstance(ledger, InMemoryLedger), "sdmx_ledger_not_admitted")


def _session(ledger):
    _check_ledger(ledger)
    return (
        {
            "kind": "postgres",
            "pid": ledger.connection.info.backend_pid,
            "host": DEV_HOST,
            "schema": SCHEMA,
            "database": ledger.connection.info.dbname,
            "port": ledger.connection.info.port,
        }
        if isinstance(ledger, PostgresLedger)
        else {"kind": "synthetic"}
    )


def _verify_graph(ledger, plan, *, complete):
    for record in (*_records(plan), plan.version):
        observed = ledger.get(record.key)
        require(
            observed == record or (not complete and observed is None),
            "sdmx_storage_lineage_mismatch",
        )
    content = ledger.get_content(plan.version.key)
    require(
        content == plan.content or (not complete and content is None),
        "sdmx_storage_content_mismatch",
    )


def _writer_idle(writer):
    # Never resolve absence while a retained writer could still commit pending
    # effects. The caller must exclusively hold both sessions during reconcile.
    c = writer.connection
    require(
        c.closed is False
        and c.info.transaction_status is TransactionStatus.IDLE
        and c._num_transactions == 0
        and c._tpc is None
        and c._pipeline is None,
        "sdmx_writer_not_quiescent",
    )


@contextmanager
def _reconcile_view(ledger, writer, recorded_writer):
    if isinstance(ledger, InMemoryLedger):
        yield ledger
        return
    if writer is not None:
        _writer_idle(writer)
    if writer is None:
        # This is only a liveness probe, never an authoritative ledger view.
        # The writer can commit and exit AFTER this probe's RR snapshot starts.
        # End it successfully before opening the committed readback snapshot.
        with ledger.operator_snapshot() as probe:
            require(
                probe.execute(
                    "SELECT pg_backend_pid(), pg_is_in_recovery()", ()
                ).fetchone()
                == (ledger.connection.info.backend_pid, False),
                "sdmx_primary_readback_required",
            )
            alive = probe.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid = %s)",
                (recorded_writer["pid"],),
            ).fetchone()[0]
            require(alive is False, "sdmx_old_writer_session_still_alive")
    # Both scopes reject ambient transactions/old snapshots untouched and
    # verify read-only RR, rollback and restored session settings. Only now is
    # the old writer unable to commit. No result/release precedes clean exit.
    with ledger.operator_snapshot() as snapshot:
        require(
            snapshot.execute(
                "SELECT pg_backend_pid(), pg_is_in_recovery()", ()
            ).fetchone()
            == (ledger.connection.info.backend_pid, False),
            "sdmx_primary_readback_required",
        )
        yield PostgresLedger(snapshot, ledger.schema)
    if writer is not None:
        _writer_idle(writer)


class _PublicationLedger(PostgresLedger):
    def __init__(self, ledger, plan):
        super().__init__(ledger.connection, ledger.schema)
        self.plan = plan

    def server_now(self):
        at = super().server_now()
        require(at >= self.plan.version.frozen_at, "sdmx_publication_before_freeze")
        return at

    def publication_by_occurrence(self, occurrence):
        _verify_graph(self, self.plan, complete=True)
        return super().publication_by_occurrence(occurrence)


class HandoffUnknown(RuntimeError):
    def __init__(self):
        super().__init__("sdmx_handoff_outcome_unknown_reconcile_required")


class Handoff:
    """One attempt per provider in the same T08 transaction directory.

    Reconciliation observes partial effects and releases protection only after
    independent readback. It never changes the terminal intent to permit retry.
    A new repair gate must explicitly authorize any further storage effects.
    """

    def __init__(self, root):
        self.root = root
        self._tickets = {}
        self._writers = {}
        self._released = {}

    def materialize(
        self,
        kernel: CanonicalKernel,
        plan: SdmxPlan,
        store,
        protection,
        *,
        enabled=False,
        authoritative_commit=None,
    ):
        require(enabled is True, "sdmx_storage_opt_in_required")
        validate_plan(plan)
        _check_ledger(kernel.ledger)
        if isinstance(kernel.ledger, PostgresLedger):
            require(
                isinstance(store, GarageMaterialStore)
                and isinstance(protection, PostgresAdvisoryWriterProtection)
                and protection.connection is kernel.ledger.connection,
                "sdmx_storage_capability_binding_required",
            )
        require(
            not isinstance(kernel.ledger, PostgresLedger)
            or authoritative_commit is not None,
            "sdmx_commit_callback_required",
        )
        provider = candidate_from_content(plan.content).provider
        materialized = durable_plan(plan)
        identities = raw_identities(plan)
        raw = tuple(r.raw for r in candidate_from_content(plan.content).entities)
        with exclusive(self.root):
            path = self.root / (provider + ".handoff.json")
            require(not path.exists(), "sdmx_handoff_consumed_no_retry")
            document = {
                "state": "started",
                "phase": "raw",
                "content": base64.b64encode(plan.content).decode(),
                "content_sha256": digest(plan.content),
                "identity": plan.run.identity.removesuffix(":run"),
                "finished_at": plan.run.finished_at.isoformat(),
                "frozen_at": plan.version.frozen_at.isoformat(),
                "writer": _session(kernel.ledger),
                "raw_verified": [],
            }
            write_json(path, document)
            self._tickets[provider], self._released[provider] = [], set()
            self._writers[provider] = kernel.ledger
            try:
                for identity, payload in zip(identities, raw, strict=True):
                    ticket = protection.acquire(identity)
                    self._tickets[provider].append((identity, ticket))
                    ticket.assert_held(identity)
                    # Read first: an explicit retained repair never repeats a
                    # successful create. Unknown PUT is observed, never retried.
                    observed = store.read_exact(identity)
                    unknown = False
                    if observed is None:
                        try:
                            store.create_if_absent(identity, payload)
                        except MaterialWriteOutcomeUnknown:
                            unknown = True
                        observed = store.read_exact(identity)
                    require(
                        observed == payload, "sdmx_raw_readback_missing_or_mismatched"
                    )
                    ticket.assert_held(identity)
                    document["raw_verified"].append(
                        {
                            "sha256": identity.sha256,
                            "size": identity.size,
                            "locator": identity.locator,
                            "unknown_put_reconciled": unknown,
                        }
                    )
                    write_json(path, document)
                document["phase"] = "ledger"
                write_json(path, document)
                # Intent precedes the first canonical write, including partial
                # add/persist errors. Those are ambiguous until fresh readback.
                self._assert_held(provider)
                for record in _records(materialized):
                    kernel.add(record)
                kernel.persist_version(materialized.version, materialized.content)
                self._assert_held(provider)
                if authoritative_commit is not None:
                    authoritative_commit()
                document["phase"] = "publication"
                write_json(path, document)
                self._assert_held(provider)

                def checked_clock():
                    at = kernel.clock()
                    require(
                        at >= plan.version.frozen_at, "sdmx_publication_before_freeze"
                    )
                    return at

                if not isinstance(kernel.ledger, PostgresLedger):
                    _verify_graph(kernel.ledger, materialized, complete=True)
                publisher = (
                    _PublicationLedger(kernel.ledger, materialized)
                    if isinstance(kernel.ledger, PostgresLedger)
                    else kernel.ledger
                )
                publication = publisher.publish(
                    materialized.publication_request, checked_clock
                )
                require(
                    publication.decision_at >= plan.version.frozen_at
                    and publication.usable_at >= publication.decision_at,
                    "sdmx_publication_time_invalid",
                )
                document["phase"] = "release"
                write_json(path, document)
                self._release(provider)
                document["state"] = "succeeded"
                write_json(path, document)
                return materialized, publication
            except Exception:
                for identity, ticket in self._tickets[provider]:
                    if id(ticket) not in self._released[provider]:
                        try:
                            ticket.mark_outcome_unknown(identity)
                        except Exception:
                            # Lost session/protection is unresolved; it cannot
                            # authorize any further write or automatic retry.
                            document["protection_unproven"] = True
                document["state"] = "outcome_unknown"
                document["classification"] = "storage_or_handoff_failure"
                # A failed journal update still leaves the consumed intent.
                write_json(path, document)
                raise HandoffUnknown() from None

    def _assert_held(self, provider):
        for identity, ticket in self._tickets[provider]:
            if id(ticket) not in self._released[provider]:
                ticket.assert_held(identity)

    def _release(self, provider):
        # Successful releases survive a subsequent release failure.
        for identity, ticket in reversed(self._tickets[provider]):
            if id(ticket) not in self._released[provider]:
                ticket.release(identity)
                self._released[provider].add(id(ticket))

    def reconcile(self, provider, ledger, store, protection, *, enabled=False):
        require(enabled is True, "sdmx_reconcile_opt_in_required")
        from .sdmx_contract import surface_for
        from .sdmx_local_read import read_local_sdmx

        surface_for(provider)
        _check_ledger(ledger)
        with exclusive(self.root):
            path = self.root / (provider + ".handoff.json")
            document = json.loads(path.read_bytes())
            content = base64.b64decode(document["content"], validate=True)
            require(
                digest(content) == document["content_sha256"],
                "sdmx_handoff_digest_mismatch",
            )
            candidate = candidate_from_content(content)
            require(candidate.provider == provider, "sdmx_handoff_provider_mismatch")
            plan = durable_plan(
                build_plan(
                    candidate,
                    identity=document["identity"],
                    finished_at=datetime.fromisoformat(document["finished_at"]),
                    frozen_at=datetime.fromisoformat(document["frozen_at"]),
                )
            )
            require(plan.content == content, "sdmx_handoff_carrier_mismatch")
            writer = self._writers.get(provider)
            require(ledger is not writer, "sdmx_independent_readback_required")
            if document["writer"]["kind"] == "postgres":
                require(
                    isinstance(ledger, PostgresLedger)
                    and ledger.connection.info.backend_pid != document["writer"]["pid"]
                    and ledger.connection.info.dbname == document["writer"]["database"]
                    and ledger.connection.info.port == document["writer"]["port"],
                    "sdmx_independent_connection_required",
                )
                require(
                    isinstance(store, GarageMaterialStore)
                    and isinstance(protection, PostgresAdvisoryWriterProtection)
                    and (
                        protection.connection is ledger.connection
                        or (
                            writer is not None
                            and protection.connection is writer.connection
                        )
                    ),
                    "sdmx_readback_capability_binding_required",
                )
                if writer is not None:
                    require(
                        ledger.connection is not writer.connection,
                        "sdmx_independent_connection_required",
                    )
            else:
                require(
                    isinstance(ledger, InMemoryLedger), "sdmx_readback_domain_mismatch"
                )
            with _reconcile_view(ledger, writer, document["writer"]) as view:
                outcome, raw_state = self._observe(
                    provider, view, store, protection, plan, candidate, read_local_sdmx
                )
            self._release(provider)
            document["reconciliation"] = {
                "outcome": outcome,
                "raw_present": raw_state,
                "lock_release_proven": True,
            }
            write_json(path, document)
            return document["reconciliation"]

    def _observe(
        self, provider, ledger, store, protection, plan, candidate, local_read
    ):
        if provider not in self._tickets:
            self._tickets[provider], self._released[provider] = [], set()
        # Reacquire only released/missing tickets; never steal live protection.
        held = {
            i
            for i, t in self._tickets[provider]
            if id(t) not in self._released[provider]
        }
        for identity in raw_identities(plan):
            if identity not in held:
                ticket = protection.acquire(identity)
                self._tickets[provider].append((identity, ticket))
                held.add(identity)
        raw_state = []
        for identity, payload in zip(
            raw_identities(plan), (r.raw for r in candidate.entities), strict=True
        ):
            observed = store.read_exact(identity)
            require(
                observed is None or observed == payload,
                "sdmx_reconcile_raw_mismatch",
            )
            raw_state.append(observed is not None)
        for identity, ticket in self._tickets[provider]:
            if id(ticket) not in self._released[provider]:
                ticket.assert_held(identity)
        publication = ledger.publication_by_occurrence(
            plan.publication_request.occurrence
        )
        if publication is None:
            _verify_graph(ledger, plan, complete=False)
            outcome = "confirmed_not_published"
        else:
            require(all(raw_state), "sdmx_published_raw_missing")
            _verify_graph(ledger, plan, complete=True)
            require(
                publication.request == plan.publication_request,
                "sdmx_reconcile_publication_mismatch",
            )
            # This tests the exact initial publication; as-of selection across
            # other accepted snapshots is independently checked by the AO.
            local_read(
                ledger,
                provider=provider,
                start=candidate.start,
                end=candidate.end,
                as_of=publication.usable_at,
                version_identity=plan.version.identity,
            )
            outcome = "confirmed_published"
        return outcome, raw_state
