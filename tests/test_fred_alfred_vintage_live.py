"""Fake-source qualification tests; no live provider or filesystem IO."""

import http.client
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest

from market_data_foundation.historical import fred_alfred_vintage_live as live

VINTAGES = ("2026-09-30", "2026-08-28")
SECRET = "private-api-key-DO-NOT-RETAIN"


def discovery():
    return json.dumps({"vintage_dates": list(VINTAGES)}).encode()


def observations():
    return json.dumps(
        {
            "observations": [
                {
                    "realtime_start": vintage,
                    "realtime_end": "9999-12-31",
                    "date": "2025-01-01",
                    "value": "1.00" if index == 0 else ".",
                }
                for index, vintage in enumerate(VINTAGES)
            ]
        }
    ).encode()


def responses():
    return [live.Response(200, body) for body in (discovery(), observations()) * 2]


class FakeSource:
    def __init__(self, outcomes, events):
        self.outcomes = iter(outcomes)
        self.calls = []
        self.events = events

    def __call__(self, request):
        self.calls.append(request)
        self.events.append(("request", request.series, request.endpoint))
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        return result


class FakeStore:
    def __init__(self, events, fail_at=None):
        self.events = events
        self.retained = []
        self.fail_at = fail_at

    def retain(self, raw, artifact):
        self.events.append(("retain", artifact.logical_read))
        if artifact.logical_read == self.fail_at:
            raise OSError(SECRET)
        self.retained.append((raw, artifact))
        prefix = f"artifacts/{artifact.logical_read}"
        return live.ArtifactPaths(f"{prefix}/raw.json", f"{prefix}/request.json")


def setup(outcomes=None, fail_at=None):
    events = []
    source = FakeSource(responses() if outcomes is None else outcomes, events)
    store = FakeStore(events, fail_at)
    return source, store, events


def run(source, store):
    return live.qualify_vintages(
        request_source=source,
        artifact_store=store,
        live_opt_in=True,
        sleep=lambda _: None,
    )


def test_exact_four_reads_contract_and_candidate():
    source, store, events = setup()
    candidate = run(source, store)
    assert [(request.series, request.endpoint) for request in source.calls] == [
        ("GDP", "vintagedates"),
        ("GDP", "observations"),
        ("PAYEMS", "vintagedates"),
        ("PAYEMS", "observations"),
    ]
    for request in source.calls:
        assert request.url == live.BASE_URL + request.endpoint
        expected = {"series_id": request.series, "file_type": "json"}
        if request.endpoint == "vintagedates":
            expected.update(sort_order="desc", limit="2")
        else:
            expected.update(
                output_type="2",
                vintage_dates=",".join(VINTAGES),
                observation_start="2025-01-01",
                observation_end="2026-12-31",
                units="lin",
            )
        assert dict(request.parameters) == expected
    assert candidate.attempt_counts == (1, 1, 1, 1)
    assert candidate.selections == (("GDP", VINTAGES), ("PAYEMS", VINTAGES))
    assert len(candidate.reads) == len(store.retained) == 4
    for index, read in enumerate(candidate.reads, 1):
        assert read.raw == store.retained[index - 1][0]
        assert read.paths.raw == f"artifacts/{index}/raw.json"
        assert read.paths.request == f"artifacts/{index}/request.json"
    assert len(candidate.semantic_bundle.observations) == 4
    assert [item.series_id for item in candidate.semantic_bundle.metadata] == [
        "GDP",
        "PAYEMS",
    ]
    assert [event[0] for event in events] == ["request", "retain"] * 4


def test_retry_budgets_are_independent_for_every_logical_read():
    outcomes = []
    for success in responses():
        outcomes.extend([OSError(SECRET), live.Response(503, b"retry"), success])
    source, store, _ = setup(outcomes)
    candidate = run(source, store)
    assert candidate.attempt_counts == (3, 3, 3, 3)
    assert len(source.calls) == 12
    assert len(store.retained) == 4
    assert [(item.attempt, item.classification) for item in candidate.observations] == [
        (3, "retained")
    ] * 4
    assert SECRET not in repr(candidate)


@pytest.mark.parametrize("status", [401, 403, 404, 301, 302, 307, 501])
def test_terminal_http_no_fallback(status):
    source, store, _ = setup([live.Response(status, SECRET.encode())] + responses())
    with pytest.raises(live.QualificationError, match="http_terminal") as caught:
        run(source, store)
    assert len(source.calls) == 1
    assert store.retained == []
    assert SECRET not in repr(caught.value.observations)


@pytest.mark.parametrize(
    "failure", [OSError(SECRET), live.Response(429, SECRET.encode())]
)
def test_exhaustion_stops_without_fallback(failure):
    source, store, _ = setup([failure] * 3 + responses())
    with pytest.raises(live.QualificationError, match="request_exhausted") as caught:
        run(source, store)
    assert len(source.calls) == 3
    assert len(set(source.calls)) == 1
    assert store.retained == []
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value.observations)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("position", range(4))
def test_parse_failure_is_retained_terminal_and_never_requeried(position):
    outcomes = responses()
    outcomes[position] = live.Response(200, b"not-json")
    source, store, events = setup(outcomes)
    with pytest.raises(live.QualificationError, match="parser_error") as caught:
        run(source, store)
    assert len(source.calls) == len(store.retained) == position + 1
    assert store.retained[-1][0] == b"not-json"
    assert events[-1] == ("retain", position + 1)
    retained = caught.value.retained
    assert len(retained) == position + 1
    assert retained[-1].raw == b"not-json"
    assert retained[-1].raw_sha256 == sha256(b"not-json").hexdigest()
    assert retained[-1].raw_size == len(b"not-json")
    assert retained[-1].paths == live.ArtifactPaths(
        f"artifacts/{position + 1}/raw.json",
        f"artifacts/{position + 1}/request.json",
    )
    assert len(source.calls) == position + 1


@pytest.mark.parametrize("position", range(1, 5))
def test_artifact_failure_is_terminal_before_parser(monkeypatch, position):
    source, store, _ = setup(fail_at=position)
    calls = []
    real_discovery = live.parse_discovery
    real_observations = live.parse_observations

    def parse_discovery(raw):
        calls.append("discovery")
        return real_discovery(raw)

    def parse_observations(*args):
        calls.append("observations")
        return real_observations(*args)

    monkeypatch.setattr(live, "parse_discovery", parse_discovery)
    monkeypatch.setattr(live, "parse_observations", parse_observations)
    with pytest.raises(live.QualificationError, match="artifact_error") as caught:
        run(source, store)
    assert len(source.calls) == position
    assert len(store.retained) == len(calls) == position - 1
    assert len(caught.value.retained) == position - 1
    assert SECRET not in repr(caught.value.observations)


def test_retention_precedes_every_parser(monkeypatch):
    source, store, events = setup()
    real_discovery = live.parse_discovery
    real_observations = live.parse_observations

    def parse_discovery(raw):
        assert store.retained[-1][0] == raw
        events.append(("parse", len(store.retained)))
        return real_discovery(raw)

    def parse_observations(raw, series, vintages):
        assert store.retained[-1][0] == raw
        events.append(("parse", len(store.retained)))
        return real_observations(raw, series, vintages)

    monkeypatch.setattr(live, "parse_discovery", parse_discovery)
    monkeypatch.setattr(live, "parse_observations", parse_observations)
    run(source, store)
    assert [event[0] for event in events] == ["request", "retain", "parse"] * 4


def test_request_artifacts_only_contain_allowlisted_request_fields():
    source, store, _ = setup([OSError(SECRET)] + responses())
    candidate = run(source, store)
    serialized = json.dumps(
        [asdict(artifact) for _, artifact in store.retained], default=str
    )
    assert SECRET not in serialized
    assert "api_key" not in serialized
    assert "Authorization" not in serialized
    assert SECRET not in repr(candidate.observations)


def test_opt_in_required_before_any_io():
    source, store, events = setup()
    with pytest.raises(live.QualificationError, match="live_opt_in_required"):
        live.qualify_vintages(request_source=source, artifact_store=store)
    assert events == []


@pytest.mark.parametrize("parser_name", ["parse_discovery", "parse_observations"])
def test_unexpected_parser_exception_is_safe_and_terminal(monkeypatch, parser_name):
    source, store, _ = setup()

    def broken_parser(*args):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(live, parser_name, broken_parser)
    with pytest.raises(live.QualificationError, match="parser_error") as caught:
        run(source, store)
    expected = 1 if parser_name == "parse_discovery" else 2
    assert len(source.calls) == len(store.retained) == expected
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value.observations)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("failure", [None, "request_write", "rename"])
def test_atomic_store_publishes_pair_or_cleans_staging_with_fake_io(
    monkeypatch, failure
):
    staged = {}
    published = {}
    removed = []

    class FakePath:
        parts = ("artifacts", "t06")

        def __init__(self, name):
            self.name = str(name)

        def __str__(self):
            return self.name

        def __truediv__(self, name):
            return FakePath(f"{self.name}/{name}")

        def mkdir(self, **kwargs):
            pass

        def write_bytes(self, value):
            staged[self.name] = value

        def write_text(self, value, **kwargs):
            if failure == "request_write":
                raise OSError(SECRET)
            staged[self.name] = value

    def replace(source, destination):
        assert len(staged) == 2
        assert str(source) == "artifacts/t06/.staging-fake"
        if failure == "rename":
            raise OSError(SECRET)
        published.update(
            {
                str(destination / key.rsplit("/", 1)[1]): value
                for key, value in staged.items()
            }
        )
        staged.clear()

    def cleanup(path, **kwargs):
        removed.append(str(path))
        staged.clear()

    monkeypatch.setattr(live, "Path", FakePath)
    monkeypatch.setattr(
        live.tempfile, "mkdtemp", lambda **kwargs: "artifacts/t06/.staging-fake"
    )
    monkeypatch.setattr(live.os, "replace", replace)
    monkeypatch.setattr(live.shutil, "rmtree", cleanup)
    store = live.AtomicArtifactStore(FakePath("artifacts/t06"))
    request = live.Request("GDP", "vintagedates", (("series_id", "GDP"),))
    stamp = datetime(2026, 10, 1, tzinfo=UTC)
    artifact = live.RequestArtifact(1, request, 3, stamp, stamp)
    if failure:
        with pytest.raises(OSError):
            store.retain(b"unaltered raw", artifact)
        assert published == staged == {}
        assert removed == ["artifacts/t06/.staging-fake"]
    else:
        paths = store.retain(b"unaltered raw", artifact)
        assert removed == []
        assert published[paths.raw] == b"unaltered raw"
        assert json.loads(published[paths.request]) == {
            "logical_read": 1,
            "series": "GDP",
            "url": live.BASE_URL + "vintagedates",
            "parameters": {"series_id": "GDP"},
            "attempts": 3,
            "status_code": 200,
            "requested_at": stamp.isoformat(),
            "obtained_at": stamp.isoformat(),
        }


@pytest.mark.parametrize(
    ("failure", "classification"),
    [
        (http.client.LineTooLong(SECRET), "protocol_failure"),
        (http.client.HTTPException(SECRET), "protocol_failure"),
        (http.client.InvalidURL(SECRET), "request_invalid"),
        (RuntimeError(SECRET), "unexpected_exception"),
    ],
)
def test_terminal_source_exception_one_attempt(failure, classification):
    source, store, _ = setup([failure] + responses())
    with pytest.raises(live.QualificationError, match=classification) as caught:
        run(source, store)
    assert len(source.calls) == 1
    assert store.retained == []
    assert caught.value.retained == ()
    assert caught.value.observations == (live.ReadObservation(1, 1, classification),)
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value.observations)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_shared_policy_http_transients_and_injected_backoff(status):
    source, store, _ = setup([live.Response(status, b"retry")] * 2 + responses())
    delays = []
    candidate = live.qualify_vintages(
        request_source=source,
        artifact_store=store,
        live_opt_in=True,
        sleep=delays.append,
    )
    assert candidate.attempt_counts == (3, 1, 1, 1)
    assert delays == [1.0, 2.0]
    assert len(source.calls) == 6
    assert source.calls[0] == source.calls[1] == source.calls[2]


@pytest.mark.parametrize(
    "failure", [TimeoutError(SECRET), OSError(SECRET), http.client.IncompleteRead(b"")]
)
def test_shared_policy_transport_transients(failure):
    source, store, _ = setup([failure] * 2 + responses())
    candidate = run(source, store)
    assert candidate.attempt_counts == (3, 1, 1, 1)
    assert len(source.calls) == 6


def test_one_shared_policy_orchestration_per_logical_read(monkeypatch):
    outcomes = []
    for success in responses():
        outcomes.extend([live.Response(503, b"retry")] * 2 + [success])
    source, store, _ = setup(outcomes)
    orchestrations = []
    shared_request = live.request_with_policy

    def tracked_request(exchange, **kwargs):
        orchestrations.append(len(source.calls))
        return shared_request(exchange, **kwargs)

    monkeypatch.setattr(live, "request_with_policy", tracked_request)
    run(source, store)
    assert orchestrations == [0, 3, 6, 9]


@pytest.mark.parametrize("boundary", ["parser", "artifact"])
def test_local_failure_after_three_attempt_success_never_retries_provider(boundary):
    raw = b"not-json" if boundary == "parser" else discovery()
    source, store, _ = setup(
        [live.Response(503, b"retry")] * 2 + [live.Response(200, raw)] + responses(),
        fail_at=1 if boundary == "artifact" else None,
    )
    with pytest.raises(live.QualificationError, match=f"{boundary}_error") as caught:
        run(source, store)
    assert len(source.calls) == 3
    assert caught.value.observations[-1].attempt == 3
    if boundary == "parser":
        assert caught.value.retained[0].raw == raw
        assert caught.value.retained[0].artifact.attempts == 3
        assert caught.value.retained[0].raw_sha256 == sha256(raw).hexdigest()
    else:
        assert caught.value.retained == ()


@pytest.mark.parametrize("enabled", [False, None, 0, 1, "true"])
def test_execute_default_off_before_all_io(monkeypatch, enabled):
    def forbidden(*args, **kwargs):
        pytest.fail("default-off touched IO")

    for name in ("load_api_key", "ExactFredHttpsSource", "AtomicArtifactStore"):
        monkeypatch.setattr(live, name, forbidden)
    with pytest.raises(live.VintageLiveError, match="live_opt_in_required"):
        live.execute_live_candidate(enabled=enabled, clock=forbidden, sleep=forbidden)
    with pytest.raises(live.VintageLiveError, match="live_opt_in_required"):
        live.execute_live_candidate()


@pytest.mark.parametrize("boundary", [None, "artifact", "parser"])
def test_logical_timing_including_retries_and_local_failures(monkeypatch, boundary):
    events = []
    stamp = datetime(2026, 10, 1, tzinfo=UTC)
    stamps = [stamp + timedelta(seconds=index) for index in range(8)]
    ticks = iter(stamps)

    def clock():
        value = next(ticks)
        events.append(("clock", value))
        return value

    outcomes = []
    for response in responses():
        outcomes.extend([TimeoutError(SECRET), response])
    source = FakeSource(outcomes, events)
    store = FakeStore(events, fail_at=2 if boundary == "artifact" else None)
    if boundary == "parser":
        monkeypatch.setattr(live, "parse_observations", lambda *args: 1 / 0)
    kwargs = dict(
        request_source=source,
        artifact_store=store,
        live_opt_in=True,
        clock=clock,
        sleep=lambda _: None,
    )
    if boundary:
        with pytest.raises(live.QualificationError) as caught:
            live.qualify_vintages(**kwargs)
        artifacts = caught.value.request_artifacts
    else:
        candidate = live.qualify_vintages(**kwargs)
        artifacts = tuple(read.artifact for read in candidate.reads)
    assert len(artifacts) == (2 if boundary else 4)
    for index, artifact in enumerate(artifacts):
        assert artifact.requested_at == stamps[2 * index]
        assert artifact.obtained_at == stamps[2 * index + 1]
        assert artifact.attempts == 2
        assert artifact.requested_at <= artifact.obtained_at
    assert [event[0] for event in events] == [
        "clock",
        "request",
        "request",
        "clock",
        "retain",
    ] * len(artifacts)


@pytest.mark.parametrize(
    "requested,obtained",
    [
        (datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=UTC)),
        (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1)),
        (datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
    ],
)
def test_request_timing_rejects_naive_or_reversed(requested, obtained):
    with pytest.raises(ValueError, match="request_timing_invalid"):
        live.RequestArtifact(1, live._request("GDP"), 1, requested, obtained)


def install_https(monkeypatch, outcomes):
    events = []
    outcomes = iter(outcomes)

    class FakeResponse:
        def __init__(self, status, headers, body):
            self.status = status
            self.headers = headers
            self.stream = BytesIO(body)

        def getheaders(self):
            return self.headers

        def read(self, amount):
            assert 0 < amount <= 65_536
            return self.stream.read(amount)

    class FakeConnection:
        def __init__(self, host, timeout):
            assert (host, timeout) == ("api.stlouisfed.org", 20)
            events.append(("connect",))

        def request(self, method, target, headers):
            assert method == "GET"
            assert headers == {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            }
            events.append(("request", target))

        def getresponse(self):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return FakeResponse(*outcome)

        def close(self):
            events.append(("close",))

    monkeypatch.setattr(live.http.client, "HTTPSConnection", FakeConnection)
    return events


def test_concrete_https_auth_only_on_wire_and_execute_seams(monkeypatch):
    events = install_https(monkeypatch, [(200, [], r.body) for r in responses()])
    store = FakeStore([])
    monkeypatch.setattr(live, "load_api_key", lambda path: SECRET)
    monkeypatch.setattr(live, "AtomicArtifactStore", lambda root: store)
    candidate = live.execute_live_candidate(enabled=True)
    assert len(candidate.reads) == 4
    assert all("api_key=" + SECRET in e[1] for e in events if e[0] == "request")
    assert events[1][1].startswith("/fred/series/vintagedates?series_id=GDP&")
    serialized = json.dumps([asdict(a) for _, a in store.retained], default=str)
    assert SECRET not in serialized
    assert "api_key" not in serialized
    assert SECRET not in repr(live.ExactFredHttpsSource(SECRET))
    assert sum(e[0] == "close" for e in events) == 4


@pytest.mark.parametrize(
    "headers,body",
    [
        ([], b"x" * 1_000_001),
        ([("Content-Length", "1000001")], b""),
        ([("Content-Length", "2")], b"x"),
        ([("Content-Length", "1")], b"xx"),
        ([("Content-Length", "-1")], b""),
        ([("Content-Length", "1, 1")], b"x"),
        ([("Content-Length", " 1")], b"x"),
        ([("Content-Length", "1"), ("content-length", "1")], b"x"),
        ([("X-Test", "a"), ("x-test", "b")], b""),
        ([("Content-Length", "1"), ("Transfer-Encoding", "chunked")], b"x"),
    ],
)
def test_concrete_https_rejects_bad_response_without_retry(monkeypatch, headers, body):
    events = install_https(monkeypatch, [(200, headers, body)])
    with pytest.raises(live.QualificationError, match="protocol_failure"):
        run(live.ExactFredHttpsSource(SECRET), FakeStore([]))
    assert sum(e[0] == "connect" for e in events) == 1
    assert events[-1] == ("close",)


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_concrete_https_redirect_is_terminal(monkeypatch, status):
    events = install_https(monkeypatch, [(status, [("Location", SECRET)], b"")])
    with pytest.raises(live.QualificationError, match="http_terminal"):
        run(live.ExactFredHttpsSource(SECRET), FakeStore([]))
    assert sum(e[0] == "connect" for e in events) == 1


@pytest.mark.parametrize(
    "failure,classification,retry",
    [
        (OSError(SECRET), "transport_failure", True),
        (TimeoutError(SECRET), "timeout", True),
        (http.client.IncompleteRead(SECRET.encode()), "transport_failure", True),
        (http.client.InvalidURL(SECRET), "request_invalid", False),
        (http.client.LineTooLong(SECRET), "protocol_failure", False),
        (http.client.HTTPException(SECRET), "protocol_failure", False),
    ],
)
def test_concrete_https_uses_shared_classifier(
    monkeypatch, failure, classification, retry
):
    outcomes = [failure] + [(200, [], r.body) for r in responses()]
    events = install_https(monkeypatch, outcomes)
    source = live.ExactFredHttpsSource(SECRET)
    if retry:
        assert run(source, FakeStore([])).attempt_counts == (2, 1, 1, 1)
    else:
        with pytest.raises(live.QualificationError, match=classification) as caught:
            run(source, FakeStore([]))
        assert SECRET not in str(caught.value)
        assert sum(e[0] == "connect" for e in events) == 1


@pytest.mark.parametrize(
    "document",
    [
        b"[fred]\napi_key = 'valid-key+/?&'",
        b"[fred]\napi_key = ''",
        b"[fred]\napi_key = 42",
        b"[fred]\napi_key = 'has space'",
        b"[fred]\napi_key = '" + b"x" * 257 + b"'",
        b"[other]\napi_key = 'private'",
        b"invalid private TOML",
        b'[fred]\napi_key = "\\n"',
        None,
    ],
)
def test_secret_loader_safe_fake_io(monkeypatch, document):
    def fake_open(path, mode):
        assert path == Path("config/local/secret.toml")
        assert mode == "rb"
        if document is None:
            raise OSError(SECRET)
        return BytesIO(document)

    monkeypatch.setattr(Path, "open", fake_open)
    if document == b"[fred]\napi_key = 'valid-key+/?&'":
        assert live.load_api_key() == "valid-key+/?&"
    else:
        with pytest.raises(live.VintageLiveError, match="secret_load_failed") as caught:
            live.load_api_key()
        assert SECRET not in repr(caught.value)
        assert caught.value.__suppress_context__


@pytest.mark.parametrize("length", [0, 1_000_000])
def test_concrete_https_accepts_exact_body_bound(monkeypatch, length):
    body = b"x" * length
    install_https(monkeypatch, [(200, [("Content-Length", str(length))], body)])
    assert live.ExactFredHttpsSource(SECRET)(live._request("GDP")) == live.Response(
        200, body
    )


def test_concrete_https_exception_text_is_safe_and_key_is_urlencoded(monkeypatch):
    key = "secret+/?&="
    events = install_https(monkeypatch, [http.client.InvalidURL(key)])
    source = live.ExactFredHttpsSource(key)
    with pytest.raises(http.client.InvalidURL) as caught:
        source(live._request("GDP"))
    assert key not in str(caught.value)
    assert key not in repr(caught.value)
    assert caught.value.__suppress_context__
    assert "api_key=secret%2B%2F%3F%26%3D" in events[1][1]
