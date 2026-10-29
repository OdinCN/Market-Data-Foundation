"""Small loopback-only HTTP adapter for CPI CLI operations; no Web application.

The server is deliberately single-threaded: one service/PG connection is never
shared concurrently. Its sole listener is 127.0.0.1, and browser/proxy requests
are rejected. Importing the module does not create a listener.
"""

import ipaddress
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl, urlsplit

from market_data_foundation.historical.cpi_timeseries_cli import (
    InterfaceError,
    error_document,
    execute,
    reject_json_constant,
    unique_json_object,
)

MAX_BODY = 65536
MAX_TARGET = 8192
ROUTES = {
    ("GET", "/api/cpi/status"): "status",
    ("GET", "/api/cpi/query"): "query",
    ("POST", "/api/cpi/init"): "init",
    ("POST", "/api/cpi/update"): "update",
    ("POST", "/api/cpi/confirm-availability"): "confirm-availability",
    ("GET", "/api/cpi/schedule"): "schedule/status",
    ("GET", "/api/cpi/schedule/status"): "schedule/status",
    ("POST", "/api/cpi/schedule/refresh"): "schedule/refresh",
    ("POST", "/api/cpi/schedule/due"): "schedule/due",
    ("GET", "/api/cpi/signals"): "signals/status",
    ("GET", "/api/cpi/signals/status"): "signals/status",
    ("POST", "/api/cpi/signals/refresh"): "signals/refresh",
    ("POST", "/api/cpi/signals/import"): "signals/import",
    ("POST", "/api/cpi/signals/dismiss"): "signals/dismiss",
}


def admitted(peer, headers, port):
    """Trust the TCP peer and exact local Host, never forwarded identity."""
    try:
        if not ipaddress.ip_address(peer).is_loopback:
            return False
        pairs = list(headers.items()) if hasattr(headers, "items") else list(headers)
        normalized = {}
        for name, value in pairs:
            name = name.lower()
            if name in normalized:
                return False
            normalized[name] = value
        if normalized.get("host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return False
        if any(
            name in normalized
            for name in (
                "origin",
                "forwarded",
                "x-forwarded-for",
                "x-forwarded-host",
                "x-forwarded-proto",
                "transfer-encoding",
            )
        ):
            return False
        if normalized.get("sec-fetch-site") not in {None, "none"}:
            return False
        return True
    except ValueError, TypeError, AttributeError:
        return False


def error_status(error):
    if isinstance(error, InterfaceError):
        return 400
    classification = error_document(error)["classification"]
    if classification == "internal_error":
        return 500
    if classification in {
        "signal_scope_unverified",
        "signal_evidence_insufficient",
        "signal_expected_evidence_insufficient",
        "signal_evidence_scope_mismatch",
        "signal_discovery_evidence_mismatch",
        "signal_verified_notice_conflict",
        "signal_not_found",
        "signal_dismissal_not_allowed",
        "signal_dismissal_conflict",
        "schedule_history_start_changed",
    }:
        return 400
    if any(word in classification for word in ("invalid", "unsupported", "required")):
        return 400
    return 503


def handle_request(
    service,
    method,
    target,
    headers,
    body=b"",
    *,
    peer="127.0.0.1",
    port=18765,
    scheduler=None,
):
    """Socket-free dispatcher, also used by offline admission/route tests."""
    if not admitted(peer, headers, port):
        return 403, {"status": "ERROR", "classification": "loopback_request_required"}
    try:
        if not isinstance(target, str) or len(target) > MAX_TARGET:
            raise InterfaceError("invalid_request_target")
        parsed = urlsplit(target)
        if (
            not target.startswith("/")
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
        ):
            raise InterfaceError("invalid_request_target")
        action = ROUTES.get((method, parsed.path))
        if action is None:
            return 404, {"status": "ERROR", "classification": "route_not_found"}
        if len(body) > MAX_BODY:
            raise InterfaceError("request_body_too_large")
        if method == "GET":
            if body:
                raise InterfaceError("get_body_not_supported")
            params = {}
            try:
                pairs = parse_qsl(
                    parsed.query, keep_blank_values=True, strict_parsing=True
                )
            except ValueError:
                raise InterfaceError("invalid_query_string") from None
            for key, value in pairs:
                if key in params:
                    raise InterfaceError("duplicate_parameter")
                params[key] = value
            if "local_only" in params:
                if params["local_only"] not in {"true", "false"}:
                    raise InterfaceError("invalid_local_only")
                params["local_only"] = params["local_only"] == "true"
        else:
            if parsed.query:
                raise InterfaceError("post_query_not_supported")
            pairs = headers.items() if hasattr(headers, "items") else headers
            content_type = (
                dict((key.lower(), value) for key, value in pairs)
                .get("content-type", "")
                .split(";")[0]
                .strip()
            )
            if content_type != "application/json":
                raise InterfaceError("application_json_required")
            try:
                params = json.loads(
                    body,
                    object_pairs_hook=unique_json_object,
                    parse_constant=reject_json_constant,
                )
            except InterfaceError:
                raise
            except ValueError, UnicodeError, RecursionError:
                raise InterfaceError("invalid_json") from None
            if not isinstance(params, dict):
                raise InterfaceError("json_object_required")
        return 200, execute(service, action, params, scheduler=scheduler)
    except Exception as error:
        return error_status(error), error_document(error)


def handler_type(service, scheduler, port):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MDF-CPI"
        sys_version = ""

        def log_message(self, *_args):
            pass

        def setup(self):
            self.request.settimeout(30)
            super().setup()

        def respond(self, status, document):
            body = json.dumps(document, ensure_ascii=False, sort_keys=True).encode()
            self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def operation(self):
            if not admitted(self.client_address[0], self.headers, port):
                self.respond(
                    403,
                    {"status": "ERROR", "classification": "loopback_request_required"},
                )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= MAX_BODY:
                    raise ValueError
            except ValueError:
                self.respond(
                    400, {"status": "ERROR", "classification": "invalid_content_length"}
                )
                return
            try:
                body = self.rfile.read(length) if length else b""
                if len(body) != length:
                    raise InterfaceError("incomplete_request_body")
                status, document = handle_request(
                    service,
                    self.command,
                    self.path,
                    self.headers,
                    body,
                    peer=self.client_address[0],
                    port=port,
                    scheduler=scheduler,
                )
                self.respond(status, document)
            except OSError, TimeoutError:
                self.close_connection = True
            except InterfaceError as error:
                self.respond(400, error_document(error))

        do_GET = operation
        do_POST = operation

        def send_error(self, code, message=None, explain=None):
            self.respond(
                code, {"status": "ERROR", "classification": "http_request_rejected"}
            )

    return Handler


def serve(service, *, scheduler=None, port=18765):
    """Start only when explicitly invoked; there is no LAN/public bind option."""
    if type(port) is not int or not 1024 <= port <= 65535:
        raise InterfaceError("port_out_of_range")
    with HTTPServer(
        ("127.0.0.1", port), handler_type(service, scheduler, port)
    ) as server:
        print(
            json.dumps(
                {"status": "LISTENING", "url": f"http://127.0.0.1:{port}/api/cpi"}
            ),
            flush=True,
        )
        server.serve_forever(poll_interval=0.5)
