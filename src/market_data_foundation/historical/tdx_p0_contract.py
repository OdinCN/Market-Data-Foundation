"One declared P0 native daily capability for catalog, API and workbench."

from dataclasses import replace

from market_data_foundation.canonical.model import (
    EvidenceRequirement,
    Grade,
    OutputMapping,
)
from market_data_foundation.historical.daily_bars import (
    RAW_DAILY_BARS,
    TDX_R1_DAILY_BINDING,
    TDX_R1_DAILY_SOURCE,
)

DATASET_ID = "mdf.market.tdx.stock.daily.native"
PREFIX = "/api/r5/tdx/daily"
ORIGIN = "http://192.168.10.20:18766"
DEFINITION = replace(
    RAW_DAILY_BARS,
    identity=DATASET_ID,
    revision="r1",
    domain_contract=(
        "TDX stock Type9 native OHLCV/amount observations; native_project"
        "ion has no unadjusted/return qualification"
    ),
    purpose="SH/SZ/BJ all-A daily observations with explicit coverage gaps",
    unit_semantics="signed integer 0.001 CNY prices and amount; volume in hands",
    temporal_semantics=(
        "trading day, source event time unknown, actual receipt and MDF v"
        "ersion availability are distinct"
    ),
)
SOURCE = replace(
    TDX_R1_DAILY_SOURCE,
    identity="mdf.source.tdx-v100.stock.daily",
    contract_revision="p0-r1",
    product="injoyai/tdx Go SDK",
    projection_revision="go-tdx-v1.0.0-native-int-v1",
    variant="stock-type9-native",
    build_revision="2a9000681f5a9896ea98e4de8bc4904f1dce78ba+mdf-wait-preregister-r1",
    acquisition_mode="explicit_dev_after_close",
    completeness_contract=(
        "SH/SZ/BJ provider roster plus digest-bound exchange lifecycle originals; "
        "historical enumeration bounded by dated evidence and qualified per date"
    ),
    pagination_contract=(
        "MDF owns <=800-row uint16 pages with overlap, duplicate/no-progr"
        "ess detection and receipt-based resume"
    ),
    freshness_contract=(
        "explicit historical init and after-close overlap update; observe"
        "d execution only"
    ),
    evidence_capability=frozenset({Grade.ORIGINAL}),
)
BINDING = replace(
    TDX_R1_DAILY_BINDING,
    identity="mdf.binding.tdx-v100.stock.daily.native",
    revision="p0-r1",
    source=SOURCE.key,
    mappings=(
        OutputMapping(
            "daily_native",
            DEFINITION.key,
            "p0-native-r1",
            "received-available-r1",
            DEFINITION.scope,
            EvidenceRequirement(frozenset({Grade.ORIGINAL}), True, True),
        ),
    ),
    compatibility_policy=(
        "pinned Go SDK, native Type9 stock units, retained per-request wi"
        "re and projection"
    ),
    limitations=(
        "native price basis; returns NOT_QUALIFIED",
        "exchange-known terminated members included; "
        "historical enumeration remains bounded",
        "empty responses and zero volume never establish suspension",
        "BJ lifecycle and historical code lineage may remain unknown",
        "no silent provider fallback",
    ),
)
HELP = dict(
    dataset_id=DATASET_ID,
    revision="r1",
    markets={"0": "SZ", "1": "SH", "2": "BJ"},
    security_type="A-share stock",
    period="1d",
    basis="native_projection",
    units=dict(price="0.001 CNY", amount="0.001 CNY", volume="hand"),
    endpoints={
        name: PREFIX + suffix
        for name, suffix in dict(
            status="/status",
            summary="/summary",
            universe="/universe",
            query="/query",
            coverage="/coverage",
            jobs="/jobs",
            resume="/jobs/resume",
            strategy="/strategy",
            help="/help",
        ).items()
    },
    examples=dict(
        history=PREFIX
        + (
            "/query?market=1&instrument=600519&start=1990-01-01&end=2026-11-1"
            "2&local_only=true&limit=100"
        ),
        cross_section=PREFIX + "/query?day=2026-11-12&local_only=true&limit=100",
        coverage=PREFIX + "/coverage?day=2026-11-12",
        job={
            "request_id": "canonical UUID",
            "action": "universe | init | update",
            "start": "YYYY-MM-DD (init default 1990-01-01)",
            "end": "YYYY-MM-DD (latest after-close day default)",
            "markets": [0, 1, 2],
        },
        resume={"request_id": "existing incomplete job UUID"},
        strategy={"enabled": True},
    ),
    notes=[
        (
            "Queries are local only and perform zero source requests; local_o"
            "nly=false is rejected."
        ),
        (
            "as_of filters MDF actual version availability, never inferred hi"
            "storical vendor publication."
        ),
        "Source event timestamp is unknown; SDK 15:00 is a date projection.",
        (
            "No adjustment or suspension inferred from bars; exchange originals "
            "supply known lifecycle and calendar evidence."
        ),
        (
            "Initialization defaults to source exhaustion or requested start;"
            " page offset overflow is a recoverable classified failure."
        ),
        (
            "Same request_id replays; resume retries incomplete steps and an "
            "uncertain commit replays its immutable batch."
        ),
        (
            "Strategy runs after 15:10 Asia/Shanghai on weekday candidates; "
            "coverage binds retained annual exchange calendars where available."
        ),
        (
            "Refresh universe, then update the last 14 calendar days; initial"
            "ization is separately submitted."
        ),
        "Failed directory refresh cannot replace the last complete roster.",
        "Exchange lifecycle import requires an explicit DEV manifest "
        "with original hashes; a directory refresh preserves known retired "
        "members and never extends evidence validity.",
        "COMPLETE collection means source exhaustion or requested range reached; "
        "coverage complete requires dated enumeration, calendar and "
        "all bar/no-trade obligations.",
    ],
)
