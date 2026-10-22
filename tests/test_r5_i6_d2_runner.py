"""Offline failure injection; every identity and byte is invented."""

import multiprocessing
import os
import signal
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256

import pytest
from r5_i6_market_fixture import KNOWN, MATERIAL, START, budget, job

from market_data_foundation.canonical.model import ContractError, Key, Kind
from market_data_foundation.domain_contracts.market_coverage import (
    CoverageClass,
    CoverageStatus,
    PointEvidence,
    expected_state,
)
from market_data_foundation.domain_contracts.market_planning import (
    PagePlan,
    plan_job,
)
from market_data_foundation.domain_contracts.market_universe import (
    DateWindow,
    EvidenceMode,
    Qualification,
    QualificationStatus,
    canonical_bytes,
)
from market_data_foundation.historical.market_daily_journal import Journal
from market_data_foundation.historical.market_daily_source import (
    MAX_LOOKUP_EVIDENCE_BYTES,
    DailyError,
    Failure,
    Lookup,
    LookupStatus,
    NativeField,
    NativeRow,
    NotPerformed,
    OfflineBinding,
    Page,
)
from market_data_foundation.historical.market_daily_worker import (
    DailyRunner,
    RuntimePolicy,
)


class Crash(BaseException):
    pass


class Clock:
    def __init__(self):
        self.now = 10**18
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += round(seconds * 10**9)


class Fault:
    def __init__(self, at):
        self.at = at
        self.fired = False

    def __call__(self, event):
        if event == self.at and not self.fired:
            self.fired = True
            raise Crash(event)


def planned(*, size=5, pages=1, order="ascending", spec=None):
    spec = spec or replace(
        job(), window=DateWindow(START + timedelta(days=1), START + timedelta(days=6))
    )
    route = replace(
        spec.routes[0],
        pages=PagePlan(
            0, size, pages, order, "frozen-synthetic-anchor", "explicit-synthetic-end"
        ),
    )
    return plan_job(replace(spec, routes=(route,)))


class Source:
    def __init__(
        self, plan, *, mode=EvidenceMode.SYNTHETIC, transform=lambda r, p: p, effects=()
    ):
        route = plan.shards[0].route
        self.binding = OfflineBinding(
            mode,
            route.source,
            route.binding,
            route.mapping_revision,
            route.implementation_identity,
            route.endpoint_contract,
            "synthetic.GetKlineDaily",
            "synthetic-upstream-v1",
            "synthetic-native-decode-v1",
            retained_materials=("invented-retained-material",)
            if mode == EvidenceMode.RETAINED
            else (),
        )
        self.calls = []
        self.lookups = []
        self.retained = {}
        self.transform = transform
        self.effects = effects

    def page(self, request):
        days = [point.day for point in request.shard.request_superset]
        if request.shard.route.pages.order == "descending":
            days.reverse()
        selected = days[request.offset : request.offset + request.count]
        terminal = request.offset + request.count >= len(days)
        return Page(
            request.identity,
            b"invented-raw-byte-page:" + str(request.offset).encode(),
            tuple(
                NativeRow(
                    day.isoformat(),
                    f"native-row:{day}",
                    (
                        NativeField(
                            "Volume",
                            "int64",
                            0,
                            "synthetic:model_kline.go:49",
                            "synthetic:KlineCache.Type/Kind/Code",
                        ),
                        NativeField(
                            "Close",
                            "Price",
                            12345,
                            "synthetic:model_kline.go:47",
                            "synthetic:types_price.go",
                        ),
                    ),
                )
                for day in selected
            ),
            "invented-frozen-snapshot",
            terminal,
            "invented-end-proof" if terminal else None,
            self.binding.producer_operation,
            self.binding.upstream_revision,
            self.binding.decode_revision,
            obtained_at=KNOWN,
            received_at=KNOWN + timedelta(seconds=1),
            retained_reference="invented-retained-material"
            if self.binding.mode == EvidenceMode.RETAINED
            else None,
        )

    def fetch(self, request, charge):
        self.calls.append(request.identity)
        for axis, count in self.effects:
            charge(axis, count)
        page = self.transform(request, self.page(request))
        self.retained[request.identity] = page
        return page

    def lookup_original(self, request):
        self.lookups.append(request.identity)
        page = self.retained.get(request.identity)
        return Lookup(
            request.identity,
            LookupStatus.FOUND if page else LookupStatus.UNKNOWN,
            "synthetic-original-identity-index",
            page,
        )


def runner(tmp_path, plan=None, source=None, **kwargs):
    plan = plan or planned()
    clock = Clock()
    journal = Journal(tmp_path, clock_ns=clock)
    source = source or Source(plan)
    return DailyRunner(plan, journal, source, sleep=clock.sleep, **kwargs), clock


def db_bytes(journal):
    return {
        path.name: path.read_bytes()
        for path in journal.directory.iterdir()
        if path.is_file()
    }


def test_importable_runner_durable_receipts_native_unknown_and_idempotency(tmp_path):
    worker, _ = runner(tmp_path)
    first = worker.run()
    assert first.state == "COMPLETE"
    assert dict(first.counters)["requests"] == 1
    assert len(worker.source.calls) == 1
    receipt = first.shards[0].receipts[0]
    page = worker.journal.read_material(receipt)
    assert receipt.digest == sha256(page.raw).hexdigest()
    assert receipt.byte_length == len(page.raw)
    assert receipt.request.shard.job_identity == first.job_identity
    assert receipt.attempt_identity == receipt.request.attempt_identity
    assert page.rows[0].fields[0].value == 0
    assert page.rows[0].fields[0].unit == "UNKNOWN"
    assert page.known_at is None and page.source_time is None
    assert page.received_at != page.obtained_at
    assert worker.run(resume=True) == first
    restarted = DailyRunner(worker.plan, Journal(tmp_path), worker.source)
    assert restarted.run() == first
    assert len(worker.source.calls) == 1
    assert [event["event"] for event in worker.journal.events(first.job_identity)] == [
        "ACCEPTED",
        "STARTED",
        "RESPONSE_OBSERVED",
        "SUCCEEDED",
    ]


@pytest.mark.parametrize("order", ["ascending", "descending"])
def test_pagination_offsets_rate_and_terminal_evidence(tmp_path, order):
    worker, clock = runner(tmp_path, planned(size=2, pages=3, order=order))
    result = worker.run()
    assert result.state == "COMPLETE"
    receipts = result.shards[0].receipts
    assert [receipt.request.offset for receipt in receipts] == [0, 2, 4]
    assert dict(result.counters)["rows"] == 5
    assert dict(result.counters)["pages"] == 3
    assert dict(result.counters)["returned_pages"] == 3
    assert len(clock.sleeps) == 2
    assert len({receipt.request.identity for receipt in receipts}) == 3


def test_explicit_terminal_stops_without_filling_page_allowance(tmp_path):
    worker, _ = runner(tmp_path, planned(size=5, pages=3))
    assert worker.run().state == "COMPLETE"
    assert len(worker.source.calls) == 1


@pytest.mark.parametrize(
    "event,expected_calls,expected_attempts",
    [
        ("before_accept_commit", 0, 0),
        ("after_accept_commit", 0, 0),
        ("before_attempt_commit", 0, 0),
        ("after_attempt_commit", 0, 1),
        ("after_material_insert", 1, 1),
        ("before_receipt_commit", 1, 1),
        ("after_receipt_commit", 1, 1),
    ],
)
def test_journal_crash_windows_no_implicit_reacquisition(
    tmp_path, event, expected_calls, expected_attempts
):
    worker, clock = runner(tmp_path)
    worker.journal.fault = Fault(event)
    with pytest.raises(Crash):
        worker.run()
    assert len(worker.source.calls) == expected_calls
    assert len(worker.journal.attempts(worker.plan.spec.identity)) == expected_attempts
    restarted = DailyRunner(
        worker.plan, Journal(tmp_path, clock_ns=clock), worker.source, sleep=clock.sleep
    )
    result = restarted.run()
    if expected_attempts == 0:
        assert result.state == "COMPLETE"
        assert len(worker.source.calls) == 1
    elif event == "after_receipt_commit":
        assert result.state == "COMPLETE"
        assert len(worker.source.calls) == 1
    else:
        assert result.state == "UNKNOWN"
        assert len(worker.source.calls) == expected_calls
        assert not result.shards[0].receipts


@pytest.mark.parametrize("event", ["before_source", "after_source"])
def test_source_crash_original_lookup_is_read_only_and_acceptance_is_separate(
    tmp_path, event
):
    worker, _ = runner(tmp_path, fault=Fault(event))
    with pytest.raises(Crash):
        worker.run()
    request = worker.request(worker.plan.shards[0], 0)
    before = db_bytes(worker.journal)
    resolution = worker.reconcile(request)
    assert db_bytes(worker.journal) == before
    assert len(worker.source.calls) == (event == "after_source")
    if event == "before_source":
        assert resolution.status == LookupStatus.UNKNOWN
        assert worker.accept_reconciliation(request, resolution).state == "UNKNOWN"
        assert db_bytes(worker.journal) == before
    else:
        assert resolution.status == LookupStatus.FOUND
        assert worker.accept_reconciliation(request, resolution).state == "COMPLETE"
        snapshot = worker.report()
        assert worker.accept_reconciliation(request, resolution) == snapshot
        assert dict(snapshot.counters)["requests"] == 1
        assert dict(snapshot.counters)["returned_pages"] == 1
        assert len(worker.source.calls) == 1


def test_lost_receipt_is_verified_from_journal_without_source_lookup(tmp_path):
    worker, _ = runner(tmp_path)
    worker.journal.fault = Fault("after_receipt_commit")
    with pytest.raises(Crash):
        worker.run()
    request = worker.request(worker.plan.shards[0], 0)
    before = db_bytes(worker.journal)
    assert worker.reconcile(request).status == LookupStatus.FOUND
    assert db_bytes(worker.journal) == before
    assert worker.source.lookups == []


@pytest.mark.parametrize(
    "mutation,classification",
    [
        (lambda r, p: replace(p, request_identity="other-request"), Failure.IDENTITY),
        (
            lambda r, p: replace(p, producer_operation="other-operation"),
            Failure.IDENTITY,
        ),
        (
            lambda r, p: replace(p, upstream_revision="unpinned-upstream"),
            Failure.IDENTITY,
        ),
        (lambda r, p: replace(p, decode_revision="other-decode"), Failure.IDENTITY),
        (lambda r, p: replace(p, rows=p.rows + (p.rows[0],)), Failure.PAGE),
        (lambda r, p: replace(p, rows=tuple(reversed(p.rows))), Failure.PAGE),
        (
            lambda r, p: replace(p, rows=(replace(p.rows[0], day="bad-date"),)),
            Failure.PAGE,
        ),
        (
            lambda r, p: replace(p, rows=(replace(p.rows[0], day="2020-01-01"),)),
            Failure.PAGE,
        ),
        (
            lambda r, p: replace(p, terminal=None, termination_evidence=None),
            Failure.EXHAUSTION,
        ),
        (
            lambda r, p: replace(p, terminal=True, termination_evidence=None),
            Failure.EXHAUSTION,
        ),
    ],
)
def test_bad_pages_retain_raw_but_never_succeed_or_retry(
    tmp_path, mutation, classification
):
    plan = planned()
    source = Source(plan, transform=mutation)
    worker, _ = runner(tmp_path, plan, source)
    result = worker.run()
    assert result.state == "BLOCKED"
    assert result.shards[0].failures == (classification,)
    receipt = result.shards[0].receipts[0]
    assert receipt.state == "REJECTED"
    assert worker.journal.read_material(receipt).raw.startswith(b"invented")
    assert worker.run(resume=True) == result
    assert len(source.calls) == 1


@pytest.mark.parametrize(
    "mutation,classification",
    [
        (
            lambda r, p: replace(p, snapshot_anchor="drift") if r.page_index else p,
            Failure.DRIFT,
        ),
        (
            lambda r, p: (
                replace(
                    p,
                    rows=(
                        replace(
                            p.rows[0],
                            day=(r.shard.window.start + timedelta(days=1)).isoformat(),
                        ),
                    )
                    + p.rows[1:],
                )
                if r.page_index == 1
                else p
            ),
            Failure.DRIFT,
        ),
        (
            lambda r, p: replace(p, rows=p.rows[:1]) if r.page_index == 0 else p,
            Failure.EXHAUSTION,
        ),
        (
            lambda r, p: (
                replace(p, terminal=False, termination_evidence=None)
                if r.page_index == 2
                else p
            ),
            Failure.EXHAUSTION,
        ),
    ],
)
def test_drift_short_page_and_exhaustion_block_further_pages(
    tmp_path, mutation, classification
):
    plan = planned(size=2, pages=3)
    worker, _ = runner(tmp_path, plan, Source(plan, transform=mutation))
    result = worker.run()
    assert result.state == "BLOCKED"
    assert classification in result.shards[0].failures
    count = len(worker.source.calls)
    worker.run(resume=True)
    assert len(worker.source.calls) == count


@pytest.mark.parametrize("order", ["ascending", "descending"])
@pytest.mark.parametrize("recover", [False, True])
def test_nonadjacent_locator_conflict_blocks_third_page(tmp_path, order, recover):
    plan = planned(size=2, pages=3, order=order)
    worker, clock = runner(tmp_path, plan)
    first_locator = None

    def conflict(request, page):
        nonlocal first_locator
        if request.page_index == 0:
            first_locator = page.rows[0].locator
        if request.page_index == 2:
            return replace(
                page,
                rows=(replace(page.rows[0], locator=first_locator),) + page.rows[1:],
            )
        return page

    worker.source.transform = conflict
    if recover:

        def crash_third(event):
            if event == "before_receipt_commit" and len(worker.source.calls) == 3:
                raise Crash()

        worker.journal.fault = crash_third
        with pytest.raises(Crash):
            worker.run()
        worker = DailyRunner.restore(
            plan.spec.identity,
            Journal(tmp_path, clock_ns=clock),
            worker.source,
            sleep=clock.sleep,
        )
        request = worker.request(plan.shards[0], 2)
        result = worker.accept_reconciliation(request, worker.reconcile(request))
    else:
        result = worker.run()
    assert result.state == "BLOCKED"
    assert result.shards[0].failures == (Failure.DRIFT,)
    assert [receipt.state for receipt in result.shards[0].receipts] == [
        "SUCCEEDED",
        "SUCCEEDED",
        "REJECTED",
    ]
    assert worker.run(resume=True) == result
    assert len(worker.source.calls) == len(set(worker.source.calls)) == 3


@pytest.mark.parametrize(
    "axis",
    [
        "connections",
        "probes",
        "handshakes",
        "heartbeats",
        "redials",
        "retries",
        "hidden-request",
    ],
)
def test_every_transport_effect_is_precharged_and_zero_retry_is_enforced(
    tmp_path, axis
):
    plan = planned()
    limit = {
        "connections": plan.spec.budget.max_connections,
        "probes": plan.spec.budget.max_probes,
    }.get(axis, 0)
    source = Source(plan, effects=((axis, limit + 1),))
    worker, _ = runner(tmp_path, plan, source)
    result = worker.run()
    assert result.state == "UNKNOWN"
    assert result.shards[0].failures == (Failure.BUDGET,)
    assert dict(result.counters).get(axis, 0) == 0
    assert not result.shards[0].receipts
    worker.run(resume=True)
    assert len(source.calls) == 1


def test_explicit_extra_transport_limits_are_pinned_and_counted(tmp_path):
    plan = planned()
    source = Source(
        plan,
        effects=(
            ("probes", 1),
            ("connections", 1),
            ("handshakes", 1),
            ("heartbeats", 2),
        ),
    )
    worker, _ = runner(tmp_path, plan, source, policy=RuntimePolicy(1, 2))
    assert worker.run().state == "COMPLETE"
    counters = dict(worker.report().counters)
    assert [
        counters[key] for key in ("probes", "connections", "handshakes", "heartbeats")
    ] == [1, 1, 1, 2]
    altered = DailyRunner(plan, worker.journal, source, policy=RuntimePolicy(2, 2))
    with pytest.raises(DailyError, match="IDENTITY_MISMATCH"):
        altered.accept()


@pytest.mark.parametrize("kind", ["raw", "metadata"])
def test_oversized_return_is_counted_and_not_persisted_or_retried(tmp_path, kind):
    plan = planned()
    transform = (
        (lambda r, p: replace(p, raw=b"x" * (plan.spec.budget.max_response_bytes + 1)))
        if kind == "raw"
        else (lambda r, p: replace(p, snapshot_anchor="x" * 5000))
    )
    worker, _ = runner(
        tmp_path,
        plan,
        Source(plan, transform=transform),
        policy=RuntimePolicy(max_metadata_bytes=2000)
        if kind == "metadata"
        else RuntimePolicy(),
    )
    result = worker.run()
    assert result.state == "BLOCKED"
    assert result.shards[0].failures == (Failure.BUDGET,)
    assert dict(result.counters)["response_bytes"] > 0
    assert dict(result.counters).get("material_bytes", 0) == 0
    assert not result.shards[0].receipts
    request = worker.request(plan.shards[0], 0)
    attempt = worker.journal.attempt(request)
    assert attempt["observed_bytes"] == len(
        worker.source.retained[request.identity].raw
    )
    assert attempt["observed_rows"] == 5
    assert (
        attempt["response_digest"]
        == sha256(worker.source.retained[request.identity].raw).hexdigest()
    )
    assert attempt["observed_at_ns"] is not None
    assert worker.run(resume=True) == result
    assert len(worker.source.calls) == 1


def test_cancel_after_acquisition_keeps_material_and_resume_only_unfinished_pages(
    tmp_path,
):
    plan = planned(size=2, pages=3)
    worker, _ = runner(tmp_path, plan)

    def cancel_after_page(request, page):
        if request.page_index == 0:
            worker.journal.cancel(plan.spec.identity)
        return page

    worker.source.transform = cancel_after_page
    result = worker.run()
    assert result.state == "CANCELLED"
    assert len(result.shards[0].receipts) == 1
    assert len(worker.source.calls) == 1
    snapshot = db_bytes(worker.journal)
    worker.cancel()
    worker.cancel()
    assert db_bytes(worker.journal) == snapshot
    assert worker.run().state == "CANCELLED"
    worker.source.transform = lambda r, p: p
    assert worker.run(resume=True).state == "COMPLETE"
    assert len(worker.source.calls) == 3
    assert dict(worker.report().counters)["requests"] == 3


def test_cancel_before_start_never_invokes_source_and_does_not_reset_budget(tmp_path):
    worker, _ = runner(tmp_path)
    worker.accept()
    accepted = worker.journal.job(worker.plan.spec.identity)["accepted_ns"]
    worker.cancel()
    assert worker.run().state == "CANCELLED"
    assert worker.source.calls == []
    assert worker.run(resume=True).state == "COMPLETE"
    assert worker.journal.job(worker.plan.spec.identity)["accepted_ns"] == accepted


def test_wall_timeout_and_clock_regression_never_expand_budget(tmp_path):
    worker, clock = runner(tmp_path)
    worker.accept()
    clock.now += worker.plan.spec.budget.wall_seconds * 10**9
    assert worker.run().state == "BLOCKED"
    assert worker.report().state == "BLOCKED"
    assert worker.report().stop_reason == Failure.BUDGET
    assert worker.source.calls == []
    clock.now -= (worker.plan.spec.budget.wall_seconds + 1) * 10**9
    assert worker.run(resume=True).state == "BLOCKED"
    assert worker.source.calls == []


@pytest.mark.parametrize(
    ("start_seconds", "restart_seconds"), [(0, -3600), (10, 9), (10, 1000), (10, 1001)]
)
def test_restart_checks_clock_and_wall_before_rate_sleep(
    tmp_path, start_seconds, restart_seconds
):
    worker, clock = runner(tmp_path, planned(size=3, pages=2))
    job_id = worker.accept()
    accepted = clock.now
    clock.now += start_seconds * 10**9
    worker.journal.fault = Fault("after_receipt_commit")
    with pytest.raises(Crash):
        worker.run()
    request = worker.request(worker.plan.shards[0], 0)
    receipt = worker.journal.receipt(request)
    assert receipt.state == "SUCCEEDED"
    assert worker.journal.read_material(receipt).terminal is False
    original = worker.journal.job(job_id)
    assert original["last_request_ns"] == accepted + start_seconds * 10**9
    assert clock.sleeps == []

    clock.now = accepted + restart_seconds * 10**9
    restored = DailyRunner.restore(
        job_id, Journal(tmp_path, clock_ns=clock), worker.source, sleep=clock.sleep
    )
    result = restored.run()
    assert clock.sleeps == []
    assert len(worker.source.calls) == 1
    assert result.state == "BLOCKED"
    assert result.stop_reason == Failure.BUDGET
    stopped = restored.journal.job(job_id)
    assert stopped["stop_reason"] == Failure.BUDGET
    assert stopped["accepted_ns"] == original["accepted_ns"]
    assert stopped["last_request_ns"] == original["last_request_ns"]
    assert stopped["counters"] == original["counters"]
    assert restored.journal.receipt(request) == receipt
    assert restored.journal.attempt(restored.request(worker.plan.shards[0], 1)) is None
    # Restoring the clock and explicitly resuming cancellation cannot erase STOP.
    clock.now = accepted + 11 * 10**9
    again = DailyRunner.restore(
        job_id, Journal(tmp_path, clock_ns=clock), worker.source, sleep=clock.sleep
    )
    assert again.run() == result
    again.cancel()
    assert again.run(resume=True) == result
    assert again.run(resume=True) == result
    assert clock.sleeps == []
    assert len(worker.source.calls) == 1
    events = [event["event"] for event in again.journal.events(job_id)]
    assert events.count("BLOCKED:BUDGET_EXCEEDED") == 1
    with again.journal.claim(job_id, 30):
        pass


@pytest.mark.parametrize(
    ("wall_seconds", "start_ns", "wait", "state"),
    [
        (1, 500_000_000, 0.5, "BLOCKED"),
        (1, 0, 1.0, "BLOCKED"),
        (2, 500_000_000, 1.0, "COMPLETE"),
    ],
)
def test_restart_rate_wait_bounded_by_remaining_wall_budget(
    tmp_path, wall_seconds, start_ns, wait, state
):
    plan = planned(size=3, pages=2)
    plan = planned(
        size=3,
        pages=2,
        spec=replace(
            plan.spec, budget=replace(plan.spec.budget, wall_seconds=wall_seconds)
        ),
    )
    worker, clock = runner(tmp_path, plan)
    job_id = worker.accept()
    accepted = clock.now
    clock.now += start_ns
    worker.journal.fault = Fault("after_receipt_commit")
    with pytest.raises(Crash):
        worker.run()
    restored = DailyRunner.restore(
        job_id, Journal(tmp_path, clock_ns=clock), worker.source, sleep=clock.sleep
    )
    result = restored.run()
    assert clock.sleeps == [wait]
    assert wait * 10**9 <= wall_seconds * 10**9 - start_ns
    assert result.state == state
    assert len(worker.source.calls) == (2 if state == "COMPLETE" else 1)
    assert restored.journal.job(job_id)["accepted_ns"] == accepted
    assert restored.run(resume=True) == result


@pytest.mark.parametrize("kind", ["raw", "metadata", "transport"])
def test_job_budget_stop_prevents_other_shard_after_restart_and_resume(tmp_path, kind):
    plan = planned(spec=job())
    assert len(plan.shards) == 2
    total_bytes = plan.request_count * plan.spec.budget.max_response_bytes
    source = Source(
        plan,
        transform=(
            (lambda r, p: replace(p, raw=b"x" * (total_bytes + 1)))
            if kind == "raw"
            else (lambda r, p: replace(p, snapshot_anchor="x" * 5000))
            if kind == "metadata"
            else (lambda r, p: p)
        ),
        effects=(("connections", plan.spec.budget.max_connections + 1),)
        if kind == "transport"
        else (),
    )
    policy = (
        RuntimePolicy(max_metadata_bytes=2000)
        if kind == "metadata"
        else RuntimePolicy()
    )
    worker, clock = runner(tmp_path, plan, source, policy=policy)
    result = worker.run()
    assert len(source.calls) == 1
    repeated = worker.run()
    assert len(source.calls) == 1
    assert repeated == result
    assert result.stop_reason == Failure.BUDGET
    assert worker.journal.job(plan.spec.identity)["stop_reason"] == Failure.BUDGET
    if kind == "raw":
        assert dict(result.counters)["response_bytes"] == 2001
    assert not result.shards[1].receipts
    assert worker.run() == result
    assert worker.run(resume=True) == result
    restored = DailyRunner.restore(
        plan.spec.identity,
        Journal(tmp_path, clock_ns=clock),
        source,
        policy=policy,
        sleep=clock.sleep,
    )
    restored.cancel()
    assert restored.run(resume=True) == result
    assert len(source.calls) == 1
    assert restored.journal.attempt(restored.request(plan.shards[1], 0)) is None
    # Fence at the journal effect boundary too, independent of runner checks.
    with restored.journal.claim(plan.spec.identity, 30) as claim:
        with pytest.raises(DailyError, match="BUDGET_EXCEEDED"):
            restored.journal.begin(
                claim,
                restored.request(plan.shards[1], 0),
                plan.spec.budget.max_requests,
                plan.spec.budget.requests_per_second,
                plan.spec.budget.wall_seconds,
            )
        with pytest.raises(DailyError, match="BUDGET_EXCEEDED"):
            restored.journal.charge(claim, "connections", 1, 500, 1000)
    request = restored.request(plan.shards[0], 0)
    before = db_bytes(restored.journal)
    proof = restored.reconcile(request)
    assert db_bytes(restored.journal) == before
    if kind != "transport":
        assert proof.status == LookupStatus.FOUND
        assert restored.accept_reconciliation(request, proof) == result
        assert restored.journal.reconciliation(request)["evidence"] == proof.evidence
    assert restored.run(resume=True) == result
    assert len(source.calls) == 1


def test_budget_stop_is_atomic_with_response_observation_before_receipt(tmp_path):
    plan = planned(spec=job())
    source = Source(plan, transform=lambda r, p: replace(p, raw=b"x" * 2001))
    worker, clock = runner(tmp_path, plan, source)
    worker.journal.fault = Fault("after_response_commit")
    with pytest.raises(Crash):
        worker.run()
    restored = DailyRunner.restore(
        plan.spec.identity,
        Journal(tmp_path, clock_ns=clock),
        source,
        sleep=clock.sleep,
    )
    request = restored.request(plan.shards[0], 0)
    original = restored.journal.attempt(request)
    assert original["state"] == "STARTED"
    assert original["observed_bytes"] == 2001
    assert restored.journal.receipt(request) is None
    assert restored.run(resume=True).stop_reason == Failure.BUDGET
    assert len(source.calls) == 1
    proof = restored.reconcile(request)
    result = restored.accept_reconciliation(request, proof)
    assert result.state == "BLOCKED"
    assert (
        restored.journal.attempt(request)["observed_at_ns"]
        == original["observed_at_ns"]
    )
    assert dict(result.counters)["response_bytes"] == 2001
    assert dict(result.counters)["returned_pages"] == 1
    assert restored.accept_reconciliation(request, proof) == result
    assert restored.run(resume=True) == result
    assert len(source.calls) == 1


@pytest.mark.parametrize("size", [1001, 2001])
@pytest.mark.parametrize("field", ["snapshot", "native_value"])
@pytest.mark.parametrize("crash", [None, "after_source", "after_response_commit"])
def test_malformed_metadata_overrun_is_durable_and_found_counts_once(
    tmp_path, size, field, crash
):
    plan = planned(spec=job())
    assert len(plan.shards) == 2

    def malformed(request, page):
        page = replace(page, raw=b"x" * size)
        if field == "snapshot":
            return replace(page, snapshot_anchor="bad-\ud800")
        row = page.rows[0]
        native = replace(row.fields[0], value="bad-\ud800")
        return replace(
            page,
            rows=(replace(row, fields=(native,) + row.fields[1:]),) + page.rows[1:],
        )

    source = Source(plan, transform=malformed)
    worker, clock = runner(
        tmp_path,
        plan,
        source,
        fault=Fault(crash) if crash == "after_source" else lambda _: None,
    )
    if crash == "after_response_commit":
        worker.journal.fault = Fault(crash)
    if crash is None:
        assert worker.run().stop_reason == Failure.BUDGET
    else:
        with pytest.raises(Crash):
            worker.run()
    request = worker.request(plan.shards[0], 0)
    original = worker.journal.attempt(request)
    page = source.retained[request.identity]
    if crash != "after_source":
        assert original["observed_bytes"] == size
        assert original["observed_rows"] == len(page.rows)
        assert original["observed_at_ns"] == clock.now
        assert original["response_digest"] == sha256(page.raw).hexdigest()
        assert worker.report().stop_reason == Failure.BUDGET

    restored = DailyRunner.restore(
        plan.spec.identity, Journal(tmp_path, clock_ns=clock), source, sleep=clock.sleep
    )
    if crash != "after_source":
        assert restored.run(resume=True).stop_reason == Failure.BUDGET
    before = db_bytes(restored.journal)
    proof = restored.reconcile(request)
    assert proof.status == LookupStatus.FOUND
    assert db_bytes(restored.journal) == before
    result = restored.accept_reconciliation(request, proof)
    assert result.stop_reason == Failure.BUDGET
    assert result.state == "BLOCKED"
    attempt = restored.journal.attempt(request)
    assert attempt["state"] == "REJECTED"
    assert attempt["classification"] == Failure.BUDGET
    assert attempt["observed_bytes"] == size
    assert attempt["observed_rows"] == len(page.rows)
    assert attempt["response_digest"] == sha256(page.raw).hexdigest()
    assert attempt["observed_at_ns"] == clock.now
    counters = dict(result.counters)
    assert counters["response_bytes"] == size
    assert counters["rows"] == len(page.rows)
    assert counters["returned_pages"] == 1
    assert counters.get("material_bytes", 0) == 0
    assert counters.get("metadata_bytes", 0) == 0
    assert not result.shards[0].receipts
    assert restored.journal.attempt(restored.request(plan.shards[1], 0)) is None
    observation = restored.journal.reconciliation(request)
    assert observation["status"] == LookupStatus.FOUND
    assert restored.accept_reconciliation(request, proof) == result
    assert restored.journal.attempt(request) == attempt
    assert restored.journal.reconciliation(request) == observation
    restored.cancel()
    assert restored.run(resume=True) == result
    assert len(source.calls) == 1
    assert (
        sum(
            event["event"] == "RESPONSE_OBSERVED"
            for event in restored.journal.events(plan.spec.identity)
        )
        == 1
    )


def test_serialization_failure_cannot_erase_effects_or_allow_absent(
    tmp_path, monkeypatch
):
    plan = planned(spec=job())
    source = Source(plan, transform=lambda r, p: replace(p, raw=b"x" * 2001))
    worker, clock = runner(tmp_path, plan, source)

    def broken_metadata(page):
        raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "synthetic-invalid")

    with monkeypatch.context() as patch:
        patch.setattr(
            "market_data_foundation.historical.market_daily_journal.page_metadata",
            broken_metadata,
        )
        result = worker.run()
    assert result.stop_reason == Failure.BUDGET
    request = worker.request(plan.shards[0], 0)
    attempt = worker.journal.attempt(request)
    assert attempt["response_identity"] is None
    assert attempt["observed_bytes"] == 2001
    assert attempt["observed_at_ns"] == clock.now
    assert attempt["response_digest"] == sha256(b"x" * 2001).hexdigest()
    restored = DailyRunner.restore(
        plan.spec.identity, Journal(tmp_path, clock_ns=clock), source, sleep=clock.sleep
    )
    with pytest.raises(DailyError, match="IDENTITY_MISMATCH"):
        restored.accept_reconciliation(
            request, Lookup(request.identity, LookupStatus.ABSENT, "false-absent")
        )
    proof = restored.reconcile(request)
    with pytest.raises(DailyError, match="IDENTITY_MISMATCH"):
        restored.accept_reconciliation(
            request, replace(proof, page=replace(proof.page, raw=b"y" * 2001))
        )
    final = restored.accept_reconciliation(request, proof)
    assert final.stop_reason == Failure.BUDGET
    assert dict(final.counters)["response_bytes"] == 2001
    assert dict(final.counters)["returned_pages"] == 1
    assert restored.journal.attempt(request)["observed_at_ns"] == clock.now
    assert restored.accept_reconciliation(request, proof) == final
    assert restored.run(resume=True) == final
    assert len(source.calls) == 1


@pytest.mark.parametrize("latency", [2, 3, 4])
@pytest.mark.parametrize("recover", [False, True])
def test_response_acceptance_enforces_original_job_wall_deadline(
    tmp_path, latency, recover
):
    plan = planned()
    plan = planned(
        spec=replace(plan.spec, budget=replace(plan.spec.budget, wall_seconds=3))
    )
    worker, clock = runner(tmp_path, plan)
    worker.accept()
    accepted_ns = worker.journal.job(plan.spec.identity)["accepted_ns"]

    def response(request, page):
        clock.sleep(latency)
        return page

    worker.source.transform = response
    request = worker.request(plan.shards[0], 0)
    if recover:
        worker.journal.fault = Fault("before_receipt_commit")
        with pytest.raises(Crash):
            worker.run()
        original = worker.journal.attempt(request)
        assert worker.journal.receipt(request) is None
        clock.sleep(10)
        worker = DailyRunner.restore(
            plan.spec.identity,
            Journal(tmp_path, clock_ns=clock),
            worker.source,
            sleep=clock.sleep,
        )
        before = db_bytes(worker.journal)
        found = worker.reconcile(request)
        assert db_bytes(worker.journal) == before
        result = worker.accept_reconciliation(request, found)
        assert (
            worker.journal.attempt(request)["observed_at_ns"]
            == original["observed_at_ns"]
        )
        assert worker.accept_reconciliation(request, found) == result
    else:
        result = worker.run()
    expired = latency >= 3
    assert result.state == ("BLOCKED" if expired else "COMPLETE")
    assert result.stop_reason == (Failure.BUDGET if expired else None)
    receipt = result.shards[0].receipts[0]
    assert receipt.state == ("REJECTED" if expired else "SUCCEEDED")
    assert receipt.classification == (Failure.BUDGET if expired else None)
    assert (
        worker.journal.read_material(receipt)
        == worker.source.retained[request.identity]
    )
    attempt = worker.journal.attempt(request)
    assert attempt["started_ns"] == accepted_ns
    assert attempt["observed_at_ns"] == accepted_ns + latency * 10**9
    assert (
        dict(result.counters)["requests"]
        == dict(result.counters)["returned_pages"]
        == 1
    )
    assert worker.run(resume=True) == result
    assert len(worker.source.calls) == 1


def test_found_without_durable_response_time_cannot_invent_timely_observation(tmp_path):
    plan = planned()
    plan = planned(
        spec=replace(plan.spec, budget=replace(plan.spec.budget, wall_seconds=3))
    )
    worker, clock = runner(tmp_path, plan, fault=Fault("after_source"))
    with pytest.raises(Crash):
        worker.run()
    request = worker.request(plan.shards[0], 0)
    original = worker.journal.attempt(request)
    assert original["observed_at_ns"] is None
    clock.sleep(4)
    restored = DailyRunner.restore(
        plan.spec.identity,
        Journal(tmp_path, clock_ns=clock),
        worker.source,
        sleep=clock.sleep,
    )
    result = restored.accept_reconciliation(request, restored.reconcile(request))
    assert result.state == "BLOCKED"
    assert result.stop_reason == Failure.BUDGET
    assert result.shards[0].receipts[0].classification == Failure.BUDGET
    attempt = restored.journal.attempt(request)
    assert attempt["started_ns"] == original["started_ns"]
    assert attempt["observed_at_ns"] == clock.now
    assert restored.run(resume=True) == result
    assert len(worker.source.calls) == 1


def test_response_wall_deadline_starts_at_acceptance_not_attempt(tmp_path):
    plan = planned()
    plan = planned(
        spec=replace(plan.spec, budget=replace(plan.spec.budget, wall_seconds=3))
    )
    worker, clock = runner(tmp_path, plan)
    worker.accept()
    accepted = clock.now
    clock.sleep(2)

    def response(request, page):
        clock.sleep(2)
        return page

    worker.source.transform = response
    result = worker.run()
    assert result.state == "BLOCKED"
    assert result.stop_reason == Failure.BUDGET
    receipt = result.shards[0].receipts[0]
    assert receipt.classification == Failure.BUDGET
    attempt = worker.journal.attempt(receipt.request)
    assert attempt["started_ns"] == accepted + 2 * 10**9
    assert attempt["observed_at_ns"] == accepted + 4 * 10**9
    assert worker.journal.read_material(receipt).raw.startswith(b"invented")


def test_slow_source_retains_raw_with_timeout_classification(tmp_path):
    worker, clock = runner(tmp_path)

    def slow(request, page):
        clock.sleep(worker.plan.spec.budget.timeout_seconds + 1)
        return page

    worker.source.transform = slow
    result = worker.run()
    assert result.state == "BLOCKED"
    assert result.shards[0].failures == (Failure.TIMEOUT,)
    assert result.shards[0].receipts


@pytest.mark.parametrize("latency", [1, 5, 6])
def test_receipt_rollback_reconciliation_preserves_original_response_timing(
    tmp_path, latency
):
    worker, clock = runner(tmp_path)

    def response(request, page):
        clock.sleep(latency)
        return page

    worker.source.transform = response
    worker.journal.fault = Fault("before_receipt_commit")
    with pytest.raises(Crash):
        worker.run()
    request = worker.request(worker.plan.shards[0], 0)
    original = worker.journal.attempt(request)
    assert original["observed_at_ns"] - original["started_ns"] == latency * 10**9
    assert worker.journal.receipt(request) is None
    clock.sleep(10)  # Recovery delay must not replace the original response clock.
    restarted = DailyRunner.restore(
        worker.plan.spec.identity,
        Journal(tmp_path, clock_ns=clock),
        worker.source,
        sleep=clock.sleep,
    )
    before = db_bytes(restarted.journal)
    found = restarted.reconcile(request)
    assert found.status == LookupStatus.FOUND
    assert db_bytes(restarted.journal) == before
    result = restarted.accept_reconciliation(request, found)
    timed_out = latency > worker.plan.spec.budget.timeout_seconds
    assert result.state == ("BLOCKED" if timed_out else "COMPLETE")
    assert result.shards[0].failures == ((Failure.TIMEOUT,) if timed_out else ())
    receipt = result.shards[0].receipts[0]
    assert receipt.state == ("REJECTED" if timed_out else "SUCCEEDED")
    assert receipt.classification == (Failure.TIMEOUT if timed_out else None)
    assert restarted.journal.read_material(receipt) == found.page
    recovered = restarted.journal.attempt(request)
    assert recovered["started_ns"] == original["started_ns"]
    assert recovered["observed_at_ns"] == original["observed_at_ns"]
    assert dict(result.counters)["requests"] == 1
    assert dict(result.counters)["returned_pages"] == 1
    assert restarted.accept_reconciliation(request, found) == result
    assert restarted.run(resume=True) == result
    assert len(worker.source.calls) == 1


@pytest.mark.parametrize(
    "exception,classification",
    [
        (TimeoutError, Failure.TIMEOUT),
        (ConnectionError, Failure.TRANSPORT),
        (ValueError, Failure.UNKNOWN),
    ],
)
def test_exception_messages_are_never_journaled_and_effect_stays_unknown(
    tmp_path, exception, classification
):
    worker, _ = runner(tmp_path)

    def fail(request, page):
        raise exception("sensitive-fixture-exception-text")

    worker.source.transform = fail
    result = worker.run()
    assert result.state == "UNKNOWN"
    assert result.shards[0].failures == (classification,)
    assert b"sensitive-fixture-exception-text" not in worker.journal.path.read_bytes()
    assert not worker.recovery_candidates(worker.coverage(), budget()).backfill


def test_absent_original_is_read_only_verified_and_becomes_independent_gap_candidate(
    tmp_path,
):
    worker, _ = runner(tmp_path, fault=Fault("before_source"))
    with pytest.raises(Crash):
        worker.run()
    request = worker.request(worker.plan.shards[0], 0)
    worker.source.lookup_original = lambda r: Lookup(
        r.identity, LookupStatus.ABSENT, "synthetic-authoritative-no-effect-index"
    )
    before = db_bytes(worker.journal)
    resolution = worker.reconcile(request)
    assert db_bytes(worker.journal) == before
    assert worker.accept_reconciliation(request, resolution).state == "BLOCKED"
    manifest = worker.coverage()
    candidates = worker.recovery_candidates(manifest, budget())
    assert candidates.backfill
    assert all(
        candidate.original_job == worker.plan.spec.identity
        for candidate in candidates.backfill
    )
    assert all(
        candidate.manifest_revision == manifest.revision
        for candidate in candidates.backfill
    )
    assert all(
        candidate.publication_occurrence_rule
        == "new_occurrence_preserve_original_history"
        for candidate in candidates.backfill
    )
    assert all(
        expected_state(worker.plan, point) is None
        for candidate in candidates.backfill
        for point in candidate.target_points
    )
    assert worker.source.calls == []
    assert worker.run(resume=True).state == "BLOCKED"
    assert worker.source.calls == []


def test_raw_success_needs_d3_assessment_and_success_is_excluded_from_gap_plan(
    tmp_path,
):
    worker, _ = runner(tmp_path)
    worker.run()
    unknown = worker.coverage()
    assert unknown.denominator == len(worker.plan.points)
    assert unknown.global_status == CoverageStatus.UNKNOWN
    assert any(
        entry.classification == CoverageClass.UNKNOWN for entry in unknown.entries
    )
    shard = worker.plan.shards[0]
    evidence = tuple(
        PointEvidence(
            point,
            CoverageClass.OBSERVED_VALID,
            ("synthetic-d3-assessment",),
            (shard.identity,),
            material=MATERIAL,
            source_row_locator=f"native-row:{point.day}",
        )
        for point in shard.target_points
        if expected_state(worker.plan, point) is None
    )
    manifest = worker.coverage(evidence)
    assert manifest.axes.bar_obligations == CoverageStatus.COMPLETE
    assert manifest.axes.formal_publication == CoverageStatus.UNKNOWN
    assert not worker.recovery_candidates(manifest, budget()).backfill
    assert not worker.recovery_candidates(manifest, budget()).investigate_original


def test_quality_with_existing_material_is_reprocess_candidate_not_recollection(
    tmp_path,
):
    worker, _ = runner(tmp_path)
    worker.run()
    shard = worker.plan.shards[0]
    point = next(
        point
        for point in shard.target_points
        if expected_state(worker.plan, point) is None
    )
    evidence = PointEvidence(
        point,
        CoverageClass.QUALITY_REJECTED,
        ("synthetic-rejected-assessment",),
        (shard.identity,),
        material=MATERIAL,
    )
    candidates = worker.recovery_candidates(worker.coverage((evidence,)), budget())
    assert point in candidates.reprocess_first
    assert not candidates.backfill


def test_real_universe_rejected_even_when_source_is_retained(tmp_path):
    spec = job()
    content = spec.universe
    calendar = replace(
        content.calendars[0],
        provenance=replace(content.calendars[0].provenance, mode=EvidenceMode.RETAINED),
        qualification=QualificationStatus.UNKNOWN,
    )
    content = replace(
        content,
        provenance=replace(content.provenance, mode=EvidenceMode.RETAINED),
        calendars=(calendar,),
        qualifications=tuple(
            (category, Qualification()) for category, _ in content.qualifications
        ),
    )
    spec = replace(
        spec,
        universe=content,
        routes=tuple(
            replace(route, qualification=QualificationStatus.UNKNOWN)
            for route in spec.routes
        ),
    )
    plan = planned(spec=spec)
    source = Source(plan, mode=EvidenceMode.RETAINED)
    with pytest.raises(ContractError, match="d1_offline_only_execution_not_admitted"):
        runner(tmp_path, plan, source)
    assert source.calls == []


def test_retained_bytes_require_explicit_binding_and_material_allowlist(tmp_path):
    plan = planned()
    source = Source(plan, mode=EvidenceMode.RETAINED)
    worker, _ = runner(tmp_path, plan, source)
    assert worker.run().state == "COMPLETE"
    with pytest.raises(ContractError, match="explicit_retained_materials"):
        replace(source.binding, retained_materials=())
    with pytest.raises(ContractError, match="offline_source_only"):
        replace(source.binding, mode=EvidenceMode.LIVE)
    wrong = Source(
        plan,
        mode=EvidenceMode.RETAINED,
        transform=lambda r, p: replace(p, retained_reference="not-allowlisted"),
    )
    failed, _ = runner(tmp_path / "other", plan, wrong)
    assert failed.run().shards[0].failures == (Failure.IDENTITY,)


def test_forged_plan_and_provider_fallback_are_rejected(tmp_path):
    plan = planned()
    with pytest.raises(ContractError, match="exact_frozen_d1_plan"):
        runner(tmp_path, replace(plan, request_count=0), Source(plan))
    source = Source(plan)
    source.binding = replace(
        source.binding, source=Key(Kind.SOURCE, "other-source", "v1")
    )
    with pytest.raises(ContractError, match="exact_source_binding_mapping"):
        runner(tmp_path, plan, source)
    source.binding = replace(
        source.binding,
        source=plan.shards[0].route.source,
        endpoint_contract="other-endpoint",
    )
    with pytest.raises(ContractError, match="exact_offline_binding"):
        runner(tmp_path, plan, source)


def test_lookup_wrong_original_and_same_identity_different_bytes_refused(tmp_path):
    worker, _ = runner(tmp_path, fault=Fault("after_source"))
    with pytest.raises(Crash):
        worker.run()
    request = worker.request(worker.plan.shards[0], 0)
    worker.source.lookup_original = lambda r: Lookup(
        "another-request", LookupStatus.UNKNOWN, "invented-index"
    )
    with pytest.raises(DailyError, match="IDENTITY_MISMATCH"):
        worker.reconcile(request)
    page = worker.source.retained[request.identity]
    found = Lookup(request.identity, LookupStatus.FOUND, "invented-index", page)
    worker.accept_reconciliation(request, found)
    with pytest.raises(DailyError, match="IDENTITY_MISMATCH"):
        worker.accept_reconciliation(
            request, replace(found, page=replace(page, raw=b"different"))
        )
    with pytest.raises(ContractError, match="original_request_required"):
        worker.reconcile(replace(request, schema_revision="different"))


def test_host_claim_is_exclusive_after_expiry_and_stale_fence_cannot_write(tmp_path):
    worker, clock = runner(tmp_path)
    worker.accept()
    job_id = worker.plan.spec.identity
    with worker.journal.claim(job_id, 1) as old:
        clock.sleep(2)
        with pytest.raises(DailyError, match="HOST_BUSY"):
            with Journal(tmp_path, clock_ns=clock).claim(job_id, 30):
                pytest.fail("stole a running host worker")
        with pytest.raises(DailyError, match="LEASE_LOST"):
            worker.journal.renew(old, 30)
    with worker.journal.claim(job_id, 30) as new:
        assert new.generation > old.generation
        with pytest.raises(DailyError, match="LEASE_LOST"):
            worker.journal.resume(old)


def _crashing_process(directory):
    worker, _ = runner(directory)
    worker.fault = lambda event: os._exit(77) if event == "after_source" else None
    worker.run()


def test_actual_process_exit_releases_host_lock_and_preserves_unknown_attempt(tmp_path):
    process = multiprocessing.get_context("fork").Process(
        target=_crashing_process, args=(tmp_path,)
    )
    process.start()
    process.join(10)
    try:
        assert process.exitcode == 77
        worker, _ = runner(tmp_path)
        assert worker.run().state == "UNKNOWN"
        assert worker.source.calls == []
        assert dict(worker.report().counters)["requests"] == 1
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)


def test_raw_corruption_fails_independent_readback(tmp_path):
    worker, _ = runner(tmp_path)
    receipt = worker.run().shards[0].receipts[0]
    with sqlite3.connect(worker.journal.path) as db:
        db.execute("UPDATE materials SET raw=?", (b"tampered",))
    with pytest.raises(DailyError, match="JOURNAL_INCONSISTENT"):
        worker.journal.read_material(receipt)


def test_canonical_identity_contains_request_binding_and_namespace(tmp_path):
    worker, _ = runner(tmp_path)
    request = worker.request(worker.plan.shards[0], 0)
    encoded = canonical_bytes(request)
    for part in (
        b"invented-request-code",
        b"synthetic.stable",
        b"execution_inputs",
        b"mapping_revision",
        b"definition",
        b"endpoint_contract",
    ):
        assert part in encoded
    assert (
        replace(request, schema_revision="another-schema").identity != request.identity
    )


def test_not_performed_is_confirmed_absent_but_never_automatically_retried(tmp_path):
    worker, _ = runner(tmp_path)

    def not_performed(request, page):
        raise NotPerformed("unrecorded-adapter-detail")

    worker.source.transform = not_performed
    assert worker.run().state == "BLOCKED"
    assert worker.recovery_candidates(worker.coverage(), budget()).backfill
    worker.run(resume=True)
    assert len(worker.source.calls) == 1
    assert b"unrecorded-adapter-detail" not in worker.journal.path.read_bytes()


def test_restart_reconstructs_frozen_plan_from_durable_intent(tmp_path):
    worker, clock = runner(tmp_path, planned(size=2, pages=3))
    job_id = worker.accept()
    original = canonical_bytes(worker.plan)
    restored = DailyRunner.restore(
        job_id, Journal(tmp_path, clock_ns=clock), worker.source, sleep=clock.sleep
    )
    assert canonical_bytes(restored.plan) == original
    assert restored.run().state == "COMPLETE"
    with sqlite3.connect(worker.journal.path) as db:
        db.execute(
            "UPDATE jobs SET intent=? WHERE id=?",
            (
                original.replace(b'"max_requests":1000', b'"max_requests":9999')
                + b"\n{}",
                job_id,
            ),
        )
    with pytest.raises(DailyError, match="JOURNAL_INCONSISTENT"):
        worker.journal.load_plan(job_id)


@pytest.mark.parametrize(
    "tamper", [b"{}", b"null", b"[]", b'{"spec":null}', b"not-json"]
)
def test_intent_decode_fails_closed_on_non_d1_content(tmp_path, tamper):
    worker, _ = runner(tmp_path)
    job_id = worker.accept()
    with sqlite3.connect(worker.journal.path) as db:
        db.execute("UPDATE jobs SET intent=? WHERE id=?", (tamper + b"\n{}", job_id))
    with pytest.raises(DailyError, match="JOURNAL_INCONSISTENT"):
        worker.journal.load_plan(job_id)


def test_partial_success_lost_second_page_recovers_same_attempt_then_only_fetches_third(
    tmp_path,
):
    worker, clock = runner(tmp_path, planned(size=2, pages=3))
    count = 0

    def crash_second(event):
        nonlocal count
        if event == "after_source":
            count += 1
            if count == 2:
                raise Crash()

    worker.fault = crash_second
    with pytest.raises(Crash):
        worker.run()
    restarted = DailyRunner.restore(
        worker.plan.spec.identity,
        Journal(tmp_path, clock_ns=clock),
        worker.source,
        sleep=clock.sleep,
    )
    result = restarted.run()
    assert result.state == "UNKNOWN"
    assert len(worker.source.calls) == 2
    assert len(result.shards[0].receipts) == 1
    original = restarted.request(restarted.plan.shards[0], 1)
    snapshot = db_bytes(restarted.journal)
    resolution = restarted.reconcile(original)
    assert db_bytes(restarted.journal) == snapshot
    restarted.accept_reconciliation(original, resolution)
    assert restarted.run(resume=True).state == "COMPLETE"
    assert len(worker.source.calls) == 3
    assert len(set(worker.source.calls)) == 3
    assert dict(restarted.report().counters)["returned_pages"] == 3


def test_receipt_failure_keeps_effect_accounting_and_original_lookup_recovers(
    tmp_path,
):
    worker, _ = runner(tmp_path)

    def broken_storage(event):
        if event == "after_material_insert":
            raise sqlite3.OperationalError("sensitive-fixture-storage-text")

    worker.journal.fault = broken_storage
    result = worker.run()
    assert result.state == "UNKNOWN"
    assert result.shards[0].failures == (Failure.UNAVAILABLE,)
    assert not result.shards[0].receipts
    assert dict(result.counters)["returned_pages"] == 1
    assert dict(result.counters).get("material_bytes", 0) == 0
    assert b"sensitive-fixture-storage-text" not in worker.journal.path.read_bytes()
    worker.journal.fault = lambda _: None
    request = worker.request(worker.plan.shards[0], 0)
    found = worker.reconcile(request)
    with pytest.raises(DailyError, match="IDENTITY_MISMATCH"):
        worker.accept_reconciliation(
            request,
            replace(
                found, page=replace(found.page, raw=b"changed-before-durable-material")
            ),
        )
    assert worker.accept_reconciliation(request, found).state == "COMPLETE"
    assert dict(worker.report().counters)["returned_pages"] == 1
    assert len(worker.source.calls) == 1


def test_known_response_cannot_be_reclassified_absent_after_material_rollback(tmp_path):
    worker, _ = runner(tmp_path)
    worker.journal.fault = Fault("before_receipt_commit")
    with pytest.raises(Crash):
        worker.run()
    request = worker.request(worker.plan.shards[0], 0)
    with pytest.raises(DailyError, match="IDENTITY_MISMATCH"):
        worker.accept_reconciliation(
            request,
            Lookup(request.identity, LookupStatus.ABSENT, "contradictory-index"),
        )
    assert not worker.recovery_candidates(worker.coverage(), budget()).backfill


def test_empty_terminal_page_does_not_invent_no_trade_or_closed_day(tmp_path):
    plan = planned()
    worker, _ = runner(
        tmp_path, plan, Source(plan, transform=lambda r, p: replace(p, rows=()))
    )
    assert worker.run().state == "COMPLETE"
    manifest = worker.coverage()
    assert CoverageClass.NO_TRADE_CONFIRMED not in {
        entry.classification for entry in manifest.entries
    }
    assert any(
        entry.classification == CoverageClass.UNKNOWN for entry in manifest.entries
    )
    assert not worker.recovery_candidates(manifest, budget()).backfill


def test_partial_job_does_not_promote_global_success_or_recollect_successful_shard(
    tmp_path,
):
    plan = planned(spec=job())
    first, second = plan.shards

    def fail_second(request, page):
        if request.shard == second:
            raise NotPerformed()
        return page

    worker, _ = runner(tmp_path, plan, Source(plan, transform=fail_second))
    result = worker.run()
    assert result.state == "BLOCKED"
    assert result.shards[0].state == "COMPLETE"
    assert result.shards[1].state == "BLOCKED"
    candidates = worker.recovery_candidates(worker.coverage(), budget()).backfill
    assert candidates
    assert all(
        candidate.original_shards == (second.identity,) for candidate in candidates
    )
    assert all(
        point not in first.target_points
        for candidate in candidates
        for point in candidate.target_points
    )
    worker.run(resume=True)
    assert len(worker.source.calls) == 2


def test_source_that_blocks_is_interrupted_and_alarm_owner_restored(tmp_path):
    spec = job()
    spec = replace(spec, budget=replace(spec.budget, timeout_seconds=1))
    plan = planned(spec=spec)
    source = Source(plan, transform=lambda r, p: (time.sleep(20), p)[1])
    worker, _ = runner(tmp_path, plan, source)
    handler = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()
    result = worker.run()
    assert time.monotonic() - started < 10
    assert result.state == "UNKNOWN"
    assert Failure.TIMEOUT in result.shards[0].failures
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) == handler


def test_expired_lease_prevents_material_commit_and_preserves_original_unknown(
    tmp_path,
):
    worker, clock = runner(tmp_path)

    def expire_lease(request, page):
        clock.sleep(worker.lease_seconds + 1)
        return page

    worker.source.transform = expire_lease
    with pytest.raises(DailyError, match="LEASE_LOST"):
        worker.run()
    assert worker.report().state == "UNKNOWN"
    assert not worker.report().shards[0].receipts
    assert worker.run().state == "UNKNOWN"
    assert len(worker.source.calls) == 1


@pytest.mark.parametrize(
    "column,value", [("metadata", "{}"), ("byte_length", 0), ("digest", "wrong-digest")]
)
def test_receipt_metadata_and_length_corruption_fail_closed(tmp_path, column, value):
    worker, _ = runner(tmp_path)
    receipt = worker.run().shards[0].receipts[0]
    with sqlite3.connect(worker.journal.path) as db:
        db.execute(f"UPDATE materials SET {column}=?", (value,))
    with pytest.raises(DailyError, match="JOURNAL_INCONSISTENT"):
        worker.journal.read_material(receipt)


def _holding_process(directory, started, release):
    worker, _ = runner(directory)

    def hold(request, page):
        started.set()
        release.wait(4)
        return page

    worker.source.transform = hold
    worker.run()


def test_simultaneous_process_cannot_start_duplicate_acquisition(tmp_path):
    context = multiprocessing.get_context("fork")
    started, release = context.Event(), context.Event()
    process = context.Process(
        target=_holding_process, args=(tmp_path, started, release)
    )
    process.start()
    try:
        assert started.wait(5)
        worker, _ = runner(tmp_path)
        with pytest.raises(DailyError, match="HOST_BUSY"):
            worker.run()
        assert worker.source.calls == []
        release.set()
        process.join(10)
        assert process.exitcode == 0
        assert worker.run().state == "COMPLETE"
        assert worker.source.calls == []
        assert dict(worker.report().counters)["requests"] == 1
    finally:
        release.set()
        if process.is_alive():
            process.kill()
            process.join(5)


def test_worker_rejects_non_main_thread_before_acceptance(tmp_path):
    worker, _ = runner(tmp_path)
    failures = []

    def start():
        try:
            worker.run()
        except ContractError as error:
            failures.append(str(error))

    thread = threading.Thread(target=start)
    thread.start()
    thread.join(5)
    assert failures == ["d2_worker_main_thread_required"]
    assert not worker.source.calls
    assert not worker.journal.attempts(worker.plan.spec.identity)


def test_existing_alarm_owner_is_not_displaced(tmp_path):
    worker, _ = runner(tmp_path)
    handler = signal.getsignal(signal.SIGALRM)
    signal.setitimer(signal.ITIMER_REAL, 1000)
    try:
        with pytest.raises(ContractError, match="alarm_already_owned"):
            worker.run()
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 900
        assert signal.getsignal(signal.SIGALRM) == handler
        assert not worker.source.calls
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def test_lookup_exception_returns_sanitized_unknown_without_writes(tmp_path):
    worker, _ = runner(tmp_path, fault=Fault("before_source"))
    with pytest.raises(Crash):
        worker.run()
    request = worker.request(worker.plan.shards[0], 0)

    def failed_lookup(original):
        raise OSError("sensitive-fixture-lookup-text")

    worker.source.lookup_original = failed_lookup
    before = db_bytes(worker.journal)
    result = worker.reconcile(request)
    assert result.status == LookupStatus.UNKNOWN
    assert result.evidence == Failure.UNKNOWN.value
    assert db_bytes(worker.journal) == before
    assert not worker.source.calls


def test_implicit_retry_denial_keeps_prior_transport_attempt_counts(tmp_path):
    plan = planned()
    source = Source(plan, effects=(("probes", 1), ("connections", 1), ("retries", 1)))
    worker, _ = runner(tmp_path, plan, source)
    result = worker.run()
    assert result.state == "UNKNOWN"
    assert dict(result.counters)["probes"] == dict(result.counters)["connections"] == 1
    assert dict(result.counters).get("retries", 0) == 0
    assert not worker.recovery_candidates(worker.coverage(), budget()).backfill


@pytest.mark.parametrize(
    "denial",
    [
        "connections",
        "probes",
        "handshakes",
        "heartbeats",
        "redials",
        "retries",
        "wall",
        "regression",
        "negative",
        "non_integer",
    ],
)
def test_charge_denial_commits_stop_before_runner_block(tmp_path, monkeypatch, denial):
    plan = planned(spec=replace(job(), budget=replace(budget(), wall_seconds=3)))
    assert len(plan.shards) == 2
    source = Source(plan)
    worker, clock = runner(tmp_path, plan, source)

    def denied_fetch(request, charge):
        source.calls.append(request.identity)
        charge("probes", 1)
        charge("connections", 1)
        if denial == "wall":
            clock.now += 3 * 10**9
        elif denial == "regression":
            clock.now -= 1
        if denial in ("wall", "regression"):
            charge("connections", 0)
        elif denial == "negative":
            charge("connections", -1)
        elif denial == "non_integer":
            charge("connections", True)
        else:
            charge(denial, worker._limits()[denial] + 1)
        pytest.fail("denied charge returned")

    def crash_before_runner_block(claim, failure):
        assert failure == Failure.BUDGET
        # The rejecting charge must already have committed, independent of this
        # runner fallback. A new connection sees its STOP before process loss.
        assert worker.journal.job(plan.spec.identity)["stop_reason"] == Failure.BUDGET
        raise Crash("before_runner_block")

    monkeypatch.setattr(source, "fetch", denied_fetch)
    monkeypatch.setattr(worker.journal, "block", crash_before_runner_block)
    with pytest.raises(Crash):
        worker.run()
    clock.now = 10**18
    restored = DailyRunner.restore(
        plan.spec.identity,
        Journal(tmp_path, clock_ns=clock),
        source,
        sleep=clock.sleep,
    )
    request = restored.request(plan.shards[0], 0)
    attempt = restored.journal.attempt(request)
    assert attempt["state"] == "STARTED"
    counters = {"requests": 1, "probes": 1, "connections": 1}
    assert restored.journal.job(plan.spec.identity)["counters"] == counters
    assert restored.journal.receipt(request) is None
    for resume in (False, True, True):
        assert restored.run(resume=resume).stop_reason == Failure.BUDGET
    restored.cancel()
    assert restored.run(resume=True).stop_reason == Failure.BUDGET
    assert restored.journal.attempt(request) == attempt
    assert restored.journal.attempt(restored.request(plan.shards[1], 0)) is None
    assert source.calls == [request.identity]
    with restored.journal.claim(plan.spec.identity, 30) as claim:
        with pytest.raises(DailyError, match="BUDGET_EXCEEDED"):
            restored.journal.charge(claim, "connections", 1, 500, 1000)
    assert restored.journal.job(plan.spec.identity)["counters"] == counters
    assert (
        sum(
            e["event"] == "BLOCKED:" + Failure.BUDGET
            for e in restored.journal.events(plan.spec.identity)
        )
        == 1
    )


def _exit_before_charge_runner_block(directory):
    plan = planned(spec=job())
    source = Source(plan, effects=(("probes", 1), ("connections", 1), ("retries", 1)))
    worker, _ = runner(directory, plan, source)
    worker.journal.block = lambda claim, failure: os._exit(79)
    worker.run()


def test_process_exit_after_charge_denial_preserves_stop_and_original_counts(tmp_path):
    process = multiprocessing.get_context("fork").Process(
        target=_exit_before_charge_runner_block, args=(tmp_path,)
    )
    process.start()
    process.join(10)
    try:
        assert process.exitcode == 79
        plan = planned(spec=job())
        source = Source(plan)
        clock = Clock()
        restored = DailyRunner.restore(
            plan.spec.identity,
            Journal(tmp_path, clock_ns=clock),
            source,
            sleep=clock.sleep,
        )
        request = restored.request(plan.shards[0], 0)
        assert restored.journal.attempt(request)["state"] == "STARTED"
        assert restored.run(resume=True).stop_reason == Failure.BUDGET
        assert not source.calls
        assert restored.journal.attempt(restored.request(plan.shards[1], 0)) is None
        assert restored.journal.job(plan.spec.identity)["counters"] == {
            "requests": 1,
            "probes": 1,
            "connections": 1,
        }
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)


@pytest.mark.parametrize("multiple_shards", [False, True])
@pytest.mark.parametrize("initial_state", ["REJECTED", "UNKNOWN", "STARTED"])
@pytest.mark.parametrize(
    "event", [None, "after_reconciliation_insert", "after_reconciliation_commit"]
)
def test_metadata_budget_is_cumulative_across_pages_and_cannot_be_reset(
    tmp_path, monkeypatch, event, multiple_shards, initial_state
):
    plan = planned(size=2, pages=3, spec=job() if multiple_shards else None)
    policy = RuntimePolicy(max_metadata_bytes=2400)
    worker, clock = runner(tmp_path, plan, policy=policy)
    finish = worker.journal.finish

    def interrupted_finish(claim, request, *args, **kwargs):
        if request.page_index == 1 and initial_state != "REJECTED":
            # Response and cumulative metadata STOP are already committed;
            # interrupt before any material transaction can accept this page.
            if initial_state == "STARTED":
                raise Crash("before_cumulative_receipt")
            raise DailyError(Failure.UNAVAILABLE)
        return finish(claim, request, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(worker.journal, "finish", interrupted_finish)
        if initial_state == "STARTED":
            with pytest.raises(Crash):
                worker.run()
            result = worker.report()
        else:
            result = worker.run()
    assert len(result.shards[0].receipts) == 1
    assert result.shards[0].failures == (
        Failure.BUDGET
        if initial_state == "REJECTED"
        else Failure.UNAVAILABLE
        if initial_state == "UNKNOWN"
        else Failure.UNKNOWN,
    )
    assert len(worker.source.calls) == 2
    assert result.stop_reason == Failure.BUDGET
    request = worker.request(plan.shards[0], 1)
    original = worker.journal.attempt(request)
    assert original["state"] == initial_state
    counters = result.counters
    receipt = result.shards[0].receipts[0]
    material = worker.journal.read_material(receipt)
    before = db_bytes(worker.journal)
    proof = worker.reconcile(request)
    assert proof.status == LookupStatus.FOUND
    assert db_bytes(worker.journal) == before
    for invalid in (
        replace(proof, status=LookupStatus.ABSENT, page=None),
        replace(proof, page=replace(proof.page, raw=b"different-original")),
        replace(proof, page=replace(proof.page, snapshot_anchor="different-metadata")),
    ):
        with pytest.raises(DailyError, match="IDENTITY_MISMATCH"):
            worker.accept_reconciliation(request, invalid)
        assert worker.journal.reconciliation(request) is None
        assert worker.report().counters == counters
    worker.journal.fault = Fault(event)
    if event is None:
        assert worker.accept_reconciliation(request, proof).state == "BLOCKED"
    else:
        with pytest.raises(Crash):
            worker.accept_reconciliation(request, proof)
    restored = DailyRunner.restore(
        plan.spec.identity,
        Journal(tmp_path, clock_ns=clock),
        worker.source,
        policy=policy,
        sleep=clock.sleep,
    )
    assert (restored.journal.reconciliation(request) is not None) == (
        event != "after_reconciliation_insert"
    )
    assert restored.journal.attempt(request)["state"] == (
        "STARTED"
        if event == "after_reconciliation_insert" and initial_state != "REJECTED"
        else "REJECTED"
    )
    accepted = restored.accept_reconciliation(request, proof)
    assert accepted.state == "BLOCKED"
    assert accepted.counters == counters
    attempt = restored.journal.attempt(request)
    assert attempt["state"] == "REJECTED"
    assert attempt["classification"] == Failure.BUDGET
    for name in (
        "id",
        "request",
        "started_ns",
        "observed_bytes",
        "observed_rows",
        "observed_at_ns",
        "response_digest",
        "response_identity",
    ):
        assert attempt[name] == original[name]
    assert restored.journal.receipt(request) is None
    assert restored.journal.receipt(receipt.request) == receipt
    assert restored.journal.read_material(receipt) == material
    observation = restored.journal.reconciliation(request)
    assert observation["status"] == LookupStatus.FOUND
    assert observation["evidence"] == proof.evidence
    assert observation["response_identity"] == original["response_identity"]
    assert restored.reconcile(request).status == LookupStatus.UNKNOWN
    assert restored.reconcile(request).evidence == proof.evidence
    events = restored.journal.events(plan.spec.identity)
    assert sum(e["event"] == "RECONCILIATION_ACCEPTED" for e in events) == 1
    for invalid in (
        replace(proof, evidence="changed-proof"),
        replace(proof, page=replace(proof.page, raw=b"different-original")),
    ):
        with pytest.raises(DailyError, match="IDENTITY_MISMATCH"):
            restored.accept_reconciliation(request, invalid)
        assert restored.journal.reconciliation(request) == observation
    assert restored.accept_reconciliation(request, proof) == accepted
    assert restored.journal.events(plan.spec.identity) == events
    restored.cancel()
    assert restored.run(resume=True) == accepted
    assert restored.run() == accepted
    assert len(worker.source.calls) == 2
    if multiple_shards:
        assert restored.journal.attempt(restored.request(plan.shards[1], 0)) is None


def test_cumulative_metadata_exact_allowance_accepts_all_pages(tmp_path):
    plan = planned(size=2, pages=3)
    baseline, _ = runner(tmp_path / "baseline", plan)
    report = baseline.run()
    assert report.state == "COMPLETE"
    exact_metadata_bytes = sum(
        len(receipt.metadata.encode()) for receipt in report.shards[0].receipts
    )
    worker, _ = runner(
        tmp_path / "exact",
        plan,
        policy=RuntimePolicy(max_metadata_bytes=exact_metadata_bytes),
    )
    result = worker.run()
    assert result.state == "COMPLETE"
    assert result.stop_reason is None
    assert dict(result.counters)["metadata_bytes"] == exact_metadata_bytes
    assert len(worker.source.calls) == 3


def test_host_claim_excludes_different_jobs_in_the_same_journal(tmp_path):
    first, _ = runner(tmp_path)
    second_plan = planned(spec=replace(job(), occurrence="independent-second-job"))
    second, _ = runner(tmp_path, second_plan)
    first.accept()
    with first.journal.claim(first.plan.spec.identity, 30):
        with pytest.raises(DailyError, match="HOST_BUSY"):
            second.run()
    assert not second.source.calls
    assert not second.journal.attempts(second.plan.spec.identity)


@pytest.mark.parametrize("evidence", [True, False, 1, None, "", " \t\n"])
@pytest.mark.parametrize("reconcile", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_terminal_evidence_requires_nonblank_string_and_retains_rejected_raw(
    tmp_path, evidence, reconcile, empty
):
    plan = planned()
    source = Source(
        plan,
        transform=lambda r, p: replace(
            p, termination_evidence=evidence, rows=() if empty else p.rows
        ),
    )
    worker, clock = runner(
        tmp_path,
        plan,
        source,
        fault=Fault("after_source") if reconcile else lambda _: None,
    )
    request = worker.request(plan.shards[0], 0)
    if reconcile:
        with pytest.raises(Crash):
            worker.run()
        result = worker.accept_reconciliation(request, worker.reconcile(request))
    else:
        result = worker.run()
    assert result.state == "BLOCKED"
    receipt = result.shards[0].receipts[0]
    assert receipt.state == "REJECTED"
    assert receipt.classification == Failure.EXHAUSTION
    assert worker.journal.read_material(receipt) == source.retained[request.identity]
    restored = DailyRunner.restore(
        plan.spec.identity, Journal(tmp_path, clock_ns=clock), source, sleep=clock.sleep
    )
    assert restored.run() == result
    assert len(source.calls) == 1
    assert not restored.recovery_candidates(restored.coverage(), budget()).backfill


def test_absent_reconciliation_preserves_independent_proof_after_restart(tmp_path):
    worker, clock = runner(tmp_path, fault=Fault("before_source"))
    with pytest.raises(Crash):
        worker.run()
    request = worker.request(worker.plan.shards[0], 0)
    resolution = Lookup(
        request.identity, LookupStatus.ABSENT, "independent-offline-proof-42"
    )
    worker.accept_reconciliation(request, resolution)
    restored = DailyRunner.restore(
        worker.plan.spec.identity,
        Journal(tmp_path, clock_ns=clock),
        worker.source,
        sleep=clock.sleep,
    )
    before = db_bytes(restored.journal)
    assert restored.reconcile(request) == resolution
    assert db_bytes(restored.journal) == before
    assert restored.accept_reconciliation(request, resolution) == worker.report()
    assert restored.reconcile(request) == resolution
    assert not worker.source.calls


@pytest.mark.parametrize("status", [LookupStatus.FOUND, LookupStatus.ABSENT])
@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize(
    "event", ["after_reconciliation_insert", "after_reconciliation_commit"]
)
def test_reconciliation_proof_is_atomic_durable_and_replay_keeps_original_history(
    tmp_path, status, unknown, event
):
    worker, clock = runner(tmp_path, fault=Fault("after_source"))
    request = worker.request(worker.plan.shards[0], 0)
    with pytest.raises(Crash):
        worker.run()
    if unknown:
        with worker.journal.claim(worker.plan.spec.identity, 30) as claim:
            worker.journal.mark(claim, request, "UNKNOWN", Failure.TRANSPORT)
    original_events = worker.journal.events(worker.plan.spec.identity)
    original_counters = worker.report().counters
    page = (
        worker.source.retained[request.identity]
        if status == LookupStatus.FOUND
        else None
    )
    proof = Lookup(request.identity, status, "independent-offline-proof-42", page)
    worker.journal.fault = Fault(event)
    with pytest.raises(Crash):
        worker.accept_reconciliation(request, proof)
    restored = DailyRunner.restore(
        worker.plan.spec.identity,
        Journal(tmp_path, clock_ns=clock),
        worker.source,
        sleep=clock.sleep,
    )
    committed = event == "after_reconciliation_commit"
    observation = restored.journal.reconciliation(request)
    assert (observation is not None) == committed
    attempt = restored.journal.attempt(request)
    assert attempt["state"] == (
        ("SUCCEEDED" if status == LookupStatus.FOUND else "ABSENT")
        if committed
        else "STARTED"
    )
    assert (restored.journal.receipt(request) is not None) == (
        committed and status == LookupStatus.FOUND
    )
    if not committed:
        assert not restored.recovery_candidates(restored.coverage(), budget()).backfill
        assert (
            restored.journal.job(worker.plan.spec.identity)["counters"].get("pages", 0)
            == 0
        )
    restored.accept_reconciliation(request, proof)
    observation = restored.journal.reconciliation(request)
    assert observation == {
        "attempt": request.attempt_identity,
        "job": worker.plan.spec.identity,
        "shard": request.shard.identity,
        "request_id": request.identity,
        "status": status.value,
        "evidence": proof.evidence,
        "response_identity": restored.journal.attempt(request)["response_identity"],
        "at_ns": clock(),
    }
    events = restored.journal.events(worker.plan.spec.identity)
    assert events[: len(original_events)] == original_events
    assert sum(e["event"] == "RECONCILIATION_ACCEPTED" for e in events) == 1
    before = db_bytes(restored.journal)
    assert restored.reconcile(request) == proof
    assert db_bytes(restored.journal) == before
    restored.accept_reconciliation(request, proof)
    assert restored.journal.reconciliation(request) == observation
    assert restored.journal.events(worker.plan.spec.identity) == events
    assert restored.run() == restored.report()
    assert len(worker.source.calls) == 1
    assert not worker.source.lookups
    assert (
        dict(restored.report().counters)["requests"]
        == dict(original_counters)["requests"]
    )
    assert bool(
        restored.recovery_candidates(restored.coverage(), budget()).backfill
    ) == (status == LookupStatus.ABSENT)
    with pytest.raises(DailyError, match="IDENTITY_MISMATCH"):
        restored.accept_reconciliation(
            request, replace(proof, evidence="different-proof")
        )
    assert restored.journal.reconciliation(request) == observation
    assert restored.journal.events(worker.plan.spec.identity) == events


@pytest.mark.parametrize("evidence", [True, None, "", " \t", "x" * 4097, "证" * 1366])
def test_reconciliation_evidence_is_nonblank_string_bounded_in_utf8(evidence):
    with pytest.raises(ContractError):
        Lookup("invented-request", LookupStatus.ABSENT, evidence)


def test_reconciliation_evidence_exact_byte_limit_is_preserved(tmp_path):
    worker, _ = runner(tmp_path, fault=Fault("before_source"))
    with pytest.raises(Crash):
        worker.run()
    request = worker.request(worker.plan.shards[0], 0)
    proof = Lookup(
        request.identity,
        LookupStatus.ABSENT,
        "证" * (MAX_LOOKUP_EVIDENCE_BYTES // 3) + "x",
    )
    assert len(proof.evidence.encode()) == MAX_LOOKUP_EVIDENCE_BYTES
    worker.accept_reconciliation(request, proof)
    assert worker.reconcile(request) == proof


def test_found_rejected_budget_page_keeps_proof_without_retaining_oversized_material(
    tmp_path,
):
    plan = planned()
    source = Source(plan, transform=lambda r, p: replace(p, raw=b"x" * 4097))
    worker, clock = runner(tmp_path, plan, source, fault=Fault("after_source"))
    with pytest.raises(Crash):
        worker.run()
    request = worker.request(plan.shards[0], 0)
    proof = worker.reconcile(request)
    assert worker.accept_reconciliation(request, proof).state == "BLOCKED"
    assert worker.journal.receipt(request) is None
    observation = worker.journal.reconciliation(request)
    assert observation["status"] == LookupStatus.FOUND
    assert observation["evidence"] == proof.evidence
    restored = DailyRunner.restore(
        plan.spec.identity, Journal(tmp_path, clock_ns=clock), source, sleep=clock.sleep
    )
    assert restored.reconcile(request).status == LookupStatus.UNKNOWN
    assert restored.reconcile(request).evidence == proof.evidence
    assert restored.accept_reconciliation(request, proof) == worker.report()
    assert restored.journal.reconciliation(request) == observation
    assert len(source.calls) == 1


def _crash_reconciliation_process(directory, status, event):
    worker, _ = runner(directory)
    request = worker.request(worker.plan.shards[0], 0)
    proof = Lookup(
        request.identity,
        status,
        "process-independent-proof",
        worker.source.page(request) if status == LookupStatus.FOUND else None,
    )
    worker.journal.fault = lambda point: os._exit(78) if point == event else None
    worker.accept_reconciliation(request, proof)


@pytest.mark.parametrize("status", [LookupStatus.FOUND, LookupStatus.ABSENT])
@pytest.mark.parametrize(
    "event", ["after_reconciliation_insert", "after_reconciliation_commit"]
)
def test_process_exit_preserves_atomic_reconciliation_and_releases_claim(
    tmp_path, status, event
):
    worker, _ = runner(tmp_path, fault=Fault("before_source"))
    with pytest.raises(Crash):
        worker.run()
    process = multiprocessing.get_context("fork").Process(
        target=_crash_reconciliation_process, args=(tmp_path, status, event)
    )
    process.start()
    process.join(10)
    try:
        assert process.exitcode == 78
        restored, _ = runner(tmp_path)
        request = restored.request(restored.plan.shards[0], 0)
        proof = Lookup(
            request.identity,
            status,
            "process-independent-proof",
            restored.source.page(request) if status == LookupStatus.FOUND else None,
        )
        committed = event == "after_reconciliation_commit"
        assert (restored.journal.reconciliation(request) is not None) == committed
        assert restored.journal.attempt(request)["state"] == (
            ("SUCCEEDED" if status == LookupStatus.FOUND else "ABSENT")
            if committed
            else "STARTED"
        )
        assert (restored.journal.receipt(request) is not None) == (
            committed and status == LookupStatus.FOUND
        )
        restored.accept_reconciliation(request, proof)
        assert restored.reconcile(request) == proof
        assert restored.run().state == (
            "COMPLETE" if status == LookupStatus.FOUND else "BLOCKED"
        )
        assert not restored.source.calls
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
