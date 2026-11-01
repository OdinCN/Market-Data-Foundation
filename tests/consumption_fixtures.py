"""Synthetic native-series fixtures. No live calls, credentials or runtime stores."""

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import FastAPI

from market_data_foundation.historical.bls_timeseries_profile import (
    CONSUMPTION_PROFILE as PROFILE,
)
from market_data_foundation.historical.consumption_schedule import (
    ConsumptionReleaseScheduler,
)
from market_data_foundation.historical.consumption_timeseries import (
    ConsumptionTimeSeriesService,
)
from market_data_foundation.historical.cpi_timeseries import month_range
from market_data_foundation.historical.cpi_timeseries_source import (
    SourceBatch,
    SourcePoint,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
    digest,
    raw_locator,
)
from market_data_foundation.historical.macro_release_records import ReleaseRecords
from market_data_foundation.web.consumption_timeseries import ConsumptionTimeSeriesHost
from market_data_foundation.web.cpi_timeseries_api import timeseries_router
from market_data_foundation.web.package_catalog import (
    CredentialState,
    PackageCatalog,
    SaveCommand,
)
from market_data_foundation.web.package_store import LocalPackageStore
from market_data_foundation.web.packages import PackageHost

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
VALUES = dict(
    DPCERC="22452780",
    A067RC="24289049",
    A067RX="18366069",
    DPCERX="16977581",
    DGDSRX="5780732",
    DSERRX="11219342",
    A072RC="3.8",
)


class Raw:
    profile = PROFILE

    def __init__(self):
        self.objects = {}

    def put_verified(self, raw):
        self.objects[digest(raw)] = raw
        return raw_locator(digest(raw), profile=PROFILE)


class Source:
    profile = PROFILE

    def __init__(self):
        self.calls = []
        self.missing = set()
        self.nulls = set()
        self.offset = 0
        self.observed = NOW

    def fetch(self, series, start, end):
        self.calls.append((series, start, end))
        points = []
        for sid in series:
            for month in month_range(start, min(end, "2026-09")):
                if (sid, month) in self.missing:
                    continue
                value = Decimal(VALUES[sid]) + self.offset
                if month == "2026-08":
                    value = Decimal("0") if sid == "A072RC" else value - 100
                if month == "2025-09":
                    value = Decimal("-1.2") if sid == "A072RC" else value - 200
                points.append(
                    SourcePoint(
                        sid,
                        month,
                        None if (sid, month) in self.nulls else value,
                        {"synthetic": True},
                        (),
                    )
                )
        return (
            SourceBatch(
                tuple(points),
                f"synthetic-consumption-{len(self.calls)}-{self.offset}".encode(),
                {
                    "seriesid": list(series),
                    "provider": "BEA",
                    "request_count": 2,
                    "retention_class": "synthetic_fixture",
                },
                self.observed,
                2,
            ),
        )


def make_host(directory, *, preload=True):
    package_store = LocalPackageStore.initialize(
        directory / "packages", PackageCatalog()
    )
    package_store.save(
        "fixture",
        "macro",
        SaveCommand(expected_revision=None, enabled_capability_ids=["consumption"]),
    )
    packages = PackageHost(
        package_store,
        "fixture",
        credentials=lambda *_: [
            CredentialState(source_id="BEA", credential_state="configured")
        ],
        credential_loader=lambda *_: "synthetic-bea-key",
    )
    store = InMemoryTimeSeriesStore(Raw(), profile=PROFILE, clock=lambda: NOW)
    source = Source()

    def read():
        return ConsumptionTimeSeriesService(store, source)

    def bound(*, credential_loader):
        credential_loader()
        return read()

    ReleaseRecords(store).prepare()
    scheduler = ConsumptionReleaseScheduler(
        None,
        directory / "consumption-calendar.json",
        clock=lambda: NOW,
        history_start="2006-10",
        calendar_fetcher=None,
    )
    host = ConsumptionTimeSeriesHost(
        packages=packages,
        read_service_factory=read,
        write_service_factory=bound,
        query_service_factory=bound,
        allow_collect=True,
        query_allowed=True,
        scheduler=scheduler,
        job_directory=directory / "consumption-jobs",
        clock=lambda: NOW,
    )
    if preload:
        store.save_batches(source.fetch(PROFILE.series_ids, "2025-09", "2026-09"))
        source.calls.clear()
    app = FastAPI()
    app.include_router(timeseries_router(host))
    return app, host, source, store
