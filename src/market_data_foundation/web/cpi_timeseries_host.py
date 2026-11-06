"""Opt-in DEV composition of capability-specific monthly time-series Hosts."""

from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

from market_data_foundation.historical.bls_timeseries_profile import (
    CONSUMPTION_PROFILE,
    CPI_PROFILE,
    EMPLOYMENT_PROFILE,
    JOLTS_PROFILE,
    PCE_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.cpi_release_schedule import CpiReleaseScheduler
from market_data_foundation.historical.cpi_timeseries import (
    DEFAULT_REQUEST_CONTROL_PATH,
    CpiTimeSeriesService,
    build_service,
    build_source,
)
from market_data_foundation.historical.cpi_timeseries_cli import (
    WorkerService,
    persisted_history_start,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    PostgresTimeSeriesStore,
)
from market_data_foundation.historical.pce_release_schedule import PceReleaseScheduler
from market_data_foundation.storage.postgres_runtime_config import load_pg_config
from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.cpi_query import CpiQueryAccess
from market_data_foundation.web.cpi_timeseries_api import (
    PREFIXES,
    TimeSeriesHost,
    admitted_job,
    admitted_strategy,
    timeseries_router,
)
from market_data_foundation.web.cpi_timeseries_jobs import WebError
from market_data_foundation.web.package_catalog import (
    CredentialState,
    Eligibility,
    HistoryEvidence,
    PackageError,
)
from market_data_foundation.web.packages import PackageHost
from market_data_foundation.web.pce_api import add_read_routes
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN, admitted
from market_data_foundation.web.pce_timeseries import PceTimeSeriesHost
from market_data_foundation.web.readonly_host import (
    ReadonlyRuntime,
    _clock,
    admitted_package_save,
    admitted_package_verify,
)


class LocalOnlySource:
    def fetch(self, *_args, **_kwargs):
        raise RuntimeError("local_only_source_required")


class TimeSeriesPackages(PackageHost):
    """CPI eligibility uses time-series evidence without inventing Publication."""

    def credential_states(self, package_id, record, *, source_ids=None):
        scoped = getattr(self, "scoped_credentials", None)
        if source_ids is None or scoped is None:
            return super().credential_states(package_id, record, source_ids=source_ids)
        # Keep reference precedence and failure handling in PackageHost while
        # restricting legacy credential inspection to this capability's source.
        isolated = PackageHost(
            self.store,
            self.environment_id,
            credentials=lambda environment, package, current: scoped(
                environment, package, current, source_ids=source_ids
            ),
            credential_replace_enabled=self.credential_replace_enabled,
        )
        return isolated.credential_states(package_id, record, source_ids=source_ids)

    def project(self, package_id, record, access):
        view = super().project(package_id, record, access)
        if package_id != "macro":
            return view
        hosts = self.timeseries_hosts
        definitions = {
            (c.definition_ref.identity, c.definition_ref.revision): hosts[
                c.capability_id
            ]
            for c in view.capabilities
            if c.capability_id in hosts
        }
        assets = []
        for asset in view.assets:
            revisions = []
            for revision in asset.revisions:
                host = definitions.get(
                    (revision.definition_ref.identity, revision.definition_ref.revision)
                )
                if host is None and revision.definition_ref.identity in {
                    "mdf.macro.bls.employment.ces-cps",
                    "mdf.macro.census.retail",
                    "mdf.macro.bea.consumption-income",
                    "mdf.macro.bea.gdp-growth",
                    "mdf.macro.dol.claims",
                    "mdf.macro.bls.jolts",
                    "mdf.macro.census.durable-capital-goods",
                }:
                    unavailable = Eligibility(
                        allowed=False,
                        reasons=[
                            "m3_timeseries_not_enabled"
                            if revision.definition_ref.identity
                            == "mdf.macro.census.durable-capital-goods"
                            else "jolts_timeseries_not_enabled"
                            if revision.definition_ref.identity == "mdf.macro.bls.jolts"
                            else "claims_timeseries_not_enabled"
                            if revision.definition_ref.identity
                            == "mdf.macro.dol.claims"
                            else "gdp_timeseries_not_enabled"
                            if revision.definition_ref.identity
                            == "mdf.macro.bea.gdp-growth"
                            else "consumption_timeseries_not_enabled"
                            if revision.definition_ref.identity
                            == "mdf.macro.bea.consumption-income"
                            else "retail_timeseries_not_enabled"
                            if revision.definition_ref.identity
                            == "mdf.macro.census.retail"
                            else "employment_timeseries_not_enabled"
                        ],
                    )
                    revision = revision.model_copy(
                        update={
                            key: unavailable
                            for key in (
                                "local_read",
                                "upstream_read",
                                "collect",
                                "plan",
                            )
                        }
                    )
                if (
                    revision.definition_ref.identity
                    == "mdf.macro.financial-conditions.daily"
                ):
                    financial = getattr(self, "financial_options", None)
                    periodic_available = bool(
                        financial
                        and financial.allow_read
                        and financial.allow_collect
                        and financial.allow_strategy
                        and financial.state_directory
                        and financial.window_path
                    )
                    revision = revision.model_copy(
                        update={
                            "collect": Eligibility(
                                allowed=bool(financial and financial.allow_collect),
                                reasons=["financial_manual_host_opt_in"],
                            ),
                            "upstream_read": Eligibility(
                                allowed=None,
                                reasons=[
                                    "financial_named_sources_checked_per_explicit_job"
                                ],
                            ),
                            "plan": Eligibility(
                                allowed=periodic_available,
                                reasons=[
                                    "financial_periodic_host_opt_in"
                                    if periodic_available
                                    else "financial_periodic_host_not_enabled"
                                ],
                            ),
                        }
                    )
                if host is not None:
                    source = host.source(record)
                    capabilities = host.capabilities(source)
                    upstream = host.upstream_access(source)
                    readable = (
                        revision.local_holding == "PRESENT"
                        and revision.metadata_view_permission == "allowed"
                        and capabilities["query"]
                    )
                    revision = revision.model_copy(
                        update={
                            "local_read": Eligibility(
                                allowed=readable,
                                reasons=[
                                    host.profile.key + "_timeseries_local_coverage"
                                    if readable
                                    else host.profile.key
                                    + "_timeseries_read_unavailable"
                                ],
                            ),
                            "upstream_read": Eligibility(
                                allowed=upstream["allowed"],
                                reasons=[
                                    upstream["reason"]
                                    or host.profile.key + "_local_first_gap_passthrough"
                                ],
                            ),
                            "collect": Eligibility(
                                allowed=capabilities["collect"],
                                reasons=[
                                    capabilities["reason"]
                                    or "host_and_package_authorized"
                                ],
                            ),
                            "plan": Eligibility(
                                allowed=False, reasons=["web_host_has_no_worker"]
                            ),
                        }
                    )
                revisions.append(revision)
            assets.append(asset.model_copy(update={"revisions": revisions}))
        return view.model_copy(update={"assets": assets})


class TimeSeriesRuntime(ReadonlyRuntime):
    def __init__(
        self,
        settings,
        *,
        schedule_path,
        job_directory,
        connector=None,
        clock=_clock,
        cpi_access=CpiQueryAccess(),
        pce_access=CpiQueryAccess(),
        allow_collect=False,
        read_service_factory=None,
        write_service_factory=None,
        max_collect_months=None,
        history_start="1999-01",
        query_service_factory=None,
        ppi_enabled=False,
        ppi_read_service_factory=None,
        ppi_write_service_factory=None,
        ppi_query_service_factory=None,
        ppi_schedule_path=None,
        ppi_job_directory=None,
        ppi_access=CpiQueryAccess(),
        ppi_allow_collect=False,
        ppi_history_start=None,
        pce_enabled=False,
        pce_read_service_factory=None,
        pce_write_service_factory=None,
        pce_query_service_factory=None,
        pce_schedule_path=None,
        pce_job_directory=None,
        pce_allow_collect=False,
        pce_history_start=None,
        pce_calendar_fetcher=None,
        pce_request_control_path=None,
        employment_enabled=False,
        employment_read_service_factory=None,
        employment_write_service_factory=None,
        employment_query_service_factory=None,
        employment_schedule_path=None,
        employment_job_directory=None,
        employment_history_start=None,
        employment_access=CpiQueryAccess(),
        employment_allow_collect=False,
        employment_calendar_fetcher=None,
        employment_reference_fetcher=None,
        consumption_enabled=False,
        consumption_read_service_factory=None,
        consumption_write_service_factory=None,
        consumption_query_service_factory=None,
        consumption_schedule_path=None,
        consumption_job_directory=None,
        consumption_history_start=None,
        consumption_access=CpiQueryAccess(),
        consumption_allow_collect=False,
        consumption_calendar_fetcher=None,
        retail_enabled=False,
        retail_schedule_path=None,
        retail_request_control_path=None,
        retail_calendar_fetcher=None,
        retail_access=CpiQueryAccess(),
        retail_allow_collect=False,
        retail_allow_source=False,
        retail_artifacts_path=None,
        retail_job_directory=None,
        retail_read_service_factory=None,
        retail_write_service_factory=None,
        retail_query_service_factory=None,
        gdp_options=None,
        claims_options=None,
        jolts_options=None,
        m3_query_host=None,
        m3_options=None,
    ):
        if m3_query_host is not None:
            from market_data_foundation.web.m3_query_api import M3QueryHost

            if not isinstance(m3_query_host, M3QueryHost):
                raise ValueError("m3_query_host_invalid")
        self.m3_query_host = m3_query_host
        super().__init__(
            settings,
            connector=connector,
            clock=clock,
            cpi_access=cpi_access,
            pce_access=pce_access,
        )
        if not isinstance(ppi_access, CpiQueryAccess):
            raise ValueError("readonly_explicit_access_required")
        if type(ppi_enabled) is not bool or type(ppi_allow_collect) is not bool:
            raise ValueError("explicit_timeseries_permissions_required")
        self.accesses["ppi"] = ppi_access
        self.employment_timeseries = None
        self.accesses["employment"] = employment_access
        if (
            not isinstance(employment_access, CpiQueryAccess)
            or type(employment_enabled) is not bool
            or type(employment_allow_collect) is not bool
        ):
            raise ValueError("explicit_timeseries_permissions_required")
        self.ppi_timeseries = None
        self.pce_timeseries = None
        if type(pce_enabled) is not bool or type(pce_allow_collect) is not bool:
            raise ValueError("explicit_timeseries_permissions_required")
        self.packages = TimeSeriesPackages(
            self.packages.store,
            settings.environment_id,
            authorize=self.authorize_package,
            credentials=self.credential_states,
            credential_loader=self.load_legacy_key,
            history=self.history,
            credential_replace_enabled=settings.package_config_save,
        )
        self.packages.scoped_credentials = self.scoped_credential_states
        self.retail_timeseries = None
        self.accesses["retail"] = retail_access
        self.retail_artifacts_path = retail_artifacts_path
        if any(
            type(v) is not bool
            for v in (retail_enabled, retail_allow_collect, retail_allow_source)
        ) or not isinstance(retail_access, CpiQueryAccess):
            raise ValueError("explicit_timeseries_permissions_required")
        from market_data_foundation.historical.cpi_request_control import (
            CpiRequestControl,
        )

        self.request_control = CpiRequestControl(
            DEFAULT_REQUEST_CONTROL_PATH, clock=clock
        )
        self.timeseries = TimeSeriesHost(
            packages=self.packages,
            read_service_factory=read_service_factory or self.read_service,
            write_service_factory=write_service_factory or self.write_service,
            scheduler=CpiReleaseScheduler(
                WorkerService(self.worker_service),
                schedule_path,
                clock=clock,
                history_start=persisted_history_start(schedule_path, history_start),
                auto_mode=True,
            ),
            query_service_factory=query_service_factory or self.query_service,
            job_directory=job_directory,
            allow_collect=allow_collect,
            query_allowed=(
                cpi_access.metadata_permission == "allowed"
                and cpi_access.read_permission == "allowed"
            ),
            max_collect_months=max_collect_months,
            clock=clock,
        )
        self.packages.timeseries = self.timeseries
        self.packages.timeseries_hosts = {"cpi": self.timeseries}
        configured = ppi_enabled or any(
            factory is not None
            for factory in (
                ppi_read_service_factory,
                ppi_write_service_factory,
                ppi_query_service_factory,
            )
        )
        if configured:
            if ppi_schedule_path is None or ppi_job_directory is None:
                raise ValueError("ppi_independent_paths_required")
            if not all(
                Path(p).is_absolute() and ".." not in Path(p).parts
                for p in (ppi_schedule_path, ppi_job_directory)
            ):
                raise ValueError("ppi_independent_absolute_paths_required")
            if (
                Path(ppi_schedule_path).resolve() == Path(schedule_path).resolve()
                or Path(ppi_job_directory).resolve() == Path(job_directory).resolve()
            ):
                raise ValueError("ppi_independent_paths_required")
            baseline = ppi_history_start
            if baseline is None:
                raise ValueError("ppi_history_start_evidence_required")
            self.ppi_timeseries = TimeSeriesHost(
                profile=PPI_PROFILE,
                packages=self.packages,
                read_service_factory=ppi_read_service_factory or self.ppi_read_service,
                write_service_factory=ppi_write_service_factory
                or (self.ppi_write_service if ppi_enabled else None),
                query_service_factory=ppi_query_service_factory
                or (self.ppi_query_service if ppi_enabled else None),
                scheduler=CpiReleaseScheduler(
                    WorkerService(self.ppi_worker_service, profile=PPI_PROFILE),
                    ppi_schedule_path,
                    profile=PPI_PROFILE,
                    clock=clock,
                    history_start=persisted_history_start(ppi_schedule_path, baseline),
                    auto_mode=True,
                ),
                job_directory=ppi_job_directory,
                allow_collect=ppi_allow_collect,
                query_allowed=ppi_access.metadata_permission == "allowed"
                and ppi_access.read_permission == "allowed",
                max_collect_months=max_collect_months,
                clock=clock,
            )
            self.packages.timeseries_hosts["ppi"] = self.ppi_timeseries

        pce_configured = pce_enabled or any(
            factory is not None
            for factory in (
                pce_read_service_factory,
                pce_write_service_factory,
                pce_query_service_factory,
            )
        )
        if pce_configured:
            from market_data_foundation.historical.pce_timeseries_source import (
                BeaRequestControl,
            )

            control_path = Path(
                pce_request_control_path
                or (settings.config_directory / "bea-timeseries-request-control.json")
            )
            if not control_path.is_absolute() or ".." in control_path.parts:
                raise ValueError("bea_absolute_request_control_path_required")
            if control_path.resolve() == self.request_control.state_path.resolve():
                raise ValueError("bea_independent_request_control_required")
            self.bea_request_control = BeaRequestControl(control_path.resolve())
            if pce_schedule_path is None or pce_job_directory is None:
                raise ValueError("pce_independent_paths_required")
            new_paths = (pce_schedule_path, pce_job_directory)
            if not all(
                Path(p).is_absolute() and ".." not in Path(p).parts for p in new_paths
            ):
                raise ValueError("pce_independent_absolute_paths_required")
            existing_paths = [schedule_path, job_directory]
            if configured:
                existing_paths.extend((ppi_schedule_path, ppi_job_directory))
            resolved = [Path(p).resolve() for p in new_paths]
            existing = [Path(p).resolve() for p in existing_paths]
            if any(
                left == right or left in right.parents or right in left.parents
                for left, right in [
                    (resolved[0], resolved[1]),
                    *((left, right) for left in resolved for right in existing),
                ]
            ):
                raise ValueError("pce_independent_paths_required")
            from market_data_foundation.historical.cpi_timeseries import month_at

            now = clock().astimezone(ZoneInfo("Asia/Shanghai"))
            # Match the 240-month preset ending in Beijing's previous month.
            baseline = pce_history_start or month_at(
                now.year * 12 + now.month - 2 - 239
            )
            scheduler_options = {}
            if pce_calendar_fetcher is not None:
                scheduler_options["calendar_fetcher"] = pce_calendar_fetcher
            self.pce_timeseries = PceTimeSeriesHost(
                packages=self.packages,
                read_service_factory=pce_read_service_factory or self.pce_read_service,
                write_service_factory=pce_write_service_factory
                or (self.pce_write_service if pce_enabled else None),
                query_service_factory=pce_query_service_factory
                or (self.pce_query_service if pce_enabled else None),
                scheduler=PceReleaseScheduler(
                    WorkerService(self.pce_worker_service, profile=PCE_PROFILE),
                    pce_schedule_path,
                    clock=clock,
                    history_start=persisted_history_start(pce_schedule_path, baseline),
                    **scheduler_options,
                ),
                job_directory=pce_job_directory,
                allow_collect=pce_allow_collect,
                query_allowed=pce_access.metadata_permission == "allowed"
                and pce_access.read_permission == "allowed",
                max_collect_months=max_collect_months,
                clock=clock,
            )
            self.packages.timeseries_hosts["pce"] = self.pce_timeseries

        self.consumption_timeseries = None
        self.accesses["consumption"] = consumption_access
        if not isinstance(consumption_access, CpiQueryAccess) or any(
            type(v) is not bool
            for v in (consumption_enabled, consumption_allow_collect)
        ):
            raise ValueError("explicit_timeseries_permissions_required")
        if consumption_enabled or any(
            factory is not None
            for factory in (
                consumption_read_service_factory,
                consumption_write_service_factory,
                consumption_query_service_factory,
            )
        ):
            from market_data_foundation.historical.consumption_schedule import (
                ConsumptionReleaseScheduler,
            )
            from market_data_foundation.historical.cpi_timeseries import month_at
            from market_data_foundation.historical.pce_timeseries_source import (
                BeaRequestControl,
            )
            from market_data_foundation.web.consumption_timeseries import (
                ConsumptionTimeSeriesHost,
            )

            paths = [consumption_schedule_path, consumption_job_directory]
            prior = [
                schedule_path,
                job_directory,
                ppi_schedule_path,
                ppi_job_directory,
                pce_schedule_path,
                pce_job_directory,
                employment_schedule_path,
                employment_job_directory,
                retail_schedule_path,
                retail_job_directory,
            ]
            prior += [
                Path(p).with_name(name)
                for p, name in (
                    (schedule_path, "strategy.json"),
                    (ppi_schedule_path, "ppi-strategy.json"),
                    (pce_schedule_path, "pce-strategy.json"),
                    (employment_schedule_path, "employment-strategy.json"),
                    (retail_schedule_path, "retail-strategy.json"),
                )
                if p is not None
            ]
            if any(
                p is None or not Path(p).is_absolute() or ".." in Path(p).parts
                for p in paths
            ):
                raise ValueError("consumption_independent_absolute_paths_required")
            resolved = [Path(p).resolve() for p in paths]
            if any(
                a == b or a in b.parents or b in a.parents
                for a, b in [
                    (resolved[0], resolved[1]),
                    *(
                        (a, Path(b).resolve())
                        for a in resolved
                        for b in prior
                        if b is not None
                    ),
                ]
            ):
                raise ValueError("consumption_independent_paths_required")
            if not hasattr(self, "bea_request_control"):
                control = Path(
                    pce_request_control_path
                    or settings.config_directory / "bea-timeseries-request-control.json"
                )
                if not control.is_absolute() or ".." in control.parts:
                    raise ValueError("bea_absolute_request_control_path_required")
                if control.resolve() == self.request_control.state_path.resolve():
                    raise ValueError("bea_independent_request_control_path_required")
                self.bea_request_control = BeaRequestControl(control)
            if any(
                a == self.bea_request_control.state_path.resolve()
                or a in self.bea_request_control.state_path.resolve().parents
                for a in resolved
            ):
                raise ValueError("consumption_independent_paths_required")
            now = clock().astimezone(ZoneInfo("Asia/Shanghai"))
            baseline = consumption_history_start or month_at(
                now.year * 12 + now.month - 2 - 239
            )
            scheduler_options = (
                {"calendar_fetcher": consumption_calendar_fetcher}
                if consumption_calendar_fetcher is not None
                else {}
            )
            self.consumption_timeseries = ConsumptionTimeSeriesHost(
                packages=self.packages,
                read_service_factory=consumption_read_service_factory
                or self.consumption_read_service,
                write_service_factory=consumption_write_service_factory
                or (self.consumption_write_service if consumption_enabled else None),
                query_service_factory=consumption_query_service_factory
                or (self.consumption_query_service if consumption_enabled else None),
                scheduler=ConsumptionReleaseScheduler(
                    WorkerService(
                        self.consumption_worker_service, profile=CONSUMPTION_PROFILE
                    ),
                    consumption_schedule_path,
                    clock=clock,
                    history_start=persisted_history_start(
                        consumption_schedule_path, baseline
                    ),
                    **scheduler_options,
                ),
                job_directory=consumption_job_directory,
                allow_collect=consumption_allow_collect,
                query_allowed=consumption_access.metadata_permission == "allowed"
                and consumption_access.read_permission == "allowed",
                max_collect_months=max_collect_months,
                clock=clock,
            )
            self.packages.timeseries_hosts["consumption"] = self.consumption_timeseries

        if self.pce_timeseries is not None or self.consumption_timeseries is not None:
            from market_data_foundation.historical.bea_shared_material import (
                calendar_fetcher,
            )

            shared_dir = self.bea_request_control.state_path.parent
            shared_calendar = calendar_fetcher(
                shared_dir / "bea-pio-calendar.json", clock=clock
            )
            from market_data_foundation.historical.pce_release_schedule import (
                fetch_official_html,
                parse_official_html,
            )

            shared_backup = calendar_fetcher(
                shared_dir / "bea-pio-calendar-html.json",
                fetcher=fetch_official_html,
                parser=parse_official_html,
                clock=clock,
            )
            for host, explicit in (
                (self.pce_timeseries, pce_calendar_fetcher),
                (self.consumption_timeseries, consumption_calendar_fetcher),
            ):
                if host is not None:
                    host.material_state_path = (
                        shared_dir / "bea-pio-material-acquisition.json"
                    )
                    if explicit is None:
                        host.scheduler.calendar_fetcher = shared_calendar
                        host.scheduler.fallback_fetcher = shared_backup

        if employment_enabled:
            paths = (employment_schedule_path, employment_job_directory)
            existing = [
                schedule_path,
                job_directory,
                ppi_schedule_path,
                ppi_job_directory,
                pce_schedule_path,
                pce_job_directory,
            ]
            if any(
                p is None or not Path(p).is_absolute() or ".." in Path(p).parts
                for p in paths
            ):
                raise ValueError("employment_independent_absolute_paths_required")
            resolved = [Path(p).resolve() for p in paths]
            prior = [Path(p).resolve() for p in existing if p is not None]
            if any(
                a == b or a in b.parents or b in a.parents
                for a, b in [
                    (resolved[0], resolved[1]),
                    *((a, b) for a in resolved for b in prior),
                ]
            ):
                raise ValueError("employment_independent_paths_required")
            from market_data_foundation.historical.cpi_timeseries import month_at

            now = clock().astimezone(ZoneInfo("Asia/Shanghai"))
            baseline = employment_history_start or month_at(
                now.year * 12 + now.month - 2 - 239
            )
            scheduler_options = {}
            if employment_calendar_fetcher is not None:
                scheduler_options["calendar_fetcher"] = employment_calendar_fetcher
            if employment_reference_fetcher is not None:
                scheduler_options["reference_fetcher"] = employment_reference_fetcher
            from market_data_foundation.historical.employment_release_schedule import (
                EmploymentReleaseScheduler,
            )
            from market_data_foundation.web.employment_timeseries import (
                EmploymentTimeSeriesHost,
            )

            self.employment_timeseries = EmploymentTimeSeriesHost(
                packages=self.packages,
                read_service_factory=employment_read_service_factory
                or self.employment_read_service,
                write_service_factory=employment_write_service_factory
                or self.employment_write_service,
                query_service_factory=employment_query_service_factory
                or self.employment_query_service,
                scheduler=EmploymentReleaseScheduler(
                    WorkerService(
                        self.employment_worker_service, profile=EMPLOYMENT_PROFILE
                    ),
                    employment_schedule_path,
                    clock=clock,
                    history_start=persisted_history_start(
                        employment_schedule_path, baseline
                    ),
                    auto_mode=True,
                    **scheduler_options,
                ),
                job_directory=employment_job_directory,
                allow_collect=employment_allow_collect,
                query_allowed=employment_access.metadata_permission == "allowed"
                and employment_access.read_permission == "allowed",
                max_collect_months=max_collect_months,
                clock=clock,
            )
            self.packages.timeseries_hosts["employment"] = self.employment_timeseries

        if retail_enabled:
            from market_data_foundation.web.retail_timeseries_host import (
                RetailContinuousHost,
            )

            if retail_job_directory is None or retail_artifacts_path is None:
                raise ValueError("retail_explicit_paths_required")
            paths = [Path(retail_job_directory), Path(retail_artifacts_path)]
            prior = [
                Path(p)
                for p in (
                    job_directory,
                    schedule_path,
                    ppi_job_directory,
                    ppi_schedule_path,
                    pce_job_directory,
                    pce_schedule_path,
                    employment_job_directory,
                    employment_schedule_path,
                )
                if p is not None
            ]
            if (
                any(not p.is_absolute() or ".." in p.parts for p in paths)
                or "artifacts" not in paths[1].parts
            ):
                raise ValueError("retail_independent_artifacts_paths_required")
            resolved = [p.resolve() for p in paths]
            if any(
                a == b or a in b.parents or b in a.parents
                for a, b in [
                    (resolved[0], resolved[1]),
                    *((a, b.resolve()) for a in resolved for b in prior),
                ]
            ):
                raise ValueError("retail_independent_paths_required")
            from market_data_foundation.historical.retail_release_scheduler import (
                calendar_fetcher,
            )

            retail_schedule_path = Path(
                retail_schedule_path
                or Path(retail_job_directory).parent / "retail-schedule.json"
            )
            retail_request_control_path = Path(
                retail_request_control_path
                or Path(retail_job_directory).parent
                / "retail-census-request-control.json"
            )
            new_files = [
                retail_schedule_path,
                retail_request_control_path,
                retail_schedule_path.with_name("retail-strategy.json"),
            ]
            isolated = (
                resolved
                + [p.resolve() for p in prior]
                + [self.request_control.state_path.resolve()]
            )
            isolated += [
                Path(p).with_name(name).resolve()
                for p, name in (
                    (schedule_path, "strategy.json"),
                    (ppi_schedule_path, "ppi-strategy.json"),
                    (pce_schedule_path, "pce-strategy.json"),
                    (employment_schedule_path, "employment-strategy.json"),
                )
                if p is not None
            ]
            if hasattr(self, "bea_request_control"):
                isolated.append(self.bea_request_control.state_path.resolve())
            if (
                any(not p.is_absolute() or ".." in p.parts for p in new_files)
                or len({p.resolve() for p in new_files}) != len(new_files)
                or any(
                    a.resolve() == b or b in a.resolve().parents
                    for a in new_files
                    for b in isolated
                )
            ):
                raise ValueError("retail_independent_control_paths_required")
            self.retail_request_control = CpiRequestControl(
                retail_request_control_path, clock=clock
            )
            self.retail_timeseries = RetailContinuousHost(
                packages=self.packages,
                read_service_factory=retail_read_service_factory
                or self.retail_read_service,
                write_service_factory=(
                    retail_write_service_factory or self.retail_write_service
                )
                if retail_allow_collect
                else None,
                query_service_factory=(
                    retail_query_service_factory or self.retail_query_service
                )
                if retail_allow_source
                else None,
                schedule_path=retail_schedule_path,
                history_calendar_fetcher=(
                    calendar_fetcher(self.retail_request_control)
                    if retail_calendar_fetcher is None
                    else None
                ),
                calendar_fetcher=retail_calendar_fetcher
                or calendar_fetcher(self.retail_request_control),
                job_directory=retail_job_directory,
                clock=clock,
                query_allowed=retail_access.metadata_permission == "allowed"
                and retail_access.read_permission == "allowed",
                allow_collect=retail_allow_collect,
                allow_source=retail_allow_source,
            )
            self.packages.timeseries_hosts["retail"] = self.retail_timeseries

        from market_data_foundation.web.gdp_runtime import bind_gdp

        paths = [
            schedule_path,
            job_directory,
            ppi_schedule_path,
            ppi_job_directory,
            pce_schedule_path,
            pce_job_directory,
            employment_schedule_path,
            employment_job_directory,
            retail_schedule_path,
            retail_job_directory,
            consumption_schedule_path,
            consumption_job_directory,
        ]
        paths += [
            Path(p).with_name(name)
            for p, name in (
                (schedule_path, "strategy.json"),
                (ppi_schedule_path, "ppi-strategy.json"),
                (pce_schedule_path, "pce-strategy.json"),
                (employment_schedule_path, "employment-strategy.json"),
                (retail_schedule_path, "retail-strategy.json"),
                (consumption_schedule_path, "consumption-strategy.json"),
            )
            if p is not None
        ]
        bind_gdp(self, gdp_options, occupied_paths=paths)
        from market_data_foundation.web.claims_runtime import bind_claims

        if gdp_options is not None:
            paths += [
                gdp_options.schedule_path,
                gdp_options.job_directory,
                gdp_options.schedule_path.with_name("gdp-strategy.json"),
            ]
        bind_claims(self, claims_options, occupied_paths=paths)
        from market_data_foundation.web.jolts_runtime import bind_jolts

        if claims_options is not None:
            paths += [
                claims_options.schedule_path,
                claims_options.job_directory,
                claims_options.schedule_path.with_name("claims-strategy.json"),
                claims_options.artifacts_path,
                claims_options.request_control_path,
            ]
        bind_jolts(self, jolts_options, occupied_paths=paths)
        from market_data_foundation.web.m3_runtime import bind_m3

        if m3_query_host is not None and m3_options is not None:
            raise ValueError("m3_host_binding_conflict")
        bind_m3(self, m3_options, occupied_paths=paths)
        from market_data_foundation.web.source_verification import bind_source_verifier

        bind_source_verifier(self, claims_options)

    def retail_read_service(self):
        return self.retail_service()

    def retail_query_service(self, *, credential_loader):
        return self.retail_service(credential_loader=credential_loader)

    def retail_write_service(self, *, credential_loader):
        return self.retail_service(write=True, credential_loader=credential_loader)

    def retail_service(self, *, write=False, credential_loader=None):
        from market_data_foundation.historical.census_retail_timeseries_source import (
            CensusRetailRangeSource,
        )
        from market_data_foundation.historical.retail_timeseries import (
            RetailTimeSeriesService,
        )
        from market_data_foundation.historical.retail_timeseries_store import (
            FileRetailBatchArchive,
            PostgresRetailStore,
        )
        from market_data_foundation.web.retail_timeseries_host import RETAIL_SCHEMA

        if (
            load_pg_config(self.settings.config_directory / "runtime.toml")
            != self.profile
        ):
            raise WebError("postgres_profile_changed", 503)
        source = None
        if credential_loader is not None:
            from market_data_foundation.historical.census_retail_live import (
                ExactCensusTransport,
            )
            from market_data_foundation.historical.retail_release_scheduler import (
                CensusControlledTransport,
            )

            source = CensusRetailRangeSource(
                transport=CensusControlledTransport(
                    ExactCensusTransport(), self.retail_request_control
                ),
                enabled=True,
                credential=credential_loader(),
                clock=self.clock,
            )
        if write:
            password = self._secret("postgres", "password")
            if password is None:
                raise WebError("postgres_credential_missing", 503)
            connection = self.connector(
                **self.profile.connection_kwargs(),
                password=password,
                autocommit=True,
                connect_timeout=5,
                options="-c statement_timeout=10000",
            )
        else:
            connection = self.connect(
                **self.profile.connection_kwargs(),
                autocommit=True,
                connect_timeout=5,
                options="-c statement_timeout=3000",
            )
        try:
            archive = (
                FileRetailBatchArchive(self.retail_artifacts_path) if write else None
            )
            return RetailTimeSeriesService(
                PostgresRetailStore(
                    connection, archive, schema=RETAIL_SCHEMA, clock=self.clock
                ),
                source,
                allow_source=source is not None,
                clock=self.clock,
            )
        except BaseException:
            connection.close()
            raise

    def load_legacy_key(self, environment_id, package_id, source_id):
        if (
            source_id == "Census"
            and environment_id == self.settings.environment_id
            and package_id == "macro"
        ):
            return self._secret("census", "api_key")
        return super().load_legacy_key(environment_id, package_id, source_id)

    def employment_read_service(self):
        return self.read_service(profile=EMPLOYMENT_PROFILE)

    def employment_query_service(self, *, credential_loader):
        return self.query_service(
            credential_loader=credential_loader, profile=EMPLOYMENT_PROFILE
        )

    def employment_write_service(self, *, credential_loader):
        return self.write_service(
            credential_loader=credential_loader, profile=EMPLOYMENT_PROFILE
        )

    def employment_worker_service(self):
        host = self.employment_timeseries
        capabilities = host.capabilities(host.source())
        if not capabilities["collect"]:
            raise WebError(capabilities["reason"], 403)
        return host.write_factory(credential_loader=host.effective_credential)

    def pce_worker_service(self):
        host = self.pce_timeseries
        capabilities = host.capabilities(host.source())
        if not capabilities["collect"]:
            raise WebError(capabilities["reason"], 403)
        return host.write_factory(credential_loader=host.effective_credential)

    def credential_states(self, environment_id, package_id, record):
        sources = self.packages.catalog.credential_sources(package_id)
        return self.scoped_credential_states(
            environment_id, package_id, record, source_ids=sources
        )

    def scoped_credential_states(
        self, environment_id, package_id, record, *, source_ids
    ):
        states = []
        for source in sorted(source_ids):
            value = "unknown"
            if source in {"BEA", "BLS", "Census", "FRED"}:
                try:
                    value = (
                        "configured"
                        if self.load_legacy_key(environment_id, package_id, source)
                        is not None
                        else "missing"
                    )
                except PackageError:
                    pass
            states.append(CredentialState(source_id=source, credential_state=value))
        return states

    def consumption_read_service(self):
        return self.read_service(profile=CONSUMPTION_PROFILE)

    def consumption_query_service(self, *, credential_loader):
        service = self.consumption_read_service()
        try:
            service.source = build_source(
                credential_loader=credential_loader,
                profile=CONSUMPTION_PROFILE,
                request_control=self.bea_request_control,
                max_transport_attempts=1,
            )
            return service
        except BaseException:
            service.close()
            raise

    def consumption_write_service(self, *, credential_loader):
        config = self.settings.config_directory
        if load_pg_config(config / "runtime.toml") != self.profile:
            raise WebError("postgres_profile_changed", 503)
        return build_service(
            runtime_path=config / "runtime.toml",
            secret_path=config / "secret.toml",
            credential_loader=credential_loader,
            profile=CONSUMPTION_PROFILE,
            request_control=self.bea_request_control,
            max_transport_attempts=1,
        )

    def consumption_worker_service(self):
        host = self.consumption_timeseries
        capabilities = host.capabilities(host.source())
        if not capabilities["collect"]:
            raise WebError(capabilities["reason"], 403)
        return host.write_factory(credential_loader=host.effective_credential)

    def pce_read_service(self):
        return self.read_service(profile=PCE_PROFILE)

    def pce_query_service(self, *, credential_loader):
        service = self.pce_read_service()
        try:
            service.source = build_source(
                credential_loader=credential_loader,
                max_transport_attempts=1,
                profile=PCE_PROFILE,
                request_control=self.bea_request_control,
            )
            return service
        except BaseException:
            service.close()
            raise

    def pce_write_service(self, *, credential_loader):
        config = self.settings.config_directory
        if load_pg_config(config / "runtime.toml") != self.profile:
            raise WebError("postgres_profile_changed", 503)
        return build_service(
            runtime_path=config / "runtime.toml",
            secret_path=config / "secret.toml",
            registered=True,
            credential_loader=credential_loader,
            max_transport_attempts=1,
            profile=PCE_PROFILE,
            request_control=self.bea_request_control,
        )

    def worker_service(self):
        capabilities = self.timeseries.capabilities(self.timeseries.source())
        if not capabilities["collect"]:
            raise WebError(capabilities["reason"], 403)
        return self.timeseries.write_factory(
            credential_loader=self.timeseries.effective_credential
        )

    def ppi_worker_service(self):
        host = self.ppi_timeseries
        capabilities = host.capabilities(host.source())
        if not capabilities["collect"]:
            raise WebError(capabilities["reason"], 403)
        return host.write_factory(credential_loader=host.effective_credential)

    def ppi_query_service(self, *, credential_loader):
        return self.query_service(
            credential_loader=credential_loader, profile=PPI_PROFILE
        )

    def ppi_write_service(self, *, credential_loader):
        return self.write_service(
            credential_loader=credential_loader, profile=PPI_PROFILE
        )

    def ppi_read_service(self):
        return self.read_service(profile=PPI_PROFILE)

    def query_service(self, *, credential_loader, profile=CPI_PROFILE):
        service = (
            self.read_service()
            if profile is CPI_PROFILE
            else self.read_service(profile=profile)
        )
        try:
            service.source = build_source(
                credential_loader=credential_loader,
                request_control=self.request_control,
                max_transport_attempts=1,
                profile=profile,
            )
            return service
        except BaseException:
            service.close()
            raise

    def write_service(self, *, credential_loader, profile=CPI_PROFILE):
        config = self.settings.config_directory
        if load_pg_config(config / "runtime.toml") != self.profile:
            raise WebError("postgres_profile_changed", 503)
        # Same core factory/configuration as CLI, but BLS authentication is
        # exclusively the effective PackageHost key. A replacement reference
        # takes precedence over legacy secret.toml and failure never falls back.
        return build_service(
            runtime_path=config / "runtime.toml",
            secret_path=config / "secret.toml",
            registered=True,
            credential_loader=credential_loader,
            request_control=self.request_control,
            max_transport_attempts=1,
            profile=profile,
        )

    def read_service(self, *, profile=CPI_PROFILE):
        # Dedicated connection with server-enforced session READ ONLY. Never call
        # build_service: GET does not require Garage/BLS credentials or clients.
        connection = self.connect(
            **self.profile.connection_kwargs(),
            autocommit=True,
            connect_timeout=5,
            options="-c statement_timeout=3000",
        )
        try:
            connection.execute("SET TIME ZONE 'UTC'")
            return CpiTimeSeriesService(
                PostgresTimeSeriesStore(connection, None, profile=profile),
                LocalOnlySource(),
                profile=profile,
            )
        except BaseException:
            connection.close()
            raise

    def _history(self, capability, definition, at):
        if capability == "financial_daily":
            options = getattr(self, "financial_options", None)
            holding = "unknown"
            if options and options.allow_read:
                connection = None
                try:
                    from market_data_foundation.historical import financial_daily_store

                    schema = financial_daily_store.SCHEMA

                    connection = self.connect(
                        **self.profile.connection_kwargs(),
                        autocommit=True,
                        connect_timeout=5,
                        options="-c statement_timeout=3000",
                    )
                    row = connection.execute(
                        f"SELECT EXISTS(SELECT 1 FROM {schema}.revisions "
                        "WHERE available_at IS NOT NULL AND available_at <= %s)",
                        (at,),
                    ).fetchone()
                    holding = "present" if row[0] else "absent"
                except Exception:
                    pass
                finally:
                    if connection:
                        connection.close()
            return HistoryEvidence(
                manual_registered=bool(options and options.allow_read),
                definition_ref=definition,
                holding=holding,
                metadata_view_permission="allowed"
                if options and options.allow_read
                else "unknown",
                read_permission="allowed"
                if options and options.allow_read
                else "unknown",
                publication_readable=True if holding == "present" else None,
            )
        if capability not in {
            "cpi",
            "ppi",
            "pce",
            "employment",
            "retail",
            "consumption",
            "gdp",
            "claims",
            "jolts",
        } or (capability == "pce" and self.pce_timeseries is None):
            return super()._history(capability, definition, at)
        access = self.accesses[capability]
        host = {
            "cpi": self.timeseries,
            "ppi": self.ppi_timeseries,
            "pce": self.pce_timeseries,
            "employment": self.employment_timeseries,
            "retail": self.retail_timeseries,
            "consumption": self.consumption_timeseries,
            "gdp": self.gdp_timeseries,
            "claims": self.claims_timeseries,
            "jolts": self.jolts_timeseries,
        }[capability]
        holding = "unknown"
        if access.metadata_permission == "allowed" and host is not None:
            try:
                with host.service() as service:
                    holding = (
                        "present" if service.status()["observation_count"] else "absent"
                    )
            except Exception:
                pass
        if capability == "pce" and holding != "present":
            legacy = super()._history(capability, definition, at)
            if legacy.holding == "present":
                return legacy
        return HistoryEvidence(
            definition_ref=definition,
            holding=holding,
            metadata_view_permission=access.metadata_permission,
            read_permission=access.read_permission,
        )

    def history(self, environment_id, package_id, record):
        result = super().history(environment_id, package_id, record)
        if package_id != "macro":
            return result
        capabilities = {
            (c.definition_ref.identity, c.definition_ref.revision): c
            for c in self.packages.catalog.package("macro").capabilities
            if c.capability_id
            in {
                "ppi",
                "employment",
                "retail",
                "consumption",
                "gdp",
                "claims",
                "jolts",
                "financial_daily",
            }
        }
        return result.model_copy(
            update={
                "history": [
                    self._history(
                        capabilities[
                            (e.definition_ref.identity, e.definition_ref.revision)
                        ].capability_id,
                        e.definition_ref,
                        self.clock(),
                    )
                    if (e.definition_ref.identity, e.definition_ref.revision)
                    in capabilities
                    else e
                    for e in result.history
                ]
            }
        )


def create_timeseries_preview(
    settings,
    *,
    enabled=False,
    ui_directory=None,
    worker_control=None,
    financial_options=None,
    **options,
):
    if enabled is not True:
        raise ValueError("cpi_timeseries_web_opt_in_required")
    runtime = TimeSeriesRuntime(settings, **options)
    runtime.financial_options = financial_options
    runtime.packages.financial_options = financial_options
    # No legacy CPI scope is installed. Its old endpoint remains unconfigured;
    # new data never traverses the old canonical schema or publication model.
    app = create_app(package_host=runtime.packages)
    add_read_routes(app, runtime.scopes["pce"], runtime.clock, write_eligible=False)

    def prune(application):
        router = getattr(application, "router", None)
        if router is None:
            return
        router.routes[:] = [
            r
            for r in router.routes
            if getattr(r, "methods", None) is None
            or r.methods <= {"GET", "HEAD"}
            or (
                settings.package_config_save
                and (
                    (r.methods == {"PUT"} and r.path == "/api/r5/packages/{package_id}")
                    or (
                        r.methods == {"POST"}
                        and r.path
                        == "/api/r5/packages/{package_id}/sources/{source_id}/verify"
                    )
                )
            )
        ]
        for route in router.routes:
            prune(getattr(route, "app", None))

    prune(app)
    app.include_router(timeseries_router(runtime.timeseries))
    app.include_router(timeseries_router(runtime.ppi_timeseries, profile=PPI_PROFILE))
    app.include_router(timeseries_router(runtime.pce_timeseries, profile=PCE_PROFILE))
    app.include_router(
        timeseries_router(runtime.consumption_timeseries, profile=CONSUMPTION_PROFILE)
    )
    app.include_router(
        timeseries_router(runtime.employment_timeseries, profile=EMPLOYMENT_PROFILE)
    )
    from market_data_foundation.web.retail_timeseries import admitted_retail_job
    from market_data_foundation.web.retail_timeseries import (
        timeseries_router as retail_router,
    )

    app.include_router(
        retail_router(
            runtime.retail_timeseries,
            authorize=lambda request: admitted(request, origin=CANDIDATE_ORIGIN),
        )
    )
    from market_data_foundation.web.gdp_api import timeseries_router as gdp_router

    app.include_router(gdp_router(runtime.gdp_timeseries))
    from market_data_foundation.web.m3_query_api import (
        timeseries_router as m3_query_router,
    )

    app.include_router(m3_query_router(runtime.m3_query_host))
    from market_data_foundation.web.claims_api import timeseries_router as claims_router

    app.include_router(claims_router(runtime.claims_timeseries))
    app.include_router(
        timeseries_router(runtime.jolts_timeseries, profile=JOLTS_PROFILE)
    )
    from market_data_foundation.web.worker_control import (
        admitted_worker,
        install_worker_control,
    )

    install_worker_control(app, runtime, worker_control)
    from market_data_foundation.web.financial_daily_api import financial_router

    app.include_router(financial_router(financial_options))
    app.state.timeseries_runtime = runtime

    @app.middleware("http")
    async def boundary(request, call_next):
        config_put = settings.package_config_save and (
            admitted_package_save(request) or admitted_package_verify(request)
        )
        prefixes = (
            *PREFIXES,
            "/api/r5/pce/timeseries",
            "/api/r5/retail/timeseries",
            "/api/r5/gdp/timeseries",
            "/api/r5/claims/timeseries",
            "/api/r5/jolts/timeseries",
            "/api/r5/m3/timeseries",
            "/api/r5/financial/timeseries",
        )
        job_post = admitted_worker(request) or any(
            admitted_job(request, prefix=prefix) for prefix in prefixes
        )
        from market_data_foundation.web.financial_daily_api import (
            admitted_financial_job,
        )

        job_post = job_post or admitted_financial_job(request)
        if request.url.path == "/api/r5/m3/timeseries/jobs/resume":
            job_post = admitted_job(
                Request(dict(request.scope, path="/api/r5/m3/timeseries/jobs")),
                prefix="/api/r5/m3/timeseries",
            )
        if request.url.path == "/api/r5/retail/timeseries/jobs/resume":
            job_post = admitted_retail_job(request)
        strategy_put = (
            request.method == "POST"
            and request.url.path == "/api/r5/retail/timeseries/strategy"
            and admitted_strategy(
                Request(dict(request.scope, method="PUT")),
                prefix="/api/r5/retail/timeseries",
            )
        ) or any(admitted_strategy(request, prefix=prefix) for prefix in prefixes)
        error = None
        if request.method not in {"GET", "HEAD"} and not (
            config_put or job_post or strategy_put
        ):
            error = (405, "method_not_allowed")
        elif not (config_put or job_post or strategy_put) and not admitted(
            request, origin=CANDIDATE_ORIGIN
        ):
            error = (403, "operator_not_authorized")
        else:
            capability = (
                "jolts"
                if request.url.path.startswith("/api/r5/jolts/")
                else "claims"
                if request.url.path.startswith("/api/r5/claims/")
                else "gdp"
                if request.url.path.startswith("/api/r5/gdp/")
                else "consumption"
                if request.url.path.startswith("/api/r5/consumption/")
                else "retail"
                if request.url.path.startswith("/api/r5/retail/")
                else "pce"
                if request.url.path.startswith("/api/r5/pce/")
                else "ppi"
                if request.url.path.startswith("/api/r5/ppi/")
                else "cpi"
                if request.url.path.startswith("/api/r5/cpi")
                else (
                    "pce"
                    if request.url.path in {"/api/pce/assets", "/api/pce/read"}
                    else None
                )
            )
            if capability:
                access = runtime.accesses[capability]
                if access.metadata_permission != "allowed":
                    error = (403, "metadata_permission_" + access.metadata_permission)
                elif (
                    request.url.path.endswith(("/read", "/query"))
                    and access.read_permission != "allowed"
                ):
                    error = (403, "read_permission_" + access.read_permission)
            if request.method in {"GET", "HEAD"} and request.url.path.startswith(
                prefixes
            ):
                if (
                    request.headers.get("content-length", "0") != "0"
                    or "transfer-encoding" in request.headers
                ):
                    error = (400, "get_body_not_supported")
        if error:
            response = JSONResponse(
                {"status": "NOT_AVAILABLE", "classification": error[1]}, error[0]
            )
        else:
            try:
                response = await call_next(request)
            except Exception:
                response = JSONResponse(
                    {
                        "status": "UNKNOWN",
                        "classification": "timeseries_host_unavailable",
                    },
                    503,
                )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    if ui_directory is not None:
        ui = Path(ui_directory)
        app.mount("/assets", StaticFiles(directory=ui / "assets"))
        app.get("/")(lambda: RedirectResponse("/pce"))
        app.get("/pce")(lambda: FileResponse(ui / "index.html"))
        app.get("/mdf-data-hub.png")(lambda: FileResponse(ui / "mdf-data-hub.png"))
    return app
