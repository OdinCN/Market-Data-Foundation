"""Consumer projections from bounded evidence; no IO or inferred vintages."""

from decimal import Decimal, InvalidOperation

SCOPE = ("series_id", "statistic", "unit", "adjustment")


def _confirmed(claim):
    return (
        claim.get("confirmation_state") == "confirmed"
        and claim.get("value") is not None
    )


def _one(candidates):
    confirmed = [c for c in candidates if _confirmed(c)]
    if (
        not confirmed
        or len({c["value"] for c in confirmed}) != 1
        or len(confirmed) != len(candidates)
    ):
        return None
    return confirmed[-1]


def _difference(original, reported):
    if original is None or reported is None:
        return None
    try:
        difference = Decimal(reported["value"]) - Decimal(original["value"])
        return str(difference) if difference.is_finite() else None
    except InvalidOperation, KeyError, TypeError:
        return None


def enrich_core_release(event, family):
    """Keep material actual usable when continuous_version is unbound.

    Prior values are selected only within identical statistic/unit/adjustment.
    This is a read projection: no events, timestamps or values are written back.
    """
    if family not in {"employment", "pce", "consumption"}:
        return
    from market_data_foundation.historical.cpi_timeseries import month_at, month_number

    comparisons = []
    for actual in (c for c in event["claims"] if c["role"] == "actual"):
        previous = month_at(month_number(actual["reference_month"]) - 1)

        def same(c):
            return all(c.get(k) == actual.get(k) for k in SCOPE)

        reported = [
            c
            for c in event["claims"]
            if same(c)
            and c["role"] in {"prior_revised", "prior_as_reported_in_this_event"}
            and c["reference_month"] < actual["reference_month"]
        ]
        revisions = []
        for period in sorted({previous, *(c["reference_month"] for c in reported)}):
            explicit = [
                c
                for c in event["claims"]
                if same(c)
                and c["reference_month"] == period
                and c["role"]
                in {"official_previous", "official_previously_reported_prior"}
            ]
            earlier = [
                c
                for c in event.get("prior_event_values", [])
                if same(c) and c["reference_month"] == period
            ]
            if not explicit and earlier:
                latest = max(
                    (c.get("source_version") or {}).get("value", "") for c in earlier
                )
                earlier = [
                    c
                    for c in earlier
                    if (c.get("source_version") or {}).get("value", "") == latest
                ]
            original_candidates = explicit or earlier
            reported_candidates = [
                c for c in reported if c["reference_month"] == period
            ]
            original, revised = _one(original_candidates), _one(reported_candidates)
            revisions.append(
                dict(
                    reference_month=period,
                    official_previous=original,
                    official_previous_candidates=original_candidates,
                    previous_as_reported_in_this_release=revised,
                    reported_previous_candidates=reported_candidates,
                    revision_difference=_difference(original, revised),
                    original_basis="explicitly_quoted_in_this_material"
                    if explicit
                    else "earlier_official_event_material"
                    if earlier
                    else None,
                    missing=(
                        ["official_previous_unconfirmed"] if original is None else []
                    )
                    + (
                        ["this_release_previous_unconfirmed"] if revised is None else []
                    ),
                )
            )
        immediate = next(r for r in revisions if r["reference_month"] == previous)
        comparisons.append(
            dict(
                **{k: actual[k] for k in (*SCOPE, "reference_month")},
                actual=actual if _confirmed(actual) else None,
                actual_candidates=[actual],
                previous_reference_month=previous,
                official_previous=immediate["official_previous"],
                previous_as_reported_in_this_release=immediate[
                    "previous_as_reported_in_this_release"
                ],
                revision_difference=immediate["revision_difference"],
                revision_history=revisions,
                mdf_pre_event_last_consumable_previous=actual.get(
                    "mdf_pre_event_last_consumable_previous"
                ),
                mdf_pre_event_previous_basis=actual.get("mdf_pre_event_previous_basis"),
                missing=immediate["missing"]
                + (
                    [
                        actual.get("mdf_pre_event_previous_missing")
                        or "mdf_pre_event_previous_unconfirmed"
                    ]
                    if actual.get("mdf_pre_event_last_consumable_previous") is None
                    else []
                )
                + (["event_actual_unconfirmed"] if not _confirmed(actual) else []),
            )
        )
    event["value_comparisons"] = comparisons
    if family in {"pce", "consumption"}:
        event["shared_publication"] = dict(
            event_id=event["event_id"],
            publisher="BEA",
            publication="Personal Income and Outlays",
            member_assets=["pce", "consumption"],
            current_asset=family,
            material_digests=sorted({p["raw_sha256"] for p in event["evidence"]}),
            interpretation="asset_specific_independent_results",
            other_asset_status="not_read_in_this_response",
            numeric_snapshot="independent_API_acquisitions_not_atomic_official_snapshot",
        )
