"""Default-off two-read official HTTPS edge. No IO happens at import time."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from market_data_foundation.source_request_policy import (
    SourceRequestFailed,
)
from market_data_foundation.source_request_policy import (
    request_source as request_with_policy,
)

from .fomc_statement import parse_calendar, parse_statement, validate_statement_url
from .fomc_statement_contract import (
    CALENDAR_URL,
    MAX_BODY_BYTES,
    CalendarDiscovery,
    StatementDocument,
)

USER_AGENT = "Market-Data-Foundation/0.1 FOMCOfficialDocument"


@dataclass(frozen=True)
class Request:
    url: str


@dataclass(frozen=True)
class Response:
    status_code: int
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()


class RequestSource(Protocol):
    def __call__(self, request: Request) -> Response: ...


@dataclass(frozen=True)
class RequestArtifact:
    logical_read: int
    request: Request
    attempts: int
    requested_at: datetime
    obtained_at: datetime
    headers: tuple[tuple[str, str], ...]
    status_code: int = 200

    def __post_init__(self):
        if (
            self.requested_at.utcoffset() is None
            or self.obtained_at.utcoffset() is None
            or self.requested_at > self.obtained_at
        ):
            raise ValueError("request_timing_invalid")


@dataclass(frozen=True)
class ArtifactPaths:
    raw: str
    request: str


class ArtifactStore(Protocol):
    def retain(self, raw: bytes, artifact: RequestArtifact) -> ArtifactPaths:
        """Publish raw and metadata atomically, or publish neither."""
        ...


class AtomicArtifactStore:
    def __init__(self, root: Path):
        if "artifacts" not in root.resolve().parts:
            raise ValueError("artifact_root_invalid")
        self.root = root

    def retain(self, raw: bytes, artifact: RequestArtifact) -> ArtifactPaths:
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
        destination = self.root / uuid4().hex
        try:
            (staging / "raw.html").write_bytes(raw)
            metadata = asdict(artifact)
            metadata.update(
                requested_at=artifact.requested_at.isoformat(),
                obtained_at=artifact.obtained_at.isoformat(),
                raw_sha256=sha256(raw).hexdigest(),
                raw_size=len(raw),
            )
            (staging / "request.json").write_text(
                json.dumps(metadata, sort_keys=True), encoding="utf-8"
            )
            os.replace(staging, destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return ArtifactPaths(
            str(destination / "raw.html"), str(destination / "request.json")
        )


@dataclass(frozen=True)
class RetainedRead:
    raw: bytes
    paths: ArtifactPaths
    artifact: RequestArtifact

    @property
    def raw_sha256(self) -> str:
        return sha256(self.raw).hexdigest()

    @property
    def raw_size(self) -> int:
        return len(self.raw)


@dataclass(frozen=True)
class ReadObservation:
    logical_read: int
    attempt: int
    classification: str


class QualificationError(RuntimeError):
    def __init__(
        self,
        classification: str,
        observations: tuple[ReadObservation, ...] = (),
        retained: tuple[RetainedRead, ...] = (),
        request_artifacts: tuple[RequestArtifact, ...] = (),
    ):
        super().__init__(classification)
        self.classification = classification
        self.observations = observations
        self.retained = retained
        self.request_artifacts = request_artifacts


@dataclass(frozen=True)
class StatementCandidate:
    reads: tuple[RetainedRead, RetainedRead]
    discovery: CalendarDiscovery
    document: StatementDocument
    observations: tuple[ReadObservation, ...]

    @property
    def attempt_counts(self) -> tuple[int, int]:
        return self.reads[0].artifact.attempts, self.reads[1].artifact.attempts


def qualify_statement(
    *,
    request_source: RequestSource,
    artifact_store: ArtifactStore,
    live_opt_in: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    after_request: Callable[[bool, int], None] | None = None,
) -> StatementCandidate:
    if live_opt_in is not True:
        raise QualificationError("live_opt_in_required")
    reads: list[RetainedRead] = []
    artifacts: list[RequestArtifact] = []
    observations: list[ReadObservation] = []

    def fail(logical: int, attempts: int, classification: str):
        observations.append(ReadObservation(logical, attempts, classification))
        raise QualificationError(
            classification, tuple(observations), tuple(reads), tuple(artifacts)
        ) from None

    def read(url: str) -> bytes:
        logical = len(reads) + 1
        request = Request(url)
        requested_at = clock()
        try:
            response, attempts = request_with_policy(
                lambda: request_source(request),
                status=lambda response: response.status_code,
                sleep=sleep,
            )
        except SourceRequestFailed as error:
            fail(logical, error.attempts, error.classification)
        try:
            artifact = RequestArtifact(
                logical, request, attempts, requested_at, clock(), response.headers
            )
        except Exception:
            fail(logical, attempts, "request_metadata_invalid")
        artifacts.append(artifact)
        try:
            paths = artifact_store.retain(response.body, artifact)
        except Exception:
            fail(logical, attempts, "artifact_error")
        reads.append(RetainedRead(response.body, paths, artifact))
        observations.append(ReadObservation(logical, attempts, "retained"))
        if after_request is not None:
            try:
                after_request(True, attempts)
            except Exception:
                fail(logical, attempts, "request_observation_error")
        return response.body

    raw = read(CALENDAR_URL)
    try:
        discovery = parse_calendar(raw)
    except Exception:
        fail(1, reads[-1].artifact.attempts, "calendar_parser_error")
    raw = read(discovery.statement_url)
    try:
        document = parse_statement(raw, discovery.statement_url)
    except Exception:
        fail(2, reads[-1].artifact.attempts, "statement_parser_error")
    return StatementCandidate(
        (reads[0], reads[1]), discovery, document, tuple(observations)
    )


class ExactFomcHttpsSource:
    """One bounded transport attempt. HTTP redirects are returned, never followed."""

    def __call__(self, request: Request) -> Response:
        if request.url != CALENDAR_URL:
            try:
                validate_statement_url(request.url)
            except ValueError:
                raise http.client.InvalidURL("request_invalid") from None
        connection = http.client.HTTPSConnection("www.federalreserve.gov", timeout=20)
        try:
            connection.request(
                "GET",
                urlsplit(request.url).path,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
            headers = {}
            for name, value in response.getheaders():
                name = name.lower()
                if name in headers or any(c in value for c in "\r\n\x00"):
                    raise http.client.HTTPException("headers_invalid")
                headers[name] = value
            if response.status != 200:
                return Response(response.status, b"")
            if headers.get("content-type", "").lower() not in {
                "text/html",
                "text/html; charset=utf-8",
                "text/html;charset=utf-8",
            }:
                raise http.client.HTTPException("content_type_invalid")
            if headers.get("content-encoding", "identity").lower() != "identity":
                raise http.client.HTTPException("content_encoding_invalid")
            transfer = headers.get("transfer-encoding")
            length = headers.get("content-length")
            if transfer not in {None, "chunked"} or (transfer and length is not None):
                raise http.client.HTTPException("framing_invalid")
            if length is not None and (
                not length.isascii()
                or not length.isdecimal()
                or len(length) > 7
                or int(length) > MAX_BODY_BYTES
            ):
                raise http.client.HTTPException("content_length_invalid")
            chunks = []
            size = 0
            while True:
                chunk = response.read(min(65_536, MAX_BODY_BYTES + 1 - size))
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    raise http.client.HTTPException("body_too_large")
                if not chunk:
                    break
                chunks.append(chunk)
            if length is not None and size != int(length):
                raise http.client.HTTPException("content_length_mismatch")
            # Retain only protocol metadata, never arbitrary response headers.
            safe_headers = tuple(
                (k, v)
                for k, v in headers.items()
                if k
                in {
                    "content-type",
                    "content-length",
                    "content-encoding",
                    "transfer-encoding",
                }
            )
            return Response(200, b"".join(chunks), safe_headers)
        finally:
            connection.close()


def execute_live_candidate(
    *,
    enabled: bool = False,
    artifact_root: Path = Path("artifacts/t07-fomc-statement"),
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> StatementCandidate:
    if enabled is not True:
        raise QualificationError("live_opt_in_required")
    return qualify_statement(
        request_source=ExactFomcHttpsSource(),
        artifact_store=AtomicArtifactStore(artifact_root),
        live_opt_in=True,
        clock=clock,
        sleep=sleep,
    )
