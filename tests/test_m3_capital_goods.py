"""Deterministic contracts; synthetic numbers are not live NXA qualification."""

import copy
import json
import socket
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import validate

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.historical.census_retail_live import (
    ExactCensusTransport,
    HttpResult,
)
from market_data_foundation.historical.m3_capital_goods import (
    FIELDS,
    QUALIFICATION,
    SERIES,
    M3Request,
    month_over_month,
    parse_response,
)
from market_data_foundation.historical.m3_capital_goods_source import (
    M3CandidateSource,
    M3HttpsTransport,
    M3SourceError,
    qualify_candidate,
)

ROOT = Path(__file__).resolve().parents[1]
TABLES = json.loads((ROOT / "tests/fixtures/m3/synthetic-tables.json").read_text())
STAMP = datetime(2026, 11, 3, 14, 37, tzinfo=UTC)
DUMMY = "synthetic0123456789abcdef"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("deterministic M3 tests attempted external IO")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def request(dataset="m3", category="MDM"):
    return M3Request(dataset, category, "2025-01", "2025-02")


def table(category="MDM"):
    return copy.deepcopy(TABLES[category])


def encoded(t):
    return json.dumps(t).encode()


def parse(t, req=None, **options):
    return parse_response(encoded(t), req or request(), obtained_at=STAMP, **options)


def change(t, field, value):
    for i, name in enumerate(t[0]):
        if name == field:
            t[1][i] = value
    return t


@pytest.mark.parametrize("dataset,stage", [("advm3", "advance"), ("m3", "full")])
@pytest.mark.parametrize("category,kinds", [("MDM", "NO"), ("NXA", "NO,VS")])
def test_official_example_request_shape_without_slot_predicate(
    dataset, stage, category, kinds
):
    req = request(dataset, category)
    params = req.persisted()["parameters"]
    assert params["get"].split(",") == list(FIELDS)
    assert params["data_type_code"] == kinds
    assert "time_slot_id" not in params and "program_code" not in params
    assert req.report_stage == stage
    assert req.endpoint.endswith("/" + dataset)
    assert "key" not in json.dumps(req.persisted())


@pytest.mark.parametrize(
    "args",
    [
        ("marts", "MDM", "2025-01", "2025-02"),
        ("m3", "NX", "2025-01", "2025-02"),
        ("m3", "MDM", "2025-1", "2025-02"),
        ("m3", "MDM", "2025-01", "2025-03"),
        ("m3", "MDM", "2025-02", "2025-01"),
    ],
)
def test_request_rejects_unbounded_or_wrong_scope(args):
    with pytest.raises(ContractError):
        M3Request(*args)


@pytest.mark.parametrize("category", ["MDM", "NXA"])
def test_projection_keeps_orders_shipments_units_and_version_separate(category):
    snap = parse(table(category), request(category=category))
    assert snap.complete and snap.qualification == QUALIFICATION
    assert snap.official_release_event is None and snap.source_version_date is None
    assert snap.obtained_at == STAMP and len(snap.raw_sha256) == 64
    assert snap.version_basis == "current_source_snapshot"
    assert all(
        c.unit == "million_current_usd"
        and c.frequency == "monthly"
        and c.seasonal_adjustment == "SA"
        and not c.annualized
        for c in snap.cells
    )
    assert all(c.time_slot_id == "0" for c in snap.cells)
    for kind in request(category=category).kinds:
        assert month_over_month(snap, SERIES[(category, kind)]) == (Decimal("10"), None)


@pytest.mark.parametrize(
    "value,expected", [("0", "0"), ("-12.5", "-12.5"), ("+12.5", "12.5")]
)
def test_net_orders_accept_signed_finite_amounts_and_real_zero(value, expected):
    snap = parse(change(table(), "cell_value", value))
    assert snap.cells[0].value == Decimal(expected)
    assert snap.cells[0].status == "available"
    if value == "0":
        assert month_over_month(snap, SERIES[("MDM", "NO")]) == (
            None,
            "zero_denominator",
        )


@pytest.mark.parametrize("value", [None, "", "NA", "(S)", "(D)", "N/A", "(NA)"])
def test_missing_is_never_zero(value):
    snap = parse(change(table(), "cell_value", value))
    assert snap.cells[0].value is None and not snap.complete
    assert month_over_month(snap, SERIES[("MDM", "NO")]) == (None, "input_missing")


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "-Infinity", "1e3", "1,000", "??", 0, True]
)
def test_unrecognized_values_are_rejected(value):
    with pytest.raises(ContractError):
        parse(change(table(), "cell_value", value))


def test_absent_row_is_local_missing_with_no_invented_slot_or_pointer():
    t = table("NXA")
    t.pop(2)
    snap = parse(t, request(category="NXA"))
    missing = [c for c in snap.cells if c.reason == "source_row_absent"]
    assert len(missing) == 1 and missing[0].data_type_code == "VS"
    assert (
        missing[0].value is missing[0].source_pointer is missing[0].time_slot_id is None
    )
    assert len([c for c in snap.cells if c.value is not None]) == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("category_code", "NXA"),
        ("data_type_code", "VS"),
        ("seasonally_adj", "no"),
        ("error_data", "yes"),
        ("us", "2"),
        ("time", "2024-12"),
        ("time_slot_id", "1"),
    ],
)
def test_wrong_scope_or_unknown_slot_rejected(field, value):
    with pytest.raises(ContractError):
        parse(change(table(), field, value))


@pytest.mark.parametrize(
    "field", ["category_code", "data_type_code", "seasonally_adj", "error_data"]
)
def test_conflicting_repeated_dimensions_rejected(field):
    t = table()
    t[1][t[0].index(field)] = "conflict"
    with pytest.raises(ContractError, match="duplicate_dimension_conflict"):
        parse(t)


def test_permuted_columns_and_single_dimensions_are_supported():
    t = table()
    indices = list(reversed(range(len(t[0]))))
    assert parse([[r[i] for i in indices] for r in t]).complete
    indices = [i for i, v in enumerate(t[0]) if t[0].index(v) == i]
    assert parse([[r[i] for i in indices] for r in t]).complete


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_cell",
        "duplicate_value_column",
        "unknown_column",
        "ragged",
        "object",
        "bad_json",
    ],
)
def test_ambiguous_or_unrecognized_structures_fail_closed(case):
    t = table()
    if case == "duplicate_cell":
        t.append(t[1])
    elif case == "duplicate_value_column":
        for r in t:
            r.append(r[4])
    elif case == "unknown_column":
        t[0][1] = "unknown"
    elif case == "ragged":
        t[1].pop()
    elif case == "object":
        t = {"data": t}
    raw = b"not-json" if case == "bad_json" else encoded(t)
    with pytest.raises(ContractError):
        parse_response(raw, request(), obtained_at=STAMP)


def test_capture_requires_timezone_and_raw_or_escaped_key_echo_is_rejected():
    with pytest.raises(ContractError, match="aware_capture"):
        parse_response(
            encoded(table()), request(), obtained_at=STAMP.replace(tzinfo=None)
        )
    raw = encoded(change(table(), "cell_value", DUMMY))
    for body in (raw, raw.replace(b"synthetic", b"\\u0073ynthetic")):
        with pytest.raises(ContractError, match="credential_echo"):
            parse_response(body, request(), obtained_at=STAMP, forbidden_secret=DUMMY)


class Control:
    def __init__(self):
        self.admissions = 0
        self.responses = []

    @contextmanager
    def attempt(self):
        self.admissions += 1
        yield self

    def record_response(self, status, headers):
        self.responses.append(status)


class Transport:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def source(outcome=None, enabled=True):
    control = Control()
    transport = Transport(
        outcome
        if outcome is not None
        else HttpResult(200, encoded(table()), {"content-type": "application/json"})
    )
    return (
        M3CandidateSource(
            credential_loader=lambda: DUMMY,
            request_control=control,
            enabled=enabled,
            transport=transport,
            clock=lambda: STAMP,
        ),
        control,
        transport,
    )


def test_default_off_precedes_any_private_or_transport_access():
    def forbidden():
        pytest.fail("private loader called before opt-in")

    s = M3CandidateSource(credential_loader=forbidden, request_control=None)
    with pytest.raises(ContractError, match="explicit_opt_in"):
        s.fetch(request())


def test_one_admitted_exchange_uses_only_explicit_dataset_and_saved_key():
    s, c, t = source()
    snap = s.fetch(request("advm3"))
    assert snap.request.report_stage == "advance"
    assert c.admissions == 1 and c.responses == [200] and len(t.calls) == 1
    args, options = t.calls[0]
    assert args[0].endswith("/advm3") and args[1]["key"] == DUMMY
    assert "time_slot_id" not in args[1] and options["timeout"] == 20


@pytest.mark.parametrize(
    "outcome",
    [
        HttpResult(204, b"", {}),
        HttpResult(429, b"", {}),
        TimeoutError("private diagnostic"),
    ],
)
def test_failure_does_not_retry_or_fabricate_zero(outcome):
    s, c, t = source(outcome)
    with pytest.raises(M3SourceError) as caught:
        s.fetch(request())
    assert caught.value.attempts == 1 and c.admissions == 1 and len(t.calls) == 1
    assert "private diagnostic" not in str(caught.value) and DUMMY not in str(
        caught.value
    )
    assert caught.value.__cause__ is None


def test_marts_transport_remains_pinned_and_m3_rejects_unrelated_endpoints():
    with pytest.raises(ContractError):
        ExactCensusTransport().get(
            request().endpoint, {}, timeout=20, max_response_bytes=100
        )
    for endpoint in (
        "https://api.census.gov/data/timeseries/eits/marts",
        "https://elsewhere.invalid/m3",
    ):
        with pytest.raises(ContractError):
            M3HttpsTransport().get(endpoint, {}, timeout=20, max_response_bytes=100)


def test_qualification_uses_macro_schema_and_local_artifacts_without_promotion(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    s, _, _ = source()
    directory = tmp_path / "artifacts" / "test-run"
    record = qualify_candidate(
        s, request(), artifact_directory=directory, allow_live=True
    )
    validate(
        record,
        json.loads((ROOT / "provider_lab/macro-observation.schema.json").read_text()),
    )
    assert record["success"] and record["semantics"]["qualification"] == QUALIFICATION
    assert DUMMY not in "".join(p.read_text() for p in directory.iterdir())
    assert (
        json.loads((directory / "capture.json").read_text())["official_release_event"]
        is None
    )
    s, _, t = source()
    with pytest.raises(ContractError, match="opt_in"):
        qualify_candidate(s, request(), artifact_directory=directory / "disabled")
    assert not t.calls


@pytest.mark.parametrize(
    "status,headers,body,valid",
    [
        (
            200,
            [("Content-Type", "application/json"), ("Content-Length", "2")],
            b"[]",
            True,
        ),
        (302, [("Location", "https://elsewhere.invalid")], b"", True),
        (200, [("Content-Length", "3")], b"[]", False),
        (200, [("Content-Length", "2"), ("Content-Length", "2")], b"[]", False),
        (
            200,
            [("Content-Length", "2"), ("Transfer-Encoding", "chunked")],
            b"[]",
            False,
        ),
        (200, [("Content-Length", "101")], b"[]", False),
    ],
)
def test_exact_transport_framing_size_and_no_redirect(
    monkeypatch, status, headers, body, valid
):
    from market_data_foundation.historical import m3_capital_goods_source as module

    calls = []

    class Response:
        def __init__(self):
            self.status = status
            self.remaining = body

        def getheaders(self):
            return headers

        def read1(self, size):
            result, self.remaining = self.remaining[:size], self.remaining[size:]
            return result

    class Connection:
        sock = None

        def __init__(self, host, timeout):
            calls.append((host, timeout))

        def request(self, method, path, headers):
            calls.append((method, path, headers))

        def getresponse(self):
            return Response()

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(module.http.client, "HTTPSConnection", Connection)
    if valid:
        result = M3HttpsTransport().get(
            request().endpoint,
            {"get": "cell_value"},
            timeout=20,
            max_response_bytes=100,
        )
        assert result.status == status
        if status == 302:
            assert result.body == b""
    else:
        with pytest.raises(ContractError):
            M3HttpsTransport().get(
                request().endpoint, {}, timeout=20, max_response_bytes=100
            )
    assert len([c for c in calls if isinstance(c, tuple) and c[0] == "GET"]) == 1
    assert calls[-1] == "closed"


def test_failed_qualification_retains_safe_attempt_and_http_facts(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    s, _, t = source(HttpResult(204, b"", {}))
    record = qualify_candidate(
        s,
        request(),
        artifact_directory=tmp_path / "artifacts" / "no-content",
        allow_live=True,
    )
    validate(
        record,
        json.loads((ROOT / "provider_lab/macro-observation.schema.json").read_text()),
    )
    assert not record["success"] and len(t.calls) == 1
    assert record["semantics"]["http_status"] == "204"
    assert record["semantics"]["transport_attempts"] == "1"
    assert record["semantics"]["source_classification"] == "census_m3_http_failure"
    assert "message" not in record["exception"]
