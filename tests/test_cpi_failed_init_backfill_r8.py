"""R8 exact successor, fake PG + real API/worker/Reader. No live IO or DDL."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from test_b3_05_dev_host import no_network  # noqa: F401
from test_cpi_local_job import NOW, RAW

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
from market_data_foundation.historical.cpi_pg_binding import (
    BACKFILL_JOB,
    BACKFILL_SCOPE,
    INITIAL_JOB,
    backfill_constraint,
)
from market_data_foundation.web.cpi_dev_host import (
    DevHost,
    candidate_spec,
    job_identity,
)
from market_data_foundation.web.cpi_jobs import CpiManualWorker
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN, ORIGIN
from market_data_foundation.web.pce_r5_host import create_native_preview
from tests.b3_dev_fixture import fixture
from tests.r5_fixture import ReadOnlySnapshot


def setup_backfill(tmp_path, *, migrated=True, origin=ORIGIN):
    server, rig, _ = fixture(tmp_path)
    spec = candidate_spec()
    assert job_identity() == INITIAL_JOB
    rig.journal.submit(INITIAL_JOB, spec.document(), spec.scope, NOW)
    rig.transport.outcome = HttpResult(400, b"{}", {})
    worker = CpiManualWorker(
        "offline-worker", rig.engine, spec, lambda _: None, rig.acquirer
    )
    parent = worker.process(INITIAL_JOB, enabled=True)
    assert (
        parent["state"] == "failed"
        and parent["classification"] == "bls_live_http_failure"
    )
    rig.transport.outcome = HttpResult(200, RAW, {})
    successor_at = NOW + timedelta(seconds=1)
    rig.engine.clock = rig.engine.kernel.clock = lambda: successor_at
    rig.acquirer = replace(rig.acquirer, clock=lambda: successor_at)
    worker = replace(worker, acquirer=rig.acquirer)
    server.successor_supported = migrated
    from market_data_foundation.historical.pce_pg_binding import BINDING as PCE_BINDING

    pce = Mock(binding=PCE_BINDING)
    pce.reader.return_value.snapshot.return_value = []
    with patch("market_data_foundation.web.pce_r5_host.clock", lambda: successor_at):
        app = create_native_preview(
            SimpleNamespace(journal=lambda: pce, read_scope=Mock()),
            SimpleNamespace(
                journal=lambda: rig.journal,
                read_scope=lambda: lambda: nullcontext(ReadOnlySnapshot(rig.ledger)),
            ),
            ui_directory=None,
            enabled=True,
            origin=origin,
        )
    return server, rig, worker, app


def client(app, origin=ORIGIN):
    return TestClient(app, base_url=origin, client=("192.168.10.25", 1234))


def post(c, intent, **changes):
    return c.post(
        "/api/r5/cpi/jobs/backfill",
        json=intent | changes,
        headers={"Origin": str(c.base_url).rstrip("/")},
    )


def test_exact_successor_preserves_parent_and_old_publication_reads_new_version(
    tmp_path,
):
    server, rig, worker, app = setup_backfill(tmp_path)
    before_ledger = json.loads(rig.ledger.snapshot())
    parent = copy.deepcopy(server.data["jobs"][INITIAL_JOB])
    parent_events = copy.deepcopy(server.data["events"][INITIAL_JOB])
    with client(app) as c:
        meta = c.get("/api/r5/cpi/jobs").json()
        assert meta["write_eligible"] is False  # No general CPI writes on 18760.
        assert meta["backfill"]["eligible"] is True
        intent = meta["backfill"]["submission"]
        assert intent["job_identity"] == BACKFILL_JOB
        assert intent["parent_job_id"] == INITIAL_JOB
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: post(c, intent), range(2)))
        assert [r.status_code for r in replies] == [202, 202]
        assert len(server.data["jobs"]) == 2
        accepted = copy.deepcopy(server.data)
        assert post(c, intent).status_code == 202
        assert server.data == accepted
        assert worker.process(BACKFILL_JOB, enabled=True)["state"] == "succeeded"
        after = c.get("/api/r5/cpi/jobs").json()
        assert after["backfill"]["reason"] == "cpi_backfill_successor_slot_used"
        child = next(j for j in after["jobs"] if j["job_identity"] == BACKFILL_JOB)
        assert child["parent_job_id"] == INITIAL_JOB
        assert child["attempt_identity"] == BACKFILL_JOB + "-a1"
        assert BACKFILL_JOB in child["version_identity"]
        read = c.get(
            "/api/r5/cpi/read",
            params=dict(
                mode="exact_version",
                reference="2026-09",
                as_of=child["finished_at"],
                version_identity=child["version_identity"],
            ),
        )
        assert read.status_code == 200 and read.json()["status"] == "OK", read.text
        assert read.json()["version_identity"] == child["version_identity"]
        assert post(c, intent, job_identity="arbitrary").status_code == 409
        for path in (
            "/api/r5/cpi/jobs",
            "/api/r5/cpi/plans",
            f"/api/r5/cpi/jobs/{INITIAL_JOB}/cancel",
        ):
            assert c.post(path, json={}, headers={"Origin": ORIGIN}).status_code == 403
    assert server.data["jobs"][INITIAL_JOB] == parent
    assert server.data["events"][INITIAL_JOB] == parent_events
    # Snapshot contains append-only records; verify every old record remains.
    after_ledger = json.loads(rig.ledger.snapshot())
    for key, values in before_ledger.items():
        if isinstance(values, dict):
            assert all(after_ledger[key][k] == v for k, v in values.items())
        else:
            assert all(v in after_ledger[key] for v in values)
    assert len(server.data["attempts"]) == 2
    assert any(
        params[1] == BACKFILL_SCOPE
        for sql, params in server.commands
        if sql.startswith("INSERT INTO") and ".jobs " in sql
    )


@pytest.mark.parametrize(
    "origin,migrated,reason",
    [
        (ORIGIN, False, "cpi_backfill_exact_two_scope_migration_required"),
        (CANDIDATE_ORIGIN, True, "cpi_backfill_read_only"),
    ],
)
def test_migration_and_readonly_are_explained_before_action(
    tmp_path, origin, migrated, reason
):
    server, _, _, app = setup_backfill(tmp_path, migrated=migrated, origin=origin)
    before = copy.deepcopy(server.data)
    with client(app, origin) as c:
        offer = c.get("/api/r5/cpi/jobs").json()["backfill"]
        assert offer["eligible"] is False and offer["reason"] == reason
        assert post(c, offer["submission"]).status_code in {403, 409}
    assert server.data == before


@pytest.mark.parametrize(
    "changes",
    [
        {"reference": "2026-08"},
        {"asset_identity": "mdf.macro.bea.pce"},
        {"parent_job_id": BACKFILL_JOB},
        {"job_identity": "cpi-third"},
        {"binding_ref": "wrong"},
        {"confirmed": False},
        {"target_schema": "other"},
    ],
)
def test_wrong_target_rejected_without_effect(tmp_path, changes):
    server, _, _, app = setup_backfill(tmp_path)
    before = copy.deepcopy(server.data)
    with client(app) as c:
        intent = c.get("/api/r5/cpi/jobs").json()["backfill"]["submission"]
        assert post(c, intent, **changes).status_code == 409
    assert server.data == before


@pytest.mark.parametrize("outcome", ["failed", "unknown"])
def test_successor_terminal_never_allocates_another_attempt(tmp_path, outcome):
    server, rig, worker, app = setup_backfill(tmp_path)
    with client(app) as c:
        intent = c.get("/api/r5/cpi/jobs").json()["backfill"]["submission"]
        assert post(c, intent).status_code == 202
        if outcome == "failed":
            rig.transport.outcome = HttpResult(400, b"{}", {})
        else:
            rig.engine.commit = Mock(side_effect=OSError("synthetic"))
        result = worker.process(BACKFILL_JOB, enabled=True)
        assert result["state"] == (
            "failed" if outcome == "failed" else "outcome_unknown"
        )
        calls = rig.transport.calls
        assert post(c, intent).status_code == 202  # Same receipt, no fresh execution.
        assert c.get("/api/r5/cpi/jobs").json()["backfill"]["eligible"] is False
        worker.process(BACKFILL_JOB, enabled=True)
        assert rig.transport.calls == calls
        assert len(server.data["attempts"]) == 2


@pytest.mark.parametrize("when", ["before", "after"])
def test_commit_receipt_unknown_poisoned_writer_requires_observation(tmp_path, when):
    server, _, _, app = setup_backfill(tmp_path)
    with client(app) as c:
        intent = c.get("/api/r5/cpi/jobs").json()["backfill"]["submission"]
        server.fail_commit = when
        assert post(c, intent).status_code == 503
        assert post(c, intent).status_code == 503
        jobs = c.get("/api/r5/cpi/jobs").json()["jobs"]
        assert len(jobs) == (2 if when == "after" else 1)
        assert len(server.data["attempts"]) == 1


@pytest.mark.parametrize(
    "alter", ["third_scope", "unvalidated", "missing_unique", "wrong_parent"]
)
def test_storage_gate_does_not_accept_markers_or_weak_constraints(tmp_path, alter):
    server, rig, _, _ = setup_backfill(tmp_path)
    rows = [
        ("jobs_scope_check", backfill_constraint(), True, False),
        ("jobs_scope_key", "UNIQUE (scope)", True, False),
    ]
    if alter == "missing_unique":
        rows.pop()
    elif alter == "unvalidated":
        rows[0] = (rows[0][0], rows[0][1], False, False)
    else:
        rows[0] = (
            rows[0][0],
            rows[0][1] + " OR true"
            if alter == "third_scope"
            else rows[0][1].replace(INITIAL_JOB, "other"),
            True,
            False,
        )
    server.constraint_override = rows
    assert (
        rig.journal.reader().backfill_eligibility()
        == "cpi_backfill_exact_two_scope_migration_required"
    )


def test_worker_rejects_arbitrary_id_before_journal_or_configuration(tmp_path):
    host = object.__new__(DevHost)
    host.journal = Mock(side_effect=AssertionError("must not read"))
    with pytest.raises(ContractError, match="job_target_unsupported"):
        host.worker_once(
            __import__("pathlib").Path("artifacts/r8"), enabled=True, job_id="third"
        )
    host.journal.assert_not_called()


def test_existing_same_slot_publication_is_preserved_and_never_borrowed(tmp_path):
    from test_cpi_local_job import Rig

    server, rig, worker, app = setup_backfill(tmp_path)
    older = Rig(tmp_path / "old", ledger=rig.ledger, store=rig.store)
    older.submit("old-cpi-publication")
    assert older.run("old-cpi-publication")["state"] == "succeeded"
    before = json.loads(rig.ledger.snapshot())
    with client(app) as c:
        intent = c.get("/api/r5/cpi/jobs").json()["backfill"]["submission"]
        assert post(c, intent).status_code == 202
        result = worker.process(BACKFILL_JOB, enabled=True)
        # Existing as-of semantics reject unordered publications of the same slot.
        # R8 must not invent a correction rule or relabel the old success.
        assert result["state"] == "outcome_unknown"
        assert result["classification"] == "as_of_ambiguous"
        child = next(
            j
            for j in c.get("/api/r5/cpi/jobs").json()["jobs"]
            if j["job_identity"] == BACKFILL_JOB
        )
        assert BACKFILL_JOB in child["version_identity"]
        assert child["publication_occurrence"] is None
    after = json.loads(rig.ledger.snapshot())
    assert all(item in after[key] for key, items in before.items() for item in items)
    assert len(server.data["attempts"]) == 2


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"state": "outcome_unknown"}, "cpi_backfill_parent_not_failed"),
        (
            {"effects": {"canonical_possible": True}},
            "cpi_backfill_parent_effects_unresolved",
        ),
        (
            {"classification": "source_or_plan_rejected"},
            "cpi_backfill_original_failure_mismatch",
        ),
    ],
)
def test_unqualified_parent_get_and_direct_submit_both_reject(tmp_path, change, reason):
    server, rig, _, app = setup_backfill(tmp_path)
    # Correlated synthetic journal fixture mutation, never a production edit.
    doc, count = server.data["jobs"][INITIAL_JOB]
    doc.update(change)
    server.data["attempts"][INITIAL_JOB] = copy.deepcopy(doc)
    server.data["events"][INITIAL_JOB][count] = copy.deepcopy(doc)
    before = copy.deepcopy(server.data)
    with client(app) as c:
        offer = c.get("/api/r5/cpi/jobs").json()["backfill"]
        assert not offer["eligible"] and offer["reason"] == reason
        assert post(c, offer["submission"]).status_code == 409
    assert before == server.data


def test_host_worker_job_id_reads_child_terminal_without_loading_config(tmp_path):
    _, rig, worker, app = setup_backfill(tmp_path)
    with client(app) as c:
        intent = c.get("/api/r5/cpi/jobs").json()["backfill"]["submission"]
        assert post(c, intent).status_code == 202
    expected = worker.process(BACKFILL_JOB, enabled=True)
    assert expected["state"] == "succeeded"
    host = object.__new__(DevHost)
    host.journal = lambda: rig.journal
    from pathlib import Path

    assert (
        host.worker_once(Path("artifacts/r8"), enabled=True, job_id=BACKFILL_JOB)
        == expected
    )
