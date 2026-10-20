"""Mixed-domain durable plans: HTTP and endpoint regressions without external IO."""

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from test_cpi_scheduling import no_external_io, setup, spec, tick  # noqa: F401

from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.cpi_jobs import Operator
from market_data_foundation.web.cpi_schedules import (
    Backfill,
    CpiSchedulesHost,
    PlanAction,
    PlanIntent,
)
from tests.r5_jobs_fixture import jobs_host

DOMAINS = ("synthetic", "owner_nonproduction")
BASE = "/api/r5/cpi/plans"


@pytest.fixture(params=["endpoint", "http"])
def exchange(request):
    """Keep the real ASGI gate separate from direct route/DTO execution."""
    clients = []

    def connect(app):
        if request.param == "http":
            client = TestClient(app)
            clients.append(client)

            def send(method, path, body=None):
                response = client.request(
                    method, path, **({"json": body} if body is not None else {})
                )
                return response.status_code, response.json()

            return send

        def send(method, path, body=None):
            if method == "GET":
                operation, args = "cpi_plans", ()
            elif path == BASE:
                operation, args = "cpi_plan_accept", (PlanIntent(**body),)
            else:
                identity, action = path.removeprefix(BASE + "/").split("/")
                operation = "cpi_plan_" + action
                args = (
                    identity,
                    (PlanAction if action == "action" else Backfill)(**body),
                )
            route = next(
                r for r in app.routes if getattr(r, "operation_id", None) == operation
            )
            value = route.endpoint(Request({"type": "http", "headers": []}), *args)
            if hasattr(value, "status_code"):
                return value.status_code, json.loads(value.body)
            return (200 if method == "GET" else 202), value.model_dump()

        return send

    yield connect
    for client in clients:
        client.close()


@pytest.fixture
def mixed(tmp_path):
    rig, store, worker, transport, clock = setup(tmp_path)
    transport.failure = True
    for domain in DOMAINS:
        store.accept(domain.replace("_", "-"), spec(effect_domain=domain), clock[0])
        # Real finite offline worker produces a failed child in each domain.
        tick(worker, clock, domain.replace("_", "-"))
    assert {d["spec"]["effect_domain"] for d in store.snapshot()} == set(DOMAINS)
    assert all(
        d["batches"][0]["jobs"][0]["state"] == "failed" for d in store.snapshot()
    )
    return rig, store, transport, clock


def app_for(mixed, domain, operator):
    rig, store, _, clock = mixed
    fixture = jobs_host(rig)
    host = replace(
        fixture,
        spec=replace(fixture.spec, effect_domain=domain),
        evidence_mode="OFFLINE_FIXTURE" if domain == "synthetic" else "AUTHORIZED_HOST",
    )
    host = replace(host, authorize=lambda _request: operator, clock=lambda: clock[0])
    return create_app(
        schedules_host=CpiSchedulesHost(host, store, rig.acquirer.history_qualification)
    )


def durable_state(mixed):
    rig, _, transport, _ = mixed
    with rig.journal._connection() as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        rows = {
            name: connection.execute(f'SELECT * FROM "{name}"').fetchall()
            for (name,) in tables
        }
    files = {
        p.relative_to(rig.journal.directory): p.read_bytes()
        for p in rig.journal.directory.rglob("*")
        if p.is_file()
    }
    return (
        rows,
        files,
        list(transport.calls),
        rig.store.create_attempts,
        rig.ledger.snapshot(),
    )


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("writable", [False, True])
def test_mixed_domain_read_projection(exchange, mixed, domain, writable):
    send = exchange(app_for(mixed, domain, Operator("reader", writable)))
    before = durable_state(mixed)
    status, body = send("GET", BASE)
    assert status == 200 and body["status"] == "OK"
    assert body["write_eligible"] is writable
    assert [p["identity"] for p in body["plans"]] == [domain.replace("_", "-")]
    foreign = next(d for d in DOMAINS if d != domain).replace("_", "-")
    assert foreign not in json.dumps(body)
    assert durable_state(mixed) == before


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("operation", ["pause", "resume", "cancel", "backfill"])
def test_cross_domain_control_has_no_effect_or_state_oracle(
    exchange, mixed, domain, operation
):
    send = exchange(app_for(mixed, domain, Operator("writer", True)))
    foreign = next(d for d in DOMAINS if d != domain).replace("_", "-")
    suffix = "backfill" if operation == "backfill" else "action"
    body = (
        {"start": "2024-12", "end": "2024-12"}
        if operation == "backfill"
        else {"action": operation}
    )
    before = durable_state(mixed)
    denied = send("POST", f"{BASE}/{foreign}/{suffix}", body)
    assert denied == (
        409,
        {"status": "NOT_AVAILABLE", "classification": "cpi_plan_not_found"},
    )
    assert send("POST", f"{BASE}/absent/{suffix}", body) == denied
    assert durable_state(mixed) == before
    # Terminal foreign state must produce exactly the same public rejection.
    mixed[1].action(foreign, "cancel", mixed[3][0])
    before = durable_state(mixed)
    assert send("POST", f"{BASE}/{foreign}/{suffix}", body) == denied
    assert durable_state(mixed) == before


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("writable", [False, True])
def test_same_domain_create_backfill_and_actions(exchange, mixed, domain, writable):
    send = exchange(app_for(mixed, domain, Operator("operator", writable)))
    before = durable_state(mixed)
    identity = domain.replace("_", "-")
    intent = {
        **spec().document(),
        "plan_identity": "new-plan",
        "asset_identity": "mdf.macro.bls.cpi.headlines",
        "asset_revision": "p3-r1",
    }
    intent.pop("effect_domain")
    calls = [
        (BASE, intent),
        (f"{BASE}/{identity}/backfill", {"start": "2024-12", "end": "2024-12"}),
    ]
    calls += [
        (f"{BASE}/{identity}/action", {"action": action})
        for action in ("pause", "resume", "cancel")
    ]
    for path, body in calls:
        status, result = send("POST", path, body)
        assert status == (202 if writable else 403)
        assert result["status"] == ("OK" if writable else "NOT_AVAILABLE")
    if writable:
        assert mixed[1].get("new-plan")["spec"]["effect_domain"] == domain
        assert mixed[1].get(identity)["state"] == "cancelled"
        assert mixed[1].get(identity)["retry_queue"] == ["0:0"]
        assert durable_state(mixed)[2:] == before[2:]
    else:
        assert durable_state(mixed) == before


@pytest.mark.parametrize(
    "operator", [None, {"identity": "spoofed", "write_eligible": True}]
)
def test_untrusted_operator_cannot_read_or_mutate(exchange, mixed, operator):
    send = exchange(app_for(mixed, "synthetic", operator))
    before = durable_state(mixed)
    assert send("GET", BASE) == (
        403,
        {"status": "NOT_AVAILABLE", "classification": "operator_not_authorized"},
    )
    for identity in ("synthetic", "owner-nonproduction", "absent"):
        for suffix, body in (
            ("action", {"action": "cancel"}),
            ("backfill", {"start": "2024-12", "end": "2024-12"}),
        ):
            assert send("POST", f"{BASE}/{identity}/{suffix}", body)[0] == 403
    assert durable_state(mixed) == before
