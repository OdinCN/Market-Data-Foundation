"""Rebuild canonical CPI fixtures with existing domain admission, never Jobs/IO.

These values are synthetic. The reusable P6 fixture constructs source lineage
in memory, without invoking that source, its transport, material store or PG.
"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

from market_data_foundation.canonical.codec import dumps
from market_data_foundation.canonical.model import Decision, Kind
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.web.cpi_api import create_app
from tests.test_p6_consumer_read import BASE, _seed_bound_cpi

AT = (BASE + timedelta(seconds=10)).isoformat()


def fixture_ledger(scenario="available"):
    ledger, plan, publication = _seed_bound_cpi()
    if scenario == "empty":
        ledger = InMemoryLedger()
        ledger.put(BLS_CPI_HEADLINES)
    elif scenario == "missing_content":
        ledger._contents.clear()
    elif scenario == "corrupt_content":
        ledger._contents[plan.version.identity] = (
            plan.version.content_digest,
            b"invalid",
        )
    elif scenario == "withdrawn":
        terminal = replace(
            publication,
            request=replace(
                publication.request,
                identity="synthetic-withdrawal",
                occurrence="synthetic-withdrawal",
                decision=Decision.WITHDRAW,
                predecessor=publication.key,
            ),
            decision_at=BASE + timedelta(seconds=5),
            usable_at=BASE + timedelta(seconds=5),
        )
        ledger.put(terminal)
    elif scenario == "ambiguous":
        other = replace(plan.version, identity="synthetic-second-version")
        ledger.put(other)
        ledger.put_content(other, plan.content)
        ledger.put(
            replace(
                publication,
                request=replace(
                    publication.request,
                    identity="synthetic-second-publication",
                    occurrence="synthetic-second-publication",
                    version=other.key,
                ),
            )
        )
    elif scenario == "partial":
        from market_data_foundation.canonical.model import FiniteScope

        partial = replace(
            plan.version,
            finite_scope=FiniteScope((plan.version.finite_scope.members[0],)),
        )
        ledger._records[(Kind.VERSION.value, partial.identity, "")] = dumps(partial)
    elif scenario == "over_budget":
        for index in range(129):
            ledger.put(replace(plan.version, identity=f"synthetic-overflow-{index}"))
    elif scenario != "available":
        raise ValueError("unknown_fixture_scenario")
    return ledger, plan, publication


class ReadOnlySnapshot:
    """No mutators; a detached snapshot supports repeatable reads per request."""

    def __init__(self, ledger):
        self._ledger = InMemoryLedger()
        self._ledger._records = ledger._records.copy()
        self._ledger._contents = ledger._contents.copy()
        self._ledger._publication_occurrences = ledger._publication_occurrences.copy()

    def get(self, key):
        return self._ledger.get(key)

    def get_content(self, key):
        return self._ledger.get_content(key)

    def publications_for_version(self, key):
        return self._ledger.publications_for_version(key)

    def versions_for_definition(self, key):
        return self._ledger.versions_for_definition(key)

    def operator_reads(self):
        return self._ledger.operator_reads()


def fixture_app(scenario="available"):
    if scenario == "unconfigured":
        return create_app()
    ledger, _, _ = fixture_ledger(scenario)

    @contextmanager
    def scope():
        yield ReadOnlySnapshot(ledger)

    return create_app(scope, evidence_mode="OFFLINE_FIXTURE")
