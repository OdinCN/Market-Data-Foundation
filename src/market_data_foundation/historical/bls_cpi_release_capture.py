"""Deterministic composition for the prebound September 2026 CPI release."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import digest, require
from market_data_foundation.canonical.repository import Ledger
from market_data_foundation.historical.bls_cpi_direct import (
    SEPTEMBER_REFERENCE_CONTRACT,
    ReleaseCausation,
)
from market_data_foundation.historical.bls_cpi_direct_live import (
    ARTIFACT_ROOT,
    DEFAULT_SECRET_PATH,
    LiveExecutionError,
    Transport,
    execute_live_candidate,
)
from market_data_foundation.historical.bls_cpi_material_store import (
    MaterializedLiveResult,
    MaterialStore,
    WriterProtection,
    materialize_persist_publish,
)
from market_data_foundation.historical.bls_cpi_release_runner import (
    CAPABILITY,
    EXECUTION_IDENTITY,
    MAX_ATTEMPTS,
    PRODUCT,
    REFERENCE,
    RELEASE,
    SLOT_IDENTITY,
    CaptureCompletion,
    CaptureRequest,
)
from market_data_foundation.source_request_policy import retryable_http_status


def _request_is_exact(request: object) -> bool:
    return (
        type(request) is CaptureRequest
        and request.product == PRODUCT
        and request.capability == CAPABILITY
        and request.reference == REFERENCE
        and request.release == RELEASE
        and request.slot_identity == SLOT_IDENTITY
        and request.execution_identity == EXECUTION_IDENTITY
        and type(request.attempt) is int
        and 1 <= request.attempt <= MAX_ATTEMPTS
    )


def _readback_exact(ledger: Ledger, result: MaterializedLiveResult) -> None:
    """Require exact canonical version, content, and Publication readback."""
    plan = result.plan
    require(
        ledger.get(plan.version.key) == result.version == plan.version,
        "bls_cpi_release_version_readback_mismatch",
    )
    content = ledger.get_content(plan.version.key)
    require(
        content == plan.content and digest(content) == plan.version.content_digest,
        "bls_cpi_release_content_readback_mismatch",
    )
    require(
        ledger.publication_by_occurrence(plan.publication_request.occurrence)
        == result.publication
        and result.publication.request == plan.publication_request,
        "bls_cpi_release_publication_readback_mismatch",
    )


@dataclass(frozen=True)
class SeptemberReleaseCapture:
    """Runner callback with all effects supplied explicitly by its caller."""

    kernel: CanonicalKernel
    material_store: MaterialStore
    writer_protection: WriterProtection
    readback_ledger_factory: Callable[[], Ledger]
    secret_path: Path = DEFAULT_SECRET_PATH
    artifact_root: Path = ARTIFACT_ROOT
    transport: Transport | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    identity_factory: Callable[[], object] = uuid4
    source_retry_sleep: Callable[[float], None] = time.sleep
    authoritative_commit: Callable[[], None] = lambda: None

    def __call__(self, request: CaptureRequest) -> CaptureCompletion:
        # Admission is intentionally complete before credential or provider access.
        if not _request_is_exact(request):
            return CaptureCompletion("request_invalid")

        causation = ReleaseCausation(
            slot_identity=request.slot_identity,
            execution_identity=request.execution_identity,
            attempt=request.attempt,
            reference=request.reference,
            release=request.release,
        )
        try:
            candidate = execute_live_candidate(
                secret_path=self.secret_path,
                artifact_root=self.artifact_root,
                transport=self.transport,
                clock=self.clock,
                identity_factory=self.identity_factory,
                reference_contract=SEPTEMBER_REFERENCE_CONTRACT,
                release_causation=causation,
                sleep=self.source_retry_sleep,
            )
        except LiveExecutionError as exc:
            if exc.classification == "bls_live_reference_not_available":
                return CaptureCompletion("no_new_release/reference_not_available")
            if exc.classification == "bls_live_transport_failure":
                return CaptureCompletion("source_transport_exhausted")
            if exc.classification == "bls_live_http_failure" and retryable_http_status(
                exc.http_status
            ):
                return CaptureCompletion(
                    "source_transport_exhausted",
                    http_status=exc.http_status,
                )
            return CaptureCompletion("terminal")
        except Exception:
            return CaptureCompletion("terminal")

        try:
            result = materialize_persist_publish(
                self.kernel,
                candidate.plan,
                candidate.evidence.raw_bytes,
                self.material_store,
                self.writer_protection,
                authoritative_commit=self.authoritative_commit,
            )
            readback_ledger = self.readback_ledger_factory()
            require(
                readback_ledger is not self.kernel.ledger,
                "bls_cpi_release_readback_ledger_not_independent",
            )
            _readback_exact(readback_ledger, result)
        except Exception:
            return CaptureCompletion("terminal")
        return CaptureCompletion("success", canonical_verified=True)
