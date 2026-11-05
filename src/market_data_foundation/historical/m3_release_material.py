"""Named Census PDF evidence, separate advance/full identities and bounded cells."""

import re
from io import BytesIO

from pypdf import PdfReader

from market_data_foundation.historical.cpi_timeseries import (
    CpiTimeSeriesError,
    month_at,
    month_number,
)
from market_data_foundation.historical.macro_release_material import (
    _date,
    claim,
    month,
)

SERIES = (
    "durable_goods_new_orders",
    "nondefense_ex_aircraft_capital_goods_new_orders",
    "nondefense_ex_aircraft_capital_goods_shipments",
)


def parse_m3_material(raw, url):
    if type(raw) is not bytes or not 0 < len(raw) <= 2_000_000:
        raise CpiTimeSeriesError("release_material_size_invalid")
    try:
        pages = [
            p.extract_text(extraction_mode="layout") or ""
            for p in PdfReader(BytesIO(raw)).pages
        ]
    except Exception:
        raise CpiTimeSeriesError("m3_official_pdf_invalid") from None
    text = "\n".join(pages)
    normalized = " ".join(text.split())
    full = "MONTHLY FULL REPORT ON MANUFACTURERS" in normalized
    advance = "ADVANCE REPORT ON DURABLE GOODS" in normalized
    if full == advance or ("/prel/" in url) != full:
        raise CpiTimeSeriesError("m3_report_stage_unproven")
    stage = "full" if full else "advance"
    title = re.search(
        r"(?:MONTHLY FULL REPORT ON MANUFACTURERS.{0,120}?ORDERS|"
        r"ADVANCE REPORT ON DURABLE GOODS.{0,120}?ORDERS)\s+(\w+)\s+(\d{4})"
        r"\s+Release Number:\s*(CB\s+\d{2}-\d+\s+M3-\d\s*\(\d+\)-\d{2})",
        normalized,
    )
    if not title:
        raise CpiTimeSeriesError("m3_report_identity_unproven")
    ref = month(title[1], int(title[2]))
    # Header announcement date is a date proof, not an observed publication time.
    released = _date(normalized)
    if ref + "-01" >= released.isoformat():
        raise CpiTimeSeriesError("release_reference_date_conflict")
    prior = month_at(month_number(ref) - 1)
    claims = []
    if full:
        for page_number, page in enumerate(pages, 1):
            if not (
                "Estimates are shown in millions of dollars" in page
                and "Seasonally Adjusted" in page
            ):
                continue
            # Validate date columns before taking the leftmost SA values.
            header = re.search(
                r"\b([A-Z][a-z]+\.)\s+([A-Z][a-z]+\.)\s+([A-Z][a-z]+\.?)\s+", page
            )
            years = re.search(r"\b(\d{4})p\s+(\d{4})r\s+(\d{4})\b", page)
            if (
                not header
                or not years
                or month(header[1], int(years[1])) != ref
                or month(header[2], int(years[2])) != prior
            ):
                continue
            candidates = []
            if "Table 2.  Value of Manufacturers' New Orders" in page:
                candidates += [
                    (SERIES[0], line, "Table 2 / New Orders")
                    for line in page.splitlines()
                    if re.match(r"\s*Durable goods industries", line)
                ]
            if "Table 5.  Value of Manufacturers' Shipments" in page:
                section = None
                lines = page.splitlines()
                for i, line in enumerate(lines):
                    label = line.strip()
                    if label in {
                        "SHIPMENTS",
                        "NEW ORDERS",
                        "UNFILLED ORDERS",
                        "TOTAL INVENTORIES",
                    }:
                        section = label
                    if (
                        "Excluding aircraft" in line
                        and i
                        and "Nondefense capital goods" in lines[i - 1]
                        and section in {"SHIPMENTS", "NEW ORDERS"}
                    ):
                        candidates.append(
                            (
                                SERIES[2] if section == "SHIPMENTS" else SERIES[1],
                                line,
                                "Table 5 / " + section + " / Nondefense capital goods",
                            )
                        )
            for series, line, table in candidates:
                # Missing first cells cannot shift later columns into actual.
                cells = re.match(
                    r"\s*(?:Durable goods industries\d*|Excluding aircraft)"
                    r"[^A-Za-z0-9]*?\s+(\S+)\s+(\S+)\s+(\S+)\s",
                    line,
                )
                if not cells:
                    continue
                for index, period, role in [
                    (0, ref, "actual"),
                    (1, prior, "prior_as_reported_in_this_event"),
                ]:
                    value = cells[index + 1]
                    if not re.fullmatch(r"\d{1,3}(?:,\d{3})+", value):
                        continue
                    claims.append(
                        claim(
                            series,
                            "level",
                            period,
                            value,
                            dict(
                                kind="official_pdf_table_cell",
                                page=page_number,
                                table=table,
                                row=line.strip().split("  ")[0],
                                column=period + " / Seasonally Adjusted",
                                excerpt=" ".join(line.split()),
                            ),
                            role=role,
                            unit="million_USD",
                            footnotes=["p" if index == 0 else "r"],
                        )
                    )
    return dict(
        event_id=f"census:m3:{stage}:{released.isoformat()}",
        family="m3",
        reference_month=ref,
        reference_months=[ref],
        report_stage=stage,
        report_number=title[3],
        actual_at=None,
        source_version=dict(
            value=released.isoformat(),
            precision="date",
            timezone="America/New_York",
            basis="official_material_release_date",
            source=f"Census M3 {stage} report / {title[3]}",
        ),
        source_url=url,
        claims=claims,
        missing_scopes=[],
        missing=[
            "actual_publication_instant_not_proven",
            "official_previous_and_revised_values_not_verified",
            "continuous_version_not_bound_to_material",
            "consensus_source_unavailable",
        ],
        parser_revision="census-m3-named-pdf-v1",
        consensus=None,
        surprise=None,
    )
