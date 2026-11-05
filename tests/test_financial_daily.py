"""Deterministic daily semantics and source ports; no LAN or provider dependency."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from market_data_foundation.historical.claims_source import ClaimsResponse
from market_data_foundation.historical.financial_daily import (
    BY_ID,
    DATASET_ID,
    SERIES_IDS,
    DailyBatch,
    FinancialError,
    dates,
    day,
    non_observation_day,
    registry,
)
from market_data_foundation.historical.financial_daily_service import (
    FinancialDailyService,
    project,
)
from market_data_foundation.historical.financial_daily_source import (
    ENDPOINT,
    DailyFredSource,
    parameters,
    parse_metadata,
    parse_observations,
)
from market_data_foundation.historical.financial_daily_store import (
    PostgresDailyStore,
    schema_name,
)

AT = datetime(2026, 11, 5, 8, tzinfo=UTC)
START, END = "2026-10-30", "2026-11-03"


def payload(sid="DGS2", values=None, *, at=AT):
    values = values or {
        "2026-10-30": "2.0000",
        "2026-11-02": ".",
        "2026-11-03": "-0.125",
    }
    raw = json.dumps(
        dict(
            count=len(values),
            offset=0,
            units="lin",
            output_type=1,
            observation_start=START,
            observation_end=END,
            sort_order="asc",
            observations=[
                dict(
                    date=d,
                    value=v,
                    realtime_start="2026-11-05",
                    realtime_end="2026-11-05",
                )
                for d, v in values.items()
            ],
        )
    ).encode()
    m = BY_ID[sid]
    metadata_raw = json.dumps(
        {
            "seriess": [
                dict(
                    id=sid,
                    title="Synthetic daily fixture",
                    units="Percent",
                    frequency_short="D",
                    frequency=m.frequency,
                    seasonal_adjustment_short="NSA",
                    observation_start="2026-01-01",
                    observation_end=END,
                    last_updated="2026-11-05 00:00:00-05",
                )
            ]
        }
    ).encode()
    return DailyBatch(
        sid,
        START,
        END,
        parse_observations(raw, sid, START, END),
        raw,
        metadata_raw,
        parse_metadata(metadata_raw, sid),
        at - timedelta(seconds=1),
        at,
        dict(url=ENDPOINT, parameters=parameters(sid, START, END)),
        "controlled_fixture",
    )


class Control:
    def __init__(self):
        self.calls = []

    @contextmanager
    def attempt(self):
        yield self

    def record_response(self, status, headers):
        self.calls.append(status)
        return AT + timedelta(minutes=5)


def snapshot(batch, *, version=1):
    return dict(
        values=[
            (
                p.series_id,
                day(p.observation_date),
                version,
                p.value,
                p.provider_extra,
                batch.batch_id,
                batch.observed_at,
                batch.observed_at + timedelta(seconds=1),
                "test://raw",
                "test://metadata",
                batch.origin,
            )
            for p in batch.points
        ],
        batches=[
            (
                batch.series_id,
                batch.batch_id,
                batch.metadata,
                batch.observed_at,
                batch.observed_at + timedelta(seconds=1),
                day(START),
                day(END),
                batch.origin,
            )
        ],
        attempts=[],
        ranges=[(batch.series_id, day(START), day(END))],
        pending=[],
        cutoff=AT + timedelta(seconds=3),
        basis="available",
    )


def test_registry_native_units_calendars_and_default_source_permission():
    assert len(registry()) == 10 and len(set(SERIES_IDS)) == 10
    assert all(r["source_use"]["storage_permitted"] is False for r in registry())
    assert DATASET_ID == "mdf.macro.financial-conditions.daily"
    assert non_observation_day(BY_ID["DGS2"], "2026-10-31")
    assert not non_observation_day(BY_ID["DFEDTARL"], "2026-10-31")
    assert not non_observation_day(BY_ID["BAMLC0A0CM"], "2026-10-31")
    assert non_observation_day(BY_ID["BAMLC0A0CM"], "2026-11-01")


def test_live_permission_blocks_before_credential_and_network(tmp_path):
    def forbidden():
        raise AssertionError("credential_was_loaded")

    for sid in SERIES_IDS:
        with pytest.raises(
            FinancialError, match="fred_retention_permission_unconfirmed"
        ):
            DailyFredSource.live(
                series=(sid,),
                credential_loader=forbidden,
                request_control_path=tmp_path / "unused",
                live_opt_in=True,
            )
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("sid", SERIES_IDS)
def test_exact_daily_metadata_and_signed_values_dot_and_date_precision(sid):
    b = payload(sid)
    assert b.points[0].value == Decimal("2.0000")
    assert b.points[1].value is None and b.points[2].value == Decimal("-0.125")
    assert b.points[0].provider_extra["source_publication_at"] is None
    assert b.points[0].observation_date == "2026-10-30"
    assert b.metadata["frequency"] == BY_ID[sid].frequency


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "DGS10"),
        ("units", "Basis Points"),
        ("frequency_short", "M"),
        ("frequency", "Monthly"),
        ("seasonal_adjustment_short", "SA"),
    ],
)
def test_wrong_source_metadata_cannot_silently_change_mapping(field, value):
    body = json.loads(payload().metadata_raw)
    body["seriess"][0][field] = value
    with pytest.raises(FinancialError):
        parse_metadata(json.dumps(body).encode(), "DGS2")


@pytest.mark.parametrize(
    "change",
    [
        {"count": 99},
        {"offset": 1},
        {"output_type": 2},
        {"units": "pch"},
        {"observation_end": "2026-10-31"},
        {"sort_order": "desc"},
    ],
)
def test_incomplete_or_transformed_response_rejected(change):
    doc = json.loads(payload().raw)
    doc.update(change)
    with pytest.raises(FinancialError):
        parse_observations(json.dumps(doc).encode(), "DGS2", START, END)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e2", "", " 1", "1,000"])
def test_bad_numeric_input_not_coerced(value):
    doc = json.loads(payload().raw)
    doc["observations"][0]["value"] = value
    with pytest.raises(FinancialError):
        parse_observations(json.dumps(doc).encode(), "DGS2", START, END)


def test_projection_keeps_dot_weekend_future_policy_and_credit_meanings():
    b = payload()
    data = snapshot(b)
    result = project(data, ("DGS2",), START, "2026-11-05")
    states = {r["observation_date"]: r["state"] for r in result["rows"]}
    assert states["2026-10-31"] == "non_observation_day"
    assert states["2026-11-02"] == "provider_missing"
    assert states["2026-11-04"] == "not_collected"
    data["ranges"] = [("DGS2", day(START), day("2026-11-05"))]
    data["batches"] = [
        tuple(list(data["batches"][0][:6]) + [day("2026-11-05"), data["batches"][0][7]])
    ]
    requested_future = project(data, ("DGS2",), START, "2026-11-05")
    assert requested_future["rows"][-1]["state"] == "not_yet_available_from_provider"
    assert result["rows"][0]["value"] == "2.0000" and result["provider_requests"] == 0
    assert all(r["source_publication_at"] is None for r in result["rows"])
    b = payload("DFEDTARL")
    r = project(snapshot(b), ("DFEDTARL",), START, END)["rows"]
    assert (
        r[0]["policy_effective_date"] == START
        and r[0]["policy_announcement_at"] is None
    )
    assert r[1]["state"] == "not_returned_calendar_unconfirmed"
    b = payload("BAMLC0A0CM", {"2026-10-30": "1.234567890123456789012345678901"})
    assert (
        project(snapshot(b), (b.series_id,), START, END)["rows"][0]["basis_points"]
        == "123.456789012345678901234567890100"
    )
    assert (
        project(snapshot(b), (b.series_id,), START, END)["rows"][1]["state"]
        == "not_returned_calendar_unconfirmed"
    )


def test_unknown_attempt_and_pending_commit_never_become_zero_or_old_success():
    b = payload()
    data = snapshot(b)
    data["values"] = []
    data["attempts"] = [
        ("DGS2", "failed", "financial_http_failure", AT, day(START), day(END))
    ]
    assert {r["state"] for r in project(data, ("DGS2",), START, END)["rows"]} == {
        "request_failed"
    }
    data["values"] = snapshot(b)["values"]
    data["pending"] = [("DGS2", day(START))]
    r = project(data, ("DGS2",), START, END)["rows"][0]
    assert r["state"] == "commit_pending" and r["value"] is None
    assert r["revision"] is None and r["available_at"] is None
    assert r["last_confirmed"]["value"] == "2.0000"
    data["values"] = []
    data["batches"] = []
    data["attempts"] = []
    data["pending"] = []
    assert {r["state"] for r in project(data, ("DGS2",), START, END)["rows"]} == {
        "not_known_at_cutoff"
    }


def test_source_retries_only_transport_and_preserves_exact_request():
    batch = payload()
    calls = []
    control = Control()

    def wire(request):
        calls.append(request)
        if len(calls) == 1:
            return ClaimsResponse(503, b"{}")
        return ClaimsResponse(
            200, batch.metadata_raw if request.operation == "metadata" else batch.raw
        )

    source = DailyFredSource(
        transport=wire, request_control=control, clock=lambda: AT, sleep=lambda _: None
    )
    result = source.fetch("DGS2", START, END)
    assert result.points == batch.points and source.request_count == 3
    assert calls[-1].parameters == parameters("DGS2", START, END)
    assert "api_key" not in str(result.request)


def test_429_has_cooldown_without_automatic_repeat_and_parser_error_not_retried():
    control = Control()
    calls = []

    def wire(request):
        calls.append(request)
        return ClaimsResponse(429, b"{}")

    source = DailyFredSource(
        transport=wire, request_control=control, clock=lambda: AT, sleep=lambda _: None
    )
    with pytest.raises(FinancialError) as caught:
        source.fetch("DGS2", START, END)
    assert caught.value.retry_at == AT + timedelta(minutes=5) and len(calls) == 1
    source = DailyFredSource(
        transport=lambda _: ClaimsResponse(200, b'{"seriess":[]}'),
        request_control=control,
        clock=lambda: AT,
        sleep=lambda _: None,
    )
    with pytest.raises(FinancialError):
        source.fetch("DGS2", START, END)
    assert source.request_count == 1


def test_escaped_credential_echo_cannot_reach_material_or_observations():
    secret = "synthetic-test-key"
    raw = (
        json.dumps({"text": "".join("\\u" + format(ord(c), "04x") for c in secret)})
        .replace("\\\\u", "\\u")
        .encode()
    )
    source = DailyFredSource(
        transport=lambda _: ClaimsResponse(200, raw),
        request_control=Control(),
        secret=secret,
    )
    with pytest.raises(FinancialError, match="credential_echo_rejected"):
        source.fetch("DGS2", START, END)


def test_fixture_rows_rejected_before_product_raw_or_sql_effects():
    db = SimpleNamespace(autocommit=True)
    store = PostgresDailyStore(db, None)
    with pytest.raises(FinancialError, match="fixture_cannot_enter_product_dataset"):
        store._safe_batch(payload())
    assert schema_name("test_financial_daily_s1") == "test_financial_daily_s1"
    for name in [
        "public",
        "other_product",
        "test_financial_daily_x;DROP SCHEMA public",
    ]:
        with pytest.raises(FinancialError):
            schema_name(name)


def test_per_series_partial_success_and_failed_only_retry_do_not_erase_success():
    saved = []
    attempts = []

    class Store:
        def save(self, b):
            saved.append(b.series_id)
            return {"batch_id": b.batch_id}

        def record_attempt(self, *args, **kwargs):
            attempts.append(args)

    def fetch(sid, start, end):
        if sid == "SOFR":
            raise FinancialError("financial_http_failure", http_status=503)
        return payload(sid)

    source = SimpleNamespace(fetch=fetch, request_count=0)
    service = FinancialDailyService(Store(), source)
    service.query = lambda *args, **kwargs: {"provider_requests": 0}
    result = service.collect(START, END, selected=("EFFR", "SOFR"))
    assert result["state"] == "partial" and saved == ["EFFR"]
    assert result["series"]["SOFR"]["http_status"] == 503
    source.fetch = lambda sid, start, end: payload(sid)
    service.collect(START, END, selected=("SOFR",))
    assert saved == ["EFFR", "SOFR"] and [a[3] for a in attempts] == [
        "complete",
        "failed",
        "complete",
    ]


def test_dates_not_monthly_and_leap_day_is_preserved():
    assert dates("2024-02-28", "2024-03-01") == (
        "2024-02-28",
        "2024-02-29",
        "2024-03-01",
    )
    with pytest.raises(FinancialError):
        dates("2024-02", "2024-03")


def test_raw_json_retained_before_semantic_failure_without_repeating_source():
    retained = []
    source = DailyFredSource(
        transport=lambda _: ClaimsResponse(200, b'{"seriess":[]}'),
        request_control=Control(),
        raw_sink=lambda request, response, at, attempts: retained.append(response.body),
    )
    with pytest.raises(FinancialError, match="metadata_invalid"):
        source.fetch("DGS2", START, END)
    assert retained == [b'{"seriess":[]}'] and source.request_count == 1


def test_future_capture_and_mutated_batch_rejected_before_material_write():
    from dataclasses import replace

    from market_data_foundation.historical.financial_daily_source import validate_batch

    batch = payload()
    with pytest.raises(FinancialError):
        validate_batch(replace(batch, request={"url": "https://other.invalid"}))
    store = PostgresDailyStore(
        SimpleNamespace(autocommit=True),
        None,
        schema="test_financial_daily_unit",
        clock=lambda: AT - timedelta(days=1),
    )
    with pytest.raises(FinancialError, match="future_capture_rejected"):
        store._safe_batch(batch)


def test_zero_is_a_real_observation_and_not_missing():
    b = payload("DFII10", {"2026-10-30": "0.0000"})
    row = project(snapshot(b), ("DFII10",), START, END)["rows"][0]
    assert row["state"] == "observed" and row["value"] == "0.0000"


def test_private_storage_probe_requires_explicit_opt_in_before_binding():
    from tests.test_financial_daily_dev import storage_probe

    with pytest.raises(FinancialError, match="storage_opt_in_required"):
        storage_probe()


def test_one_permission_block_does_not_prevent_other_approved_series(
    monkeypatch, tmp_path
):
    from market_data_foundation.historical import financial_daily as contract

    monkeypatch.setattr(
        contract, "source_policy", lambda sid: {"storage_permitted": sid == "EFFR"}
    )
    assert contract.permitted_series(("EFFR", "SOFR")) == ("EFFR",)
    loaded = []
    source = DailyFredSource.live(
        series=("EFFR", "SOFR"),
        credential_loader=lambda: loaded.append(True) or "synthetic-key",
        request_control_path=tmp_path / "control",
        live_opt_in=True,
    )
    assert loaded == [True]
    with pytest.raises(FinancialError, match="permission_unconfirmed"):
        source.fetch("SOFR", START, END)
    assert source.request_count == 0 and not (tmp_path / "control").exists()


def test_unrequested_dates_and_narrow_failed_attempt_are_not_calendar_gaps():
    batch = payload()
    data = snapshot(batch)
    data["attempts"] = [
        (
            "DGS2",
            "failed",
            "financial_http_failure",
            AT,
            day("2026-11-03"),
            day("2026-11-03"),
        )
    ]
    rows = project(data, ("DGS2",), "2026-10-29", END)["rows"]
    assert rows[0]["state"] == "not_collected"
    assert rows[0]["latest_request_state"] is None
    assert rows[2]["state"] == "non_observation_day"
    assert (
        rows[-1]["value"] == "-0.125" and rows[-1]["latest_request_state"] == "failed"
    )
