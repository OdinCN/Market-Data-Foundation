"""Named dense-market contracts and P2 handoff dispositions for P3."""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from market_data_foundation.canonical.model import (
    DatasetDefinition,
    Key,
    Kind,
    Scope,
    Transform,
    require,
)
from market_data_foundation.qualification.contracts import Capability


class Disposition(StrEnum):
    BOUNDED_CONTRACT = "bounded_contract"
    NOT_ADMITTED = "not_admitted"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class P2Disposition:
    capability: Capability
    disposition: Disposition
    bounded_scope: str
    reason: str
    evidence_needed: str
    later_gate: str


SECURITY_UNIVERSE = DatasetDefinition(
    identity="mdf.security.universe.a-share",
    revision="p3-r1",
    domain_contract=(
        "Explicit finite A-share security membership only; exchange-qualified "
        "identity is required. Current P2 evidence does not admit a complete "
        "market universe."
    ),
    purpose="bounded security membership and security-identity contract",
    source_qualified=True,
    key_semantics=(
        "security_id=<ticker>.<exchange> plus an explicit identity namespace; "
        "provider suffixes and MIC codes are not silently equated"
    ),
    unit_semantics="not_applicable",
    temporal_semantics=(
        "effective/known times must be explicit when claimed; absent listing, "
        "delisting, or suspension state remains unknown"
    ),
    revision_semantics=(
        "new evidence or membership changes form a new immutable version"
    ),
    completeness_semantics=(
        "bounded explicit membership only; complete A-share universe is NOT_ADMITTED"
    ),
    scope=Scope(),
)

RAW_DAILY_BARS = DatasetDefinition(
    identity="mdf.market.bars.daily.raw",
    revision="p3-r1",
    domain_contract=(
        "Source-qualified unadjusted daily OHLCV/amount observations with "
        "binding-explicit units and local trading-date semantics."
    ),
    purpose="raw daily market facts before any adjustment transform",
    source_qualified=True,
    key_semantics="security_id + local_trading_date",
    unit_semantics=(
        "price/volume/amount units must be explicit in the source mapping; "
        "generic placeholders are not normalization contracts"
    ),
    temporal_semantics=(
        "date is the source-qualified local trading date; encoded midnight is "
        "not automatically UTC event time"
    ),
    revision_semantics="source revision and repeated acquisition remain distinct",
    completeness_semantics=(
        "bounded observed rows only; missing sessions are not filled and gaps "
        "do not prove suspension"
    ),
    scope=Scope(),
)

CORPORATE_ACTIONS = DatasetDefinition(
    identity="mdf.market.corporate-actions",
    revision="p3-r1",
    domain_contract=(
        "Typed source-qualified corporate-action events. P3-R1 admits a "
        "normalized cash-dividend event only with an explicit per-share unit "
        "mapping; other event categories remain source-qualified or unknown."
    ),
    purpose="corporate-action lineage input without inferred event semantics",
    source_qualified=True,
    key_semantics=("security_id + event identity/effective date + source revision"),
    unit_semantics=(
        "event-category-specific; normalized cash dividend = CNY_per_share"
    ),
    temporal_semantics=(
        "provider event/effective date is distinct from MDF obtained/usable time"
    ),
    revision_semantics=(
        "provider correction creates a new source revision; no in-place rewrite"
    ),
    completeness_semantics=(
        "only explicitly admitted event categories; unknown categories fail closed"
    ),
    scope=Scope(),
)

ADJUSTMENT_FACTORS = DatasetDefinition(
    identity="mdf.market.adjustment-factors",
    revision="p3-r1",
    domain_contract=(
        "Explicit factor-series semantics only. Current P2 evidence does not "
        "establish a provider-independent admitted factor series."
    ),
    purpose="future explicit adjustment-factor lineage input",
    source_qualified=True,
    key_semantics=("security_id + factor effective date + factor semantic revision"),
    unit_semantics="dimensionless factor only when independently evidenced",
    temporal_semantics=("factor effective date and MDF known/usable time are distinct"),
    revision_semantics="factor-provider/algorithm revision must be explicit",
    completeness_semantics=(
        "NOT_ADMITTED as a general factor output under current P2 evidence"
    ),
    scope=Scope(),
)

PROVIDER_ADJUSTED_DAILY_BARS = DatasetDefinition(
    identity="mdf.market.bars.daily.adjusted.provider",
    revision="p3-r1",
    domain_contract=(
        "Provider-returned adjusted bars retain provider-qualified adjustment "
        "identity; adjustment labels do not become a universal MDF algorithm."
    ),
    purpose="source-qualified provider adjusted view",
    source_qualified=True,
    key_semantics=(
        "security_id + local_trading_date + exact source adjustment contract"
    ),
    unit_semantics=(
        "explicit price/volume/amount units as required for raw daily bars"
    ),
    temporal_semantics="local trading date plus exact source revision/known time",
    revision_semantics=(
        "provider adjustment semantics are part of the binding revision"
    ),
    completeness_semantics=(
        "bounded to the exact qualified source contract; no universal equivalence"
    ),
    scope=Scope(),
)

MDF_DERIVED_ADJUSTED_DAILY_BARS = DatasetDefinition(
    identity="mdf.market.bars.daily.adjusted.mdf",
    revision="p3-r1",
    domain_contract=(
        "MDF-derived adjusted bars require raw-bar input lineage plus admitted "
        "action/factor inputs and an exact transform revision, parameters, and "
        "anchor policy."
    ),
    purpose="deterministic MDF-derived adjusted view",
    source_qualified=False,
    key_semantics=("raw-bar version + factor/action versions + transform revision"),
    unit_semantics="derived price units follow the explicit transform contract",
    temporal_semantics=(
        "inherits source dates; MDF freeze/usable time is not backdated"
    ),
    revision_semantics=(
        "transform revision/parameters/anchor changes create a new version"
    ),
    completeness_semantics=(
        "fails closed when required action/factor inputs are missing or unknown"
    ),
    scope=Scope(),
)


P2_HANDOFF_DISPOSITIONS = (
    P2Disposition(
        capability=Capability.SECURITY_UNIVERSE,
        disposition=Disposition.NOT_ADMITTED,
        bounded_scope="complete A-share universe",
        reason=(
            "TDX and Fuyao R1 did not prove complete multi-market coverage, "
            "pagination exhaustion, listing status, or historical membership."
        ),
        evidence_needed=(
            "explicit market coverage and exhaustion/completeness evidence plus "
            "security identity and listing-status semantics"
        ),
        later_gate="capability-specific source admission before P4 collection",
    ),
    P2Disposition(
        capability=Capability.CORPORATE_ACTIONS,
        disposition=Disposition.BOUNDED_CONTRACT,
        bounded_scope=(
            "typed source-qualified events; normalized P3-R1 support is limited "
            "to explicit cash-dividend-per-share mapping"
        ),
        reason=(
            "TDX R1 has bounded event/category evidence while Fuyao r17 stayed "
            "UNRESOLVED_DATA_NOT_READY; categories and units cannot be unified."
        ),
        evidence_needed=(
            "per-event-category unit/effective-date mapping and completeness "
            "evidence for any broader normalized event contract"
        ),
        later_gate="dataset/source-binding admission per event family",
    ),
    P2Disposition(
        capability=Capability.ADJUSTMENT_FACTORS,
        disposition=Disposition.NOT_ADMITTED,
        bounded_scope="general canonical adjustment-factor series",
        reason=(
            "TDX Go/pytdxdata expose no admitted native factor series; mootdx "
            "evidence is derived/anchored and Fuyao factor evidence is unresolved."
        ),
        evidence_needed=(
            "factor-series identity, unit, anchor, revision, and source evidence"
        ),
        later_gate=("factor-specific source/derivation admission before adjusted data"),
    ),
    P2Disposition(
        capability=Capability.BARS_ADJUSTED,
        disposition=Disposition.NOT_ADMITTED,
        bounded_scope="provider-independent adjusted daily bars",
        reason=(
            "qualified samples show provider-specific views/derivations with "
            "algorithm and anchor limits; labels do not prove universal identity."
        ),
        evidence_needed=(
            "exact provider contract or MDF transform with admitted raw/action/"
            "factor inputs, revision, parameters, and anchor"
        ),
        later_gate="provider-view or MDF-derived adjustment contract admission",
    ),
)


def disposition_for(capability: Capability) -> P2Disposition:
    matches = [
        item for item in P2_HANDOFF_DISPOSITIONS if item.capability == capability
    ]
    require(len(matches) == 1, "p2_handoff_disposition_missing")
    return matches[0]


def require_bounded_universe(
    members: tuple[str, ...], *, completeness: str, identity_namespace: str
) -> tuple[str, ...]:
    require(bool(members), "empty_universe")
    require(len(set(members)) == len(members), "duplicate_universe_member")
    allowed_by_namespace = {
        "provider_suffix": {"SH", "SZ", "BJ"},
        "mic": {"XSHG", "XSHE", "XBSE"},
    }
    require(
        identity_namespace in allowed_by_namespace, "security_namespace_not_admitted"
    )
    allowed_exchanges = allowed_by_namespace[identity_namespace]
    require(
        all(
            member.count(".") == 1
            and member.split(".", 1)[0]
            and member.split(".", 1)[1] in allowed_exchanges
            for member in members
        ),
        "invalid_security_identity",
    )
    require(
        completeness in {"bounded_observed", "bounded_explicit_membership"},
        "universe_completeness_not_admitted",
    )
    return members


def require_explicit_daily_units(
    *, price_unit: str, volume_unit: str, amount_unit: str
) -> tuple[str, str, str]:
    require(price_unit == "CNY", "daily_bar_price_unit_not_admitted")
    require(volume_unit == "shares", "daily_bar_volume_unit_not_admitted")
    require(amount_unit == "CNY", "daily_bar_amount_unit_not_admitted")
    return price_unit, volume_unit, amount_unit


def require_no_session_inference(
    observed: tuple[date, ...], output: tuple[date, ...]
) -> tuple[date, ...]:
    require(output == observed, "session_inference_not_admitted")
    return output


def require_supported_corporate_action(
    *, event_kind: str, unit: str
) -> tuple[str, str]:
    require(event_kind == "cash_dividend", "corporate_action_category_not_admitted")
    require(unit == "CNY_per_share", "corporate_action_unit_not_admitted")
    return event_kind, unit


def mdf_adjustment_transform(
    *,
    revision: str,
    raw_definition: Key,
    raw_version: Key,
    adjustment_definition: Key,
    adjustment_input_version: Key,
    anchor_policy: str,
) -> Transform:
    require(bool(revision.strip()), "adjustment_transform_revision_required")
    require(
        raw_definition == RAW_DAILY_BARS.key, "adjustment_raw_contract_not_admitted"
    )
    require(raw_version.kind == Kind.VERSION, "adjustment_raw_version_required")
    require(
        adjustment_definition == CORPORATE_ACTIONS.key,
        "adjustment_input_contract_not_admitted",
    )
    require(
        adjustment_input_version.kind == Kind.VERSION,
        "adjustment_input_version_required",
    )
    require(
        anchor_policy == "latest_observed_raw_bar",
        "adjustment_anchor_policy_not_admitted",
    )
    return Transform(
        identity="mdf.market.bars.daily.adjust",
        revision=revision,
        parameters=(
            ("raw_definition", f"{raw_definition.identity}@{raw_definition.revision}"),
            ("raw_version", raw_version.identity),
            (
                "adjustment_definition",
                f"{adjustment_definition.identity}@{adjustment_definition.revision}",
            ),
            ("adjustment_input_version", adjustment_input_version.identity),
            ("anchor_policy", anchor_policy),
        ),
    )
