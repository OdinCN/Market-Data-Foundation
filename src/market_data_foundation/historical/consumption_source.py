"""Strict native level/rate mapping over the existing credential-safe BEA edge."""

import re
from decimal import Decimal, InvalidOperation

from market_data_foundation.historical.bls_timeseries_profile import CONSUMPTION_PROFILE
from market_data_foundation.historical.cpi_timeseries_source import SourcePoint, _series
from market_data_foundation.historical.pce_timeseries_source import (
    BeaRangeSource,
    PceSourceError,
    _results,
    _validate_range,
)


def _parse_table(results, start, end, table):
    metadata = CONSUMPTION_PROFILE.series_metadata
    lines = {
        m["line_number"]: sid for sid, m in metadata.items() if m["table_name"] == table
    }
    selected, seen = {}, set()
    for row in results["Data"]:
        if not isinstance(row, dict) or row.get("TableName") != table:
            raise PceSourceError("consumption_table_invalid")
        line, sid = row.get("LineNumber"), row.get("SeriesCode")
        if not isinstance(line, str) or not line.isdigit() or not isinstance(sid, str):
            raise PceSourceError("consumption_series_invalid")
        if (line in lines and lines[line] != sid) or (
            sid in metadata and lines.get(line) != sid
        ):
            raise PceSourceError("consumption_series_mapping_invalid")
        period = row.get("TimePeriod")
        if not isinstance(period, str) or not re.fullmatch(
            r"[0-9]{4}M(?:0[1-9]|1[0-2])", period
        ):
            raise PceSourceError("consumption_period_invalid")
        month = period[:4] + "-" + period[5:]
        if not start[:4] <= month[:4] <= end[:4] or (line, month) in seen:
            raise PceSourceError("consumption_duplicate_or_out_of_range")
        seen.add((line, month))
        if line not in lines:
            continue
        meta = metadata[sid]
        if (
            row.get("METRIC_NAME") != meta["metric_name"]
            or row.get("UNIT_MULT") != meta["unit_mult"]
            or row.get("CL_UNIT") != "Level"
        ):
            raise PceSourceError("consumption_unit_invalid")
        if meta["chained_base"]:
            descriptions = [row.get("LineDescription", "")]
            descriptions += [
                note.get("NoteText", "")
                for note in results.get("Notes", [])
                if isinstance(note, dict)
            ]
            bases = {
                year
                for text in descriptions
                if isinstance(text, str)
                for year in re.findall(r"chained\s*\((\d{4})\)", text, re.I)
            }
            if bases != {meta["chained_base"]}:
                raise PceSourceError("consumption_chained_base_invalid")
        if "DataValue" not in row:
            raise PceSourceError("consumption_value_missing")
        raw = row["DataValue"]
        if raw in (None, "", "(NA)", "NA", "---", "--", "..."):
            value = None
        else:
            if not isinstance(raw, str) or not re.fullmatch(
                r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", raw.strip()
            ):
                raise PceSourceError("consumption_value_invalid")
            try:
                value = Decimal(raw.replace(",", ""))
            except InvalidOperation:
                raise PceSourceError("consumption_value_invalid") from None
            if not value.is_finite() or (sid != "A072RC" and value <= 0):
                raise PceSourceError("consumption_value_invalid")
        if start <= month <= end:
            selected[sid, month] = SourcePoint(
                sid,
                month,
                value,
                {"bea": dict(row), "retained_notes": results.get("Notes", [])},
                (),
            )
    return selected


def parse_results(first, series, start, end, second=None):
    points = _parse_table(first, start, end, "T20600")
    if second is not None:
        points.update(_parse_table(second, start, end, "T20806"))
    return tuple(points[k] for k in sorted(points) if k[0] in series)


def parse_response(
    raw, series_ids, start_month, end_month, *, second_raw=None, forbidden_secret=None
):
    _series(series_ids, profile=CONSUMPTION_PROFILE)
    _validate_range(start_month, end_month)
    return parse_results(
        _results(raw, forbidden_secret),
        series_ids,
        start_month,
        end_month,
        _results(second_raw, forbidden_secret) if second_raw is not None else None,
    )


class ConsumptionRangeSource(BeaRangeSource):
    profile_default = CONSUMPTION_PROFILE
    tables = ("T20600", "T20806")
    parse_results = staticmethod(parse_results)

    def __init__(self, **options):
        options.setdefault("profile", CONSUMPTION_PROFILE)
        super().__init__(**options)
