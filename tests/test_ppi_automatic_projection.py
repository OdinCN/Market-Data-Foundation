"""Automatic results expose only persisted coverage evidence for PPI."""

import pytest

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.web.cpi_timeseries_policy import automatic_executions


def history_stage(**fields):
    return {
        "state": "complete",
        "last_attempt_at": "2026-10-29T12:00:00+00:00",
        "completed_at": "2026-10-29T12:01:00+00:00",
        "attempts": 2,
        "actual_http": 3,
        "update_start": "2006-10",
        "update_end": "2026-09",
        **fields,
    }


def project(stage, *, profile=PPI_PROFILE):
    return automatic_executions(
        {"targets": [{"reference_month": "2026-09", "history": stage}]},
        profile=profile,
    )[0]


def test_ppi_history_projects_persisted_source_gap():
    missing = [{"series_id": "WPSFD4", "month": "2006-10"}]
    record = project(history_stage(coverage_complete=False, source_missing=missing))
    assert record["state"] == "complete"
    assert record["stage"] == "history"
    assert record["reference_month"] == "2026-09"
    assert record["result"] == {
        "summary_scope": "latest_stage_attempt",
        "scheduler_attempts_total": 2,
        "actual_http_total": 3,
        "coverage_complete": False,
        "source_missing": missing,
        "completion": "source_coverage_partial",
    }


@pytest.mark.parametrize("fields", [{}, {"coverage_complete": None}])
def test_ppi_unknown_coverage_stays_unknown(fields):
    assert project(history_stage(**fields))["result"] == {
        "summary_scope": "latest_stage_attempt",
        "scheduler_attempts_total": 2,
        "actual_http_total": 3,
    }


def test_ppi_projects_each_coverage_field_independently():
    result = project(history_stage(source_missing=[]))["result"]
    assert result["source_missing"] == []
    assert "coverage_complete" not in result
    assert "completion" not in result
    result = project(history_stage(coverage_complete=True))["result"]
    assert result["coverage_complete"] is True
    assert "source_missing" not in result
    assert "completion" not in result


def test_cpi_result_dictionary_is_unchanged():
    record = project(
        history_stage(
            coverage_complete=False,
            source_missing=[{"series_id": "CUSR0000SA0", "month": "2006-10"}],
        ),
        profile=CPI_PROFILE,
    )
    assert record["result"] == {
        "summary_scope": "latest_stage_attempt",
        "scheduler_attempts_total": 2,
        "actual_http_total": 3,
    }


def test_ppi_history_evidence_and_failure_stay_with_their_month():
    missing = [{"series_id": "WPSFD4", "month": "2006-10"}]
    records = automatic_executions(
        {
            "targets": [
                {
                    "reference_month": "2026-08",
                    "history": history_stage(
                        state="failed",
                        classification="provider_failure",
                        completed_at="2026-10-28T12:00:00+00:00",
                    ),
                },
                {
                    "reference_month": "2026-09",
                    "history": history_stage(
                        coverage_complete=False, source_missing=missing
                    ),
                },
                {
                    "reference_month": "2026-07",
                    "history": history_stage(satisfied_by_monthly_history=True),
                },
            ]
        },
        profile=PPI_PROFILE,
    )
    assert [record["reference_month"] for record in records] == ["2026-08", "2026-09"]
    assert records[0]["state"] == "failed"
    assert records[0]["classification"] == "provider_failure"
    assert records[0]["finished_at"] is None
    assert "coverage_complete" not in records[0]["result"]
    assert "source_missing" not in records[0]["result"]
    assert records[1]["result"]["source_missing"] == missing
