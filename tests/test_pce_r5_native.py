"""Offline native composition: real API/domain paths, synthetic PG protocol only."""

import copy
import socket
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_bea_pce_vertical import BASE, publish

from market_data_foundation.historical.pce_pg_binding import BINDING
from market_data_foundation.web.cpi_dev_host import candidate_spec, job_identity
from market_data_foundation.web.cpi_jobs import CpiManualWorker
from market_data_foundation.web.pce_api import create_preview, submission
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN, ORIGIN
from market_data_foundation.web.pce_r5_host import create_native_preview
from tests.b3_dev_fixture import fixture
from tests.r5_fixture import AT, ReadOnlySnapshot, fixture_ledger


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("native R5 offline test attempted external IO")

    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


def empty_journal():
    journal = Mock(binding=BINDING)
    journal.reader.return_value.snapshot.return_value = []
    return journal


def client(app, origin=CANDIDATE_ORIGIN):
    return TestClient(app, base_url=origin, client=("192.168.10.25", 12345))


def test_native_r5_reader_failed_job_and_seven_page_apis_are_preserved(tmp_path):
    server, rig, _ = fixture(tmp_path)
    spec = candidate_spec()
    rig.journal.submit(job_identity(), spec.document(), spec.scope, rig.engine.clock())
    worker = CpiManualWorker(
        "offline-worker",
        rig.engine,
        spec,
        Mock(side_effect=ValueError("synthetic preflight failure")),
        rig.acquirer,
    )
    assert worker.process(job_identity(), enabled=True)["state"] == "failed"
    ledger, plan, _ = fixture_ledger()
    pce = empty_journal()
    # Explicit test scopes only; production uses independently pinned DevHosts.
    cpi_host = SimpleNamespace(
        journal=lambda: rig.journal,
        read_scope=lambda: lambda: nullcontext(ReadOnlySnapshot(ledger)),
    )
    pce_host = SimpleNamespace(journal=lambda: pce, read_scope=Mock())
    before = copy.deepcopy(server.data)
    app = create_native_preview(pce_host, cpi_host, ui_directory=None, enabled=True)
    pce.reader.assert_not_called()
    with client(app) as c:
        for path in ("/api/r5/sources", "/api/r5/observability", "/api/r3/management"):
            assert c.get(path).status_code == 200
        entries = c.get("/api/r5/sources").json()["entries"]
        assert len(entries) == 20
        assert "mdf.source.fred.dol.claims@r1" in {e["identity"] for e in entries}
        jolts = next(e for e in entries if e["identity"] == "mdf.source.bls.jolts@r1")
        assert jolts["asset"]["identity"] == "mdf.macro.bls.jolts"
        assert jolts["binding_contract"]["identity"] == "mdf.binding.bls.jolts"
        assert jolts["source_qualified_contract"] is False
        assert jolts["current_binding"] == jolts["runtime_observation"] == "UNKNOWN"
        assert jolts["web_read"] == "DOMAIN_SPECIFIC_ONLY"
        assert c.get("/api/r5/jolts/timeseries/status").status_code == 404
        bea = next(
            e
            for e in entries
            if e["identity"] == f"{BINDING.source}@{BINDING.source_revision}"
        )
        assert bea["identity"] == f"{BINDING.source}@{BINDING.source_revision}"
        assert bea["binding_contract"]["identity"] == BINDING.binding
        jobs = c.get("/api/r5/cpi/jobs").json()
        assert jobs["jobs"][0]["state"] == "failed"
        assert jobs["write_eligible"] is False
        query = {"reference": "2026-08", "as_of": AT}
        assert c.get("/api/r5/cpi", params=query).json()["status"] == "OK"
        for mode in ("as_of", "exact_version"):
            params = query | {"mode": mode}
            if mode == "exact_version":
                params["version_identity"] = plan.version.identity
            result = c.get("/api/r5/cpi/read", params=params)
            assert result.status_code == 200, result.text
            assert result.json()["version_identity"] == plan.version.identity
        for path in ("/api/r5/cpi/jobs", "/api/r5/cpi/plans", "/api/r3/management"):
            assert c.post(path, json={}, headers={"Origin": ORIGIN}).status_code == 403
    assert server.data == before
    pce.submit.assert_not_called()
    pce_host.read_scope.assert_not_called()
    assert rig.transport.calls == 0


def test_preview_alias_assets_and_old_publication_never_register_a_job(tmp_path):
    ledger, _, result = publish()
    journal = empty_journal()
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text('<div id="root"></div>')
    app = create_preview(
        journal,
        lambda: nullcontext(ledger),
        lambda: BASE + timedelta(seconds=20),
        ui_directory=ui,
        enabled=True,
        origin=CANDIDATE_ORIGIN,
    )
    before = ledger.snapshot()
    with client(app) as c:
        redirect = c.get("/", follow_redirects=False)
        assert redirect.status_code == 307 and redirect.headers["location"] == "/pce"
        assert c.get("/").content == c.get("/pce").content
        metadata = c.get("/api/pce/assets").json()
        assert metadata["source_qualified"] is False
        version = metadata["versions"][0]
        assert version["version_identity"] == result.plan.version.identity
        assert version["publications"]
        assert c.get("/api/pce/jobs").json()["jobs"] == []
        query = {"reference": "2026-07", "as_of": metadata["as_of"]}
        exact = c.get(
            "/api/pce/read", params=query | {"version": version["version_identity"]}
        )
        assert exact.status_code == 200
        assert exact.json() == c.get("/api/pce/read", params=query).json()
    assert ledger.snapshot() == before
    journal.submit.assert_not_called()


def test_candidate_origin_keeps_original_bea_admission_and_exact_post_boundary():
    journal = empty_journal()
    journal.submit.return_value = {"state": "pending"}
    scope = Mock(side_effect=AssertionError("unexpected storage read"))
    app = create_preview(journal, scope, lambda: BASE, enabled=True, origin=ORIGIN)
    with client(app) as c:
        assert c.get("/api/pce/catalog").status_code == 403
    with client(app, ORIGIN) as c:
        for path in (
            "/api/pce/catalog",
            "/api/pce/jobs",
            "/api/pce/read",
            "/api/r5/sources",
        ):
            assert c.get(path, headers={"X-Operator": "owner"}).status_code == 403
        assert (
            c.post(
                "/api/pce/jobs", json=submission(), headers={"Origin": CANDIDATE_ORIGIN}
            ).status_code
            == 403
        )
        assert (
            c.post(
                "/api/pce/jobs?x=1",
                json=submission(),
                headers={"Origin": ORIGIN},
            ).status_code
            == 403
        )
        assert (
            c.post("/api/pce/jobs", json={}, headers={"Origin": ORIGIN}).status_code
            == 422
        )
        journal.submit.assert_not_called()
        assert (
            c.post(
                "/api/pce/jobs", json=submission(), headers={"Origin": ORIGIN}
            ).status_code
            == 202
        )
        journal.submit.assert_called_once()
    scope.assert_not_called()


def test_18761_native_preview_launcher_denies_every_mutation_before_asgi(monkeypatch):
    from pathlib import Path
    from runpy import run_path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    namespace = run_path(str(scripts / "mdf_b3_pce_r5_preview.py"))
    handler_type = namespace["CandidateHandler"]
    calls = []
    handler = handler_type.__new__(handler_type)
    handler.respond = lambda status: calls.append(status)
    handler.dispatch = lambda: pytest.fail("unsafe HTTP mutation delegated to ASGI")
    for method in ("do_POST", "do_PUT", "do_PATCH", "do_DELETE", "do_OPTIONS"):
        getattr(handler, method)()
    assert calls == [403] * 5
    assert handler_type.do_GET is handler_type.dispatch or (
        handler_type.do_GET is handler_type.__mro__[1].dispatch
    )


def test_18761_asgi_denies_exact_pce_post_and_advertises_read_only():
    journal = empty_journal()
    app = create_preview(
        journal, Mock(), lambda: BASE, enabled=True, origin=CANDIDATE_ORIGIN
    )
    with client(app) as c:
        assert c.get("/api/pce/catalog").json()["write_eligible"] is False
        response = c.post(
            "/api/pce/jobs", json=submission(), headers={"Origin": CANDIDATE_ORIGIN}
        )
        assert response.status_code == 403
    journal.submit.assert_not_called()


def test_native_dev_origin_composes_pce_submit_with_read_only_cpi(tmp_path):
    _, rig, _ = fixture(tmp_path)
    cpi_host = SimpleNamespace(
        journal=lambda: rig.journal,
        read_scope=lambda: Mock(),
    )
    journal = empty_journal()
    journal.submit.return_value = {
        "job_id": submission()["job_identity"],
        "state": "pending",
    }
    pce_host = SimpleNamespace(journal=lambda: journal, read_scope=Mock())
    app = create_native_preview(
        pce_host, cpi_host, ui_directory=None, enabled=True, origin=ORIGIN
    )
    with client(app, ORIGIN) as c:
        assert c.get("/api/pce/catalog").json()["write_eligible"] is True
        assert c.get("/api/r5/cpi/jobs").json()["write_eligible"] is False
        response = c.post(
            "/api/pce/jobs", json=submission(), headers={"Origin": ORIGIN}
        )
        assert response.status_code == 202
        assert response.json()["binding"] == BINDING.document()
        for path in ("/api/r5/cpi/jobs", "/api/pce/jobs/worker", "/api/r3/management"):
            assert c.post(path, json={}, headers={"Origin": ORIGIN}).status_code == 403
    journal.submit.assert_called_once()
    pce_host.read_scope.assert_not_called()
    assert rig.transport.calls == 0


def test_native_backfill_boundary_keeps_cpi_and_bea_composite_identities():
    from market_data_foundation.historical.pce_pg_binding import (
        backfill_identity,
    )
    from market_data_foundation.historical.pce_pg_binding import (
        job_identity as pce_initial,
    )
    from market_data_foundation.web.pce_api import backfill_submission

    journal = empty_journal()
    journal.submit_backfill.return_value = {
        "job_id": backfill_identity(),
        "parent_job_id": pce_initial(),
        "state": "pending",
        "attempts": 0,
    }
    scope = Mock(side_effect=AssertionError("no canonical IO from submission"))
    app = create_preview(journal, scope, lambda: BASE, enabled=True, origin=ORIGIN)
    with client(app, ORIGIN) as c:
        before = c.get("/api/r5/sources").json()
        for headers, path in [
            ({"Origin": CANDIDATE_ORIGIN}, "/api/pce/jobs/backfill"),
            ({"Origin": ORIGIN, "X-Operator": "owner"}, "/api/pce/jobs/backfill"),
            ({"Origin": ORIGIN}, "/api/pce/jobs/backfill?reference=2026-07"),
            ({"Origin": ORIGIN}, "/api/pce/jobs/backfill/"),
        ]:
            assert (
                c.post(path, json=backfill_submission(), headers=headers).status_code
                == 403
            )
        journal.submit_backfill.assert_not_called()
        result = c.post(
            "/api/pce/jobs/backfill",
            json=backfill_submission(),
            headers={"Origin": ORIGIN},
        )
        assert result.status_code == 202
        assert journal.submit_backfill.call_args.args[:2] == (
            backfill_identity(),
            pce_initial(),
        )
        assert c.get("/api/r5/sources").json() == before
        assert len(before["entries"]) == 20
        assert "mdf.source.fred.dol.claims@r1" in {
            e["identity"] for e in before["entries"]
        }
        jolts = next(
            e for e in before["entries"] if e["identity"] == "mdf.source.bls.jolts@r1"
        )
        assert jolts["asset"]["identity"] == "mdf.macro.bls.jolts"
        assert jolts["source_qualified_contract"] is False
        assert jolts["current_binding"] == "UNKNOWN"
        assert c.get("/api/r5/jolts/timeseries/status").status_code == 404
        bea = next(
            e
            for e in before["entries"]
            if e["identity"] == f"{BINDING.source}@{BINDING.source_revision}"
        )
        assert bea["identity"] == f"{BINDING.source}@{BINDING.source_revision}"
    readonly = create_preview(
        journal, scope, lambda: BASE, enabled=True, origin=CANDIDATE_ORIGIN
    )
    with client(readonly) as c:
        assert (
            c.post(
                "/api/pce/jobs/backfill",
                json=backfill_submission(),
                headers={"Origin": CANDIDATE_ORIGIN},
            ).status_code
            == 403
        )
    assert journal.submit_backfill.call_count == 1
    scope.assert_not_called()
