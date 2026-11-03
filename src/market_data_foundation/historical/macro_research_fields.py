"""Column projection for the existing monthly query interfaces."""


def query_columns(family):
    if family == "jolts":
        return (
            "value",
            "change_1m_level",
            "change_1m_ratio",
            "change_12m_ratio",
            "change_1m_percentage_points",
            "change_12m_percentage_points",
        )
    if family == "consumption":
        return (
            "value",
            "mom_pct",
            "yoy_pct",
            "change_1m_ratio",
            "change_3m_ratio",
            "change_6m_ratio",
            "change_12m_ratio",
            "change_1m_percentage_points",
            "change_12m_percentage_points",
        )
    if family == "employment":
        return (
            "value",
            "net_change_1m_thousand_jobs",
            "change_1m_percentage_points",
            "change_1m_hours",
            "mom_pct",
            "yoy_pct",
            "change_1m_ratio",
            "change_12m_ratio",
        )
    columns = (
        "index_value",
        "change_1m_ratio",
        "change_3m_ratio",
        "change_6m_ratio",
        "change_12m_ratio",
    )
    if family == "retail":
        return (*columns, "official_mpcsm_percent")
    return (
        *columns,
        "mom_pct",
        "yoy_pct",
        *(("annualized_3m_ratio", "annualized_6m_ratio") if family == "pce" else ()),
    )


def selected_columns(value, family):
    from market_data_foundation.historical.cpi_timeseries_cli import InterfaceError

    if value is None:
        return None
    if not isinstance(value, str):
        raise InterfaceError("invalid_columns")
    columns = value.split(",")
    if (
        not columns
        or len(set(columns)) != len(columns)
        or not set(columns) <= set(query_columns(family))
    ):
        raise InterfaceError("invalid_columns")
    return columns


def project_columns(result, columns, family):
    if columns is None:
        return result
    excluded = set(query_columns(family)) - set(columns)
    return {
        **result,
        "selected_columns": columns,
        "points": [
            {k: v for k, v in point.items() if k not in excluded}
            for point in result["points"]
        ],
        "calculation_missing": [
            gap for gap in result["calculation_missing"] if gap["metric"] in columns
        ],
    }
