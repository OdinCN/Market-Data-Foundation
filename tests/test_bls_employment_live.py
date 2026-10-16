"""Named source-specific PRE-REQUEST simulation; never external qualification."""

import base64
import json
import os
import pickle
import socket
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from test_bls_employment_vertical import encode, response

from market_data_foundation.canonical.model import digest
from market_data_foundation.historical import bls_employment as employment
from market_data_foundation.historical import bls_employment_live as live
from market_data_foundation.historical.bls_employment import (
    ENDPOINT,
    SERIES_IDS,
    BlsEmploymentRequest,
    parse_response,
)

DUMMY = "0123456789abcdef" * 2
BASE = datetime(2026, 10, 16, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("T03 offline qualification attempted real IO")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(live, "_load_key", forbidden)


class FakeTransport:
    effect_domain = "offline"

    def __init__(self, raw=None, *, status=200, headers=None, advance=None):
        self.raw = encode(response()) if raw is None else raw
        self.status = status
        self.headers = headers or {}
        self.calls = 0
        self.advance = advance

    def post(self, endpoint, body, headers, *, deadline, monotonic):
        self.calls += 1
        assert endpoint == ENDPOINT
        wire = json.loads(body)
        assert wire.pop("registrationkey") == DUMMY
        assert wire == {
            "seriesid": list(SERIES_IDS),
            "startyear": "2025",
            "endyear": "2026",
            "catalog": True,
            "calculations": True,
        }
        assert headers == live._HEADERS
        assert 0 < deadline - monotonic() <= 20
        if self.advance:
            self.advance()
        return live.HttpResponse(self.status, self.raw, self.headers)


def qualify(fake=None, **kwargs):
    return live.qualify_offline(fake or FakeTransport(), dummy_key=DUMMY, **kwargs)


def rejected(fake, code, *, attempts=1, **kwargs):
    with pytest.raises(live.EmploymentAcquisitionError) as failure:
        qualify(fake, **kwargs)
    assert failure.value.classification == code
    assert failure.value.attempts == attempts
    assert DUMMY not in str(failure.value)
    assert fake.calls == attempts
    return failure.value


def limits():
    return live.EmploymentSourceLimits()


def test_t03_named_offline_pre_request_qualification_fixture():
    fake = FakeTransport()
    observation = qualify(fake)
    assert fake.calls == 1
    assert observation.fixture_id == live.FIXTURE_ID
    assert observation.capability == "timeseries.data:CES-CPS-employment"
    assert observation.provider_requests == 0
    assert observation.simulated_attempts == 1
    assert observation.source_qualification == "NOT_ESTABLISHED"
    assert observation.complete and observation.missing_fields == ()
    assert not hasattr(observation, "controlled_external_receipt")
    assert "registrationkey" not in BlsEmploymentRequest().persisted()["body"]


@pytest.mark.parametrize("enabled", [False, None, 0, 1, "true"])
def test_default_closed_before_secret_transport_or_writer(enabled, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("default-closed gate crossed")

    monkeypatch.setattr(live, "_supervise", forbidden)
    with pytest.raises(live.EmploymentAcquisitionError) as failure:
        live.execute_live_candidate(enabled=enabled, limits=limits())
    assert failure.value.classification == live.Classification.DISABLED
    assert failure.value.attempts == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_requests": 0},
        {"provider_requests": 2},
        {"provider_requests": True},
        {"response_bytes": 200001},
        {"network_seconds": 21},
        {"attempt_seconds": 31},
        {"request_bytes": 8192},
        {"pg_connections": 1},
        {"pg_mutations": 1},
        {"garage_requests": 1},
        {"source_binding_activations": 1},
        {"request_identity": "other"},
        {"source_identity": "cpi"},
        {"parser_revision": "other"},
        {"fixture_id": "generic"},
        {"plan_sha256": "c" * 64},
        {"revision": "other"},
        {"contract_revision": "other"},
    ],
)
def test_exact_budget_and_identity_required_before_io(changes, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid limits crossed effect gate")

    monkeypatch.setattr(live, "_supervise", forbidden)
    with pytest.raises(live.EmploymentAcquisitionError) as failure:
        live.execute_live_candidate(enabled=True, limits=replace(limits(), **changes))
    assert failure.value.classification == live.Classification.LIMITS
    assert failure.value.attempts == 0


@pytest.mark.parametrize(
    "status,code,attempts",
    [
        (301, live.Classification.REDIRECT, 1),
        (307, live.Classification.REDIRECT, 1),
        (401, live.Classification.HTTP, 1),
        (429, live.Classification.HTTP, 3),
        (503, live.Classification.HTTP, 3),
    ],
)
def test_http_retry_policy_has_no_redirect_or_fallback(status, code, attempts):
    failure = rejected(
        FakeTransport(status=status),
        code,
        attempts=attempts,
        sleep=lambda _seconds: None,
    )
    assert failure.http_status == status


def test_transport_failure_then_success_uses_two_attempts():
    class Flaky(FakeTransport):
        def post(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("temporary")
            return live.HttpResponse(200, self.raw, self.headers)

    waits = []
    observation = qualify(Flaky(), sleep=waits.append)
    assert observation.complete
    assert waits == [1.0]


def test_503_503_200_uses_three_attempts_and_stops():
    class Flaky(FakeTransport):
        def post(self, *args, **kwargs):
            self.calls += 1
            status = 503 if self.calls < 3 else 200
            return live.HttpResponse(status, self.raw, self.headers)

    transport = Flaky()
    waits = []
    observation = qualify(transport, sleep=waits.append)
    assert observation.complete
    assert transport.calls == 3
    assert waits == [1.0, 2.0]


@pytest.mark.parametrize(
    "headers,code",
    [
        ({"content-length": "200001"}, live.Classification.SIZE),
        ({"content-length": "0"}, live.Classification.SIZE),
        ({"content-length": "invalid"}, live.Classification.HEADERS),
        ({"content-length": "-1"}, live.Classification.HEADERS),
        ({"content-encoding": "gzip"}, live.Classification.HEADERS),
        ({"transfer-encoding": "chunked"}, live.Classification.HEADERS),
    ],
)
def test_untrusted_headers_and_false_lengths(headers, code):
    rejected(FakeTransport(headers=headers), code)


def test_overflow_sentinel_even_with_deceptive_content_length():
    rejected(
        FakeTransport(b" " * 200001, headers={"content-length": "1"}),
        live.Classification.SIZE,
    )
    raw = encode(response())
    # Exact boundary accepted; bytes are preserved, including trailing whitespace.
    padded = raw + b" " * (200000 - len(raw))
    assert qualify(FakeTransport(padded, headers={"content-length": "200000"})).complete
    rejected(FakeTransport(b""), live.Classification.SIZE)


@pytest.mark.parametrize(
    "mode",
    [
        "literal",
        "unicode",
        "url",
        "nested",
        "nested-url",
        "sensitive",
        "escaped-name",
        "url-name",
        "deep",
    ],
)
def test_echo_traps_before_digest_report_or_artifact(mode, monkeypatch):
    value = response()
    if mode == "literal":
        value["message"] = [DUMMY]
    elif mode == "unicode":
        raw = encode(value).replace(
            b'"message":[]',
            (
                '"message":["' + "".join("\\u%04x" % ord(c) for c in DUMMY) + '"]'
            ).encode(),
        )
    elif mode == "url":
        value["message"] = ["".join("%%%02x" % ord(c) for c in DUMMY)]
    elif mode == "nested":
        value["Results"]["nested"] = {"value": [DUMMY]}
    elif mode == "nested-url":
        value["message"] = [
            json.dumps({"nested": "".join("%%%02x" % ord(c) for c in DUMMY)})
        ]
    elif mode == "sensitive":
        value["Results"]["api_key"] = "unrelated"
    elif mode == "escaped-name":
        raw = encode(value).replace(
            b'"message":[]', b'"regi\\u0073trationkey":"unrelated"'
        )
    elif mode == "url-name":
        value["Results"]["%72egistration%6bey"] = "unrelated"
    else:
        value["message"] = ["%" + "25" * 9 + "30"]
    if mode not in {"unicode", "escaped-name"}:
        raw = encode(value)

    def forbidden(*args, **kwargs):
        pytest.fail("unscreened response reached digest/parser/artifact")

    monkeypatch.setattr(live, "parse_response", forbidden)
    monkeypatch.setattr(live, "_write_artifacts", forbidden)
    rejected(FakeTransport(raw), live.Classification.ECHO)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_series",
        "extra_series",
        "missing_field",
        "catalog",
        "sa",
        "survey",
        "units",
        "population",
        "nfp_calculation",
        "ahe_missing_calculation",
        "operand",
    ],
)
def test_source_specific_semantic_failures(mutation):
    value = response()
    series = value["Results"]["series"]
    if mutation == "missing_series":
        series.pop()
    elif mutation == "extra_series":
        series.append(series[0])
    elif mutation == "missing_field":
        del value["responseTime"]
    elif mutation == "catalog":
        del series[0]["catalog"]
    elif mutation == "sa":
        series[0]["catalog"]["seasonality"] = "Not Seasonally Adjusted"
    elif mutation == "survey":
        series[1]["catalog"]["survey_abbreviation"] = "CE"
    elif mutation == "units":
        series[2]["catalog"]["measure_data_type"] = "dollars"
    elif mutation == "population":
        series[1]["catalog"]["demographic_gender"] = "Men"
    elif mutation == "nfp_calculation":
        series[0]["data"][0]["calculations"]["net_changes"]["1"] = "11"
    elif mutation == "ahe_missing_calculation":
        del series[2]["data"][0]["calculations"]["pct_changes"]["12"]
    else:
        series[2]["data"].pop()
    rejected(FakeTransport(encode(value)), live.Classification.CONTRACT)


def test_ces_cps_and_returned_ahe_not_rounded_level_derivation():
    raw = encode(response())
    assert qualify(FakeTransport(raw)).complete
    measurements = parse_response(raw).data["measurements"]["2026-09"]
    assert measurements["unemployment_rate_sa"]["survey"] == "CPS"
    assert measurements["nfp_change_sa"]["survey"] == "CES"
    assert measurements["nfp_change_sa"]["unit"] == "thousand_jobs"
    assert measurements["ahe_mom_sa"]["value"] == "0.7"
    assert measurements["ahe_mom_sa"]["operands"][0]["unit"] == "usd_per_hour"
    assert measurements["ahe_yoy_sa"]["value"] == "3.2"


def test_injected_monotonic_slow_drip_cannot_extend_network_budget():
    at = [100.0]

    def drip():
        # Every individual read would satisfy an inactivity timeout.
        for _ in range(21):
            at[0] += 1

    rejected(
        FakeTransport(advance=drip),
        live.Classification.TIMEOUT,
        attempts=3,
        monotonic=lambda: at[0],
        sleep=lambda _seconds: None,
    )


def test_timeout_and_raw_exception_are_classified_without_messages():
    class Failure(FakeTransport):
        def post(self, *args, **kwargs):
            self.calls += 1
            raise ConnectionError("provider message " + DUMMY)

    failure = rejected(
        Failure(),
        live.Classification.TRANSPORT,
        attempts=3,
        sleep=lambda _seconds: None,
    )
    assert failure.__cause__ is None
    assert str(failure) == "transport_failure_exhausted"


def _drip_until_killed(marker):
    # No network: verifies the production supervisor, including blocked IO.
    while True:
        with open(marker, "ab") as file:
            file.write(b".")
        time.sleep(0.005)


@pytest.mark.parametrize(
    "code", [live.Classification.TIMEOUT, live.Classification.ATTEMPT_TIMEOUT]
)
def test_process_supervisor_kills_slow_drip_and_never_restarts(tmp_path, code):
    marker = tmp_path / "drip"
    started = time.monotonic()
    with pytest.raises(live.EmploymentAcquisitionError) as failure:
        live._supervise(
            lambda: _drip_until_killed(marker),
            deadline=started + 0.15,
            monotonic=time.monotonic,
            timeout_code=code,
        )
    assert failure.value.classification == code
    assert time.monotonic() - started < 1
    size = marker.stat().st_size
    time.sleep(0.03)
    assert marker.stat().st_size == size


def test_transport_reads_only_overflow_sentinel_and_closes(monkeypatch):
    calls = []

    class Response:
        status = 200

        def getheaders(self):
            return [("Content-Length", "1")]

        def read(self, amount):
            calls.append(amount)
            return b" " * amount

    class Connection:
        def __init__(self, host, timeout):
            assert host == "api.bls.gov"
            assert 0 < timeout <= 20

        def request(self, method, path, body, headers):
            calls.append((method, path))

        def getresponse(self):
            return Response()

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(live.http.client, "HTTPSConnection", Connection)
    with pytest.raises(live.EmploymentAcquisitionError) as failure:
        live._exchange(live.EmploymentHttpsTransport(), DUMMY, time.monotonic)
    assert failure.value.classification == live.Classification.SIZE
    assert calls == [("POST", "/publicAPI/v2/timeseries/data/"), 200001, "closed"]


def test_writer_gates_screening_original_bytes_readonly_and_no_overwrite(
    tmp_path, monkeypatch
):
    # All IO below is local simulation under a temporary ignored artifacts root.
    monkeypatch.setattr(live, "REPOSITORY", tmp_path)
    monkeypatch.setattr(live, "ARTIFACT_ROOT", tmp_path / "artifacts/t03")
    import subprocess

    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("/artifacts/\n")
    raw = b"  " + encode(response()) + b"\n"
    files = {
        "raw-response.bin": raw,
        "request.json": b"{}",
        "source-envelope.json": b"{}",
        "observation.json": b"{}",
    }
    args = dict(limits=limits(), run_token="c" * 32)
    with pytest.raises(live.EmploymentAcquisitionError):
        live._write_artifacts(files, DUMMY, enabled=False, **args)
    assert not live.ARTIFACT_ROOT.exists()
    bad = dict(files, **{"source-envelope.json": encode({"nested": DUMMY})})
    with pytest.raises(live.EmploymentAcquisitionError) as failure:
        live._write_artifacts(bad, DUMMY, enabled=True, **args)
    assert failure.value.classification == live.Classification.ECHO
    assert not live.ARTIFACT_ROOT.exists()
    directory = live._write_artifacts(files, DUMMY, enabled=True, **args)
    assert (directory / "raw-response.bin").read_bytes() == raw
    assert digest((directory / "raw-response.bin").read_bytes()) == digest(raw)
    assert (directory / "raw-response.bin").stat().st_mode & 0o222 == 0
    with pytest.raises(live.EmploymentAcquisitionError) as failure:
        live._write_artifacts(files, DUMMY, enabled=True, **args)
    assert failure.value.classification == live.Classification.ARTIFACT
    assert (directory / "raw-response.bin").read_bytes() == raw
    os.chmod(directory, 0o700)  # permit pytest temporary cleanup


def test_future_attempt_fake_pipeline_retains_unqualified_candidate(
    tmp_path, monkeypatch
):
    # Internal future path simulation only; no real opt-in or secret file is used.
    monkeypatch.setattr(live, "_load_key", lambda _: DUMMY)
    monkeypatch.setattr(live, "_supervise", lambda operation, **_: operation())
    saved = {}

    def writer(files, key, **kwargs):
        saved.update(files)
        assert kwargs["enabled"] is True
        assert kwargs["limits"] == limits()
        assert key == DUMMY
        return tmp_path

    monkeypatch.setattr(live, "_write_artifacts", writer)
    instants = iter(BASE + timedelta(milliseconds=i) for i in range(4))
    result = live._attempt(
        limits(),
        FakeTransport(),
        tmp_path / "unused",
        lambda: next(instants),
        time.monotonic,
    )
    item = result.candidate
    assert item.evidence.raw_bytes == saved["raw-response.bin"]
    assert item.evidence.requested_at <= item.evidence.obtained_at
    assert (
        item.evidence.obtained_at
        <= item.plan.run.finished_at
        <= item.plan.version.frozen_at
    )
    assert result.observation.source_qualification == "NOT_ESTABLISHED"
    receipt = json.loads(saved["observation.json"])
    assert receipt["publication_known_at"] is None
    assert receipt["consumer_usable_at"] is None
    assert receipt["source_known_at"] == item.evidence.obtained_at.isoformat()
    assert receipt["raw_sha256"] == digest(saved["raw-response.bin"])
    assert all(DUMMY.encode() not in content for content in saved.values())
    assert item.plan.raw_material.durable is False


def test_offline_entry_rejects_live_transport_before_effects():
    with pytest.raises(live.EmploymentAcquisitionError) as failure:
        qualify(live.EmploymentHttpsTransport())
    assert failure.value.classification == live.Classification.DISABLED
    assert failure.value.attempts == 0


def test_failure_hides_raw_exception_traceback_context():
    class Timeout(FakeTransport):
        def post(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError("sensitive provider message " + DUMMY)

    failure = rejected(
        Timeout(),
        live.Classification.TIMEOUT,
        attempts=3,
        sleep=lambda _seconds: None,
    )
    assert failure.__suppress_context__


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Encoding": "gzip"},
        {"Content-Length": "bad"},
        {"Content-Length": "1", "content-length": "1"},
        {"content-length": 1},
    ],
)
def test_header_normalization_and_duplicates(headers):
    rejected(FakeTransport(headers=headers), live.Classification.HEADERS)


def _artifact_rig(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(live, "REPOSITORY", tmp_path)
    monkeypatch.setattr(live, "ARTIFACT_ROOT", tmp_path / "artifacts/t03")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("/artifacts/\n")
    files = {
        "raw-response.bin": encode(response()),
        "request.json": b"{}",
        "source-envelope.json": b"{}",
        "observation.json": b"{}",
    }
    return files, dict(enabled=True, limits=limits(), run_token="d" * 32)


def test_writer_requires_ignore_and_forbids_symlink(tmp_path, monkeypatch):
    files, args = _artifact_rig(tmp_path, monkeypatch)
    (tmp_path / ".gitignore").unlink()
    with pytest.raises(live.EmploymentAcquisitionError) as failure:
        live._write_artifacts(files, DUMMY, **args)
    assert failure.value.classification == live.Classification.ARTIFACT
    assert not live.ARTIFACT_ROOT.exists()
    (tmp_path / ".gitignore").write_text("/artifacts/\n")
    other = tmp_path / "other"
    other.mkdir()
    (tmp_path / "artifacts").symlink_to(other, target_is_directory=True)
    with pytest.raises(live.EmploymentAcquisitionError):
        live._write_artifacts(files, DUMMY, **args)
    assert list(other.iterdir()) == []


def test_partial_writer_stops_without_receipt_retry_or_cleanup(tmp_path, monkeypatch):
    files, args = _artifact_rig(tmp_path, monkeypatch)
    original_open = live.os.open
    writes = []

    def fail_open(path, flags, mode=0o777, **kwargs):
        if flags & os.O_CREAT:
            writes.append(str(path))
            if len(writes) == 2:
                raise OSError("sensitive IO message " + DUMMY)
        return original_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(live.os, "open", fail_open)
    with pytest.raises(live.EmploymentAcquisitionError) as failure:
        live._write_artifacts(files, DUMMY, **args)
    assert failure.value.classification == live.Classification.ARTIFACT
    assert failure.value.__suppress_context__
    directory = live.ARTIFACT_ROOT / ("run-" + "d" * 32)
    assert [p.name for p in directory.iterdir()] == ["raw-response.bin"]
    assert writes == ["raw-response.bin", "request.json"]
    assert not (directory / "observation.json").exists()


@pytest.mark.parametrize("offsets", [(1, 0, 2, 3), (0, 2, 1, 3), (0, 1, 3, 2)])
def test_future_fake_pipeline_rejects_inverted_time_before_writer(
    offsets, tmp_path, monkeypatch
):
    monkeypatch.setattr(live, "_load_key", lambda _: DUMMY)
    monkeypatch.setattr(live, "_supervise", lambda operation, **_: operation())

    def forbidden(*args, **kwargs):
        pytest.fail("inverted time reached persistence")

    monkeypatch.setattr(live, "_write_artifacts", forbidden)
    instants = iter(BASE + timedelta(seconds=i) for i in offsets)
    with pytest.raises(live.EmploymentAcquisitionError) as failure:
        live._attempt(
            limits(),
            FakeTransport(),
            tmp_path,
            lambda: next(instants),
            time.monotonic,
        )
    assert failure.value.classification in {
        live.Classification.TIME,
        live.Classification.CONTRACT,
    }


def test_full_supervised_future_fake_pipeline_only(tmp_path, monkeypatch):
    _artifact_rig(tmp_path, monkeypatch)
    # Real supervisors and IPC, dummy key, fake transport, local writer capture.
    monkeypatch.setattr(live, "_load_key", lambda _: DUMMY)

    def writer(files, key, **kwargs):
        assert key == DUMMY
        for name, content in files.items():
            (tmp_path / name).write_bytes(content)
        return tmp_path

    monkeypatch.setattr(live, "_write_artifacts", writer)
    result = live.execute_live_candidate(
        enabled=True, limits=limits(), transport=FakeTransport()
    )
    assert (
        result.candidate.evidence.raw_bytes
        == (tmp_path / "raw-response.bin").read_bytes()
    )
    assert result.observation.source_qualification == "NOT_ESTABLISHED"
    assert result.observation.provider_requests == 1  # simulated future receipt only
    assert json.loads((tmp_path / "observation.json").read_bytes())[
        "receipt_state"
    ].startswith("RETAINED_UNQUALIFIED")


def test_real_transport_ignores_duplicate_irrelevant_headers(monkeypatch):
    raw = encode(response())
    closed = []

    class Response:
        status = 200

        def getheaders(self):
            return [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(raw))),
                ("Server-Timing", "edge;dur=1"),
                ("server-timing", "cache;dur=2"),
                ("Vary", "Accept-Encoding"),
                ("vary", "Origin"),
            ]

        def read(self, amount):
            assert amount == live.MAX_RESPONSE_BYTES + 1
            return raw

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            closed.append(True)

    monkeypatch.setattr(live.http.client, "HTTPSConnection", Connection)
    result = live._exchange(
        live.EmploymentHttpsTransport(),
        DUMMY,
        time.monotonic,
        sleep=lambda _seconds: None,
    )
    assert result.status == 200
    assert result.body == raw
    assert result.transport_attempts == 1
    assert result.headers == {"content-length": str(len(raw))}
    assert closed == [True]


@pytest.mark.parametrize(
    "headers,status,code",
    [
        ([("Content-Length", "200001")], 200, live.Classification.SIZE),
        (
            [("Content-Length", "1"), ("content-length", "1")],
            200,
            live.Classification.HEADERS,
        ),
        ([], 302, live.Classification.REDIRECT),
        ([], 503, live.Classification.HTTP),
    ],
)
def test_real_transport_rejects_headers_before_body_read(
    headers, status, code, monkeypatch
):
    closed = []

    class Response:
        def getheaders(self):
            return headers

        def read(self, amount):
            pytest.fail("rejected headers reached response body read")

    Response.status = status

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            closed.append(True)

    monkeypatch.setattr(live.http.client, "HTTPSConnection", Connection)
    with pytest.raises(live.EmploymentAcquisitionError) as failure:
        live._exchange(live.EmploymentHttpsTransport(), DUMMY, time.monotonic)
    assert failure.value.classification == code
    expected_closes = 3 if status in {429, 500, 502, 503, 504} else 1
    assert closed == [True] * expected_closes


def test_safe_failure_observation_has_no_response_or_raw_exception():
    failed = rejected(
        FakeTransport(status=503),
        live.Classification.HTTP,
        attempts=3,
        sleep=lambda _seconds: None,
    )
    observation = failed.safe_observation()
    assert observation["fixture_id"] == live.FIXTURE_ID
    assert observation["transport_attempts"] == 3
    assert observation["source_qualification"] == "NOT_ESTABLISHED"
    assert observation["exception"]["type"] == "EmploymentAcquisitionError"
    assert observation["max_transport_attempts"] == 3
    assert observation["retry_exhausted"] is True
    assert "retry_allowed" not in observation
    assert DUMMY not in json.dumps(observation)
    assert "raw_sha256" not in observation


def test_outer_cancellation_kills_nested_network_worker(tmp_path):
    marker = tmp_path / "nested-drip"

    def nested():
        return live._supervise(
            lambda: _drip_until_killed(marker),
            deadline=time.monotonic() + 3,
            monotonic=time.monotonic,
            timeout_code=live.Classification.TIMEOUT,
        )

    with pytest.raises(live.EmploymentAcquisitionError) as failure:
        live._supervise(
            nested,
            deadline=time.monotonic() + 0.2,
            monotonic=time.monotonic,
            timeout_code=live.Classification.ATTEMPT_TIMEOUT,
        )
    assert failure.value.classification == live.Classification.ATTEMPT_TIMEOUT
    time.sleep(0.03)
    size = marker.stat().st_size
    time.sleep(0.03)
    assert marker.stat().st_size == size


def test_real_transport_empty_early_eof_retries_three_times(monkeypatch):
    connections = []

    class Response:
        status = 200

        def getheaders(self):
            return [("Content-Type", "application/json"), ("Content-Length", "20")]

        def read(self, amount):
            assert amount == employment.MAX_RESPONSE_BYTES + 1
            return b""

    class Connection:
        def __init__(self, host, **kwargs):
            assert host == "api.bls.gov"
            connections.append(self)

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(live.http.client, "HTTPSConnection", Connection)
    waits = []
    with pytest.raises(live.EmploymentAcquisitionError) as caught:
        live._exchange(
            live.EmploymentHttpsTransport(),
            DUMMY,
            time.monotonic,
            sleep=waits.append,
        )
    assert caught.value.classification == live.Classification.TRANSPORT
    assert caught.value.attempts == 3
    assert len(connections) == 3
    assert waits == [1.0, 2.0]


def test_real_transport_short_body_retries_three_times(monkeypatch):
    connections = []

    class Response:
        status = 200

        def getheaders(self):
            return [("Content-Type", "application/json"), ("Content-Length", "20")]

        def read(self, amount):
            assert amount == employment.MAX_RESPONSE_BYTES + 1
            return b"{}"

    class Connection:
        def __init__(self, host, **kwargs):
            assert host == "api.bls.gov"
            connections.append(self)

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(live.http.client, "HTTPSConnection", Connection)
    waits = []
    with pytest.raises(live.EmploymentAcquisitionError) as caught:
        live._exchange(
            live.EmploymentHttpsTransport(),
            DUMMY,
            time.monotonic,
            sleep=waits.append,
        )
    assert caught.value.classification == live.Classification.TRANSPORT
    assert caught.value.attempts == 3
    assert len(connections) == 3
    assert waits == [1.0, 2.0]


def test_partial_ipc_transfer_still_obeys_deadline(monkeypatch):
    def partial(channel, parent_pid, operation, *args):
        # Simulate a writer blocked after starting a frame; no external IO.
        live.os.write(channel, live.struct.pack("!I", 1000) + b"partial")
        time.sleep(3)

    monkeypatch.setattr(live, "_child", partial)
    started = time.monotonic()
    with pytest.raises(live.EmploymentAcquisitionError) as failure:
        live._supervise(
            lambda: None,
            deadline=started + 0.1,
            monotonic=time.monotonic,
            timeout_code=live.Classification.TIMEOUT,
        )
    assert failure.value.classification == live.Classification.TIMEOUT
    assert time.monotonic() - started < 1


@pytest.mark.parametrize(
    "encoding",
    [
        "utf-16",
        "utf-16-le",
        "utf-16-be",
        "utf-32",
        "utf-32-le",
        "utf-32-be",
        "utf-8-sig",
    ],
)
def test_correction_strict_original_encoding_before_parser_digest_artifact(
    encoding, monkeypatch
):
    value = response()
    value["message"] = [{"nested": DUMMY}]
    raw = encode(value).decode().encode(encoding)
    _reject_before_any_payload(raw, monkeypatch)


def _reject_before_any_payload(raw, monkeypatch):
    # The fixed credential-free request identity is not a response payload hash.
    source_limits = limits()
    saved = []

    def forbidden(*args, **kwargs):
        saved.append(True)
        pytest.fail("rejected original reached parser/digest/artifact")

    monkeypatch.setattr(live, "parse_response", forbidden)
    monkeypatch.setattr(employment, "digest", forbidden)
    monkeypatch.setattr(live, "EmploymentEvidence", forbidden)
    monkeypatch.setattr(live, "build_plan", forbidden)
    monkeypatch.setattr(live, "EmploymentRetainedCandidate", forbidden)
    monkeypatch.setattr(live, "EmploymentObservation", forbidden)
    monkeypatch.setattr(live, "_write_artifacts", forbidden)
    rejected(FakeTransport(raw), live.Classification.ECHO)
    monkeypatch.setattr(live, "_load_key", lambda _: DUMMY)
    monkeypatch.setattr(live, "_supervise", lambda operation, **_: operation())
    with pytest.raises(live.EmploymentAcquisitionError) as caught:
        live._attempt(
            source_limits,
            FakeTransport(raw),
            live.SECRET_PATH,
            lambda: BASE,
            time.monotonic,
        )
    assert caught.value.classification == live.Classification.ECHO
    assert saved == []


def _encoded_text(value, kind):
    if kind == "url":
        return "".join("%%%02x" % ord(c) for c in value)
    if kind == "json":
        return "".join("\\u%04x" % ord(c) for c in value)
    if kind == "html":
        return "".join("&#x%x;" % ord(c) for c in value)
    if kind == "hex":
        return value.encode().hex()
    if kind == "unicode":
        return "".join(
            chr(ord(c) + 0xFEE0) if 33 <= ord(c) <= 126 else c for c in value
        )
    if kind == "base64url":
        return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
    return base64.b64encode(value.encode()).decode()


@pytest.mark.parametrize(
    "kind", ["url", "json", "html", "base64", "base64url", "hex", "unicode"]
)
@pytest.mark.parametrize(
    "context", ["0x", "reply=", "sentence", "attached", "nested", "spaced"]
)
@pytest.mark.parametrize("location", ["message", "nested-value", "nested-name"])
def test_correction002_encoded_contexts_stop_before_all_payload_boundaries(
    kind, context, location, monkeypatch
):
    body = _encoded_text(DUMMY, kind)
    if context == "sentence":
        body = "The service returned " + body + " for this response."
    elif context == "attached":
        body = "prefix" + body + "suffix"
    elif context == "nested":
        body = _encoded_text("reply=" + _encoded_text("0x" + body, "hex"), "base64")
    elif context == "spaced":
        body = "The service returned reply=" + " \t".join(body)
    else:
        body = context + body
    value = response()
    if location == "message":
        value["message"] = [body]
    elif location == "nested-value":
        value["Results"]["extra"] = [{"reply": {"body": body}}]
    else:
        value["Results"]["extra"] = [{body: "ordinary response"}]
    _reject_before_any_payload(encode(value), monkeypatch)


@pytest.mark.parametrize(
    "prefix",
    ["a", "ab", "abc", "abcd", "reply=", "0x", "reply==", "reply=0x", "reply:"],
)
@pytest.mark.parametrize("kind", ["hex", "base64", "base64url"])
def test_correction002_prefix_alignment_binary_noise_and_recursive_encoding(
    prefix, kind, monkeypatch
):
    # Non-UTF8 surrounding bytes cannot conceal an inner text encoding.
    inner = prefix + _encoded_text(DUMMY, kind) + " done."
    binary = b"\xff\xfe\x80" + inner.encode() + b"\xff"
    value = response()
    value["message"] = [prefix + base64.b64encode(binary).decode()]
    _reject_before_any_payload(encode(value), monkeypatch)


@pytest.mark.parametrize(
    "limit", ["SCREEN_WORK_UNITS", "SCREEN_VARIANTS", "SCREEN_DEPTH"]
)
def test_correction002_screening_budget_fails_closed_before_payloads(
    limit, monkeypatch
):
    monkeypatch.setattr(live, limit, 1)
    value = response()
    value["message"] = ["reply=" + _encoded_text(DUMMY, "base64")]
    _reject_before_any_payload(encode(value), monkeypatch)


@pytest.mark.parametrize("sample", [0, 1, 2])
@pytest.mark.parametrize("kind", ["hex", "base64", "base64url"])
@pytest.mark.parametrize("context", ["reply=", "0x", "sentence", "nested"])
def test_correction002_nonrepeating_dummy_values(sample, kind, context, monkeypatch):
    # Generated synthetic hex, unrelated to owner-local material.
    dummy = (
        "fedcba98765432100123456789abcdef",
        "a5c82e7b163904dfb62031ac9e7854d0",
        "11112222333344445555666677778888",
    )[sample]
    monkeypatch.setitem(globals(), "DUMMY", dummy)
    body = _encoded_text(dummy, kind)
    if context == "sentence":
        body = "The reply contained " + body + ". End of reply."
    elif context == "nested":
        body = _encoded_text("reply=0x" + _encoded_text(body, "hex"), "base64")
    else:
        body = context + body
    value = response()
    value["message"] = [body]
    _reject_before_any_payload(encode(value), monkeypatch)


@pytest.mark.parametrize("kind", ["url", "json", "html", "base64", "hex"])
def test_correction002_unresolved_deep_encoding_fails_closed(kind, monkeypatch):
    body = "An ordinary but deeply wrapped reply."
    if kind == "url":
        body = "%" + "25" * 12 + "41"
    elif kind == "json":
        body = "\\u0041"
        for _ in range(10):
            body = body.replace("\\", "\\u005c")
    elif kind == "html":
        body = "&" + "amp;" * 12 + "#65;"
    else:
        for _ in range(10):
            body = _encoded_text(body, kind)
    value = response()
    value["message"] = ["reply=" + body]
    _reject_before_any_payload(encode(value), monkeypatch)


@pytest.mark.parametrize(
    "kind", ["url", "json", "html", "base64", "base64url", "hex", "unicode"]
)
@pytest.mark.parametrize("sample", range(16))
def test_correction002_legitimate_encoded_contexts_retain_exact_ces_cps_nfp_ahe(
    kind, sample, tmp_path, monkeypatch
):
    value = response()
    ordinary = "CES payroll levels; CPS unemployment; NFP change; AHE earnings."
    value["message"] = ["The response is reply=" + _encoded_text(ordinary, kind)]
    raw = encode(value)
    assert qualify(FakeTransport(raw)).complete
    expected = parse_response(encode(response()))
    actual = parse_response(raw)
    assert actual.data == expected.data
    monkeypatch.setattr(live, "_load_key", lambda _: DUMMY)
    monkeypatch.setattr(live, "_supervise", lambda operation, **_: operation())
    saved = {}

    def writer(files, key, **kwargs):
        for content in files.values():
            live._screen(content, key)
        saved.update(files)
        return tmp_path

    monkeypatch.setattr(live, "_write_artifacts", writer)
    result = live._attempt(
        limits(),
        FakeTransport(raw),
        live.SECRET_PATH,
        lambda: BASE,
        time.monotonic,
        attempt_id=digest(f"synthetic-attempt-{sample}".encode())[:32],
    )
    assert result.candidate.evidence.raw_bytes == saved["raw-response.bin"] == raw
    assert set(saved) == {
        "raw-response.bin",
        "request.json",
        "source-envelope.json",
        "observation.json",
    }
    assert json.loads(saved["observation.json"])["raw_sha256"] == digest(raw)
    assert result.observation.source_qualification == "NOT_ESTABLISHED"


@pytest.mark.parametrize(
    "body", ["reply=%ff%fe", "reply=%u0030", "reply=\\uGGGG", "reply=\\x30\\x31"]
)
def test_correction002_unreliable_escape_text_fails_closed(body, monkeypatch):
    value = response()
    value["message"] = [body]
    _reject_before_any_payload(encode(value), monkeypatch)


@pytest.mark.parametrize(
    "encoding",
    [
        "html-decimal",
        "html-hex",
        "html-nested",
        "base64",
        "base64url",
        "hex",
        "base64-hex",
        "hex-base64",
        "base64-utf16",
        "base64-spaced",
        "hex-spaced",
        "json-url-base64",
        "nul",
        "unknown-escape",
        "deep-url",
    ],
)
def test_correction_encoded_echo_in_provider_nested_message(encoding, monkeypatch):
    import urllib.parse

    forms = {
        "html-decimal": "".join("&#%d;" % ord(c) for c in DUMMY),
        "html-hex": "".join("&#x%x;" % ord(c) for c in DUMMY),
        "html-nested": "".join("&amp;#%d;" % ord(c) for c in DUMMY),
        "base64": base64.b64encode(DUMMY.encode()).decode(),
        "base64url": base64.urlsafe_b64encode(DUMMY.encode()).decode().rstrip("="),
        "hex": DUMMY.encode().hex(),
        "base64-hex": base64.b64encode(DUMMY.encode().hex().encode()).decode(),
        "hex-base64": base64.b64encode(DUMMY.encode()).hex(),
        "base64-utf16": base64.b64encode(DUMMY.encode("utf-16")).decode(),
        "base64-spaced": " ".join(base64.b64encode(DUMMY.encode()).decode()),
        "hex-spaced": " ".join(DUMMY.encode().hex()),
        "nul": "\x00".join(DUMMY),
        "unknown-escape": "".join("\\x%02x" % ord(c) for c in DUMMY),
        "deep-url": "%" + "25" * 12 + "30",
    }
    forms["json-url-base64"] = json.dumps(
        {"body": urllib.parse.quote(forms["base64"], safe="")}
    )
    value = response()
    value["Results"]["nested"] = {"messages": [{"body": forms[encoding]}]}
    _reject_before_any_payload(encode(value), monkeypatch)


def _reduce_marker(path):
    # If a parent ever unpickles this, record its PID. The callback must not run.
    with open(path, "w") as output:
        output.write(str(os.getpid()))
    return 200


class MaliciousField:
    def __init__(self, path):
        self.path = str(path)

    def __reduce__(self):
        return _reduce_marker, (self.path,)


@pytest.mark.parametrize("field", ["classification", "attempts", "http_status"])
@pytest.mark.parametrize("route", ["transport", "child-operation"])
def test_correction_callback_error_fields_never_executable_or_secret(
    field, route, tmp_path
):
    marker = tmp_path / "executed"

    class Attack(FakeTransport):
        def post(self, *args, **kwargs):
            error = live.EmploymentAcquisitionError(live.Classification.HTTP, 1, 503)
            setattr(error, field, MaliciousField(marker))
            error.args = (DUMMY,)
            raise error

    operation = (
        (
            lambda: live._exchange(
                Attack(),
                DUMMY,
                time.monotonic,
                sleep=lambda _seconds: None,
            )
        )
        if route == "transport"
        else (lambda: Attack().post())
    )
    with pytest.raises(live.EmploymentAcquisitionError) as failed:
        live._supervise(
            operation,
            deadline=time.monotonic() + 2,
            monotonic=time.monotonic,
            timeout_code=live.Classification.TIMEOUT,
            attempt_id="e" * 32,
        )
    assert failed.value.classification == live.Classification.TRANSPORT
    assert failed.value.attempt_id == "e" * 32
    observation = failed.value.safe_observation()
    assert DUMMY not in json.dumps(observation) and len(json.dumps(observation)) < 3000
    assert not marker.exists()


@pytest.mark.parametrize(
    "change",
    [
        {"http_status": DUMMY},
        {"http_status": True},
        {"http_status": 99},
        {"http_status": 600},
        {"attempts": True},
        {"attempts": 4},
        {"code": DUMMY},
        {"code": "x" * 10000},
        {"extra": DUMMY},
    ],
)
def test_correction_json_error_exact_types_keys_and_bounds(change):
    error = {"code": live.Classification.HTTP.value, "attempts": 1, "http_status": 503}
    error.update(change)
    envelope = {"v": 1, "attempt_id": "e" * 32, "ok": False, "result": error}
    with pytest.raises((ValueError, TypeError)):
        live._decode_envelope(encode(envelope), "e" * 32, "response")


def test_correction_json_roundtrip_and_fixed_safe_error():
    raw = encode(response())
    payload = live._encode_envelope(
        lambda: live.HttpResponse(200, raw), "e" * 32, "response"
    )
    assert live._decode_envelope(payload, "e" * 32, "response").body == raw
    assert len(payload) <= live.IPC_BYTES
    error = live.EmploymentAcquisitionError(live.Classification.HTTP, 1, DUMMY)
    assert error.safe_observation()["http_status"] is None

    def failure():
        raise RuntimeError(DUMMY)

    payload = live._encode_envelope(failure, "e" * 32, "response")
    assert DUMMY.encode() not in payload
    with pytest.raises(live.EmploymentAcquisitionError) as caught:
        live._decode_envelope(payload, "e" * 32, "response")
    assert caught.value.classification == live.Classification.TRANSPORT


def test_correction_pickle_frame_is_never_loaded(tmp_path, monkeypatch):
    marker = tmp_path / "unpickled"
    payload = pickle.dumps(MaliciousField(marker))

    def hostile(channel, *args):
        os.write(channel, live.struct.pack("!I", len(payload)) + payload)
        os.close(channel)

    monkeypatch.setattr(live, "_child", hostile)
    with pytest.raises(live.EmploymentAcquisitionError) as caught:
        live._supervise(
            lambda: None,
            deadline=time.monotonic() + 1,
            monotonic=time.monotonic,
            timeout_code=live.Classification.TIMEOUT,
        )
    assert caught.value.classification == live.Classification.TRANSPORT
    assert not marker.exists()


@pytest.mark.parametrize(
    "payload", [b'{"v":1,"v":1}', b"{}", b"[]", b"NaN", b"x" * (live.IPC_BYTES + 1)]
)
def test_correction_invalid_ipc_payloads_fail_closed(payload):
    with pytest.raises((ValueError, TypeError)):
        live._decode_envelope(payload, None, "response")


def test_correction_partial_response_parent_identity_and_pending_receipt(
    tmp_path, monkeypatch
):
    _artifact_rig(tmp_path, monkeypatch)
    monkeypatch.setattr(live, "_load_key", lambda _: DUMMY)

    class Lost(FakeTransport):
        def post(self, *args, **kwargs):
            raise ConnectionError(DUMMY)

    with pytest.raises(live.EmploymentAcquisitionError) as caught:
        live.execute_live_candidate(
            enabled=True,
            limits=limits(),
            transport=Lost(),
            sleep=lambda _seconds: None,
        )
    error = caught.value
    safe = error.safe_observation()
    assert safe["transport_attempts"] == 3
    assert safe["max_transport_attempts"] == 3
    assert safe["retry_exhausted"] is True
    assert "retry_allowed" not in safe
    assert len(error.attempt_id) == 32
    pending = live.ARTIFACT_ROOT / safe["pending_receipt"]
    assert json.loads(pending.read_bytes())["attempt_id"] == error.attempt_id
    assert pending.stat().st_mode & 0o222 == 0
    release = live.ARTIFACT_ROOT / safe["writer_release_receipt"]
    assert json.loads(release.read_bytes())["transport_attempts"] == 3
    assert release.stat().st_mode & 0o222 == 0
    original = pending.read_bytes()
    with pytest.raises(live.EmploymentAcquisitionError):
        live._write_pending(error.attempt_id)
    assert pending.read_bytes() == original
    assert not (live.ARTIFACT_ROOT / safe["artifact_run"]).exists()
    assert DUMMY not in json.dumps(safe)


def test_correction_partial_artifact_parent_can_locate_without_success(
    tmp_path, monkeypatch
):
    _artifact_rig(tmp_path, monkeypatch)
    monkeypatch.setattr(live, "_load_key", lambda _: DUMMY)
    original_open = live.os.open

    def interrupted(name, flags, mode=0o777, **kwargs):
        if name == "request.json" and flags & os.O_CREAT:
            raise OSError(DUMMY)
        return original_open(name, flags, mode, **kwargs)

    monkeypatch.setattr(live.os, "open", interrupted)
    with pytest.raises(live.EmploymentAcquisitionError) as caught:
        live.execute_live_candidate(
            enabled=True, limits=limits(), transport=FakeTransport()
        )
    safe = caught.value.safe_observation()
    assert safe["classification"] == live.Classification.ARTIFACT.value
    assert safe["transport_attempts"] == 1
    assert safe["retry_exhausted"] is False
    assert "retry_allowed" not in safe
    directory = live.ARTIFACT_ROOT / safe["artifact_run"]
    assert (directory / "raw-response.bin").read_bytes() == encode(response())
    assert not (directory / "observation.json").exists()
    assert (live.ARTIFACT_ROOT / safe["pending_receipt"]).exists()


def test_correction_timeout_preserves_parent_id_and_immutable_intent(
    tmp_path, monkeypatch
):
    _artifact_rig(tmp_path, monkeypatch)
    monkeypatch.setattr(live, "_load_key", lambda _: DUMMY)
    monkeypatch.setattr(live, "ATTEMPT_SECONDS", 0.2)

    class Blocked(FakeTransport):
        def post(self, *args, **kwargs):
            time.sleep(3)

    with pytest.raises(live.EmploymentAcquisitionError) as caught:
        live.execute_live_candidate(enabled=True, limits=limits(), transport=Blocked())
    safe = caught.value.safe_observation()
    assert safe["classification"] == live.Classification.ATTEMPT_TIMEOUT.value
    assert safe["transport_attempts"] == 1 and safe["attempt_id"] is not None
    pending = json.loads((live.ARTIFACT_ROOT / safe["pending_receipt"]).read_bytes())
    assert pending["attempt_id"] == safe["attempt_id"]
    assert pending["transport_policy"] == "mdf_shared_source_request_policy"
    assert pending["max_transport_attempts"] == 3
    assert "retry_allowed" not in pending
    assert not (live.ARTIFACT_ROOT / safe["writer_release_receipt"]).exists()
    assert not (live.ARTIFACT_ROOT / safe["artifact_run"]).exists()


@pytest.mark.parametrize(
    "mutation",
    ["wrong-id", "bool-version", "non-bool-ok", "extra", "long-body", "nul-body"],
)
def test_correction_ipc_success_shape_is_exact(mutation):
    token = "e" * 32
    value = {
        "v": 1,
        "attempt_id": token,
        "ok": True,
        "result": {"kind": "response", "body": "{}", "obtained_at": None},
    }
    if mutation == "wrong-id":
        value["attempt_id"] = "f" * 32
    elif mutation == "bool-version":
        value["v"] = True
    elif mutation == "non-bool-ok":
        value["ok"] = 1
    elif mutation == "extra":
        value["result"]["http_status"] = DUMMY
    elif mutation == "long-body":
        value["result"]["body"] = "x" * 200001
    else:
        value["result"]["body"] = "\x00"
    with pytest.raises((ValueError, TypeError)):
        live._decode_envelope(encode(value), token, "response")


def test_correction_supervisor_reports_cancel_with_known_attempt(monkeypatch):
    counter = live.multiprocessing.get_context("fork").Value("b", 1, lock=False)

    def cancel(*args, **kwargs):
        raise KeyboardInterrupt(DUMMY)

    monkeypatch.setattr(live.select, "select", cancel)
    with pytest.raises(live.EmploymentAcquisitionError) as caught:
        live._supervise(
            lambda: time.sleep(3),
            deadline=time.monotonic() + 1,
            monotonic=time.monotonic,
            timeout_code=live.Classification.TIMEOUT,
            attempt_id="e" * 32,
            attempt_counter=counter,
        )
    safe = caught.value.safe_observation()
    assert safe["attempt_id"] == "e" * 32 and safe["transport_attempts"] == 1
    assert safe["max_transport_attempts"] == 3
    assert "retry_allowed" not in safe
    assert DUMMY not in json.dumps(safe)


def test_correction_complete_fake_retention_uses_same_parent_attempt_everywhere(
    tmp_path, monkeypatch
):
    _artifact_rig(tmp_path, monkeypatch)
    monkeypatch.setattr(live, "_load_key", lambda _: DUMMY)
    from market_data_foundation.historical import (
        bls_employment_source_control as control,
    )

    def no_historical_gate():
        pytest.fail("historical evidence inspection became a Source execution gate")

    monkeypatch.setattr(control, "inspect_committed_source", no_historical_gate)
    result = control.execute_source_once(enabled=True, transport=FakeTransport())
    token = result.attempt_id
    directory = live.ARTIFACT_ROOT / ("run-" + token)
    assert result.artifact_directory == directory
    assert result.candidate.evidence.acquisition_identity.endswith(token)
    assert result.candidate.plan.run.key.identity == "t03-employment-" + token + ":run"
    assert result.observation.attempt_id == token
    receipt = json.loads((directory / "observation.json").read_bytes())
    assert receipt["attempt_id"] == token
    assert receipt["revision"] == "T03_EMPLOYMENT_SOURCE_OCCAM_R1"
    assert receipt["limits"] == limits().__dict__
    assert receipt["raw_sha256"] == digest(encode(response()))
    assert "authority" not in receipt
    assert (
        receipt["receipt_state"] == "RETAINED_UNQUALIFIED_PENDING_INDEPENDENT_READBACK"
    )
    pending = json.loads((live.ARTIFACT_ROOT / live._pending_name(token)).read_bytes())
    release = json.loads((live.ARTIFACT_ROOT / live._release_name(token)).read_bytes())
    assert pending["attempt_id"] == release["attempt_id"] == token
    assert release["state"] == "ARTIFACT_HANDLES_CLOSED_WORKER_REAP_PENDING"
    assert release["transport_attempts"] == 1
    assert release["transport_policy"] == "mdf_shared_source_request_policy"
    assert release["max_transport_attempts"] == 3
    assert "retry_allowed" not in release
    assert (directory / "raw-response.bin").read_bytes() == encode(response())
    os.chmod(directory, 0o700)


def test_source_candidate_enters_existing_retained_job_and_local_read(
    tmp_path, monkeypatch
):
    from test_bls_employment_vertical import rig, run, target

    from market_data_foundation.historical import (
        bls_employment_source_control as control,
    )
    from market_data_foundation.historical.employment_local_read import (
        read_local_employment,
    )

    _artifact_rig(tmp_path, monkeypatch)
    monkeypatch.setattr(live, "_load_key", lambda _: DUMMY)
    acquisition = control.execute_source_once(enabled=True, transport=FakeTransport())
    item = acquisition.candidate
    assert acquisition.observation.source_qualification == "NOT_ESTABLISHED"
    engine, ledger, store, protection = rig(tmp_path / "journal")
    engine.clock.value = item.plan.version.frozen_at + timedelta(seconds=1)

    def forbidden(*args, **kwargs):
        pytest.fail("retained job or local Read attempted acquisition")

    monkeypatch.setattr(live, "execute_live_candidate", forbidden)
    monkeypatch.setattr(live, "_load_key", forbidden)
    engine.submit("source-retained", target(item))
    document = run(engine, "source-retained", item)
    assert document["state"] == "succeeded" and document["attempts"] == 1
    assert run(engine, "source-retained", None) == document
    assert document["raw"]["sha256"] == digest(item.evidence.raw_bytes)
    assert document["effects"]["ledger_commit_confirmed"] is True
    before = ledger.snapshot()
    exact = read_local_employment(
        ledger,
        reference="2026-09",
        version_identity=item.plan.version.identity,
        as_of=engine.clock(),
    )
    selected = read_local_employment(
        ledger,
        reference="2026-09",
        as_of=engine.clock(),
    )
    assert exact == selected
    assert exact.acquisitions[0].identity == item.evidence.acquisition_identity
    assert exact.content == item.plan.content
    assert ledger.snapshot() == before


@pytest.mark.parametrize("value", [False, True, {}, "grant"])
def test_invalid_limits_type_rejected_before_io(value, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid limits crossed effect boundary")

    monkeypatch.setattr(live, "_supervise", forbidden)
    with pytest.raises(live.EmploymentAcquisitionError) as caught:
        live.execute_live_candidate(enabled=True, limits=value)
    assert caught.value.classification == live.Classification.LIMITS
    assert caught.value.attempts == 0


def test_explicit_falsey_transport_never_falls_back(tmp_path, monkeypatch):
    _artifact_rig(tmp_path, monkeypatch)
    monkeypatch.setattr(live, "_load_key", lambda _: DUMMY)

    class FalseyTransport(FakeTransport):
        def __bool__(self):
            return False

    def forbidden():
        pytest.fail("explicit transport was replaced by default transport")

    monkeypatch.setattr(live, "EmploymentHttpsTransport", forbidden)
    with pytest.raises(live.EmploymentAcquisitionError) as caught:
        live.execute_live_candidate(
            enabled=True,
            transport=FalseyTransport(status=503),
            sleep=lambda _seconds: None,
        )
    assert caught.value.classification == live.Classification.HTTP
    assert caught.value.attempts == 3
    safe = caught.value.safe_observation()
    assert safe["retry_exhausted"] is True
    assert "retry_allowed" not in safe
