"""Published stock membership is a base capability shared by daily consumers."""

from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from market_data_foundation.canonical.model import require
from market_data_foundation.historical.tdx_p0_contract import (
    BINDING as DAILY_BINDING,
)
from market_data_foundation.historical.tdx_p0_contract import (
    DEFINITION as DAILY_DEFINITION,
)
from market_data_foundation.historical.tdx_p0_contract import SOURCE as DAILY_SOURCE
from market_data_foundation.historical.tdx_p0_roster import lifecycle

PREFIX = "/api/r5/tdx/roster"
DEFINITION = replace(
    DAILY_DEFINITION,
    identity="mdf.market.tdx.stock.roster",
    domain_contract="Dated SH/SZ/BJ A-share membership and original roster evidence",
    purpose="Published membership; provider directory is not historical census",
    key_semantics="snapshot_id, market, instrument; historical code lineage separate",
    unit_semantics="security membership, no price or volume unit",
    temporal_semantics="Actual receipt/publication; separate dated lifecycle evidence",
    revision_semantics="Immutable snapshot identities; no backdating later membership",
    completeness_semantics="Dated market enumeration; unknown intervals stay unknown",
)
SOURCE = replace(
    DAILY_SOURCE,
    identity="mdf.source.tdx-v100.stock.roster",
    operation="stock.directory",
    projection_revision="go-tdx-v1.0.0-stock-directory-v1",
    variant="stock-directory-with-exchange-lifecycle",
    acquisition_mode="explicit_dev_roster_refresh",
    temporal_contract="Actual directory receipt; exchange originals date lifecycle",
    freshness_contract="Directory refresh never extends exchange evidence validity",
    pagination_contract="SH/SZ Count/Code pages; BJ GetBJDirectory; retained wire",
)
BINDING = replace(
    DAILY_BINDING,
    identity="mdf.binding.tdx-v100.stock.roster",
    source=SOURCE.key,
    mappings=tuple(
        replace(
            m,
            identity="stock_roster",
            definition=DEFINITION.key,
            semantic_revision="stock-roster-r1",
            temporal_revision="received-published-membership-r1",
        )
        for m in DAILY_BINDING.mappings
    ),
    compatibility_policy="Pinned SDK Count/Code/BJDirectory, wire and exchange digests",
    limitations=(
        "Current provider directory is not a full historical census",
        "Known lifecycle intervals require dated exchange originals",
        "BJ code aliases and historical lineage remain separately qualified",
        "Incomplete refresh does not replace a complete published roster",
        "No silent provider fallback",
    ),
)


def facts(snapshot, day):
    details = (snapshot or {}).get("details", {})
    through = details.get("valid_through")
    observed = (snapshot or {}).get("observed_at")
    if isinstance(observed, str):
        observed = datetime.fromisoformat(observed)
    observed_day = (
        observed.astimezone(ZoneInfo("Asia/Shanghai")).date() if observed else None
    )
    refresh = details.get("directory_refreshed_at")
    refreshed_day = (
        datetime.fromisoformat(refresh).astimezone(ZoneInfo("Asia/Shanghai")).date()
        if refresh
        else None
    )
    markets = details.get("markets", [])
    complete = {m.get("market") for m in markets if m.get("state") == "COMPLETE"}
    state = (
        "MISSING"
        if snapshot is None
        else "INCOMPLETE"
        if complete != {0, 1, 2}
        else "STALE"
        if (through and date.fromisoformat(through) < day)
        or (not through and observed_day and observed_day < day)
        else "DATED"
        if through
        else "CURRENT_DIRECTORY_ONLY"
    )
    return dict(
        state=state,
        classification={
            "MISSING": "tdx_universe_required",
            "INCOMPLETE": "tdx_roster_incomplete",
            "STALE": "tdx_roster_stale",
            "CURRENT_DIRECTORY_ONLY": "tdx_roster_historical_membership_unverified",
        }.get(state),
        snapshot_id=(snapshot or {}).get("snapshot_id"),
        observed_at=(snapshot or {}).get("observed_at"),
        available_at=(snapshot or {}).get("available_at"),
        requested_day=day.isoformat(),
        valid_through=through,
        directory_observed_day=(refreshed_day or observed_day).isoformat()
        if (refreshed_day or observed_day)
        else None,
        same_day_directory=refreshed_day == day,
        markets=markets,
        historical_membership=details.get("historical_membership", "UNKNOWN"),
        code_reuse=details.get("code_reuse", "UNKNOWN"),
        provider_requests=0,
    )


def published(store, day, *, snapshot_id=None, as_of=None, require_current=False):
    options = dict(as_of=as_of)
    if snapshot_id is not None:
        options["snapshot_id"] = snapshot_id
    snapshot = store.universe(**options)
    status = facts(snapshot, day)
    if require_current:
        require_usable(snapshot, status)
    return snapshot, status


def require_usable(snapshot, status):
    require(snapshot is not None, "tdx_universe_required")
    require(status["state"] != "INCOMPLETE", "tdx_roster_incomplete")
    # A fresh complete provider directory can support acquisition while the
    # independently dated exchange census still cannot certify coverage.
    require(
        status["state"] != "STALE" or status.get("same_day_directory"),
        "tdx_roster_stale",
    )


def query(
    store,
    day,
    *,
    as_of=None,
    snapshot_id=None,
    market=None,
    search="",
    offset=0,
    limit=100,
):
    require(market is None or market in {0, 1, 2}, "tdx_roster_market")
    require(offset >= 0 and 1 <= limit <= 1000, "tdx_roster_page")
    snapshot, status = published(store, day, snapshot_id=snapshot_id, as_of=as_of)
    members = [
        {**m, "membership_state": lifecycle(m, day)}
        for m in (snapshot or {}).get("members", [])
        if (market is None or m["market"] == market)
        and search.casefold() in (m["instrument"] + " " + m["name"]).casefold()
    ]
    members.sort(key=lambda m: (m["market"], m["instrument"]))
    return dict(
        status="OK" if snapshot else "NOT_AVAILABLE",
        **status,
        rows=members[offset : offset + limit],
        total=len(members),
        offset=offset,
        next_offset=offset + limit if offset + limit < len(members) else None,
        sources=[
            {k: s[k] for k in ("kind", "url", "sha256", "received_at") if k in s}
            for s in (snapshot or {}).get("details", {}).get("sources", [])
        ],
    )
