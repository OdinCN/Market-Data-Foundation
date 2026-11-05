"""Durable, explicitly submitted daily jobs; no worker or read-side acquisition."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

from market_data_foundation.historical.financial_daily import (
    SERIES_IDS,
    DailyBatch,
    FinancialError,
    dates,
    day,
    digest,
    instant,
    observation_today,
    registry,
)
from market_data_foundation.historical.financial_daily_credit import (
    ARCHIVE_END,
    ARCHIVE_START,
    CREDIT,
    CreditArchiveSource,
    archive_points,
)
from market_data_foundation.historical.financial_daily_origin import (
    derive,
    derived_points,
    points,
)
from market_data_foundation.historical.financial_daily_runtime import (
    DEFAULT_CONTROL,
    bound_fred_credential,
    build_service,
)
from market_data_foundation.historical.financial_daily_source import (
    DailyFredSource,
    parse_metadata,
    parse_observations,
)
from market_data_foundation.web.cpi_timeseries_jobs import JobJournal, WebError


def stamp(value):
    return instant(datetime.fromisoformat(value) if isinstance(value, str) else value)


def now():
    return datetime.now(UTC).isoformat()


def defaults(today=None, *, at=None):
    end = today or observation_today(at)
    try:
        start = end.replace(year=end.year - 20)
    except ValueError:
        start = end.replace(year=end.year - 20, day=28)
    return dict(start=start.isoformat(), end=end.isoformat())


def identifier(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise WebError("financial_invalid_request_id") from None
    return value


def validate(command):
    if type(command) is not dict or set(command) != {
        "request_id",
        "action",
        "start",
        "end",
    }:
        raise WebError("financial_invalid_job_request")
    identifier(command["request_id"])
    if command["action"] not in {"init", "update"}:
        raise WebError("financial_invalid_job_action")
    days = dates(command["start"], command["end"])
    if (
        len(days) > 366 * 30
        or day(command["end"]) > datetime.now(ZoneInfo("Asia/Shanghai")).date()
    ):
        raise WebError("financial_job_range_invalid")


def years(start, end):
    for year in range(day(start).year, day(end).year + 1):
        yield max(start, f"{year}-01-01"), min(end, f"{year}-12-31")


def plan(command):
    start, end = command["start"], command["end"]
    steps = []

    def add(sid, a, b, source):
        if a <= b:
            steps.append(
                dict(series_id=sid, start=a, end=b, source=source, state="pending")
            )

    for sid in SERIES_IDS:
        if sid.startswith("DFED"):
            add(sid, start, end, "policy")
        elif sid in CREDIT:
            if command["action"] == "init":
                add(sid, max(start, ARCHIVE_START), min(end, ARCHIVE_END), "archive")
                tail = (day(ARCHIVE_END) + timedelta(days=1)).isoformat()
                add(sid, max(start, tail), end, "fred")
            else:
                add(sid, start, end, "fred")
        else:
            # Declared SOFR inception; earlier requested days remain explicit gaps.
            lower = max(start, "2018-04-02") if sid == "SOFR" else start
            for a, b in years(lower, end) if lower <= end else []:
                add(sid, a, b, "derived" if sid == "T10YIE" else "origin")
    # Derived legs must be committed before the one-cutoff calculation.
    return [s for s in steps if s["source"] != "derived"] + [
        s for s in steps if s["source"] == "derived"
    ]


def retain(store, batch):
    store.raw_store.put_verified(batch.raw)
    store.raw_store.put_verified(batch.metadata_raw)
    return dict(
        series_id=batch.series_id,
        start=batch.start,
        end=batch.end,
        raw_sha=digest(batch.raw),
        raw_bytes=len(batch.raw),
        metadata_sha=digest(batch.metadata_raw),
        metadata_bytes=len(batch.metadata_raw),
        observed_at=batch.observed_at.isoformat(),
        metadata_observed_at=batch.metadata_observed_at.isoformat(),
        request=batch.request,
        origin=batch.origin,
        attempts=batch.attempts,
        batch_id=batch.batch_id,
    )


def retained_batch(store, seed):
    raw = store.raw_store.read_verified(seed["raw_sha"], seed["raw_bytes"])
    meta_raw = store.raw_store.read_verified(
        seed["metadata_sha"], seed["metadata_bytes"]
    )
    sid, start, end, origin = (seed[k] for k in ("series_id", "start", "end", "origin"))
    if origin in {"fred", "controlled_fixture"}:
        parsed, meta = (
            parse_observations(raw, sid, start, end),
            parse_metadata(meta_raw, sid),
        )
    else:
        meta = json.loads(meta_raw)
        parsed = (
            archive_points(raw, sid, start, end)
            if origin == "credit_archive"
            else derived_points(raw, start, end)
            if origin == "mdf_derived"
            else points(
                raw,
                sid,
                start,
                end,
                period="year"
                if "field_tdr_date_value" in seed["request"].get("parameters", {})
                else "month",
            )
        )
    batch = DailyBatch(
        sid,
        start,
        end,
        parsed,
        raw,
        meta_raw,
        meta,
        stamp(seed["metadata_observed_at"]),
        stamp(seed["observed_at"]),
        seed["request"],
        origin,
        seed["attempts"],
    )
    if batch.batch_id != seed["batch_id"]:
        raise FinancialError("financial_retained_identity_mismatch")
    return batch


def coverage(store, start, end, *, as_of=None):
    cutoff = stamp(as_of or now())
    s = store.schema
    with store.read_snapshot():
        rows = store.connection.execute(
            f"""
            SELECT series_id,count(*),count(value),
            min(observation_date) FILTER(WHERE value IS NOT NULL),
            max(observation_date) FILTER(WHERE value IS NOT NULL),
            count(*) FILTER(WHERE value IS NULL)
            FROM (SELECT DISTINCT ON(series_id,observation_date) * FROM {s}.revisions
            WHERE observation_date BETWEEN %s AND %s AND available_at<=%s
            ORDER BY series_id,observation_date,revision DESC) r GROUP BY series_id
            """,
            (day(start), day(end), cutoff),
        ).fetchall()
    found = {r[0]: r for r in rows}
    return dict(
        as_of=cutoff.isoformat(),
        provider_requests=0,
        series=[
            dict(
                series_id=sid,
                requested_start=start,
                requested_end=end,
                returned_days=found[sid][1] if sid in found else 0,
                valid_rows=found[sid][2] if sid in found else 0,
                first=found[sid][3].isoformat()
                if sid in found and found[sid][3]
                else None,
                last=found[sid][4].isoformat()
                if sid in found and found[sid][4]
                else None,
                explicit_missing=found[sid][5] if sid in found else 0,
                calendar_days_without_row=len(dates(start, end))
                - (found[sid][1] if sid in found else 0),
                gap_explanation=(
                    "区间早于双边政策目标的有据起点时留缺；其后按具体政策生效日投影，含周末。"
                    if sid.startswith("DFED")
                    else "2018-04-02 前无本来源 SOFR；周末、休市与尚未发布日不补零。"
                    if sid == "SOFR"
                    else (
                        "缺值和无观测日分开；信用保留月末周末观测，"
                        "原机构收益率/隔夜利率不填补休市日。"
                    )
                ),
            )
            for sid in SERIES_IDS
        ],
    )


def local_summary(store, *, as_of=None):
    cutoff = stamp(as_of or now())
    with store.read_snapshot():
        rows = store.connection.execute(
            f"""SELECT series_id,count(*),count(value),
            min(observation_date) FILTER(WHERE value IS NOT NULL),
            max(observation_date) FILTER(WHERE value IS NOT NULL),
            max(available_at),max(observed_at),sum(revision-1)
            FROM (SELECT DISTINCT ON(series_id,observation_date) *
            FROM {store.schema}.revisions WHERE available_at<=%s
            ORDER BY series_id,observation_date,revision DESC) r GROUP BY series_id""",
            (cutoff,),
        ).fetchall()
    found = {r[0]: r for r in rows}
    return dict(
        status="OK",
        as_of=cutoff.isoformat(),
        provider_requests=0,
        observation_count=sum(r[1] for r in rows),
        revision_count=sum(r[7] for r in rows),
        last_available_at=max((r[5] for r in rows), default=None).isoformat()
        if rows
        else None,
        series=[
            dict(
                series_id=sid,
                returned_days=found[sid][1] if sid in found else 0,
                valid_rows=found[sid][2] if sid in found else 0,
                first=found[sid][3].isoformat()
                if sid in found and found[sid][3]
                else None,
                last=found[sid][4].isoformat()
                if sid in found and found[sid][4]
                else None,
                last_available_at=found[sid][5].isoformat() if sid in found else None,
                last_observed_at=found[sid][6].isoformat() if sid in found else None,
            )
            for sid in SERIES_IDS
        ],
    )


class FinancialJobs:
    def __init__(
        self,
        directory,
        *,
        service_factory=build_service,
        fred_factory=None,
        archive_factory=None,
    ):
        self.journal = JobJournal(directory, profile=SimpleNamespace(key="financial"))
        self.service_factory = service_factory
        self.fred_factory, self.archive_factory = fred_factory, archive_factory

    def submit(self, command, *, on_started=None, resume=False):
        validate(command)
        with self.journal.execution():
            previous = self.journal.read(command["request_id"])
            if previous:
                if previous["command"] != command:
                    raise WebError("financial_request_id_conflict", 409)
                if not resume or previous["state"] == "complete":
                    return previous
                record = dict(
                    previous,
                    state="running",
                    resume_count=previous.get("resume_count", 0) + 1,
                )
            else:
                if resume:
                    raise WebError("financial_job_not_found", 404)
                record = dict(
                    request_id=command["request_id"],
                    job_id=command["request_id"],
                    command=command,
                    created_at=now(),
                    started_at=now(),
                    origin="manual",
                    state="running",
                    steps=plan(command),
                    provider_requests=0,
                    resume_count=0,
                )
            self.journal.save(record)
            if on_started:
                on_started(record)
            service = None
            try:
                service = self.service_factory(write=True, live_opt_in=True)
                # A bounded twenty-year job has an explicit 160-request origin budget;
                # ordinary small-window callers retain their existing 20-request budget.
                service.source.max_requests = 160
                archive = fred = None
                for step in record["steps"]:
                    if step["state"] == "complete":
                        continue
                    if step.get("retry_at") and stamp(step["retry_at"]) > stamp(now()):
                        step["state"] = "cooling_down"
                        continue
                    sid, start, end, source = (
                        step[k] for k in ("series_id", "start", "end", "source")
                    )
                    step["state"] = "running"
                    self.journal.save(record)
                    requests_before = 0
                    active_source = None
                    try:
                        if step.get("retained"):
                            batch = retained_batch(service.store, step["retained"])
                        elif source == "derived":
                            batch = derive(
                                service.query(start, end, selected=("DGS10", "DFII10")),
                                observed_at=datetime.now(UTC),
                            )
                            if not batch.points:
                                raise FinancialError(
                                    "financial_derived_inputs_unavailable"
                                )
                        else:

                            def sink(_request, response, _at, _attempts):
                                return service.store.raw_store.put_verified(
                                    response.body
                                )

                            if source == "archive":
                                if archive is None:
                                    archive = (
                                        self.archive_factory()
                                        if self.archive_factory
                                        else CreditArchiveSource.live(
                                            control_path=self.journal.directory
                                            / "archive-control.json",
                                            live_opt_in=True,
                                            raw_sink=sink,
                                        )
                                    )
                                active_source = archive
                            elif source == "fred":
                                if fred is None:
                                    fred = (
                                        self.fred_factory()
                                        if self.fred_factory
                                        else DailyFredSource.live(
                                            series=CREDIT,
                                            credential_loader=bound_fred_credential,
                                            request_control_path=DEFAULT_CONTROL,
                                            live_opt_in=True,
                                            raw_sink=sink,
                                        )
                                    )
                                active_source = fred
                            else:
                                active_source = service.source
                            requests_before = active_source.request_count
                            batch = (
                                active_source.fetch(sid, start, end, policy_span=True)
                                if source == "policy"
                                else active_source.fetch(sid, start, end, period="year")
                                if source == "origin"
                                else active_source.fetch(sid, start, end)
                            )
                        step["retained"] = retain(service.store, batch)
                        self.journal.save(record)
                        saved = service.store.save(batch)
                        service.store.record_attempt(
                            sid, start, end, "complete", bid=batch.batch_id
                        )
                        step.update(
                            state="complete",
                            result=saved,
                            valid_rows=sum(p.value is not None for p in batch.points),
                            returned_days=len(batch.points),
                            observed_at=batch.observed_at.isoformat(),
                            raw_locator=step["retained"]["raw_sha"],
                            origin=batch.origin,
                            acquired_start=batch.start,
                            acquired_end=batch.end,
                            latest_source_observation=batch.metadata.get(
                                "response_last_observation"
                            ),
                            deferred_after=batch.end if batch.end < end else None,
                            deferred_reason="source_observation_day_not_yet_available"
                            if batch.end < end
                            else None,
                        )
                        step.pop("classification", None)
                        step.pop("retry_at", None)
                    except Exception as error:
                        code = (
                            error.classification
                            if isinstance(error, FinancialError)
                            else "financial_storage_outcome_unknown"
                        )
                        state = (
                            "waiting_publication"
                            if code == "financial_origin_future_window_not_acquired"
                            else "unknown"
                            if "unknown" in code
                            else "failed"
                        )
                        step.update(
                            state=state,
                            classification=code,
                            http_status=error.http_status
                            if isinstance(error, FinancialError)
                            else None,
                            retry_at=error.retry_at.isoformat()
                            if isinstance(error, FinancialError) and error.retry_at
                            else None,
                        )
                        service.store.record_attempt(sid, start, end, state, code)
                    finally:
                        count = (
                            active_source.request_count - requests_before
                            if active_source
                            else 0
                        )
                        step["provider_requests"] = (
                            step.get("provider_requests", 0) + count
                        )
                        record["provider_requests"] += count
                        self.journal.save(record)
                record["coverage"] = coverage(
                    service.store, command["start"], command["end"]
                )
                record["state"] = (
                    "complete"
                    if all(s["state"] == "complete" for s in record["steps"])
                    else "partial"
                )
            except Exception as error:
                record.update(
                    state="unknown",
                    classification=error.classification
                    if isinstance(error, FinancialError)
                    else "financial_job_outcome_unknown",
                )
            finally:
                if service:
                    service.store.close()
                record["updated_at"] = now()
                if record["state"] != "running":
                    record["finished_at"] = record["updated_at"]
                self.journal.save(record)
            return record

    def resume(self, request_id, *, on_started=None):
        record = self.journal.read(identifier(request_id))
        if record is None:
            raise WebError("financial_job_not_found", 404)
        return self.submit(record["command"], on_started=on_started, resume=True)


def status():
    return dict(
        status="OK",
        dataset_id="mdf.macro.financial-conditions.daily",
        definition_revision="r1",
        defaults=defaults(),
        series=registry(),
        resident_worker=False,
        source_requests_on_read=0,
        observation_timezone="America/New_York",
        default_date_basis="美国东部当前观测日；并不保证当日已公布，实际可得以来源响应为准。",
        cutoff_semantics="MDF 首次观察/确认可用截止；历史首发与历史初值无证据则未知。",
    )
