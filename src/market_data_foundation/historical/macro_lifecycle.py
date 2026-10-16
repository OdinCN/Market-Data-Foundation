"""Shared CPI/PPI lifecycle mechanics; no provider selection or client creation.

Adapters retain their admitted source plans, PG ownership guards and local Read
validation. These helpers only meter explicit calls and observe exact identities.
"""

from dataclasses import fields, replace

from market_data_foundation.canonical import codec
from market_data_foundation.canonical.model import digest, require


class LogicalRequestMeteringError(RuntimeError):
    """Local request-intent/observation bookkeeping failed; never retry provider."""


def _meter(callback, *args):
    try:
        callback(*args)
    except Exception:
        raise LogicalRequestMeteringError("logical_request_metering_failed") from None


class MeteredTransport:
    """Meter one logical request while observing each transport attempt."""

    def __init__(self, delegate, before_request, after_request):
        self.delegate = delegate
        self.before_request = before_request
        self.after_request = after_request
        self.attempts = 0
        self.intent_recorded = False

    def post(self, endpoint, body, headers, *, timeout, max_response_bytes):
        if not self.intent_recorded:
            _meter(self.before_request)
            self.intent_recorded = True
        self.attempts += 1
        try:
            response = self.delegate.post(
                endpoint,
                body,
                headers,
                timeout=timeout,
                max_response_bytes=max_response_bytes,
            )
        except Exception:
            _meter(self.after_request, False, self.attempts)
            raise
        _meter(self.after_request, True, self.attempts)
        return response


def encode_plan(plan) -> dict:
    return {
        field.name: codec.dumps(getattr(plan, field.name)) for field in fields(plan)
    }


def decode_plan(value: dict, plan_type, classification: str):
    require(
        type(value) is dict
        and set(value) == {field.name for field in fields(plan_type)},
        classification,
    )
    return plan_type(**{key: codec.loads(item) for key, item in value.items()})


def observe_exact_material(
    *, store, protection, identity, readback, check_ledger, plan, verify_local
) -> dict:
    """Observe under protection with a new snapshot; never retry any write.

    Presence of a Version alone cannot establish publication. An invalid graph
    or inaccessible raw raises, leaving the caller's unknown state unresolved.
    """
    ticket = protection.acquire(identity)
    try:
        ticket.assert_held(identity)
        raw = store.read_exact(identity)
        verified = (
            raw is not None
            and len(raw) == identity.size
            and digest(raw) == identity.sha256
        )
        with readback() as ledger:
            check_ledger(ledger)
            publication = ledger.publication_by_occurrence(
                plan.publication_request.occurrence
            )
            if publication is None:
                observation = {
                    "outcome": "confirmed_not_published",
                    "raw_state": "absent"
                    if raw is None
                    else "verified"
                    if verified
                    else "mismatched",
                    "version_present": ledger.get(plan.version.key) is not None,
                }
            else:
                require(verified, "macro_job_raw_readback_failed")
                require(
                    publication.request == plan.publication_request,
                    "macro_job_publication_mismatch",
                )
                observation = {
                    "outcome": "confirmed_published",
                    "raw_state": "verified",
                    "evidence": verify_local(ledger),
                }
        ticket.assert_held(identity)
        return observation
    finally:
        ticket.release(identity)


class RetainedRepair:
    """Explicit new job/attempt from an exact predecessor; never reset history.

    Repair reserves one successor per predecessor. It can only use the same
    original acquisition plan and bytes with a zero Provider budget. Unknown
    writes require a separate confirmed-not-published reconciliation first.
    """

    def repair(
        self, job_id: str, replacement_id: str, *, enabled: bool = False
    ) -> dict:
        require(enabled is True, "macro_job_explicit_opt_in_required")
        with self.journal.executor(job_id):
            old = self.journal.get(job_id)
            spec = self.spec_type.from_document(old["spec"])
            require(old["raw"] is not None, "macro_job_repair_raw_required")
            retained = replace(
                spec,
                mode="retained",
                max_provider_requests=0,
                expected_raw_sha256=old["raw"]["sha256"],
            )
            return self.journal.submit_repair(
                job_id,
                replacement_id,
                retained.document(),
                retained.scope,
                self.clock(),
            )

    def _check_repair_candidate(self, job_id: str, plan) -> None:
        document = self.journal.get(job_id)
        if document.get("repair_of") is not None:
            old = self.journal.get(document["repair_of"])
            require(encode_plan(plan) == old["plan"], "macro_job_repair_plan_mismatch")
