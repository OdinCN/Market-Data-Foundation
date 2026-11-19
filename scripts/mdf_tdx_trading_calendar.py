"""Prepare or explicitly publish the unified A-share annual calendar in DEV."""

import argparse
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import mkdtemp
from zoneinfo import ZoneInfo

import httpx

from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.historical.tdx_p0_jobs import atomic
from market_data_foundation.historical.tdx_trading_calendar import (
    EXCHANGE_INDEX,
    TIMOR_URL,
    exchange_notice_url,
    government_notice_url,
    government_search_url,
    government_url_valid,
    notice_holidays,
    timor_holidays,
    year_sessions,
)
from market_data_foundation.storage.tdx_p0 import PgStore, connect, migrate

SHANGHAI = ZoneInfo("Asia/Shanghai")
SOURCE_ERRORS = (httpx.HTTPError, ContractError, ValueError, TypeError)


def _failure(error):
    if isinstance(error, httpx.HTTPStatusError):
        return f"http_{error.response.status_code}"
    if isinstance(error, ContractError):
        return str(error)
    return type(error).__name__


def prepare_calendar(year, *, get, artifact, government_url=None):
    attempts = []

    def original(url, name):
        raw = get(url)
        require(0 < len(raw) < 2**21, "tdx_calendar_original_size")
        path = artifact / name
        path.write_bytes(raw)
        return raw, dict(
            url=url,
            sha256=sha256(raw).hexdigest(),
            path=str(path),
            received_at=datetime.now(UTC).isoformat(),
        )

    def exchange():
        raw, index = original(EXCHANGE_INDEX, "sse-index.html")
        url = exchange_notice_url(raw, year)
        raw, evidence = original(url, "sse-notice.html")
        return notice_holidays(raw, year, exchange=True), dict(evidence, index=index)

    def government():
        url = government_url
        discovery = {}
        if not url:
            raw, index = original(government_search_url(year), "government-index.json")
            url = government_notice_url(raw, year)
            discovery["index"] = index
        require(government_url_valid(url), "tdx_calendar_government_notice_url")
        raw, evidence = original(url, "government-notice.html")
        return notice_holidays(raw, year, exchange=False), dict(evidence, **discovery)

    def timor():
        raw, evidence = original(TIMOR_URL.format(year=year), "timor.json")
        return timor_holidays(raw, year), evidence

    def attempt(source, acquire):
        try:
            return acquire()
        except SOURCE_ERRORS as error:
            attempts.append(dict(source=source, classification=_failure(error)))
            return None

    source = "state_council_holiday_notice"
    candidate = attempt(source, government)
    if candidate is None:
        source = "timor_holiday_api"
        candidate = attempt(source, timor)
    # The market notice is an independent check, even when a candidate is ready.
    market = attempt("sse_annual_notice", exchange)
    if market is None and candidate is None:
        return None, dict(
            year=year,
            state="UNAVAILABLE",
            schedulable=False,
            exchange_confirmed=False,
            attempts=attempts,
        )
    if market is not None:
        holidays, proof = market
        proof = dict(proof)
        if candidate is not None:
            candidate_days, candidate_proof = candidate
            proof["candidate"] = dict(source=source, **candidate_proof)
            proof["reconciliation"] = dict(
                matches=candidate_days == holidays,
                exchange_only=[
                    d.isoformat() for d in sorted(holidays - candidate_days)
                ],
                candidate_only=[
                    d.isoformat() for d in sorted(candidate_days - holidays)
                ],
            )
        source = "sse_annual_notice"
    else:
        holidays, proof = candidate
    return holidays, dict(
        year=year,
        state="CONFIRMED" if market is not None else "AWAITING_EXCHANGE",
        schedulable=True,
        exchange_confirmed=market is not None,
        source=source,
        evidence=proof,
        attempts=attempts,
        trading_days=sum(year_sessions(year, holidays).values()),
        holidays=[day.isoformat() for day in sorted(holidays)],
    )


def publish_calendar(store, holidays, result):
    """Publish eligible candidates; report the effective version after retention."""
    if holidays is None:
        return result
    evidence = dict(
        source=result["source"],
        schedulable=result["schedulable"],
        exchange_confirmed=result["exchange_confirmed"],
        **result["evidence"],
    )
    available_at, changed = store.save_trading_calendar(
        result["year"], holidays, evidence
    )
    publication = store.trading_calendar_publication(result["year"], as_of=available_at)
    retained = publication["evidence"]["source"] != result["source"]
    return dict(
        result,
        state="PUBLISHED"
        if changed
        else ("RETAINED_HIGHER_AUTHORITY" if retained else "ALREADY_PUBLISHED"),
        available_at=available_at.isoformat(),
        publication=dict(
            source=publication["evidence"]["source"],
            schedulable=True,
            exchange_confirmed=publication["evidence"]["source"] == "sse_annual_notice",
        ),
    )


def scheduled_year(at):
    local = at.astimezone(SHANGHAI)
    return local.year + (local.month in (11, 12))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--year",
        type=int,
        help="Defaults to next Shanghai year in November/December, else current year.",
    )
    parser.add_argument("--government-url", help="Optional official gov.cn notice.")
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--dev-opt-in", action="store_true")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    require(args.dev_opt_in, "tdx_calendar_dev_opt_in_required")
    year = args.year if args.year is not None else scheduled_year(datetime.now(UTC))
    require(1990 <= year <= 2100, "tdx_calendar_year")
    if args.government_url:
        require(
            government_url_valid(args.government_url),
            "tdx_calendar_government_notice_url",
        )
    args.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifact = Path(mkdtemp(prefix=f"{year}-", dir=args.artifact_dir))
    with httpx.Client(timeout=httpx.Timeout(15, connect=5)) as client:

        def get(url):
            response = client.get(url)
            response.raise_for_status()
            return response.content

        holidays, result = prepare_calendar(
            year,
            get=get,
            artifact=artifact,
            government_url=args.government_url,
        )
    if args.publish and result["schedulable"]:
        with connect() as conn:
            migrate(conn, dev_opt_in=True)
        result = publish_calendar(PgStore(), holidays, result)
    atomic(artifact / "receipt.json", result)
    print(json.dumps(result, ensure_ascii=False))
    if result["state"] == "UNAVAILABLE":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
