"""Local-first MARTS service; input revisions precede every derived calculation."""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

from market_data_foundation.historical.census_retail import CATEGORY_CODES
from market_data_foundation.historical.census_retail_timeseries import calculate_inputs
from market_data_foundation.historical.census_retail_timeseries_source import (
    CensusRetailRangeSource,
    RetailCell,
    RetailSourceError,
    _now,
    plan_range,
)
from market_data_foundation.historical.cpi_timeseries import month_at, month_number
from market_data_foundation.historical.retail_timeseries_store import collection_key
from market_data_foundation.source_request_policy import retryable_http_status


class RetailServiceError(RuntimeError):
    def __init__(self, classification):
        self.classification = classification
        super().__init__(classification)


def cutoff(value):
    if value is None:
        return None
    try:
        at = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
        if at.utcoffset() is None:
            raise ValueError
        return at.astimezone(UTC)
    except Exception:
        raise RetailServiceError("retail_aware_cutoff_required") from None


def missing_cell(month, category, kind):
    return RetailCell(
        month,
        category,
        kind,
        None,
        "not_provided",
        "local_input_absent",
        None,
        None,
        "",
    )


class RetailTimeSeriesService:
    def __init__(
        self, store, source=None, *, allow_source=False, clock=lambda: datetime.now(UTC)
    ):
        self.store, self.source = store, source
        self.allow_source, self.clock = allow_source, clock

    def _source(self):
        if self.allow_source is not True or self.source is None:
            raise RetailServiceError("retail_source_not_bound")
        if not isinstance(self.source, CensusRetailRangeSource):
            raise RetailServiceError("retail_source_binding_mismatch")
        return self.source

    def default_range(self):
        now = _now(self.clock)
        end = now.year * 12 + now.month - 2
        return month_at(end - 239), month_at(end)

    def initialize(self, start=None, end=None, *, collection_id=None):
        _, default_end = self.default_range()
        end = end or default_end
        start = start or month_at(month_number(end) - 239)
        return self._collect(start, end, collection_id)

    def update(self, start=None, end=None, *, collection_id=None):
        return self.initialize(start, end, collection_id=collection_id)

    def _collect(self, start, end, collection_id):
        source = self._source()
        plan = source.plan(start, end)
        collection = collection_key(collection_id or uuid4().hex)
        completed, staged, saved, attempts = [], [], 0, 0
        requests = observations = revisions = 0

        def save(batch):
            nonlocal observations, revisions
            count = self.store.save_batch(collection, batch)
            added, revised = self.store.last_write_counts
            observations += added
            revisions += revised
            return count

        failure = persist_failure = None
        try:
            self.store.ensure_schema()
            self.store.register_collection(collection, plan)
            for request in plan.requests:
                batch = self.store.retained_batch(collection, request)
                if batch is not None:
                    staged.append(batch)
                    saved += save(batch)
                    completed.append(batch)

            def persist(batch):
                nonlocal saved, attempts, requests, persist_failure
                # Count and stage before attempting DB: uncertain commits replay
                # this exact guarded response without another source request.
                requests += 1
                attempts += batch.attempts
                try:
                    self.store.stage_batch(collection, batch)
                    staged.append(batch)
                    saved += save(batch)
                    completed.append(batch)
                except Exception as error:
                    persist_failure = getattr(
                        error, "classification", "retail_local_failure"
                    )
                    raise

            source.fetch(start, end, completed=tuple(completed), on_batch=persist)
        except Exception as error:
            failure = persist_failure or getattr(
                error, "classification", "retail_local_failure"
            )
            # Source includes all newly attempted exchanges, including successful
            # response followed by local persistence failure.
            if not persist_failure and getattr(error, "attempts", 0) > attempts:
                requests += 1
            attempts = max(attempts, getattr(error, "attempts", 0))
            retry_at = getattr(error, "retry_at", None)
        return {
            "status": "PARTIAL"
            if failure and completed
            else "ERROR"
            if failure
            else "COMPLETED",
            "classification": failure,
            "collection_id": collection,
            "start_month": start,
            "end_month": end,
            "completed_batches": len(completed),
            "staged_batches": len({b.request for b in completed + staged}),
            "planned_batches": len(plan.requests),
            "saved_points": saved,
            "saved_observations": observations,
            "saved_revisions": revisions,
            "provider_requests": requests,
            "provider_attempts": attempts,
            "retained_provider_attempts": sum(b.attempts for b in completed),
            "retry_at": retry_at if failure else None,
            "response_observed_at": min(
                (b.observed_at for b in completed), default=None
            ),
            "resume": "reuse collection_id and exact range; successful inputs are "
            "reconciled locally",
        }

    def query_range(
        self,
        start,
        end,
        *,
        local_only=False,
        as_of=None,
        query_mode=None,
        as_of_basis=None,
        vintage_mode=None,
    ):
        at = cutoff(as_of)
        if type(local_only) is not bool:
            raise RetailServiceError("retail_local_only_invalid")
        if as_of_basis and vintage_mode and as_of_basis != vintage_mode:
            raise RetailServiceError("retail_cutoff_basis_conflict")
        basis = as_of_basis or vintage_mode
        mode = query_mode or (
            "history" if at is not None or basis == "available" else "current"
        )
        if mode not in {"current", "history"} or basis not in {
            None,
            "observed",
            "available",
        }:
            raise RetailServiceError("retail_query_mode_invalid")
        basis = basis or ("available" if mode == "history" else "observed")
        if (mode == "history" or basis == "available") and at is None:
            at = _now(self.clock)
        historical = mode == "history" or at is not None
        plan = plan_range(start, end)
        # Any local failure propagates before upstream admission is considered.
        selected = self.store.read_range(
            plan.input_start_month, end, as_of=at, basis=basis
        )
        upstream = {}
        cells = {k: v.cell for k, v in selected.items()}
        origins = {k: "local" for k in cells}
        expected = {
            (month_at(n), c, kind)
            for n in range(month_number(plan.input_start_month), month_number(end) + 1)
            for c, kind in [(c, "SM") for c in CATEGORY_CODES] + [("44X72", "MPCSM")]
        }
        gaps = {k for k in expected if k not in cells or cells[k].status == "missing"}
        attempts = requests = 0
        source_failure = None
        source_no_content = []
        # UTC reference months must have ended before requesting monthly actuals.
        # This is an eligibility bound, never a claim of official publication.
        last_completed_month = month_at(
            month_number(_now(self.clock).strftime("%Y-%m")) - 1
        )
        source_skipped_months = sorted(
            {k[0] for k in gaps if k[0] > last_completed_month}
        )
        eligible_gaps = {k for k in gaps if k[0] <= last_completed_month}
        trusted_local = any(
            start <= key[0] <= end
            and cell.status == "available"
            and cell.value is not None
            for key, cell in cells.items()
        )
        if (
            start <= last_completed_month
            and eligible_gaps
            and not local_only
            and not historical
        ):
            source = self._source()
            # Preflight the source's global range budget. Within each admitted
            # window fetch only the span containing gaps, with no extra buffer.
            admitted = source.plan(start, end)
            for request in admitted.requests:
                months = sorted(
                    {
                        key[0]
                        for key in eligible_gaps
                        if request.start_month <= key[0] <= request.end_month
                    }
                )
                if not months:
                    continue
                try:
                    snapshot = source.fetch(months[0], months[-1], buffer_months=0)
                except RetailSourceError as error:
                    if (
                        error.classification == "census_retail_http_failure"
                        and error.http_status == 204
                        and error.attempts > 0
                    ):
                        attempts += error.attempts
                        requests += 1
                        source_no_content.append(
                            {
                                "classification": "census_retail_no_content",
                                "http_status": 204,
                                "response_bytes": 0,
                                "start_month": months[0],
                                "end_month": months[-1],
                            }
                        )
                        continue
                    transient = error.classification in {
                        "census_retail_timeout",
                        "census_retail_transport_failure",
                    } or (
                        error.classification == "census_retail_http_failure"
                        and retryable_http_status(error.http_status)
                    )
                    if not trusted_local or not transient or not error.attempts:
                        raise RetailSourceError(
                            error.classification,
                            attempts + error.attempts,
                            http_status=error.http_status,
                            retry_at=error.retry_at,
                        ) from None
                    attempts += error.attempts
                    requests += 1
                    source_failure = {
                        "classification": error.classification,
                        "http_status": error.http_status,
                        "retry_at": error.retry_at,
                    }
                    # Stop this bounded query after a transient failure; retain
                    # local inputs and any earlier successful upstream window.
                    break
                attempts += sum(b.attempts for b in snapshot.batches)
                requests += len(snapshot.batches)
                for batch in snapshot.batches:
                    for cell in batch.cells:
                        if cell.key in gaps:
                            cells[cell.key], origins[cell.key] = cell, "upstream"
                            upstream[cell.key] = batch
        matrix = tuple(cells.get(key, missing_cell(*key)) for key in sorted(expected))
        material = json.dumps(
            [
                start,
                end,
                basis,
                at.isoformat() if at else None,
                [asdict(cell) for cell in matrix],
            ],
            sort_keys=True,
            default=str,
        ).encode()
        identity = "census-marts-projection:" + hashlib.sha256(material).hexdigest()
        points = []
        for point in reversed(calculate_inputs(plan, matrix, identity)):
            document = asdict(point)
            # Carry all source operands, official percent and comparison bases.
            all_inputs = {cell.key: cell for cell in point.source_cells}
            for calculation in (
                *point.headline_rates.values(),
                *point.control_rates.values(),
            ):
                all_inputs.update((cell.key, cell) for cell in calculation.inputs)
            lineage = {}
            for key, cell in all_inputs.items():
                version = selected.get(key) if origins.get(key) == "local" else None
                live = upstream.get(key)
                lineage["/".join(key)] = {
                    "origin": origins.get(key, "absent"),
                    "local_revision": version.revision if version else None,
                    "batch_id": version.batch_id if version else None,
                    "observed_at": version.observed_at
                    if version
                    else live.observed_at
                    if live
                    else None,
                    "available_at": version.available_at if version else None,
                    "raw_sha256": cell.raw_sha256 or None,
                    "source_pointer": cell.source_pointer,
                }
            document["input_versions"] = lineage
            document["source_cells"] = [
                dict(asdict(cell), unit=cell.unit) for cell in point.source_cells
            ]
            document["official_headline_mpcsm"]["unit"] = "percent"
            points.append(document)
        return {
            "status": "PARTIAL" if source_failure or source_no_content else "OK",
            "source_failure": source_failure,
            "source_no_content": source_no_content,
            "missing_input_count": sum(
                cell.status in {"missing", "not_provided"} for cell in matrix
            ),
            "last_completed_month": last_completed_month,
            "source_skipped_months": source_skipped_months,
            "provider": "Census",
            "dataset": "MARTS",
            "query_mode": mode,
            "as_of": at,
            "as_of_basis": basis,
            "local_only": local_only,
            "start_month": start,
            "end_month": end,
            "persisted": False,
            "provider_requests": requests,
            "provider_attempts": attempts,
            "units": {
                "levels": "million_current_usd",
                "rates": "ratio",
                "official_headline_mpcsm": "percent",
            },
            "points": points,
        }

    def status(self):
        return {
            "status": "OK",
            "provider": "Census",
            "dataset": "MARTS",
            "source_allowed": self.allow_source is True and self.source is not None,
            **self.store.status(),
        }
