"""National JOLTS evidence; admit only matching SA tables and total rows."""

import re
from datetime import date

from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
from market_data_foundation.historical.jolts_contract import METRICS
from market_data_foundation.historical.macro_release_material import (
    Material,
    _date,
    claim,
    month,
    table_locator,
)


def parse_jolts_material(raw, url):
    try:
        material = Material(raw)
    except CpiTimeSeriesError as error:
        if error.classification != "release_material_encoding_invalid":
            raise
        # BLS archive bodies include legacy Windows punctuation. The retained
        # evidence stays byte-exact; only the parser input is normalized.
        try:
            material = Material(raw.decode("windows-1252").encode("utf-8"))
        except UnicodeError:
            raise CpiTimeSeriesError("release_material_encoding_invalid") from None
    title = re.search(
        r"JOB OPENINGS AND LABOR TURNOVER\s*[-–—]+\s*(\w+)\s+(\d{4})",
        material.text,
        re.I,
    )
    stamp = re.search(r"/archives/jolts_(\d{2})(\d{2})(\d{4})\.(?:htm|txt)$", url)
    if (
        not title
        or not stamp
        or re.search(
            r"STATE JOB OPENINGS AND LABOR TURNOVER\s*[-–—]", material.text, re.I
        )
    ):
        raise CpiTimeSeriesError("jolts_release_identity_unproven")
    released = date(int(stamp[3]), int(stamp[1]), int(stamp[2]))
    ref = month(title[1], int(title[2]))
    if ref + "-01" >= released.isoformat():
        raise CpiTimeSeriesError("release_reference_date_conflict")
    header = re.search(r"\b(?:embargoed until|For release)\b", material.text, re.I)
    if (
        not header
        or _date(material.text[header.end() : header.end() + 500]) != released
    ):
        raise CpiTimeSeriesError("release_archive_header_date_conflict")
    # Prose can describe NSA, earlier periods, industries or regions. Only
    # qualified table captions, units, dates and national rows admit numbers.
    claims = []
    from market_data_foundation.historical.cpi_timeseries import month_at, month_number

    prior = month_at(month_number(ref) - 1)
    for number, indicator, code in [
        (1, "Job openings", "JO"),
        (2, "Hires", "HI"),
        (4, "Quits", "QU"),
        (5, "Layoffs and discharges", "LD"),
    ]:
        table = f"jolts_table{number}"
        caption = " ".join(material.table_text.get(table, [])).split()
        caption = " ".join(caption)
        expected = (
            f"Table {number}. {indicator} levels and rates by industry and region, "
            "seasonally adjusted"
        )
        rows = [row for identity, row in material.rows if identity == table]
        if (
            not caption.startswith(expected)
            or len(rows) < 3
            or rows[0]
            != ["Industry and region", "Levels (in thousands)", "Rates ( 2 )"]
        ):
            continue
        headers = rows[1]
        if len(headers) != 12 or headers[:6] != headers[6:]:
            continue
        periods = []
        for column in headers[:6]:
            match = re.fullmatch(r"([A-Za-z]+\.?) (\d{4})(?: \( p \))?", column)
            periods.append(month(match[1], int(match[2])) if match else None)
        totals = [r for r in rows if r[0] == "Total"]
        if len(totals) != 1 or len(totals[0]) != 13 or periods.count(ref) != 1:
            continue
        for period, role in [
            (ref, "actual"),
            (prior, "prior_as_reported_in_this_event"),
        ]:
            if periods.count(period) != 1:
                continue
            index = periods.index(period)
            for kind, offset in [("L", 1), ("R", 7)]:
                metric = next(m for m in METRICS if m.series_id.endswith(code + kind))
                claims.append(
                    claim(
                        metric.series_id,
                        "level",
                        period,
                        totals[0][index + offset],
                        table_locator(table, totals[0], headers[index]),
                        role=role,
                        unit=metric.value_unit,
                        footnotes=["p"] if "( p )" in headers[index] else [],
                    )
                )
    scopes = [
        dict(
            series_id=m.series_id,
            reference_month=ref,
            statistic="level",
            unit=m.value_unit,
            adjustment="sa",
            reason="jolts_statistic_not_verified_in_material",
        )
        for m in METRICS
        if not any(
            c["series_id"] == m.series_id and c["role"] == "actual" for c in claims
        )
    ]
    return dict(
        event_id="bls:jolts:" + released.isoformat(),
        family="jolts",
        reference_month=ref,
        reference_months=[ref],
        actual_at=None,
        source_version=dict(
            value=released.isoformat(),
            precision="date",
            timezone="America/New_York",
            basis="BLS_official_archive_filename",
            source="BLS national JOLTS archive",
        ),
        event_time_evidence=dict(
            value=released.isoformat(),
            precision="date",
            timezone="America/New_York",
            basis="BLS_archive_header_and_filename",
        ),
        source_url=url,
        claims=claims,
        missing_scopes=scopes,
        missing=[
            "actual_publication_instant_not_proven",
            *(["jolts_statistical_tables_not_parsed"] if scopes else []),
            *(["jolts_narrative_numeric_scope_not_verified"] if scopes else []),
            "official_previous_and_revised_values_not_verified",
            "continuous_version_not_bound_to_material",
            "consensus_source_unavailable",
        ],
        parser_revision="national-jolts-tables-v3",
        consensus=None,
        surprise=None,
    )
