import os
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    AcquisitionOccurrence,
    ContractError,
    DataRun,
    DatasetDefinition,
    DatasetVersion,
    Decision,
    EvidenceMaterial,
    EvidenceRequirement,
    FiniteScope,
    Grade,
    OutputMapping,
    PublicationRequest,
    RunKind,
    Scope,
    SourceBindingRevision,
    SourceOperationRef,
    digest,
)
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.canonical.protection import ProtectedMaterial
from market_data_foundation.canonical.rules import Outcome

pytestmark = pytest.mark.postgres_integration


def _configuration():
    runtime = tomllib.loads(Path("config/local/runtime.toml").read_text())["postgres"]
    secret = tomllib.loads(Path("config/local/secret.toml").read_text())["postgres"]
    assert runtime["host"] == "192.168.10.4"
    return runtime, secret


def _connect(*, autocommit: bool = False):
    runtime, secret = _configuration()
    return psycopg.connect(
        host=runtime["host"],
        port=runtime["port"],
        dbname=runtime["database"],
        user=runtime["user"],
        sslmode=runtime["sslmode"],
        password=secret["password"],
        connect_timeout=5,
        autocommit=autocommit,
    )


def _seed(kernel: CanonicalKernel):
    base = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
    public = Scope()
    source = SourceOperationRef(
        identity="synthetic:operation",
        contract_revision="1",
        owner="mdf-test",
        product="mdf-test",
        operation="synthetic.read",
        projection_revision="p1",
        variant="synthetic",
        build_revision="build-1",
        registry_revision=None,
        provider_app_revision=None,
        access_effect="read",
        acquisition_mode="historical_query",
        scope=public,
        temporal_contract="synthetic",
        completeness_contract="bounded",
        pagination_contract="none",
        freshness_contract="synthetic",
        evidence_capability=frozenset({Grade.PROJECTED}),
    )
    definition = DatasetDefinition(
        identity="synthetic:dataset",
        revision="d1",
        domain_contract="bounded synthetic content",
        purpose="P3 durable-content proof",
        source_qualified=True,
        key_semantics="synthetic-key",
        unit_semantics="unitless",
        temporal_semantics="synthetic-time",
        revision_semantics="immutable",
        completeness_semantics="bounded",
        scope=public,
    )
    binding = SourceBindingRevision(
        identity="synthetic:binding",
        revision="b1",
        source=source.key,
        mappings=(
            OutputMapping(
                identity="out",
                definition=definition.key,
                semantic_revision="s1",
                temporal_revision="t1",
                scope=public,
                evidence=EvidenceRequirement(
                    allowed=frozenset({Grade.PROJECTED}),
                ),
            ),
        ),
        compatibility_policy="exact",
    )
    run = DataRun(
        identity="synthetic-run",
        kind=RunKind.COLLECT,
        requested_scope=public,
        started_at=base,
        finished_at=base + timedelta(seconds=1),
        result="success",
        binding=binding.key,
    )
    acquisition = AcquisitionOccurrence(
        identity="synthetic-acq",
        run=run.key,
        request_identity="synthetic-request",
        source=source.key,
        binding=binding.key,
        mapping="out",
        scope=public,
        parameters=(),
        requested_at=base,
        obtained_at=base + timedelta(milliseconds=100),
        source_observed_at=None,
        source_frame_identity=None,
        result_mode="historical_query",
        completeness="bounded",
        freshness="synthetic",
        terminal_result="success",
        provider_data_attempts=1,
    )
    evidence_bytes = b'{"synthetic":"projection"}'
    evidence = EvidenceMaterial(
        identity="synthetic-material",
        grade=Grade.PROJECTED,
        digest=digest(evidence_bytes),
        scope=public,
        acquired_from=acquisition.key,
        controlled_ref="synthetic://projection",
    )
    content = b'[{"id":"fixture-1","value":42}]'
    version = DatasetVersion(
        identity="synthetic-version",
        definition=definition.key,
        finite_scope=FiniteScope(("fixture-1",)),
        scope=public,
        frozen_at=base + timedelta(seconds=2),
        run=run.key,
        content_digest=digest(content),
        binding=binding.key,
        mapping="out",
        acquisitions=(acquisition.key,),
        materials=(evidence.key,),
        completeness="bounded",
    )
    for record in (source, definition, binding, run, acquisition, evidence):
        kernel.add(record)
    return evidence, version, content


def _publish_request(version, evidence, predecessor=None):
    return PublicationRequest(
        identity="synthetic-publish",
        occurrence="synthetic-occurrence",
        version=version.key,
        rule_revision="r1",
        decision=Decision.PUBLISH,
        scope=version.scope,
        evidence=(evidence.key,),
        claimed_grades=frozenset({Grade.PROJECTED}),
        reason="synthetic-integration",
        predecessor=predecessor,
        required_completeness="bounded",
    )


def test_postgres_durable_content_restart_and_reconciliation() -> None:
    if os.environ.get("MDF_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("private PostgreSQL integration is explicit opt-in")

    schema = os.environ["MDF_PG_TEST_SCHEMA"]
    connection = _connect()
    ledger = PostgresLedger(connection, schema)
    assert not ledger.namespace_exists()

    try:
        ledger.create_namespace()
        connection.commit()

        kernel = CanonicalKernel(ledger, ledger.server_now)
        evidence, version, content = _seed(kernel)
        protected = ProtectedMaterial({"canonical-content": content})
        protected.activate()
        protected.final_verify({"canonical-content": version.content_digest})
        assert protected.payload("canonical-content", version.content_digest) == content

        kernel.persist_version(version, content)
        connection.commit()
        protected.resolve()
        protected.release()

        publish_request = _publish_request(version, evidence)
        observer = _connect(autocommit=True)
        observer_ledger = PostgresLedger(observer, schema)
        before_publish = observer_ledger.server_now()
        assert (
            observer_ledger.publication_by_occurrence(publish_request.occurrence)
            is None
        )

        # A caller-owned open transaction may not contain the authoritative
        # publication decision. It is rejected before any Publication is visible.
        connection.execute("SELECT 1")
        with pytest.raises(ContractError, match="publication_connection_not_idle"):
            kernel.publish(publish_request)
        assert (
            observer_ledger.publication_by_occurrence(publish_request.occurrence)
            is None
        )
        connection.rollback()

        # publish() owns its transaction and confirms COMMIT before returning.
        publication = kernel.publish(publish_request)
        after_publish = observer_ledger.server_now()
        assert before_publish <= publication.decision_at <= after_publish
        assert (
            observer_ledger.publication_by_occurrence(publish_request.occurrence)
            == publication
        )

        # A caller rollback after the durable API returns cannot erase the decision.
        connection.rollback()
        assert (
            observer_ledger.publication_by_occurrence(publish_request.occurrence)
            == publication
        )
        observer_kernel = CanonicalKernel(observer_ledger, observer_ledger.server_now)
        assert not observer_kernel.eligible(version, before_publish)
        assert observer_kernel.eligible(
            version,
            publication.decision_at + timedelta(seconds=1),
        )
        recovered = observer_kernel.reconcile_publication(
            publication.request.occurrence,
            Outcome.OUTCOME_UNKNOWN,
        )
        assert recovered.outcome == Outcome.CONFIRMED_COMMITTED
        assert recovered.canonical_identity == publication.key
        observer.close()
        connection.close()

        connection = _connect()
        ledger = PostgresLedger(connection, schema)
        kernel = CanonicalKernel(ledger, ledger.server_now)
        restored = ledger.get(version.key)
        assert restored == version
        assert ledger.get_content(version.key) == content
        assert (
            ledger.publication_by_occurrence(publication.request.occurrence)
            == publication
        )

        # Confirmed-not-committed remains a distinct observable outcome. The
        # request is never submitted to the durable publication API.
        absent_occurrence = "synthetic-withdraw-not-submitted"
        absent = kernel.reconcile_publication(
            absent_occurrence,
            Outcome.CONFIRMED_NOT_COMMITTED,
        )
        assert absent.outcome == Outcome.CONFIRMED_NOT_COMMITTED
    finally:
        try:
            connection.close()
        except Exception:
            pass
        cleanup = _connect(autocommit=True)
        try:
            cleanup_ledger = PostgresLedger(cleanup, schema)
            if cleanup_ledger.namespace_exists():
                cleanup_ledger.drop_namespace()
        finally:
            cleanup.close()
