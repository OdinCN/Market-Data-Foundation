"""Offline check or explicitly enabled, fixed BEA PCE DEV Host operations."""

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
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.historical.bea_pce_live import ExecutionIdentity
from market_data_foundation.historical.pce_pg_binding import (
    BINDING,
    SCHEMA,
    backfill_identity,
    candidate_spec,
    job_identity,
)
from market_data_foundation.historical.pce_pg_journal import PgJournalIdentity
from market_data_foundation.storage.postgres_runtime_config import load_pg_config
from market_data_foundation.web.pce_api import catalog, create_preview
from market_data_foundation.web.pce_dev_host import (
    CONFIG,
    LISTENER,
    ORIGIN,
    DevHost,
    admitted,
    clock,
)

ROOT = Path(__file__).resolve().parents[1]


def initialize_schema(host, *, create=False, enabled=False):
    require(enabled is True, "pce_ddl_opt_in_required")
    require(host.identity.schema_owner == host.profile.user, "pce_ddl_owner_required")
    if not create:
        # Validates the independently supplied OID, owner, canonical tables and
        # bounded existing content before append. No assumption of emptiness.
        with host.read_scope():
            pass
    with host.connection() as connection:
        BINDING.target.check_connection(connection)
        require(connection.pgconn.ssl_in_use is True, "pce_ddl_tls_required")
        row = connection.execute(
            "SELECT current_user, session_user, current_database(), "
            "host(inet_server_addr()), inet_server_port()"
        ).fetchone()
        require(
            row
            == (
                host.profile.user,
                host.profile.user,
                host.profile.database,
                host.profile.host,
                5432,
            ),
            "pce_ddl_identity_mismatch",
        )
        with connection.transaction():
            connection.execute("SET LOCAL lock_timeout = '2000ms'")
            if create:
                PostgresLedger(connection, SCHEMA).create_namespace()
            else:
                connection.execute(
                    f"LOCK TABLE {SCHEMA}.records, {SCHEMA}.contents, "
                    f"{SCHEMA}.publications IN ACCESS EXCLUSIVE MODE"
                )
                observed = connection.execute(
                    "SELECT oid, pg_get_userbyid(nspowner) FROM pg_namespace "
                    "WHERE nspname=%s",
                    (SCHEMA,),
                ).fetchone()
                require(
                    observed == (host.identity.schema_oid, host.identity.schema_owner),
                    "pce_ddl_schema_identity_changed",
                )
            # Colliding journal tables fail closed; transaction leaves no partial DDL.
            connection.execute(
                (ROOT / "db/candidates/b3_bea_pce_jobs_r1.sql").read_text()
            )
            identity = connection.execute(
                "SELECT oid, pg_get_userbyid(nspowner) FROM pg_namespace "
                "WHERE nspname=%s",
                (SCHEMA,),
            ).fetchone()
        return {
            "status": "DDL_RETURNED",
            "schema": SCHEMA,
            "schema_oid": identity[0],
            "schema_owner": identity[1],
        }


class PreviewHandler(BaseHTTPRequestHandler):
    """Small bounded LAN preview adapter over the qualified ASGI TestClient.

    Not a production server. TCP peer is explicitly passed into ASGI; headers
    cannot impersonate it. One request per connection, no proxy, no access logs.
    """

    origin = ORIGIN
    listener = LISTENER
    body_methods = {"POST"}
    body_limit = 8192
    response_limit = 2_000_000

    def request_admitted(self, request):
        return admitted(request, origin=self.origin)

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

    def respond(self, status, body=b"", content_type="application/json", location=None):
        # Forward only the same-origin /pce redirect. Without Location,
        # browsers show the response as a blank document.
        require(
            location is None or (status == 307 and location == "/pce"),
            "preview_redirect_not_allowlisted",
        )
        require(status != 307 or location == "/pce", "preview_redirect_missing")
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        if location is not None:
            self.send_header("Location", location)
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
                server=self.listener,
            )
            if not self.request_admitted(Request(scope)):
                return self.respond(403)
            lengths = self.headers.get_all("Content-Length", [])
            require(
                len(lengths) <= 1 and "Transfer-Encoding" not in self.headers,
                "preview_framing_invalid",
            )
            size = int(lengths[0]) if lengths else 0
            require(0 <= size <= self.body_limit, "preview_body_too_large")
            require(
                self.command in self.body_methods or size == 0, "preview_body_forbidden"
            )
            body = self.rfile.read(size)
            require(len(body) == size, "preview_body_incomplete")
            with TestClient(
                self.server.app, base_url=self.origin, client=self.client_address
            ) as client:
                result = client.request(
                    self.command,
                    self.path,
                    headers=headers,
                    content=body,
                    follow_redirects=False,
                )
            require(
                len(result.content) <= self.response_limit, "preview_response_too_large"
            )
            return self.respond(
                result.status_code,
                result.content,
                result.headers.get("content-type", "application/json"),
                result.headers.get("location"),
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
    parser.add_argument("--opt-in-ddl", action="store_true")
    parser.add_argument("--create-absent-schema", action="store_true")
    parser.add_argument("--opt-in-provider-garage-write", action="store_true")
    parser.add_argument("--schema-owner")
    parser.add_argument("--schema-oid", type=int)
    parser.add_argument("--code-git-sha")
    parser.add_argument("--code-manifest-sha256")
    parser.add_argument("--observation-id")
    parser.add_argument(
        "--job-id",
        choices=[job_identity(), backfill_identity()],
        default=job_identity(),
    )
    args = parser.parse_args()
    os.umask(0o077)
    require(sys.version_info[:3] == (3, 14, 8), "python_baseline_mismatch")
    require(Path.cwd().resolve() == ROOT, "pce_worktree_cwd_required")
    if args.action == "check":
        BINDING.check_spec(candidate_spec().document(), candidate_spec().scope)
        print(
            json.dumps(
                {
                    "status": "OFFLINE_CODE_CHECK",
                    "catalog": catalog(),
                    "job_identity": job_identity(),
                    "backfill_job_identity": backfill_identity(),
                    "origin": ORIGIN,
                }
            )
        )
        return
    require(args.opt_in_dev_host, "pce_dev_host_opt_in_required")
    if args.create_absent_schema:
        require(
            args.action == "ddl"
            and args.opt_in_ddl
            and not args.schema_oid
            and not args.schema_owner,
            "pce_create_scope_invalid",
        )
        profile = load_pg_config(CONFIG / "runtime.toml")
        identity = PgJournalIdentity(profile.user, 1)  # Only used for absent namespace.
    else:
        require(
            args.schema_owner and args.schema_oid,
            "independent_schema_identity_required",
        )
        identity = PgJournalIdentity(args.schema_owner, args.schema_oid)
    host = DevHost(identity, enabled=True)
    if args.action == "ddl":
        print(
            json.dumps(
                initialize_schema(
                    host, create=args.create_absent_schema, enabled=args.opt_in_ddl
                )
            )
        )
    elif args.action == "worker":
        execution = ExecutionIdentity(
            args.job_id,
            args.code_git_sha,
            args.code_manifest_sha256,
            args.job_id + "-a1",
            args.observation_id,
        )
        result = host.worker_once(execution, enabled=args.opt_in_provider_garage_write)
        print(
            json.dumps(
                {
                    key: result.get(key)
                    for key in (
                        "job_id",
                        "attempt_id",
                        "state",
                        "stage",
                        "classification",
                    )
                }
            )
        )
    else:
        host.preflight(candidate_spec())
        if args.action == "preflight":
            print(json.dumps({"status": "HOST_PREFLIGHT_RETURNED", "schema": SCHEMA}))
        else:
            ui = ROOT / "web/dist"
            require((ui / "index.html").is_file(), "preview_ui_build_required")
            serve(
                create_preview(
                    host.journal(),
                    host.read_scope,
                    clock,
                    ui_directory=ui,
                    enabled=True,
                )
            )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        # Never print raw driver, provider, URL, request or credential details.
        print(
            json.dumps({"status": "UNKNOWN", "exception_class": type(error).__name__})
        )
        sys.exit(1)
