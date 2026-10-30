"""Synthetic BEA range contracts; never an upstream qualification."""

import base64
import copy
import json
import socket
import subprocess
import sys
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
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


def synthetic_response(months=("2026-09",), *, value="103.120", key=KEY):
    rows = [
        {
            "TableName": "T20804",
            "LineNumber": line,
            "SeriesCode": series,
            "TimePeriod": month[:4] + "M" + month[-2:],
            "CL_UNIT": "Level",
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
    def __init__(self, response=None):
        self.response, self.calls = response, []

    def post(self, endpoint, body, headers, **options):
        params = parse_qs(body.decode())
        self.calls.append(params)
        document = self.response
        if document is None:
            months = tuple(
                f"{year}-{month:02d}"
                for year in params["Year"][0].split(",")
                for month in range(1, 13)
            )
            document = synthetic_response(months, value="100", key=params["UserID"][0])
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
    assert sum(len(b.points) for b in batches) == 2 * 252
    assert wire.calls[0]["Year"] == ["2005,2006,2007,2008,2009"]
    for batch in batches:
        projection = json.loads(batch.raw)
        assert projection["exact_original_retained"] is False
        assert projection["retention_class"] == "credential_free_bea_results_projection"
        assert KEY.encode() not in batch.raw
        assert b"UserID" not in batch.raw and b"Request" not in batch.raw
        assert "UserID" not in batch.request
        assert batch.observed_at == AT and batch.attempts == 1


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
    assert batch.attempts == 3 and sleeps == [1.0, 2.0]


def test_invalid_binding_and_bounded_plan_precede_credentials():
    with pytest.raises(PceSourceError, match="binding_invalid"):
        BeaRangeSource(request_control=SimpleNamespace())
    assert len(year_windows("2005-10", "2026-09")) == 5
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
