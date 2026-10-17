"""Bounded offline recovery authority for the two retained macro source jobs.

The synchronous in-process writer is the authority. This does not qualify a PG
reader, replica, old transaction or lost writer session. Such recovery stops
until a separately qualified operator capability exists.
"""

import json
import os
from uuid import uuid4
from weakref import ref

from market_data_foundation.canonical.model import digest, require
from market_data_foundation.canonical.repository import InMemoryLedger

_WRITER_AUTHORITIES = {}
# Only this live process can register/use these synchronous writer sessions.
# A fork child must exec a fresh process before creating its own sessions;
# inherited objects and even a restored registry never prove parent quiescence.
_AUTHORITY_PROCESS = os.getpid()
os.register_at_fork(after_in_child=lambda: _WRITER_AUTHORITIES.clear())


def _writer_authority(writer):
    if os.getpid() != _AUTHORITY_PROCESS:
        return None
    entry = _WRITER_AUTHORITIES.get(id(writer))
    return entry[1] if entry is not None and entry[0]() is writer else None


def synthetic_writer_session(writer):
    # Process-local object provenance. Serialized/copied ledgers cannot inherit
    # this authority, and process restart cannot invent writer quiescence.
    require(isinstance(writer, InMemoryLedger), "source_recovery_writer_unsupported")
    require(
        os.getpid() == _AUTHORITY_PROCESS, "source_recovery_original_process_required"
    )
    token = _writer_authority(writer)
    if token is None:
        token = uuid4().hex
        writer_id = id(writer)
        _WRITER_AUTHORITIES[writer_id] = (
            ref(writer, lambda _: _WRITER_AUTHORITIES.pop(writer_id, None)),
            token,
        )
    return {"kind": "synthetic", "lock_release_proven": False, "authority": token}


def recovery_binding(document):
    require(
        document["attempts"] == 1
        and document["attempt_id"] == document["job_id"] + "-a1"
        and document["plan"] is not None
        and document["raw"] is not None,
        "source_recovery_attempt_binding_required",
    )
    value = {
        key: document[key]
        for key in ("job_id", "attempt_id", "plan", "raw", "writer_session")
    }
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def recovery_snapshot(writer, readback, document, effect_domain):
    """Validate authority before releasing any retained protection.

    Caller owns the original job executor: its synchronous writer attempt has
    returned. Check base serialized state on both sides of all reader hooks,
    then use a base ledger so overridden query methods cannot invent absence.
    No caller assertion, changed PID or distinct reader establishes PG writer
    quiescence or transaction freshness; PG is deliberately unsupported here.
    """
    require(
        effect_domain == "synthetic" and isinstance(writer, InMemoryLedger),
        "source_recovery_fresh_committed_operator_capability_missing",
    )
    require(
        (document.get("writer_session") or {}).get("kind") == "synthetic"
        and document["writer_session"].get("authority") is not None
        and document["writer_session"]["authority"] == _writer_authority(writer),
        "source_recovery_writer_session_unproven",
    )
    binding = recovery_binding(document)
    authoritative = InMemoryLedger.snapshot(writer)
    with readback() as reader:
        require(
            isinstance(reader, InMemoryLedger)
            and reader is not writer
            and InMemoryLedger.snapshot(reader) == authoritative
            and InMemoryLedger.snapshot(writer) == authoritative,
            "source_recovery_stale_or_untrusted_reader",
        )
    require(
        InMemoryLedger.snapshot(writer) == authoritative,
        "source_recovery_writer_changed_during_readback",
    )
    return InMemoryLedger.from_snapshot(authoritative), {
        "authority": "offline_synchronous_inmemory_writer",
        "binding_sha256": binding,
        "snapshot_sha256": digest(authoritative.encode()),
    }


def require_repair_absence(writer, readback, document, effect_domain, plan):
    ledger, proof = recovery_snapshot(writer, readback, document, effect_domain)
    require(
        (document["reconciliation"] or {}).get("source_recovery") == proof
        and document["reconciliation"].get("readback_protection_pending") is False
        and ledger.publication_by_occurrence(plan.publication_request.occurrence)
        is None
        and not ledger.publications_for_version(plan.version.key),
        "source_repair_fresh_absence_required",
    )
