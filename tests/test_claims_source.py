import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from market_data_foundation.historical.claims_contract import SERIES_IDS, ClaimsError
from market_data_foundation.historical.claims_source import (
    ClaimsFredSource,
    ClaimsResponse,
    parse_metadata,
    parse_observations,
)
from market_data_foundation.historical.cpi_request_control import CpiRequestControl

NOW = datetime(2026, 11, 2, tzinfo=UTC)
START, END = "2026-10-01", "2026-10-31"


def body(rows=None, **changes):
    doc = {
        "count": 1,
        "offset": 0,
        "units": "lin",
        "output_type": 1,
        "observation_start": START,
        "observation_end": END,
        "sort_order": "asc",
        "observations": rows
        if rows is not None
        else [
            {
                "date": "2026-10-24",
                "value": "194000",
                "realtime_start": "2026-11-02",
                "realtime_end": "2026-11-02",
            }
        ],
    }
    doc.update(changes)
    return json.dumps(doc).encode()


class Control:
    def __init__(self):
        self.calls = []

    @contextmanager
    def attempt(self):
        yield self

    def record_response(self, status, headers):
        self.calls.append(status)
        return NOW + timedelta(seconds=300) if status == 429 else None


def test_parse_keeps_decimal_null_date_precision_and_event_unbound():
    point = parse_observations(body(), "ICSA", START, END)[0]
    assert point.value == Decimal("194000")
    assert point.week_ending == "2026-10-24"
    assert point.provider_extra["event_binding_status"] == "unconfirmed"
    assert point.provider_extra["realtime_precision"] == "date"
    doc = json.loads(body())
    doc["observations"][0]["value"] = "."
    missing = parse_observations(json.dumps(doc).encode(), "ICSA", START, END)[0]
    assert missing.value is None
    assert missing.provider_extra["source_missing_reason"] == "provider_missing"


@pytest.mark.parametrize(
    "change",
    [
        {"count": 2},
        {"offset": 1},
        {"units": "pch"},
        {"output_type": 2},
        {"observation_start": "2000-01-01"},
        {"sort_order": "desc"},
    ],
)
def test_observation_contract_rejects_partial_or_wrong_transformation(change):
    with pytest.raises(ClaimsError):
        parse_observations(body(**change), "ICSA", START, END)


@pytest.mark.parametrize(
    "key,value",
    [
        ("date", "2026-10-23"),
        ("date", "2026-11-07"),
        ("value", "NaN"),
        ("value", "-1"),
        ("value", ""),
        ("value", "1,000"),
        ("realtime_start", "2026-11-03"),
    ],
)
def test_bad_rows_rejected(key, value):
    doc = json.loads(body())
    doc["observations"][0][key] = value
    with pytest.raises(ClaimsError):
        parse_observations(json.dumps(doc).encode(), "ICSA", START, END)


def test_duplicate_week_rejected_and_empty_response_not_zero():
    doc = json.loads(body())
    with pytest.raises(ClaimsError, match="week_ending_invalid"):
        parse_observations(body(doc["observations"] * 2, count=2), "ICSA", START, END)
    assert parse_observations(body([], count=0), "ICSA", START, END) == ()


@pytest.mark.parametrize("sid", SERIES_IDS)
def test_metadata_binds_native_unit_seasonality_and_week(sid):
    data = {
        "id": sid,
        "frequency_short": "W",
        "frequency": "Weekly, Ending Saturday",
        "units": "Number",
        "seasonal_adjustment_short": "NSA" if sid.endswith("NSA") else "SA",
    }
    assert parse_metadata(json.dumps({"seriess": [data]}).encode(), sid) == data
    data["units"] = "Thousands"
    with pytest.raises(ClaimsError, match="metadata_mapping_invalid"):
        parse_metadata(json.dumps({"seriess": [data]}).encode(), sid)


def test_three_attempt_transport_policy_and_shared_admission():
    control = Control()
    replies = iter(
        [
            TimeoutError("synthetic-sensitive"),
            ClaimsResponse(503, b"bad"),
            ClaimsResponse(200, body()),
        ]
    )

    def transport(request):
        response = next(replies)
        if isinstance(response, Exception):
            raise response
        return response

    sleeps, retained = [], []
    source = ClaimsFredSource(
        transport=transport,
        request_control=control,
        sleep=sleeps.append,
        raw_sink=lambda *args: retained.append(args),
    )
    batch = source.fetch(("ICSA",), START, END)[0]
    assert batch.attempts == source.attempts == 3
    assert source.request_count == 1
    assert control.calls == [503, 200]
    assert sleeps == [1.0, 2.0]
    assert len(retained) == 2
    assert "api_key" not in repr(batch.request)


def test_parse_failure_is_retained_and_never_retried():
    retained = []
    source = ClaimsFredSource(
        transport=lambda request: ClaimsResponse(200, b"broken"),
        request_control=Control(),
        raw_sink=lambda *args: retained.append(args),
    )
    with pytest.raises(ClaimsError, match="response_invalid"):
        source.fetch(("ICSA",), START, END)
    assert source.request_count == source.attempts == len(retained) == 1


def test_opt_in_budget_and_nonretryable_failure_are_explicit():
    with pytest.raises(ClaimsError, match="live_opt_in_required"):
        ClaimsFredSource()
    source = ClaimsFredSource(
        transport=lambda req: ClaimsResponse(403, b"denied"),
        request_control=Control(),
        sleep=lambda delay: pytest.fail("no retry"),
    )
    with pytest.raises(ClaimsError) as caught:
        source.fetch(("ICSA",), START, END)
    assert caught.value.attempts == 1
    assert caught.value.http_status == 403
    assert source.attempts == 1
    source = ClaimsFredSource(
        transport=lambda req: pytest.fail("no request"),
        request_control=Control(),
        max_requests=1,
    )
    with pytest.raises(ClaimsError, match="budget_exceeded"):
        source.fetch(("ICSA", "CCSA"), START, END)
    assert source.request_count == 0


def test_429_cooldown_is_shared_durable_and_no_sleep_poll(tmp_path):
    state = tmp_path / "fred.json"
    transport_calls = []

    def transport(req):
        transport_calls.append(req)
        return ClaimsResponse(429, b"rate limited", {"Retry-After": "600"})

    for _ in range(2):
        source = ClaimsFredSource(
            transport=transport,
            request_control=CpiRequestControl(state, clock=lambda: NOW),
            clock=lambda: NOW,
            sleep=lambda delay: pytest.fail("no cooldown polling"),
        )
        with pytest.raises(ClaimsError) as caught:
            source.fetch(("ICSA",), START, END)
        assert caught.value.retry_at == NOW + timedelta(seconds=600)
    assert len(transport_calls) == 1


def test_terminal_transport_failure_does_not_expose_secret_or_raw_exception():
    calls = []

    def transport(request):
        calls.append(request)
        raise TimeoutError("synthetic-api-key-and-sensitive-url")

    source = ClaimsFredSource(
        transport=transport, request_control=Control(), sleep=lambda delay: None
    )
    with pytest.raises(ClaimsError) as caught:
        source.fetch(("ICSA",), START, END)
    assert str(caught.value) == "claims_timeout"
    assert caught.value.attempts == 3
    assert caught.value.request_count == 1
    assert len(calls) == 3


def test_raw_retention_failure_is_terminal_not_a_transport_retry():
    def sink(*args):
        raise OSError("synthetic-sensitive-filesystem-text")

    source = ClaimsFredSource(
        transport=lambda req: ClaimsResponse(200, body()),
        request_control=Control(),
        raw_sink=sink,
    )
    with pytest.raises(ClaimsError, match="claims_raw_retention_failed") as caught:
        source.fetch(("ICSA",), START, END)
    assert source.attempts == caught.value.attempts == 1


def test_spacing_is_one_bounded_wait_and_does_not_spend_transport_attempt(tmp_path):
    ticks = [NOW]

    def sleep(seconds):
        ticks[0] += timedelta(seconds=seconds)

    control = CpiRequestControl(
        tmp_path / "fred.json", clock=lambda: ticks[0], min_interval_seconds=1.1
    )
    source = ClaimsFredSource(
        transport=lambda req: ClaimsResponse(200, body()),
        request_control=control,
        clock=lambda: ticks[0],
        sleep=sleep,
    )
    source.fetch(("ICSA", "ICNSA"), START, END)
    assert source.request_count == source.attempts == 2
    assert ticks[0] == NOW + timedelta(seconds=1.1)
