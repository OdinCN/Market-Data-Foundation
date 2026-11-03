"""Public synthetic responses serialized by the actual Claims HTTP router."""

import copy
from datetime import timedelta
from decimal import Decimal

from market_data_foundation.historical.claims_contract import (
    SERIES_IDS,
    ClaimsError,
    weeks,
)
from market_data_foundation.historical.claims_release import (
    ClaimsReleaseRecords,
    parse_release_pages,
)
from market_data_foundation.historical.claims_timeseries import (
    ClaimsObservation,
    ClaimsTimeSeriesService,
)
from tests.test_claims_release import URL, pages
from tests.test_claims_schedule import html
from tests.test_claims_web import AT, PREFIX, client, setup


def responses(tmp_path, monkeypatch):
    app, host, store, clock, calls = setup(
        tmp_path, monkeypatch, calendar_fetcher=html, fallback_fetcher=html
    )
    captured = AT - timedelta(days=1)
    for sid in SERIES_IDS:
        for i, week in enumerate(weeks("2026-09-01", "2026-10-31")):
            if sid in {"CCSA", "CCNSA", "CC4WSA"} and week >= "2026-10-24":
                continue
            store.rows.append(
                ClaimsObservation(
                    sid,
                    week,
                    None
                    if sid == "ICNSA" and week == "2026-10-17"
                    else Decimal("0")
                    if sid == "ICSA" and week == "2026-10-10"
                    else Decimal("200000.25") + i,
                    captured,
                    captured,
                    1,
                    batch_id="synthetic-public-fixture",
                )
            )
    event = dict(
        parse_release_pages(pages(), URL),
        evidence_id="synthetic-public-evidence",
        raw_sha256="0" * 64,
        raw_locator="synthetic-public-fixture.txt",
        obtained_at=captured.isoformat(),
        available_at=captured.isoformat(),
    )
    previous_event = copy.deepcopy(event)
    previous_event.update(
        evidence_id="synthetic-public-prior-evidence",
        available_at=(captured - timedelta(minutes=1)).isoformat(),
        obtained_at=(captured - timedelta(minutes=1)).isoformat(),
        parser_revision="synthetic-prior-parser",
        source_url="https://www.dol.gov/ui/data.pdf?os=v0",
        evidence_kind="official_current_report_snapshot",
        raw_sha256="1" * 64,
        raw_locator="synthetic-public-prior-fixture.txt",
        source_version=dict(
            value="2026-10-28", precision="date", basis="synthetic_source_version"
        ),
    )
    for claim in previous_event["claims"]:
        claim["evidence"]["source_url"] = previous_event["source_url"]
    # Earlier proof alone supports one original prior; the latest PDF does not.
    prior = next(
        c
        for c in previous_event["claims"]
        if c["series_id"] == "ICNSA"
        and c["role"] == "official_previously_reported_prior"
    )
    prior.update(
        value="95",
        confirmation_state="confirmed",
        value_identity="synthetic-prior-value",
    )
    prior["evidence"].update(
        page=2, fragment="Synthetic earlier proof: original prior 95"
    )
    event["claims"] = [
        c
        for c in event["claims"]
        if not (
            c["series_id"] == "ICNSA"
            and c["role"] == "official_previously_reported_prior"
        )
    ]
    monkeypatch.setattr(
        ClaimsReleaseRecords,
        "_rows",
        lambda self, cutoff: [
            copy.deepcopy(e)
            for e in (event, previous_event)
            if e["available_at"] <= cutoff.isoformat()
        ],
    )
    host.scheduler.refresh_calendar()
    # A genuine compact terminal journal receipt, all inputs are synthetic.
    host.submit(
        dict(
            request_id="c1400000-0000-4000-8000-000000000001",
            action="init",
            start="2026-09-01",
            end="2026-09-26",
        )
    )
    paths = {
        "status": "/status",
        "success": (
            "/query?start=2026-10-03&end=2026-10-03"
            "&local_only=true&as_of_basis=available"
        ),
        "query": (
            "/query?start=2026-10-10&end=2026-10-24"
            "&local_only=true&as_of_basis=available"
        ),
        "history": (
            "/query?start=2026-10-10&end=2026-10-24&local_only=true"
            "&query_mode=history&as_of_basis=available&as_of=2026-10-31T18%3A00%3A00Z"
        ),
        "releases": (
            "/releases?event_id=dol-claims-2026-10-29&reference_week=2026-10-24"
        ),
        "source_history": (
            "/releases?reference_week=2026-10-17&read_mode=source_history"
        ),
        "source_version_filtered": (
            "/releases?event_id=dol-claims-2026-10-29&read_mode=source_history"
            "&source_version=2026-10-28&reference_week=2026-10-17"
        ),
        "available_before_latest": "/releases?event_id=dol-claims-2026-10-29&as_of="
        + previous_event["available_at"].replace("+", "%2B"),
        "available_before_all": "/releases?as_of="
        + (captured - timedelta(minutes=2)).isoformat().replace("+", "%2B"),
        **{
            name: "/" + name
            for name in ("jobs", "executions", "strategy", "schedule", "api-help")
        },
    }
    before = len(calls)
    result = {}
    with client(app) as http:
        for name, path in paths.items():
            response = http.get(PREFIX + path)
            assert response.status_code == 200, response.text
            result[name] = response.json()
        directory = http.get("/api/r5/sources").json()
        result["directory_entry"] = next(
            e
            for e in directory["entries"]
            if (e.get("asset") or {}).get("identity") == "mdf.macro.dol.claims"
        )
    assert len(calls) == before
    conflicting = copy.deepcopy(event)
    conflicting.update(
        evidence_id="synthetic-conflicting-evidence", raw_sha256="2" * 64
    )
    conflicting["claims"][0].update(
        value="101", value_identity="synthetic-conflicting-value"
    )
    conflicting["claims"][0]["evidence"]["fragment"] = (
        "Synthetic conflicting actual 101"
    )
    monkeypatch.setattr(
        ClaimsReleaseRecords,
        "_rows",
        lambda self, cutoff: [
            copy.deepcopy(e)
            for e in (event, conflicting)
            if e["available_at"] <= cutoff.isoformat()
        ],
    )
    with client(app) as http:
        response = http.get(PREFIX + "/releases")
        assert response.status_code == 200
        result["release_conflict"] = response.json()
    assert len(calls) == before
    from tests.test_claims_jobs import service_for, submit

    gap_job = submit(service_for([]), tmp_path / "gap-job", "2006-11-02", "2026-11-02")
    gap_job.update(
        job_id="c1400000-0000-4000-8000-000000000002",
        request_id="c1400000-0000-4000-8000-000000000002",
    )
    host.journal.save(gap_job)
    with client(app) as http:
        response = http.get(PREFIX + "/executions")
        assert response.status_code == 200
        result["gap_executions"] = response.json()

    class FailedSource:
        request_count, attempts = 0, 0

        def fetch(self, *args):
            self.request_count += 1
            self.attempts += 1
            raise ClaimsError("claims_fred_http_503")

    host.query_factory = lambda **kwargs: ClaimsTimeSeriesService(
        store, FailedSource(), clock=lambda: clock[0]
    )
    with client(app) as http:
        response = http.get(
            PREFIX
            + (
                "/query?start=2026-10-10&end=2026-10-24"
                "&local_only=false&as_of_basis=available"
            )
        )
        assert response.status_code == 200
        result["source_failure"] = response.json()

    # No private configuration or ephemeral path is included in public fixtures.
    result["status"] = {
        k: result["status"][k]
        for k in (
            "latest_weeks",
            "observation_count",
            "revision_count",
            "batch_count",
            "web_capabilities",
            "initialization",
            "collection_scope",
            "source_allowed",
        )
    }

    def stable(value):
        if isinstance(value, dict):
            return {
                k: (
                    v.replace(str(tmp_path), "synthetic-public-fixture")
                    if k == "raw_locator" and isinstance(v, str)
                    else stable(v)
                )
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [stable(v) for v in value]
        return value

    return stable(result)
