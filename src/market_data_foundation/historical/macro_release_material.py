"""Official material adapters. Columns are admitted only with matching table identity.

Retained projections are input evidence, never described as original HTTP bodies.
No network, storage, calendar extrapolation, or continuous-value writes here.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlsplit

from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
from market_data_foundation.historical.cpi_timeseries_store import canonical, digest

MONTHS = (
    "January February March April May June July August September "
    "October November December"
).split()


class Material(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        if type(raw) is not bytes or not 0 < len(raw) <= 2_000_000:
            raise CpiTimeSeriesError("release_material_size_invalid")
        self.rows, self.parts, self.pre = [], [], []
        self.table_text = {}
        self.inside_table = False
        self.table, self.row, self.cell, self.hidden, self.in_pre = (
            "",
            None,
            None,
            0,
            False,
        )
        try:
            self.feed(raw.decode("utf-8-sig"))
        except UnicodeError:
            raise CpiTimeSeriesError("release_material_encoding_invalid") from None
        self.text = " ".join(" ".join(self.parts).split())

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag == "table":
            self.table = dict(attrs).get("id", "")
            self.inside_table = True
            self.table_text.setdefault(self.table, [])
        if tag == "tr":
            self.row = []
        if tag in {"td", "th"}:
            self.cell = []
        if tag == "pre":
            self.in_pre = True

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.inside_table:
                self.table_text[self.table].append(data)
            if self.cell is not None:
                self.cell.append(data)
            if self.in_pre:
                self.pre.append(data)

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"td", "th"} and self.cell is not None:
            if self.row is not None:
                self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        if tag == "tr" and self.row:
            self.rows.append((self.table, self.row))
            self.row = None
        if tag == "pre":
            self.in_pre = False
        if tag == "table":
            self.inside_table = False


def month(value, year):
    name = re.sub(r"[^A-Za-z]", "", value).lower()
    matches = [i for i, m in enumerate(MONTHS, 1) if m.lower().startswith(name)]
    if len(matches) != 1:
        raise CpiTimeSeriesError("release_reference_month_invalid")
    return f"{year:04d}-{matches[0]:02d}"


def claim(
    series,
    statistic,
    reference,
    value,
    locator,
    *,
    role="actual",
    unit="percent",
    adjustment="sa",
    footnotes=None,
):
    try:
        parsed = Decimal(value.replace(",", "").replace("$", "").replace("−", "-"))
        value = str(parsed) if parsed.is_finite() else None
    except InvalidOperation, AttributeError:
        value = None
    scope = dict(
        series_id=series,
        statistic=statistic,
        reference_month=reference,
        unit=unit,
        adjustment=adjustment,
    )
    return dict(
        **scope,
        role=role,
        value=value,
        value_identity=digest(canonical({**scope, "value": value}).encode()),
        continuous_version=None,
        locator=locator,
        footnotes=footnotes or [],
    )


def table_locator(table, row, column):
    return dict(
        kind="official_table_cell",
        table=table,
        row=row[0],
        column=column,
        excerpt=" | ".join(row),
    )


def _date(text):
    match = re.search(r"(" + "|".join(MONTHS) + r")\s+(\d{1,2}),\s*(\d{4})", text, re.I)
    if not match:
        raise CpiTimeSeriesError("release_source_date_missing")
    return date(int(match[3]), MONTHS.index(match[1].title()) + 1, int(match[2]))


def official_url(family, url):
    p = urlsplit(url)
    patterns = {
        "employment": (
            "www.bls.gov",
            r"/news.release/archives/empsit_\d{8}\.(?:htm|txt)",
        ),
        "cpi": ("www.bls.gov", r"/news.release/archives/cpi_\d{8}\.(?:htm|txt)"),
        "jolts": ("www.bls.gov", r"/news.release/archives/jolts_\d{8}\.(?:htm|txt)"),
        "ppi": ("www.bls.gov", r"/news.release/archives/ppi_\d{8}\.(?:htm|txt)"),
        "pce": (
            "www.bea.gov",
            r"/news/\d{4}/personal-income-and-outlays-[a-z-]+-\d{4}",
        ),
        "retail": ("www.census.gov", r"/retail/sales.html"),
        "m3": (
            "www.census.gov",
            r"/manufacturing/m3/(?:prel/pdf/s-i-o|adv/pdf/durgd)\.pdf",
        ),
    }
    host, path = patterns["pce" if family == "consumption" else family]
    if (
        p.scheme != "https"
        or p.netloc != host
        or p.query
        or p.fragment
        or not re.fullmatch(path, p.path)
    ):
        raise CpiTimeSeriesError(f"{family}_official_archive_url_required")
    return p


def parse_material(raw, url, family):
    p = official_url(family, url)
    if family == "m3":
        from market_data_foundation.historical.m3_release_material import (
            parse_m3_material,
        )

        doc = parse_m3_material(raw, url)
        _scope_absence(doc, family)
        return doc
    if family == "jolts":
        from market_data_foundation.historical.jolts_release_material import (
            parse_jolts_material,
        )

        return parse_jolts_material(raw, url)
    m = Material(raw)
    if family == "employment":
        from market_data_foundation.historical.macro_release_records import (
            parse_employment_release,
        )

        doc = parse_employment_release(raw, url)
        doc["missing"] = [
            r
            for r in doc["missing"]
            if r
            not in {
                "other_employment_statistics_not_covered_by_nfp_narrative_adapter",
                "mdf_pre_release_value_requires_proven_release_instant",
            }
        ]
        _employment(m, doc)
    else:
        if family in {"cpi", "ppi"}:
            stamp = re.search(r"_(\d{2})(\d{2})(\d{4})\.", p.path)
            released = date(int(stamp[3]), int(stamp[1]), int(stamp[2]))
            title = re.search(
                (
                    r"CONSUMER PRICE INDEX"
                    if family == "cpi"
                    else r"PRODUCER PRICE INDEXES"
                )
                + r"\s*[-–—]+\s*(\w+)\s+(\d{4})",
                m.text,
                re.I,
            )
        elif family in {"pce", "consumption"}:
            released = _date(m.text[m.text.find("EMBARGOED UNTIL") :])
            names = "(?:" + "|".join(MONTHS) + ")"
            clause = (
                rf"{names}\s+\d{{4}}\s+and\s+{names}\s+\d{{4}}|"
                rf"{names}(?:\s+and\s+{names})?\s+\d{{4}}"
            )
            title = re.search(
                r"Personal Income and Outlays,\s*(" + clause + ")", m.text, re.I
            )
        else:
            released = _date(m.text[m.text.find("FOR IMMEDIATE RELEASE") :])
            title = re.search(
                (
                    r"Advance estimates of U.S. retail and food services sales "
                    r"for (\w+) (\d{4})"
                ),
                m.text,
            )
        if not title:
            raise CpiTimeSeriesError("release_reference_month_missing")
        if family in {"pce", "consumption"}:
            from market_data_foundation.historical.pce_release_schedule import (
                _reference_months,
            )

            covered = list(_reference_months(title[0]))
        else:
            covered = [month(title[1], int(title[2]))]
        ref = max(covered)
        prefix = {
            "cpi": "bls:cpi",
            "ppi": "bls:ppi",
            "pce": "bea:personal-income-and-outlays",
            "consumption": "bea:personal-income-and-outlays",
            "retail": "census:advance-retail",
        }[family]
        doc = dict(
            event_id=f"{prefix}:{released.isoformat()}",
            family=family,
            reference_month=ref,
            actual_at=None,
            source_version=dict(
                value=released.isoformat(),
                precision="date",
                timezone="America/New_York",
                basis="BLS_official_archive_filename"
                if family in {"cpi", "ppi"}
                else "official_material_release_date",
                source={
                    "cpi": "BLS CPI archive",
                    "ppi": "BLS PPI archive",
                    "pce": "BEA Personal Income and Outlays archive",
                    "consumption": "BEA Personal Income and Outlays archive",
                    "retail": "Census retained release material",
                }[family],
            ),
            source_url=url,
            claims=[],
            reference_months=covered,
            missing=["actual_publication_instant_not_proven"],
            consensus=None,
            surprise=None,
        )
        {
            "cpi": _cpi,
            "ppi": _ppi,
            "pce": _pce,
            "consumption": _consumption,
            "retail": _retail,
        }[family](m, doc)
    unavailable = [c for c in doc["claims"] if c["value"] is None]
    doc["missing_scopes"] = [
        dict(c, reason="official_statistic_null_suppressed_or_unavailable")
        for c in unavailable
    ]
    doc["claims"] = [c for c in doc["claims"] if c["value"] is not None]
    if unavailable:
        doc["missing"].append("some_official_statistics_unavailable_in_material")
    if doc["reference_month"] + "-01" >= doc["source_version"]["value"]:
        raise CpiTimeSeriesError("release_reference_date_conflict")
    if family in {"employment", "cpi", "ppi"} and "embargoed until" in m.text.lower():
        declared = _date(m.text[m.text.lower().find("embargoed until") :][:500])
        if declared.isoformat() != doc["source_version"]["value"]:
            raise CpiTimeSeriesError("release_archive_header_date_conflict")
    if family in {"pce", "consumption"}:
        expected = re.sub(r"[^a-z0-9]+", "-", title[0].lower()).strip("-")
        if p.path != f"/news/{released.year}/{expected}":
            raise CpiTimeSeriesError("release_material_url_title_conflict")
    if not any(c["role"] == "actual" for c in doc["claims"]):
        doc["missing"].append(f"{family}_current_scope_not_found_in_material")
    doc["parser_revision"] = (
        "official-consumption-income-v1"
        if family == "consumption"
        else "official-five-family-v3"
    )
    doc["reference_months"] = sorted(
        {
            *doc.get("reference_months", []),
            doc["reference_month"],
            *(c["reference_month"] for c in doc["claims"] if c["role"] == "actual"),
        }
    )
    doc["event_time_evidence"] = dict(
        value=doc["source_version"]["value"],
        precision="date",
        basis=doc["source_version"]["basis"],
        timezone="America/New_York",
    )
    doc["missing"] += [
        "consensus_source_unavailable",
        "continuous_version_not_bound_to_material",
    ]
    for item in doc["claims"]:
        item["value_basis"] = "official_material_reported_statistic"
        item["publisher_reported_rate"] = item["statistic"] in {"mom_pct", "yoy_pct"}
    _scope_absence(doc, family)
    doc["missing"] = list(dict.fromkeys(doc["missing"]))
    return doc


def _employment(m, doc):
    ref, claims = doc["reference_month"], doc["claims"]
    mapping = {
        "Participation rate": "LNS11300000",
        "Employment-population ratio": "LNS12300000",
        "Unemployment rate": "LNS14000000",
    }
    sectors = {
        "Total private": "CES0500000001",
        "Mining and logging": "CES1000000001",
        "Construction": "CES2000000001",
        "Manufacturing": "CES3000000001",
        "Information": "CES5000000001",
        "Financial activities": "CES5500000001",
        "Professional and business services": "CES6000000001",
        "Private education and health services": "CES6500000001",
        "Leisure and hospitality": "CES7000000001",
        "Other services": "CES8000000001",
        "Government": "CES9000000001",
    }
    section = ""
    valid_tables = set()
    for table, row in m.rows:
        if row[0] == "Category" and len(row) >= 5:
            header = re.sub(r"\(.*?\)", "", row[4])
            parts = re.search(r"([A-Za-z.]+)\s*(\d{4})", header)
            if (
                parts
                and month(parts[1], int(parts[2])) == ref
                and "seasonally adjusted"
                in " ".join(m.table_text.get(table, [])).lower()
            ):
                valid_tables.add(table)
    for table, row in m.rows:
        if table not in valid_tables:
            continue
        label = re.sub(r"\(\s*\d+\s*\)", "", row[0]).strip()
        if table == "cps_empsit_sum" and label in mapping and len(row) == 6:
            claims.append(
                claim(
                    mapping[label], "level", ref, row[4], table_locator(table, row, ref)
                )
            )
        if table != "ces_table10":
            continue
        if len(row) <= 2 and label:
            if "EMPLOYMENT BY SELECTED" in label:
                section = "net_change_1m"
            elif "3-month average" in label:
                section = "average"
            elif "HOURS AND EARNINGS" in label:
                section = "hours"
            elif label.isupper():
                section = "other"
        if len(row) != 5:
            continue
        if section == "net_change_1m" and label in sectors:
            claims.append(
                claim(
                    sectors[label],
                    "net_change_1m",
                    ref,
                    row[4],
                    table_locator(table, row, ref),
                    unit="thousand_jobs",
                    footnotes=["Preliminary"] if "(p) Preliminary" in m.text else [],
                )
            )
        if section == "hours" and label in {
            "Average weekly hours",
            "Average hourly earnings",
        }:
            hours = label == "Average weekly hours"
            claims.append(
                claim(
                    "CES0500000002" if hours else "CES0500000003",
                    "level",
                    ref,
                    row[4],
                    table_locator(table, row, ref),
                    unit="hours" if hours else "USD/hour",
                )
            )
    doc["missing"] += [
        "employment_sector_levels_not_in_summary_B_changes_table",
        "employment_trade_transport_utilities_aggregate_not_in_material",
    ]


def _cpi(m, doc):
    mapping = {
        "All items": "SA0",
        "All items less food and energy": "SA0L1E",
        "Food": "SAF1",
        "Energy": "SA0E",
        "Shelter": "SAH1",
        "Commodities less food and energy commodities": "SACL1E",
        "Services less energy services": "SASLE",
    }
    header = " ".join(m.text.split()).lower()
    if (
        "unadjusted indexes" not in header
        or "seasonally adjusted percent change" not in header
    ):
        doc["missing"].append("cpi_table1_header_not_supported")
        return
    ref = doc["reference_month"]
    month_name = MONTHS[int(ref[5:]) - 1]
    valid_header = any(
        t == "cpipress1"
        and len(r) == 8
        and re.fullmatch(month_name[:3] + r"\.?\s*" + ref[:4], r[2], re.I)
        for t, r in m.rows
    )
    if not valid_header:
        doc["missing"].append("cpi_reference_month_columns_not_verified")
        return
    for table, row in m.rows:
        if table != "cpipress1" or row[0] not in mapping or len(row) != 10:
            continue
        suffix, ref = mapping[row[0]], doc["reference_month"]
        for prefix, stat, col, unit, adj in [
            ("CUUR0000", "index", 4, "index", "nsa"),
            ("CUUR0000", "yoy_pct", 5, "percent", "nsa"),
            ("CUSR0000", "mom_pct", 9, "percent", "sa"),
        ]:
            doc["claims"].append(
                claim(
                    prefix + suffix,
                    stat,
                    ref,
                    row[col],
                    table_locator(table, row, f"{ref}:{stat}:{adj}"),
                    unit=unit,
                    adjustment=adj,
                )
            )
    doc["missing"].append("cpi_sa_index_not_in_selected_table1")


def _ppi(m, doc):
    ref = doc["reference_month"]
    headers = [r for t, r in m.rows if t == "ppi_nrtablea"]

    def normalize(text):
        return " ".join(text.lower().split())

    expected = [
        "month",
        "total final demand",
        "final demand less foods, energy, and trade",
        "final demand goods",
        "final demand services",
        "change in final demand from 12 months ago (unadj.)",
        "change in final demand less foods, energy, and trade from 12 mo. ago (unadj.)",
    ]
    subheaders = [
        "total",
        "foods",
        "energy",
        "less foods and energy",
        "total",
        "trade",
        "transportation and warehousing",
        "other",
    ]
    caption = normalize(" ".join(m.table_text.get("ppi_nrtablea", [])))
    if (
        len(headers) < 2
        or list(map(normalize, headers[0])) != expected
        or list(map(normalize, headers[1])) != subheaders
        or "monthly and 12-month percent changes" not in caption
        or "price indexes, seasonally adjusted" not in caption
    ):
        doc["missing"].append("ppi_tablea_header_not_supported")
        return
    row_year = None
    for table, row in m.rows:
        if table == "ppi_nrtablea" and re.fullmatch(r"\d{4}", row[0]):
            row_year = int(row[0])
        if table == "ppi_nrtablea" and len(row) == 13 and row_year is not None:
            try:
                target = month(re.sub(r"\(\s*\d+\s*\)", "", row[0]), row_year)
            except CpiTimeSeriesError:
                continue
            if target != ref:
                continue
            for series, stat, col, adj in [
                ("WPSFD4", "mom_pct", 1, "sa"),
                ("WPSFD49116", "mom_pct", 2, "sa"),
                ("WPUFD4", "yoy_pct", 11, "nsa"),
                ("WPUFD49116", "yoy_pct", 12, "nsa"),
            ]:
                doc["claims"].append(
                    claim(
                        series,
                        stat,
                        ref,
                        row[col],
                        table_locator(table, row, f"{ref}:{stat}:{adj}"),
                        adjustment=adj,
                    )
                )
    # Table A's less-foods-and-energy column is GOODS, not core final demand.
    doc["missing"] += [
        "ppi_core_ex_food_energy_not_in_selected_material",
        "ppi_index_not_in_selected_material",
    ]
    doc["material_footnotes"] = [
        s for s in re.findall(r"[^.]*[Pp]reliminary[^.]*\.", m.text) if len(s) < 1500
    ]
    doc["material_footnotes"] += [
        r[0] for t, r in m.rows if t == "ppi_nrtablea" and r[0].startswith("Footnotes")
    ]


def _pce(m, doc):
    ref = doc["reference_month"]
    covered = doc.get("reference_months", [ref])
    headers = next(
        (
            row
            for _, row in m.rows
            if row[0] == ""
            and all(re.fullmatch(r"[A-Za-z]+", c) for c in row[1:])
            and 2 <= len(row) <= 5
        ),
        None,
    )
    if headers is None or "Percent change from preceding month" not in m.text:
        doc["missing"].append("pce_monthly_table_header_not_supported")
        return
    refs = []
    for label in headers[1:]:
        number = MONTHS.index(label) + 1
        year = int(ref[:4]) - (number > int(ref[5:]))
        refs.append(month(label, year))
    for table, row in m.rows:
        if row[0] not in {
            "PCE price index",
            "PCE price index excluding food and energy",
        } or len(row) != len(headers):
            continue
        series = "DPCCRG" if "excluding" in row[0] else "DPCERG"
        for col, reference in enumerate(refs, 1):
            doc["claims"].append(
                claim(
                    series,
                    "mom_pct",
                    reference,
                    row[col],
                    table_locator(table, row, reference),
                    role="actual" if reference in covered else "prior_revised",
                )
            )
    # Narrative annual rates are official rounded figures, never recomputed.
    pattern = (
        r"From the same month one year ago, the PCE price index for "
        r"(\w+) (increased|decreased) ([\d.]+) percent\s*\. "
        r"Excluding food and energy, the PCE price index "
        r"(increased|decreased) ([\d.]+) percent from one year ago"
    )
    for match in re.finditer(pattern, m.text):
        number = MONTHS.index(match[1]) + 1
        reference = month(match[1], int(ref[:4]) - (number > int(ref[5:])))
        if reference not in covered:
            continue
        for series, direction, value in [
            ("DPCERG", match[2], match[3]),
            ("DPCCRG", match[4], match[5]),
        ]:
            doc["claims"].append(
                claim(
                    series,
                    "yoy_pct",
                    reference,
                    ("-" if direction == "decreased" else "") + value,
                    dict(
                        kind="normalized_text_span",
                        start=match.start(),
                        end=match.end(),
                        excerpt=match[0],
                    ),
                )
            )
    doc["missing"] += [
        "pce_index_not_in_release_html",
        "official_previous_requires_separate_prior_event_material",
    ]


def _consumption(m, doc):
    ref = doc["reference_month"]
    covered = doc["reference_months"]
    headers = next(
        (
            row
            for _, row in m.rows
            if row[0] == ""
            and 2 <= len(row) <= 5
            and all(label in MONTHS for label in row[1:])
        ),
        None,
    )
    mapping = {
        "Current-dollar DPI": "A067RC",
        "Real DPI": "A067RX",
        "Current-dollar PCE": "DPCERC",
        "Real PCE": "DPCERX",
    }
    if headers and "Percent change from preceding month" in m.text:
        for table, row in m.rows:
            if row[0] not in mapping or len(row) != len(headers):
                continue
            for col, label in enumerate(headers[1:], 1):
                number = MONTHS.index(label) + 1
                reference = month(label, int(ref[:4]) - (number > int(ref[5:])))
                doc["claims"].append(
                    claim(
                        mapping[row[0]],
                        "mom_pct",
                        reference,
                        row[col],
                        table_locator(table, row, reference),
                        role="actual" if reference in covered else "prior_revised",
                    )
                )
    # Native rate, not percent growth. Never infer levels from rounded changes.
    pattern = (
        r"personal saving rate\s*[—–-]\s*personal saving as a percentage "
        r"of DPI\s*[—–-]\s*was\s+(-?[\d.]+)\s+percent"
    )
    for match in re.finditer(pattern, m.text, re.I) if len(covered) == 1 else ():
        doc["claims"].append(
            claim(
                "A072RC",
                "level",
                ref,
                match[1],
                dict(
                    kind="normalized_text_span",
                    start=match.start(),
                    end=match.end(),
                    excerpt=match[0],
                ),
            )
        )
    doc["missing"] += [
        "native_money_levels_not_in_release_html",
        "real_goods_services_not_in_release_html",
        "official_previous_requires_separate_prior_event_material",
        "historical_comparisons_xlsx_is_not_vintage_revision_table",
    ]


def _retail(m, doc):
    pattern = (
        r"Advance estimates of U.S. retail and food services sales "
        r"for \w+ \d{4}, adjusted for seasonal variation.*?were "
        r"\$([\d,.]+) billion, (up|down) ([\d.]+) percent"
    )
    match = re.search(pattern, m.text)
    if match:
        loc = dict(
            kind="normalized_text_span",
            start=match.start(),
            end=match.end(),
            excerpt=match[0],
        )
        doc["claims"] += [
            claim(
                "44X72:SM",
                "level",
                doc["reference_month"],
                str(Decimal(match[1].replace(",", "")) * 1000),
                loc,
                unit="million_USD",
            ),
            claim(
                "44X72:MPCSM",
                "mom_pct",
                doc["reference_month"],
                ("-" if match[2] == "down" else "") + match[3],
                loc,
            ),
        ]
        doc["claims"][-2]["reported_value"] = match[1]
        doc["claims"][-2]["reported_unit"] = "billion_USD"
        doc["claims"][-2]["normalization"] = dict(
            multiplier="1000",
            target_unit="million_USD",
            reported_scale="billion",
            display_decimal_places=max(
                0, -Decimal(match[1].replace(",", "")).as_tuple().exponent
            ),
            precision_basis="official_display_precision_not_api_precision",
        )
    rev = re.search(
        (
            r"The (\w+) (\d{4}) to (\w+) (\d{4}) percent change was "
            r"revised from (up|down) ([\d.]+) percent.*?to (up|down) "
            r"([\d.]+) percent"
        ),
        m.text,
    )
    if rev:
        prior = month(rev[3], int(rev[4]))
        for role, direction, value in [
            ("official_previous", rev[5], rev[6]),
            ("prior_revised", rev[7], rev[8]),
        ]:
            doc["claims"].append(
                claim(
                    "44X72:MPCSM",
                    "mom_pct",
                    prior,
                    ("-" if direction == "down" else "") + value,
                    dict(
                        kind="normalized_text_span",
                        start=rev.start(),
                        end=rev.end(),
                        excerpt=rev[0],
                    ),
                    role=role,
                )
            )
    doc["missing"].append("retail_control_ex_auto_inputs_not_in_release_html")


def _scope_absence(doc, family):
    """Machine-readable expected scopes, local to the retained material coverage."""
    from market_data_foundation.historical.bls_timeseries_profile import (
        CONSUMPTION_PROFILE,
        CPI_PROFILE,
        EMPLOYMENT_PROFILE,
        JOLTS_PROFILE,
        PCE_PROFILE,
        PPI_PROFILE,
    )

    profiles = {
        "cpi": CPI_PROFILE,
        "ppi": PPI_PROFILE,
        "pce": PCE_PROFILE,
        "consumption": CONSUMPTION_PROFILE,
        "employment": EMPLOYMENT_PROFILE,
        "jolts": JOLTS_PROFILE,
    }
    expected = []
    for ref in doc["reference_months"]:
        if family == "m3":
            from market_data_foundation.historical.m3_release_material import SERIES

            expected.extend(
                dict(
                    series_id=series,
                    reference_month=ref,
                    statistic="level",
                    unit="million_USD",
                    adjustment="sa",
                )
                for series in SERIES
            )
        elif family == "retail":
            for series in ("44X72:SM", "441:SM", "444:SM", "447:SM", "722:SM"):
                expected.append(
                    dict(
                        series_id=series,
                        reference_month=ref,
                        statistic="level",
                        unit="million_USD",
                        adjustment="sa",
                    )
                )
            expected.append(
                dict(
                    series_id="44X72:MPCSM",
                    reference_month=ref,
                    statistic="mom_pct",
                    unit="percent",
                    adjustment="sa",
                )
            )
        else:
            for series, meta in profiles[family].series_metadata.items():
                adjustment = meta["seasonal_adjustment"].lower()
                stats = (
                    ["level"]
                    if family in {"employment", "consumption", "jolts"}
                    else ["index", "mom_pct" if adjustment == "sa" else "yoy_pct"]
                )
                if family == "pce":
                    stats.append("yoy_pct")
                if family == "consumption" and series != "A072RC":
                    stats.append("mom_pct")
                if family == "employment" and meta.get("value_unit") == "thousand_jobs":
                    stats.append("net_change_1m")
                for stat in stats:
                    unit = (
                        meta.get("value_unit")
                        if family in {"employment", "jolts"}
                        or (family == "consumption" and stat == "level")
                        else "index"
                        if stat == "index"
                        else "percent"
                    )
                    unit = {"usd_per_hour": "USD/hour"}.get(unit, unit)
                    expected.append(
                        dict(
                            series_id=series,
                            reference_month=ref,
                            statistic=stat,
                            unit=unit,
                            adjustment=adjustment,
                        )
                    )
    keys = ("series_id", "reference_month", "statistic", "unit", "adjustment")
    proven = {tuple(c[k] for k in keys) for c in doc["claims"] if c["role"] == "actual"}
    already = {tuple(c[k] for k in keys) for c in doc["missing_scopes"]}
    for scope in expected:
        identity = tuple(scope[k] for k in keys)
        if identity not in proven and identity not in already:
            doc["missing_scopes"].append(
                dict(
                    scope,
                    reason="exact_scope_not_in_selected_official_material",
                    coverage_basis="retained_material_only_no_latest_substitution",
                )
            )
