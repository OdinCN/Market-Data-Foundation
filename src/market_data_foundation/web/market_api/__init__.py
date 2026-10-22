"""Independent bounded ASGI boundary; importing mounts nothing or opens no IO."""

from market_data_foundation.canonical.market_consumer_read import ReadError
from market_data_foundation.canonical.market_consumer_read.contracts import (
    check,
    encode,
)
from market_data_foundation.canonical.market_consumer_wire import error_body, read_body

PATH = "/mdf/consumer/market/read/v1"


def create_app(reader, dispatch=None):
    """Host injects projection, binding, clock, signing, routing and deadlines."""

    async def app(scope, receive, send):
        try:
            check(scope["type"] == "http", "HTTP_REQUIRED", 400)
            check(scope.get("path") == PATH, "NOT_FOUND", 404)
            check(scope.get("method") == "POST", "METHOD_NOT_ALLOWED", 405)
            check(not scope.get("query_string"), "JSON_BODY_REQUIRED", 400)
            headers = scope.get("headers", [])
            check(
                not any(k.lower() in {b"cursor", b"x-cursor"} for k, _ in headers),
                "JSON_BODY_REQUIRED",
                400,
            )
            types = [v for k, v in headers if k.lower() == b"content-type"]
            check(
                len(types) == 1 and types[0].split(b";", 1)[0] == b"application/json",
                "JSON_BODY_REQUIRED",
                415,
            )
            parts, size = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                check(message["type"] == "http.request", "INVALID_REQUEST", 400)
                part = message.get("body", b"")
                check(type(part) is bytes, "INVALID_REQUEST", 400)
                size += len(part)
                check(size <= reader.limits.request, "REQUEST_TOO_LARGE", 413)
                parts.append(part)
                if not message.get("more_body", False):
                    break
            raw_body = b"".join(parts)
            # The standalone offline app runs synchronously. A concurrent host
            # injects its bounded worker dispatch, as create_router does below.
            if dispatch is None:
                status, raw = read_body(reader, raw_body)
            else:
                status, raw = await dispatch(read_body, reader, raw_body)
        except ReadError as exc:
            status, raw = exc.status, encode(error_body(exc))
        except Exception:
            status, raw = 503, encode(error_body(ReadError("STORAGE_UNAVAILABLE", 503)))
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(raw)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": raw})

    return app


def create_router(reader):
    """Optional FastAPI integration; requires the host's locked dependencies."""
    from fastapi import APIRouter, Request
    from fastapi.responses import Response
    from starlette.concurrency import run_in_threadpool

    app = create_app(reader, dispatch=run_in_threadpool)
    router = APIRouter()

    async def endpoint(request: Request):
        messages = []

        async def send(message):
            messages.append(message)

        await app(request.scope, request.receive, send)
        if not messages:
            return Response(status_code=499)
        return Response(
            messages[1]["body"],
            status_code=messages[0]["status"],
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    router.add_api_route(PATH, endpoint, methods=["POST"])
    return router
