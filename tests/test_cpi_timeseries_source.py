"""Offline range/secret/retry contract tests; no live provider access."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from market_data_foundation.historical import cpi_timeseries_source as source
from market_data_foundation.historical.bls_cpi_direct_live import HttpResult

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
TEST_KEY = "a" * 32


def response(series_ids=source.SERIES_IDS, rows=None):
    if rows is None:
        rows = [
            {
                "year": "2026",
                "period": "M09",
                "value": "320.123",
                "calculations": {"pct_changes": {"1": "0.2", "12": "2.9"}},
                "footnotes": [{"code": "P", "text": "Preliminary"}],
            }
        ]
    return {
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [{"seriesID": sid, "data": rows} for sid in series_ids]},
    }


def encoded(document):
    return json.dumps(document).encode()


class WindowTransport:
    def __init__(self, statuses=()):
        self.requests = []
        self.statuses = list(statuses)

    def post(self, endpoint, body, headers, *, timeout, max_response_bytes):
        request = json.loads(body)
        self.requests.append(request)
        if self.statuses:
            status = self.statuses.pop(0)
            if isinstance(status, Exception):
                raise status
            if status != 200:
                return HttpResult(status, b"", {})
        rows = [
            {
                "year": str(year),
                "period": f"M{month:02d}",
                "value": str(100 + year % 100 + month / 100),
                "calculations": {"pct_changes": {"1": "0.2", "12": "2.9"}},
                "footnotes": [],
            }
            for year in range(int(request["startyear"]), int(request["endyear"]) + 1)
            for month in range(1, 13)
        ]
        return HttpResult(200, encoded(response(request["seriesid"], rows)), {})


@pytest.fixture
def key_loader(monkeypatch):
    calls = []

    def load(path):
        calls.append(path)
        return TEST_KEY

    monkeypatch.setattr(source, "load_registration_key", load)
    return calls


def test_registered_decades_are_two_requests_not_monthly(key_loader):
    transport = WindowTransport()
    batches = source.BlsRangeSource(transport=transport, clock=lambda: NOW).fetch(
        source.SERIES_IDS, "1999-09", "2026-09"
    )
    assert len(batches) == len(transport.requests) == 2
    assert [
        (request["startyear"], request["endyear"]) for request in transport.requests
    ] == [("1999", "2018"), ("2019", "2026")]
    points = [point for batch in batches for point in batch.points]
    assert len(points) == (27 * 12 + 1) * 4
    assert len({(p.series_id, p.month) for p in points}) == len(points)
    assert min(point.month for point in points) == "1999-09"
    assert max(point.month for point in points) == "2026-09"
    assert all(batch.observed_at == NOW and batch.attempts == 1 for batch in batches)
    assert len(batches[0].raw) > 100_000
    assert all("registrationkey" not in batch.request for batch in batches)
    assert all(TEST_KEY.encode() not in batch.raw for batch in batches)
    assert all(
        request["calculations"] and request["catalog"] for request in transport.requests
    )
    assert len(key_loader) == 1


def test_unregistered_uses_ten_year_windows_without_loading_secret(key_loader):
    transport = WindowTransport()
    batches = source.BlsRangeSource(
        registered=False, transport=transport, clock=lambda: NOW
    ).fetch(source.SERIES_IDS, "1999-01", "2026-09")
    assert len(batches) == 3
    assert [(r["startyear"], r["endyear"]) for r in transport.requests] == [
        ("1999", "2008"),
        ("2009", "2018"),
        ("2019", "2026"),
    ]
    assert not key_loader
    assert all("registrationkey" not in request for request in transport.requests)


def test_budget_and_input_validation_precede_credentials_or_io(key_loader):
    transport = WindowTransport()
    client = source.BlsRangeSource(transport=transport, max_requests=1)
    with pytest.raises(source.CpiSourceError, match="request_budget_exceeded"):
        client.fetch(source.SERIES_IDS, "1999-01", "2026-09")
    with pytest.raises(source.CpiSourceError, match="range_invalid"):
        client.fetch(source.SERIES_IDS, "2026-09", "2026-01")
    with pytest.raises(source.CpiSourceError, match="month_invalid"):
        client.fetch(source.SERIES_IDS, "2026-13", "2026-13")
    with pytest.raises(source.CpiSourceError, match="series_invalid"):
        client.fetch((source.SERIES_IDS[0], source.SERIES_IDS[0]), "2026-01", "2026-09")
    assert not key_loader and not transport.requests


def test_parser_preserves_original_calculations_and_ignores_annual_average():
    document = response()
    for series in document["Results"]["series"]:
        series["data"] = [
            *series["data"],
            {"year": "2026", "period": "M13", "value": "123.4"},
        ]
    points = source.parse_response(
        encoded(document), source.SERIES_IDS, "2026-01", "2026-09"
    )
    assert len(points) == 4  # Missing months stay visible to the service as gaps.
    assert points[0].index_value == Decimal("320.123")
    assert points[0].provider_calculations == {"pct_changes": {"1": "0.2", "12": "2.9"}}
    assert points[0].footnotes == ({"code": "P", "text": "Preliminary"},)


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "0", "-1", "1x2", None, 123, "1e2"]
)
def test_parser_rejects_invalid_index(value):
    document = response()
    document["Results"]["series"][0]["data"][0]["value"] = value
    with pytest.raises(source.CpiSourceError, match="index_invalid"):
        source.parse_response(
            encoded(document), source.SERIES_IDS, "2026-01", "2026-09"
        )


@pytest.mark.parametrize(
    ("mutation", "classification"),
    [
        (lambda d: d["Results"]["series"].pop(), "series_missing"),
        (
            lambda d: d["Results"]["series"].append(d["Results"]["series"][0]),
            "series_response_invalid",
        ),
        (
            lambda d: d["Results"]["series"][0]["data"].append(
                d["Results"]["series"][0]["data"][0]
            ),
            "duplicate_point",
        ),
        (
            lambda d: d["Results"]["series"][0]["data"][0].update(period="M00"),
            "period_invalid",
        ),
        (
            lambda d: d["Results"]["series"][0]["data"][0].update(year="2025"),
            "period_invalid",
        ),
        (
            lambda d: d["Results"]["series"][0]["data"][0].update(footnotes="bad"),
            "metadata_invalid",
        ),
        (lambda d: d.update(status="REQUEST_FAILED"), "provider_rejected"),
        (
            lambda d: d.update(registrationkey="do-not-retain"),
            "credential_echo_forbidden",
        ),
    ],
)
def test_parser_fails_closed(mutation, classification):
    document = response()
    mutation(document)
    with pytest.raises(source.CpiSourceError, match=classification):
        source.parse_response(
            encoded(document), source.SERIES_IDS, "2026-01", "2026-09"
        )


@pytest.mark.parametrize(
    ("raw", "classification"),
    [
        (b'{"status":"x","status":"REQUEST_SUCCEEDED"}', "duplicate_json_key"),
        (b'{"unexpected":NaN}', "invalid_json"),
        (b"[]", "response_invalid"),
        (b"<html>upstream failure</html>", "invalid_json"),
        (b"", "response_size_invalid"),
    ],
)
def test_invalid_json_has_safe_classification(raw, classification):
    with pytest.raises(source.CpiSourceError, match=classification):
        source.parse_response(raw, source.SERIES_IDS, "2026-01", "2026-09")


def test_shared_retry_policy_applies_to_wire_only(key_loader):
    transport = WindowTransport([503, TimeoutError("sensitive wire message"), 200])
    sleeps = []
    batches = source.BlsRangeSource(
        transport=transport, clock=lambda: NOW, sleep=sleeps.append
    ).fetch(source.SERIES_IDS, "2026-09", "2026-09")
    assert len(transport.requests) == 3
    assert batches[0].attempts == 3
    assert sleeps == [1.0, 2.0]


def test_retry_exhaustion_never_exposes_transport_message(key_loader):
    transport = WindowTransport([TimeoutError(TEST_KEY)] * 3)
    with pytest.raises(source.CpiSourceError) as caught:
        source.BlsRangeSource(transport=transport, sleep=lambda _: None).fetch(
            source.SERIES_IDS, "2026-09", "2026-09"
        )
    assert caught.value.attempts == 3
    assert TEST_KEY not in str(caught.value)
    assert len(transport.requests) == 3


@pytest.mark.parametrize(
    ("raw", "headers", "classification"),
    [
        (encoded({"status": "REQUEST_FAILED"}), {}, "provider_rejected"),
        (encoded({"message": TEST_KEY}), {}, "credential_echo_forbidden"),
        (
            encoded(response()),
            {"content-encoding": "gzip"},
            "content_encoding_rejected",
        ),
    ],
)
def test_parse_failure_and_credential_echo_are_not_retried(
    key_loader, raw, headers, classification
):
    class FixedTransport:
        calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            return HttpResult(200, raw, headers)

    transport = FixedTransport()
    with pytest.raises(source.CpiSourceError, match=classification) as caught:
        source.BlsRangeSource(transport=transport, clock=lambda: NOW).fetch(
            source.SERIES_IDS, "2026-09", "2026-09"
        )
    assert transport.calls == caught.value.attempts == 1
    assert TEST_KEY not in str(caught.value)


def test_naive_observation_clock_is_rejected_after_exactly_one_request(key_loader):
    transport = WindowTransport()
    with pytest.raises(source.CpiSourceError, match="observed_at_invalid"):
        source.BlsRangeSource(
            transport=transport, clock=lambda: datetime(2026, 10, 28)
        ).fetch(source.SERIES_IDS, "2026-09", "2026-09")
    assert len(transport.requests) == 1


@pytest.mark.parametrize("placement", ["message", "key", "nested"])
def test_unicode_escaped_credential_echo_is_rejected(key_loader, placement):
    escaped = "\\u0061" * 32
    document = response()
    raw = encoded(document)
    if placement == "message":
        raw = raw[:-1] + b', "message": "prefix-' + escaped.encode() + b'-suffix"}'
    elif placement == "key":
        raw = raw[:-1] + b', "' + escaped.encode() + b'": "unexpected"}'
    else:
        raw = raw[:-1] + b', "other": [{"text": "' + escaped.encode() + b'"}]}'
    assert TEST_KEY.encode() not in raw

    class EchoTransport:
        calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            return HttpResult(200, raw, {})

    transport = EchoTransport()
    with pytest.raises(
        source.CpiSourceError, match="credential_echo_forbidden"
    ) as caught:
        source.BlsRangeSource(transport=transport, clock=lambda: NOW).fetch(
            source.SERIES_IDS, "2026-09", "2026-09"
        )
    assert transport.calls == caught.value.attempts == 1
    assert TEST_KEY not in str(caught.value)


def test_overflowed_json_number_is_rejected():
    raw = encoded(response())
    raw = raw[:-1] + b', "responseTime": 1e9999}'
    with pytest.raises(source.CpiSourceError, match="invalid_json"):
        source.parse_response(raw, source.SERIES_IDS, "2026-09", "2026-09")


def unavailable_response():
    document = json.loads(encoded(response()))
    row = document["Results"]["series"][0]["data"][0]
    row["value"] = "-"
    row["footnotes"] = [
        {
            "code": "X",
            "text": "Data unavailable due to the 2025 lapse in appropriations",
        }
    ]
    return document


def test_explicit_provider_absence_is_retained_with_original_metadata():
    document = unavailable_response()
    raw = encoded(document)
    points = source.parse_response(raw, source.SERIES_IDS, "2026-09", "2026-09")
    assert len(points) == 4
    missing = next(point for point in points if point.series_id == source.SERIES_IDS[0])
    assert missing.index_value is None
    assert missing.provider_calculations == {"pct_changes": {"1": "0.2", "12": "2.9"}}
    assert missing.footnotes == (
        {
            "code": "X",
            "text": "Data unavailable due to the 2025 lapse in appropriations",
        },
    )
    assert sum(point.index_value is not None for point in points) == 3
    assert raw == encoded(document)


@pytest.mark.parametrize("field", ["calculations", "footnotes"])
def test_explicit_provider_absence_still_validates_metadata(field):
    document = unavailable_response()
    document["Results"]["series"][0]["data"][0][field] = "invalid metadata"
    with pytest.raises(source.CpiSourceError, match="metadata_invalid"):
        source.parse_response(
            encoded(document), source.SERIES_IDS, "2026-09", "2026-09"
        )


def test_explicit_provider_absence_still_rejects_duplicate_month():
    document = unavailable_response()
    rows = document["Results"]["series"][0]["data"]
    rows.append(dict(rows[0]))
    with pytest.raises(source.CpiSourceError, match="duplicate_point"):
        source.parse_response(
            encoded(document), source.SERIES_IDS, "2026-09", "2026-09"
        )


@pytest.mark.parametrize("value", ["", " -", "- ", "--", "N/A", ".", None, 0])
def test_only_exact_observed_absence_marker_is_accepted(value):
    document = unavailable_response()
    document["Results"]["series"][0]["data"][0]["value"] = value
    with pytest.raises(source.CpiSourceError, match="index_invalid"):
        source.parse_response(
            encoded(document), source.SERIES_IDS, "2026-09", "2026-09"
        )


def test_batch_keeps_provider_original_missing_marker(key_loader):
    raw = encoded(unavailable_response())

    class UnavailableTransport:
        calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            return HttpResult(200, raw, {})

    transport = UnavailableTransport()
    batches = source.BlsRangeSource(transport=transport, clock=lambda: NOW).fetch(
        source.SERIES_IDS, "2026-09", "2026-09"
    )
    assert len(batches) == transport.calls == 1
    assert batches[0].raw == raw
    assert any(point.index_value is None for point in batches[0].points)
