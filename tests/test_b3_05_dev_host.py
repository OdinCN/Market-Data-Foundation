"""Offline WebUI Host tests: protocol doubles, no real DB/provider/Garage claim."""

import copy
import socket
from unittest.mock import Mock

import psycopg
import pytest
from fastapi.testclient import TestClient

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.historical.cpi_pg_journal import PgJournalIdentity
from market_data_foundation.web.cpi_dev_host import (
    BINDING,
    ORIGIN,
    DevHost,
    candidate_spec,
    create_preview,
    job_identity,
)
from market_data_foundation.web.cpi_jobs import CpiManualWorker
from tests.b3_dev_fixture import fixture


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline test attempted network")

    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


def intent(**changes):
    return (
        dict(
            job_identity=job_identity(),
            asset_identity=BINDING.definition,
            asset_revision=BINDING.definition_revision,
            reference=BINDING.reference,
            release_slot=BINDING.release_slot,
            mode="manual_once",
            source_operation_ref=f"{BINDING.source}@{BINDING.source_revision}",
            binding_ref=f"{BINDING.binding}@{BINDING.binding_revision}",
            target_schema=BINDING.target.schema,
            confirmed=True,
        )
        | changes
    )


def client(app, peer="192.168.10.25"):
    return TestClient(app, base_url=ORIGIN, client=(peer, 12345))


def post(c, **changes):
    return c.post(
        "/api/r5/cpi/jobs", json=intent(**changes), headers={"Origin": ORIGIN}
    )


def test_disabled_host_touches_no_configuration_or_connection(tmp_path, monkeypatch):
    probe = Mock(side_effect=AssertionError("config read forbidden"))
    monkeypatch.setattr("market_data_foundation.web.cpi_dev_host.load_pg_config", probe)
    with pytest.raises(ContractError, match="opt_in"):
        DevHost(PgJournalIdentity("owner", 1))
    with pytest.raises(ContractError, match="opt_in"):
        create_preview(None, None)
    probe.assert_not_called()


def test_fixed_identity_same_intent_idempotence_and_independent_records(tmp_path):
    server, rig, app = fixture(tmp_path)
    with client(app) as c:
        meta = c.get("/api/r5/cpi/jobs").json()
        assert meta["operator_identity"] == BINDING.operator
        assert meta["source_operation_ref"] == intent()["source_operation_ref"]
        assert meta["storage_target"] == BINDING.target.schema
        assert meta["write_eligible"] is True
        assert post(c).status_code == 202
        before = copy.deepcopy(server.data)
        assert post(c).status_code == 202
        assert server.data == before
        assert post(c, job_identity="another").status_code == 409
        jobs = c.get("/api/r5/cpi/jobs").json()["jobs"]
        assert len(jobs) == 1 and jobs[0]["attempts"] == 0
        assert rig.transport.calls == rig.store.create_attempts == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"reference": "2026-08"},
        {"asset_revision": "wrong"},
        {"source_operation_ref": "mdf.other@r1"},
        {"binding_ref": "mdf.other@r1"},
        {"target_schema": "mdf_local_first_cpi_operable_r1"},
        {"confirmed": False},
        {"confirmed": None},
        {"mode": "periodic"},
        {"operator_identity": "owner"},
        {"confirmed": "true"},
    ],
)
def test_bad_identity_month_source_confirmation_do_not_write(tmp_path, changes):
    server, _, app = fixture(tmp_path)
    with client(app) as c:
        assert post(c, **changes).status_code in {409, 422}
    assert not server.data["jobs"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
        {"Origin": ORIGIN, "Host": "evil.example"},
        {"Origin": ORIGIN, "X-Operator": "owner"},
        {"Origin": ORIGIN, "X-Forwarded-For": "127.0.0.1"},
        {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
        [("Origin", ORIGIN), ("Origin", ORIGIN)],
    ],
)
def test_origin_host_and_browser_identity_rejected(tmp_path, headers):
    server, _, app = fixture(tmp_path)
    with client(app) as c:
        assert (
            c.post("/api/r5/cpi/jobs", json=intent(), headers=headers).status_code
            == 403
        )
    assert not server.connections


@pytest.mark.parametrize("peer", ["10.0.0.3", "192.168.11.2", "203.0.113.7"])
def test_only_finite_lan_and_no_forwarded_peer(tmp_path, peer):
    server, _, app = fixture(tmp_path)
    with client(app, peer) as c:
        assert post(c).status_code == 403
        assert c.get("/api/r5/cpi/jobs").status_code == 403
    assert not server.connections


@pytest.mark.parametrize(
    "path",
    [
        "/api/r5/cpi/plans",
        "/api/r5/cpi/jobs/any/cancel",
        "/api/r5/cpi/jobs?operator=owner",
    ],
)
def test_no_other_write_route(tmp_path, path):
    server, _, app = fixture(tmp_path)
    with client(app) as c:
        assert c.post(path, json={}, headers={"Origin": ORIGIN}).status_code == 403
    assert not server.connections


@pytest.mark.parametrize("mode", ["before", "after"])
def test_failed_acceptance_receipt_is_unknown_no_effect_or_auto_retry(tmp_path, mode):
    server, rig, app = fixture(tmp_path)
    server.fail_commit = mode
    with client(app) as c:
        response = post(c)
        assert response.status_code == 503 and response.json()["status"] == "UNKNOWN"
        assert "private" not in response.text
        assert rig.transport.calls == 0
        observed = c.get("/api/r5/cpi/jobs").json()
        assert len(observed["jobs"]) == (mode == "after")
        assert post(c).status_code == 503  # poisoned writer; observation only
        assert not server.data["attempts"]


@pytest.mark.parametrize("outcome", ["success", "unknown", "failed"])
def test_separate_worker_one_attempt_and_05_to_06_exact_asof(tmp_path, outcome):
    server, rig, app = fixture(tmp_path)
    if outcome == "unknown":
        rig.engine.commit = Mock(side_effect=OSError("private commit detail"))
    preflight = (
        Mock(side_effect=ValueError("private preflight"))
        if outcome == "failed"
        else Mock()
    )
    worker = CpiManualWorker(
        "offline-worker", rig.engine, candidate_spec(), preflight, rig.acquirer
    )
    with client(app) as c:
        assert post(c).status_code == 202
        result = worker.process(job_identity(), enabled=True)
        expected = {
            "success": "succeeded",
            "unknown": "outcome_unknown",
            "failed": "failed",
        }[outcome]
        assert result["state"] == expected
        count = rig.transport.calls
        assert len(server.data["attempts"]) == 1
        worker.process(job_identity(), enabled=True)
        assert rig.transport.calls == count == (outcome != "failed")
        job = c.get("/api/r5/cpi/jobs").json()["jobs"][0]
        assert job["state"] == expected and job["attempts"] == 1
        if outcome == "success":
            for mode in ("exact_version", "as_of"):
                query = dict(mode=mode, reference="2026-09", as_of=job["finished_at"])
                if mode == "exact_version":
                    query["version_identity"] = job["version_identity"]
                response = c.get("/api/r5/cpi/read", params=query)
                assert response.status_code == 200, response.text
                assert response.json()["status"] == "OK", response.text
        assert rig.transport.calls == count


@pytest.mark.parametrize(
    "fault", [None, "peer", "duplicate_length", "oversize", "chunked"]
)
def test_preview_http_adapter_preserves_peer_and_bounds_body_without_sockets(
    tmp_path, fault
):
    import json
    from email.message import Message
    from io import BytesIO
    from types import SimpleNamespace

    from scripts.mdf_b3_cpi_dev import PreviewHandler

    server, _, app = fixture(tmp_path)
    body = json.dumps(intent()).encode()
    handler = object.__new__(PreviewHandler)
    handler.server = SimpleNamespace(app=app)
    handler.client_address = (
        "203.0.113.1" if fault == "peer" else "192.168.10.25",
        1234,
    )
    handler.command = "POST"
    handler.path = "/api/r5/cpi/jobs"
    handler.headers = Message()
    for key, value in [
        ("Host", ORIGIN.removeprefix("http://")),
        ("Origin", ORIGIN),
        ("Content-Type", "application/json"),
        ("Content-Length", str(8193 if fault == "oversize" else len(body))),
    ]:
        handler.headers[key] = value
    if fault == "duplicate_length":
        handler.headers["Content-Length"] = str(len(body))
    if fault == "chunked":
        handler.headers["Transfer-Encoding"] = "chunked"
    handler.rfile = BytesIO(body)
    replies = []
    handler.respond = lambda *args: replies.append(args)
    handler.dispatch()
    assert replies[0][0] == (202 if fault is None else 403 if fault == "peer" else 503)
    assert len(server.data["jobs"]) == (fault is None)


def test_ddl_and_worker_opt_in_precede_any_config_or_database_access(monkeypatch):
    from scripts.mdf_b3_cpi_dev import initialize_schema

    probe = Mock(side_effect=AssertionError("configuration forbidden"))
    monkeypatch.setattr("scripts.mdf_b3_cpi_dev.load_pg_config", probe)
    with pytest.raises(ContractError, match="opt_in"):
        initialize_schema()
    host = object.__new__(DevHost)
    with pytest.raises(ContractError, match="opt_in"):
        host.worker_once(None)
    probe.assert_not_called()


@pytest.mark.parametrize(
    "failure, expected_attempts",
    [
        ("timeout", 3),
        ("network", 3),
        ("408", 3),
        ("429", 3),
        ("500", 3),
        ("502", 3),
        ("503", 3),
        ("504", 3),
        ("redirect", 1),
        ("401", 1),
        ("403", 1),
        ("404", 1),
    ],
)
def test_dev_source_uses_global_three_attempt_policy_same_one_job(
    tmp_path, failure, expected_attempts
):
    from dataclasses import replace

    from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
    from market_data_foundation.source_request_policy import SOURCE_REQUEST_POLICY

    assert SOURCE_REQUEST_POLICY.max_transport_attempts == 3
    assert SOURCE_REQUEST_POLICY.backoff_seconds == (1.0, 2.0)
    server, rig, app = fixture(tmp_path)
    wire = Mock()
    if failure == "timeout":
        wire.post.side_effect = TimeoutError("private provider detail")
    elif failure == "network":
        wire.post.side_effect = OSError("private socket detail")
    else:
        wire.post.return_value = HttpResult(
            302 if failure == "redirect" else int(failure), b"", {}
        )
    delays = []
    acquirer = replace(rig.acquirer, transport=wire, sleep=delays.append)
    with client(app) as c:
        assert post(c).status_code == 202
    worker = CpiManualWorker(
        "offline-worker", rig.engine, candidate_spec(), lambda _: None, acquirer
    )
    result = worker.process(job_identity(), enabled=True)
    assert result["state"] == "failed"
    assert result["attempts"] == 1
    assert result["effects"]["provider_attempts_observed"] == expected_attempts
    assert result["effects"]["provider_request_possible"] == 1
    assert delays == ([1.0, 2.0] if expected_attempts == 3 else [])
    if failure not in {"timeout", "network"}:
        assert result["effects"]["provider_last_http_status"] == (
            302 if failure == "redirect" else int(failure)
        )
    else:
        assert "provider_last_http_status" not in result["effects"]
    assert wire.post.call_count == expected_attempts
    assert rig.store.create_attempts == 0
    assert len(server.data["attempts"]) == len(server.data["jobs"]) == 1
    assert "private" not in str(result)
    assert worker.process(job_identity(), enabled=True) == result
    assert wire.post.call_count == expected_attempts


def test_dev_source_retry_503_429_success_has_single_logical_effect(tmp_path):
    from dataclasses import replace

    from test_cpi_local_job import RAW

    from market_data_foundation.historical.bls_cpi_direct_live import HttpResult

    server, rig, app = fixture(tmp_path)
    wire = Mock()
    wire.post.side_effect = [
        HttpResult(503, b"", {}),
        HttpResult(429, b"", {}),
        HttpResult(200, RAW, {}),
    ]
    delays = []
    acquirer = replace(rig.acquirer, transport=wire, sleep=delays.append)
    with client(app) as c:
        assert post(c).status_code == 202
    worker = CpiManualWorker(
        "offline-worker", rig.engine, candidate_spec(), lambda _: None, acquirer
    )
    result = worker.process(job_identity(), enabled=True)
    assert result["state"] == "succeeded"
    assert result["attempts"] == 1
    assert result["effects"]["provider_attempts_observed"] == 3
    assert result["effects"]["provider_response_received"] is True
    assert delays == [1.0, 2.0] and wire.post.call_count == 3
    assert rig.store.create_attempts == rig.commits == 1
    assert len(server.data["jobs"]) == len(server.data["attempts"]) == 1
    assert worker.process(job_identity(), enabled=True) == result
    assert wire.post.call_count == 3


def test_mixed_source_503_followed_by_network_timeouts_retains_503_on_failed_job(
    tmp_path,
):
    from dataclasses import replace

    from market_data_foundation.historical.bls_cpi_direct_live import HttpResult

    server, rig, app = fixture(tmp_path)
    wire = Mock()
    wire.post.side_effect = [
        HttpResult(503, b"", {}),
        TimeoutError("private transport detail"),
        TimeoutError("private transport detail"),
    ]
    delays = []
    acquirer = replace(rig.acquirer, transport=wire, sleep=delays.append)
    with client(app) as c:
        assert post(c).status_code == 202
    worker = CpiManualWorker(
        "offline-worker", rig.engine, candidate_spec(), lambda _: None, acquirer
    )
    result = worker.process(job_identity(), enabled=True)
    assert result["state"] == "failed"
    assert result["attempts"] == 1
    assert result["classification"] == "bls_live_transport_failure"
    assert result["effects"]["provider_attempts_observed"] == 3
    assert result["effects"]["provider_last_http_status"] == 503
    assert delays == [1.0, 2.0]
    assert wire.post.call_count == 3
    assert len(server.data["jobs"]) == len(server.data["attempts"]) == 1
    assert rig.store.create_attempts == 0
    assert "private" not in str(result)
    assert worker.process(job_identity(), enabled=True) == result
    assert wire.post.call_count == 3
