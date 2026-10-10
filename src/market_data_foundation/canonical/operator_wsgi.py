"""Bounded WSGI host boundary for OperatorWebEntry; no server or identity policy."""

from collections.abc import Callable, Iterator
from http import HTTPStatus
from wsgiref.types import StartResponse, WSGIEnvironment

from .operator_web import (
    MAX_TARGET_BYTES,
    OperatorWebEntry,
    OperatorWebRequest,
    OperatorWebResponse,
    _admit,
    _error,
)


def _bytes(value: object, limit: int) -> bytes:
    # PEP 3333 transports original octets in native strings via Latin-1.
    # Bound before encoding; never str(), UTF-8 repair or percent-decode input.
    if type(value) is not str or len(value) > limit:
        raise ValueError
    return value.encode("latin-1")


def _request(environ: WSGIEnvironment) -> OperatorWebRequest:
    if type(environ) is not dict:
        raise ValueError
    method = _bytes(environ.get("REQUEST_METHOD"), 16)
    path = _bytes(environ.get("PATH_INFO", ""), MAX_TARGET_BYTES)
    query = _bytes(environ.get("QUERY_STRING", ""), MAX_TARGET_BYTES)
    # PATH_INFO is already decoded by WSGI. Delimiters here are path octets,
    # not permission to inject a query/fragment while joining the two fields.
    if b"?" in path or b"#" in path or len(path) + 1 + len(query) > MAX_TARGET_BYTES:
        raise ValueError

    # No body is admitted. Do not read/probe a WSGI stream past a declared zero
    # length or EOF: it may block. Missing/empty CGI length means zero per WSGI.
    length = _bytes(environ.get("CONTENT_LENGTH", ""), 1024)
    if length and any(byte != ord("0") for byte in length):
        raise ValueError
    if (
        "wsgi.input_terminated" in environ
        and environ["wsgi.input_terminated"] is not False
    ):
        # EOF-delimited extension bodies cannot establish a zero-length frame.
        raise ValueError
    headers = []
    if length:
        headers.append((b"Content-Length", length))
    if "CONTENT_TYPE" in environ:
        headers.append((b"Content-Type", _bytes(environ["CONTENT_TYPE"], 1024)))
    for key, value in environ.items():
        if type(key) is not str:
            raise ValueError
        if not key.startswith("HTTP_"):
            continue
        # WSGI/CGI uppercases header names and replaces hyphens with underscores.
        if len(key) > 69:
            raise ValueError
        suffix = _bytes(key[5:], 64)
        if suffix != suffix.upper():
            raise ValueError
        name = suffix.replace(b"_", b"-")
        if name.lower() in {b"content-length", b"content-type", b"transfer-encoding"}:
            raise ValueError
        headers.append((name, _bytes(value, 1024)))
        if len(headers) > 16:
            raise ValueError
    return OperatorWebRequest(method, path + b"?" + query, tuple(headers))


class OperatorWSGIGateway:
    """Synchronous PEP 3333 callable with a separate host-only context factory.

    context_factory() receives no environ or request arguments. The host must
    obtain its capability independently of request data, and configure the
    existing entry's exact-selection authorizer and consistent read-only scope.
    No callback/None/failure denies; no request field supplies a default context.
    This is not a security boundary against malicious in-process Python code.
    """

    def __init__(
        self,
        entry: OperatorWebEntry | None = None,
        *,
        context_factory: Callable[[], object] | None = None,
    ) -> None:
        if entry is not None and type(entry) is not OperatorWebEntry:
            raise TypeError("OperatorWebEntry required")
        self._entry = OperatorWebEntry() if entry is None else entry
        self._context_factory = context_factory

    def __call__(
        self, environ: WSGIEnvironment, start_response: StartResponse
    ) -> tuple[bytes] | Iterator[bytes]:
        try:
            request = _request(environ)
            # Reuse the delivered syntax/budget admission before touching the
            # host context. handle() retains its own admission unmodified.
            admitted = _admit(request)
        except BaseException:
            admitted = _error(400, "invalid_request")
        if type(admitted) is OperatorWebResponse:
            response = admitted
        else:
            context = None
            try:
                if self._context_factory is not None:
                    context = self._context_factory()
            except BaseException:
                pass
            response = self._entry.handle(request, operator_context=context)
        # All status/read/cleanup/render work has finished. Transport failures
        # propagate to the WSGI host; never retry start_response or emit write().
        is_head = (
            type(environ) is dict
            and type(environ.get("REQUEST_METHOD")) is str
            and environ["REQUEST_METHOD"] == "HEAD"
        )
        headers = [
            (name, value)
            for name, value in response.headers
            if not (is_head and name.lower() == "content-length")
            and not (response.status == 405 and name.lower() == "allow")
        ]
        if response.status == 405:
            headers.append(("Allow", "GET"))
        start_response(
            f"{response.status} {HTTPStatus(response.status).phrase}",
            headers,
        )
        if is_head:
            # Send headers with one empty block but no iterable length: a bare
            # SimpleHandler must not infer Content-Length: 0 for this HEAD.
            return iter((b"",))
        return (response.body,)
