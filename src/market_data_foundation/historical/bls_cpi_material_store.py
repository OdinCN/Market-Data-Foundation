"""Bounded Garage handoff for provider-original BLS CPI material."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import psycopg
from psycopg.pq import TransactionStatus

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    DatasetVersion,
    Publication,
    PublicationRequest,
    digest,
    require,
)
from market_data_foundation.canonical.repository import Ledger
from market_data_foundation.historical.bls_cpi_direct import (
    MAX_RESPONSE_BYTES,
    DirectCpiLivePlan,
    persist_live_plan_before_publication,
    publish_live_plan,
)

BUCKET = "mdf-artifacts"
KEY_PREFIX = "materials/sha256/"
FILENAME = "raw-response.bin"
LOCATOR_PREFIX = "mdf-s3://"
GARAGE_ENDPOINT = "http://192.168.10.8:3900"
GARAGE_REGION = "garage"
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class MaterialIdentity:
    sha256: str
    size: int
    bucket: str = BUCKET
    key: str = ""

    def __post_init__(self) -> None:
        require(_SHA256.fullmatch(self.sha256) is not None, "material_digest_invalid")
        require(
            type(self.size) is int and 0 < self.size <= MAX_RESPONSE_BYTES,
            "material_size_invalid",
        )
        expected = f"{KEY_PREFIX}{self.sha256}/{FILENAME}"
        require(self.bucket == BUCKET, "material_bucket_not_admitted")
        if self.key:
            require(self.key == expected, "material_key_not_admitted")
        else:
            object.__setattr__(self, "key", expected)

    @property
    def locator(self) -> str:
        return f"{LOCATOR_PREFIX}{self.bucket}/{self.key}"

    @classmethod
    def from_locator(
        cls, locator: str, *, expected_sha256: str, expected_size: int
    ) -> MaterialIdentity:
        expected = cls(expected_sha256, expected_size)
        require(locator == expected.locator, "material_locator_not_admitted")
        return expected


class WriteDisposition(StrEnum):
    CREATED = "created"
    ALREADY_PRESENT = "already_present"


class MaterialWriteOutcomeUnknown(RuntimeError):
    def __init__(self) -> None:
        super().__init__("material_write_outcome_unknown")


class MaterialStoreFailure(RuntimeError):
    def __init__(self, classification: str) -> None:
        self.classification = classification
        super().__init__(classification)


class MaterialStore(Protocol):
    def create_if_absent(
        self, identity: MaterialIdentity, payload: bytes
    ) -> WriteDisposition: ...
    def read_exact(self, identity: MaterialIdentity) -> bytes | None: ...


class WriterProtectionTicket(Protocol):
    def assert_held(self, identity: MaterialIdentity) -> None: ...
    def release(self, identity: MaterialIdentity) -> None: ...
    def mark_outcome_unknown(self, identity: MaterialIdentity) -> None: ...


class WriterProtection(Protocol):
    def acquire(self, identity: MaterialIdentity) -> WriterProtectionTicket: ...


@dataclass(frozen=True)
class MaterialVerification:
    """Non-authorizing observation returned only after completed materialization."""

    identity: MaterialIdentity
    disposition: WriteDisposition
    reconciled_outcome_unknown: bool


@dataclass(frozen=True)
class MaterializedLiveResult:
    plan: DirectCpiLivePlan
    verification: MaterialVerification
    version: DatasetVersion
    publication: Publication


class ReconciliationPhase(StrEnum):
    LEDGER_HANDOFF = "ledger_handoff"
    PUBLICATION = "publication"


class ConfirmedOutcome(StrEnum):
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"


class MaterializationOutcomeUnknown(RuntimeError):
    """Same-identity reconciliation state that retains writer protection."""

    def __init__(
        self,
        *,
        phase: ReconciliationPhase,
        identity: MaterialIdentity,
        expected_version: DatasetVersion,
        expected_content: bytes,
        expected_publication_request: PublicationRequest,
        ticket: WriterProtectionTicket,
    ) -> None:
        self.phase = phase
        self.identity = identity
        self.expected_version = expected_version
        self.expected_content = expected_content
        self.expected_publication_request = expected_publication_request
        self._ticket = ticket
        self._resolved = False
        ticket.mark_outcome_unknown(identity)
        super().__init__("authoritative_outcome_unknown")

    @property
    def version_identity(self) -> str:
        return self.expected_version.identity

    @property
    def publication_occurrence(self) -> str:
        return self.expected_publication_request.occurrence

    def assert_protection_held(self) -> None:
        self._ticket.assert_held(self.identity)

    def reconcile(self, ledger: Ledger) -> ConfirmedOutcome:
        """Derive the outcome from exact authoritative readback, then release."""
        require(not self._resolved, "material_reconciliation_already_resolved")
        self._ticket.assert_held(self.identity)
        if self.phase == ReconciliationPhase.LEDGER_HANDOFF:
            observed = ledger.get(self.expected_version.key)
            if observed is None:
                require(
                    ledger.get_content(self.expected_version.key) is None,
                    "material_ledger_absence_readback_mismatch",
                )
                outcome = ConfirmedOutcome.NOT_COMMITTED
            else:
                require(
                    observed == self.expected_version,
                    "material_ledger_readback_mismatch",
                )
                content = ledger.get_content(self.expected_version.key)
                require(
                    content == self.expected_content
                    and digest(content) == self.expected_version.content_digest,
                    "material_ledger_content_readback_mismatch",
                )
                outcome = ConfirmedOutcome.COMMITTED
        else:
            observed = ledger.publication_by_occurrence(
                self.expected_publication_request.occurrence
            )
            if observed is None:
                outcome = ConfirmedOutcome.NOT_COMMITTED
            else:
                require(
                    observed.request == self.expected_publication_request,
                    "material_publication_readback_mismatch",
                )
                outcome = ConfirmedOutcome.COMMITTED
        self._ticket.assert_held(self.identity)
        self._ticket.release(self.identity)
        self._resolved = True
        return outcome

    def recover_lost_session(self, protection: WriterProtection) -> None:
        """Reacquire the same identity after confirmed loss of the old session."""
        require(not self._resolved, "material_reconciliation_already_resolved")
        try:
            self._ticket.assert_held(self.identity)
        except Exception:
            self._ticket = protection.acquire(self.identity)
            self._ticket.mark_outcome_unknown(self.identity)
            return
        require(False, "material_writer_protection_still_held")


class InMemoryMaterialStore:
    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], bytes] = {}
        self.create_attempts = 0
        self.reads = 0
        self.outcome_unknown_after_create = False

    def create_if_absent(
        self, identity: MaterialIdentity, payload: bytes
    ) -> WriteDisposition:
        _verify_payload(identity, payload)
        self.create_attempts += 1
        key = (identity.bucket, identity.key)
        existing = self._objects.get(key)
        if existing is not None:
            _verify_payload(identity, existing, "material_existing_mismatch")
            return WriteDisposition.ALREADY_PRESENT
        self._objects[key] = payload
        if self.outcome_unknown_after_create:
            raise MaterialWriteOutcomeUnknown
        return WriteDisposition.CREATED

    def read_exact(self, identity: MaterialIdentity) -> bytes | None:
        self.reads += 1
        return self._objects.get((identity.bucket, identity.key))


class _InMemoryTicket:
    def __init__(self, owner: InMemoryWriterProtection) -> None:
        self._owner = owner
        self._held = True
        self.outcome_unknown = False

    def assert_held(self, identity: MaterialIdentity) -> None:
        require(
            self._held and self._owner._identity == identity,
            "material_writer_protection_gap",
        )

    def mark_outcome_unknown(self, identity: MaterialIdentity) -> None:
        self.assert_held(identity)
        self.outcome_unknown = True

    def release(self, identity: MaterialIdentity) -> None:
        self.assert_held(identity)
        self._held = False
        self._owner._identity = None
        self._owner._lock.release()

    def force_gap_for_counterexample(self) -> None:
        if self._held:
            self._held = False
            self._owner._identity = None
            self._owner._lock.release()


class InMemoryWriterProtection:
    def __init__(self) -> None:
        self._lock = Lock()
        self._identity: MaterialIdentity | None = None

    def acquire(self, identity: MaterialIdentity) -> _InMemoryTicket:
        require(self._lock.acquire(blocking=False), "material_writer_busy")
        self._identity = identity
        return _InMemoryTicket(self)


@dataclass(frozen=True)
class GarageCredentials:
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    def __post_init__(self) -> None:
        require(bool(self.access_key and self.secret_key), "garage_credentials_missing")


@dataclass(frozen=True)
class WireRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class WireResponse:
    status: int
    body: bytes


class WireTransport(Protocol):
    def send(self, request: WireRequest) -> WireResponse: ...


class _UrllibOneShotTransport:
    def __init__(self, timeout_seconds: float) -> None:
        self._timeout_seconds = timeout_seconds

    def send(self, request: WireRequest) -> WireResponse:
        wire = Request(
            request.url,
            data=request.body if request.method == "PUT" else None,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            opener = build_opener(_NoRedirect)
            with opener.open(wire, timeout=self._timeout_seconds) as response:
                return WireResponse(response.status, response.read())
        except HTTPError as exc:
            return WireResponse(exc.code, exc.read())


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class GarageMaterialStore:
    """Fixed-endpoint path-style SigV4 adapter; performs no retry."""

    def __init__(
        self,
        credentials: GarageCredentials,
        *,
        endpoint: str = GARAGE_ENDPOINT,
        transport: WireTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout_seconds: float = 10.0,
    ) -> None:
        require(endpoint == GARAGE_ENDPOINT, "garage_endpoint_not_admitted")
        require(urlsplit(endpoint).path in {"", "/"}, "garage_endpoint_not_admitted")
        self._credentials = credentials
        self._endpoint = endpoint.rstrip("/")
        self._transport = transport or _UrllibOneShotTransport(timeout_seconds)
        self._clock = clock

    def create_if_absent(
        self, identity: MaterialIdentity, payload: bytes
    ) -> WriteDisposition:
        _verify_payload(identity, payload)
        existing = self.read_exact(identity)
        if existing is not None:
            return WriteDisposition.ALREADY_PRESENT
        try:
            response = self._send("PUT", identity, payload)
        except OSError, TimeoutError, URLError:
            raise MaterialWriteOutcomeUnknown from None
        if response.status in {200, 201}:
            return WriteDisposition.CREATED
        if 500 <= response.status < 600:
            raise MaterialWriteOutcomeUnknown
        raise MaterialStoreFailure(_classify_status("write", response.status))

    def read_exact(self, identity: MaterialIdentity) -> bytes | None:
        try:
            response = self._send("GET", identity, b"")
        except OSError, TimeoutError, URLError:
            raise MaterialStoreFailure("material_read_transport_failure") from None
        if response.status == 200:
            _verify_payload(identity, response.body, "material_existing_mismatch")
            return response.body
        if response.status == 404:
            return None
        raise MaterialStoreFailure(_classify_status("read", response.status))

    def _send(
        self, method: str, identity: MaterialIdentity, body: bytes
    ) -> WireResponse:
        path = "/" + quote(f"{identity.bucket}/{identity.key}", safe="/-_.~")
        now = self._clock().astimezone(UTC)
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = _signed_headers(
            method,
            path,
            urlsplit(self._endpoint).netloc,
            payload_hash,
            now,
            self._credentials,
        )
        return self._transport.send(
            WireRequest(method, self._endpoint + path, headers, body)
        )


def _classify_status(operation: str, status: int) -> str:
    if 400 <= status < 500:
        return f"material_{operation}_rejected"
    if 500 <= status < 600:
        return f"material_{operation}_service_failure"
    return f"material_{operation}_unexpected_status"


def _signed_headers(
    method: str,
    path: str,
    host: str,
    payload_hash: str,
    now: datetime,
    credentials: GarageCredentials,
) -> dict[str, str]:
    amz_date, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    canonical = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    names = ";".join(sorted(canonical))
    canonical_headers = "".join(
        f"{name}:{canonical[name]}\n" for name in sorted(canonical)
    )
    request = "\n".join((method, path, "", canonical_headers, names, payload_hash))
    scope = f"{date}/{GARAGE_REGION}/s3/aws4_request"
    to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(request.encode()).hexdigest(),
        )
    )
    key = ("AWS4" + credentials.secret_key).encode()
    for value in (date, GARAGE_REGION, "s3", "aws4_request"):
        key = hmac.new(key, value.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={credentials.access_key}/{scope}, "
        f"SignedHeaders={names}, Signature={signature}"
    )
    return {**canonical, "authorization": authorization}


def _advisory_keys(identity: MaterialIdentity) -> tuple[int, int]:
    bound = hashlib.sha256(
        f"{identity.bucket}\0{identity.key}\0{identity.sha256}\0{identity.size}".encode()
    ).digest()
    return (
        int.from_bytes(bound[:4], "big") & 0x7FFF_FFFF,
        int.from_bytes(bound[4:8], "big") & 0x7FFF_FFFF,
    )


class _PostgresAdvisoryTicket:
    def __init__(
        self,
        connection: psycopg.Connection,
        identity: MaterialIdentity,
        keys: tuple[int, int],
    ) -> None:
        self._connection, self._identity, self._keys = connection, identity, keys
        self._held, self.outcome_unknown = True, False

    def _query(self, statement: str) -> bool:
        require(not self._connection.closed, "material_writer_session_lost")
        if self._connection.info.transaction_status == TransactionStatus.INERROR:
            self._connection.rollback()
        was_idle = self._connection.info.transaction_status == TransactionStatus.IDLE
        row = self._connection.execute(statement, self._keys).fetchone()
        if was_idle:
            self._connection.commit()
        return bool(row[0])

    def assert_held(self, identity: MaterialIdentity) -> None:
        require(
            self._held and identity == self._identity, "material_writer_protection_gap"
        )
        statement = """SELECT EXISTS (SELECT 1 FROM pg_locks
            WHERE locktype = 'advisory' AND pid = pg_backend_pid()
              AND classid::bigint = %s AND objid::bigint = %s
              AND objsubid = 2 AND granted)"""
        require(self._query(statement), "material_writer_protection_gap")

    def mark_outcome_unknown(self, identity: MaterialIdentity) -> None:
        # Do not query here: the ambiguous operation may have broken the session.
        # The reconciliation object can explicitly reacquire this same identity.
        require(
            self._held and identity == self._identity,
            "material_writer_protection_gap",
        )
        self.outcome_unknown = True

    def release(self, identity: MaterialIdentity) -> None:
        # A confirmed pre-handoff failure must discard any ordinary uncommitted
        # writes before serialization is released.
        if self._connection.info.transaction_status != TransactionStatus.IDLE:
            self._connection.rollback()
        self.assert_held(identity)
        require(
            self._query("SELECT pg_advisory_unlock(%s, %s)"),
            "material_writer_protection_gap",
        )
        self._held = False


class PostgresAdvisoryWriterProtection:
    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    def acquire(self, identity: MaterialIdentity) -> _PostgresAdvisoryTicket:
        keys = _advisory_keys(identity)
        ticket = _PostgresAdvisoryTicket(self.connection, identity, keys)
        require(
            ticket._query("SELECT pg_try_advisory_lock(%s, %s)"), "material_writer_busy"
        )
        return ticket


@dataclass(frozen=True)
class _VerifiedMaterial:
    identity: MaterialIdentity
    disposition: WriteDisposition
    reconciled_outcome_unknown: bool


def _verify_payload(
    identity: MaterialIdentity,
    payload: bytes,
    classification: str = "material_content_mismatch",
) -> None:
    require(type(payload) is bytes, "mutable_material")
    require(
        len(payload) == identity.size and digest(payload) == identity.sha256,
        classification,
    )


def _verify_or_create(
    store: MaterialStore,
    ticket: WriterProtectionTicket,
    identity: MaterialIdentity,
    payload: bytes,
) -> _VerifiedMaterial:
    ticket.assert_held(identity)
    _verify_payload(identity, payload)
    reconciled = False
    try:
        disposition = store.create_if_absent(identity, payload)
    except MaterialWriteOutcomeUnknown:
        reconciled, disposition = True, WriteDisposition.CREATED
    ticket.assert_held(identity)
    observed = store.read_exact(identity)
    require(observed is not None, "material_write_outcome_unknown")
    _verify_payload(identity, observed, "material_existing_mismatch")
    ticket.assert_held(identity)
    return _VerifiedMaterial(identity, disposition, reconciled)


def _verified_live_plan(
    plan: DirectCpiLivePlan, verification: _VerifiedMaterial
) -> DirectCpiLivePlan:
    identity, raw = verification.identity, plan.raw_material
    require(raw.digest == identity.sha256, "material_plan_digest_mismatch")
    return replace(
        plan,
        raw_material=replace(
            raw, controlled_ref=identity.locator, durable=True, replayable=True
        ),
    )


def materialize_persist_publish(
    kernel: CanonicalKernel,
    plan: DirectCpiLivePlan,
    raw: bytes,
    store: MaterialStore,
    protection: WriterProtection,
    *,
    authoritative_commit: Callable[[], None] = lambda: None,
) -> MaterializedLiveResult:
    """Verify, ordinary commit, Publication last, then confirmed release."""
    require(plan.raw_material.digest is not None, "material_plan_digest_missing")
    identity = MaterialIdentity(plan.raw_material.digest, len(raw))
    ticket = protection.acquire(identity)
    authoritative_started = False
    try:
        verification = _verify_or_create(store, ticket, identity, raw)
        materialized = _verified_live_plan(plan, verification)
        version = persist_live_plan_before_publication(kernel, materialized)
        authoritative_started = True
        try:
            authoritative_commit()
        except Exception as exc:
            raise _unknown(
                ReconciliationPhase.LEDGER_HANDOFF,
                identity,
                materialized,
                ticket,
            ) from exc
        ticket.assert_held(identity)
        try:
            publication = publish_live_plan(kernel, materialized)
        except Exception as exc:
            raise _unknown(
                ReconciliationPhase.PUBLICATION,
                identity,
                materialized,
                ticket,
            ) from exc
        ticket.assert_held(identity)
        ticket.release(identity)
        observation = MaterialVerification(
            identity, verification.disposition, verification.reconciled_outcome_unknown
        )
        return MaterializedLiveResult(materialized, observation, version, publication)
    except MaterializationOutcomeUnknown:
        raise
    except Exception:
        if not authoritative_started:
            ticket.release(identity)
        raise


def _unknown(
    phase: ReconciliationPhase,
    identity: MaterialIdentity,
    plan: DirectCpiLivePlan,
    ticket: WriterProtectionTicket,
) -> MaterializationOutcomeUnknown:
    return MaterializationOutcomeUnknown(
        phase=phase,
        identity=identity,
        expected_version=plan.version,
        expected_content=plan.content,
        expected_publication_request=plan.publication_request,
        ticket=ticket,
    )
