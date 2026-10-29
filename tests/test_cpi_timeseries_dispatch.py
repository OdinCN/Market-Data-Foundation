"""Real thread/lock dispatch checks; no network, database or timing sleeps."""

from threading import Event, current_thread
from uuid import uuid4

import pytest

from market_data_foundation.web.cpi_timeseries_dispatch import dispatch_job
from market_data_foundation.web.cpi_timeseries_jobs import JobJournal, WebError


def command():
    return {
        "request_id": str(uuid4()),
        "action": "init",
        "start": "1999-01",
        "end": "2026-09",
    }


class BlockingSubmission:
    """Exercise the real journal lock with an explicitly blocked source step."""

    def __init__(self, directory, *, interrupt=False):
        self.journal = JobJournal(directory)
        self.started = Event()
        self.release = Event()
        self.finished = Event()
        self.interrupt = interrupt
        self.calls = 0
        self.worker_daemon = None

    def __call__(self, request, *, on_started):
        existing = self.journal.read(request["request_id"])
        if existing is not None:
            return existing
        claimed = False
        try:
            with self.journal.execution():
                existing = self.journal.read(request["request_id"])
                if existing is not None:
                    return existing
                claimed = True
                job = dict(
                    request,
                    job_id=request["request_id"],
                    created_at="2026-10-29T00:00:00+00:00",
                    state="running",
                    result={"completed_batches": []},
                )
                self.journal.save(job)
                on_started(job)
                self.calls += 1
                self.worker_daemon = current_thread().daemon
                self.started.set()
                if not self.release.wait(5):
                    raise AssertionError("test_source_was_not_released")
                if self.interrupt:
                    raise SystemExit("synthetic_interruption")
                job["result"]["completed_batches"].append("1998-2017")
                job["state"] = "complete"
                self.journal.save(job)
                return job
        finally:
            if claimed:
                self.finished.set()


@pytest.fixture
def submission(tmp_path):
    submit = BlockingSubmission(tmp_path / "jobs")
    yield submit
    submit.release.set()
    if submit.started.is_set():
        assert submit.finished.wait(5)


def test_running_acknowledgment_precedes_blocked_source_and_is_a_snapshot(submission):
    request = command()
    result = dispatch_job(submission, request)
    assert result["state"] == "running"
    assert submission.started.wait(5)
    assert not submission.finished.is_set()
    assert submission.worker_daemon is True
    assert submission.journal.list()[0]["state"] == "running"

    submission.release.set()
    assert submission.finished.wait(5)
    saved = submission.journal.read(request["request_id"])
    assert saved["state"] == "complete"
    assert saved["result"]["completed_batches"] == ["1998-2017"]
    assert result["state"] == "running"
    assert result["result"]["completed_batches"] == []


def test_busy_submission_is_reported_without_creating_a_second_job(submission):
    first, second = command(), command()
    dispatch_job(submission, first)
    assert submission.started.wait(5)
    with pytest.raises(WebError, match="cpi_jobs_busy"):
        dispatch_job(submission, second)
    assert submission.calls == 1
    assert submission.journal.read(second["request_id"]) is None
    assert len(submission.journal.list()) == 1


def test_running_replay_returns_without_waiting_or_starting_another_source(submission):
    request = command()
    original = dispatch_job(submission, request)
    assert submission.started.wait(5)
    assert dispatch_job(submission, request) == original
    assert submission.calls == 1
    assert not submission.finished.is_set()


def test_complete_replay_needs_no_started_callback(submission):
    request = command()
    dispatch_job(submission, request)
    assert submission.started.wait(5)
    submission.release.set()
    assert submission.finished.wait(5)
    assert dispatch_job(submission, request)["state"] == "complete"
    assert submission.calls == 1


@pytest.mark.parametrize("error", [WebError("not_admitted", 403), SystemExit(3)])
def test_pre_admission_failure_is_propagated_without_waiting(error):
    def reject(_request, *, on_started):
        raise error

    with pytest.raises(type(error)) as caught:
        dispatch_job(reject, command())
    assert caught.value is error


def test_worker_interruption_preserves_running_record_for_journal_recovery(tmp_path):
    submit = BlockingSubmission(tmp_path / "jobs", interrupt=True)
    request = command()
    result = dispatch_job(submit, request)
    assert submit.started.wait(5)
    submit.release.set()
    assert submit.finished.wait(5)
    # The completion event is set only after the worker releases its lock.
    with submit.journal.execution():
        record = submit.journal.read(request["request_id"])
        assert record["state"] == "unknown"
        assert record["classification"] == "execution_interrupted"
    assert result["state"] == "running"


def test_command_and_terminal_result_are_independent_snapshots():
    request = command()
    terminal = {"state": "complete", "result": {"saved_points": 0}}

    def replay(pending, *, on_started):
        pending["action"] = "update"
        return terminal

    result = dispatch_job(replay, request)
    terminal["result"]["saved_points"] = 99
    assert result["result"]["saved_points"] == 0
    assert request["action"] == "init"
