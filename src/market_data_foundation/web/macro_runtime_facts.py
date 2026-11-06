"""Read-only collection facts, independent of strategy and process liveness."""

from datetime import datetime


def collection_facts(host):
    facts = {
        "last_successful_collection_at": None,
        "latest_data_period": None,
        "latest_data_periods": {},
        "collection_facts_classification": None,
    }
    try:
        records = host.executions()["executions"]
        successes = []
        for record in records:
            result = record.get("result") or {}
            finished = record.get("finished_at")
            verified = (result.get("readback") or {}).get(
                "verified"
            ) is True or result.get("availability_verified") is True
            if host.profile.key == "claims":
                from market_data_foundation.web.claims_jobs import actuals_ready

                # Range readback can be verified while this release's six exact
                # weeks are still missing. Conversely the normal one-week lag in
                # continued claims must not invalidate a proved current release.
                verified = (
                    actuals_ready(result)
                    if result.get("material", {}).get("actual_weeks")
                    else verified
                    and result.get("status") not in {"partial", "unavailable"}
                )
            if (
                record.get("state") in {"complete", "succeeded"}
                and verified
                and finished
            ):
                at = datetime.fromisoformat(finished)
                if at.utcoffset() is not None:
                    successes.append((at, finished))
        if successes:
            facts["last_successful_collection_at"] = max(successes)[1]
    except Exception:
        facts["collection_facts_classification"] = "execution_history_unavailable"
    try:
        with host.service() as service:
            if host.profile.key == "m3":
                status = {
                    "latest_months": {
                        f"{dataset}/{series}": period
                        for dataset in ("m3", "advm3")
                        for series, period in service.store.status(dataset)[
                            "latest_months"
                        ].items()
                    }
                }
            else:
                status = (
                    service.status()
                    if hasattr(service, "status")
                    else service.store.status()
                )
        periods = {}
        for key in ("latest_months", "latest_quarters", "latest_weeks"):
            periods.update(status.get(key) or {})
        facts["latest_data_periods"] = periods
        facts["latest_data_period"] = max(
            (value for value in periods.values() if value), default=None
        )
    except Exception:
        facts["collection_facts_classification"] = "local_data_status_unavailable"
    return facts
