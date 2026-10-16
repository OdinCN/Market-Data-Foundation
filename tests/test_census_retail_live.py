"""Deterministic live-edge simulations; never real Census/PG/Garage IO."""

import http.client
import json
import socket
from contextlib import nullcontext
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from test_census_retail_vertical import BASE, encode, response

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import census_retail_live as live
from market_data_foundation.historical.bls_cpi_material_store import (
    InMemoryMaterialStore,
    InMemoryWriterProtection,
)
from market_data_foundation.historical.census_retail import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    CensusRetailRequest,
)
from market_data_foundation.historical.retail_local_job import (
    DirectRetailAcquirer,
    RetailJobJournal,
    RetailJobSpec,
    RetailLocalJob,
)
from market_data_foundation.source_request_policy import SOURCE_REQUEST_POLICY

DUMMY = "0123456789abcdef" * 2


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Retail deterministic tests attempted external IO")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


@pytest.fixture
def secret(tmp_path):
    path = tmp_path / "synthetic.toml"
    path.write_text('[census]\napi_key="' + DUMMY + '"\n')
    return path


def success(*, body=None, headers=None):
    return live.HttpResult(
        200,
        encode(response()) if body is None else body,
        {"content-type": "application/json"} if headers is None else headers,
    )


class FakeTransport:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes if outcomes is not None else [success()])
        self.calls = []

    def get(self, endpoint, parameters, *, timeout, max_response_bytes):
        self.calls.append((endpoint, dict(parameters), timeout, max_response_bytes))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def execute(secret, tmp_path, fake, **kwargs):
    return live.execute_live_candidate(
        enabled=True,
        secret_path=secret,
        artifact_root=tmp_path / "artifacts",
        transport=fake,
        clock=lambda: BASE,
        identity_factory=lambda: "synthetic",
        **kwargs,
    )


def assert_safe_failure(secret, tmp_path, fake, code, attempts):
    delays = []
    with pytest.raises(live.RetailLiveError) as failure:
        execute(secret, tmp_path, fake, sleep=delays.append)
    assert failure.value.classification == code
    assert failure.value.provider_data_attempts == attempts
    assert len(fake.calls) == attempts
    assert delays == list(SOURCE_REQUEST_POLICY.backoff_seconds[: max(0, attempts - 1)])
    assert DUMMY not in str(failure.value)
    assert DUMMY not in repr(failure.value)
    assert failure.value.__cause__ is None
    assert not (tmp_path / "artifacts").exists()


def test_default_off_precedes_secret_transport_and_artifact_io(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("default-off executed live dependency")

    monkeypatch.setattr(live, "load_api_key", forbidden)
    monkeypatch.setattr(live, "ExactCensusTransport", forbidden)
    with pytest.raises(ContractError, match="explicit_opt_in_required"):
        live.execute_live_candidate(artifact_root=tmp_path / "artifacts")
    assert not (tmp_path / "artifacts").exists()


def test_request_is_bounded_nonsecret_and_wire_adds_only_key(secret, tmp_path):
    fake = FakeTransport()
    candidate = execute(secret, tmp_path, fake)
    assert fake.calls == [
        (
            ENDPOINT,
            {
                "get": (
                    "category_code,data_type_code,seasonally_adj,cell_value,error_data"
                ),
                "seasonally_adj": "yes",
                "error_data": "no",
                "for": "us:*",
                "time": "from 2026-08 to 2026-09",
                "key": DUMMY,
            },
            20.0,
            MAX_RESPONSE_BYTES,
        )
    ]
    persisted = json.loads((candidate.artifact_directory / "request.json").read_bytes())
    assert persisted == CensusRetailRequest().persisted()
    for parameters in (persisted["parameters"], fake.calls[0][1]):
        assert "category_code" not in parameters
        assert "data_type_code" not in parameters
    assert "key" not in persisted["parameters"]
    assert DUMMY.encode() not in candidate.plan.content
    assert DUMMY not in repr(candidate.plan.acquisition)
    for artifact in candidate.artifact_directory.iterdir():
        assert DUMMY.encode() not in artifact.read_bytes()
    observation = json.loads(
        (candidate.artifact_directory / "observation.json").read_bytes()
    )
    assert observation["classification"] == "success"
    assert observation["provider_data_attempts"] == 1


def test_exact_https_get_query_shape_without_network(monkeypatch):
    observed = {}
    body = encode(response())

    class Response:
        status = 200

        def getheaders(self):
            return [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ]

        def read(self, size):
            assert size == MAX_RESPONSE_BYTES + 1
            return body

    class Connection:
        def __init__(self, host, *, timeout):
            observed.update(host=host, timeout=timeout)

        def request(self, method, target, *, headers):
            observed.update(method=method, target=target, headers=headers)

        def getresponse(self):
            return Response()

        def close(self):
            observed["closed"] = True

    monkeypatch.setattr(live.http.client, "HTTPSConnection", Connection)
    parameters = CensusRetailRequest().persisted()["parameters"] | {"key": DUMMY}
    result = live.ExactCensusTransport().get(
        ENDPOINT, parameters, timeout=20, max_response_bytes=MAX_RESPONSE_BYTES
    )
    assert result.body == body
    assert observed["host"] == "api.census.gov"
    assert observed["method"] == "GET"
    target = urlsplit(observed["target"])
    assert target.path == "/data/timeseries/eits/marts"
    assert parse_qs(target.query) == {key: [value] for key, value in parameters.items()}
    assert "category_code" not in parse_qs(target.query)
    assert "data_type_code" not in parse_qs(target.query)
    assert observed["closed"] is True
    assert observed["headers"] == {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }


@pytest.mark.parametrize(
    "first", [408, 429, 500, 502, 503, 504, "timeout", "os", "partial"]
)
def test_shared_retry_transient_only_then_success(secret, tmp_path, first):
    failures = {
        "timeout": TimeoutError(DUMMY),
        "os": OSError(DUMMY),
        "partial": http.client.IncompleteRead(b"partial"),
    }
    initial = (
        failures[first] if isinstance(first, str) else live.HttpResult(first, b"", {})
    )
    fake = FakeTransport([initial, success()])
    before, after, delays = [], [], []
    metered = live.MeteredRetailTransport(
        fake, lambda: before.append(True), lambda *args: after.append(args)
    )
    candidate = execute(secret, tmp_path, metered, sleep=delays.append)
    assert len(fake.calls) == 2
    assert before == [True]
    assert after == [(not isinstance(first, str), 1), (True, 2)]
    assert delays == [1.0]
    assert candidate.evidence.provider_data_attempts == 2
    assert candidate.plan.acquisition.provider_data_attempts == 2


@pytest.mark.parametrize("terminal", [400, 401, 403, 404, 302, 501])
def test_terminal_http_never_retries_or_falls_back(secret, tmp_path, terminal):
    fake = FakeTransport([live.HttpResult(terminal, DUMMY.encode(), {})])
    assert_safe_failure(secret, tmp_path, fake, "http_failure", 1)
    assert fake.calls[0][0] == ENDPOINT


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ValueError(DUMMY), "unexpected_exception"),
        (http.client.InvalidURL(DUMMY), "request_invalid"),
        (http.client.HTTPException(DUMMY), "protocol_failure"),
    ],
)
def test_terminal_exceptions_are_secret_safe(secret, tmp_path, error, code):
    assert_safe_failure(secret, tmp_path, FakeTransport([error]), code, 1)


@pytest.mark.parametrize("kind", ["http", "timeout"])
def test_retry_exhaustion_is_three_attempts_with_one_two_second_backoff(
    secret, tmp_path, kind
):
    outcomes = (
        [live.HttpResult(503, b"", {})] * 3
        if kind == "http"
        else [TimeoutError(DUMMY)] * 3
    )
    assert_safe_failure(
        secret,
        tmp_path,
        FakeTransport(outcomes),
        "http_failure" if kind == "http" else "timeout",
        3,
    )


def test_third_attempt_success_retains_shared_count(secret, tmp_path):
    fake = FakeTransport(
        [TimeoutError(DUMMY), live.HttpResult(429, b"", {}), success()]
    )
    delays = []
    candidate = execute(secret, tmp_path, fake, sleep=delays.append)
    assert delays == [1.0, 2.0]
    assert candidate.plan.acquisition.provider_data_attempts == 3


@pytest.mark.parametrize(
    "result",
    [
        success(body=b"invalid json"),
        success(body=encode(response()[:-1])),
        success(body=DUMMY.encode()),
        success(body=b"x" * (MAX_RESPONSE_BYTES + 1)),
        success(headers={"content-type": "text/html"}),
        success(
            headers={"content-type": "application/json", "content-encoding": "gzip"}
        ),
    ],
)
def test_success_response_contract_failure_does_not_requery(secret, tmp_path, result):
    assert_safe_failure(
        secret,
        tmp_path,
        FakeTransport([result]),
        "census_retail_response_contract_failed",
        1,
    )


@pytest.mark.parametrize(
    "content", [None, "[census]\n", '[census]\napi_key="invalid"\n']
)
def test_credential_failure_precedes_provider_io(tmp_path, content):
    secret = tmp_path / "missing.toml"
    if content is not None:
        secret.write_text(content)
    fake = FakeTransport()
    assert_safe_failure(
        secret, tmp_path, fake, "census_retail_credential_unavailable", 0
    )


def test_success_then_artifact_failure_does_not_requery(secret, tmp_path):
    fake = FakeTransport()
    root = tmp_path / "artifacts"
    root.write_text("synthetic obstruction")
    with pytest.raises(live.RetailLiveError, match="artifact_write_failed") as failure:
        execute(secret, tmp_path, fake)
    assert failure.value.provider_data_attempts == 1
    assert len(fake.calls) == 1


@pytest.mark.parametrize("stage", ["storage", "commit", "readback"])
def test_success_then_local_job_failure_never_requeries_or_reexecutes(
    secret, tmp_path, stage
):
    class Store(InMemoryMaterialStore):
        def create_if_absent(self, identity, payload):
            if stage == "storage":
                raise OSError(DUMMY)
            return super().create_if_absent(identity, payload)

    def commit():
        if stage == "commit":
            raise OSError(DUMMY)

    def readback():
        if stage == "readback":
            raise OSError(DUMMY)
        pytest.fail("unexpected readback after prior failure")
        return nullcontext()

    ledger = InMemoryLedger()
    journal = RetailJobJournal(tmp_path / "journal")
    job = RetailLocalJob(
        journal,
        lambda: BASE,
        CanonicalKernel(ledger, lambda: BASE),
        Store(),
        InMemoryWriterProtection(),
        readback,
        commit,
    )
    job.submit("synthetic", RetailJobSpec())
    fake = FakeTransport()
    acquirer = DirectRetailAcquirer(fake, secret, tmp_path / "artifacts", lambda: BASE)
    result = job.run(
        "synthetic", enabled=True, preflight=lambda spec: None, acquirer=acquirer
    )
    assert result["state"] == "outcome_unknown"
    assert len(fake.calls) == 1
    assert result["effects"]["provider_attempts_observed"] == 1
    assert DUMMY not in json.dumps(result)
    assert (
        job.run(
            "synthetic", enabled=True, preflight=lambda spec: None, acquirer=acquirer
        )
        == result
    )
    assert len(fake.calls) == 1
