"""Host-only deterministic HTTP → durable acceptance → separate real CPI path.

Codex sandbox must not run this module. All clients and targets are synthetic.
"""

import json
import socket
from dataclasses import replace
from unittest.mock import Mock

import psycopg
import pytest
from fastapi.testclient import TestClient

from market_data_foundation.canonical.model import ContractError, digest
from market_data_foundation.historical.cpi_job_journal import FileJobJournal
from market_data_foundation.historical.cpi_local_job import DirectCpiAcquirer
from market_data_foundation.source_request_policy import SOURCE_REQUEST_POLICY
from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.cpi_jobs import CpiJobsHost, Operator
from tests.r5_jobs_fixture import Rig, intent, jobs_app, jobs_host, worker
from tests.test_cpi_local_job import NOW, RAW, spec

PATH = "/api/r5/cpi/jobs"


@pytest.fixture(autouse=True)
def forbid_external_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("deterministic test attempted external IO")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


def records(client, job=None):
    result = client.get(PATH, params={} if job is None else {"job_identity": job})
    assert result.headers["cache-control"] == "no-store"
    assert result.status_code == 200
    assert result.json()["status"] == "OK"
    return result.json()["jobs"]


def test_accept_separate_run_raw_version_publication_existing_reader_restart(tmp_path):
    rig = Rig(tmp_path)
    with TestClient(jobs_app(rig)) as client:
        before = rig.ledger.snapshot()
        assert records(client) == []
        for _ in range(2):
            assert client.post(PATH, json=intent()).status_code == 202
        job = records(client)[0]
        assert job["state"] == "pending" and job["attempts"] == 0
        assert len(job["history"]) == 1
        assert job["attempt_identity"] is job["worker_identity"] is None
        assert rig.transport.calls == rig.store.create_attempts == 0
        assert rig.ledger.snapshot() == before
    # Close the HTTP context, reopen the same SQLite journal in a separate host.
    separate = Rig(tmp_path, ledger=rig.ledger, store=rig.store)
    result = worker(separate).process("manual-first", enabled=True)
    assert result["state"] == "succeeded"
    assert separate.transport.calls == separate.store.create_attempts == 1
    assert worker(separate).process("manual-first", enabled=True) == result
    assert separate.transport.calls == separate.store.create_attempts == 1
    with TestClient(jobs_app(separate)) as client:
        job = records(client, "manual-first")[0]
        assert job["worker_identity"] == "synthetic-cpi-worker"
        assert job["attempt_identity"] == "manual-first-a1"
        assert job["attempts"] == 1 and job["state"] == "succeeded"
        assert job["history"][-1]["state"] == "succeeded"
        assert job["publication_occurrence"] is not None
        read = client.get(
            "/api/r5/cpi/read",
            params={
                "reference": "2026-09",
                "as_of": NOW.isoformat(),
                "mode": "exact_version",
                "version_identity": job["version_identity"],
            },
        )
        assert read.status_code == 200
        assert len(read.json()["measurements"]) == 4
        assert read.json()["evidence_mode"] == "OFFLINE_FIXTURE"
        assert (
            read.json()["version_identity"]
            == result["last_success"]["evidence"]["version_identity"]
        )
        assert records(client)[0] == job
    assert (
        FileJobJournal(tmp_path / "journal", read_only=True).get("manual-first")
        == result
    )


@pytest.mark.parametrize(
    "operator", [None, Operator("reader", False), {"role": "operator"}]
)
def test_server_identity_fail_closed_browser_roles_have_no_authority(
    tmp_path, operator
):
    rig = Rig(tmp_path)
    with TestClient(jobs_app(rig, operator=operator)) as client:
        response = client.post(
            PATH, json=intent(), headers={"X-Role": "operator", "X-Operator": "owner"}
        )
        assert response.status_code == 403
        assert response.json()["classification"] == "operator_not_authorized"
        assert (
            client.post(
                PATH + "/manual-first/cancel",
                json={},
                headers={"X-Role": "operator", "X-Operator": "owner"},
            ).status_code
            == 403
        )
        response = client.get(
            PATH, headers={"X-Role": "operator", "X-Operator": "owner"}
        )
        if isinstance(operator, Operator):
            assert response.status_code == 200
            assert response.json()["jobs"] == []
            assert response.json()["operator_identity"] == "reader"
            assert response.json()["write_eligible"] is False
        else:
            assert response.status_code == 403
            assert response.json() == {
                "status": "NOT_AVAILABLE",
                "classification": "operator_not_authorized",
            }
    assert rig.journal.list_jobs() == ()
    assert rig.transport.calls == rig.store.create_attempts == 0


@pytest.mark.parametrize("operator", [None, {"role": "operator"}])
@pytest.mark.parametrize("job_identity", [None, "manual-first", "missing"])
def test_bound_host_unauthenticated_get_never_reads_or_discloses_jobs(
    tmp_path, monkeypatch, operator, job_identity
):
    rig = Rig(tmp_path)
    rig.engine.submit("manual-first", spec())
    result = worker(rig).process("manual-first", enabled=True)
    assert result["state"] == "succeeded"
    assert result["attempt_id"]
    assert result["last_success"]["evidence"]["version_identity"]
    assert result["last_success"]["evidence"]["publication_occurrence"]
    before = rig.journal.path.read_bytes(), rig.ledger.snapshot()
    reader = Mock(side_effect=AssertionError("unauthenticated journal reader"))
    snapshot = Mock(side_effect=AssertionError("unauthenticated journal snapshot"))
    monkeypatch.setattr(CpiJobsHost, "reader", reader)
    monkeypatch.setattr(FileJobJournal, "snapshot", snapshot)
    with TestClient(jobs_app(rig, operator=operator)) as client:
        for headers in ({}, {"X-Role": "operator", "X-Operator": "owner"}):
            response = client.get(
                PATH,
                params={} if job_identity is None else {"job_identity": job_identity},
                headers=headers,
            )
            assert response.status_code == 403
            assert response.headers["cache-control"] == "no-store"
            assert response.json() == {
                "status": "NOT_AVAILABLE",
                "classification": "operator_not_authorized",
            }
    reader.assert_not_called()
    snapshot.assert_not_called()
    assert before == (rig.journal.path.read_bytes(), rig.ledger.snapshot())
    assert rig.transport.calls == rig.store.create_attempts == rig.commits == 1


def test_no_binding_get_is_unknown_and_post_never_creates_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app()) as client:
        response = client.get(
            PATH, headers={"X-Role": "operator", "X-Operator": "owner"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "UNKNOWN" and body["write_eligible"] is False
        assert body["evidence_mode"] == "UNCONFIGURED" and body["jobs"] == []
        assert body["classification"] == "job_capability_not_configured"
        assert client.post(PATH, json=intent()).status_code == 403
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "change",
    [
        {"reference": "2026-08"},
        {"asset_identity": "other-asset"},
        {"asset_revision": "future"},
        {"release_slot": "other-slot"},
        {"mode": "cron"},
        {"max_provider_requests": 9},
        {"start": "2020-01", "end": "2026-10"},
        {"storage": "browser-pg"},
        {"role": "operator"},
        {"job_identity": "../../private"},
    ],
)
def test_target_mode_budget_identity_and_storage_reject_before_effects(
    tmp_path, change
):
    rig = Rig(tmp_path)
    with TestClient(jobs_app(rig)) as client:
        response = client.post(PATH, json=intent(**change))
        assert response.status_code in {409, 422}
        assert records(client) == []
    assert rig.transport.calls == rig.store.create_attempts == 0


def test_identity_and_scope_conflict_persist_no_second_job(tmp_path):
    rig = Rig(tmp_path)
    with TestClient(jobs_app(rig)) as client:
        assert client.post(PATH, json=intent()).status_code == 202
        assert (
            client.post(PATH, json=intent("different")).json()["classification"]
            == "job_scope_already_registered"
        )
    with TestClient(
        jobs_app(rig, target=spec(expected_raw_sha256=digest(RAW)))
    ) as client:
        assert (
            client.post(PATH, json=intent()).json()["classification"]
            == "job_identity_reused"
        )
    assert len(rig.journal.list_jobs()) == 1


def test_durable_cancel_then_worker_makes_no_attempt_or_effect(tmp_path):
    rig = Rig(tmp_path)
    with TestClient(jobs_app(rig)) as client:
        client.post(PATH, json=intent())
        assert client.post(PATH + "/manual-first/cancel", json={}).status_code == 202
        assert records(client)[0]["cancel_requested"] is True
        assert records(client)[0]["state"] == "pending"
        assert worker(rig).process("manual-first", enabled=True)["state"] == "cancelled"
        assert records(client)[0]["attempts"] == 0
    assert rig.transport.calls == rig.store.create_attempts == 0


def test_preflight_failure_and_exception_redaction(tmp_path):
    rig = Rig(tmp_path)

    def denied(_spec):
        raise OSError("sensitive-endpoint-password")

    with TestClient(jobs_app(rig)) as client:
        client.post(PATH, json=intent())
        worker(rig, preflight=denied).process("manual-first", enabled=True)
        job = records(client)[0]
        assert (
            job["state"] == "failed" and job["classification"] == "preflight_rejected"
        )
        assert job["failure_stage"] == "preflight"
        assert "sensitive" not in json.dumps(job)
        assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in json.dumps(job)
    assert "sensitive" not in rig.journal.path.read_bytes().decode(errors="ignore")
    assert rig.transport.calls == rig.store.create_attempts == 0


def test_source_exception_never_discloses_credentials_or_raw_messages(
    tmp_path, monkeypatch
):
    rig = Rig(tmp_path)
    synthetic = "A" * 32
    rig.transport.outcome = OSError("private-source-" + synthetic)
    acquisitions = []
    acquire = DirectCpiAcquirer.acquire

    def counted_acquire(self, job_id, *callbacks):
        acquisitions.append(job_id)
        return acquire(self, job_id, *callbacks)

    monkeypatch.setattr(DirectCpiAcquirer, "acquire", counted_acquire)
    before = rig.ledger.snapshot()
    with TestClient(jobs_app(rig)) as client:
        assert client.post(PATH, json=intent()).status_code == 202
        result = worker(rig).process("manual-first", enabled=True)
        job = records(client)[0]
        assert job["state"] == "failed"
        assert job["classification"] == "bls_live_transport_failure"
        # One logical Job/acquisition; only the bounded transport is retried.
        assert job["attempts"] == 1 and job["attempt_identity"] == "manual-first-a1"
        assert result["effects"]["provider_request_possible"] == 1
        assert SOURCE_REQUEST_POLICY.max_transport_attempts == 3
        assert result["effects"]["provider_attempts_observed"] == 3
        assert result["effects"]["provider_response_received"] is False
        assert job["version_identity"] is job["publication_occurrence"] is None
        assert client.post(PATH, json=intent()).status_code == 202
        assert worker(rig).process("manual-first", enabled=True) == result
        assert records(client) == [job]
        assert acquisitions == ["manual-first"]
        assert synthetic not in client.get(PATH).text
        assert "private-source" not in client.get(PATH).text
    persisted = rig.journal.path.read_bytes().decode(errors="ignore")
    assert synthetic not in persisted and "private-source" not in persisted
    assert len(rig.journal.list_jobs()) == 1
    assert rig.transport.calls == SOURCE_REQUEST_POLICY.max_transport_attempts == 3
    assert rig.store.create_attempts == rig.commits == 0
    assert rig.ledger.snapshot() == before


def test_host_journals_are_isolated(tmp_path):
    first, second = Rig(tmp_path / "first"), Rig(tmp_path / "second")
    with TestClient(jobs_app(first)) as one, TestClient(jobs_app(second)) as two:
        one.post(PATH, json=intent())
        assert records(two) == []
        assert two.post(PATH + "/manual-first/cancel", json={}).status_code == 409
        assert records(one)[0]["cancel_requested"] is False
        assert records(two) == []
    assert first.transport.calls == second.transport.calls == 0


def test_unknown_commit_worker_only_reconciles_never_retries(tmp_path):
    def uncertain_commit():
        raise OSError("private-commit-details")

    rig = Rig(tmp_path, commit=uncertain_commit)
    with TestClient(jobs_app(rig)) as client:
        client.post(PATH, json=intent())
        assert (
            worker(rig).process("manual-first", enabled=True)["state"]
            == "outcome_unknown"
        )
        before = rig.transport.calls, rig.store.create_attempts
        recovered = worker(rig).process("manual-first", enabled=True)
        assert recovered["state"] == "outcome_unknown"
        assert recovered["reconciliation"]["outcome"] == "confirmed_not_published"
        assert (rig.transport.calls, rig.store.create_attempts) == before == (1, 1)
        assert records(client)[0]["state"] == "outcome_unknown"
        assert "private-commit" not in client.get(PATH).text


def test_restart_and_lock_isolation_and_opt_in(tmp_path):
    rig = Rig(tmp_path)
    rig.engine.submit("manual-first", spec())
    with pytest.raises(ContractError, match="explicit_opt_in"):
        worker(rig).process("manual-first")
    with rig.journal.executor("manual-first"):
        with pytest.raises(ContractError, match="executor_busy"):
            worker(rig).process("manual-first", enabled=True)
    rig.journal._update(
        "manual-first",
        NOW,
        state="running",
        stage="acquiring",
        attempts=1,
        attempt_id="manual-first-a1",
    )
    restarted = Rig(tmp_path, ledger=rig.ledger, store=rig.store)
    assert (
        worker(restarted).process("manual-first", enabled=True)["state"]
        == "outcome_unknown"
    )
    assert (
        worker(restarted).process("manual-first", enabled=True)["reconciliation"][
            "outcome"
        ]
        == "unresolved"
    )
    assert restarted.transport.calls == restarted.store.create_attempts == 0
    with TestClient(jobs_app(restarted)) as client:
        assert records(client)[0]["classification"] == "interrupted_attempt"
        assert records(client)[0]["attempts"] == 1


def test_missing_journal_and_query_budget_fail_unknown_without_creation(tmp_path):
    rig = Rig(tmp_path)
    rig.journal.path.unlink()
    with TestClient(jobs_app(rig)) as client:
        assert client.get(PATH).json()["status"] == "UNKNOWN"
    assert not rig.journal.path.exists()
    rig = Rig(tmp_path / "budget")
    for index in range(101):
        rig.journal.submit(
            f"bounded-{index}", spec().document(), f"synthetic-{index}", NOW
        )
    with TestClient(jobs_app(rig)) as client:
        result = client.get(PATH).json()
        assert result["status"] == "UNKNOWN" and result["jobs"] == []
        assert result["classification"] == "job_query_budget_exceeded"


def test_read_only_scope_no_lock_or_history_writes_and_strict_contract(tmp_path):
    rig = Rig(tmp_path)
    rig.engine.submit("manual-first", spec())
    before = rig.journal.path.read_bytes(), tuple(rig.journal.directory.iterdir())
    with TestClient(jobs_app(rig, operator=Operator("reader", False))) as client:
        for _ in range(2):
            assert records(client)[0]["job_identity"] == "manual-first"
        assert client.post(PATH, json=intent()).status_code == 403
        assert client.post(PATH + "/manual-first/cancel", json={}).status_code == 403
        assert (
            client.get(
                PATH + "?job_identity=manual-first&job_identity=other"
            ).status_code
            == 422
        )
        assert client.get(PATH + "?role=operator").status_code == 422
        assert client.get(PATH + "?job_identity=missing").status_code == 404
        assert client.post(PATH, json=intent(role="operator")).status_code == 422
    assert before == (
        rig.journal.path.read_bytes(),
        tuple(rig.journal.directory.iterdir()),
    )
    with pytest.raises(ContractError, match="evidence_mode_mismatch"):
        replace(jobs_host(rig), evidence_mode="AUTHORIZED_HOST")
    with pytest.raises(ContractError, match="writable_journal_required"):
        CpiJobsHost(
            FileJobJournal(rig.journal.directory, read_only=True),
            spec(),
            lambda: NOW,
            lambda _: None,
            "OFFLINE_FIXTURE",
        )
