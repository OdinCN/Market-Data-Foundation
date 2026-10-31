"""Pure normalization of verified redundant PCE provider metadata."""

import copy


def compact_pce_metadata(extra, *, series_id):
    """Drop only exact verified BEA identities/constants; preserve other values."""
    result = copy.deepcopy(extra)
    if not isinstance(result, dict):
        return result
    metadata = result.get("bea")
    if isinstance(metadata, dict):
        known = {
            "SeriesCode": series_id,
            "TableName": "T20804",
            "CL_UNIT": "Level",
            "UNIT_MULT": "0",
            "METRIC_NAME": "Fisher Price Index",
        }
        line = {"DPCERG": "1", "DPCCRG": "25"}.get(series_id)
        if line is not None:
            known["LineNumber"] = line
        for key, expected in known.items():
            if key in metadata and metadata[key] == expected:
                del metadata[key]
        if not metadata:
            del result["bea"]
    return result or None
