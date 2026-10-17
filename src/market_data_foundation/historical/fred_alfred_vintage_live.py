"""Explicitly opted-in, injected IO orchestration for T06 Stage-1.

The request source owns authentication. Credentials never enter request artifacts.
A store must publish the raw body and request artifact as one atomic unit.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from market_data_foundation.source_request_policy import (
    SOURCE_REQUEST_POLICY,
    SourceRequestFailed,
    classify_source_exception,
)
from market_data_foundation.source_request_policy import (
    request_source as request_with_policy,
)

from .fred_alfred_vintage import parse_discovery, parse_observations
from .vintage_contract import (
    METADATA,
    OBSERVATION_END,
    OBSERVATION_START,
    SERIES,
    SemanticBundle,
)

BASE_URL = "https://api.stlouisfed.org/fred/series/"


@dataclass(frozen=True)
class Request:
    series: str
    endpoint: str
    parameters: tuple[tuple[str, str], ...]

    @property
    def url(self) -> str:
        return BASE_URL + self.endpoint


@dataclass(frozen=True)
class Response:
    status_code: int
    body: bytes


class RequestSource(Protocol):
    def __call__(self, request: Request) -> Response:
        """Perform exactly one attempt, using private authentication if needed."""
        ...


@dataclass(frozen=True)
class RequestArtifact:
    logical_read: int
    request: Request
    attempts: int
    requested_at: datetime
    obtained_at: datetime
    status_code: int = 200

    def __post_init__(self) -> None:
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
        """Atomically publish both artifacts or raise without publishing either."""
        ...


class AtomicArtifactStore:
    """Publish a directory containing both files by one same-filesystem rename.

    The caller supplies a destination under an ignored artifacts/ directory.
    No request-source exceptions, response headers or credentials are serialized.
    """

    def __init__(self, root: Path):
        if "artifacts" not in root.parts:
            raise ValueError("retention destination must be under artifacts/")
        self.root = root

    def retain(self, raw: bytes, artifact: RequestArtifact) -> ArtifactPaths:
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
        destination = self.root / uuid4().hex
        try:
            (staging / "raw.json").write_bytes(raw)
            request_document = {
                "logical_read": artifact.logical_read,
                "series": artifact.request.series,
                "url": artifact.request.url,
                "parameters": dict(artifact.request.parameters),
                "attempts": artifact.attempts,
                "status_code": artifact.status_code,
                "requested_at": artifact.requested_at.isoformat(),
                "obtained_at": artifact.obtained_at.isoformat(),
            }
            (staging / "request.json").write_text(
                json.dumps(request_document, sort_keys=True), encoding="utf-8"
            )
            os.replace(staging, destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return ArtifactPaths(
            str(destination / "raw.json"), str(destination / "request.json")
        )


@dataclass(frozen=True)
class ReadObservation:
    logical_read: int
    attempt: int
    classification: str


class QualificationError(RuntimeError):
    """Secret-safe failure with classifications, never raw exception messages."""

    def __init__(
        self,
        classification: str,
        observations: tuple[ReadObservation, ...],
        retained: tuple[RetainedRead, ...] = (),
        request_artifacts: tuple[RequestArtifact, ...] = (),
    ):
        super().__init__(classification)
        self.classification = classification
        self.observations = observations
        self.retained = retained
        self.request_artifacts = request_artifacts


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
class VintageCandidate:
    reads: tuple[RetainedRead, ...]
    selections: tuple[tuple[str, tuple[str, str]], ...]
    semantic_bundle: SemanticBundle
    observations: tuple[ReadObservation, ...]

    @property
    def attempt_counts(self) -> tuple[int, ...]:
        return tuple(read.artifact.attempts for read in self.reads)


def _request(series: str, vintages: tuple[str, str] | None = None) -> Request:
    parameters = {"series_id": series, "file_type": "json"}
    if vintages is None:
        parameters.update(sort_order="desc", limit="2")
        endpoint = "vintagedates"
    else:
        parameters.update(
            output_type="2",
            vintage_dates=",".join(vintages),
            observation_start=OBSERVATION_START,
            observation_end=OBSERVATION_END,
            units="lin",
        )
        endpoint = "observations"
    return Request(series, endpoint, tuple(parameters.items()))


def qualify_vintages(
    *,
    request_source: RequestSource,
    artifact_store: ArtifactStore,
    live_opt_in: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> VintageCandidate:
    """Read GDP discovery/observations, then PAYEMS discovery/observations.

    Every logical read receives its own three-attempt budget. Only transport
    exceptions and HTTP statuses allowed by the shared policy are retried.
    HTTP 200 retention and parsing are terminal boundaries: neither can cause
    another provider request.
    """
    if live_opt_in is not True:
        raise QualificationError("live_opt_in_required", ())
    observations: list[ReadObservation] = []
    reads: list[RetainedRead] = []
    request_artifacts: list[RequestArtifact] = []
    selections = []
    semantic_rows = []

    def fail(logical: int, attempt: int, classification: str) -> None:
        observations.append(ReadObservation(logical, attempt, classification))
        raise QualificationError(
            classification, tuple(observations), tuple(reads), tuple(request_artifacts)
        ) from None

    def read(request: Request) -> bytes:
        logical = len(reads) + 1
        requested_at = clock()
        try:
            response, attempts = request_with_policy(
                lambda: request_source(request),
                status=lambda response: response.status_code,
                classify=classify_source_exception,
                sleep=sleep,
            )
        except SourceRequestFailed as error:
            classification = error.classification
            if error.http_status is not None:
                classification = (
                    "request_exhausted"
                    if SOURCE_REQUEST_POLICY.retryable_http_status(error.http_status)
                    else "http_terminal"
                )
            elif error.attempts == SOURCE_REQUEST_POLICY.max_transport_attempts:
                classification = "request_exhausted"
            fail(logical, error.attempts, classification)
        obtained_at = clock()
        try:
            artifact = RequestArtifact(
                logical, request, attempts, requested_at, obtained_at
            )
        except ValueError:
            fail(logical, attempts, "request_timing_invalid")
        request_artifacts.append(artifact)
        try:
            paths = artifact_store.retain(response.body, artifact)
        except Exception:
            fail(logical, attempts, "artifact_error")
        reads.append(RetainedRead(response.body, paths, artifact))
        observations.append(ReadObservation(logical, attempts, "retained"))
        return response.body

    for series in SERIES:
        raw = read(_request(series))
        try:
            vintages = parse_discovery(raw)
        except Exception:
            fail(len(reads), reads[-1].artifact.attempts, "parser_error")
        selections.append((series, vintages))
        raw = read(_request(series, vintages))
        try:
            semantic_rows.extend(parse_observations(raw, series, vintages))
        except Exception:
            fail(len(reads), reads[-1].artifact.attempts, "parser_error")
    return VintageCandidate(
        tuple(reads),
        tuple(selections),
        SemanticBundle(
            tuple(METADATA[series] for series in SERIES), tuple(semantic_rows)
        ),
        tuple(observations),
    )


class VintageLiveError(QualificationError):
    """A secret-safe setup failure."""

    def __init__(self, classification: str):
        super().__init__(classification, ())


def _safe_api_key(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and all(33 <= ord(char) <= 126 for char in value)
    )


def load_api_key(secret_path: Path = Path("config/local/secret.toml")) -> str:
    try:
        with secret_path.open("rb") as stream:
            key = tomllib.load(stream)["fred"]["api_key"]
        if not _safe_api_key(key):
            raise ValueError("secret_invalid")
    except Exception:
        raise VintageLiveError("secret_load_failed") from None
    return key


class ExactFredHttpsSource:
    """One bounded HTTPS exchange; authentication exists only on the wire."""

    def __init__(self, api_key: str):
        if not _safe_api_key(api_key):
            raise VintageLiveError("secret_invalid")
        self._api_key = api_key

    def __call__(self, request: Request) -> Response:
        connection = None
        try:
            if request.endpoint not in ("vintagedates", "observations") or any(
                name.lower() == "api_key" for name, _ in request.parameters
            ):
                raise http.client.InvalidURL("request_invalid")
            path = urlsplit(request.url).path
            query = urlencode((*request.parameters, ("api_key", self._api_key)))
            connection = http.client.HTTPSConnection("api.stlouisfed.org", timeout=20)
            connection.request(
                "GET",
                path + "?" + query,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            )
            response = connection.getresponse()
            headers = {}
            for name, value in response.getheaders():
                name = name.lower()
                if name in headers:
                    raise http.client.HTTPException("duplicate_header")
                headers[name] = value
            length = headers.get("content-length")
            if length is not None:
                if not length or not length.isascii() or not length.isdecimal():
                    raise http.client.HTTPException("content_length_invalid")
                if len(length) > 7 or int(length) > 1_000_000:
                    raise http.client.HTTPException("body_too_large")
                if "transfer-encoding" in headers:
                    raise http.client.HTTPException("content_length_ambiguous")
            chunks = []
            size = 0
            while True:
                chunk = response.read(min(65_536, 1_000_001 - size))
                size += len(chunk)
                if size > 1_000_000:
                    raise http.client.HTTPException("body_too_large")
                if not chunk:
                    break
                chunks.append(chunk)
            if length is not None and size != int(length):
                raise http.client.HTTPException("content_length_mismatch")
            return Response(response.status, b"".join(chunks))
        except Exception as error:
            classification, _ = classify_source_exception(error)
            if classification == "timeout":
                raise TimeoutError(classification) from None
            if classification == "transport_failure":
                raise OSError(classification) from None
            if classification == "request_invalid":
                raise http.client.InvalidURL(classification) from None
            raise http.client.HTTPException(classification) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


def execute_live_candidate(
    *,
    enabled: bool = False,
    secret_path: Path = Path("config/local/secret.toml"),
    artifact_root: Path = Path("artifacts/t06-fred-alfred-vintage"),
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> VintageCandidate:
    if enabled is not True:
        raise VintageLiveError("live_opt_in_required")
    key = load_api_key(secret_path)
    return qualify_vintages(
        request_source=ExactFredHttpsSource(key),
        artifact_store=AtomicArtifactStore(artifact_root),
        live_opt_in=True,
        clock=clock,
        sleep=sleep,
    )
