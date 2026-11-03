"""Controlled M3 collection/readback and local-first cutoff queries."""

from __future__ import annotations

import time
from datetime import datetime
from uuid import UUID, uuid4

from market_data_foundation.historical.cpi_timeseries import month_at, month_number
from market_data_foundation.historical.m3_capital_goods import (
    SERIES,
    M3Request,
    check,
)
from market_data_foundation.historical.m3_capital_goods_source import (
    M3CandidateSource,
    M3SourceError,
)
from market_data_foundation.historical.m3_capital_goods_store import (
    SERIES_IDS,
    M3Batch,
    _rows,
)

MAX_QUERY_SOURCE_REQUESTS = 6
COLLECTION_WINDOW_MONTHS = 240
MAX_COLLECTION_REQUESTS = 3600
QUERY_SOURCE_BUDGET_SECONDS = 15.0


def _months(start, end):
    first, last = month_number(start), month_number(end)
    check(0 <= last - first < 2400, "query_range_invalid")
    return tuple(month_at(n) for n in range(first, last + 1))


def _planned_requests(dataset, months, indexed, series_ids=SERIES_IDS, window_months=2):
    """Query windows stay small; collection uses verified bounded historical windows."""
    for offset in range(0, len(months), window_months):
        window = months[offset : offset + window_months]
        for (category, kind), series in SERIES.items():
            if series not in series_ids:
                continue
            missing = [
                m
                for m in window
                if (series, m) not in indexed or indexed[series, m]["value"] is None
            ]
            if missing:
                yield M3Request(dataset, category, missing[0], missing[-1], kind)


def _failure(error, request):
    # Provider text, URLs, credentials and exception messages are never exposed.
    accepted = {
        "census_m3_http_failure",
        "census_m3_rate_wait",
        "census_m3_request_control_unknown",
        "census_m3_timeout",
        "census_m3_transport_failure",
        "census_m3_protocol_failure",
        "census_m3_request_invalid",
        "census_m3_response_contract_invalid",
        "census_m3_unexpected_exception",
    }
    if isinstance(error, M3SourceError):
        classification = (
            error.classification
            if error.classification in accepted
            else "census_m3_source_failure"
        )
        status = (
            error.http_status
            if type(error.http_status) is int and 100 <= error.http_status <= 599
            else None
        )
        attempts = (
            error.attempts
            if type(error.attempts) is int and error.attempts in (0, 1)
            else None
        )
    else:
        classification, status, attempts = "census_m3_source_failure", None, None
    return {
        "classification": classification,
        "retry_at": getattr(error, "retry_at", None),
        "http_status": status,
        "attempts": attempts,
        "series_id": SERIES[(request.category_code, request.data_type_code)],
        "start_month": request.start_month,
        "end_month": request.end_month,
    }


def _bound(value, maximum=MAX_QUERY_SOURCE_REQUESTS):
    check(
        type(value) is int and 1 <= value <= maximum,
        "source_request_budget_invalid",
    )
    return value


class M3TimeSeriesService:
    def __init__(
        self, store, source=None, *, collection_window_months=COLLECTION_WINDOW_MONTHS
    ):
        self.store, self.source = store, source
        check(
            type(collection_window_months) is int
            and 1 <= collection_window_months <= COLLECTION_WINDOW_MONTHS,
            "collection_window_invalid",
        )
        self.collection_window_months = collection_window_months

    def fetch_batch(self, dataset, start, end):
        check(self.source is not None, "source_not_bound")
        return M3Batch(
            tuple(
                self.source.fetch(M3Request(dataset, category, start, end, kind))
                for category, kind in (("MDM", "NO"), ("NXA", "NO"), ("NXA", "VS"))
            )
        )

    def collect_range(
        self,
        start,
        end,
        *,
        dataset="m3",
        confirm_write=False,
        max_source_requests=MAX_COLLECTION_REQUESTS,
        resume_id=None,
        on_progress=None,
    ):
        """Revalidate all inputs unless resuming the same collection."""
        check(confirm_write is True, "explicit_write_opt_in_required")
        _bound(max_source_requests, MAX_COLLECTION_REQUESTS)
        M3Request(dataset, "MDM", start, start)
        months = _months(start, end)
        collection_id = str(uuid4()) if resume_id is None else resume_id
        try:
            valid_id = (
                type(collection_id) is str and str(UUID(collection_id)) == collection_id
            )
        except TypeError, ValueError, AttributeError:
            valid_id = False
        check(valid_id, "collection_id_invalid")
        completed_targets = set()
        completed_keys, completed, confirmed_points = set(), [], 0
        storage_failure = None
        from market_data_foundation.historical.m3_capital_goods_store import _same

        for batch_id, meta in self.store.collection_progress(collection_id):
            check(
                type(meta) is dict
                and meta.get("collection_id") == collection_id
                and (meta.get("dataset"), meta.get("start"), meta.get("end"))
                == (dataset, start, end),
                "collection_scope_mismatch",
            )
            key = (
                meta.get("category_code"),
                meta.get("data_type_code"),
                meta.get("fragment_start"),
                meta.get("fragment_end"),
            )
            check(
                type(meta.get("confirmed")) is bool
                and type(meta.get("expected")) is list,
                "collection_progress_invalid",
            )
            request = M3Request(dataset, key[0], key[2], key[3], key[1])
            check(
                start <= request.start_month <= request.end_month <= end,
                "collection_progress_invalid",
            )
            expected = meta["expected"]
            series = SERIES[(request.category_code, request.data_type_code)]
            check(
                not completed_targets.intersection((series, m) for m in request.months),
                "collection_progress_overlap",
            )
            check(
                len(expected) == len(request.months)
                and all(
                    type(row) is dict
                    and set(row) == {"series_id", "month", "value", "status", "reason"}
                    for row in expected
                )
                and {(row["series_id"], row["month"]) for row in expected}
                == {(series, month) for month in request.months}
                and all(
                    (
                        row["status"] == "available"
                        and row["value"] is not None
                        and row["reason"] is None
                    )
                    or (
                        row["status"] == "missing"
                        and row["value"] is None
                        and isinstance(row["reason"], str)
                    )
                    for row in expected
                ),
                "collection_progress_invalid",
            )
            if not meta["confirmed"]:
                try:
                    confirmed_points += self.store.confirm_batch_availability(batch_id)
                    rows = self.store.read_range(dataset, key[2], key[3])
                    indexed = {(r["series_id"], r["month"]): r for r in rows}
                    check(
                        all(
                            (r["series_id"], r["month"]) in indexed
                            and _same(indexed[(r["series_id"], r["month"])], r)
                            for r in meta["expected"]
                        ),
                        "collection_recovery_conflict",
                    )
                    self.store.mark_collection_fragment(batch_id)
                except Exception:
                    storage_failure = "census_m3_storage_outcome_unknown"
                    break
            completed_keys.add(key)
            completed_targets.update((series, month) for month in request.months)
            completed.append(batch_id)
        # Subtract this collection's confirmed fragments, not unrelated local values.
        # Uncovered contiguous months bridge old fragments and new wide batches.
        plan = []
        for (category, kind), series in SERIES.items():
            pending = [m for m in months if (series, m) not in completed_targets]
            windows = []
            for month in pending:
                if (
                    not windows
                    or len(windows[-1]) >= self.collection_window_months
                    or month_number(month) != month_number(windows[-1][-1]) + 1
                ):
                    windows.append([])
                windows[-1].append(month)
            plan.extend(
                M3Request(dataset, category, w[0], w[-1], kind) for w in windows
            )
        plan.sort(
            key=lambda r: (
                r.start_month,
                tuple(SERIES).index((r.category_code, r.data_type_code)),
            )
        )
        planned_count = len(completed_keys) + len(plan)
        attempts = calls = saved_points = saved_revisions = 0
        failure = None
        for request in [] if storage_failure else plan[:max_source_requests]:
            calls += 1
            try:
                check(self.source is not None, "source_not_bound")
                snapshot = self.source.fetch(request)
                check(snapshot.request == request, "source_request_mismatch")
                batch = M3Batch((snapshot,), collection_id, (start, end))
            except Exception as error:
                failure = _failure(error, request)
                attempts = (
                    None
                    if failure["attempts"] is None
                    else attempts + failure["attempts"]
                )
                break
            attempts += 1
            try:
                receipt = self.import_batch(batch, confirm_write=True)
                self.store.mark_collection_fragment(batch.identity)
            except Exception:
                storage_failure = "census_m3_storage_outcome_unknown"
                break
            completed.append(receipt["batch_id"])
            completed_keys.add(
                (
                    request.category_code,
                    request.data_type_code,
                    request.start_month,
                    request.end_month,
                )
            )
            completed_targets.update(
                (SERIES[(request.category_code, request.data_type_code)], m)
                for m in request.months
            )
            saved_points += receipt["saved_points"]
            saved_revisions += receipt["saved_revisions"]
            if on_progress:
                on_progress(
                    {
                        "phase": "collecting",
                        "completed_requests": len(completed_keys),
                        "remaining_requests": planned_count - len(completed_keys),
                        "requested_points": len(completed_targets),
                        "expected_points": len(months) * len(SERIES_IDS),
                        "provider_requests": calls,
                        "saved_points": saved_points,
                        "saved_revisions": saved_revisions,
                    }
                )
        try:
            after = self.store.read_range(dataset, start, end)
            readback = "confirmed"
        except Exception:
            after = []
            readback = "unavailable"
            storage_failure = "census_m3_storage_outcome_unknown"
        present = {
            (r["series_id"], r["month"]) for r in after if r["value"] is not None
        }
        after_keys = {(r["series_id"], r["month"]) for r in after}
        if completed_targets - after_keys:
            storage_failure = "census_m3_storage_outcome_unknown"
            readback = "incomplete"

        def pending_reason(series, month):
            return (
                "source_request_failed"
                if failure
                and failure["series_id"] == series
                and failure["start_month"] <= month <= failure["end_month"]
                else "not_requested"
            )

        missing = [
            {
                "series_id": s,
                "month": m,
                "reason": "storage_readback_missing"
                if (s, m) in completed_targets and (s, m) not in after_keys
                else "source_unavailable"
                if (s, m) in completed_targets
                else pending_reason(s, m),
            }
            for m in months
            for s in SERIES_IDS
            if (s, m) not in present
        ]
        remaining = planned_count - len(completed_keys)
        return {
            "status": "complete"
            if not remaining and not storage_failure and not failure
            else "partial",
            "collection_id": collection_id,
            "saved_points": saved_points,
            "saved_revisions": saved_revisions,
            "confirmed_points": confirmed_points,
            "readback_points": len(after),
            "persisted": None if storage_failure else bool(completed),
            "completed_batches": completed,
            "remaining_requests": remaining,
            "missing": missing,
            "source_missing": [
                r for r in missing if r["reason"] == "source_unavailable"
            ],
            "pending": [
                {
                    "series_id": series,
                    "month": month,
                    "reason": pending_reason(series, month),
                }
                for series in SERIES_IDS
                for month in months
                if (series, month) not in completed_targets
            ],
            "requested_points": len(completed_targets),
            "planned_requests": planned_count,
            "completed_requests": len(completed_keys),
            "source_failure": failure,
            "storage_failure": storage_failure,
            "readback_status": readback,
            "provider_requests": calls,
            "provider_attempts": attempts,
            "budget_exhausted": failure is None
            and storage_failure is None
            and remaining > 0,
            "resume": "pass collection_id as resume_id with the same range and dataset",
        }

    def import_batch(self, batch, *, confirm_write=False):
        check(confirm_write is True, "explicit_write_opt_in_required")
        check(type(batch) is M3Batch, "batch_invalid")
        saved = self.store.save_batch(batch)
        actual = self.store.read_range(
            batch.dataset, batch.request.start_month, batch.request.end_month
        )
        indexed = {(r["series_id"], r["month"]): r for r in actual}
        from market_data_foundation.historical.m3_capital_goods_store import _same

        check(
            all(
                (r["series_id"], r["month"]) in indexed
                and _same(indexed[(r["series_id"], r["month"])], r)
                for r in _rows(batch)
            ),
            "collection_readback_mismatch",
        )
        return saved | {
            "batch_id": batch.identity,
            "readback_points": len(actual),
            "persisted": True,
            "provider_requests": 0,
            "provider_attempts": 0,
        }

    def query_range(
        self,
        start,
        end,
        *,
        dataset="m3",
        as_of=None,
        basis="available",
        allow_upstream=False,
        series_ids=SERIES_IDS,
    ):
        req = M3Request(dataset, "MDM", start, start)
        months = _months(start, end)
        check(type(allow_upstream) is bool, "upstream_flag_invalid")
        check(basis in {"observed", "available"}, "cutoff_basis_invalid")
        check(
            type(series_ids) is tuple
            and bool(series_ids)
            and len(set(series_ids)) == len(series_ids)
            and set(series_ids) <= set(SERIES_IDS),
            "series_invalid",
        )
        if as_of is not None:
            check(not allow_upstream, "history_must_be_local")
            try:
                as_of = (
                    datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                    if isinstance(as_of, str)
                    else as_of
                )
                check(
                    isinstance(as_of, datetime) and as_of.utcoffset() is not None,
                    "cutoff_invalid",
                )
            except TypeError, ValueError:
                check(False, "cutoff_invalid")
        rows = self.store.read_range(dataset, start, end, as_of, basis)
        indexed = {(r["series_id"], r["month"]): dict(r, origin="local") for r in rows}
        expected = {(s, m) for s in series_ids for m in months}
        requests = attempts = 0
        failures = []
        budget_exhausted = False
        if allow_upstream:
            plan = list(_planned_requests(dataset, months, indexed, series_ids))
            budget_exhausted = len(plan) > MAX_QUERY_SOURCE_REQUESTS
            deadline = time.monotonic() + QUERY_SOURCE_BUDGET_SECONDS
            for request in plan[:MAX_QUERY_SOURCE_REQUESTS]:
                remaining = deadline - time.monotonic()
                if remaining <= 0.1:
                    budget_exhausted = True
                    failures.append(
                        {
                            "classification": "census_m3_query_deadline",
                            "http_status": None,
                            "attempts": 0,
                            "series_id": SERIES[
                                (request.category_code, request.data_type_code)
                            ],
                            "start_month": request.start_month,
                            "end_month": request.end_month,
                        }
                    )
                    break
                requests += 1
                try:
                    check(self.source is not None, "source_not_bound")
                    if isinstance(self.source, M3CandidateSource):
                        snapshot = self.source.fetch(
                            request, timeout=min(20, remaining)
                        )
                    else:
                        snapshot = self.source.fetch(request)
                    check(snapshot.request == request, "source_request_mismatch")
                    batch = M3Batch((snapshot,))
                except Exception as error:
                    failure = _failure(error, request)
                    failures.append(failure)
                    if attempts is not None:
                        attempts = (
                            None
                            if failure["attempts"] is None
                            else attempts + failure["attempts"]
                        )
                    if (
                        failure["http_status"] == 429
                        or (
                            failure["http_status"] is not None
                            and failure["http_status"] >= 500
                        )
                        or failure["classification"]
                        in {"census_m3_timeout", "census_m3_transport_failure"}
                        or failure["attempts"] in (None, 0)
                    ):
                        break
                    continue
                if attempts is not None:
                    attempts += 1
                for row in _rows(batch):
                    key = row["series_id"], row["month"]
                    if key not in indexed or indexed[key]["value"] is None:
                        indexed[key] = dict(
                            row,
                            origin="upstream",
                            revision=None,
                            available_at=None,
                        )
        points = []
        for series, month in sorted(expected, key=lambda k: (k[1], k[0]), reverse=True):
            row = indexed.get((series, month))
            points.append(
                row
                if row is not None
                else {
                    "dataset": dataset,
                    "series_id": series,
                    "month": month,
                    "value": None,
                    "status": "missing",
                    "reason": "local_record_missing",
                    "origin": "local",
                    "observed_at": None,
                    "available_at": None,
                    "revision": None,
                    "source": {
                        "unit": "million_current_usd",
                        "seasonal_adjustment": "SA",
                        "frequency": "monthly",
                        "annualized": False,
                        "report_stage": req.report_stage,
                        "official_release_event": None,
                        "source_version_date": None,
                    },
                }
            )
        return {
            "points": points,
            "dataset": dataset,
            "report_stage": req.report_stage,
            "query_mode": "history" if as_of is not None else "current",
            "as_of": None if as_of is None else as_of.isoformat(),
            "as_of_basis": basis,
            "provider_requests": requests,
            "provider_attempts": attempts,
            "persisted": False,
            "source_failure": failures[0] if failures else None,
            "source_failures": failures,
            "budget_exhausted": budget_exhausted,
            "status": "complete"
            if all(p["value"] is not None for p in points)
            else "partial"
            if any(p["value"] is not None for p in points)
            else "unavailable",
            "missing": [
                {
                    "series_id": p["series_id"],
                    "month": p["month"],
                    "reason": p.get("reason") or "source_unavailable",
                }
                for p in points
                if p["value"] is None
            ],
            "coverage": {
                "expected": len(expected),
                "available": sum(p["value"] is not None for p in points),
            },
        }


def build_service(
    *,
    runtime_path,
    secret_path,
    package_directory,
    request_control=None,
    allow_live=False,
    credential_loader=None,
):
    """Exact existing DEV PG/Garage binding; construction does no DDL or HTTP."""
    import tomllib
    from pathlib import Path

    from market_data_foundation.historical.bls_cpi_material_store import (
        BUCKET,
        GARAGE_ENDPOINT,
        GarageCredentials,
    )
    from market_data_foundation.historical.m3_capital_goods_source import (
        M3CandidateSource,
    )
    from market_data_foundation.historical.m3_capital_goods_store import (
        GarageM3RawStore,
        PostgresM3Store,
    )
    from market_data_foundation.storage.market_dataset_postgres import (
        connect_dev,
        preflight,
    )
    from market_data_foundation.web.package_catalog import PackageCatalog
    from market_data_foundation.web.package_store import LocalPackageStore

    runtime = tomllib.loads(Path(runtime_path).read_text())
    secret = tomllib.loads(Path(secret_path).read_text())
    s3 = runtime["s3"]
    check(
        runtime["storage"]["backend"] == "s3"
        and s3["endpoint_url"] == GARAGE_ENDPOINT
        and s3["bucket"] == BUCKET
        and s3["region"] == "garage"
        and s3["key_prefix"] == ""
        and s3["addressing_style"] == "path",
        "storage_binding_invalid",
    )
    check(not allow_live or request_control is not None, "request_control_required")
    packages = LocalPackageStore(Path(package_directory), PackageCatalog())

    def credential():
        record = packages.read("mdf-dev", "macro")
        check(
            record is not None and "Census" in record.credential_refs,
            "saved_credential_required",
        )
        return packages.resolve_credential(record, "Census")

    connection = connect_dev(
        secret["postgres"]["password"], runtime_path=Path(runtime_path)
    )
    try:
        preflight(connection)
        raw = GarageM3RawStore(
            GarageCredentials(
                secret["s3"]["access_key_id"], secret["s3"]["secret_access_key"]
            )
        )
        return M3TimeSeriesService(
            PostgresM3Store(connection, raw),
            M3CandidateSource(
                credential_loader=credential_loader or credential,
                request_control=request_control,
                enabled=allow_live,
            ),
        )
    except Exception:
        connection.close()
        raise
