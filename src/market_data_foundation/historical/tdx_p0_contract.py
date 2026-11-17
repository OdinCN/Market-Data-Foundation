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
        "local_only": (
            "false by default: local first, bound TDX ephemeral supplement; "
            "true or as_of means local only"
        ),
        "snapshot_id": (
            "optional published-roster identity for cross-section pagination"
        ),
        "basis": "native_projection only; default native_projection",
        "period/security_type": "1d/stock only",
        "order": "asc or desc; default asc, history form explicitly desc",
        "offset/limit": "nonnegative offset; 1 <= limit <= 10000; defaults 0/100",
    },
    response_fields={
        "rows": "market/instrument/trading_day, integer OHLCV/amount, source/batch_id",
        "received_at/available_at": "actual receipt / confirmed local publication",
        "total/next_offset": (
            "rows for history/local mode; roster candidates for local-first "
            "cross-section; never universe completeness"
        ),
        "pagination_basis/page_end_offset": (
            "rows or roster_candidates; use returned cursor, including empty candidates"
        ),
        "snapshot_id/membership": "base-roster identity / history qualification",
        "provider_requests": (
            "actual Type9 operations sent this GET, including failed sent "
            "requests; 0 for local-only/as_of"
        ),
        "rows[].origin/persisted/bar_state/is_complete": (
            "local or upstream; ephemeral unfinished today / after-close "
            "unpersisted; no final-publication assertion"
        ),
        "rows[].observed_at/available_at": (
            "actual receipt clock / local publication; upstream available_at is null"
        ),
        "upstream/query_coverage": (
            "classified partial failures, request budget and this query page; "
            "distinct from persisted /coverage"
        ),
        "source_event_time": "null; SDK date does not establish absolute source time",
    },
    errors={
        "400": [
            "tdx_query_parameters",
            "tdx_query_direction",
            "tdx_query_contract",
            "tdx_query_page",
            "tdx_query_cutoff",
            "tdx_query_stock_scope",
            "tdx_query_snapshot",
            "tdx_query_snapshot_unavailable",
            "tdx_invalid_parameter",
            "tdx_missing_parameter",
        ],
        "403": ["operator_not_authorized", "tdx_capability_not_enabled"],
        "404": ["job_not_found"],
        "409": ["job_running"],
        "503": ["classified source or storage unavailability"],
        "200_partial": [
            "tdx_source_query_not_enabled",
            "tdx_query_roster_required",
            "tdx_query_history_instrument_required",
            "tdx_query_current_observation_unavailable",
            "tdx_query_request_budget_exceeded",
            "tdx_query_source_blocked",
            "classified upstream failure in upstream.issues",
        ],
        "job_PARTIAL": [
            "tdx_universe_required",
            "tdx_roster_stale",
            "commit_outcome_unknown",
            "tdx_page_offset_exhausted",
            "tdx_periodic_roster_job_missing",
            "tdx_periodic_roster_job_unreadable",
            "tdx_periodic_roster_origin",
            "tdx_periodic_roster_scope",
            "tdx_periodic_roster_incomplete",
            "tdx_periodic_roster_snapshot_missing",
            "tdx_periodic_roster_snapshot_changed",
            "tdx_periodic_roster_binding_invalid",
            "tdx_bound_roster_missing",
            "tdx_bound_roster_unreadable",
            "tdx_bound_roster_snapshot_mismatch",
            "tdx_bound_roster_producer_mismatch",
            "tdx_bound_roster_publication_mismatch",
            "tdx_job_roster_binding_missing",
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
            "2&local_only=false&order=desc&limit=100"
        ),
        cross_section=PREFIX + "/query?day=2026-11-12&local_only=false&limit=100",
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
        (
            "自动日线在受理时绑定目标交易日的晨间周期名册作业和其实际快照；"
            "jobs 的 roster_binding 给出 request_id/day/snapshot_id/source_binding，"
            "snapshot_id 为实际使用的发布名册。首次执行及同号恢复不选最新名册；"
            "绑定证据缺失或不匹配时保留分类并停止，不改采手动快照。旧记录不推造晨间来源。"
        ),
        (
            "Queries require an independent --allow-tdx-source-query opt-in on "
            "DEV 18766; collection permission and workers are unchanged."
        ),
        (
            "Today is refreshed without a response cache during the unfinished "
            "session, including lunch and closing-price trading until 15:30 "
            "Beijing. Before 09:25 and on confirmed closed days no current bar "
            "is fabricated."
        ),
        (
            "Source receipt time is observed_at; SDK 15:00 is only a date "
            "projection. Missing/failed upstream never becomes yesterday or a "
            "zero bar."
        ),
        (
            "Local-first cross-section pages process at most 100 published-"
            "roster/local candidates; request/time bounds can shorten a page. "
            "Follow next_offset and pin snapshot_id. total counts candidates, "
            "not returned bars."
        ),
        (
            "Historical single-stock source paging overlaps <=800 Type9 rows, "
            "merges by date/market/code and preserves published local values. "
            "Current unfinished values may temporarily supersede a held "
            "intraday row without writing it."
        ),
        (
            "Current queries are new observations, not an atomic historical "
            "snapshot across pages; explicit as_of freezes only published local"
            " versions. Query results never increase persisted coverage."
        ),
        "历史日线可用 order=desc 按交易日倒序读取；省略时使用 asc。",
        (
            "Local-first queries supplement only missing eligible dates from "
            "the bound TDX source. GET never writes facts, raw files, rosters "
            "or jobs. as_of and local_only=true are strictly local."
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
            "Refresh the universe at 09:00. Reuse that job at 15:35. "
            "Each daily update covers D minus 14 "
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
