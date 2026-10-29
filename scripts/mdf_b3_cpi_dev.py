"""Explicit DEV Host entry points; check/help are offline and create no sockets."""

import argparse
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from fastapi import Request
from fastapi.testclient import TestClient

from market_data_foundation.canonical.model import require
from market_data_foundation.historical.cpi_pg_binding import SCHEMA
from market_data_foundation.historical.cpi_pg_journal import PgJournalIdentity
from market_data_foundation.storage.postgres_runtime_config import load_pg_config
from market_data_foundation.web.cpi_dev_host import (
    BINDING,
    CONFIG,
    LISTENER,
    ORIGIN,
    DevHost,
    admitted,
    candidate_spec,
    create_preview,
    job_identity,
)

ROOT = Path(__file__).resolve().parents[1]


def initialize_schema(*, enabled=False):
    require(enabled is True, "cpi_dev_ddl_opt_in_required")
    profile = load_pg_config(CONFIG / "runtime.toml")
    # DDL creates a fresh namespace; there is no pre-existing OID to trust here.
    host = DevHost(PgJournalIdentity(profile.user, 1), enabled=True)
    with host.connection() as connection:
        BINDING.target.check_connection(connection)
        require(connection.pgconn.ssl_in_use is True, "cpi_dev_tls_required")
        row = connection.execute(
            "SELECT current_user, session_user, current_database(), "
            "host(inet_server_addr()), inet_server_port()"
        ).fetchone()
        require(
            row == (profile.user, profile.user, profile.database, profile.host, 5432),
            "cpi_dev_identity_mismatch",
        )
        # CREATE SCHEMA deliberately fails on collision. Never migrate/reuse/drop.
        connection.execute((ROOT / "db/candidates/b3_05_cpi_dev_r1.sql").read_text())
        row = connection.execute(
            "SELECT oid, pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=%s",
            (SCHEMA,),
        ).fetchone()
        return {
            "status": "DDL_RETURNED",
            "schema": SCHEMA,
            "schema_oid": row[0],
            "schema_owner": row[1],
        }


class PreviewHandler(BaseHTTPRequestHandler):
    """Small bounded LAN preview adapter over the qualified ASGI TestClient.

    Not a production server. TCP peer is explicitly passed into ASGI; headers
    cannot impersonate it. One request per connection, no proxy, no access logs.
    """

    def log_message(self, *_args):
        pass

    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def handle(self):
        def expire():
            try:
                self.request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(20, expire)
        timer.daemon = True
        timer.start()
        try:
            super().handle()
        except OSError, TimeoutError:
            pass
        finally:
            timer.cancel()

    def respond(self, status, body=b"", content_type="application/json"):
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def dispatch(self):
        try:
            require(
                self.path.startswith("/")
                and not self.path.startswith("//")
                and len(self.path) <= 3000,
                "preview_path_invalid",
            )
            headers = [
                (k.lower().encode(), v.encode()) for k, v in self.headers.items()
            ]
            path, _, query = self.path.partition("?")
            scope = dict(
                type="http",
                method=self.command,
                scheme="http",
                path=path,
                query_string=query.encode(),
                headers=headers,
                client=self.client_address,
                server=LISTENER,
            )
            if not admitted(Request(scope)):
                return self.respond(403)
            lengths = self.headers.get_all("Content-Length", [])
            require(
                len(lengths) <= 1 and "Transfer-Encoding" not in self.headers,
                "preview_framing_invalid",
            )
            size = int(lengths[0]) if lengths else 0
            require(0 <= size <= 8192, "preview_body_too_large")
            require(self.command == "POST" or size == 0, "preview_body_forbidden")
            body = self.rfile.read(size)
            require(len(body) == size, "preview_body_incomplete")
            with TestClient(
                self.server.app, base_url=ORIGIN, client=self.client_address
            ) as client:
                result = client.request(
                    self.command,
                    self.path,
                    headers=headers,
                    content=body,
                    follow_redirects=False,
                )
            require(len(result.content) <= 2_000_000, "preview_response_too_large")
            return self.respond(
                result.status_code,
                result.content,
                result.headers.get("content-type", "application/json"),
            )
        except Exception:
            return self.respond(503, b'{"status":"UNKNOWN"}')

    do_GET = dispatch
    do_HEAD = dispatch
    do_POST = dispatch
    do_PUT = dispatch
    do_PATCH = dispatch
    do_DELETE = dispatch
    do_OPTIONS = dispatch


def serve(app):
    with HTTPServer(LISTENER, PreviewHandler) as server:
        server.app = app
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["check", "ddl", "preflight", "serve", "worker"]
    )
    parser.add_argument("--opt-in-dev-host", action="store_true")
    parser.add_argument("--opt-in-create-schema", action="store_true")
    parser.add_argument("--opt-in-provider-garage-write", action="store_true")
    parser.add_argument(
        "--job-id", help="Exact registered INIT or R8 successor identity"
    )
    parser.add_argument("--schema-owner")
    parser.add_argument("--schema-oid", type=int)
    args = parser.parse_args()
    os.umask(0o077)
    require(sys.version_info[:3] == (3, 14, 8), "python_baseline_mismatch")
    if args.action == "check":
        BINDING.check_spec(candidate_spec().document(), candidate_spec().scope)
        print(
            json.dumps(
                {
                    "status": "OFFLINE_CODE_CHECK",
                    "binding": BINDING.document(),
                    "job_identity": job_identity(),
                    "origin": ORIGIN,
                }
            )
        )
        return
    require(args.opt_in_dev_host, "cpi_dev_host_opt_in_required")
    if args.action == "ddl":
        print(json.dumps(initialize_schema(enabled=args.opt_in_create_schema)))
        return
    require(
        args.schema_owner and args.schema_oid, "independent_schema_identity_required"
    )
    host = DevHost(PgJournalIdentity(args.schema_owner, args.schema_oid), enabled=True)
    if args.action == "worker":
        result = host.worker_once(
            ROOT / "artifacts/b3-05-cpi-r8",
            enabled=args.opt_in_provider_garage_write,
            job_id=args.job_id,
        )
        print(
            json.dumps(
                {
                    "job_identity": result["job_id"],
                    "state": result["state"],
                    "classification": result["classification"],
                }
            )
        )
        return
    host.preflight(candidate_spec())
    if args.action == "preflight":
        print(json.dumps({"status": "HOST_PREFLIGHT_RETURNED", "schema": SCHEMA}))
        return
    ui = ROOT / "web/dist"
    require((ui / "index.html").is_file(), "preview_ui_build_required")
    serve(
        create_preview(host.journal(), host.read_scope(), ui_directory=ui, enabled=True)
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(
            json.dumps({"status": "UNKNOWN", "exception_class": type(error).__name__})
        )
        sys.exit(1)
