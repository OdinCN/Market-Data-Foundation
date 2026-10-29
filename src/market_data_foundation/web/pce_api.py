"""Bounded candidate UI API: PG registration only; no Worker/Source execution."""

import json
from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.historical import bea_pce as pce
from market_data_foundation.historical.pce_local_read import read_local_pce
from market_data_foundation.historical.pce_pg_binding import (
    BINDING,
    SCHEMA,
    backfill_identity,
    candidate_spec,
    job_identity,
)
from market_data_foundation.web.cpi_api import create_app

CONFIRMATION = "确认登记一次 DEV BEA PCE 2026-07 候选采集"


def submission():
    return {
        "confirmation": CONFIRMATION,
        "job_identity": job_identity(),
        "binding": BINDING.document(),
    }


def backfill_submission():
    return {
        "confirmation": "确认为原失败任务登记唯一 DEV BEA PCE 2026-07 回补",
        "job_identity": backfill_identity(),
        "parent_job_id": job_identity(),
        "binding": BINDING.document(),
        "reference": pce.REFERENCE,
    }


def catalog():
    return {
        "title": "BEA PCE 2026-07 / 候选来源",
        "binding": BINDING.document(),
        "parameters": [pce.request_parameters(table) for table in pce.TABLES],
        "lines": [1, 25],
        "measurements": list(pce.MEASUREMENTS),
        "source_qualified": False,
        "exact_original_retained": False,
        "provider_release_at": None,
        "source_grade": "source_projection",
        "normalized_grade": "mdf_normalized",
        "submission": submission(),
        "worker": "Host explicit single invocation",
    }


def public_job(doc):
    keys = (
        "job_id",
        "parent_job_id",
        "state",
        "stage",
        "attempt_id",
        "attempts",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "classification",
        "failure_stage",
        "effects",
        "reconciliation",
    )
    return {key: doc.get(key) for key in keys} | {
        "binding": BINDING.document(),
        "schema": SCHEMA,
        "retry_allowed": False,
    }


def _unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "pce_duplicate_json_key")
        result[key] = value
    return result


def create_preview(
    journal,
    read_scope,
    clock,
    *,
    ui_directory=None,
    enabled=False,
    cpi_read_scope=None,
    cpi_jobs_host=None,
    observability_host=None,
    management_host=None,
    package_host=None,
    query_host=None,
    origin=None,
):
    from market_data_foundation.web.pce_dev_host import (
        CANDIDATE_ORIGIN,
        ORIGIN,
        admitted,
    )

    require(enabled is True, "pce_preview_opt_in_required")
    require(journal.binding == BINDING, "pce_pg_binding_mismatch")
    # Native R5 routes, no HTTP forwarding and no alternate provider. Missing
    # capabilities retain the original UNCONFIGURED/UNKNOWN contracts.
    origin = ORIGIN if origin is None else origin
    app = create_app(
        cpi_read_scope,
        jobs_host=cpi_jobs_host,
        observability_host=observability_host,
        management_host=management_host,
        package_host=package_host,
        query_host=query_host,
    )

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        package_path = request.url.path == "/api/r5/packages" or (
            request.url.path.startswith("/api/r5/packages/")
            and len(request.url.path.removeprefix("/api/r5/packages/").split("/")) == 1
        )
        # The explicit package Host grants only this PUT. Reuse the existing
        # transport admission with a GET-shaped scope; its own trusted callback
        # still decides metadata/write permissions in the package router.
        package_put = (
            package_path and request.method == "PUT" and package_host is not None
        )
        if package_put:
            allowed = (
                admitted(Request({**request.scope, "method": "GET"}), origin=origin)
                and not request.url.query
                and request.headers.getlist("origin") == [origin]
                and request.headers.getlist("content-type") == ["application/json"]
            )
        else:
            allowed = not (
                origin == CANDIDATE_ORIGIN and request.method not in {"GET", "HEAD"}
            ) and admitted(request, origin=origin)
        if not allowed:
            if request.url.path == "/api/r5/cpi/query":
                from market_data_foundation.web.cpi_query import (
                    QueryRejected,
                )
                from market_data_foundation.web.cpi_query import (
                    failure_response as query_failure,
                )

                return query_failure(
                    QueryRejected("operator_not_authorized", 403, "REJECTED")
                )
            if package_path:
                from market_data_foundation.web.package_catalog import PackageError
                from market_data_foundation.web.packages import failure_response

                return failure_response(
                    PackageError("operator_not_authorized", 403, "REJECTED")
                )
            return JSONResponse({"classification": "operator_not_authorized"}, 403)
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse(
                {"classification": "pce_storage_or_outcome_unknown"}, 503
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    add_read_routes(app, read_scope, clock, write_eligible=origin == ORIGIN)

    @app.get("/api/pce/jobs")
    def jobs():
        reader = journal.reader()
        snapshot = reader.snapshot()
        reason = (
            reader.backfill_eligibility() if snapshot else "pce_backfill_parent_missing"
        )
        if origin != ORIGIN:
            reason = "pce_backfill_read_only"
        return {
            "backfill": {
                "eligible": reason is None,
                "reason": reason,
                "submission": backfill_submission(),
            },
            "jobs": [
                {**public_job(doc), "events": [public_job(event) for event in events]}
                for doc, events in snapshot
            ],
            "schema": SCHEMA,
        }

    @app.post("/api/pce/jobs")
    async def accept(request: Request):
        # Reject bounded streaming bodies before parsing; never echo user input.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                return JSONResponse({"classification": "pce_request_too_large"}, 413)
        try:
            document = json.loads(bytes(body), object_pairs_hook=_unique)
            require(
                pce.encode(document) == pce.encode(submission()),
                "pce_scope_or_confirmation_mismatch",
            )
        except Exception:
            return JSONResponse(
                {"classification": "pce_scope_or_confirmation_mismatch"}, 422
            )
        spec = candidate_spec()
        # submit returns only after commit. Ambiguity becomes 503; no background task.
        doc = journal.submit(job_identity(), spec.document(), spec.scope, clock())
        return JSONResponse(public_job(doc), 202 if doc["state"] == "pending" else 200)

    @app.post("/api/pce/jobs/backfill")
    async def accept_backfill(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                return JSONResponse({"classification": "pce_request_too_large"}, 413)
        try:
            document = json.loads(bytes(body), object_pairs_hook=_unique)
            require(
                pce.encode(document) == pce.encode(backfill_submission()),
                "pce_scope_or_confirmation_mismatch",
            )
        except Exception:
            return JSONResponse(
                {"classification": "pce_scope_or_confirmation_mismatch"}, 422
            )
        spec = candidate_spec()
        try:
            doc = journal.submit_backfill(
                backfill_identity(),
                job_identity(),
                spec.document(),
                spec.scope,
                clock(),
            )
        except ContractError as error:
            # Storage uncertainty is handled by the outer 503 boundary, never
            # recast as a definite rejection or permission for a new identity.
            from market_data_foundation.historical.pce_pg_journal import (
                PgJournalUnavailable,
            )

            if isinstance(error, PgJournalUnavailable):
                raise
            allowed = {
                "pce_backfill_parent_not_failed",
                "pce_backfill_original_failure_mismatch",
                "pce_backfill_schema_oid_mismatch",
                "pce_backfill_failed_attempt_required",
                "pce_backfill_parent_effects_unresolved",
                "job_not_found",
                "pce_backfill_exact_two_scope_migration_required",
                "job_scope_already_registered",
                "job_query_budget_exceeded",
                "pce_backfill_parent_missing",
            }
            return JSONResponse(
                {
                    "classification": str(error)
                    if str(error) in allowed
                    else "pce_backfill_not_eligible"
                },
                409,
            )
        return JSONResponse(public_job(doc), 202 if doc["state"] == "pending" else 200)

    if ui_directory is not None:
        ui = Path(ui_directory)
        app.mount("/assets", StaticFiles(directory=ui / "assets"))

        @app.get("/")
        def root():
            return RedirectResponse("/pce")

        @app.get("/pce")
        def index():
            return FileResponse(ui / "index.html")

        @app.get("/mdf-data-hub.png")
        def logo():
            return FileResponse(ui / "mdf-data-hub.png")

    return app


def add_read_routes(app, read_scope, clock, *, write_eligible=False):
    """Attach existing PCE reads without constructing Jobs, Workers or sources."""

    @app.get("/api/pce/catalog")
    def get_catalog():
        return catalog() | {"write_eligible": write_eligible}

    @app.get("/api/pce/assets")
    def assets(request: Request):
        if request.url.query:
            return JSONResponse({"classification": "pce_asset_scope_mismatch"}, 422)
        at = clock()
        try:
            with read_scope() as scope:
                # The generic operator qualification allowlist excludes PCE.
                # Inventory exact stored candidate metadata without asserting
                # qualification; the domain Reader alone validates read eligibility.
                reads = scope.operator_reads()
                require(
                    reads.get(pce.PCE_DEFINITION.key) == pce.PCE_DEFINITION,
                    "pce_definition_mismatch",
                )
                require(
                    reads.get(pce.PCE_SOURCE.key) == pce.PCE_SOURCE,
                    "pce_source_mismatch",
                )
                require(
                    reads.get(pce.PCE_BINDING_CANDIDATE.key)
                    == pce.PCE_BINDING_CANDIDATE,
                    "pce_binding_mismatch",
                )
                stored = reads.versions_for_definition(pce.PCE_DEFINITION.key, 128)
                require(len(stored) <= 128, "pce_version_limit")
                versions = []
                total_publications = 0
                for version in stored:
                    require(
                        version.binding == pce.PCE_BINDING_CANDIDATE.key,
                        "pce_version_binding_mismatch",
                    )
                    history = reads.publications_for_version(version.key, 128)
                    total_publications += len(history)
                    require(total_publications <= 128, "pce_publication_limit")
                    require(
                        all(pub.request.version == version.key for pub in history),
                        "pce_publication_version_mismatch",
                    )
                    versions.append(
                        {
                            "version_identity": version.identity,
                            "frozen_at": version.frozen_at.isoformat(),
                            "selection_state": "stored_candidate_read_unverified",
                            "publications": [
                                {
                                    "publication_identity": pub.request.identity,
                                    "decision": pub.request.decision.value,
                                    "known_at": pub.decision_at.isoformat(),
                                    "usable_at": pub.usable_at.isoformat(),
                                }
                                for pub in history
                            ],
                        }
                    )
            return {
                "binding": BINDING.document(),
                "as_of": at.isoformat(),
                "source_qualified": False,
                "versions": versions,
            }
        except Exception:
            return JSONResponse({"classification": "pce_assets_unavailable"}, 409)

    @app.get("/api/pce/read")
    def read(request: Request):
        try:
            query = request.query_params
            require(
                set(query)
                in ({"reference", "as_of"}, {"reference", "as_of", "version"})
                and all(len(query.getlist(key)) == 1 for key in query)
                and query["reference"] == pce.REFERENCE,
                "pce_read_scope_mismatch",
            )
            at = datetime.fromisoformat(query["as_of"])
            require(at.tzinfo is not None, "pce_read_time_required")
            version = query.get("version")
            require(version is None or len(version) <= 160, "pce_version_invalid")
        except Exception:
            return JSONResponse({"classification": "pce_read_scope_mismatch"}, 422)
        try:
            with read_scope() as ledger:
                view = read_local_pce(
                    ledger, reference=pce.REFERENCE, as_of=at, version_identity=version
                )
                content = pce.decode(view.content, max_bytes=pce.MAX_NORMALIZED_BYTES)
                return {
                    "binding": BINDING.document(),
                    "schema": SCHEMA,
                    "reference": pce.REFERENCE,
                    "as_of": at.isoformat(),
                    "version_identity": view.version.identity,
                    "publication_identity": view.publication.request.identity,
                    "acquisition_identity": view.acquisitions[0].identity,
                    "known_at": view.publication.decision_at.isoformat(),
                    "usable_at": view.publication.usable_at.isoformat(),
                    "source_qualified": False,
                    "exact_original_retained": False,
                    "provider_release_at": None,
                    "measurements": content["data"]["measurements"],
                }
        except Exception:
            return JSONResponse({"classification": "pce_read_unavailable"}, 409)
