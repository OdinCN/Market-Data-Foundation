"""Explicit original-publisher DEV binding; FRED codes are metric aliases only."""

import http.client
import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree as ET

from market_data_foundation.historical.claims_source import ClaimsResponse
from market_data_foundation.historical.cpi_request_control import CpiRequestControl
from market_data_foundation.historical.financial_daily import (
    DailyBatch,
    DailyPoint,
    FinancialError,
    canonical,
    dates,
    day,
    digest,
    instant,
    observation_today,
    series_ids,
)
from market_data_foundation.historical.financial_daily_source import DailyFredSource
from market_data_foundation.historical.public_web_transport import (
    PublicFetchError,
    fetch_public,
    header_aware,
    raise_io,
)

BOARD = "https://www.federalreserve.gov/monetarypolicy/openmarket.htm"
NYFED = "https://markets.newyorkfed.org/api/rates/"
TREASURY = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
DERIVED = "mdf:financial-conditions:treasury-10y-inflation-compensation:r1"
BINDING_ID = "mdf.binding.financial-conditions.origin-dev@r1"
APPROVED = ("DFEDTARL", "DFEDTARU", "EFFR", "SOFR", "DGS2", "DGS10", "DFII10", "T10YIE")
NY_TERMS = "https://www.newyorkfed.org/privacy/termsofuse"
NY_NOTICE = (
    "The {name} is subject to the Terms of Use posted at newyorkfed.org. "
    "The New York Fed is not responsible for publication of the {name} by "
    "Market-Data-Foundation, does not sanction or endorse any particular "
    "republication, and has no liability for your use."
)
NY_AFFILIATION = (
    "Market-Data-Foundation is not affiliated with the New York Fed. The New York "
    "Fed does not sanction, endorse, or recommend any products or services "
    "offered by Market-Data-Foundation."
)


def binding(sid):
    series_ids((sid,))
    if sid in {"BAMLC0A0CM", "BAMLH0A0HYM2"}:
        from market_data_foundation.historical.financial_daily_credit import (
            credit_binding,
        )

        return credit_binding(sid)
    common = dict(
        binding_id=BINDING_ID,
        fred_code_role="metric_mapping_only",
        scope="internal MDF DEV; no third-party distribution",
        unit="Percent",
        source_publication_at=None,
        source_available_at=None,
    )
    if sid not in APPROVED:
        return dict(
            common,
            storage_permitted=False,
            identity="restricted_fred_ice",
            classification="fred_ice_permission_required",
            distributor="FRED",
            original_agency="ICE Data Indices, LLC",
            source_url="https://fred.stlouisfed.org/series/" + sid,
            terms_url="https://fred.stlouisfed.org/legal/",
        )
    if sid.startswith("DFED"):
        return dict(
            common,
            storage_permitted=True,
            origin="federal_reserve_board",
            identity="policy_effective_projection",
            distributor="Federal Reserve Board",
            original_agency="Federal Reserve Board",
            source_url=BOARD,
            terms_url="https://www.federalreserve.gov/disclaimer.htm",
            attribution="Source: Board of Governors of the Federal Reserve System.",
            declared_history_start=None,
            declared_history_end=None,
        )
    if sid in {"EFFR", "SOFR"}:
        name = {
            "EFFR": "Effective Federal Funds Rate (EFFR)",
            "SOFR": "Secured Overnight Financing Rate (SOFR)",
        }[sid]
        return dict(
            common,
            storage_permitted=True,
            origin="new_york_fed",
            identity="original_daily_observation",
            distributor="Federal Reserve Bank of New York",
            original_agency="Federal Reserve Bank of New York",
            source_url=NYFED,
            terms_url=NY_TERMS,
            attribution="Source: Federal Reserve Bank of New York. " + name,
            required_notice=NY_NOTICE.format(name=name),
            affiliation_notice=NY_AFFILIATION,
            publication_schedule=(
                "next business day, normally approximately 09:00 America/New_York "
                "for EFFR; 08:00 for SOFR; not evidence of this row's "
                "actual release instant"
            ),
            declared_history_start=None,
            declared_history_end=None,
        )
    if sid == "T10YIE":
        return dict(
            common,
            storage_permitted=True,
            origin="mdf_derived",
            identity="MDF_derived_not_FRED_T10YIE",
            distributor="Market-Data-Foundation",
            original_agency="Market-Data-Foundation",
            source_url=DERIVED,
            terms_url="https://home.treasury.gov/treasury-daily-interest-rate-xml-feed",
            attribution=(
                "MDF calculation: U.S. Treasury nominal 10Y minus real 10Y, "
                "same observation date and MDF cutoff; not a FRED observation "
                "or pure inflation expectation."
            ),
            declared_history_start=None,
            declared_history_end=None,
        )
    return dict(
        common,
        storage_permitted=True,
        origin="us_treasury",
        identity="original_daily_observation",
        distributor="U.S. Department of the Treasury",
        original_agency="U.S. Department of the Treasury",
        source_url=TREASURY,
        terms_url="https://catalog.data.gov/dataset/"
        + (
            "daily-treasury-real-yield-curve-rates"
            if sid == "DFII10"
            else "interest-rate-statistics-daily-treasury-yield-curve-rates"
        ),
        license="CC0, per official government dataset catalog",
        attribution="Source: U.S. Department of the Treasury, daily par "
        + ("real" if sid == "DFII10" else "nominal")
        + " yield curve rates (percent).",
        declared_history_start="2003-01-01" if sid == "DFII10" else "1990-01-01",
        declared_history_precision="year",
        declared_history_end=None,
    )


class _PolicyTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.year, self.heading, self.cell, self.row, self.records = (
            None,
            None,
            None,
            None,
            [],
        )
        self.in_table = False

    def handle_starttag(self, tag, attrs):
        if tag == "h4":
            self.heading = ""
        if tag == "table":
            self.in_table = True
        if tag == "tr" and self.in_table:
            self.row = []
        if tag == "td" and self.row is not None:
            self.cell = ""

    def handle_data(self, data):
        if self.heading is not None:
            self.heading += data
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag == "h4" and self.heading is not None:
            if re.fullmatch(r"20[0-9]{2}", self.heading.strip()):
                self.year = self.heading.strip()
            self.heading = None
        if tag == "td" and self.cell is not None:
            self.row.append(self.cell.strip())
            self.cell = None
        if tag == "tr" and self.row is not None:
            if self.year and len(self.row) == 4:
                self.records.append((self.year, self.row))
            self.row = None
        if tag == "table":
            self.in_table = False


def number(text):
    if text is None or text == ".":
        return None
    if not isinstance(text, (str, int, float, Decimal)) or isinstance(text, bool):
        raise FinancialError("financial_origin_value_invalid")
    text = str(text)
    if not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", text):
        raise FinancialError("financial_origin_value_invalid")
    return Decimal(text)


def policies(raw):
    parser = _PolicyTable()
    try:
        parser.feed(raw.decode("utf-8"))
        records = []
        for year, row in parser.records:
            label = re.sub(r"[^A-Za-z0-9 ]", "", row[0]).strip()
            effective = (
                datetime.strptime(year + " " + label, "%Y %B %d").date().isoformat()
            )
            level = re.sub(r"\s", "", row[3]).replace("–", "-")
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)-([0-9]+(?:\.[0-9]+)?)", level)
            if match:
                lower, upper = map(Decimal, match.groups())
                if lower > upper:
                    raise ValueError
                records.append((effective, lower, upper))
            elif not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", level):
                raise ValueError
        records.sort()
        if not records or len({r[0] for r in records}) != len(records):
            raise ValueError
        return records
    except ValueError, UnicodeError:
        raise FinancialError("financial_policy_table_invalid") from None


def points(raw, sid, start, end, *, period="month"):
    selected_dates = dates(start, end)
    extra = dict(
        source_publication_at=None, source_available_at=None, binding_id=BINDING_ID
    )
    if sid.startswith("DFED"):
        changes = policies(raw)
        result = []
        for at in selected_dates:
            prior = next((r for r in reversed(changes) if r[0] <= at), None)
            if prior:
                later = next((r[0] for r in changes if r[0] > prior[0]), None)
                result.append(
                    DailyPoint(
                        sid,
                        at,
                        prior[1 if sid == "DFEDTARL" else 2],
                        dict(
                            extra,
                            policy_effective_date=prior[0],
                            policy_effective_until_exclusive=later,
                            policy_announcement_at=None,
                            identity="policy_effective_projection",
                            semantic_identity=prior[0],
                            projection_basis=(
                                "specific Board policy change; not daily observation"
                            ),
                        ),
                    )
                )
        return tuple(result)
    records = []
    try:
        if sid in {"EFFR", "SOFR"}:
            doc = json.loads(raw)
            rows = doc["refRates"]
            if type(rows) is not list:
                raise ValueError
            for row in rows:
                at = row["effectiveDate"]
                day(at)
                if row["type"] != sid or not start <= at <= end:
                    raise ValueError
                records.append(
                    DailyPoint(
                        sid,
                        at,
                        number(row["percentRate"]),
                        dict(
                            extra,
                            revision_indicator=row.get("revisionIndicator"),
                            historical_initial_value_proven=False,
                        ),
                    )
                )
        else:
            root = ET.fromstring(raw)
            if root.tag != "{http://www.w3.org/2005/Atom}feed":
                raise ValueError
            key = {"DGS2": "BC_2YEAR", "DGS10": "BC_10YEAR", "DFII10": "TC_10YEAR"}[sid]
            for node in root.findall(
                ".//{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}properties"
            ):
                row = {child.tag.split("}")[-1]: child for child in node}
                at = (row["NEW_DATE"].text or "")[:10]
                day(at)
                if at[:4] != start[:4] if period == "year" else at[:7] != start[:7]:
                    raise ValueError
                if not start <= at <= end:
                    continue
                # Some official historical entries have only a date/display
                # field. An absent maturity is not zero or an explicit null;
                # retain the other dates and describe this gap in metadata.
                if key not in row:
                    continue
                cell = row[key]
                null = (
                    cell.attrib.get(
                        "{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}null"
                    )
                    == "true"
                )
                if cell.text is None and not null:
                    raise ValueError
                records.append(
                    DailyPoint(
                        sid,
                        at,
                        None if null else number(cell.text),
                        dict(extra, native_field=key),
                    )
                )
        records.sort(key=lambda p: p.observation_date)
        if len({p.observation_date for p in records}) != len(records):
            raise ValueError
        return tuple(records)
    except ValueError, TypeError, KeyError, ET.ParseError:
        raise FinancialError("financial_origin_response_invalid") from None


def request_for(sid, start, end, *, period="month"):
    series_ids((sid,))
    dates(start, end)
    if sid.startswith("DFED"):
        return dict(url=BOARD, parameters={})
    if sid in {"EFFR", "SOFR"}:
        category = "unsecured" if sid == "EFFR" else "secured"
        return dict(
            url=NYFED + category + "/" + sid.lower() + "/search.json",
            parameters=dict(startDate=start, endDate=end),
        )
    if sid in {"DGS2", "DGS10", "DFII10"}:
        if start[:4] != end[:4] if period == "year" else start[:7] != end[:7]:
            raise FinancialError("financial_origin_one_month_window_required")
        return dict(
            url=TREASURY,
            parameters=dict(
                data="daily_treasury_real_yield_curve"
                if sid == "DFII10"
                else "daily_treasury_yield_curve",
                **(
                    {"field_tdr_date_value": start[:4]}
                    if period == "year"
                    else {"field_tdr_date_value_month": start[:7].replace("-", "")}
                ),
            ),
        )
    raise FinancialError("financial_origin_not_direct")


def metadata(sid, parsed, *, raw=None, start=None, end=None):
    result = dict(
        binding(sid),
        id=sid,
        frequency="Daily, 7-Day" if sid.startswith("DFED") else "Daily",
        seasonal_adjustment="NSA",
        declared_history_end=None,
        response_first_observation=parsed[0].observation_date if parsed else None,
        response_last_observation=parsed[-1].observation_date if parsed else None,
        range_basis="response coverage only; not full provider history",
    )
    if raw is not None and sid in {"DGS2", "DGS10", "DFII10"}:
        key = {"DGS2": "BC_2YEAR", "DGS10": "BC_10YEAR", "DFII10": "TC_10YEAR"}[sid]
        absent = []
        for node in ET.fromstring(raw).findall(
            ".//{http://schemas.microsoft.com/ado/2007/08/"
            "dataservices/metadata}properties"
        ):
            row = {child.tag.split("}")[-1]: child for child in node}
            at = (row["NEW_DATE"].text or "")[:10]
            if key not in row and (start is None or start <= at <= end):
                absent.append(at)
        if absent:
            result["native_field_absent_dates"] = sorted(set(absent))
            result["absent_native_field"] = key
    return result


def validate(batch):
    series_ids((batch.series_id,))
    dates(batch.start, batch.end)
    instant(batch.observed_at)
    instant(batch.metadata_observed_at)
    if batch.observed_at < batch.metadata_observed_at or not 1 <= batch.attempts <= 3:
        raise FinancialError("financial_origin_batch_invalid")
    if batch.series_id == "T10YIE":
        parsed = derived_points(batch.raw, batch.start, batch.end)
        cutoff = instant(datetime.fromisoformat(json.loads(batch.raw)["cutoff"]))
        if cutoff > batch.observed_at:
            raise FinancialError("financial_derived_future_cutoff_rejected")
        expected_request = dict(
            url=DERIVED, parameters=dict(start=batch.start, end=batch.end)
        )
    else:
        period = (
            "year"
            if "field_tdr_date_value" in batch.request.get("parameters", {})
            else "month"
        )
        parsed = points(
            batch.raw, batch.series_id, batch.start, batch.end, period=period
        )
        expected_request = request_for(
            batch.series_id, batch.start, batch.end, period=period
        )
    expected_meta = metadata(
        batch.series_id, parsed, raw=batch.raw, start=batch.start, end=batch.end
    )
    if (
        not binding(batch.series_id)["storage_permitted"]
        or batch.origin != binding(batch.series_id)["origin"]
        or batch.request != expected_request
        or batch.points != parsed
        or batch.metadata != expected_meta
        or batch.metadata_raw != canonical(expected_meta).encode()
    ):
        raise FinancialError("financial_origin_batch_invalid")


@header_aware
def direct_transport(request, *, on_headers=None):
    u = urlsplit(request.url)
    if (
        u.scheme != "https"
        or u.username
        or u.port
        or u.query
        or u.fragment
        or u.hostname
        not in {"www.federalreserve.gov", "markets.newyorkfed.org", "home.treasury.gov"}
    ):
        raise FinancialError("financial_origin_endpoint_rejected")
    if u.hostname != "markets.newyorkfed.org":
        url = request.url + (
            "?" + urlencode(request.parameters) if request.parameters else ""
        )
        try:
            response = fetch_public(
                url,
                "board" if u.hostname == "www.federalreserve.gov" else "treasury",
                timeout=30,
                accept="text/html"
                if u.hostname == "www.federalreserve.gov"
                else "application/xml",
                on_headers=on_headers,
            )
        except PublicFetchError as error:
            try:
                raise_io(error)
            except PublicFetchError:
                reason = (
                    "financial_origin_response_too_large"
                    if error.classification == "payload_too_large"
                    else "financial_origin_" + error.classification
                )
                raise FinancialError(reason) from None
        return ClaimsResponse(response.status, response.body, response.headers)
    # Existing NY Fed numeric JSON API: keep its headers and wire contract.
    c = http.client.HTTPSConnection(u.hostname, timeout=30)
    try:
        c.request(
            "GET",
            u.path
            + ("?" + urlencode(request.parameters) if request.parameters else ""),
            headers={
                "Accept-Encoding": "identity",
                "User-Agent": "MDF-DEV financial-conditions research",
            },
        )
        r = c.getresponse()
        if r.getheader("Content-Encoding", "identity").lower() != "identity":
            raise FinancialError("financial_origin_encoding_rejected")
        raw = r.read(2_000_001)
        if len(raw) > 2_000_000:
            raise FinancialError("financial_origin_response_too_large")
        headers = dict(r.getheaders())
        if on_headers is not None:
            on_headers(r.status, headers)
        return ClaimsResponse(r.status, raw, headers)
    finally:
        c.close()


class OriginSource(DailyFredSource):
    is_origin_binding = True

    def __init__(
        self,
        *,
        controls,
        transport,
        raw_sink=None,
        clock=lambda: datetime.now(UTC),
        sleep=time.sleep,
        max_requests=20,
    ):
        super().__init__(
            transport=transport,
            request_control=None,
            raw_sink=raw_sink,
            clock=clock,
            sleep=sleep,
            max_requests=20,
        )
        self.controls, self.cache = controls, {}
        if type(max_requests) is not int or not 1 <= max_requests <= 160:
            raise FinancialError("financial_request_budget_invalid")
        self.max_requests = max_requests

    def fetch(self, sid, start, end, *, period="month", policy_span=False):
        limit = (
            366 * 31
            if policy_span and sid.startswith("DFED")
            else 366
            if period == "year"
            else 31
        )
        if period not in {"month", "year"} or len(dates(start, end)) > limit:
            raise FinancialError("financial_origin_bounded_window_exceeded")
        source_day = observation_today(self.clock())
        if day(start) > source_day:
            # No historical subwindow exists yet; never manufacture future raw bytes.
            raise FinancialError("financial_origin_future_window_not_acquired")
        end = min(day(end), source_day).isoformat()
        spec = binding(sid)
        if not spec["storage_permitted"]:
            raise FinancialError(spec["classification"])
        req = request_for(sid, start, end, period=period)
        key = canonical(req)
        if key not in self.cache:
            from types import SimpleNamespace

            self._control = self.controls[urlsplit(req["url"]).hostname]
            # _read retains validated HTTP bytes before semantic parsing; unlike
            # FRED, original publishers also return HTML/XML, not only JSON.
            self.cache[key] = self._read(SimpleNamespace(**req))
        raw, at, attempts = self.cache[key]
        parsed = points(raw, sid, start, end, period=period)
        meta = metadata(sid, parsed, raw=raw, start=start, end=end)
        return DailyBatch(
            sid,
            start,
            end,
            parsed,
            raw,
            canonical(meta).encode(),
            meta,
            at,
            at,
            req,
            spec["origin"],
            attempts,
        )

    def _validate_wire(self, raw):
        return None

    @classmethod
    def live(cls, *, control_directory, live_opt_in=False, **options):
        if live_opt_in is not True:
            raise FinancialError("financial_live_opt_in_required")
        return cls(
            controls={
                host: CpiRequestControl(
                    control_directory / (host + ".json"), min_interval_seconds=1.1
                )
                for host in [
                    "www.federalreserve.gov",
                    "markets.newyorkfed.org",
                    "home.treasury.gov",
                ]
            },
            transport=direct_transport,
            **options,
        )


def derived_points(raw, start, end):
    try:
        doc = json.loads(raw)
        if doc["formula"] != "DGS10 - DFII10" or doc["binding_id"] != BINDING_ID:
            raise ValueError
        cutoff = instant(datetime.fromisoformat(doc["cutoff"]))
        result = []
        for item in doc["observations"]:
            at = item["date"]
            if at not in dates(start, end):
                raise ValueError
            legs = item["inputs"]
            if [r["series_id"] for r in legs] != ["DGS10", "DFII10"]:
                raise ValueError
            for leg in legs:
                if (
                    leg["observation_date"] != at
                    or leg["state"] != "observed"
                    or leg["origin"] != "us_treasury"
                    or type(leg["revision"]) is not int
                    or leg["revision"] < 1
                    or instant(datetime.fromisoformat(leg["available_at"])) > cutoff
                    or number(leg["value"]) is None
                ):
                    raise ValueError
            value = number(legs[0]["value"]) - number(legs[1]["value"])
            identity = digest(
                canonical(
                    [
                        {
                            k: leg[k]
                            for k in (
                                "series_id",
                                "observation_date",
                                "revision",
                                "batch_id",
                            )
                        }
                        for leg in legs
                    ]
                ).encode()
            )
            result.append(
                DailyPoint(
                    "T10YIE",
                    at,
                    value,
                    dict(
                        identity="MDF_derived_not_FRED_T10YIE",
                        inputs=legs,
                        cutoff=doc["cutoff"],
                        semantic_identity=identity,
                        source_publication_at=None,
                        source_available_at=None,
                    ),
                )
            )
        if len({p.observation_date for p in result}) != len(result):
            raise ValueError
        return tuple(sorted(result, key=lambda p: p.observation_date))
    except ValueError, KeyError, TypeError:
        raise FinancialError("financial_derived_inputs_invalid") from None


def derive(readback, *, observed_at=None):
    start, end = (
        min(r["observation_date"] for r in readback["rows"]),
        max(r["observation_date"] for r in readback["rows"]),
    )
    by = {(r["series_id"], r["observation_date"]): r for r in readback["rows"]}
    records = []
    for at in dates(start, end):
        legs = [by.get((sid, at)) for sid in ("DGS10", "DFII10")]
        if all(
            r and r["state"] == "observed" and r["origin"] == "us_treasury"
            for r in legs
        ):
            records.append(dict(date=at, inputs=legs))
    raw = canonical(
        dict(
            binding_id=BINDING_ID,
            formula="DGS10 - DFII10",
            cutoff=readback["as_of"],
            observations=records,
        )
    ).encode()
    parsed = derived_points(raw, start, end)
    meta = metadata("T10YIE", parsed)
    at = instant(observed_at or datetime.now(UTC))
    return DailyBatch(
        "T10YIE",
        start,
        end,
        parsed,
        raw,
        canonical(meta).encode(),
        meta,
        at,
        at,
        dict(url=DERIVED, parameters=dict(start=start, end=end)),
        "mdf_derived",
    )
