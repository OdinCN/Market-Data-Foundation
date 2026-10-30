"""Synthetic BEA range contracts; never an upstream qualification."""

import base64
import copy
import json
import socket
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
from market_data_foundation.historical.bls_timeseries_profile import PCE_PROFILE
from market_data_foundation.historical.pce_timeseries_source import (
    BeaRangeSource,
    BeaRequestControl,
    PceSourceError,
    parse_response,
    year_windows,
)

AT = datetime(2026, 10, 30, tzinfo=UTC)
KEY = "synthetic-bea-not-a-real-key"
CONTROL = SimpleNamespace(
    provider="BEA", attempt=lambda: nullcontext(lambda retry_at: None)
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("synthetic tests attempted network access")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def synthetic_response(
    months=("2026-09",), *, value="103.120", key=KEY, table="T20804"
):
    official = table == "T20807"
    rows = [
        {
            "TableName": table,
            "LineNumber": line,
            "SeriesCode": series + "M" if official else series,
            "TimePeriod": month[:4] + "M" + month[-2:],
            "CL_UNIT": "Percent change" if official else "Level",
            "UNIT_MULT": "0",
            "METRIC_NAME": "Fisher Price Index",
            "DataValue": value,
        }
        for line, series in (("1", "DPCERG"), ("25", "DPCCRG"))
        for month in months
    ]
    return {
        "BEAAPI": {
            "Request": {
                "RequestParam": [{"ParameterName": "UserID", "ParameterValue": key}]
            },
            "Results": {
                "Data": rows,
                "Notes": [{"NoteRef": "LastRevised", "NoteText": "October 30, 2026"}],
                "UTCProductionTime": "2026-10-30T12:00:00",
            },
        }
    }


def encode(document):
    return json.dumps(document).encode()


class SyntheticTransport:
    def __init__(self, response=None, *, official_response=None):
        self.response, self.official_response = response, official_response
        self.calls, self.documents = [], []

    def post(self, endpoint, body, headers, **options):
        params = parse_qs(body.decode())
        self.calls.append(params)
        table = params["TableName"][0]
        document = self.response if table == "T20804" else self.official_response
        if document is None:
            months = tuple(
                f"{year}-{month:02d}"
                for year in params["Year"][0].split(",")
                for month in range(1, 13)
            )
            document = synthetic_response(
                months,
                value="0.3" if table == "T20807" else "100",
                key=params["UserID"][0],
                table=table,
            )
        self.documents.append(document)
        return HttpResult(200, encode(document), {})


def source(transport=None, **options):
    return BeaRangeSource(
        credential_loader=lambda: KEY,
        transport=transport or SyntheticTransport(),
        request_control=CONTROL,
        clock=lambda: AT,
        sleep=lambda _: None,
        **options,
    )


def test_real_protocol_shape_and_projection_never_keep_request_or_key():
    wire = SyntheticTransport()
    batches = source(wire).fetch(PCE_PROFILE.series_ids, "2005-10", "2026-09")
    assert len(batches) == 5
    assert len(wire.calls) == 10
    assert sum(len(b.points) for b in batches) == 2 * 252
    assert wire.calls[0]["Year"] == ["2005,2006,2007,2008,2009"]
    for number, batch in enumerate(batches):
        projection = json.loads(batch.raw)
        assert projection["exact_original_retained"] is False
        assert projection["retention_class"] == "credential_free_bea_results_projection"
        assert KEY.encode() not in batch.raw
        assert b"UserID" not in batch.raw and b"Request" not in batch.raw
        assert "UserID" not in batch.request
        assert batch.observed_at == AT and batch.attempts == 2
        pair = wire.calls[2 * number : 2 * number + 2]
        assert [call["TableName"][0] for call in pair] == ["T20804", "T20807"]
        assert pair[0]["Year"] == pair[1]["Year"]
        assert batch.request["TableName"] == "T20804"
        assert batch.request["request_count"] == 2
        assert batch.request["requests"] == [
            {name: values[0] for name, values in call.items() if name != "UserID"}
            for call in pair
        ]
        assert "UserID" not in repr(batch.request)
        assert projection["Results"] == wire.documents[2 * number]["BEAAPI"]["Results"]
        assert projection["official_mom"] == {
            "TableName": "T20807",
            "Results": wire.documents[2 * number + 1]["BEAAPI"]["Results"],
        }
        assert all(
            p.provider_calculations == {"bea_pct_1m": "0.3"} for p in batch.points
        )


@pytest.mark.parametrize(
    "field,value,classification",
    [
        ("TableName", "T20807", "table_invalid"),
        ("SeriesCode", "DPCERGM", "mapping_invalid"),
        ("LineNumber", "25", "mapping_invalid"),
        ("TimePeriod", "2026Q3", "period_invalid"),
        ("CL_UNIT", "Percent change", "unit_invalid"),
        ("UNIT_MULT", "6", "unit_invalid"),
        ("METRIC_NAME", "Current Dollars", "unit_invalid"),
        ("DataValue", "-1", "index_invalid"),
        ("DataValue", "NaN", "index_invalid"),
    ],
)
def test_invalid_provider_row_is_fixed_classification(field, value, classification):
    document = synthetic_response()
    document["BEAAPI"]["Results"]["Data"][0][field] = value
    with pytest.raises(PceSourceError, match=classification):
        parse_response(encode(document), PCE_PROFILE.series_ids, "2026-09", "2026-09")


@pytest.mark.parametrize("value", [None, "", "(NA)", "---"])
def test_explicit_empty_values_are_preserved_as_unavailable(value):
    points = parse_response(
        encode(synthetic_response(value=value)),
        PCE_PROFILE.series_ids,
        "2026-09",
        "2026-09",
    )
    assert len(points) == 2 and all(p.index_value is None for p in points)


def test_omissions_duplicates_and_unrelated_full_table_rows():
    document = synthetic_response()
    rows = document["BEAAPI"]["Results"]["Data"]
    rows.append(
        {**rows[0], "LineNumber": "2", "SeriesCode": "OTHER", "DataValue": "(NA)"}
    )
    assert len(parse_response(encode(document), ("DPCERG",), "2026-08", "2026-09")) == 1
    rows.append(copy.deepcopy(rows[0]))
    with pytest.raises(PceSourceError, match="duplicate_point"):
        parse_response(encode(document), ("DPCERG",), "2026-09", "2026-09")


@pytest.mark.parametrize(
    "echo", [KEY, base64.b64encode(KEY.encode()).decode(), KEY.encode().hex()]
)
def test_results_credential_echo_fails_before_returning_batch(echo):
    document = synthetic_response()
    document["BEAAPI"]["Results"]["Notes"][0]["NoteText"] = echo
    with pytest.raises(PceSourceError, match="credential_echo") as caught:
        source(SyntheticTransport(document)).fetch(
            PCE_PROFILE.series_ids, "2026-09", "2026-09"
        )
    assert caught.value.attempts == 1 and KEY not in str(caught.value)


@pytest.mark.parametrize(
    "echo",
    [
        KEY,
        base64.b64encode(KEY.encode()).decode(),
        KEY.encode().hex(),
        "".join(f"%{byte:02X}" for byte in KEY.encode()),
        "".join(f"&#{byte};" for byte in KEY.encode()),
    ],
)
def test_second_response_credential_echo_is_rejected(echo):
    official = synthetic_response(table="T20807", value="0.3")
    official["BEAAPI"]["Results"]["Notes"][0]["NoteText"] = echo
    wire = SyntheticTransport(official_response=official)
    with pytest.raises(PceSourceError, match="credential_echo") as caught:
        source(wire).fetch(("DPCERG",), "2026-09", "2026-09")
    assert caught.value.attempts == len(wire.calls) == 2
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("field", ["UserID", "Request", "RequestParam"])
def test_second_response_sensitive_key_is_rejected(field):
    official = synthetic_response(table="T20807")
    official["BEAAPI"]["Results"]["Notes"][0][field] = "redacted"
    with pytest.raises(PceSourceError, match="credential_echo") as caught:
        source(SyntheticTransport(official_response=official)).fetch(
            ("DPCERG",), "2026-09", "2026-09"
        )
    assert caught.value.attempts == 2


def test_provider_error_never_returns_raw_text_or_credentials():
    document = {"BEAAPI": {"Results": {"Error": {"APIErrorDescription": KEY}}}}
    with pytest.raises(PceSourceError, match="provider_rejected") as caught:
        source(SyntheticTransport(document)).fetch(
            PCE_PROFILE.series_ids, "2026-09", "2026-09"
        )
    assert KEY not in str(caught.value)


def test_retry_policy_and_exact_attempt_count():
    class RetryTransport(SyntheticTransport):
        attempts = 0

        def post(self, *args, **kwargs):
            self.attempts += 1
            if self.attempts < 3:
                raise TimeoutError(KEY)
            return super().post(*args, **kwargs)

    wire, sleeps = RetryTransport(), []
    client = BeaRangeSource(
        credential_loader=lambda: KEY,
        transport=wire,
        request_control=CONTROL,
        sleep=sleeps.append,
    )
    batch = client.fetch(("DPCERG",), "2026-09", "2026-09")[0]
    assert batch.attempts == 4 and sleeps == [1.0, 2.0]


def test_invalid_binding_and_bounded_plan_precede_credentials():
    with pytest.raises(PceSourceError, match="binding_invalid"):
        BeaRangeSource(request_control=SimpleNamespace())
    assert len(year_windows("2005-10", "2026-09")) == 5
    assert year_windows("2026-01", "2026-09", max_requests=2) == ((2026, 2026),)
    with pytest.raises(PceSourceError, match="budget_exceeded"):
        year_windows("2026-01", "2026-09", max_requests=1)
    assert len(year_windows("1901-01", "2020-12", max_requests=48)) == 24
    with pytest.raises(PceSourceError, match="budget_exceeded"):
        year_windows("1901-01", "2021-01", max_requests=48)
    with pytest.raises(PceSourceError, match="budget_exceeded"):
        year_windows("1901-01", "2200-12")
    client = BeaRangeSource(
        credential_loader=lambda: pytest.fail("loaded credential"), max_requests=1
    )
    with pytest.raises(PceSourceError, match="budget_exceeded"):
        client.fetch(("DPCERG",), "2000-01", "2026-09")


def test_control_does_not_swallow_transport_exception(tmp_path):
    control = BeaRequestControl(tmp_path / "bea-budget")
    with pytest.raises(TimeoutError):
        with control.attempt():
            raise TimeoutError("synthetic")
    assert KEY not in control.state_path.read_text()


def test_bls_source_cannot_bind_pce_and_bea_cannot_bind_bls():
    from market_data_foundation.historical.bls_timeseries_profile import CPI_PROFILE
    from market_data_foundation.historical.cpi_timeseries_source import BlsRangeSource

    with pytest.raises(PceSourceError, match="provider_binding_invalid"):
        BlsRangeSource(profile=PCE_PROFILE)
    with pytest.raises(PceSourceError, match="profile_mismatch"):
        BeaRangeSource(profile=CPI_PROFILE)


def test_provider_error_after_success_counts_every_wire_attempt():
    class LaterFailure(SyntheticTransport):
        def post(self, *args, **kwargs):
            if self.calls:
                return HttpResult(503, b"", {})
            return super().post(*args, **kwargs)

    with pytest.raises(PceSourceError, match="http_failure") as caught:
        source(LaterFailure(), max_transport_attempts=1).fetch(
            ("DPCERG",), "2010-01", "2026-09"
        )
    assert caught.value.attempts == 2 and caught.value.http_status == 503


def test_429_returns_retry_at_without_long_sleep():
    class Throttled:
        def post(self, *args, **kwargs):
            return HttpResult(429, b"", {"Retry-After": "600"})

    with pytest.raises(PceSourceError, match="http_failure") as caught:
        source(Throttled()).fetch(("DPCERG",), "2026-09", "2026-09")
    assert caught.value.attempts == 1
    assert (caught.value.retry_at - AT).total_seconds() == 600


def test_projection_is_safe_through_store_and_raw_port():
    from market_data_foundation.historical.cpi_timeseries_store import digest
    from market_data_foundation.historical.pce_timeseries_store import (
        InMemoryTimeSeriesStore,
        raw_locator,
    )

    class Raw:
        profile = PCE_PROFILE
        materials = []

        def put_verified(self, raw):
            self.materials.append(raw)
            return raw_locator(digest(raw))

    raw = Raw()
    store = InMemoryTimeSeriesStore(raw, clock=lambda: AT)
    batch = source().fetch(PCE_PROFILE.series_ids, "2026-09", "2026-09")[0]
    store.save_batches((batch,))
    assert len(raw.materials) == 1 and KEY.encode() not in raw.materials[0]
    snapshot = repr(store._batches) + repr(
        store.read_range(PCE_PROFILE.series_ids, "2026-09", "2026-09")
    )
    assert KEY not in snapshot and "UserID" not in snapshot


def test_failed_admission_reports_zero_wire_attempts(tmp_path):
    path = tmp_path / "invalid-control"
    path.write_text("invalid")
    wire = SyntheticTransport()
    client = BeaRangeSource(
        credential_loader=lambda: KEY,
        transport=wire,
        request_control=BeaRequestControl(path),
    )
    with pytest.raises(PceSourceError, match="request_control_failed") as caught:
        client.fetch(("DPCERG",), "2026-09", "2026-09")
    assert caught.value.attempts == 0 and wire.calls == []


@pytest.mark.parametrize("retry_after", ["600", "Fri, 30 Oct 2026 00:10:00 GMT"])
def test_two_instances_share_fixed_429_deadline_and_restart(tmp_path, retry_after):
    now = [AT]
    sleeps = []

    class Throttled:
        calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            return HttpResult(429, b"", {"Retry-After": retry_after})

    path = tmp_path / "shared-bea-control.json"
    wire = Throttled()

    def client():
        return BeaRangeSource(
            credential_loader=lambda: KEY,
            transport=wire,
            request_control_path=path,
            clock=lambda: now[0],
            sleep=sleeps.append,
        )

    with pytest.raises(PceSourceError) as first:
        client().fetch(("DPCERG",), "2026-09", "2026-09")
    deadline = AT + timedelta(seconds=600)
    assert first.value.retry_at == deadline and first.value.attempts == 1
    assert json.loads(path.read_text())["cooldown_until"] == deadline.timestamp()
    for _ in range(2):
        now[0] += timedelta(seconds=10)
        with pytest.raises(PceSourceError, match="request_cooldown") as blocked:
            client().fetch(("DPCERG",), "2026-09", "2026-09")
        assert blocked.value.retry_at == deadline and blocked.value.attempts == 0
    assert wire.calls == 1 and sleeps == []
    now[0] = deadline + timedelta(seconds=1)
    with pytest.raises(PceSourceError) as resumed:
        client().fetch(("DPCERG",), "2026-09", "2026-09")
    assert resumed.value.attempts == 1 and wire.calls == 2
    assert KEY not in path.read_text()


def test_utc_admission_rollback_has_bounded_spacing(tmp_path):
    path = tmp_path / "control"
    path.write_text(str(AT.timestamp() + 86400))
    sleeps = []
    with BeaRequestControl(path, clock=lambda: AT, sleep=sleeps.append).attempt():
        pass
    assert len(sleeps) == 1 and sleeps[0] <= 3.1


def test_default_instances_share_candidate_cwd_and_trusted_builder_path(
    tmp_path, monkeypatch
):
    from market_data_foundation.historical.cpi_timeseries import build_source

    monkeypatch.chdir(tmp_path)
    first = BeaRangeSource(clock=lambda: AT)
    second = BeaRangeSource(clock=lambda: AT)
    assert first._control.state_path == second._control.state_path
    assert (
        first._control.state_path
        == tmp_path / "artifacts/pce-timeseries/bea-request-control.json"
    )
    deadline = AT + timedelta(seconds=600)
    with first._control.attempt() as record_retry:
        record_retry(deadline)
    with pytest.raises(PceSourceError, match="request_cooldown") as caught:
        with second._control.attempt():
            pytest.fail("shared cooldown bypassed")
    assert caught.value.retry_at == deadline
    fixed = tmp_path / "host-bea-control.json"
    built = build_source(
        credential_loader=lambda: KEY, profile=PCE_PROFILE, request_control_path=fixed
    )
    assert built._control.state_path == fixed


def test_independent_process_recovers_utc_cooldown(tmp_path):
    path = tmp_path / "bea-control.json"
    deadline = AT + timedelta(seconds=600)
    with BeaRequestControl(path, clock=lambda: AT).attempt() as record_retry:
        record_retry(deadline)
    script = """
import json
import sys
from datetime import datetime
from market_data_foundation.historical.pce_timeseries_source import (
    BeaRequestControl, PceSourceError,
)
control = BeaRequestControl(
    sys.argv[1], clock=lambda: datetime.fromisoformat(sys.argv[2])
)
try:
    with control.attempt():
        print(json.dumps({"admitted": True}))
except PceSourceError as error:
    print(json.dumps({
        "classification": error.classification,
        "retry_at": error.retry_at.isoformat(),
    }))
"""
    for now in (AT, deadline + timedelta(seconds=1)):
        result = subprocess.run(
            [sys.executable, "-c", script, str(path), now.isoformat()],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        state = json.loads(result.stdout)
        if now < deadline:
            assert state == {
                "classification": "pce_timeseries_request_cooldown",
                "retry_at": deadline.isoformat(),
            }
        else:
            assert state == {"admitted": True}


def parse_pair(index, official, start="2026-08", end="2026-09"):
    return parse_response(
        encode(index),
        PCE_PROFILE.series_ids,
        start,
        end,
        official_raw=encode(official),
        forbidden_secret=KEY,
    )


def test_index_only_parsing_does_not_consume_paired_request_budget():
    points = parse_response(
        encode(synthetic_response()), ("DPCERG",), "1901-01", "2026-09"
    )
    assert len(points) == 1
    assert points[0].index_value == Decimal("103.120")
    assert points[0].provider_calculations == {}
    with pytest.raises(PceSourceError, match="range_invalid"):
        parse_response(encode(synthetic_response()), ("DPCERG",), "2026-09", "2026-08")


def test_official_percent_change_is_not_derived_from_index():
    index = synthetic_response(("2026-08", "2026-09"), value="100")
    for row in index["BEAAPI"]["Results"]["Data"]:
        if row["TimePeriod"] == "2026M09":
            row["DataValue"] = "150"
    official = synthetic_response(("2026-08", "2026-09"), table="T20807", value="0.3")
    points = parse_pair(index, official)
    assert len(points) == 4
    for point in points:
        assert point.provider_calculations == {"bea_pct_1m": "0.3"}
        if point.month == "2026-09":
            assert point.index_value == Decimal("150")
            assert (
                Decimal(point.provider_calculations["bea_pct_1m"])
                != (point.index_value / Decimal("100") - 1) * 100
            )


def test_exact_series_and_month_join_never_fabricates_index_points():
    months = ("2026-07", "2026-08", "2026-09")
    index = synthetic_response(months)
    rows = index["BEAAPI"]["Results"]["Data"]
    rows[:] = [
        r for r in rows if (r["SeriesCode"], r["TimePeriod"]) != ("DPCERG", "2026M07")
    ]
    for row in rows:
        if (row["SeriesCode"], row["TimePeriod"]) == ("DPCCRG", "2026M08"):
            row["DataValue"] = "(NA)"
    official = synthetic_response(months, table="T20807")
    changes = {
        ("DPCERGM", "2026M07"): "9.9",  # Official-only row.
        ("DPCERGM", "2026M08"): "-0.2",
        ("DPCERGM", "2026M09"): "0",
        ("DPCCRGM", "2026M07"): "1.1",
        ("DPCCRGM", "2026M08"): "0.3",
        ("DPCCRGM", "2026M09"): "2.2",
    }
    official_rows = official["BEAAPI"]["Results"]["Data"]
    for row in official_rows:
        row["DataValue"] = changes[(row["SeriesCode"], row["TimePeriod"])]
    official_rows.reverse()
    points = parse_pair(index, official, "2026-07")
    actual = {(p.series_id, p.month): p for p in points}
    assert len(points) == 5 and ("DPCERG", "2026-07") not in actual
    for (series, month), point in actual.items():
        assert point.provider_calculations == {
            "bea_pct_1m": changes[(series + "M", month.replace("-", "M"))]
        }
    assert actual[("DPCCRG", "2026-08")].index_value is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("-0.3000", "-0.3"),
        ("0.000", "0"),
        ("-0.00", "0"),
        ("+0.300", "0.3"),
        ("001.2300", "1.23"),
        ("100.000", "100"),
        (
            "0.123456789012345678901234567890123456789",
            "0.123456789012345678901234567890123456789",
        ),
    ],
)
def test_signed_changes_are_canonical_decimals_without_precision_loss(value, expected):
    with localcontext() as context:
        context.prec = 5
        points = parse_pair(
            synthetic_response(), synthetic_response(table="T20807", value=value)
        )
    assert all(p.provider_calculations == {"bea_pct_1m": expected} for p in points)


@pytest.mark.parametrize("value", [None, "", "(NA)", "NA", "---", "--", "..."])
def test_unavailable_or_missing_official_rows_do_not_derive_change(value):
    index = synthetic_response(("2026-08", "2026-09"))
    official = synthetic_response(("2026-09",), table="T20807", value=value)
    points = parse_pair(index, official)
    assert len(points) == 4
    assert all(p.provider_calculations == {} for p in points)
    official["BEAAPI"]["Results"]["Data"] = []
    assert all(p.provider_calculations == {} for p in parse_pair(index, official))


@pytest.mark.parametrize(
    "field,value,classification",
    [
        ("TableName", "T20804", "table_invalid"),
        ("SeriesCode", "DPCERG", "mapping_invalid"),
        ("SeriesCode", "DPCCRGM", "mapping_invalid"),
        ("LineNumber", "25", "mapping_invalid"),
        ("LineNumber", "2", "mapping_invalid"),
        ("LineNumber", 1, "series_invalid"),
        ("TimePeriod", "2026Q3", "period_invalid"),
        ("TimePeriod", "2026M13", "period_invalid"),
        ("TimePeriod", "2025M09", "period_invalid"),
        ("CL_UNIT", "Level", "unit_invalid"),
        ("UNIT_MULT", "2", "unit_invalid"),
        ("METRIC_NAME", "Current Dollars", "unit_invalid"),
        ("DataValue", "NaN", "pct_1m_invalid"),
        ("DataValue", "Infinity", "pct_1m_invalid"),
        ("DataValue", 0.3, "pct_1m_invalid"),
        ("DataValue", "1e-2", "pct_1m_invalid"),
    ],
)
def test_official_mapping_units_and_values_are_validated(field, value, classification):
    official = synthetic_response(table="T20807", value="0.3")
    official["BEAAPI"]["Results"]["Data"][0][field] = value
    with pytest.raises(PceSourceError, match=classification):
        parse_pair(synthetic_response(), official)


def test_official_duplicates_and_missing_value_are_rejected():
    official = synthetic_response(table="T20807", value="0.3")
    rows = official["BEAAPI"]["Results"]["Data"]
    rows.append(copy.deepcopy(rows[0]))
    with pytest.raises(PceSourceError, match="duplicate_point"):
        parse_pair(synthetic_response(), official)
    rows.pop()
    del rows[0]["DataValue"]
    with pytest.raises(PceSourceError, match="point_invalid"):
        parse_pair(synthetic_response(), official)


def test_only_real_row_notes_are_stored_with_distinct_table_provenance():
    index = synthetic_response()
    official = synthetic_response(table="T20807", value="0.300")
    for document, note in ((index, "index-note"), (official, "official-note")):
        for row in document["BEAAPI"]["Results"]["Data"]:
            row["LineDescription"] = "Personal consumption expenditures"
        document["BEAAPI"]["Results"]["Data"][0]["NoteRef"] = note
    batch = source(SyntheticTransport(index, official_response=official)).fetch(
        PCE_PROFILE.series_ids, "2026-09", "2026-09"
    )[0]
    points = {p.series_id: p for p in batch.points}
    assert points["DPCERG"].provider_calculations == {
        "bea_pct_1m": "0.3",
        "bea": {"NoteRef": "index-note", "pct_1m": {"NoteRef": "official-note"}},
    }
    assert points["DPCCRG"].provider_calculations == {"bea_pct_1m": "0.3"}
    projection = json.loads(batch.raw)
    assert projection["Results"] == index["BEAAPI"]["Results"]
    assert projection["official_mom"]["Results"] == official["BEAAPI"]["Results"]
    assert parse_response(encode(index), ("DPCERG",), "2026-09", "2026-09")[
        0
    ].provider_calculations == {"bea": {"NoteRef": "index-note"}}
    del index["BEAAPI"]["Results"]["Data"][0]["NoteRef"]
    headline = next(p for p in parse_pair(index, official) if p.series_id == "DPCERG")
    assert headline.provider_calculations == {
        "bea_pct_1m": "0.3",
        "bea": {"pct_1m": {"NoteRef": "official-note"}},
    }


@pytest.mark.parametrize(
    "index_retries,official_retries", [(0, 2), (2, 0), (1, 2), (2, 2)]
)
def test_paired_requests_share_controller_and_sum_all_retries(
    index_retries, official_retries
):
    class Control:
        provider = "BEA"
        active = False
        admissions = 0

        @contextmanager
        def attempt(self):
            assert not self.active
            self.active = True
            self.admissions += 1
            try:
                yield lambda retry_at: None
            finally:
                self.active = False

    control = Control()
    attempts, sleeps = [], []
    retries = {"T20804": index_retries, "T20807": official_retries}

    class Wire(SyntheticTransport):
        def post(self, endpoint, body, headers, **options):
            assert control.active
            table = parse_qs(body.decode())["TableName"][0]
            attempts.append(table)
            if retries[table]:
                retries[table] -= 1
                raise TimeoutError(KEY)
            return super().post(endpoint, body, headers, **options)

    batch = BeaRangeSource(
        credential_loader=lambda: KEY,
        transport=Wire(),
        request_control=control,
        clock=lambda: AT,
        sleep=sleeps.append,
    ).fetch(("DPCERG",), "2026-09", "2026-09")[0]
    assert attempts == ["T20804"] * (1 + index_retries) + ["T20807"] * (
        1 + official_retries
    )
    assert batch.attempts == control.admissions == 2 + index_retries + official_retries
    assert batch.request["request_count"] == 2
    assert sleeps == [1.0, 2.0][:index_retries] + [1.0, 2.0][:official_retries]


@pytest.mark.parametrize("failed_year,earlier_attempts", [(2010, 1), (2015, 3)])
@pytest.mark.parametrize(
    "failure,classification,failed_attempts,status",
    [
        ("timeout", "transport_failure", 3, None),
        ("http", "http_failure", 3, 503),
        ("parse", "invalid_json", 1, None),
        ("mapping", "series_mapping_invalid", 1, None),
        ("provider", "provider_rejected", 1, None),
        ("echo", "credential_echo_forbidden", 1, None),
        ("encoding", "content_encoding_rejected", 1, None),
    ],
)
def test_second_table_failures_include_earlier_windows_without_double_counting(
    failed_year, earlier_attempts, failure, classification, failed_attempts, status
):
    attempts = []

    class Wire(SyntheticTransport):
        def post(self, endpoint, body, headers, **options):
            params = parse_qs(body.decode())
            attempts.append(params)
            if params["TableName"] == ["T20807"] and params["Year"][0].startswith(
                str(failed_year)
            ):
                if failure == "timeout":
                    raise TimeoutError(KEY)
                if failure == "http":
                    return HttpResult(503, b"", {})
                if failure == "parse":
                    return HttpResult(200, b"invalid " + KEY.encode(), {})
                if failure == "encoding":
                    return HttpResult(200, b"", {"Content-Encoding": "gzip"})
                if failure == "provider":
                    return HttpResult(
                        200, encode({"BEAAPI": {"Results": {"Error": KEY}}}), {}
                    )
                document = synthetic_response(
                    (f"{failed_year}-01",), table="T20807", value="0.3"
                )
                if failure == "echo":
                    document["BEAAPI"]["Results"]["Notes"][0]["NoteText"] = KEY
                else:
                    document["BEAAPI"]["Results"]["Data"][0]["SeriesCode"] = "DPCCRGM"
                return HttpResult(200, encode(document), {})
            return super().post(endpoint, body, headers, **options)

    with pytest.raises(PceSourceError, match=classification) as caught:
        source(Wire()).fetch(("DPCERG",), "2010-01", "2015-12")
    assert caught.value.attempts == len(attempts) == earlier_attempts + failed_attempts
    assert caught.value.http_status == status
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("denied_admission", [1, 2, 4])
def test_controller_denial_counts_only_completed_wire_attempts(denied_admission):
    class Control:
        provider = "BEA"
        admissions = 0

        @contextmanager
        def attempt(self):
            self.admissions += 1
            if self.admissions == denied_admission:
                raise PceSourceError("pce_timeseries_request_control_failed")
            yield lambda retry_at: None

    wire = SyntheticTransport()
    client = BeaRangeSource(
        credential_loader=lambda: KEY,
        transport=wire,
        request_control=Control(),
        clock=lambda: AT,
    )
    with pytest.raises(PceSourceError, match="request_control_failed") as caught:
        client.fetch(("DPCERG",), "2010-01", "2015-12")
    assert caught.value.attempts == len(wire.calls) == denied_admission - 1


@pytest.mark.parametrize("failed_year,expected_attempts", [(2010, 2), (2015, 4)])
@pytest.mark.parametrize("retry_after", ["600", "Fri, 30 Oct 2026 00:10:00 GMT"])
def test_second_table_429_is_durable_and_counts_prior_windows(
    tmp_path, failed_year, expected_attempts, retry_after
):
    now, sleeps, attempts = [AT], [], []
    path = tmp_path / "paired-bea-control.json"

    class Wire(SyntheticTransport):
        def post(self, endpoint, body, headers, **options):
            params = parse_qs(body.decode())
            attempts.append(params)
            if params["TableName"] == ["T20807"] and params["Year"][0].startswith(
                str(failed_year)
            ):
                return HttpResult(429, b"", {"Retry-After": retry_after})
            return super().post(endpoint, body, headers, **options)

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += timedelta(seconds=seconds)

    wire = Wire()

    def client():
        return BeaRangeSource(
            credential_loader=lambda: KEY,
            transport=wire,
            clock=lambda: now[0],
            sleep=sleep,
            request_control_path=path,
        )

    with pytest.raises(PceSourceError, match="http_failure") as caught:
        client().fetch(("DPCERG",), "2010-01", "2015-12")
    deadline = caught.value.retry_at
    assert caught.value.http_status == 429
    assert caught.value.attempts == len(attempts) == expected_attempts
    assert deadline == (now[0] if retry_after == "600" else AT) + timedelta(seconds=600)
    assert all(seconds <= 3.1 for seconds in sleeps)
    assert json.loads(path.read_text())["cooldown_until"] == deadline.timestamp()
    with pytest.raises(PceSourceError, match="request_cooldown") as blocked:
        client().fetch(("DPCERG",), "2010-01", "2015-12")
    assert blocked.value.attempts == 0 and blocked.value.retry_at == deadline
    assert len(attempts) == expected_attempts
