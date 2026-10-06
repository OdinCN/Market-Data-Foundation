"""Deterministic P6 consumer-neutral whole-version read contract tests."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from market_data_foundation.canonical.codec import dumps
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
    read_as_of,
    read_exact_version,
)
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    ContractError,
    Decision,
    Grade,
    Key,
    Kind,
    PublicationRequest,
    Scope,
)
from market_data_foundation.canonical.postgres import PostgresLedger, validate_schema
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical import bls_cpi_direct as cpi
from market_data_foundation.historical.daily_bars import (
    TdxDailyReplayBatch,
    TdxStockDailyRawRow,
    build_replay_plan,
    persist_replay_before_publication,
    publish_replay,
)

BASE = datetime(2026, 10, 6, tzinfo=UTC)


def test_postgres_allowlist_adds_only_exact_p6_consumer_read_namespace() -> None:
    schema = "mdf_p6_consumer_read_r1"
    assert validate_schema(schema) == schema
    for unsafe in (schema + "_extra", schema + ";drop"):
        with pytest.raises(ContractError, match="unsafe_postgres_namespace"):
            validate_schema(unsafe)


def _classification(exc: pytest.ExceptionInfo[ReadContractError]) -> ReadFailure:
    assert str(exc.value) == exc.value.classification.value
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    assert exc.value.__suppress_context__ is True
    return exc.value.classification


def _catalog(series_id: str) -> dict[str, str]:
    seasonality, item = cpi._CATALOG[series_id]
    return {
        "series_title": (
            f"{item} in U.S. city average, all urban consumers, {seasonality.lower()}"
        ),
        "series_id": series_id,
        "seasonality": seasonality,
        "survey_name": "Consumer Price Index for All Urban Consumers (CPI-U)",
        "survey_abbreviation": "CU",
        "measure_data_type": item,
        "area": "U.S. city average",
        "item": item,
    }


def _cpi_bytes() -> bytes:
    values = {
        "CUSR0000SA0": {"1": "0.4", "12": "3.4"},
        "CUUR0000SA0": {"1": "0.3", "12": "3.4"},
        "CUSR0000SA0L1E": {"1": "0.3", "12": "2.4"},
        "CUUR0000SA0L1E": {"1": "0.3", "12": "2.4"},
    }
    series = []
    for series_id in cpi.SERIES_IDS:
        series.append(
            {
                "seriesID": series_id,
                "catalog": _catalog(series_id),
                "data": [
                    {
                        "year": "2026",
                        "period": "M08",
                        "periodName": "August",
                        "latest": "true",
                        "value": "334.131",
                        "footnotes": [{}],
                        "calculations": {
                            "net_changes": {},
                            "pct_changes": values[series_id],
                        },
                    },
                    {
                        "year": "2026",
                        "period": "M07",
                        "periodName": "July",
                        "value": "332.813",
                        "footnotes": [{}],
                        "calculations": {
                            "net_changes": {},
                            "pct_changes": {"1": "0.1"},
                        },
                    },
                    {
                        "year": "2025",
                        "period": "M08",
                        "periodName": "August",
                        "value": "323.291",
                        "footnotes": [{}],
                        "calculations": {
                            "net_changes": {},
                            "pct_changes": {"12": "2.9"},
                        },
                    },
                ],
            }
        )
    return json.dumps(
        {
            "status": "REQUEST_SUCCEEDED",
            "responseTime": 12,
            "message": [],
            "Results": {"series": series},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _seed_bound_cpi() -> tuple[InMemoryLedger, object, object]:
    evidence = cpi.DirectCpiLiveEvidence(
        raw_bytes=_cpi_bytes(),
        request=cpi.BlsCpiRequest(),
        request_identity=cpi.BlsCpiRequest().identity,
        requested_at=BASE,
        obtained_at=BASE + timedelta(seconds=1),
        acquisition_identity="p6-cpi-acquisition",
        provider_data_attempts=1,
        controlled_external_receipt="controlled-receipt",
    )
    plan = cpi.build_future_live_plan(
        evidence,
        identity="p6-cpi-live",
        finished_at=BASE + timedelta(seconds=2),
        frozen_at=BASE + timedelta(seconds=3),
    )
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=4))
    cpi.persist_live_plan_before_publication(kernel, plan)
    publication = cpi.publish_live_plan(kernel, plan)
    return ledger, plan, publication


def _daily_plan(identity: str = "p6-bars"):
    batch = TdxDailyReplayBatch(
        fixture_id=identity,
        security_id="600000.SH",
        identity_namespace="provider_suffix",
        requested_start=date(2024, 1, 2),
        requested_end=date(2024, 1, 4),
        rows=(
            TdxStockDailyRawRow(
                "2024-01-02T15:00:00", 10000, 11000, 9000, 10500, 100, 105000000
            ),
        ),
    )
    return build_replay_plan(
        batch,
        run_identity=f"{identity}:run",
        material_identity=f"{identity}:material",
        version_identity=f"{identity}:version",
        publication_identity=f"{identity}:publication",
        publication_occurrence=f"{identity}:occurrence",
        started_at=BASE,
        finished_at=BASE + timedelta(seconds=1),
        frozen_at=BASE + timedelta(seconds=2),
    )


def _seed_daily(identity: str = "p6-bars", publish_at: datetime | None = None):
    ledger = InMemoryLedger()
    plan = _daily_plan(identity)
    kernel = CanonicalKernel(ledger, lambda: publish_at or BASE + timedelta(seconds=3))
    persist_replay_before_publication(kernel, plan)
    publication = publish_replay(kernel, plan)
    return ledger, kernel, plan, publication


def test_exact_bound_cpi_returns_separate_content_publication_and_lineage() -> None:
    ledger, plan, publication = _seed_bound_cpi()
    view = read_exact_version(ledger, BLS_CPI_HEADLINES.key, plan.version.key)

    assert view.definition == BLS_CPI_HEADLINES
    assert view.version == plan.version
    assert view.content == plan.content
    assert view.publication == publication
    assert view.binding == plan.binding_candidate
    assert view.source == plan.source
    assert tuple(item.identity for item in view.acquisitions) == (
        plan.acquisition.identity,
    )
    assert view.acquisitions[0].binding == plan.binding_candidate.key
    assert view.acquisitions[0].external_receipt_present is True
    assert tuple(item.identity for item in view.materials) == (
        plan.raw_material.identity,
        plan.semantic_material.identity,
    )
    assert all(item.controlled_ref_present for item in view.materials)
    assert all(len(item.canonical_record_digest) == 64 for item in view.materials)
    assert "external_receipt" not in vars(view.acquisitions[0])
    assert all("controlled_ref" not in vars(item) for item in view.materials)
    public_shape = repr(view)
    assert "controlled-receipt" not in public_shape
    assert all(term not in set(vars(view)) for term in ("postgres", "s3", "path"))
    assert view.content_mode == "whole_version"
    assert set(vars(view)) == {
        "definition",
        "version",
        "content",
        "publication",
        "binding",
        "source",
        "acquisitions",
        "materials",
        "content_mode",
    }


def test_exact_offline_daily_bars_does_not_invent_binding_or_acquisition() -> None:
    ledger, _kernel, plan, publication = _seed_daily()
    view = read_exact_version(ledger, RAW_DAILY_BARS.key, plan.version.key)

    assert view.definition == RAW_DAILY_BARS
    assert view.version == plan.version
    assert view.content == plan.content
    assert view.publication == publication
    assert view.binding is None
    assert view.source is None
    assert view.acquisitions == ()
    assert tuple(item.identity for item in view.materials) == (plan.material.identity,)
    assert view.materials[0].controlled_ref_present is True


@pytest.mark.parametrize(
    "case,expected",
    [
        ("definition_not_admitted", ReadFailure.DEFINITION_NOT_ADMITTED),
        ("definition_not_found", ReadFailure.DEFINITION_NOT_FOUND),
        ("version_not_found", ReadFailure.VERSION_NOT_FOUND),
        ("version_definition_mismatch", ReadFailure.VERSION_DEFINITION_MISMATCH),
        ("content_missing", ReadFailure.CONTENT_MISSING),
        ("content_digest_mismatch", ReadFailure.CONTENT_DIGEST_MISMATCH),
    ],
)
def test_exact_failures_are_typed_and_closed(case: str, expected: ReadFailure) -> None:
    ledger, _kernel, plan, _publication = _seed_daily()
    definition = RAW_DAILY_BARS.key
    version = plan.version.key
    if case == "definition_not_admitted":
        definition = Key(Kind.DEFINITION, "not-admitted", "p3-r1")
    elif case == "definition_not_found":
        ledger._records.pop(
            (Kind.DEFINITION.value, definition.identity, definition.revision)
        )
    elif case == "version_not_found":
        version = Key(Kind.VERSION, "absent")
    elif case == "version_definition_mismatch":
        definition = BLS_CPI_HEADLINES.key
        ledger._records[
            (Kind.DEFINITION.value, definition.identity, definition.revision)
        ] = dumps(BLS_CPI_HEADLINES)
    elif case == "content_missing":
        ledger._contents.pop(version.identity)
    elif case == "content_digest_mismatch":
        expected_digest, _content = ledger._contents[version.identity]
        ledger._contents[version.identity] = (expected_digest, b"corrupt")

    with pytest.raises(ReadContractError) as exc:
        read_exact_version(ledger, definition, version)
    assert _classification(exc) == expected


def test_exact_as_of_before_publication_or_usable_time_has_no_fallback() -> None:
    ledger, kernel, plan, publication = _seed_daily()
    before = publication.decision_at - timedelta(microseconds=1)
    with pytest.raises(ReadContractError) as exc:
        read_exact_version(ledger, RAW_DAILY_BARS.key, plan.version.key, as_of=before)
    assert _classification(exc) == ReadFailure.UNPUBLISHED_OR_UNAVAILABLE_AS_OF

    ledger = InMemoryLedger()
    plan = _daily_plan("p6-bars-delayed-usable")
    decision_at = BASE + timedelta(seconds=3)
    usable_at = decision_at + timedelta(seconds=1)
    kernel = CanonicalKernel(ledger, lambda: decision_at)
    persist_replay_before_publication(kernel, plan)
    publication = kernel.publish(replace(plan.publication_request, usable_at=usable_at))

    with pytest.raises(ReadContractError) as exc:
        read_exact_version(
            ledger,
            RAW_DAILY_BARS.key,
            plan.version.key,
            as_of=usable_at - timedelta(microseconds=1),
        )
    assert _classification(exc) == ReadFailure.UNPUBLISHED_OR_UNAVAILABLE_AS_OF
    assert (
        read_exact_version(
            ledger, RAW_DAILY_BARS.key, plan.version.key, as_of=usable_at
        ).publication
        == publication
    )


@pytest.mark.parametrize(
    "decision,expected",
    [
        (Decision.WITHDRAW, ReadFailure.VERSION_WITHDRAWN),
        (Decision.SUPERSEDE, ReadFailure.VERSION_SUPERSEDED),
    ],
)
def test_exact_terminal_decisions_are_typed_only_when_effective(
    decision: Decision, expected: ReadFailure
) -> None:
    ledger, kernel, plan, publication = _seed_daily()
    kernel.clock = lambda: BASE + timedelta(seconds=5)
    terminal = kernel.publish(
        PublicationRequest(
            identity=f"p6-bars:{decision.value.lower()}",
            occurrence=f"p6-bars:{decision.value.lower()}-occurrence",
            version=plan.version.key,
            rule_revision="p6-consumer-read-r1",
            decision=decision,
            scope=Scope(),
            evidence=plan.version.materials,
            claimed_grades=frozenset({plan.material.grade}),
            reason=f"deterministic-{decision.value.lower()}-test",
            predecessor=publication.key,
            supersedes_version=(
                plan.version.key if decision == Decision.SUPERSEDE else None
            ),
        )
    )
    before_terminal = read_exact_version(
        ledger,
        RAW_DAILY_BARS.key,
        plan.version.key,
        as_of=terminal.decision_at - timedelta(microseconds=1),
    )
    assert before_terminal.publication == publication

    for as_of in (
        terminal.decision_at,
        terminal.decision_at + timedelta(microseconds=1),
    ):
        with pytest.raises(ReadContractError) as exc:
            read_exact_version(
                ledger,
                RAW_DAILY_BARS.key,
                plan.version.key,
                as_of=as_of,
            )
        assert _classification(exc) == expected

    with pytest.raises(ReadContractError) as exc:
        read_exact_version(ledger, RAW_DAILY_BARS.key, plan.version.key)
    assert _classification(exc) == expected

    with pytest.raises(ReadContractError) as exc:
        read_as_of(
            ledger,
            RAW_DAILY_BARS.key,
            as_of=terminal.decision_at + timedelta(microseconds=1),
        )
    assert _classification(exc) == ReadFailure.AS_OF_NO_VERSION


def test_as_of_zero_one_and_multiple_versions_never_choose_latest() -> None:
    ledger = InMemoryLedger()
    first = _daily_plan("p6-bars-a")
    second = _daily_plan("p6-bars-z")
    times = iter((BASE + timedelta(seconds=3), BASE + timedelta(seconds=4)))
    kernel = CanonicalKernel(ledger, lambda: next(times))
    for plan in (first, second):
        persist_replay_before_publication(kernel, plan)

    with pytest.raises(ReadContractError) as exc:
        read_as_of(ledger, RAW_DAILY_BARS.key, as_of=BASE + timedelta(seconds=2))
    assert _classification(exc) == ReadFailure.AS_OF_NO_VERSION

    first_publication = publish_replay(kernel, first)
    one = read_as_of(
        ledger, RAW_DAILY_BARS.key, as_of=BASE + timedelta(seconds=3, microseconds=1)
    )
    assert one.version == first.version
    assert one.publication == first_publication

    publish_replay(kernel, second)
    with pytest.raises(ReadContractError) as exc:
        read_as_of(ledger, RAW_DAILY_BARS.key, as_of=BASE + timedelta(seconds=5))
    assert _classification(exc) == ReadFailure.AS_OF_AMBIGUOUS


def test_bound_lineage_missing_and_publication_chain_corruption_fail_closed() -> None:
    ledger, plan, publication = _seed_bound_cpi()
    ledger._records.pop(
        (
            Kind.BINDING.value,
            plan.version.binding.identity,
            plan.version.binding.revision,
        )
    )
    with pytest.raises(ReadContractError) as exc:
        read_exact_version(ledger, BLS_CPI_HEADLINES.key, plan.version.key)
    assert _classification(exc) == ReadFailure.LINEAGE_MISSING_OR_MISMATCHED

    ledger, _plan, publication = _seed_bound_cpi()
    corrupt = replace(
        publication,
        request=replace(
            publication.request,
            predecessor=Key(Kind.PUBLICATION, "impossible-first-predecessor"),
        ),
    )
    ledger._records[(Kind.PUBLICATION.value, publication.key.identity, "")] = dumps(
        corrupt
    )
    with pytest.raises(ReadContractError) as exc:
        read_exact_version(ledger, BLS_CPI_HEADLINES.key, _plan.version.key)
    assert _classification(exc) == ReadFailure.PUBLICATION_STATE_INVALID


def test_acquisition_source_must_match_binding_source() -> None:
    ledger, plan, _publication = _seed_bound_cpi()
    other_source = replace(plan.source, identity="p6-other-source")
    ledger._records[
        (Kind.SOURCE.value, other_source.key.identity, other_source.key.revision)
    ] = dumps(other_source)
    mismatched = replace(plan.acquisition, source=other_source.key)
    ledger._records[(Kind.ACQUISITION.value, mismatched.identity, "")] = dumps(
        mismatched
    )

    with pytest.raises(ReadContractError) as exc:
        read_exact_version(ledger, BLS_CPI_HEADLINES.key, plan.version.key)
    assert _classification(exc) == ReadFailure.LINEAGE_MISSING_OR_MISMATCHED


@pytest.mark.parametrize("record_kind", ["binding", "source"])
def test_malformed_binding_or_source_contract_fails_closed(record_kind: str) -> None:
    ledger, plan, _publication = _seed_bound_cpi()
    if record_kind == "binding":
        malformed = replace(plan.binding_candidate, silent_fallback=True)
        key = malformed.key
    else:
        malformed = replace(plan.source, access_effect="write_provider")
        key = malformed.key
    ledger._records[(key.kind.value, key.identity, key.revision)] = dumps(malformed)

    with pytest.raises(ReadContractError) as exc:
        read_exact_version(ledger, BLS_CPI_HEADLINES.key, plan.version.key)
    assert _classification(exc) == ReadFailure.LINEAGE_MISSING_OR_MISMATCHED


def test_invalid_evidence_material_provenance_fails_closed() -> None:
    ledger, plan, _publication = _seed_bound_cpi()
    malformed = replace(
        plan.raw_material,
        produced_by=Key(Kind.RUN, "not-the-acquisition-run"),
    )
    ledger._records[(Kind.MATERIAL.value, malformed.identity, "")] = dumps(malformed)

    with pytest.raises(ReadContractError) as exc:
        read_exact_version(ledger, BLS_CPI_HEADLINES.key, plan.version.key)
    assert _classification(exc) == ReadFailure.LINEAGE_MISSING_OR_MISMATCHED


def test_invalid_evidence_material_capability_fails_closed() -> None:
    ledger, plan, _publication = _seed_bound_cpi()
    malformed = replace(plan.raw_material, grade=Grade.DECODED)
    ledger._records[(Kind.MATERIAL.value, malformed.identity, "")] = dumps(malformed)

    with pytest.raises(ReadContractError) as exc:
        read_exact_version(ledger, BLS_CPI_HEADLINES.key, plan.version.key)
    assert _classification(exc) == ReadFailure.LINEAGE_MISSING_OR_MISMATCHED


def test_lineage_resolver_exception_is_redacted_as_lineage_failure() -> None:
    ledger, plan, _publication = _seed_bound_cpi()

    class BrokenLineageLedger(InMemoryLedger):
        def get(self, key):
            if key == plan.source.key:
                raise RuntimeError("private canonical storage details")
            return super().get(key)

    ledger.__class__ = BrokenLineageLedger
    with pytest.raises(ReadContractError) as exc:
        read_exact_version(ledger, BLS_CPI_HEADLINES.key, plan.version.key)
    assert _classification(exc) == ReadFailure.LINEAGE_MISSING_OR_MISMATCHED
    assert "private" not in str(exc.value)


def test_repository_primitive_is_exact_stable_and_postgres_has_same_ordering() -> None:
    ledger = InMemoryLedger()
    z_plan = _daily_plan("z-version")
    a_plan = _daily_plan("a-version")
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=3))
    for plan in (z_plan, a_plan):
        persist_replay_before_publication(kernel, plan)
    before_query = ledger.snapshot()
    assert ledger.versions_for_definition(RAW_DAILY_BARS.key) == (
        a_plan.version,
        z_plan.version,
    )
    assert ledger.versions_for_definition(BLS_CPI_HEADLINES.key) == ()
    assert ledger.snapshot() == before_query

    class Rows:
        def __init__(self, values):
            self.values = values

        def fetchall(self):
            return self.values

    class Connection:
        def __init__(self):
            self.query = None
            self.parameters = None

        def execute(self, query, parameters):
            self.query = query.as_string()
            self.parameters = parameters
            return Rows([(dumps(a_plan.version),), (dumps(z_plan.version),)])

    connection = Connection()
    postgres = PostgresLedger(connection, "mdf_p3_canonical_kernel_r1_unit")
    assert postgres.versions_for_definition(RAW_DAILY_BARS.key) == (
        a_plan.version,
        z_plan.version,
    )
    assert connection.parameters == (Kind.VERSION.value,)
    assert "WHERE kind = %s" in connection.query
    assert "ORDER BY identity, revision" in connection.query


def test_storage_exception_text_is_not_exposed() -> None:
    class BrokenLedger(InMemoryLedger):
        def get(self, key):
            raise RuntimeError("postgres password and local path must stay private")

    with pytest.raises(ReadContractError) as exc:
        read_exact_version(
            BrokenLedger(), RAW_DAILY_BARS.key, Key(Kind.VERSION, "anything")
        )
    assert _classification(exc) == ReadFailure.STORAGE_UNAVAILABLE
    assert "password" not in str(exc.value)
