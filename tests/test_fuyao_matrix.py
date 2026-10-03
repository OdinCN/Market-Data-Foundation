"""最终报告的确定性约束与实际响应检查反例，不依赖 live artifacts。"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import jsonschema
import pytest

from market_data_foundation.qualification import Capability
from provider_lab.fuyao_r1_adjudication import (
    IMPLEMENTATION,
    bars_check,
    calendar_check,
    compare_modes,
    universe_check,
)

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "175d1a0037312ebe734b73763f601d2469b0bb01"
CAPABILITIES = {c.value for c in Capability}


def test_matrix_report_schema_and_single_access_path():
    matrix = json.loads((ROOT / "provider_lab/fuyao_r1.matrix.json").read_text())
    report = (ROOT / "docs/qualification/fuyao-r1.md").read_text()
    cell_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["disposition", "basis", "observed_request_ids", "evidence"],
        "properties": {
            "disposition": {
                "enum": ["QUALIFIED_WITH_LIMITATIONS", "UNRESOLVED", "NOT_SUPPORTED"]
            },
            "basis": {"type": "string", "minLength": 1},
            "observed_request_ids": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string", "pattern": "^r(0[1-9]|1[0-9]|2[0-4])$"},
            },
            "evidence": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
        },
    }
    jsonschema.Draft202012Validator(
        {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(CAPABILITIES),
            "properties": {cap: cell_schema for cap in CAPABILITIES},
        }
    ).validate(matrix["implementations"][IMPLEMENTATION])
    assert set(matrix["implementations"]) == {IMPLEMENTATION}
    assert matrix["qualification_implementation"] == IMPLEMENTATION
    assert IMPLEMENTATION in report
    assert matrix["baseline"] == BASELINE and matrix["python"] == "3.14.8"
    for field in (
        "binding_authorized",
        "production_adapter_created",
        "deployment_started",
        "db_effect",
    ):
        assert matrix[field] is False
    assert matrix["live_stage"] == {
        "status": "COMPLETE",
        "total_http_attempts": 24,
        "r17_disposition": "UNRESOLVED_DATA_NOT_READY",
        "r17_http_status": 200,
        "r17_business_code": 3002,
        "retry_authorized": False,
        "retry_performed": False,
        "tested_and_collected_request_ids": [f"r{i}" for i in range(18, 25)],
        "not_tested_request_ids": [],
        "further_provider_requests_authorized": False,
    }
    assert matrix["evidence_manifest"] == (
        "artifacts/fuyao-r1/final-adjudication-002/evidence-manifest.json"
    )
    rows = {}
    for line in report.splitlines():
        columns = [column.strip() for column in line.strip("|").split("|")]
        if columns[0] in CAPABILITIES:
            assert columns[0] not in rows
            rows[columns[0]] = columns[1:]
    assert set(rows) == CAPABILITIES
    cells = matrix["implementations"][IMPLEMENTATION]
    for cap, cell in cells.items():
        assert rows[cap] == [cell["disposition"], cell["basis"]]
        for path in cell["evidence"]:
            evidence = PurePosixPath(path)
            assert evidence.parts[:2] == ("artifacts", "fuyao-r1")
            assert ".." not in evidence.parts
    limited = {
        "security.metadata",
        "quote.snapshot",
        "bars.daily",
        "calendar.trading_days",
        "financial.statements",
        "sector.industry",
        "sector.concept",
        "sector.membership",
    }
    scoped_unsupported = {"bars.intraday", "trades.intraday"}
    for cap in CAPABILITIES:
        expected = (
            "QUALIFIED_WITH_LIMITATIONS"
            if cap in limited
            else "NOT_SUPPORTED"
            if cap in scoped_unsupported
            else "UNRESOLVED"
        )
        assert cells[cap]["disposition"] == expected
    for cap in (
        "finance.snapshot",
        "finance.f10",
        *scoped_unsupported,
    ):
        assert cells[cap]["observed_request_ids"] == []
        assert "NOT TESTED" in cells[cap]["basis"]
    assert cells["corporate_actions"]["observed_request_ids"] == ["r17"]
    expected_requests = {
        "financial.statements": ["r18", "r19", "r20"],
        "sector.industry": ["r21", "r22"],
        "sector.concept": ["r23", "r24"],
        "sector.membership": ["r21", "r22", "r23", "r24"],
    }
    for cap, ids in expected_requests.items():
        assert cells[cap]["observed_request_ids"] == ids
        assert "NOT TESTED" not in cells[cap]["basis"]
    assert "24 total HTTP attempts" in report and "3002" in report
    assert "no further provider requests authorized" in report
    assert "STOPPED_AT_R17" not in report
    assert "UNRESOLVED_DATA_NOT_READY" in report
    assert "r18–r24" in report and "没有重试" in report


def test_tdx_and_observation_bytes_preserved():
    paths = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", BASELINE], cwd=ROOT, text=True
    ).splitlines()
    preserved = [path for path in paths if "tdx" in path.lower()]
    preserved += ["provider_lab/fixtures.toml", "provider_lab/observation.schema.json"]
    for path in preserved:
        baseline = subprocess.check_output(
            ["git", "show", f"{BASELINE}:{path}"], cwd=ROOT
        )
        assert (ROOT / path).read_bytes() == baseline


def test_successful_universe_page_can_fail_exchange_scope():
    result = universe_check(
        {"exchange": "BJ", "offset": "2", "limit": "2"},
        {"item": [{"thscode": "600006.SH", "exchange": "SH"}]},
    )
    assert result["exchange_filter_matches"] is False
    assert result["returned_exchanges"] == ["SH"]
    assert result["complete"] is result["exhausted"] is None


def sample(mode="none", dates=None):
    stamps = dates or [1704124800000, 1704211200000]
    return {
        "thscode": "600000.SH",
        "interval": "1d",
        "adjust": mode,
        "item": [
            {
                "date_ms": stamp,
                "open_price": 6.6,
                "high_price": 6.7,
                "low_price": 6.5,
                "close_price": 6.6,
                "volume": 100,
                "turnover": 660,
            }
            for stamp in stamps
        ],
    }


def query():
    return {
        "thscode": "600000.SH",
        "interval": "1d",
        "adjust": "none",
        "start": "1704124800000",
        "end": "1704902399999",
    }


@pytest.mark.parametrize("damage", ["duplicate", "ohlc", "identity", "window", "nan"])
def test_daily_checks_reject_semantic_failures(damage):
    data = sample()
    field = {
        "duplicate": "strictly_ascending_unique",
        "ohlc": "ohlc_valid",
        "identity": "identity_matches",
        "window": "within_requested_window",
        "nan": "finite_nonnegative_values",
    }[damage]
    if damage == "duplicate":
        data["item"][1]["date_ms"] = data["item"][0]["date_ms"]
    elif damage == "ohlc":
        data["item"][0]["low_price"] = 7
    elif damage == "identity":
        data["thscode"] = "000001.SZ"
    elif damage == "window":
        data["item"][1]["date_ms"] = 1705000000000
    else:
        data["item"][0]["volume"] = float("nan")
    assert bars_check(query(), data)[field] is False


def test_adjustment_labels_and_equal_rows_do_not_prove_qfq_hfq():
    result = compare_modes(sample(), sample("forward"), sample("backward"))
    assert result["forward"]["ohlc_all_different"] is False
    assert result["forward"]["volume_unchanged"] is True
    assert result["qualified_qfq_hfq_mapping"] is False
    assert result["anchor_established"] is result["events_established"] is False
    result = compare_modes(sample(), sample("forward", [1704297600000]), sample())
    assert result["forward"] == {"dates_and_identity_align": False}


def test_calendar_timezone_must_be_checked_against_explicit_date():
    stamp = int(datetime(2025, 10, 9, tzinfo=timezone.utc).timestamp() * 1000)
    result = calendar_check({"item": [{"date": "20251009", "date_ms": stamp}]})
    assert result["date_ms_matches_utc_date"] is result["date_ms_utc_midnight"] is True
    assert result["date_ms_asia_shanghai_midnight"] is False
    local = calendar_check(
        {"item": [{"date": "20251009", "date_ms": stamp - 28800000}]}
    )
    assert local["date_ms_matches_utc_date"] is False
    assert local["date_ms_utc_midnight"] is False
    assert local["date_ms_matches_asia_shanghai_date"] is True
    assert local["date_ms_asia_shanghai_midnight"] is True
    assert result["complete"] is None
    assert result["historical_calendar_established"] is False
    result = bars_check(query(), sample())
    assert result["dates_asia_shanghai"] == ["2024-01-02", "2024-01-03"]
    assert (
        datetime.fromtimestamp(1704124800, timezone.utc).date().isoformat()
        == "2024-01-01"
    )
