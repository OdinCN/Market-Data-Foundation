"""Entirely invented identities/dates; not retained or live source evidence."""

from datetime import UTC, date, datetime, timedelta

from market_data_foundation.canonical.model import Key, Kind
from market_data_foundation.domain_contracts.market_planning import (
    DAILY_DEFINITIONS,
    JobMode,
    JobSpec,
    PagePlan,
    PlanningBudget,
    SourceRoute,
)
from market_data_foundation.domain_contracts.market_universe import (
    AssetClass,
    CalendarDay,
    CalendarRevision,
    DateWindow,
    EvidenceMode,
    InstrumentIdentity,
    Lifecycle,
    MembershipMode,
    Provenance,
    Qualification,
    QualificationStatus,
    SourceMapping,
    UniverseMember,
    UniverseRevision,
)
from market_data_foundation.qualification.contracts import Capability, Fixture

START = date(2024, 1, 29)
WINDOW = DateWindow(START, date(2024, 2, 7))
KNOWN = datetime(2024, 2, 8, 1, tzinfo=UTC)
SOURCE = Key(Kind.SOURCE, "synthetic.daily", "v1")
BINDING = Key(Kind.BINDING, "synthetic.daily", "v1")
MATERIAL = Key(Kind.MATERIAL, "synthetic-material")
FIXTURE = Fixture(
    fixture_id="r5-i6-d1-synthetic-only",
    capability=Capability.BARS_DAILY,
    symbols=("invented-stable-id",),
    start=WINDOW.start,
    end=WINDOW.end_exclusive - timedelta(days=1),
    page_size=5,
    semantics={"evidence_mode": "synthetic", "full_market": "UNKNOWN"},
)


def provenance(capability: Capability) -> Provenance:
    return Provenance(
        capability,
        Key(Kind.SOURCE, f"synthetic.{capability}", "v1"),
        Key(Kind.BINDING, f"synthetic.{capability}", "v1"),
        "v1",
        (MATERIAL,),
        KNOWN,
        KNOWN,
        EvidenceMode.SYNTHETIC,
    )


def universe(category: AssetClass = AssetClass.A_SHARE) -> UniverseRevision:
    identity = InstrumentIdentity("synthetic.stable", "invented-stable-id")
    member = UniverseMember(
        identity,
        category,
        "SYNTHETIC_EXCHANGE",
        "invented-code",
        "synthetic.display",
        (DateWindow(START + timedelta(days=1), START + timedelta(days=8)),),
        Lifecycle(
            WINDOW,
            START + timedelta(days=1),
            START + timedelta(days=8),
            ("synthetic-lifecycle",),
        ),
    )
    calendar = CalendarRevision(
        "synthetic-calendar",
        "d1.v1",
        member.market,
        "Asia/Shanghai",
        WINDOW,
        KNOWN,
        tuple(
            CalendarDay(
                START + timedelta(days=offset),
                offset in (1, 6),
                "synthetic-session",
                "synthetic-calendar-evidence",
            )
            for offset in range(9)
        ),
        provenance(Capability.TRADING_CALENDAR),
        QualificationStatus.QUALIFIED,
    )
    return UniverseRevision(
        "synthetic-named-bounded-universe",
        "d1.v1",
        WINDOW,
        WINDOW,
        KNOWN,
        MembershipMode.HISTORICAL,
        "only invented member",
        "all other identities excluded",
        "synthetic bounded authority",
        ("synthetic-enumeration-termination",),
        (member,),
        (calendar,),
        (
            SourceMapping(
                identity,
                SOURCE,
                BINDING,
                "v1",
                "synthetic.request.exchange",
                "invented-request-code",
                WINDOW,
                ("synthetic-explicit-mapping",),
            ),
        ),
        provenance(Capability.SECURITY_UNIVERSE),
        tuple(
            (item, Qualification(*(QualificationStatus.QUALIFIED for _ in range(8))))
            for item in AssetClass
        ),
    )


def budget() -> PlanningBudget:
    return PlanningBudget(
        max_members=1,
        max_days=10,
        max_grid_points=7400,
        max_shards=500,
        max_publication_units=500,
        max_requests=20,
        max_rows=10000,
        max_response_bytes=1024,
        max_material_bytes=32768,
        max_versions=500,
        max_publications=500,
        max_connections=500,
        max_probes=500,
        max_concurrency=1,
        requests_per_second=1,
        timeout_seconds=5,
        wall_seconds=60,
        recovery_requests=100,
    )


def job(occurrence: str) -> JobSpec:
    content = universe()
    categories = {member.category for member in content.members}
    routes = tuple(
        SourceRoute(
            next(item for item in DAILY_DEFINITIONS if item.category == category),
            SOURCE,
            BINDING,
            "v1",
            "synthetic-implementation-v1",
            "synthetic-date-window-endpoint",
            (("fixture", FIXTURE.fixture_id),),
            5,
            PagePlan(
                0,
                1,
                5,
                "ascending",
                "synthetic-frozen-cutoff-and-anchor",
                "synthetic-terminal-page-only",
            ),
            QualificationStatus.QUALIFIED,
            ("synthetic-category-source-qualification",),
        )
        for category in sorted(categories)
    )
    return JobSpec(
        content,
        WINDOW,
        WINDOW.end_exclusive - timedelta(days=1),
        routes,
        "synthetic-storage-policy-v1",
        "offline-planning-only",
        JobMode.INITIALIZE,
        occurrence,
        budget(),
    )


def decoder_for(plan):
    from market_data_foundation.canonical.market_material import (
        FieldSemantics,
        NormalizationContract,
        SyntheticDailyDecoder,
    )

    route = plan.spec.routes[0]
    return SyntheticDailyDecoder(
        NormalizationContract(
            route.source,
            route.binding,
            route.mapping_revision,
            route.daily,
            Capability.BARS_DAILY,
            "synthetic.daily",
            "synthetic-upstream-r1",
            "synthetic-json-r2",
            (("period", "daily"), ("kind", "invented-stock")),
            tuple(
                FieldSemantics(
                    name,
                    "decimal-text",
                    unit,
                    name,
                    unit,
                    "EXPLICIT",
                    "synthetic-field-fixture",
                    "identity.synthetic.r1",
                )
                for name, unit in (
                    ("open", "CNY"),
                    ("high", "CNY"),
                    ("low", "CNY"),
                    ("close", "CNY"),
                    ("volume", "shares"),
                    ("amount", "CNY"),
                )
            ),
        )
    )


class SyntheticSource:
    """Invented fixed rows. No network API or live fallback exists."""

    def __init__(self, plan, decoder):
        from market_data_foundation.historical.market_daily_source import OfflineBinding

        self.plan, self.contract = plan, decoder.contract
        route = plan.spec.routes[0]
        self.binding = OfflineBinding(
            EvidenceMode.SYNTHETIC,
            route.source,
            route.binding,
            route.mapping_revision,
            route.implementation_identity,
            route.endpoint_contract,
            self.contract.producer_operation,
            self.contract.upstream_revision,
            self.contract.decoder_revision,
        )
        self.calls = []

    def fetch(self, request, charge):
        from market_data_foundation.canonical.market_material import DailyBar
        from market_data_foundation.domain_contracts.market_coverage import (
            expected_state,
        )
        from market_data_foundation.domain_contracts.market_universe import (
            canonical_bytes,
        )
        from market_data_foundation.historical.market_daily_source import (
            NativeField,
            NativeRow,
            Page,
        )

        self.calls.append(request.identity)
        points = tuple(
            p
            for p in request.shard.request_superset
            if expected_state(self.plan, p) is None
        )
        chosen = points[request.offset : request.offset + request.count]
        assert len(chosen) <= 1
        raw = (
            canonical_bytes(DailyBar(chosen[0].day, "10", "12", "9", "11", "0", "0"))
            if chosen
            else b'{"evidence":"synthetic explicit empty response"}'
        )
        return Page(
            request.identity,
            raw,
            tuple(
                NativeRow(
                    p.day.isoformat(),
                    "row:" + p.day.isoformat(),
                    tuple(
                        NativeField(
                            k,
                            "decimal-text",
                            v,
                            "synthetic-field",
                            "identity.synthetic",
                        )
                        for k, v in (
                            ("open", "10"),
                            ("high", "12"),
                            ("low", "9"),
                            ("close", "11"),
                            ("volume", "0"),
                            ("amount", "0"),
                        )
                    ),
                )
                for p in chosen
            ),
            "synthetic-frozen-snapshot",
            request.offset + request.count >= len(points),
            "synthetic-bounded-exhaustion",
            self.contract.producer_operation,
            self.contract.upstream_revision,
            self.contract.decoder_revision,
            KNOWN + timedelta(seconds=20),
            KNOWN + timedelta(seconds=21),
            KNOWN + timedelta(seconds=22),
        )

    def lookup_original(self, request):
        raise AssertionError("synthetic_reacquisition_forbidden")


def run_job(directory, occurrence):
    from market_data_foundation.domain_contracts.market_planning import plan_job
    from market_data_foundation.historical.market_daily_journal import Journal
    from market_data_foundation.historical.market_daily_worker import DailyRunner

    plan = plan_job(job(occurrence))
    decoder = decoder_for(plan)
    source = SyntheticSource(plan, decoder)
    clock = [int((KNOWN + timedelta(seconds=10)).timestamp()) * 10**9]

    def sleep(seconds):
        clock[0] += int(seconds * 10**9)

    journal = Journal(directory, clock_ns=lambda: clock[0])
    worker = DailyRunner(plan, journal, source, sleep=sleep)
    report = worker.run()
    assert report.state == "COMPLETE"
    assert len(plan.units) == 2 and len(source.calls) <= 8
    reopened = DailyRunner.restore(plan.spec.identity, journal, source, sleep=sleep)
    assert reopened.run().state == "COMPLETE"
    assert len(source.calls) == len(set(source.calls))
    return reopened, decoder


def layout_for(worker, request):
    from market_data_foundation.canonical.market_material import RowLocator, byte_digest
    from market_data_foundation.integration.market_vertical import (
        IntakeLayout,
        response_identity,
    )

    page = worker.journal.read_material(worker.journal.receipt(request))
    return IntakeLayout(
        tuple(
            RowLocator(
                row.locator,
                response_identity(page),
                0,
                len(page.raw),
                byte_digest(page.raw),
            )
            for row in page.rows
        ),
        worker.source.contract.request_fields,
        FIXTURE.fixture_id,
        KNOWN + timedelta(seconds=10),
        "synthetic-original-request-clock",
    )


def version_for(worker, decoder, store, materials, unit):
    from market_data_foundation.domain_contracts.market_coverage import (
        CoverageClass,
        PointEvidence,
        expected_state,
    )
    from market_data_foundation.integration.market_vertical import prepare_version

    receipts = tuple(
        r
        for r in materials
        if next(s for s in worker.plan.shards if s.identity == r.shard).window.overlaps(
            unit.window
        )
    )
    evidence = []
    for logical in unit.expand():
        point = logical.point
        found = next(
            (
                (r, row)
                for r in receipts
                for row in r.rows
                if row.identity == "row:" + point.day.isoformat()
            ),
            None,
        )
        if found:
            receipt, row = found
            evidence.append(
                PointEvidence(
                    point,
                    CoverageClass.OBSERVED_VALID,
                    ("synthetic-explicit-row",),
                    (receipt.shard,),
                    (receipt.attempt,),
                    (receipt.request,),
                    receipt.key,
                    row.identity,
                )
            )
        else:
            classification = expected_state(worker.plan, point)
            assert classification is not None
            evidence.append(
                PointEvidence(
                    point,
                    classification,
                    ("synthetic-frozen-calendar-lifecycle",),
                )
            )
    return prepare_version(
        Key(Kind.VERSION, "i1-i3:" + worker.plan.spec.identity + ":" + unit.identity),
        unit,
        receipts,
        tuple(evidence),
        decoder,
        store,
        KNOWN + timedelta(seconds=30),
        KNOWN + timedelta(seconds=40),
        plan=worker.plan,
    )
