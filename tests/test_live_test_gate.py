"""Verify pytest marker/opt-in separation without any live provider test."""

from pathlib import Path

pytest_plugins = ["pytester"]


def test_live_selection_requires_both_marker_and_opt_in(pytester) -> None:
    pytester.makeconftest((Path(__file__).with_name("conftest.py")).read_text())
    pytester.makeini(
        "[pytest]\naddopts = --strict-markers -m 'not live_provider'\n"
        "markers = live_provider: opt-in live tests\n"
    )
    pytester.makepyfile(
        "import pytest\n"
        "def test_offline(): pass\n"
        "@pytest.mark.live_provider\n"
        "def test_simulated_live(): pass\n"
    )
    pytester.runpytest_subprocess().assert_outcomes(passed=1, deselected=1)
    pytester.runpytest_subprocess("-m", "live_provider").assert_outcomes(
        skipped=1, deselected=1
    )
    pytester.runpytest_subprocess(
        "--live-provider", "-m", "live_provider"
    ).assert_outcomes(passed=1, deselected=1)
