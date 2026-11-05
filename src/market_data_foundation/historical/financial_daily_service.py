"""Reusable per-series collection and local-only, one-cutoff daily read contract."""

from datetime import UTC, datetime
from decimal import Decimal, localcontext

from market_data_foundation.historical.financial_daily import (
    BY_ID,
    DATASET_ID,
    DEFINITION_REVISION,
    SERIES_IDS,
    FinancialError,
    dates,
    non_observation_day,
    series_ids,
)


def credit_bp(value):
    with localcontext() as context:
        context.prec = max(28, len(value.as_tuple().digits) + 4)
        return str(value * Decimal(100))


def project(snapshot, selected, start, end):
    values = {(r[0], r[1].isoformat()): r for r in snapshot["values"]}
    batches = snapshot["batches"]
    attempts = {}
    for record in snapshot["attempts"]:
        attempts.setdefault(record[0], []).append(record)
    pending = {(sid, at.isoformat()) for sid, at in snapshot["pending"]}
    rows = []
    for sid in series_ids(selected):
        metric = BY_ID[sid]
        for at in dates(start, end):
            batch = next(
                (
                    r
                    for r in batches
                    if r[0] == sid and r[5].isoformat() <= at <= r[6].isoformat()
                ),
                None,
            )
            metadata = batch[2] if batch else {}
            attempt = next(
                (
                    r
                    for r in attempts.get(sid, [])
                    if r[4].isoformat() <= at <= r[5].isoformat()
                ),
                None,
            )
            row = values.get((sid, at))
            from market_data_foundation.historical.financial_daily_origin import binding

            spec = binding(sid)
            if (sid, at) in pending:
                state = "commit_pending"
            elif row:
                state = "observed" if row[3] is not None else "provider_missing"
            elif (
                not spec["storage_permitted"]
                and attempt
                and attempt[2] == "fred_ice_permission_required"
            ):
                state = "source_permission_required"
            elif attempt and attempt[1] in {"failed", "unknown"}:
                state = (
                    "request_failed"
                    if attempt[1] == "failed"
                    else "request_outcome_unknown"
                )
            elif not batch and not any(r[0] == sid for r in batches):
                state = "not_known_at_cutoff"
            elif not any(
                r[0] == sid and r[1].isoformat() <= at <= r[2].isoformat()
                for r in snapshot["ranges"]
            ):
                state = "not_collected"
            elif (
                metadata.get("observation_start") and at < metadata["observation_start"]
            ):
                state = "outside_provider_range"
            elif non_observation_day(metric, at):
                state = "non_observation_day"
            elif metadata.get("observation_end") and at > metadata["observation_end"]:
                state = "not_yet_available_from_provider"
            elif sid == "T10YIE" and metadata.get("origin") == "mdf_derived":
                state = "derived_inputs_unavailable"
            else:
                state = "not_returned_calendar_unconfirmed"
            last_confirmed = None
            if state == "commit_pending" and row:
                last_confirmed = dict(
                    value=str(row[3]) if row[3] is not None else None,
                    revision=row[2],
                    batch_id=row[5],
                    observed_at=row[6].isoformat(),
                    available_at=row[7].isoformat(),
                )
                row = None
            value = (
                row[3] if row and state in {"observed", "provider_missing"} else None
            )
            origin = row[10] if row else batch[7] if batch else None
            primary = origin in {
                "federal_reserve_board",
                "new_york_fed",
                "us_treasury",
                "mdf_derived",
            }
            provenance = (
                spec
                if primary
                else dict(
                    original_agency=metric.original_agency,
                    distributor=metric.distributor,
                    source_url=metric.source_url,
                    attribution=f"{metric.original_agency} via FRED",
                    identity="FRED_original_series" if origin == "fred" else None,
                )
            )
            if origin == "controlled_fixture":
                provenance["attribution"] = (
                    "Controlled fixture; no official numeric acquisition"
                )
            extra = row[4] if row else {}
            rows.append(
                dict(
                    dataset_id=DATASET_ID,
                    definition_revision=DEFINITION_REVISION,
                    series_id=sid,
                    observation_date=at,
                    state=state,
                    last_confirmed=last_confirmed,
                    value=str(value) if value is not None else None,
                    unit="percent",
                    basis_points=credit_bp(value)
                    if value is not None and metric.credit_bp
                    else None,
                    original_agency=provenance["original_agency"],
                    distributor=provenance["distributor"],
                    source_url=provenance["source_url"],
                    attribution=provenance["attribution"],
                    source_binding=provenance,
                    source_request=row[11] if row and len(row) > 11 else None,
                    identity=provenance.get("identity"),
                    source_publication_at=None,
                    source_available_at=None,
                    policy_effective_date=extra.get("policy_effective_date", at)
                    if row and metric.policy_effective
                    else None,
                    policy_announcement_at=None,
                    revision=row[2] if row else None,
                    observed_at=row[6].isoformat() if row else None,
                    available_at=row[7].isoformat() if row else None,
                    batch_id=row[5] if row else None,
                    raw_locator=row[8] if row else None,
                    metadata_locator=row[9] if row else None,
                    origin=origin,
                    provider_extra=row[4] if row else None,
                    latest_request_state=attempt[1] if attempt else None,
                    latest_request_classification=attempt[2] if attempt else None,
                )
            )
    return dict(
        dataset_id=DATASET_ID,
        definition_revision=DEFINITION_REVISION,
        rows=rows,
        as_of=snapshot["cutoff"].isoformat(),
        as_of_basis=snapshot["basis"],
        provider_requests=0,
        persisted=False,
        semantics="MDF cutoff history; official vintage and release instant unproven",
        coverage={
            sid: {
                "numeric": sum(
                    r["value"] is not None for r in rows if r["series_id"] == sid
                ),
                "states": sorted({r["state"] for r in rows if r["series_id"] == sid}),
            }
            for sid in selected
        },
    )


class FinancialDailyService:
    def __init__(self, store, source=None, *, clock=lambda: datetime.now(UTC)):
        self.store, self.source, self.clock = store, source, clock

    def query(self, start, end, *, selected=SERIES_IDS, as_of=None, basis="available"):
        series_ids(selected)
        dates(start, end)
        return project(
            self.store.read(selected, start, end, as_of=as_of, basis=basis),
            selected,
            start,
            end,
        )

    def collect(self, start, end, *, selected=SERIES_IDS):
        series_ids(selected)
        dates(start, end)
        if self.source is None:
            raise FinancialError("financial_source_not_enabled")
        results = {}
        before = self.source.request_count
        for sid in selected:
            if sid == "T10YIE" and getattr(self.source, "is_origin_binding", False):
                continue
            batch = None
            try:
                batch = self.source.fetch(sid, start, end)
                saved = self.store.save(batch)
                self.store.record_attempt(
                    sid, start, end, "complete", bid=saved["batch_id"]
                )
                results[sid] = dict(state="complete", **saved)
            except FinancialError as error:
                state = (
                    "unknown" if "outcome_unknown" in error.classification else "failed"
                )
                self.store.record_attempt(sid, start, end, state, error.classification)
                results[sid] = dict(
                    state=state,
                    classification=error.classification,
                    retained_batch_id=batch.batch_id if batch else None,
                    http_status=error.http_status,
                    retry_at=error.retry_at.isoformat() if error.retry_at else None,
                )
            except Exception:
                self.store.record_attempt(
                    sid, start, end, "unknown", "financial_storage_outcome_unknown"
                )
                results[sid] = dict(
                    state="unknown",
                    classification="financial_storage_outcome_unknown",
                    retained_batch_id=batch.batch_id if batch else None,
                )
        if "T10YIE" in selected and getattr(self.source, "is_origin_binding", False):
            from market_data_foundation.historical.financial_daily_origin import derive

            try:
                inputs = self.query(start, end, selected=("DGS10", "DFII10"))
                batch = derive(inputs, observed_at=self.clock())
                if not batch.points:
                    raise FinancialError("financial_derived_inputs_unavailable")
                saved = self.store.save(batch)
                self.store.record_attempt(
                    "T10YIE", start, end, "complete", bid=saved["batch_id"]
                )
                results["T10YIE"] = dict(state="complete", **saved)
            except FinancialError as error:
                self.store.record_attempt(
                    "T10YIE", start, end, "failed", error.classification
                )
                results["T10YIE"] = dict(
                    state="failed", classification=error.classification
                )
        return dict(
            state="complete"
            if all(r["state"] == "complete" for r in results.values())
            else "partial",
            dataset_id=DATASET_ID,
            series=results,
            provider_requests=self.source.request_count - before,
            readback=self.query(start, end, selected=selected),
        )

    def recover(self, batch_id):
        # Uses retained content and original capture times; never calls FRED.
        return self.store.recover(batch_id)

    def close(self):
        self.store.close()
