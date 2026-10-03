"""Lab invariants use synthetic payloads and require no provider SDK."""

import importlib.util
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "tdx_checks", ROOT / "provider_lab/tdx_checks.py"
)
CHECKS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKS)


def bar(day="2024-01-02", **kwargs):
    return dict(time=day, open=2, high=3, low=1, close=2.5, **kwargs)


def test_daily_window_inclusive_sorted_and_no_fill():
    rows = [bar("2024-01-10"), bar("2024-01-02"), bar("2024-01-11")]
    selected = CHECKS.daily_window(rows, date(2024, 1, 2), date(2024, 1, 10))
    assert [row["time"] for row in selected] == ["2024-01-02", "2024-01-10"]


@pytest.mark.parametrize("failure", ("overlap", "nonfinite", "ohlc"))
def test_bad_history_is_rejected(failure):
    rows = [bar()]
    if failure == "overlap":
        rows.append(bar())
    elif failure == "nonfinite":
        rows[0]["close"] = float("nan")
    else:
        rows[0]["low"] = 2.7
    with pytest.raises(ValueError):
        CHECKS.daily_window(rows, date(2024, 1, 2), date(2024, 1, 10))


def test_taxonomy_identity_and_membership_are_checked():
    groups = [dict(name="synthetic", type=2, codes=["600000", "000001"])]
    assert CHECKS.concept_memberships(groups) == {
        ("synthetic", "600000"),
        ("synthetic", "000001"),
    }
    groups[0]["codes"].append("600000")
    with pytest.raises(ValueError, match="duplicate"):
        CHECKS.concept_memberships(groups)


@pytest.mark.parametrize("change", ({"type": 0}, {"codes": ["garbage"]}))
def test_wrong_taxonomy_or_invalid_code_rejected(change):
    group = dict(name="synthetic", type=2, codes=["600000"])
    group.update(change)
    with pytest.raises(ValueError):
        CHECKS.concept_memberships([group])


def test_lab_requires_opt_in_before_importing_sdks(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "provider_lab/tdx_r1.py"),
            "--case",
            "bj-go",
            "--output",
            str(tmp_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "explicit --live-provider opt-in required" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_representatives_require_observed_positive_price_codes():
    universes = {"SH": [], "SZ": [], "BJ": []}
    for market, code in (
        ("SH", "600000"),
        ("SH", "688001"),
        ("SH", "510010"),
        ("SH", "000001"),
        ("SZ", "000001"),
        ("SZ", "300001"),
        ("BJ", "920000"),
    ):
        universes[market].append(dict(code=code, last_price=1))
    universes["SZ"].append(dict(code="300000", last_price=0))
    selections = CHECKS.select_representatives(universes)
    assert len(selections) == 7
    assert (
        next(row for row in selections if row["label"] == "ChiNext")["code"] == "300001"
    )
    universes["BJ"].clear()
    with pytest.raises(ValueError, match="no observed representative for BJ"):
        CHECKS.select_representatives(universes)
