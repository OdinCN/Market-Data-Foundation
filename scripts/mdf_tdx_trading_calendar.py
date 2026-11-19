"""Prepare or explicitly publish the unified A-share annual calendar in DEV."""

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import mkdtemp
from zoneinfo import ZoneInfo

import httpx

from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.historical.tdx_p0_jobs import atomic
from market_data_foundation.historical.tdx_trading_calendar import (
    EXCHANGE_INDEX,
    calendar_with_supplements,
    exchange_notice_url,
    exchange_supplement,
    exchange_supplement_urls,
    exchange_url_valid,
    government_notice_url,
    government_search_url,
    government_url_valid,
    holiday_dates,
    notice_windows,
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


def prepare_calendar(
    year,
    *,
    get,
    artifact,
    government_url=None,
    exchange_annual_url=None,
    exchange_supplement_overrides=(),
):
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

    def annual(raw, evidence, *, exchange):
        windows = notice_windows(raw, year, exchange=exchange)
        return holiday_dates(windows), dict(
            evidence,
            holiday_windows={
                name: [day.isoformat() for day in window]
                for name, window in windows.items()
            },
        )

    def exchange():
        url = exchange_annual_url
        discovery = {}
        if not url:
            if index is None:
                return None
            index_raw, index_evidence = index
            url = exchange_notice_url(index_raw, year)
            discovery["index"] = index_evidence
        require(exchange_url_valid(url), "tdx_calendar_exchange_notice_url")
        raw, evidence = original(url, "sse-notice.html")
        return annual(raw, dict(evidence, **discovery), exchange=True)

    def supplement(url, sequence):
        require(exchange_url_valid(url), "tdx_calendar_exchange_notice_url")
        raw, proof = original(url, f"sse-supplement-{sequence}.html")
        return dict(proof, **exchange_supplement(raw, year, url))

    def government():
        url = government_url
        discovery = {}
        if not url:
            raw, index = original(government_search_url(year), "government-index.json")
            url = government_notice_url(raw, year)
            discovery["index"] = index
        require(government_url_valid(url), "tdx_calendar_government_notice_url")
        raw, evidence = original(url, "government-notice.html")
        return annual(raw, dict(evidence, **discovery), exchange=False)

    def attempt(source, acquire, **context):
        try:
            return acquire()
        except SOURCE_ERRORS as error:
            attempts.append(
                dict(source=source, classification=_failure(error), **context)
            )
            return None

    source = "state_council_holiday_notice"
    candidate = attempt(source, government)
    index = attempt(
        "sse_notice_index", lambda: original(EXCHANGE_INDEX, "sse-index.html")
    )
    market = attempt("sse_annual_notice", exchange)
    discovered = (
        attempt(
            "sse_supplement_index", lambda: exchange_supplement_urls(index[0], year)
        )
        if index
        else set()
    )
    urls = sorted((discovered or set()).union(exchange_supplement_overrides))
    supplements = []
    for sequence, url in enumerate(urls, start=1):
        evidence = attempt("sse_supplement", lambda: supplement(url, sequence), url=url)
        if evidence is not None:
            supplements.append(evidence)
    if market is None and candidate is None:
        result = dict(
            year=year,
            state="UNAVAILABLE",
            schedulable=False,
            exchange_confirmed=False,
            attempts=attempts,
        )
        if supplements:
            result["evidence"] = dict(supplements=supplements)
        return None, result
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
    if supplements:
        proof["supplements"] = supplements
        proof["annual_holidays"] = [day.isoformat() for day in sorted(holidays)]
        holidays, applied, _ = calendar_with_supplements(
            year, holidays, proof["holiday_windows"], supplements
        )
        proof["applied_supplements"] = applied
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
    proof = result.get("evidence", {})
    if holidays is None and not proof.get("supplements"):
        return result
    evidence = dict(
        source=result.get("source"),
        schedulable=result["schedulable"],
        exchange_confirmed=result["exchange_confirmed"],
        **proof,
    )
    base = proof.get("annual_holidays")
    if base is not None:
        holidays = {date.fromisoformat(day) for day in base}
    available_at, changed = store.save_trading_calendar(
        result["year"], holidays, evidence
    )
    if available_at is None:
        return result
    publication = store.trading_calendar_publication(result["year"], as_of=available_at)
    effective = publication["evidence"]
    source = effective["source"]
    confirmed = source == "sse_annual_notice"
    retained = source != result.get("source")
    info = dict(source=source, schedulable=True, exchange_confirmed=confirmed)
    if "applied_supplements" in effective:
        info["applied_supplements"] = effective["applied_supplements"]
    return dict(
        result,
        state="PUBLISHED"
        if changed
        else ("RETAINED_HIGHER_AUTHORITY" if retained else "ALREADY_PUBLISHED"),
        available_at=available_at.isoformat(),
        publication=info,
        schedulable=True,
        exchange_confirmed=confirmed,
        holidays=[day.isoformat() for day in publication["holidays"]],
        trading_days=sum(
            year_sessions(result["year"], set(publication["holidays"])).values()
        ),
    )


def scheduled_year(at):
    local = at.astimezone(SHANGHAI)
    return local.year + (local.month in (11, 12))


def scheduled_years(at):
    current = at.astimezone(SHANGHAI).year
    return sorted({current, scheduled_year(at)})


def verify_history(store, *, start, end, as_of):
    """Compare scheduling evidence to observed trading across cash-equity markets."""
    require(
        start <= end < as_of.astimezone(SHANGHAI).date(), "tdx_calendar_history_range"
    )
    scheduled = store.trading_calendar(as_of=as_of, start=start, end=end)
    counts = store.market_trade_counts(as_of=as_of, start=start, end=end)
    days = []
    summary = {}
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        planned = scheduled.get(day)
        observed = counts.get(day, {})
        if observed:
            status = {
                True: "CONFIRMED_OPEN",
                False: "PLANNED_CLOSED_WITH_TRADES",
                None: "OBSERVED_OPEN_NO_PLAN",
            }[planned]
        else:
            status = "UNVERIFIED"
        summary[status] = summary.get(status, 0) + 1
        days.append(
            dict(
                day=day.isoformat(),
                scheduled_open=planned,
                markets_with_positive_volume=observed,
                status=status,
            )
        )
    return dict(
        start=start.isoformat(),
        end=end.isoformat(),
        as_of=as_of.isoformat(),
        source="mdf_tdx_published_cash_equity_daily_bars",
        summary=summary,
        days=days,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--year",
        type=int,
        help="Defaults to next Shanghai year in November/December, else current year.",
    )
    mode.add_argument(
        "--scheduled",
        action="store_true",
        help="Check the current and upcoming calendar.",
    )
    mode.add_argument(
        "--verify-history", nargs=2, metavar=("START", "END"), type=date.fromisoformat
    )
    parser.add_argument("--government-url", help="Optional official gov.cn notice.")
    parser.add_argument(
        "--exchange-annual-url", help="Optional SSE annual notice from the archive."
    )
    parser.add_argument(
        "--exchange-supplement-url",
        action="append",
        default=[],
        help="Optional SSE supplemental notice from the archive. Repeatable.",
    )
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--dev-opt-in", action="store_true")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    require(args.dev_opt_in, "tdx_calendar_dev_opt_in_required")
    args.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = datetime.now(UTC)
    if args.verify_history:
        require(
            not (
                args.publish
                or args.government_url
                or args.exchange_annual_url
                or args.exchange_supplement_url
            ),
            "tdx_calendar_history_read_only",
        )
        start, end = args.verify_history
        result = verify_history(PgStore(), start=start, end=end, as_of=now)
        artifact = Path(mkdtemp(prefix="history-", dir=args.artifact_dir))
        atomic(artifact / "receipt.json", result)
        print(json.dumps(result, ensure_ascii=False))
        return

    years = (
        scheduled_years(now)
        if args.scheduled
        else [args.year if args.year is not None else scheduled_year(now)]
    )
    require(all(1990 <= year <= 2100 for year in years), "tdx_calendar_year")
    if args.government_url:
        require(
            government_url_valid(args.government_url),
            "tdx_calendar_government_notice_url",
        )
    overrides = list(args.exchange_supplement_url)
    if args.exchange_annual_url:
        overrides.append(args.exchange_annual_url)
    require(
        all(exchange_url_valid(url) for url in overrides),
        "tdx_calendar_exchange_notice_url",
    )
    unavailable = False
    with httpx.Client(timeout=httpx.Timeout(15, connect=5)) as client:

        def get(url):
            response = client.get(url)
            response.raise_for_status()
            return response.content

        for year in years:
            artifact = Path(mkdtemp(prefix=f"{year}-", dir=args.artifact_dir))
            explicit_year = year == years[-1]
            holidays, result = prepare_calendar(
                year,
                get=get,
                artifact=artifact,
                government_url=args.government_url if explicit_year else None,
                exchange_annual_url=args.exchange_annual_url if explicit_year else None,
                exchange_supplement_overrides=args.exchange_supplement_url
                if explicit_year
                else (),
            )
            if args.publish and (
                result["schedulable"] or result.get("evidence", {}).get("supplements")
            ):
                with connect() as conn:
                    migrate(conn, dev_opt_in=True)
                result = publish_calendar(PgStore(), holidays, result)
            atomic(artifact / "receipt.json", result)
            print(json.dumps(result, ensure_ascii=False))
            # Absence of next year's notice before publication is expected.
            if year == years[0]:
                unavailable = result["state"] == "UNAVAILABLE"
    if unavailable:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
