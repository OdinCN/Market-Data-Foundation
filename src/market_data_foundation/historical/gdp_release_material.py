"""Quarterly GDP release evidence; current NIPA values are never rebound here."""

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from market_data_foundation.historical.cpi_timeseries_store import canonical, digest
from market_data_foundation.historical.gdp_contract import (
    METRICS,
    quarter_start,
    shift_quarter,
)
from market_data_foundation.historical.gdp_timeseries import GdpTimeSeriesError
from market_data_foundation.historical.macro_release_material import Material, _date

PARSER = "bea-gdp-release-summary-v1"
ESTIMATES = ("advance", "second", "third")
ORDINALS = ("first", "second", "third", "fourth")


class _Material(Material):
    """Assign distinct table identities even when the official HTML omits ids."""

    def __init__(self, raw):
        self.number = 0
        self.headings, self.heading = [], None
        super().__init__(raw)

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.number += 1
            attrs = [(k, v) for k, v in attrs if k != "id"]
            attrs.append(("id", f"table-{self.number}"))
        if tag == "h1":
            self.heading = []
        super().handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self.heading is not None and not self.hidden:
            self.heading.append(data)
        super().handle_data(data)

    def handle_endtag(self, tag):
        if tag == "h1" and self.heading is not None:
            self.headings.append(" ".join(" ".join(self.heading).split()))
            self.heading = None
        super().handle_endtag(tag)


def _error(reason):
    return GdpTimeSeriesError("gdp_release_" + reason)


def _value(text):
    try:
        value = Decimal(text.replace(",", "").replace("−", "-"))
        if not value.is_finite():
            raise ValueError
        return str(value)
    except InvalidOperation, AttributeError, ValueError:
        if text in {"…", "...", "—", "--", "(NA)", "NA"}:
            return None
        raise _error("numeric_value_invalid") from None


def _claim(reference, value, role, estimate, table, row, column):
    scope = dict(
        series_id="A191RL",
        reference_quarter=reference,
        statistic="official_annualized_qoq",
        unit="percent",
        adjustment="SA",
        role=role,
        estimate_kind=estimate,
    )
    parsed = _value(value)
    return dict(
        **scope,
        value=parsed,
        value_identity=digest(canonical({**scope, "value": parsed}).encode()),
        confirmation_state="confirmed" if parsed is not None else "unavailable",
        continuous_version=None,
        locator=dict(
            kind="official_table_cell",
            table=table,
            row="Real GDP",
            column=column,
            excerpt=" | ".join(row),
        ),
    )


def parse_release(raw, source_url):
    """Admit only explicit official GDP summary tables and date-level evidence."""
    try:
        url = urlsplit(source_url)
        path = re.fullmatch(
            r"/news/(\d{4})/gdp-(advance|second|third)-estimate(?:-[a-z0-9]+)*",
            url.path,
        )
        if (
            url.scheme != "https"
            or url.netloc != "www.bea.gov"
            or url.query
            or url.fragment
            or path is None
        ):
            raise _error("official_archive_url_required")
        material = _Material(raw)
        titles = [
            match
            for title in material.headings
            if (
                match := re.fullmatch(
                    r"GDP \((Advance|Second|Third) Estimate\).*?\b"
                    r"([1-4])(?:st|nd|rd|th) Quarter (\d{4})(?:;.*)?",
                    title,
                )
            )
        ]
        if len(titles) != 1:
            raise _error("title_ambiguous_or_missing")
        title = titles[0]
        kind = title[1].lower()
        reference = f"{title[3]}-Q{title[2]}"
        if kind != path[2]:
            raise _error("url_title_conflict")
        slug = re.search(r"([1-4])(?:st|nd|rd|th)-quarter-(\d{4})", url.path)
        if slug and f"{slug[2]}-Q{slug[1]}" != reference:
            raise _error("url_title_conflict")
        declarations = re.findall(
            r"EMBARGOED UNTIL RELEASE AT (.{1,180}?) BEA \d{2}",
            material.text,
        )
        if len(declarations) != 1:
            raise _error("release_date_ambiguous_or_missing")
        released = _date(declarations[0])
        if released.year != int(path[1]) or released < quarter_start(
            shift_quarter(reference, 1)
        ):
            raise _error("reference_date_conflict")
    except GdpTimeSeriesError:
        raise
    except Exception:
        raise _error("material_invalid") from None

    claims, matched = [], 0
    for table, parts in material.table_text.items():
        context = " ".join(" ".join(parts).split())
        rows = [row for key, row in material.rows if key == table]
        period = re.search(
            r"Percent change \(SAAR\) from (\d{4}):Q([1-4]) to (\d{4}):Q([1-4])",
            context,
            re.I,
        )
        if not period:
            continue
        target = f"{period[3]}-Q{period[4]}"
        if target != reference:
            continue
        if f"{period[1]}-Q{period[2]}" != shift_quarter(reference, -1):
            raise _error("table_period_invalid")
        if "Real GDP and Related Measures" not in context:
            continue
        headers = [
            row
            for row in rows
            if len(row) >= 2
            and all(
                value.lower() in {k + " estimate" for k in ESTIMATES}
                for value in row[1:]
            )
        ]
        values = [row for row in rows if row[0] == "Real GDP"]
        if len(headers) != 1 or len(values) != 1:
            raise _error("estimate_columns_ambiguous")
        header, row = headers[0], values[0]
        kinds = [value.lower().removesuffix(" estimate") for value in header[1:]]
        if (
            len(row) != len(header)
            or not kinds
            or kinds[-1] != kind
            or len(set(kinds)) != len(kinds)
            or kinds != sorted(kinds, key=ESTIMATES.index)
        ):
            raise _error("estimate_columns_invalid")
        matched += 1
        for i, estimate in enumerate(kinds, 1):
            claims.append(
                _claim(
                    reference,
                    row[i],
                    "actual" if estimate == kind else "previous_estimate",
                    estimate,
                    table,
                    row,
                    header[i],
                )
            )
    if matched != 1:
        raise _error("target_summary_ambiguous_or_missing")

    # Annual updates can also report explicit old/new values for earlier quarters.
    # These are prior-quarter revisions, not previous estimates of the target quarter.
    for table, parts in material.table_text.items():
        context = " ".join(" ".join(parts).split())
        prior = re.match(
            r"(First|Second|Third|Fourth) Quarter (\d{4})\b", context, re.I
        )
        if prior is None:
            continue
        q = f"{prior[2]}-Q{ORDINALS.index(prior[1].lower()) + 1}"
        if q >= reference:
            continue
        rows = [row for key, row in material.rows if key == table]
        if not any(
            row[-2:] == ["Previous Estimate", "Revised Estimate"] for row in rows
        ):
            continue
        if not re.search(
            r"Percent change \(SAAR\) from preceding quarter", context, re.I
        ):
            continue
        values = [row for row in rows if row[0] == "Real GDP"]
        if len(values) != 1 or len(values[0]) != 3:
            raise _error("prior_revision_columns_ambiguous")
        row = values[0]
        for i, role in ((1, "prior_original"), (2, "prior_revised")):
            claims.append(
                _claim(
                    q,
                    row[i],
                    role,
                    "unknown",
                    table,
                    row,
                    ("Previous", "Revised")[i - 1],
                )
            )

    actual = next(c for c in claims if c["role"] == "actual")
    previous = [
        c
        for c in claims
        if c["role"] == "previous_estimate" and c["reference_quarter"] == reference
    ]
    previous = previous[-1] if previous else None
    delta = (
        str(Decimal(actual["value"]) - Decimal(previous["value"]))
        if previous and actual["value"] is not None and previous["value"] is not None
        else None
    )
    return dict(
        event_id=f"bea:gdp:{released.isoformat()}:{reference}:{kind}",
        family="gdp",
        reference_quarter=reference,
        reference_quarters=sorted({c["reference_quarter"] for c in claims}),
        estimate_kind=kind,
        actual_at=None,
        source_version=dict(
            value=released.isoformat(),
            precision="date",
            timezone="America/New_York",
            basis="official_material_release_date",
            source="BEA GDP official release archive",
        ),
        source_url=source_url,
        parser_revision=PARSER,
        claims=claims,
        previous_estimate_revision_pp=delta,
        consensus=None,
        surprise=None,
        surprise_missing=(
            ["consensus_source_unavailable"]
            if actual["value"] is not None
            else ["event_actual_unconfirmed", "consensus_source_unavailable"]
        ),
        missing=["actual_publication_instant_not_proven"],
        missing_scopes=[
            dict(series_id=m.series_id, reason="not_covered_by_summary_adapter")
            for m in METRICS
            if m.series_id != "A191RL"
        ],
    )
