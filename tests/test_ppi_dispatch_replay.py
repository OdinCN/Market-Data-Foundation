"""Direct journal and dispatch regressions without an HTTP client."""

import pytest

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.web.cpi_timeseries_dispatch import dispatch_job
from market_data_foundation.web.cpi_timeseries_jobs import JobJournal, WebError
from tests.test_cpi_timeseries_dispatch import BlockingSubmission, command


def test_ppi_running_callback_and_replay_are_identical(tmp_path):
    submit = BlockingSubmission(tmp_path / "jobs")
    submit.journal = JobJournal(tmp_path / "jobs", profile=PPI_PROFILE)
    request = command()
    try:
        original = dispatch_job(submit, request)
        assert submit.started.wait(5)
        assert original["state"] == "running"
        assert original["asset"] == "ppi"
        assert submit.journal.read(request["request_id"]) == original
        assert dispatch_job(submit, request) == original
        assert submit.calls == 1
        assert not submit.finished.is_set()
    finally:
        submit.release.set()
        if submit.started.is_set():
            assert submit.finished.wait(5)


@pytest.mark.parametrize(
    "profile,other", [(CPI_PROFILE, PPI_PROFILE), (PPI_PROFILE, CPI_PROFILE)]
)
def test_save_normalizes_caller_and_rejects_cross_asset(tmp_path, profile, other):
    journal = JobJournal(tmp_path / "jobs", profile=profile)
    record = dict(command(), state="complete")
    with journal.execution():
        journal.save(record)
    assert record["asset"] == profile.key
    assert journal.read(record["request_id"]) == record
    wrong = JobJournal(journal.directory, profile=other)
    with pytest.raises(WebError, match=other.key + "_job_journal_asset_mismatch"):
        wrong.read(record["request_id"])
    with pytest.raises(WebError, match=other.key + "_job_journal_asset_mismatch"):
        wrong.save(record)
    assert record["asset"] == profile.key
    assert journal.read(record["request_id"]) == record
