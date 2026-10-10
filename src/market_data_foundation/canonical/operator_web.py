"""Internal HTTP-shaped, in-process Operator adapter; no listener or public wire.

Host code alone supplies authorization, an out-of-band context and an exclusive
consistent read-only scope factory. Request headers never supply that context.
Unconfigured hosts deny every valid request. These hooks are prerequisites, not
an authentication implementation or a security boundary against hostile Python.
"""

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime

from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES

from .model import Key
from .operator_reads import OperatorReadSource
from .operator_report import MAX_REPORT_BYTES, render_operator_report
from .operator_status import inspect_operator_status

MAX_TARGET_BYTES = 512
MAX_HEADER_BYTES = 4096
_DEFINITIONS = {
    "bls-cpi-headlines": BLS_CPI_HEADLINES.key,
    "raw-daily-bars": RAW_DAILY_BARS.key,
}
_TIMESTAMP = re.compile(
    r"[0-9]{4}(?:-[0-9]{2}){2}T[0-9]{2}(?::[0-9]{2}){2}(?:\.[0-9]{6})?Z"
)
_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


@dataclass(frozen=True)
class OperatorWebRequest:
    method: bytes
    target: bytes
    headers: tuple[tuple[bytes, bytes], ...] = ()
    body: bytes = b""


@dataclass(frozen=True)
class OperatorWebResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


def _response(status: int, body: bytes, *, html: bool = False) -> OperatorWebResponse:
    return OperatorWebResponse(
        status,
        (
            (
                "Content-Type",
                "text/html; charset=utf-8" if html else "text/plain; charset=utf-8",
            ),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Content-Security-Policy", _CSP),
        ),
        body,
    )


def _error(status: int, classification: str) -> OperatorWebResponse:
    # Only adapter-owned literal classifications reach this function.
    return _response(status, (classification + "\n").encode("ascii"))


def _admit(request: OperatorWebRequest):
    if type(request) is not OperatorWebRequest:
        return _error(400, "invalid_request")
    if (
        type(request.method) is not bytes
        or not 1 <= len(request.method) <= 16
        or type(request.target) is not bytes
        or not 1 <= len(request.target) <= MAX_TARGET_BYTES
        or type(request.body) is not bytes
        or request.body != b""
        or type(request.headers) is not tuple
        or len(request.headers) > 16
    ):
        return _error(400, "invalid_request")
    total = 0
    names = set()
    for header in request.headers:
        if type(header) is not tuple or len(header) != 2:
            return _error(400, "invalid_request")
        name, value = header
        if (
            type(name) is not bytes
            or not 1 <= len(name) <= 64
            or type(value) is not bytes
            or len(value) > 1024
        ):
            return _error(400, "invalid_request")
        total += len(name) + len(value)
        if total > MAX_HEADER_BYTES:
            return _error(400, "invalid_request")
        if (
            _HEADER_NAME.fullmatch(name) is None
            or any(byte < 32 or byte > 126 for byte in value)
            or name.lower() in names
        ):
            return _error(400, "invalid_request")
        names.add(name.lower())
    if any(byte < 65 or byte > 90 for byte in request.method) or any(
        byte < 33 or byte > 126 for byte in request.target
    ):
        return _error(400, "invalid_request")
    if request.method != b"GET":
        return _error(405, "method_not_allowed")
    path, separator, query = request.target.decode("ascii").partition("?")
    if path != "/operator/report":
        return _error(404, "route_not_found")
    if not separator:
        return _error(400, "invalid_request")
    # Internal raw target syntax: no percent decoding, implicit defaults, extra
    # fields, repeated fields, fragments or normalization of authority claims.
    fields = query.split("&")
    if len(fields) != 2 or any(field.count("=") != 1 for field in fields):
        return _error(400, "invalid_request")
    pairs = [field.split("=") for field in fields]
    parameters = dict(pairs)
    if len(parameters) != 2 or set(parameters) != {"definition", "as_of"}:
        return _error(400, "invalid_request")
    definition = _DEFINITIONS.get(parameters["definition"])
    if definition is None:
        return _error(400, "definition_not_admitted")
    timestamp = parameters["as_of"]
    if _TIMESTAMP.fullmatch(timestamp) is None:
        return _error(400, "invalid_as_of")
    try:
        as_of = datetime.fromisoformat(timestamp)
    except ValueError:
        return _error(400, "invalid_as_of")
    precision = "microseconds" if "." in timestamp else "seconds"
    if as_of.isoformat(timespec=precision).replace("+00:00", "Z") != timestamp:
        return _error(400, "invalid_as_of")
    return definition, as_of


class OperatorWebEntry:
    """One synchronous request; callbacks/context come exclusively from the host.

    authorize must independently establish permission for this exact selection
    and return the literal bool True. A client identity/role claim is insufficient.
    snapshot must supply an exclusively held consistent read-only context (for
    PostgreSQL, the existing ledger.operator_snapshot). No defaults or fallback
    acquire a connection. Host callbacks must not suppress inspection failures.
    """

    def __init__(
        self,
        *,
        authorize: Callable[[object, Key, datetime], bool] | None = None,
        snapshot: Callable[[], AbstractContextManager[OperatorReadSource]]
        | None = None,
    ) -> None:
        self._authorize = authorize
        self._snapshot = snapshot

    def handle(
        self, request: OperatorWebRequest, *, operator_context: object = None
    ) -> OperatorWebResponse:
        admitted = _admit(request)
        if type(admitted) is OperatorWebResponse:
            return admitted
        definition, as_of = admitted
        if self._authorize is None or operator_context is None:
            return _error(403, "operator_forbidden")
        try:
            authorized = self._authorize(operator_context, definition, as_of) is True
        except BaseException:
            # No exception property access, stringification, traceback or retry.
            return _error(403, "operator_forbidden")
        if not authorized:
            return _error(403, "operator_forbidden")
        if self._snapshot is None:
            return _error(503, "operator_status_unavailable")
        completed = False
        try:
            with self._snapshot() as scope:
                status = inspect_operator_status(scope, definition, as_of=as_of)
                completed = True
            if not completed:
                # A custom manager suppressing a failed inspection is no success.
                return _error(503, "operator_status_unavailable")
        except BaseException:
            return _error(503, "operator_status_unavailable")
        try:
            # Only after successful exit. No callbacks emit/stream partial data.
            html = render_operator_report(status)
            if type(html) is not str or len(html) > MAX_REPORT_BYTES:
                return _error(503, "operator_report_unavailable")
            body = html.encode("utf-8")
            if len(body) > MAX_REPORT_BYTES:
                return _error(503, "operator_report_unavailable")
        except BaseException:
            return _error(503, "operator_report_unavailable")
        return _response(200, body, html=True)
