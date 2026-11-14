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
    field_semantics={
        "open/high/low/close": "Integer milliyuan per share; CNY = raw / 1000",
        "amount": "SDK integer milliyuan; display CNY = raw / 1000",
        "volume": "SDK integer hands; hands * 100 is a 100-share-step conversion",
        "Last": "SDK page decoder context; page head may be zero; excluded from facts",
        "Order/UpCount/DownCount": "retained SDK projection only; excluded from facts",
        "returns/adjustments": "no return, adjusted price or factor in daily facts",
    },
    base_roster="/api/r5/tdx/roster",
    query_parameters={
        "day": "YYYY-MM-DD cross-section; mutually exclusive with start/end",
        "start/end": "inclusive; 1990-01-01 <= start <= end <= 2100-12-31",
        "market": "optional integer 0=SZ, 1=SH, 2=BJ",
        "instrument": "optional six ASCII digits; requires market",
        "as_of": "aware ISO8601 with microseconds; MDF availability",
        "local_only": "true only; default true; GET never acquires upstream",
        "basis": "native_projection only; default native_projection",
        "period/security_type": "1d/stock only",
        "order": "asc or desc; default asc, history form explicitly desc",
        "offset/limit": "nonnegative offset; 1 <= limit <= 10000; defaults 0/100",
    },
    response_fields={
        "rows": "market/instrument/trading_day, integer OHLCV/amount, source/batch_id",
        "received_at/available_at": "actual receipt / confirmed local publication",
        "total/next_offset": "count / next cursor or null; not universe coverage",
        "snapshot_id/membership": "base-roster identity / history qualification",
        "provider_requests": "0 for all local GET reads",
        "source_event_time": "null; SDK date does not establish absolute source time",
    },
    errors={
        "400": [
            "tdx_query_parameters",
            "tdx_query_direction",
            "tdx_query_contract",
            "tdx_query_page",
            "tdx_query_cutoff",
            "tdx_invalid_parameter",
            "tdx_missing_parameter",
        ],
        "403": ["operator_not_authorized", "tdx_capability_not_enabled"],
        "404": ["job_not_found"],
        "409": ["job_running"],
        "503": ["classified source or storage unavailability"],
        "job_PARTIAL": [
            "tdx_universe_required",
            "tdx_roster_stale",
            "commit_outcome_unknown",
            "tdx_page_offset_exhausted",
        ],
    },
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
            "2&local_only=true&order=desc&limit=100"
        ),
        cross_section=PREFIX + "/query?day=2026-11-12&local_only=true&limit=100",
        coverage=PREFIX + "/coverage?day=2026-11-12",
        job={
            "request_id": "8c9604c4-3281-4f14-9f08-939a7a362d11",
            "action": "universe | init | update",
            "start": "YYYY-MM-DD (init default 1990-01-01)",
            "end": "YYYY-MM-DD (latest after-close day default)",
            "markets": [0, 1, 2],
        },
        resume={"request_id": "existing incomplete job UUID"},
        strategy={"enabled": True},
    ),
    notes=[
        "历史日线可用 order=desc 按交易日倒序读取；省略时使用 asc。",
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
            "Strategy: one 15:35 Asia/Shanghai collection per trading day "
            "confirmed by dated exchange calendars. A restart catches up missed "
            "dates using the existing durable universe and update identities."
        ),
        (
            "First refresh the universe. Each daily update covers D minus 14 "
            "calendar days through D inclusive. The same response supplies new "
            "bars and historical corrections at the new publication time. "
            "Retained qualified receipts can be explicitly admitted in DEV via "
            "mdf_tdx_daily_receive.py using normal jobs and publication clocks."
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
