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
                offset != 2,
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
        max_members=20,
        max_days=370,
        max_grid_points=7400,
        max_shards=500,
        max_publication_units=500,
        max_requests=1000,
        max_rows=10000,
        max_response_bytes=1000,
        max_material_bytes=1000000,
        max_versions=500,
        max_publications=500,
        max_connections=500,
        max_probes=500,
        max_concurrency=1,
        requests_per_second=1,
        timeout_seconds=5,
        wall_seconds=1000,
        recovery_requests=100,
    )


def job(content: UniverseRevision | None = None) -> JobSpec:
    content = content or universe()
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
                5,
                1,
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
        "synthetic-occurrence-1",
        budget(),
    )
