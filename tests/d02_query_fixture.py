"""Explicit synthetic D02 assembly: real domain validation, no live clients."""

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from market_data_foundation.canonical.codec import dumps
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import Grade, Kind
from market_data_foundation.historical import bls_cpi_direct as direct
from market_data_foundation.historical import cpi_history
from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.cpi_query import (
    CpiQueryAccess,
    CpiQueryBinding,
    CpiQueryHost,
    CpiSourceAdmission,
    CpiSourceResponse,
)
from market_data_foundation.web.package_catalog import SaveCommand
from tests.d01_package_fixture import fixture_host
from tests.r5_fixture import BASE, ReadOnlySnapshot, fixture_ledger
from tests.test_cpi_scheduling import raw_indices
from tests.test_p4_bls_cpi_direct import encode, september_response

AT = datetime(2026, 10, 27, 8, tzinfo=UTC)
SCENARIOS = {
    "local_hit",
    "local_other_reference",
    "miss",
    "partial",
    "stale",
    "storage_failure",
    "scope_exit_failure",
    "content_invalid",
    "missing_content",
    "corrupt_content",
    "read_denied",
    "read_unknown",
    "metadata_denied",
    "metadata_unknown",
    "source_disabled",
    "source_unknown",
    "credential_missing",
    "credential_unknown",
    "source_failure",
    "source_partial",
    "unconfigured",
    "source_not_qualified",
    "source_scope_denied",
    "source_permission_unknown",
    "query_permission_denied",
    "config_failure",
    "withdrawn",
    "unpublished",
    "ambiguous",
    "lineage_invalid",
    "source_bad_time",
    "source_secret_error",
}


def fixture(root, scenario="local_hit"):
    if scenario not in SCENARIOS:
        raise ValueError("unknown_synthetic_scenario")
    local_cases = {
        "local_hit",
        "local_other_reference",
        "partial",
        "stale",
        "source_disabled",
        "storage_failure",
        "scope_exit_failure",
        "content_invalid",
        "missing_content",
        "corrupt_content",
        "withdrawn",
        "unpublished",
        "ambiguous",
        "lineage_invalid",
    }
    seed = {
        "missing_content": "missing_content",
        "corrupt_content": "corrupt_content",
        "withdrawn": "withdrawn",
        "ambiguous": "ambiguous",
        "partial": "partial",
    }.get(scenario, "available" if scenario in local_cases else "empty")
    ledger, plan, publication = fixture_ledger(seed)
    if scenario == "local_other_reference":
        other = direct.build_offline_replay_plan(
            encode(september_response()),
            identity="d02-other-reference",
            started_at=BASE,
            finished_at=BASE + timedelta(seconds=5),
            frozen_at=BASE + timedelta(seconds=6),
            reference_contract=direct.SEPTEMBER_REFERENCE_CONTRACT,
        )
        kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=7))
        direct.persist_offline_replay(kernel, other)
        direct.publish_offline_replay(kernel, other)
    if scenario == "unpublished":
        ledger._records.pop((Kind.PUBLICATION.value, publication.key.identity, ""))
        ledger._publication_occurrences.clear()
    if scenario == "lineage_invalid":
        ledger._records.pop(
            (
                plan.source.key.kind.value,
                plan.source.key.identity,
                plan.source.key.revision,
            )
        )
    if scenario == "content_invalid":
        from market_data_foundation.canonical.model import digest

        content = json.loads(plan.content)
        content["data"]["measurements"][0]["unit"] = "secret-invalid-unit"
        raw = encode(content)
        version = replace(plan.version, content_digest=digest(raw))
        ledger._records[(Kind.VERSION.value, version.identity, "")] = dumps(version)
        ledger._contents[version.identity] = (version.content_digest, raw)
    state = SimpleNamespace(
        source=0, scope_open=False, requests=[], ledger=ledger, plan=plan
    )
    state.before = ledger.snapshot()
    root.mkdir(parents=True, exist_ok=True)
    packages = fixture_host(root / "configuration")
    packages.store.save(
        "synthetic-d01",
        "macro",
        SaveCommand(expected_revision=None, enabled_capability_ids=["cpi"]),
    )
    if scenario in local_cases:
        original_history = packages.history

        def retained_history(environment_id, package_id, record):
            history = original_history(environment_id, package_id, record)
            return history.model_copy(
                update={
                    "history": [
                        h.model_copy(update={"holding": "present"})
                        if h.definition_ref.identity == "mdf.macro.bls.cpi.headlines"
                        else h
                        for h in history.history
                    ]
                }
            )

        packages.history = retained_history
    if scenario == "source_disabled":
        packages.store.save(
            "synthetic-d01",
            "macro",
            SaveCommand(expected_revision="r1", enabled_capability_ids=[]),
        )
    if scenario == "config_failure":
        packages.store.path.write_text("broken synthetic config")

    @contextmanager
    def scope():
        if scenario == "storage_failure":
            raise RuntimeError("sensitive-storage-error")
        state.scope_open = True
        try:
            yield ReadOnlySnapshot(ledger)
        finally:
            state.scope_open = False
        if scenario == "scope_exit_failure":
            raise RuntimeError("sensitive-close-error")

    def authorize(_request):
        return CpiQueryAccess(
            metadata_permission="denied"
            if scenario == "metadata_denied"
            else "unknown"
            if scenario == "metadata_unknown"
            else "allowed",
            read_permission="denied"
            if scenario == "read_denied"
            else "unknown"
            if scenario == "read_unknown"
            else "allowed",
        )

    def fetch(request):
        assert not state.scope_open
        assert not hasattr(request, "put")
        state.source += 1
        state.requests.append(request)
        if scenario in {"source_failure", "source_secret_error"}:
            raise RuntimeError("credential=super-secret; sensitive upstream payload")
        if request.source.key == cpi_history.CPI_HISTORY_INDEX_SOURCE.key:
            reference = request.reference
            year = int(reference[:4])
            content = {
                "schema": cpi_history.RULE,
                "data": cpi_history.project(raw_indices(year - 1, year), reference),
                "request": cpi_history.request(reference),
                "obtained_at": (AT + timedelta(seconds=1)).isoformat(),
                "provider_released_at": None,
                "provider_vintage": None,
                "provider_revision": {"state": "not_declared", "value": None},
                "mode": "historical_index_observation",
                "historical_as_of_verified": False,
            }
        else:
            content = json.loads(plan.content)
            if request.reference == "2026-09":
                content["data"] = direct.parse_response(
                    encode(september_response()),
                    reference=direct.SEPTEMBER_REFERENCE_CONTRACT.semantics,
                ).data
            content["obtained_at"] = (AT + timedelta(seconds=1)).isoformat()
        if scenario == "source_partial":
            content["data"]["measurements"].pop()
        if scenario == "source_bad_time":
            content["obtained_at"] = "2026-10-01T00:00:00"
        return CpiSourceResponse(
            encode(content), frozenset({Grade.ORIGINAL, Grade.PROJECTED})
        )

    admission = CpiSourceAdmission(
        qualified=None
        if scenario == "source_unknown"
        else scenario != "source_not_qualified",
        credential_state="missing"
        if scenario == "credential_missing"
        else "unknown"
        if scenario == "credential_unknown"
        else "configured",
        source_permission="unknown"
        if scenario == "source_permission_unknown"
        else "allowed",
        query_permission="denied"
        if scenario == "query_permission_denied"
        else "allowed",
        references=()
        if scenario == "source_scope_denied"
        else ("2026-08", "2026-09", "2025-06"),
    )
    contracts = (
        (direct.BLS_CPI_DIRECT_SOURCE, direct.BLS_CPI_DIRECT_BINDING_CANDIDATE),
        (
            direct.BLS_CPI_SEPTEMBER_RELEASE_SOURCE_CANDIDATE,
            direct.BLS_CPI_SEPTEMBER_RELEASE_BINDING_CANDIDATE,
        ),
        cpi_history.source_binding(),
    )
    host = CpiQueryHost(
        scope,
        authorize,
        packages,
        tuple(CpiQueryBinding(s, b, admission, fetch) for s, b in contracts),
        lambda: AT,
        lambda _view, _at: "stale" if scenario == "stale" else "satisfied",
    )
    state.host = host
    state.scope = scope
    app = create_app(
        scope,
        query_host=None if scenario == "unconfigured" else host,
        package_host=packages,
        evidence_mode="OFFLINE_FIXTURE",
    )
    return app, state


def effects(state):
    # Fixture construction precedes the baseline; these are query-time increments.
    after = json.loads(state.ledger.snapshot())
    before = json.loads(state.before)

    def count(snapshot, kind):
        return sum(key[0] == kind for key, _value in snapshot["records"])

    return {
        "source": state.source,
        "jobs": 0,
        "versions": count(after, Kind.VERSION.value)
        - count(before, Kind.VERSION.value),
        "publications": count(after, Kind.PUBLICATION.value)
        - count(before, Kind.PUBLICATION.value),
    }
