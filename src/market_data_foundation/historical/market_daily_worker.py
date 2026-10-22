"""Callable bounded offline daily Job/Shard/Attempt worker, no live fallback."""

import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterator

from market_data_foundation.canonical.model import require
from market_data_foundation.domain_contracts.market_coverage import (
    CoverageClass,
    CoverageManifest,
    PointEvidence,
    RecoveryCandidates,
    classify_coverage,
    expected_state,
    plan_recovery,
)
from market_data_foundation.domain_contracts.market_planning import (
    Plan,
    PlanningBudget,
    Shard,
    plan_job,
)
from market_data_foundation.domain_contracts.market_universe import (
    EvidenceMode,
    canonical_bytes,
)
from market_data_foundation.historical.market_daily_journal import (
    Claim,
    Journal,
    MaterialReceipt,
    page_metadata,
)
from market_data_foundation.historical.market_daily_source import (
    DailyError,
    Failure,
    Lookup,
    LookupStatus,
    NotPerformed,
    OfflineSource,
    Page,
    PageRequest,
)


@dataclass(frozen=True)
class RuntimePolicy:
    # D1 does not assign handshake/heartbeat or decoded metadata budgets. The
    # offline extension is explicitly frozen with the durable acceptance.
    max_handshakes: int = 0
    max_heartbeats: int = 0
    max_metadata_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        require(
            all(
                type(value) is int and value >= 0
                for value in (
                    self.max_handshakes,
                    self.max_heartbeats,
                    self.max_metadata_bytes,
                )
            ),
            "invalid_runtime_policy",
        )


@dataclass(frozen=True)
class ShardResult:
    shard_identity: str
    state: str
    receipts: tuple[MaterialReceipt, ...]
    failures: tuple[Failure, ...]


@dataclass(frozen=True)
class RunReport:
    job_identity: str
    state: str
    shards: tuple[ShardResult, ...]
    counters: tuple[tuple[str, int], ...]
    stop_reason: Failure | None = None


@contextmanager
def _source_timeout(seconds: float) -> Iterator[None]:
    """Isolated POSIX worker main thread; never displace another alarm owner."""
    require(
        threading.current_thread() is threading.main_thread(),
        "d2_worker_main_thread_required",
    )
    require(
        signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0),
        "d2_worker_alarm_already_owned",
    )
    handler = signal.getsignal(signal.SIGALRM)

    def expired(_signum: int, _frame: object) -> None:
        raise TimeoutError()

    if seconds <= 0:
        raise TimeoutError()
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, handler)


class DailyRunner:
    def __init__(
        self,
        plan: Plan,
        journal: Journal,
        source: OfflineSource,
        *,
        policy: RuntimePolicy = RuntimePolicy(),
        sleep: Callable[[float], None] = time.sleep,
        fault: Callable[[str], None] = lambda _: None,
    ):
        # Do not change JobSpec.require_execution or reinterpret retained source
        # bytes as permission for a real Universe. Only invented universes run.
        if plan.spec.universe.provenance.mode != EvidenceMode.SYNTHETIC:
            plan.spec.require_execution()
        require(plan == plan_job(plan.spec), "exact_frozen_d1_plan_required")
        require(
            source.binding.mode in (EvidenceMode.SYNTHETIC, EvidenceMode.RETAINED),
            "d2_offline_source_only",
        )
        for shard in plan.shards:
            require(
                shard.route.implementation_identity
                == source.binding.implementation_identity
                and shard.route.endpoint_contract == source.binding.endpoint_contract,
                "exact_offline_binding_required",
            )
            require(
                shard.route.source == source.binding.source
                and shard.route.binding == source.binding.binding
                and shard.route.mapping_revision == source.binding.mapping_revision,
                "exact_source_binding_mapping_required",
            )
        self.plan = plan
        self.journal = journal
        self.source = source
        self.binding = source.binding
        self.policy = policy
        self.sleep = sleep
        self.fault = fault
        self.contract = canonical_bytes((source.binding, policy))
        self.lease_seconds = max(30, plan.spec.budget.timeout_seconds + 2)

    @classmethod
    def restore(
        cls, job: str, journal: Journal, source: OfflineSource, **kwargs
    ) -> DailyRunner:
        worker = cls(journal.load_plan(job), journal, source, **kwargs)
        journal.check_intent(worker.plan, worker.contract)
        return worker

    def request(self, shard: Shard, index: int) -> PageRequest:
        require(shard in self.plan.shards, "original_shard_required")
        return PageRequest(
            shard, index, self.binding.schema_revision, self.plan.spec.budget
        )

    def accept(self) -> str:
        require(self.source.binding == self.binding, "offline_binding_changed")
        return self.journal.accept(self.plan, self.contract)

    def cancel(self) -> RunReport:
        self.journal.check_intent(self.plan, self.contract)
        self.journal.cancel(self.plan.spec.identity)
        return self.report()

    def _check_request(self, request: PageRequest) -> None:
        require(
            request == self.request(request.shard, request.page_index),
            "original_request_required",
        )

    def _limits(self) -> dict[str, int]:
        budget = self.plan.spec.budget
        return {
            "connections": budget.max_connections,
            "probes": budget.max_probes,
            "handshakes": self.policy.max_handshakes,
            "heartbeats": self.policy.max_heartbeats,
            "redials": budget.sdk_redials,
            "retries": budget.automatic_retries,
            "response_bytes": self.plan.request_count * budget.max_response_bytes,
            "material_bytes": budget.max_material_bytes,
            "rows": budget.max_rows,
            "metadata_bytes": self.policy.max_metadata_bytes,
            "pages": self.plan.request_count,
        }

    def _page_failure(self, request: PageRequest, page: Page) -> Failure | None:
        if page.request_identity != request.identity:
            return Failure.IDENTITY
        if (page.producer_operation, page.upstream_revision, page.decode_revision) != (
            self.binding.producer_operation,
            self.binding.upstream_revision,
            self.binding.decode_revision,
        ):
            return Failure.IDENTITY
        if (
            self.binding.mode == EvidenceMode.RETAINED
            and page.retained_reference not in self.binding.retained_materials
        ):
            return Failure.IDENTITY
        if len(page.raw) > self.plan.spec.budget.max_response_bytes:
            return Failure.BUDGET
        if len(page.rows) > request.count or len(
            {row.locator for row in page.rows}
        ) != len(page.rows):
            return Failure.PAGE
        try:
            days = tuple(date.fromisoformat(row.day) for row in page.rows)
        except ValueError:
            return Failure.PAGE
        if any(
            day.isoformat() != row.day or not request.shard.window.contains(day)
            for row, day in zip(page.rows, days, strict=True)
        ):
            return Failure.PAGE
        descending = request.shard.route.pages.order == "descending"
        if tuple(sorted(set(days), reverse=descending)) != days:
            return Failure.PAGE
        locators = {row.locator for row in page.rows}
        # Scan the frozen, bounded Shard history, including after a restart.
        # Keep only one prior page in memory; adjacent-page checks miss reuse
        # of a locator from an earlier page with a different monotonic date.
        for index in range(request.page_index):
            previous = self.journal.receipt(self.request(request.shard, index))
            if previous is None or previous.state != "SUCCEEDED":
                return Failure.DRIFT
            prior = self.journal.read_material(previous)
            if prior.terminal or prior.snapshot_anchor != page.snapshot_anchor:
                return Failure.DRIFT
            if index == request.page_index - 1 and prior.rows and days:
                last = date.fromisoformat(prior.rows[-1].day)
                if (descending and days[0] >= last) or (
                    not descending and days[0] <= last
                ):
                    return Failure.DRIFT
            if any(row.locator in locators for row in prior.rows):
                return Failure.DRIFT
        if page.terminal is True and not (
            isinstance(page.termination_evidence, str)
            and page.termination_evidence.strip()
        ):
            return Failure.EXHAUSTION
        if len(page.rows) < request.count and page.terminal is not True:
            return Failure.EXHAUSTION
        if (
            request.page_index == request.shard.route.pages.page_count - 1
            and page.terminal is not True
        ):
            return Failure.EXHAUSTION
        return None

    def _finish(
        self,
        claim: Claim,
        request: PageRequest,
        page: Page,
        *,
        reconciliation: Lookup | None = None,
    ) -> None:
        require(isinstance(page, Page), "offline_page_required")
        self.journal.observe_response(claim, request, page, self._limits())
        failure = self._page_failure(request, page)
        # Response observation commits before receipt/material acceptance.
        # Use those original durable clocks on both execution and recovery so
        # rollback cannot erase a timeout or count later recovery delay as one.
        attempt = self.journal.attempt(request)
        if attempt is None or attempt["observed_at_ns"] is None:
            raise DailyError(Failure.CORRUPT)
        elapsed = attempt["observed_at_ns"] - attempt["started_ns"]
        if elapsed < 0 or elapsed > self.plan.spec.budget.timeout_seconds * 10**9:
            failure = Failure.TIMEOUT
        job = self.journal.job(self.plan.spec.identity)
        wall_elapsed = attempt["observed_at_ns"] - job["accepted_ns"]
        if (
            wall_elapsed < 0
            or wall_elapsed >= self.plan.spec.budget.wall_seconds * 10**9
        ):
            failure = Failure.BUDGET
            self.journal.block(claim, Failure.BUDGET)
        # Bound metadata before a DB write. Oversized raw is not retained beyond
        # the configured allowance; the attempt blocks and is never auto-retried.
        if (
            len(page.raw) > self.plan.spec.budget.max_response_bytes
            or len(page_metadata(page).encode()) > self.policy.max_metadata_bytes
        ):
            self.journal.mark(
                claim,
                request,
                "REJECTED",
                Failure.BUDGET,
                reconciliation=reconciliation,
            )
            return
        try:
            self.journal.finish(
                claim,
                request,
                page,
                failure,
                self._limits(),
                reconciliation=reconciliation,
            )
        except DailyError as error:
            if error.classification == Failure.BUDGET:
                # Cumulative refusal has the same terminal semantics as a
                # single oversized page, including explicit FOUND acceptance.
                self.journal.mark(
                    claim,
                    request,
                    "REJECTED",
                    Failure.BUDGET,
                    reconciliation=reconciliation,
                )
                return
            raise

    def run(self, *, resume: bool = False) -> RunReport:
        require(
            threading.current_thread() is threading.main_thread(),
            "d2_worker_main_thread_required",
        )
        require(
            signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0),
            "d2_worker_alarm_already_owned",
        )
        self.accept()
        budget = self.plan.spec.budget
        with self.journal.claim(self.plan.spec.identity, self.lease_seconds) as claim:
            if resume:
                self.journal.resume(claim)
            for shard in self.plan.shards:
                for index in range(shard.route.pages.page_count):
                    request = self.request(shard, index)
                    old = self.journal.attempt(request)
                    if old is not None:
                        if old["state"] != "SUCCEEDED":
                            break
                        receipt = self.journal.receipt(request)
                        if receipt is None:
                            raise DailyError(Failure.CORRUPT)
                        if self.journal.read_material(receipt).terminal:
                            break
                        continue
                    job = self.journal.job(self.plan.spec.identity)
                    if job["cancelled"] or job["stop_reason"]:
                        return self.report()
                    last = job["last_request_ns"]
                    now = self.journal.clock_ns()
                    remaining_ns = (
                        job["accepted_ns"] + budget.wall_seconds * 10**9 - now
                    )
                    # Check durable clocks before a rate wait: after restart a
                    # rollback must STOP, never turn into a long host-claim hold.
                    if (
                        now < job["accepted_ns"]
                        or (last is not None and now < last)
                        or remaining_ns <= 0
                    ):
                        self.journal.block(claim, Failure.BUDGET)
                        return self.report()
                    if last is not None:
                        delay_ns = (
                            last
                            + (10**9 + budget.requests_per_second - 1)
                            // budget.requests_per_second
                            - now
                        )
                        if delay_ns > 0:
                            self.sleep(min(delay_ns, remaining_ns) / 10**9)
                    self.journal.renew(claim, self.lease_seconds)
                    try:
                        if not self.journal.begin(
                            claim,
                            request,
                            budget.max_requests,
                            budget.requests_per_second,
                            budget.wall_seconds,
                        ):
                            break
                    except DailyError as error:
                        if error.classification in (Failure.BUDGET, Failure.CANCELLED):
                            if error.classification == Failure.BUDGET:
                                self.journal.block(claim, error.classification)
                            return self.report(blocked=error.classification)
                        raise
                    started = self.journal.clock_ns()

                    def charge(axis: str, amount: int = 1) -> None:
                        # Acquisition request/page/bytes are charged by the
                        # runner. Adapters may only meter transport axes here.
                        if axis not in {
                            "connections",
                            "probes",
                            "handshakes",
                            "heartbeats",
                            "redials",
                            "retries",
                        }:
                            raise DailyError(Failure.BUDGET)
                        self.journal.charge(
                            claim,
                            axis,
                            amount,
                            self._limits()[axis],
                            budget.wall_seconds,
                        )

                    try:
                        elapsed = (started - job["accepted_ns"]) / 10**9
                        with _source_timeout(
                            min(budget.timeout_seconds, budget.wall_seconds - elapsed)
                        ):
                            self.fault("before_source")
                            page = self.source.fetch(request, charge)
                            self.fault("after_source")
                    except NotPerformed:
                        self.journal.mark(claim, request, "ABSENT", Failure.ABSENT)
                        break
                    except TimeoutError:
                        self.journal.mark(claim, request, "UNKNOWN", Failure.TIMEOUT)
                        break
                    except ConnectionError:
                        self.journal.mark(claim, request, "UNKNOWN", Failure.TRANSPORT)
                        break
                    except DailyError as error:
                        # Even a budget exception may follow earlier SDK effects;
                        # do not turn it into a confirmed-absent retry candidate.
                        if error.classification == Failure.BUDGET:
                            self.journal.block(claim, Failure.BUDGET)
                        self.journal.mark(
                            claim, request, "UNKNOWN", error.classification
                        )
                        return self.report(blocked=error.classification)
                    except Exception:
                        self.journal.mark(claim, request, "UNKNOWN", Failure.UNKNOWN)
                        break
                    try:
                        self._finish(claim, request, page)
                    except DailyError as error:
                        if error.classification == Failure.LEASE:
                            raise
                        self.journal.mark(
                            claim, request, "UNKNOWN", error.classification
                        )
                        return self.report(blocked=error.classification)
                    except Exception:
                        self.journal.mark(claim, request, "UNKNOWN", Failure.PAGE)
                        break
                    # No attempt, including a rejected page, is silently retried.
                    current = self.journal.attempt(request)
                    if current["classification"] == Failure.BUDGET.value:
                        return self.report(blocked=Failure.BUDGET)
                    if current["state"] != "SUCCEEDED" or page.terminal:
                        break
        return self.report()

    def reconcile(self, request: PageRequest) -> Lookup:
        """Original identity verification ONLY: no claim, Provider or DB writes."""
        self.journal.check_intent(self.plan, self.contract)
        require(self.source.binding == self.binding, "offline_binding_changed")
        self._check_request(request)
        attempt = self.journal.attempt(request)
        require(attempt is not None, "durable_original_attempt_required")
        observation = self.journal.reconciliation(request)
        if observation is not None:
            receipt = self.journal.receipt(request)
            page = self.journal.read_material(receipt) if receipt is not None else None
            if observation["status"] == LookupStatus.FOUND and page is None:
                # Over-budget responses deliberately have no retained material.
                return Lookup(
                    request.identity, LookupStatus.UNKNOWN, observation["evidence"]
                )
            return Lookup(
                request.identity,
                LookupStatus(observation["status"]),
                observation["evidence"],
                page,
            )
        receipt = self.journal.receipt(request)
        if receipt is not None:
            return Lookup(
                request.identity,
                LookupStatus.FOUND,
                receipt.locator,
                self.journal.read_material(receipt),
            )
        if attempt["state"] == "ABSENT":
            return Lookup(request.identity, LookupStatus.ABSENT, Failure.ABSENT.value)
        try:
            result = self.source.lookup_original(request)
        except Exception:
            return Lookup(request.identity, LookupStatus.UNKNOWN, Failure.UNKNOWN.value)
        if result.request_identity != request.identity or (
            result.page is not None and result.page.request_identity != request.identity
        ):
            raise DailyError(Failure.IDENTITY)
        return result

    def accept_reconciliation(self, request: PageRequest, result: Lookup) -> RunReport:
        """Separate explicit journal/material acceptance, never a new fetch."""
        self.journal.check_intent(self.plan, self.contract)
        require(self.source.binding == self.binding, "offline_binding_changed")
        self._check_request(request)
        require(result.request_identity == request.identity, "original_lookup_required")
        if result.status == LookupStatus.UNKNOWN:
            return self.report()
        with self.journal.claim(self.plan.spec.identity, self.lease_seconds) as claim:
            receipt = self.journal.receipt(request)
            if receipt is not None:
                if (
                    result.status != LookupStatus.FOUND
                    or self.journal.read_material(receipt) != result.page
                ):
                    raise DailyError(Failure.IDENTITY)
                self.journal.accept_reconciliation_observation(claim, request, result)
                return self.report()
            attempt = self.journal.attempt(request)
            require(attempt is not None, "durable_original_attempt_required")
            if (
                result.status == LookupStatus.ABSENT
                and attempt["observed_at_ns"] is not None
            ):
                raise DailyError(Failure.IDENTITY)
            if attempt["state"] == "ABSENT" and result.status == LookupStatus.ABSENT:
                self.journal.accept_reconciliation_observation(claim, request, result)
                return self.report()
            if attempt["state"] == "REJECTED" and result.status == LookupStatus.FOUND:
                self.journal.accept_reconciliation_observation(claim, request, result)
                return self.report()
            # UNKNOWN was recorded for a caught exception, STARTED for a crash.
            if attempt["state"] == "UNKNOWN":
                # Transition only under the same host fence; original identity
                # and consumed counters remain intact.
                self.journal.reopen_unknown(claim, request)
            elif attempt["state"] != "STARTED":
                raise DailyError(Failure.IDENTITY)
            if result.status == LookupStatus.FOUND:
                self._finish(claim, request, result.page, reconciliation=result)
            else:
                self.journal.mark(
                    claim, request, "ABSENT", Failure.ABSENT, reconciliation=result
                )
        return self.report()

    def report(self, *, blocked: Failure | None = None) -> RunReport:
        self.journal.check_intent(self.plan, self.contract)
        attempts = self.journal.attempts(self.plan.spec.identity)
        results = []
        for shard in self.plan.shards:
            items = [item for item in attempts if item["shard"] == shard.identity]
            receipts = tuple(
                receipt
                for item in items
                if (
                    receipt := self.journal.receipt(
                        self.request(shard, item["page_index"])
                    )
                )
                is not None
            )
            complete = (
                bool(receipts)
                and all(item["state"] == "SUCCEEDED" for item in items)
                and self.journal.read_material(receipts[-1]).terminal is True
            )
            unknown = any(item["state"] in ("STARTED", "UNKNOWN") for item in items)
            state = (
                "COMPLETE"
                if complete
                else "UNKNOWN"
                if unknown
                else "BLOCKED"
                if any(item["state"] in ("ABSENT", "REJECTED") for item in items)
                else "PENDING"
            )
            failures = tuple(
                Failure(item["classification"])
                if item["classification"]
                else Failure.UNKNOWN
                for item in items
                if item["state"] != "SUCCEEDED"
            )
            results.append(ShardResult(shard.identity, state, receipts, failures))
        job = self.journal.job(self.plan.spec.identity)
        state = (
            "CANCELLED"
            if job["cancelled"]
            else "COMPLETE"
            if all(item.state == "COMPLETE" for item in results)
            else "UNKNOWN"
            if any(item.state == "UNKNOWN" for item in results)
            else "BLOCKED"
            if blocked
            or job["stop_reason"]
            or any(item.state == "BLOCKED" for item in results)
            else "PENDING"
        )
        return RunReport(
            self.plan.spec.identity,
            state,
            tuple(results),
            tuple(sorted(job["counters"].items())),
            Failure(job["stop_reason"]) if job["stop_reason"] else None,
        )

    def coverage(self, assessments: tuple[PointEvidence, ...] = ()) -> CoverageManifest:
        """Raw acquisition is not OBSERVED_VALID. D3 supplies validated evidence."""
        self.journal.check_intent(self.plan, self.contract)
        supplied = {item.point for item in assessments}
        evidence = list(assessments)
        attempts = self.journal.attempts(self.plan.spec.identity)
        for shard in self.plan.shards:
            items = [item for item in attempts if item["shard"] == shard.identity]
            # A whole-shard absent first request has no successful raw page to
            # reprocess. Partial shards remain UNKNOWN pending D3 assessment.
            absent = (
                len(items) == 1
                and items[0]["page_index"] == 0
                and items[0]["state"] == "ABSENT"
            )
            for point in shard.target_points:
                if point in supplied or expected_state(self.plan, point) is not None:
                    continue
                evidence.append(
                    PointEvidence(
                        point,
                        CoverageClass.COLLECTION_FAILED
                        if absent
                        else CoverageClass.UNKNOWN,
                        ("d2-acquisition-only",),
                        (shard.identity,),
                        tuple(item["id"] for item in items),
                        tuple(item["request_id"] for item in items),
                        effects_confirmed_absent=absent,
                        retryable_confirmed=absent,
                    )
                )
        return classify_coverage(self.plan, tuple(evidence))

    def recovery_candidates(
        self, manifest: CoverageManifest, budget: PlanningBudget
    ) -> RecoveryCandidates:
        require(manifest.plan == self.plan, "original_manifest_required")
        return plan_recovery(manifest, budget)
