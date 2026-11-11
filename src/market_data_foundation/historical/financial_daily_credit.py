"""Owner-bound pinned credit archive and explicit FRED credit-only DEV tail."""

import csv
import io
from types import SimpleNamespace

from market_data_foundation.historical.claims_source import ClaimsResponse
from market_data_foundation.historical.cpi_request_control import CpiRequestControl
from market_data_foundation.historical.financial_daily import (
    DailyBatch,
    DailyPoint,
    FinancialError,
    canonical,
    dates,
    instant,
)
from market_data_foundation.historical.financial_daily_source import DailyFredSource
from market_data_foundation.historical.public_web_transport import (
    PublicFetchError,
    fetch_public,
    header_aware,
    raise_io,
)

CREDIT = ("BAMLC0A0CM", "BAMLH0A0HYM2")
ARCHIVE_COMMIT = "5f63f9ad367be0e9d28aac6a7b982500c23ffaa6"
ARCHIVE_ROOT = (
    "https://raw.githubusercontent.com/PStoclet/BBA_TFG_CODE_PABLO_STOCLET/"
    + ARCHIVE_COMMIT
    + "/data/05_Credit_Spreads/"
)
FILES = dict(
    zip(
        CREDIT,
        ("IG_OAS_spread_daily_1990_2024.csv", "HY_OAS_spread_daily_1990_2024.csv"),
    )
)
ARCHIVE_START, ARCHIVE_END = "1996-12-31", "2026-03-17"


def credit_binding(sid, *, origin=None):
    if sid not in CREDIT:
        raise FinancialError("financial_credit_series_invalid")
    archive = origin == "credit_archive"
    return dict(
        binding_id="mdf.binding.credit.archive-fred.personal-dev@r1",
        storage_permitted=True,
        authorized_scope=(
            "personal noncommercial research in MDF DEV; no third-party distribution"
        ),
        fred_code_role="metric_mapping",
        origin=origin or "credit_archive_and_fred",
        identity="pinned_archive_of_ICE_FRED_series"
        if archive
        else "FRED_distributed_ICE_observation"
        if origin == "fred"
        else "explicit_archive_initialization_and_FRED_tail",
        original_agency="ICE Data Indices, LLC",
        distributor="PStoclet / GitHub pinned archive"
        if archive
        else "FRED"
        if origin == "fred"
        else "PStoclet GitHub archive + FRED API",
        source_url=ARCHIVE_ROOT + FILES[sid]
        if archive
        else "https://fred.stlouisfed.org/series/" + sid,
        archive_commit=ARCHIVE_COMMIT,
        archive_actual_start=ARCHIVE_START,
        archive_actual_end=ARCHIVE_END,
        unit="Percent",
        basis_points="native percent * 100",
        frequency="Daily, Close",
        calendar="business_day_or_month_end",
        is_copyrighted=None,
        terms_url="https://fred.stlouisfed.org/legal/",
        attribution="Index provider: ICE Data Indices, LLC; FRED series "
        + sid
        + ". Historical archive: PStoclet/BBA_TFG_CODE_PABLO_STOCLET at "
        + ARCHIVE_COMMIT
        + ". Personal noncommercial research only; "
        + "historical backfill does not prove initial release.",
    )


def archive_points(raw, sid, start, end):
    from market_data_foundation.historical.financial_daily_origin import number

    dates(start, end)
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        if reader.fieldnames != ["observation_date", sid]:
            raise ValueError
        result = []
        previous = None
        first = None
        for row in reader:
            at = row["observation_date"]
            dates(at, at)
            if previous and at <= previous:
                raise ValueError
            first = first or at
            previous = at
            text = row[sid]
            if text is None:
                raise ValueError
            value = number(None if text in ("", ".") else text)
            if value is not None and value < 0:
                raise ValueError
            if start <= at <= end:
                result.append(
                    DailyPoint(
                        sid,
                        at,
                        value,
                        dict(
                            source_publication_at=None,
                            source_available_at=None,
                            archive_commit=ARCHIVE_COMMIT,
                            raw_value=text,
                            historical_initial_value_proven=False,
                        ),
                    )
                )
        if first != ARCHIVE_START or previous != ARCHIVE_END:
            raise ValueError
        return tuple(result)
    except ValueError, TypeError, KeyError, UnicodeError, csv.Error:
        raise FinancialError("financial_credit_archive_invalid") from None


def archive_metadata(sid):
    return dict(
        credit_binding(sid, origin="credit_archive"),
        id=sid,
        observation_start=ARCHIVE_START,
        observation_end=ARCHIVE_END,
        range_basis="full pinned CSV actual extent, not the misleading filename years",
        source_publication_at=None,
        source_available_at=None,
    )


def validate_archive(batch):
    instant(batch.observed_at)
    instant(batch.metadata_observed_at)
    if (
        batch.series_id not in CREDIT
        or batch.metadata_observed_at > batch.observed_at
        or not 1 <= batch.attempts <= 3
        or batch.request
        != dict(
            url=ARCHIVE_ROOT + FILES[batch.series_id],
            parameters={},
            local_selection=dict(start=batch.start, end=batch.end),
        )
        or batch.metadata != archive_metadata(batch.series_id)
        or batch.metadata_raw != canonical(batch.metadata).encode()
        or batch.points
        != archive_points(batch.raw, batch.series_id, batch.start, batch.end)
    ):
        raise FinancialError("financial_credit_archive_batch_invalid")


@header_aware
def archive_transport(request, *, on_headers=None):
    if (
        request.url not in {ARCHIVE_ROOT + f for f in FILES.values()}
        or request.parameters
    ):
        raise FinancialError("financial_archive_endpoint_rejected")
    try:
        response = fetch_public(
            request.url,
            "credit_archive",
            timeout=30,
            accept="text/csv",
            on_headers=on_headers,
        )
    except PublicFetchError as error:
        try:
            raise_io(error)
        except PublicFetchError:
            reason = (
                "financial_archive_response_too_large"
                if error.classification == "payload_too_large"
                else "financial_archive_" + error.classification
            )
            raise FinancialError(reason) from None
    return ClaimsResponse(response.status, response.body, response.headers)


class CreditArchiveSource(DailyFredSource):
    def __init__(self, *, transport, **options):
        super().__init__(transport=transport, **options)
        self.cache = {}

    def _validate_wire(self, raw):
        return None

    def fetch(self, sid, start, end):
        spec = credit_binding(sid, origin="credit_archive")
        request = SimpleNamespace(url=spec["source_url"], parameters={})
        if sid not in self.cache:
            self.cache[sid] = self._read(request)
        raw, at, attempts = self.cache[sid]
        meta = archive_metadata(sid)
        return DailyBatch(
            sid,
            start,
            end,
            archive_points(raw, sid, start, end),
            raw,
            canonical(meta).encode(),
            meta,
            at,
            at,
            dict(
                url=request.url,
                parameters={},
                local_selection=dict(start=start, end=end),
            ),
            "credit_archive",
            attempts,
        )

    @classmethod
    def live(cls, *, control_path, live_opt_in=False, **options):
        if live_opt_in is not True:
            raise FinancialError("financial_live_opt_in_required")
        return cls(
            transport=archive_transport,
            request_control=CpiRequestControl(control_path, min_interval_seconds=1.1),
            **options,
        )
