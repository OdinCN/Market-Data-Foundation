"""Independent weekly HTTP contract using shared macro permissions and job controls."""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from market_data_foundation.historical.calendar_resilience import failure
from market_data_foundation.historical.claims_calendar import CURRENT_REPORT_URL
from market_data_foundation.historical.claims_contract import (
    METRICS,
    SERIES_IDS,
    twenty_year_range,
    validate_series,
    weeks,
)
from market_data_foundation.historical.claims_release import (
    ClaimsReleaseRecords,
    parse_release,
)
from market_data_foundation.historical.claims_schedule import (
    ClaimsAutoStrategy,
    ClaimsReleaseScheduler,
)
from market_data_foundation.historical.claims_store import SCHEMA, PostgresClaimsStore
from market_data_foundation.historical.claims_timeseries import (
    ClaimsTimeSeriesService,
    _cutoff,
)
from market_data_foundation.web.claims_jobs import (
    CLAIMS_PROFILE,
    ClaimsJobRunner,
    validate_command,
)
from market_data_foundation.web.cpi_timeseries_jobs import JobJournal, WebError
from market_data_foundation.web.gdp_host import GdpTimeSeriesHost

PREFIX = "/api/r5/claims/timeseries"


class ClaimsTimeSeriesHost(GdpTimeSeriesHost):
    profile, source_id, prefix = CLAIMS_PROFILE, "FRED", PREFIX

    def __init__(
        self,
        *,
        packages,
        read_service_factory,
        job_directory,
        schedule_path,
        write_service_factory=None,
        query_service_factory=None,
        query_allowed=False,
        allow_collect=False,
        allow_source=False,
        history_start=None,
        clock=lambda: datetime.now(UTC),
        calendar_fetcher=None,
        fallback_fetcher=None,
        material_fetcher=None,
    ):
        if any(
            type(v) is not bool for v in (query_allowed, allow_collect, allow_source)
        ):
            raise ValueError("explicit_timeseries_permissions_required")
        self.packages, self.read_factory = packages, read_service_factory
        self.write_factory, self.query_factory = (
            write_service_factory,
            query_service_factory,
        )
        self.query_allowed, self.allow_collect, self.allow_source = (
            query_allowed,
            allow_collect,
            allow_source,
        )
        self.clock, self.material_fetcher = clock, material_fetcher
        self.journal = JobJournal(job_directory, profile=CLAIMS_PROFILE)
        self.scheduler = ClaimsReleaseScheduler(
            self,
            schedule_path,
            clock=clock,
            history_start=history_start,
            calendar_fetcher=calendar_fetcher,
            fallback_fetcher=fallback_fetcher,
        )
        self.auto_strategy = ClaimsAutoStrategy(
            self.scheduler,
            Path(schedule_path).with_name("claims-strategy.json"),
            clock=clock,
        )

    def upstream_access(self, source):
        result = super().upstream_access(source)
        if not self.allow_source:
            return dict(allowed=False, reason="claims_source_query_not_enabled")
        return result

    @contextmanager
    def service(self, *, write=False, upstream=False, credential_loader=None):
        factory = (
            self.write_factory
            if write
            else self.query_factory
            if upstream
            else self.read_factory
        )
        if factory is None:
            raise WebError("claims_service_not_bound", 503)
        service = (
            factory(credential_loader=credential_loader)
            if write or upstream
            else factory()
        )
        try:
            if not isinstance(service, ClaimsTimeSeriesService) or (
                isinstance(service.store, PostgresClaimsStore)
                and service.store.schema != SCHEMA
            ):
                raise WebError("claims_service_binding_mismatch", 503)
            yield service
        finally:
            service.close()

    def status(self):
        self._read_allowed()
        with self.service() as service:
            result = service.status()
        start, end = twenty_year_range(self.clock().date().isoformat())
        source = self.source()
        return dict(
            result,
            **self.asset_metadata(),
            source=source,
            provider="FRED",
            original_agency="DOL-ETA",
            web_capabilities=self.capabilities(source),
            source_allowed=self.upstream_access(source)["allowed"],
            initialization=dict(
                preset_id="claims_twenty_calendar_years",
                start=start,
                end=end,
                editable=True,
                available=True,
                years=20,
            ),
            collection_scope=dict(
                series="all_six_native_inputs",
                frequency="weekly",
                range="inclusive_natural_days",
            ),
        )

    def query(self, params):
        self._read_allowed()
        if set(params) - {
            "start",
            "end",
            "series",
            "local_only",
            "query_mode",
            "as_of",
            "as_of_basis",
        }:
            raise WebError("unsupported_parameters")
        weeks(params.get("start"), params.get("end"))
        selected = (
            validate_series(tuple(params["series"].split(",")))
            if "series" in params
            else SERIES_IDS
        )
        local = params.get("local_only", "true")
        if local not in {"true", "false"}:
            raise WebError("invalid_local_only")
        cutoff = _cutoff(params.get("as_of"))
        mode = params.get("query_mode", "history" if cutoff else "current")
        if mode not in {"history", "current"} or (mode == "history") != (
            cutoff is not None
        ):
            raise WebError("claims_history_cutoff_required")
        basis = params.get("as_of_basis", "available")
        if basis not in {"observed", "available"}:
            raise WebError("claims_cutoff_basis_unsupported")
        upstream = local == "false" and mode == "current"
        if upstream:
            access = self.upstream_access(self.source())
            if not access["allowed"]:
                raise WebError(access["reason"], 403)
        with self.service(
            upstream=upstream, credential_loader=self.effective_credential
        ) as service:
            result = service.query_range(
                params["start"],
                params["end"],
                series_ids=selected,
                as_of=cutoff,
                as_of_basis=basis,
                allow_upstream=upstream,
            )
            result = ClaimsReleaseRecords(service.store).link_result(result)
        # A release date is not a reference-week assignment. Keep plans separate.
        return dict(
            result,
            publication_events=self.scheduler.publications(cutoff),
            metadata=dict(
                calendar_binds_current_revision=False,
                calendar_semantics=(
                    "planned_only; reference weeks require source reports"
                ),
            ),
        )

    def releases(self, params):
        self._read_allowed()
        if set(params) - {
            "as_of",
            "read_mode",
            "event_id",
            "reference_week",
            "source_version",
        }:
            raise WebError("unsupported_parameters")
        _cutoff(params.get("as_of"))
        with self.service() as service:
            return ClaimsReleaseRecords(service.store).read(**params)

    def capture_material(self, service, event_id=None):
        if self.material_fetcher is None:
            return dict(status="unbound", classification="claims_dol_material_unbound")
        if event_id is None:
            due = [
                e
                for e in self.scheduler.status()["calendar_publications"]
                if not e.get("classification")
                and e["status"] == "CONFIRMED"
                and datetime.fromisoformat(e["release_at"]) <= self.clock()
            ]
            event_id = (
                max(due, key=lambda e: e["release_at"])["event_id"] if due else None
            )
        try:
            raw = self.material_fetcher()
            obtained = getattr(raw, "obtained_at", self.clock())
            parsed = parse_release(raw, CURRENT_REPORT_URL)
            if event_id and parsed["event_id"] != event_id:
                return dict(
                    status="source_not_updated",
                    expected_event_id=event_id,
                    reported_event_id=parsed["event_id"],
                    classification="claims_dol_source_not_updated",
                )
            records = ClaimsReleaseRecords(service.store)
            records.import_material(raw, CURRENT_REPORT_URL, obtained_at=obtained)
            saved = [
                e
                for e in records.read()["events"]
                if e["event_id"] == parsed["event_id"]
            ]
            if not saved or not saved[0]["actual_available"]:
                raise WebError("claims_material_readback_mismatch", 503)
            return dict(
                status="available",
                event_id=parsed["event_id"],
                evidence_kind="official_current_report_snapshot",
                readback_verified=True,
                actual_weeks={
                    c["series_id"]: c["week_ending"]
                    for c in parsed["claims"]
                    if c["role"] == "actual"
                },
            )
        except Exception as error:
            return dict(
                status="unavailable", **failure(error, "claims_dol_material_failed")
            )

    def submit(
        self, command, *, on_started=None, origin="manual", stage=None, event_id=None
    ):
        self._read_allowed()
        validate_command(command, self.clock())
        existing = next(
            (
                j
                for j in self.journal.list()
                if j["request_id"] == command["request_id"]
            ),
            None,
        )
        if existing:
            if any(existing.get(k) != v for k, v in command.items()):
                raise WebError("request_id_conflict", 409)
            return existing
        record = self.packages.store.read(self.packages.environment_id, "macro")
        access = self.capabilities(self.source(record))
        if not access["collect"]:
            raise WebError(access["reason"], 403)

        def credential_loader():
            if (
                self.packages.store.read(self.packages.environment_id, "macro")
                != record
            ):
                raise WebError("package_configuration_changed", 409)
            return self.effective_credential()

        with self.service(write=True, credential_loader=credential_loader) as service:
            return ClaimsJobRunner(
                service,
                self.journal.directory,
                clock=self.clock,
                capture_material=lambda s: self.capture_material(s, event_id),
            ).submit(command, on_started=on_started, origin=origin, stage=stage)

    def api_help(self):
        return dict(
            **self.asset_metadata(),
            prefix=PREFIX,
            provider="FRED",
            original_agency="DOL-ETA",
            operations=[
                "status",
                "query",
                "releases",
                "jobs",
                "executions",
                "strategy",
                "schedule",
                "api-help",
            ],
            frequency="weekly_ending_saturday",
            series_ids=list(SERIES_IDS),
            series=[asdict(m) for m in METRICS],
            parameters=dict(
                start="required YYYY-MM-DD inclusive natural day",
                end="required YYYY-MM-DD inclusive natural day",
                series="optional native IDs CSV; default all six",
                local_only=(
                    "true default; false current temporary gap fill only; "
                    "no persistence"
                ),
                query_mode="current default; history inferred with as_of",
                as_of="aware ISO8601 inclusive cutoff; required for history",
                as_of_basis=(
                    "available default / observed; "
                    "all derived operands use the same cutoff"
                ),
            ),
            query_defaults=dict(local_only=True, as_of_basis="available"),
            history_local_only=True,
            query_persists=False,
            response_fields=dict(
                rows="week_ending descending, then requested series order",
                value="decimal string|null; Number; SA/NSA explicit",
                weekly_change="Number(t)-Number(t-7d)",
                weekly_change_ratio="ratio; 0.01 means 1%",
                local_four_week_average="local derived; independent from IC4WSA/CC4WSA",
                related_releases="scope_only; no value-equality version binding",
            ),
            release_records=dict(
                parameters=[
                    "as_of",
                    "read_mode",
                    "event_id",
                    "reference_week",
                    "source_version",
                ],
                filters=dict(
                    event_id="exact dol-claims-YYYY-MM-DD; cross-family IDs rejected",
                    reference_week=(
                        "Saturday YYYY-MM-DD coverage filter; actual or prior roles; "
                        "never reference_month"
                    ),
                    source_version="exact YYYY-MM-DD source version, date precision",
                    as_of="inclusive aware available cutoff, no latest fallback",
                ),
                row_association=(
                    "series/week scope_only; explicit roles/source_version; "
                    "scheduled_publication_at only for confirmed actual with "
                    "embargo_header; prior roles never imply first release time"
                ),
                read_modes=["event", "source_history"],
                evidence_identity=(
                    "source_history: (event_id, evidence_id); event_id unchanged"
                ),
                aggregation=(
                    "event: full evidence[] with each original claims/raw metadata; "
                    "top-level metadata_scope=representative_evidence_only; "
                    "claim evidence_id selects original locator, evidence_ids lists "
                    "nonnull supporters or conflicting candidates; conflict has no "
                    "selected evidence/locator"
                ),
                evidence_kinds=["official_current_report_snapshot", "official_archive"],
                surprise="null without named pre-release consensus",
            ),
            collection=dict(
                series="all six",
                default_years=20,
                buffer_days=28,
                job_ack=(
                    "202 after durable running receipt; "
                    "GET jobs/executions for terminal state"
                ),
                interrupted="unknown; no silent replay",
                material_failure="separate from numeric success",
                receipt=dict(
                    fields=[
                        "saved_points",
                        "saved_revisions",
                        "status",
                        "coverage",
                        "series_coverage",
                        "provider_requests",
                        "provider_attempts",
                        "start_date",
                        "end_date",
                        "readback_digest",
                        "readback",
                        "material",
                        "event_binding_status",
                        "completion",
                    ],
                    gap_limit_per_series=16,
                    gap_totals="missing_weeks_total / trailing_missing_weeks_total",
                    truncation="explicit *_truncated flags; totals are uncapped",
                    series_data=PREFIX + "/query",
                    readiness=(
                        "six exact report (series, week) local nonnull available keys"
                    ),
                    readback=(
                        "verified, as_of_basis, actuals_verified, actual_weeks, "
                        "actual_missing"
                    ),
                ),
            ),
            calendar=dict(
                provider="DOL-ETA",
                planned_only=True,
                refresh="explicit worker only; GET never refreshes",
                reference_week=(
                    "independently parsed from source; never release-date offset"
                ),
            ),
            examples=dict(
                current_local=PREFIX + "/query?start=2026-10-01&end=2026-10-31",
                history=PREFIX
                + (
                    "/query?start=2026-10-01&end=2026-10-31"
                    "&as_of=2026-11-02T18%3A00%3A00Z"
                ),
            ),
        )
