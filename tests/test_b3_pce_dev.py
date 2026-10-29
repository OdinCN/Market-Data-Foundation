"""Synthetic protocol/vertical tests. No BEA, PG, Garage or listening sockets."""

import copy
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import psycopg
import pytest
import test_b3_05_cpi_pg_jobs as protocol
from fastapi.testclient import TestClient
from test_bea_pce_vertical import BASE, candidate, rig
from test_postgres_runtime_config import ca_file, runtime_file

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import bea_pce_live as live
from market_data_foundation.historical.pce_dev_acquire import (
    LivePceAcquirer,
    PceAcquisitionRejected,
)
from market_data_foundation.historical.pce_local_job import PceJobSpec
from market_data_foundation.historical.pce_pg_binding import (
    BACKFILL_SCOPE,
    BINDING,
    DOMAIN,
    SCHEMA,
    PcePgBinding,
    PcePgTarget,
    backfill_identity,
    candidate_spec,
    job_identity,
)
from market_data_foundation.historical.pce_pg_journal import (
    PgJournalIdentity,
    PgJournalUnavailable,
    PostgresJobJournal,
)
from market_data_foundation.storage.postgres_runtime_config import load_pg_config
from market_data_foundation.web.pce_api import (
    backfill_submission,
    create_preview,
    submission,
)
from market_data_foundation.web.pce_dev_host import ORIGIN, DevHost


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("PCE offline test attempted external IO")

    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


class PceConnection(protocol.Connection):
    """PCE-only protocol double. Actual inherited DDL blocks a second scope.

    Tests must explicitly opt into a hypothetical storage capability to exercise
    successor behavior; that opt-in is NOT a real schema qualification.
    """

    def execute(self, text, params=()):
        if text.startswith("SELECT conname, pg_catalog.pg_get_constraintdef(oid)"):
            assert params == (f"{SCHEMA}.jobs",)
            # Match the actual DEV pg_get_constraintdef(PG18) precisely.
            check = (
                f"CHECK ((((identity = '{job_identity()}'::text) "
                f"AND (scope = '{DOMAIN}'::text) "
                f"AND ((document ->> 'parent_job_id'::text) IS NULL)) "
                f"OR ((identity = '{backfill_identity()}'::text) "
                f"AND (scope = '{BACKFILL_SCOPE}'::text) "
                f"AND ((document ->> 'parent_job_id'::text) = "
                f"'{job_identity()}'::text))))"
                if self.server.successor_supported
                else f"CHECK (scope = '{DOMAIN}')"
            )
            override = getattr(self.server, "scope_constraint_override", None)
            if override is not None:
                check = override
            return protocol.Result(
                [
                    ("jobs_scope_key", "UNIQUE (scope)"),
                    ("jobs_scope_check", check),
                ]
            )
        return super().execute(text, params)

    def write(self, text, params):
        if text.startswith(f"INSERT INTO {SCHEMA}.jobs ") and self.data["jobs"]:
            job, scope, doc, count = params
            assert self.server.successor_supported, "inherited UNIQUE(scope)"
            assert job == backfill_identity() and scope == BACKFILL_SCOPE
            assert set(self.data["jobs"]) == {job_identity()}
            self.data["jobs"][job] = (copy.deepcopy(doc.obj), count)
        else:
            super().write(text, params)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # Reuse the strict SQL protocol double, with isolated PCE namespace pins.
    monkeypatch.setattr(protocol, "SCHEMA", SCHEMA)
    monkeypatch.setattr(protocol, "DOMAIN", DOMAIN)
    server = protocol.Server()
    server.oid = 141635
    server.successor_supported = False

    def connect(**kwargs):
        connection = PceConnection(server, kwargs)
        server.connections.append(connection)
        server.mutate_connection(connection)
        return connection

    server.connect = connect
    profile = load_pg_config(
        runtime_file(tmp_path, "verify-full", root=ca_file(tmp_path))
    )
    identity = PgJournalIdentity(server.owner, server.oid)

    def factory():
        return PostgresJobJournal(
            server.connect, profile, identity, BINDING, enabled=True
        )

    engine, _, ledger, store, _, clock = rig(tmp_path)
    # In-memory canonical/store stand-ins are TEST ONLY; production constructor
    # requires a real PG ledger for the PG journal and owned Garage bindings.
    engine.journal = factory()
    check_bindings = engine._check_bindings
    independent = engine._check_independent
    monkeypatch.setattr(
        engine,
        "_check_bindings",
        lambda s: check_bindings(replace(s, effect_domain="synthetic")),
    )
    monkeypatch.setattr(
        engine,
        "_check_independent",
        lambda ledger, s: independent(ledger, replace(s, effect_domain="synthetic")),
    )
    app = create_preview(
        engine.journal,
        lambda: nullcontext(InMemoryLedger.from_snapshot(ledger.snapshot())),
        clock,
        enabled=True,
    )
    return server, factory, engine, store, app


def client(app, peer="192.168.10.25"):
    return TestClient(app, base_url=ORIGIN, client=(peer, 12345))


def submit(journal):
    spec = candidate_spec()
    return journal.submit(job_identity(), spec.document(), spec.scope, BASE)


def start(journal):
    return journal._update(
        job_identity(),
        BASE,
        state="running",
        stage="preflight",
        attempts=1,
        attempt_id=job_identity() + "-a1",
        started_at=BASE.isoformat(),
    )


def test_explicit_binding_and_scope_are_immutable():
    assert BINDING.source_qualified is False and BINDING.candidate_only is True
    assert candidate_spec().max_provider_requests == 2
    for values in (
        {"reference": "2026-09"},
        {"operator": "browser"},
        {"source": "bls"},
        {"candidate_only": False},
    ):
        with pytest.raises(ContractError):
            PcePgBinding(**values)
    with pytest.raises(ContractError):
        PcePgTarget(schema="mdf_b3_cpi_05_dev_r1")
    with pytest.raises(ContractError):
        PceJobSpec(mode="acquire", max_provider_requests=3)


def test_disabled_host_does_not_load_configuration(monkeypatch):
    probe = Mock(side_effect=AssertionError("configuration forbidden"))
    monkeypatch.setattr("market_data_foundation.web.pce_dev_host.load_pg_config", probe)
    with pytest.raises(ContractError, match="opt_in"):
        DevHost(PgJournalIdentity("owner", 1))
    with pytest.raises(ContractError, match="opt_in"):
        create_preview(None, None, None)
    probe.assert_not_called()


def test_api_accepts_only_pg_then_one_worker_four_values(setup):
    server, factory, engine, store, app = setup
    with client(app) as c:
        catalog = c.get("/api/pce/catalog").json()
        assert catalog["parameters"][0]["TableName"] == "T20807"
        assert catalog["parameters"][1]["Year"] == "2025,2026"
        assert (
            c.post(
                "/api/pce/jobs", json=submission(), headers={"Origin": ORIGIN}
            ).status_code
            == 202
        )
        before = copy.deepcopy(server.data)
        assert (
            c.post(
                "/api/pce/jobs", json=submission(), headers={"Origin": ORIGIN}
            ).status_code
            == 202
        )
        assert server.data == before
        assert not server.data["attempts"] and not store._objects

        def acquire():
            durable = factory().get(job_identity())
            assert durable["attempts"] == 1
            assert durable["effects"]["provider_logical_reads_possible"] == 2
            assert server.data["attempts"][job_identity()] == durable
            return candidate(job_identity())

        source = Mock(side_effect=acquire)
        result = engine.run(
            job_identity(), enabled=True, preflight=lambda _: None, acquire=source
        )
        assert result["state"] == "succeeded"
        assert source.call_count == 1
        assert (
            engine.run(
                job_identity(), enabled=True, preflight=lambda _: None, acquire=source
            )
            == result
        )
        assert source.call_count == 1
        assert len(server.data["jobs"]) == len(server.data["attempts"]) == 1
        listed = c.get("/api/pce/jobs").json()["jobs"][0]
        assert (
            listed["state"] == "succeeded"
            and listed["events"][-1]["stage"] == "verified"
        )
        assert "plan" not in listed and "raw" not in listed
        query = {"reference": "2026-07", "as_of": engine.clock().isoformat()}
        as_of = c.get("/api/pce/read", params=query)
        assert as_of.status_code == 200, as_of.text
        exact = c.get(
            "/api/pce/read",
            params=query | {"version": as_of.json()["version_identity"]},
        )
        assert exact.json() == as_of.json()
        assert {k: v["value"] for k, v in exact.json()["measurements"].items()} == {
            "headline_mom_sa": "0.3",
            "core_mom_sa": "0.3",
            "headline_yoy_sa": "10.000000",
            "core_yoy_sa": "10.000000",
        }
        assert exact.json()["source_qualified"] is False
        assert exact.json()["provider_release_at"] is None
        assert (
            c.get("/api/pce/read", params=query | {"version": "wrong"}).status_code
            == 409
        )
        assert (
            c.get(
                "/api/pce/read", params=query | {"as_of": "2020-01-01T00:00:00Z"}
            ).status_code
            == 409
        )


@pytest.mark.parametrize(
    "kind",
    [
        "reference",
        "source",
        "binding",
        "target",
        "operator",
        "confirmation",
        "extra",
        "bool",
    ],
)
def test_scope_mutations_have_no_effect(setup, kind):
    server, _, _, _, app = setup
    body = submission()
    if kind == "target":
        body["binding"]["target"]["schema"] = "mdf_b3_cpi_05_dev_r1"
    elif kind in {"reference", "source", "binding", "operator"}:
        body["binding"][kind] = "wrong"
    elif kind == "bool":
        body["binding"]["candidate_only"] = 1
    else:
        body[kind] = "wrong"
    with client(app) as c:
        assert (
            c.post("/api/pce/jobs", json=body, headers={"Origin": ORIGIN}).status_code
            == 422
        )
    assert not server.data["jobs"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://evil.invalid"},
        {"Origin": ORIGIN, "X-Operator": "owner"},
        {"Origin": ORIGIN, "X-Forwarded-For": "127.0.0.1"},
        {"Origin": ORIGIN, "Host": "localhost:18759"},
        {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
        {"Origin": ORIGIN, "Content-Type": "text/plain"},
    ],
)
def test_untrusted_request_rejected(setup, headers):
    server, _, _, _, app = setup
    with client(app) as c:
        assert (
            c.post("/api/pce/jobs", json=submission(), headers=headers).status_code
            == 403
        )
    assert not server.data["jobs"]


def test_external_peer_bad_json_unknown_endpoint_no_worker(setup):
    server, _, _, _, app = setup
    with client(app, "198.51.100.1") as c:
        assert c.get("/api/pce/catalog").status_code == 403
    with client(app) as c:
        assert (
            c.post(
                "/api/pce/jobs/worker", json={}, headers={"Origin": ORIGIN}
            ).status_code
            == 403
        )
        headers = {"Origin": ORIGIN, "Content-Type": "application/json"}
        assert (
            c.post(
                "/api/pce/jobs", content='{"a":1,"a":2}', headers=headers
            ).status_code
            == 422
        )
        assert (
            c.post("/api/pce/jobs", content=b"x" * 8193, headers=headers).status_code
            == 413
        )
        assert (
            c.get(
                "/api/pce/read?reference=2026-07&reference=2026-09&as_of=bad"
            ).status_code
            == 422
        )
        # The native R5 route exists, but no CPI Host was injected here.
        cpi_jobs = c.get("/api/r5/cpi/jobs").json()
        assert cpi_jobs["evidence_mode"] == "UNCONFIGURED"
        assert cpi_jobs["status"] == "UNKNOWN" and not cpi_jobs["write_eligible"]
    assert not server.data["jobs"]


def test_concurrent_duplicate_and_other_job_rejected(setup):
    server, factory, _, _, _ = setup
    with ThreadPoolExecutor(max_workers=4) as pool:
        docs = list(pool.map(lambda _: submit(factory()), range(8)))
    assert all(d == docs[0] for d in docs)
    assert len(server.data["events"][job_identity()]) == 1
    with pytest.raises(ContractError):
        factory().submit(
            "other", candidate_spec().document(), candidate_spec().scope, BASE
        )


@pytest.mark.parametrize("mode", ["before", "after"])
def test_accept_commit_unknown_returns_no_success_or_source(setup, mode):
    server, factory, _, _, app = setup
    server.fail_commit = mode
    with client(app) as c:
        response = c.post(
            "/api/pce/jobs", json=submission(), headers={"Origin": ORIGIN}
        )
        assert response.status_code == 503
        assert protocol.PRIVATE not in response.text
    assert not server.data["attempts"]
    assert len(factory().list_jobs()) == (mode == "after")


@pytest.mark.parametrize("mode", ["before", "after"])
def test_attempt_commit_unknown_stops_source(setup, mode):
    server, factory, engine, _, _ = setup
    submit(engine.journal)
    server.fail_commit = mode
    source = Mock()
    with pytest.raises(PgJournalUnavailable):
        engine.run(
            job_identity(), enabled=True, preflight=lambda _: None, acquire=source
        )
    source.assert_not_called()
    assert factory().get(job_identity())["attempts"] == (mode == "after")


@pytest.mark.parametrize(
    "classification",
    ["source_shape_rejected", "credential_echo_rejected", "transport_unknown"],
)
def test_failure_persisted_once_never_success_or_retry(setup, classification):
    server, factory, engine, store, _ = setup
    submit(engine.journal)
    source = Mock(side_effect=PceAcquisitionRejected(classification))
    result = engine.run(
        job_identity(), enabled=True, preflight=lambda _: None, acquire=source
    )
    assert result["state"] == "failed" and result["classification"] == classification
    assert result == factory().get(job_identity())
    assert server.data["attempts"][job_identity()] == result
    engine.run(job_identity(), enabled=True, preflight=lambda _: None, acquire=source)
    assert source.call_count == 1 and not store._objects


def test_preflight_failure_persists_attempt_without_provider(setup):
    _, factory, engine, _, _ = setup
    submit(engine.journal)
    source = Mock()
    result = engine.run(
        job_identity(),
        enabled=True,
        preflight=Mock(side_effect=RuntimeError("PRIVATE")),
        acquire=source,
    )
    assert result["state"] == "failed" and result["failure_stage"] == "preflight"
    assert factory().get(job_identity())["attempts"] == 1
    assert "PRIVATE" not in json.dumps(result)
    source.assert_not_called()


def test_interrupted_worker_does_not_resume_or_read_credentials(setup):
    _, factory, _, _, _ = setup
    journal = factory()
    submit(journal)
    with journal.executor(job_identity()):
        start(journal)
    host = object.__new__(DevHost)
    host.journal = factory
    host.connection = Mock(side_effect=AssertionError("writer forbidden"))
    execution = live.ExecutionIdentity(
        job_identity(),
        "a" * 40,
        "b" * 64,
        job_identity() + "-a1",
        "offline-observation",
    )
    result = host.worker_once(execution, enabled=True)
    assert result["state"] == "outcome_unknown" and result["attempts"] == 1
    assert result["classification"] == "interrupted_attempt"
    host.connection.assert_not_called()


def test_acquirer_failure_only_safe_observation_artifact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    identity = live.ExecutionIdentity(
        job_identity(), "a" * 40, "b" * 64, job_identity() + "-a1", "offline"
    )
    acquire = Mock(return_value=live.AcquisitionResult("source_shape_rejected"))
    monkeypatch.setattr(live, "acquire", acquire)
    acquirer = LivePceAcquirer(identity, Path("artifacts/one"), lambda: BASE)
    with pytest.raises(PceAcquisitionRejected, match="source_shape_rejected"):
        acquirer()
    acquire.assert_called_once_with(identity, enabled=True, provider_budget=2)
    assert [p.name for p in Path("artifacts/one").iterdir()] == ["observations.json"]
    with pytest.raises(FileExistsError):
        acquirer()
    assert acquire.call_count == 1


def test_no_source_qualification_mark_changed_and_ddl_exact_binding():
    from market_data_foundation.historical.bea_pce import PCE_BINDING_CANDIDATE

    assert "candidate_only_not_active" in PCE_BINDING_CANDIDATE.limitations
    ddl = Path("db/candidates/b3_bea_pce_jobs_r1.sql").read_text()
    assert json.dumps(BINDING.document(), sort_keys=True) in ddl
    assert "CREATE SCHEMA" not in ddl and "IF NOT EXISTS" in ddl.splitlines()[2]
    assert "TO odin_dba" in ddl and "CREATE ROLE" not in ddl


@pytest.mark.parametrize("owner_metadata", [False, True])
def test_live_adapter_replays_clean_result_and_never_retains_request(
    tmp_path, monkeypatch, owner_metadata
):
    """Fabricated IPC result; owner marker here tests wiring, never provenance."""
    from datetime import UTC, datetime

    from test_bea_pce_live import DUMMY, CapturingPipe, SyntheticTransport

    from market_data_foundation.historical import bea_pce as pce

    identity = live.ExecutionIdentity(
        job_identity(), "a" * 40, "b" * 64, job_identity() + "-a1", "offline"
    )
    pipe, output, sizes = CapturingPipe(), bytearray(live.OUTPUT_LIMIT), [0, 0]
    live._worker(pipe, output, sizes, identity, SyntheticTransport(), lambda: DUMMY)
    assert pipe.frames[-1] == b"candidate"
    projection = bytes(output[: sizes[0]])
    receipt = pce.decode(bytes(output[sizes[0] : sizes[0] + sizes[1]]))
    if owner_metadata:
        receipt["execution_class"] = "owner_local_candidate"
    result = live.AcquisitionResult(
        "candidate_pending_independent_readback",
        projection=projection,
        receipt=pce.encode(receipt),
    )
    acquire = Mock(return_value=result)
    monkeypatch.setattr(live, "acquire", acquire)
    monkeypatch.chdir(tmp_path)
    adapter = LivePceAcquirer(
        identity, Path("artifacts/adapter"), lambda: datetime(2026, 10, 25, tzinfo=UTC)
    )
    if owner_metadata:
        retained = adapter()
        assert retained.plan.version.identity == job_identity() + ":version"
        assert retained.plan.acquisition.external_receipt.endswith("/receipt.json")
        assert retained.plan.run.budget == 0  # Existing retained canonical workflow.
        assert pce.decode(retained.plan.content)["data"]["release_at"] is None
        files = list(Path("artifacts/adapter").iterdir())
        assert len(files) == 3
        for file in files:
            body = file.read_bytes()
            assert DUMMY.encode() not in body and b"RequestParam" not in body
    else:
        with pytest.raises(PceAcquisitionRejected):
            adapter()
        assert not Path("artifacts/adapter/receipt.json").exists()
    assert acquire.call_count == 1


def test_journal_cannot_downgrade_tls(setup):
    _, factory, _, _, _ = setup
    journal = factory()
    with pytest.raises(ContractError, match="verify_full"):
        PostgresJobJournal(
            Mock(),
            replace(journal.profile, sslmode="require"),
            journal.identity,
            BINDING,
            enabled=True,
        )


def test_canonical_writer_cannot_escape_fixed_database(setup):
    from market_data_foundation.canonical.kernel import CanonicalKernel
    from market_data_foundation.canonical.postgres import PostgresLedger
    from market_data_foundation.historical.pce_local_job import PceLocalJob

    server, factory, engine, _, _ = setup
    journal = factory()
    writer = server.connect(**journal.profile.connection_kwargs(), autocommit=True)
    writer.info.dbname = "other_database"
    with pytest.raises(ContractError, match="target_not_admitted"):
        PceLocalJob(
            journal,
            engine.clock,
            CanonicalKernel(PostgresLedger(writer, SCHEMA), engine.clock),
            engine.store,
            engine.protection,
            engine.readback,
            engine.commit,
        )


def test_dev_18760_preview_root_redirect_preserves_location():
    """Offline proof that / redirects to /pce instead of an empty 307 page."""
    import runpy
    from io import BytesIO

    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/mdf_b3_pce_dev.py")
    )
    handler_type = namespace["PreviewHandler"]

    def fake(method="GET"):
        h = handler_type.__new__(handler_type)
        h.command = method
        h.wfile = BytesIO()
        h.emitted = []
        h.send_response = lambda status: h.emitted.append(("Status", status))
        h.send_header = lambda key, value: h.emitted.append((key, value))
        h.end_headers = lambda: None
        return h

    for method in ("GET", "HEAD"):
        h = fake(method)
        h.respond(307, b"", location="/pce")
        assert ("Status", 307) in h.emitted
        assert ("Location", "/pce") in h.emitted
        assert ("Content-Length", "0") in h.emitted
        assert h.close_connection

    h = fake()
    h.respond(200, b"OK", content_type="text/plain")
    assert h.wfile.getvalue() == b"OK"
    assert not any(k == "Location" for k, _ in h.emitted)

    for invalid in (
        None,
        "//other",
        "https://other",
        "/api/pce/jobs",
        "/pce\r\nInjected: 1",
    ):
        h = fake()
        with pytest.raises(ContractError):
            h.respond(307, b"", location=invalid)


def fail_initial(factory, state="failed"):
    journal = factory()
    submit(journal)
    with journal.executor(job_identity()):
        start(journal)
        journal._update(
            job_identity(),
            BASE,
            stage="source_acquisition",
            effects={"provider_logical_reads_possible": 2},
        )
        journal._update(
            job_identity(),
            BASE,
            state=state,
            stage="source_acquisition",
            classification="credential_echo_rejected",
            finished_at=BASE.isoformat(),
        )
    return journal


def test_inherited_scope_constraint_is_a_real_blocker_not_fake_eligibility(setup):
    server, factory, _, store, app = setup
    fail_initial(factory)
    before = copy.deepcopy(server.data)
    with client(app) as c:
        eligibility = c.get("/api/pce/jobs").json()["backfill"]
        assert not eligibility["eligible"]
        assert (
            eligibility["reason"] == "pce_backfill_exact_two_scope_migration_required"
        )
        result = c.post(
            "/api/pce/jobs/backfill",
            json=backfill_submission(),
            headers={"Origin": ORIGIN},
        )
        assert result.status_code == 409
        assert result.json()["classification"] == eligibility["reason"]
    assert server.data == before and not store._objects


def test_backfill_pending_replay_worker_and_parent_immutable(setup):
    server, factory, engine, store, app = setup
    journal = fail_initial(factory)
    server.successor_supported = True  # Hypothetical qualified storage, OFFLINE ONLY.
    original = journal.snapshot(job_identity())
    with client(app) as c:
        assert c.get("/api/pce/jobs").json()["backfill"]["eligible"]
        result = c.post(
            "/api/pce/jobs/backfill",
            json=backfill_submission(),
            headers={"Origin": ORIGIN},
        )
        assert result.status_code == 202
        assert result.json()["parent_job_id"] == job_identity()
        assert result.json()["attempts"] == 0
        assert len(server.data["attempts"]) == 1 and not store._objects
        before = copy.deepcopy(server.data)
        assert (
            c.post(
                "/api/pce/jobs/backfill",
                json=backfill_submission(),
                headers={"Origin": ORIGIN},
            ).status_code
            == 202
        )
        assert server.data == before
        assert not c.get("/api/pce/jobs").json()["backfill"]["eligible"]
        # Initial endpoint still observes precisely the initial failed record.
        assert (
            c.post(
                "/api/pce/jobs", json=submission(), headers={"Origin": ORIGIN}
            ).json()["state"]
            == "failed"
        )

        def acquire():
            doc = factory().get(backfill_identity())
            assert doc["effects"]["provider_logical_reads_possible"] == 2
            assert doc["attempt_id"] == backfill_identity() + "-a1"
            return candidate(backfill_identity())

        source = Mock(side_effect=acquire)
        result = engine.run(
            backfill_identity(), enabled=True, preflight=lambda _: None, acquire=source
        )
        assert result["state"] == "succeeded"
        assert (
            result["reconciliation"]["evidence"]["version_identity"]
            == backfill_identity() + ":version"
        )
        assert source.call_count == 1
        assert (
            c.post(
                "/api/pce/jobs/backfill",
                json=backfill_submission(),
                headers={"Origin": ORIGIN},
            ).status_code
            == 200
        )
        assert journal.snapshot(job_identity()) == original
        assert len(journal.list_jobs()) == len(server.data["attempts"]) == 2
        with (
            journal.executor(job_identity()),
            pytest.raises(ContractError, match="initial_failed_immutable"),
        ):
            journal._update(job_identity(), BASE, classification="changed")


@pytest.mark.parametrize(
    "state", ["outcome_unknown", "pending", "succeeded", "cancelled"]
)
def test_backfill_rejects_nonfailed_parent(setup, state):
    server, factory, _, _, app = setup
    server.successor_supported = True
    if state == "pending":
        submit(factory())
    else:
        fail_initial(factory, state)
    before = copy.deepcopy(server.data)
    with client(app) as c:
        assert (
            c.post(
                "/api/pce/jobs/backfill",
                json=backfill_submission(),
                headers={"Origin": ORIGIN},
            ).status_code
            == 409
        )
    assert server.data == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("reference", "2026-08"),
        ("parent_job_id", "foreign"),
        ("job_identity", "third"),
        ("confirmation", ""),
    ],
)
def test_backfill_exact_request_only(setup, field, value):
    server, factory, _, _, app = setup
    fail_initial(factory)
    server.successor_supported = True
    body = backfill_submission() | {field: value}
    before = copy.deepcopy(server.data)
    with client(app) as c:
        assert (
            c.post(
                "/api/pce/jobs/backfill", json=body, headers={"Origin": ORIGIN}
            ).status_code
            == 422
        )
    assert server.data == before


def test_backfill_concurrent_submissions_one_slot_and_failed_successor_no_third(setup):
    server, factory, _, _, _ = setup
    journal = fail_initial(factory)
    server.successor_supported = True
    original = journal.snapshot(job_identity())
    spec = candidate_spec()

    def submit_one(_):
        return factory().submit_backfill(
            backfill_identity(), job_identity(), spec.document(), spec.scope, BASE
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit_one, range(16)))
    assert all(r == results[0] for r in results)
    assert len(journal.history(backfill_identity())) == 1
    with journal.executor(backfill_identity()):
        journal._update(
            backfill_identity(),
            BASE,
            state="running",
            attempts=1,
            attempt_id=backfill_identity() + "-a1",
            started_at=BASE.isoformat(),
        )
        journal._update(
            backfill_identity(), BASE, state="failed", classification="source_rejected"
        )
    before = copy.deepcopy(server.data)
    assert submit_one(None)["state"] == "failed"
    for identity, parent in [
        ("third", job_identity()),
        (backfill_identity(), backfill_identity()),
    ]:
        with pytest.raises(ContractError):
            journal.submit_backfill(identity, parent, spec.document(), spec.scope, BASE)
    assert server.data == before
    assert journal.snapshot(job_identity()) == original


@pytest.mark.parametrize("mode", ["before", "after"])
def test_backfill_ambiguous_commit_never_launches_worker_or_changes_identity(
    setup, mode
):
    server, factory, _, store, app = setup
    fail_initial(factory)
    server.successor_supported = True
    server.fail_commit = mode
    with client(app) as c:
        result = c.post(
            "/api/pce/jobs/backfill",
            json=backfill_submission(),
            headers={"Origin": ORIGIN},
        )
        assert result.status_code == 503
        jobs = c.get("/api/pce/jobs").json()["jobs"]
        assert len(jobs) == (2 if mode == "after" else 1)
    assert len(server.data["attempts"]) == 1 and not store._objects


def test_successor_host_entry_requires_persisted_job_opt_in_and_exact_identity(
    setup, monkeypatch
):
    import market_data_foundation.web.pce_dev_host as host_module

    server, factory, _, _, _ = setup
    fail_initial(factory)
    server.successor_supported = True
    host = object.__new__(DevHost)
    host.journal = factory
    host.connection = Mock(side_effect=AssertionError("writer forbidden"))
    execution = live.ExecutionIdentity(
        backfill_identity(),
        "a" * 40,
        "b" * 64,
        backfill_identity() + "-a1",
        "offline-successor",
    )
    with pytest.raises(ContractError, match="opt_in"):
        host.worker_once(execution)
    with pytest.raises(ContractError, match="job_not_found"):
        host.worker_once(execution, enabled=True)
    host.connection.assert_not_called()
    spec = candidate_spec()
    journal = factory()
    journal.submit_backfill(
        backfill_identity(), job_identity(), spec.document(), spec.scope, BASE
    )
    # Check actual Host dispatch only, without calling prepare (secret IO) or
    # the live acquirer. Engine behavior is covered by the domain tests above.
    writer = Mock()
    host.connection = Mock(return_value=nullcontext(writer))
    worker = Mock()
    worker.run.return_value = journal.get(backfill_identity())
    monkeypatch.setattr(host_module, "PostgresLedger", Mock())
    monkeypatch.setattr(host_module, "CanonicalKernel", Mock())
    monkeypatch.setattr(host_module, "PostgresAdvisoryWriterProtection", Mock())
    monkeypatch.setattr(host_module, "PceLocalJob", Mock(return_value=worker))
    assert host.worker_once(execution, enabled=True)["state"] == "pending"
    assert worker.run.call_args.args == (backfill_identity(),)
    acquirer = worker.run.call_args.kwargs["acquire"]
    assert type(acquirer) is LivePceAcquirer
    assert acquirer.identity == execution
    assert acquirer.artifact_root.name == backfill_identity() + "-a1"
    writer.rollback.assert_called_once()
    assert journal.get(backfill_identity())["attempts"] == 0


def test_successor_interrupted_host_is_unknown_and_cannot_restart(setup):
    server, factory, _, _, _ = setup
    journal = fail_initial(factory)
    server.successor_supported = True
    spec = candidate_spec()
    journal.submit_backfill(
        backfill_identity(), job_identity(), spec.document(), spec.scope, BASE
    )
    with journal.executor(backfill_identity()):
        journal._update(
            backfill_identity(),
            BASE,
            state="running",
            attempts=1,
            attempt_id=backfill_identity() + "-a1",
            started_at=BASE.isoformat(),
        )
    host = object.__new__(DevHost)
    host.journal = factory
    host.connection = Mock(side_effect=AssertionError("writer forbidden"))
    execution = live.ExecutionIdentity(
        backfill_identity(), "a" * 40, "b" * 64, backfill_identity() + "-a1", "offline"
    )
    result = host.worker_once(execution, enabled=True)
    assert result["state"] == "outcome_unknown"
    before = copy.deepcopy(server.data)
    assert host.worker_once(execution, enabled=True) == result
    assert (
        journal.submit_backfill(
            backfill_identity(), job_identity(), spec.document(), spec.scope, BASE
        )
        == result
    )
    assert server.data == before
    host.connection.assert_not_called()


@pytest.mark.parametrize("corrupt", ["event", "attempt"])
def test_backfill_cannot_use_inconsistent_parent_history(setup, corrupt):
    server, factory, _, _, app = setup
    fail_initial(factory)
    server.successor_supported = True
    if corrupt == "event":
        server.data["events"][job_identity()].pop(4)
    else:
        server.data["attempts"].pop(job_identity())
    before = copy.deepcopy(server.data)
    with client(app) as c:
        assert (
            c.post(
                "/api/pce/jobs/backfill",
                json=backfill_submission(),
                headers={"Origin": ORIGIN},
            ).status_code
            == 409
        )
    assert server.data == before


@pytest.mark.parametrize("variant", ["third-scope", "swapped-parent-child"])
def test_backfill_constraint_rejects_broadened_or_swapped_scope(setup, variant):
    server, factory, _, store, _ = setup
    journal = fail_initial(factory)
    old = copy.deepcopy(server.data)
    server.successor_supported = True
    if variant == "third-scope":
        extra = " OR (scope = 'unauthorized-third-scope'::text)"
    else:
        extra = ""
    # Obtain the fixture's legitimate exact constraint text, then create
    # an adversarial check that still contains all identity/scope keywords.
    with journal._transaction() as c:
        text = dict(
            c.execute(
                "SELECT conname, pg_catalog.pg_get_constraintdef(oid) "
                "FROM pg_catalog.pg_constraint WHERE conrelid=%s::regclass "
                "AND conname IN ('jobs_scope_key','jobs_scope_check')",
                (f"{SCHEMA}.jobs",),
            ).fetchall()
        )["jobs_scope_check"]
    if variant == "third-scope":
        server.scope_constraint_override = text[:-1] + extra + ")"
    else:
        server.scope_constraint_override = (
            text.replace(
                f"scope = '{DOMAIN}'::text",
                "scope = '__temporary__'::text",
            )
            .replace(
                f"scope = '{BACKFILL_SCOPE}'::text",
                f"scope = '{DOMAIN}'::text",
            )
            .replace(
                "scope = '__temporary__'::text",
                f"scope = '{BACKFILL_SCOPE}'::text",
            )
        )
    assert (
        journal.backfill_eligibility()
        == "pce_backfill_exact_two_scope_migration_required"
    )
    assert server.data == old and not store._objects
