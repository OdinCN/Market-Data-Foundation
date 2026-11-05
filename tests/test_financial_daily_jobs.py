"""Manual daily job, credit identity and local-only API regressions (no network)."""

import json
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.historical.financial_daily import FinancialError, digest
from market_data_foundation.historical.financial_daily_credit import (
    ARCHIVE_COMMIT,
    ARCHIVE_END,
    CREDIT,
    archive_points,
    credit_binding,
)
from market_data_foundation.historical.financial_daily_source import parse_metadata
from market_data_foundation.web import financial_daily_jobs as jobs_module
from market_data_foundation.web.financial_daily_api import (
    PREFIX,
    FinancialOptions,
    financial_router,
)
from market_data_foundation.web.financial_daily_jobs import (
    FinancialJobs,
    defaults,
    identifier,
    plan,
    validate,
)
from tests.test_financial_daily import END, START, payload


def command(**changes):
    return dict(request_id=str(uuid4()), action="init", start=START, end=END, **changes)


def test_default_is_twenty_calendar_years_and_clamps_leap_day():
    assert defaults(date(2026, 11, 5)) == dict(start="2006-11-05", end="2026-11-05")
    assert defaults(date(2024, 2, 29))["start"] == "2004-02-29"


def test_twenty_year_plan_and_incremental_sources_are_explicit():
    c = command()
    c.update(defaults(date(2026, 11, 5)))
    steps = plan(c)
    assert len(steps) < 160
    assert {s["series_id"] for s in steps} == set(jobs_module.SERIES_IDS)
    for sid in CREDIT:
        credit = [s for s in steps if s["series_id"] == sid]
        assert [s["source"] for s in credit] == ["archive", "fred"]
        assert credit[0]["end"] == ARCHIVE_END
        assert credit[1]["start"] == "2026-03-18"
    assert min(s["start"] for s in steps if s["series_id"] == "SOFR") == "2018-04-02"
    assert all(s["source"] == "derived" for s in steps[-21:])
    c["action"] = "update"
    assert all(s["source"] == "fred" for s in plan(c) if s["series_id"] in CREDIT)


@pytest.mark.parametrize("value", ["../escape", "X", str(uuid4()).upper(), None])
def test_identifier_rejects_paths_and_noncanonical_ids(value):
    with pytest.raises(ValueError):
        identifier(value)


def test_credit_csv_keeps_month_end_weekend_and_nulls_and_pin():
    sid = CREDIT[0]
    raw = (
        f"observation_date,{sid}\n1996-12-31,0.60\n2026-01-31,0.75\n"
        "2026-02-01,\n2026-03-17,0.92\n"
    ).encode()
    parsed = archive_points(raw, sid, "2026-01-31", "2026-02-01")
    assert str(parsed[0].value) == "0.75" and parsed[1].value is None
    assert parsed[0].provider_extra["archive_commit"] == ARCHIVE_COMMIT
    assert not parsed[0].provider_extra["historical_initial_value_proven"]
    with pytest.raises(FinancialError):
        archive_points(raw.replace(b"2026-03-17", b"2026-03-16"), sid, START, END)
    assert credit_binding(sid, origin="credit_archive")["distributor"] != "FRED"
    assert credit_binding(sid, origin="fred")["distributor"] == "FRED"


@pytest.mark.parametrize(
    "notes,expected", [("COPYRIGHT ICE", True), ("Public", False), (None, None)]
)
def test_copyright_metadata_is_nullable_case_insensitive(notes, expected):
    batch = payload(CREDIT[0])
    document = json.loads(batch.metadata_raw)
    if notes is None:
        document["seriess"][0].pop("notes", None)
    else:
        document["seriess"][0]["notes"] = notes
    assert (
        parse_metadata(json.dumps(document).encode(), CREDIT[0])["is_copyrighted"]
        is expected
    )


class Raw:
    def __init__(self):
        self.items = {}

    def put_verified(self, value):
        self.items[digest(value)] = value
        return digest(value)

    def read_verified(self, sha, length):
        value = self.items[sha]
        assert len(value) == length
        return value


def test_same_job_resume_uses_retained_bytes_without_source_or_first_time_reset(
    tmp_path, monkeypatch
):
    original = payload("DGS2")
    calls = []
    saved = []
    raw = Raw()
    state = {"fail": True}

    class Source:
        request_count = 0

        def fetch(self, *_args, **_kwargs):
            calls.append("fetch")
            self.request_count += 1
            return original

    def save(batch):
        if state["fail"]:
            raise FinancialError("financial_storage_outcome_unknown")
        saved.append(batch)
        return dict(batch_id=batch.batch_id, saved_points=3, saved_revisions=0)

    store = SimpleNamespace(
        raw_store=raw,
        save=save,
        record_attempt=lambda *a, **k: None,
        close=lambda: None,
    )
    source = Source()
    service = SimpleNamespace(store=store, source=source)
    monkeypatch.setattr(
        jobs_module, "coverage", lambda *a, **k: dict(series=[], provider_requests=0)
    )
    monkeypatch.setattr(
        jobs_module,
        "plan",
        lambda c: [
            dict(
                series_id="DGS2", start=START, end=END, source="origin", state="pending"
            )
        ],
    )
    host = FinancialJobs(tmp_path, service_factory=lambda **k: service)
    c = command()
    first = host.submit(c)
    assert first["state"] == "partial" and first["steps"][0]["retained"]
    state["fail"] = False
    completed = host.resume(c["request_id"])
    assert completed["state"] == "complete" and completed["resume_count"] == 1
    assert calls == ["fetch"] and saved[0].observed_at == original.observed_at
    assert saved[0].batch_id == original.batch_id
    assert host.submit(c) == completed
    assert host.resume(c["request_id"]) == completed
    assert len(calls) == 1


def test_journal_get_only_reads_and_unknown_interrupted_jobs_are_not_auto_resumed(
    tmp_path,
):
    directory = tmp_path / "not-created"
    host = FinancialJobs(
        directory, service_factory=lambda **k: pytest.fail("read constructed source")
    )
    assert host.journal.list() == [] and not directory.exists()


def test_api_defaults_denied_and_read_does_not_construct_live_service(tmp_path):
    app = FastAPI()
    app.include_router(financial_router(None))
    with TestClient(app) as client:
        assert client.get(PREFIX + "/status").status_code == 403
    app = FastAPI()

    def factory(**k):
        pytest.fail("GET status/jobs/help constructed source")

    app.include_router(
        financial_router(
            FinancialOptions(tmp_path, True, True), service_factory=factory
        )
    )
    with TestClient(app, base_url="http://odin.pub:18761") as client:
        assert client.get(PREFIX + "/status").json()["resident_worker"] is False
        assert client.get(PREFIX + "/jobs").json()["jobs"] == []
        assert client.get(PREFIX + "/api-help").status_code == 200
        for endpoint in ("/jobs", "/jobs/resume"):
            response = client.post(
                PREFIX + endpoint,
                json=command(),
                headers={"Origin": "http://odin.pub:18761"},
            )
            assert response.status_code == 403
    assert not (tmp_path / "execution.lock").exists()


def test_validation_rejects_extra_keys_and_future_range():
    c = command()
    validate(c)
    with pytest.raises(ValueError):
        validate(dict(c, unit="arbitrary"))
    c["end"] = "2099-01-01"
    with pytest.raises(ValueError):
        validate(c)


def test_financial_host_composition_keeps_public_write_denial(tmp_path, monkeypatch):
    from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
    from tests.test_cpi_timeseries_web_host import client, setup

    app, *_ = setup(
        tmp_path,
        monkeypatch,
        financial_options=FinancialOptions(tmp_path / "daily", True, True),
    )
    with client(app) as http:
        assert http.get(PREFIX + "/status").status_code == 200
        response = http.post(
            PREFIX + "/jobs",
            json={"request_id": "bad"},
            headers={"Origin": CANDIDATE_ORIGIN},
        )
        # The normal private gate admits the route; command validation rejects it.
        assert response.status_code == 400
        assert response.json()["classification"] == "financial_invalid_job_request"
        response = http.post(
            PREFIX + "/jobs",
            json=command(),
            headers={"Host": "odin.pub:18761", "Origin": "http://odin.pub:18761"},
        )
        assert response.status_code in {403, 405}
    assert not (tmp_path / "daily" / "execution.lock").exists()


def test_partial_job_continues_other_series_and_resumes_only_failed_step(
    tmp_path, monkeypatch
):
    requested = []
    unavailable = {"DGS2"}

    class Source:
        request_count = 0

        def fetch(self, sid, *_args, **_kwargs):
            requested.append(sid)
            self.request_count += 1
            if sid in unavailable:
                raise FinancialError("financial_http_failure", http_status=503)
            return payload(sid)

    store = SimpleNamespace(
        raw_store=Raw(),
        close=lambda: None,
        record_attempt=lambda *a, **k: None,
        save=lambda b: dict(batch_id=b.batch_id, saved_points=3),
    )
    service = SimpleNamespace(store=store, source=Source())
    monkeypatch.setattr(jobs_module, "coverage", lambda *a, **k: dict(series=[]))
    monkeypatch.setattr(
        jobs_module,
        "plan",
        lambda c: [
            dict(series_id=sid, start=START, end=END, source="origin", state="pending")
            for sid in ("DGS2", "DGS10")
        ],
    )
    host = FinancialJobs(tmp_path, service_factory=lambda **k: service)
    c = command()
    partial = host.submit(c)
    assert partial["state"] == "partial"
    assert [s["state"] for s in partial["steps"]] == ["failed", "complete"]
    unavailable.clear()
    complete = host.resume(c["request_id"])
    assert complete["state"] == "complete"
    assert requested == ["DGS2", "DGS10", "DGS2"]


def test_default_window_uses_new_york_observation_day_across_beijing_and_utc_midnights():
    from datetime import UTC, datetime

    for at in [
        datetime(2026, 11, 5, 17, tzinfo=UTC),
        datetime(2026, 11, 6, 1, tzinfo=UTC),
    ]:
        assert defaults(at=at) == {"start": "2006-11-05", "end": "2026-11-05"}
