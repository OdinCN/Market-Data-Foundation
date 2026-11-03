"""National JOLTS event scope; numeric claims await verified table mappings."""

import re
from datetime import date

from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
from market_data_foundation.historical.jolts_contract import METRICS
from market_data_foundation.historical.macro_release_material import (
    Material,
    _date,
    month,
)


def parse_jolts_material(raw, url):
    material = Material(raw)
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
    embargo = material.text.lower().find("embargoed until")
    if embargo < 0 or _date(material.text[embargo : embargo + 500]) != released:
        raise CpiTimeSeriesError("release_archive_header_date_conflict")
    # A document's national title/SA phrase cannot establish the scope of a
    # particular number. Prose can describe NSA, earlier periods, industries or
    # regions. Until a retained official table has a qualified mapping of its
    # headers and cells, preserve event scope without manufacturing actuals.
    claims = []
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
            "jolts_statistical_tables_not_parsed",
            "jolts_narrative_numeric_scope_not_verified",
            "official_previous_and_revised_values_not_verified",
            "continuous_version_not_bound_to_material",
            "consensus_source_unavailable",
        ],
        parser_revision="national-jolts-scope-v2",
        consensus=None,
        surprise=None,
    )
