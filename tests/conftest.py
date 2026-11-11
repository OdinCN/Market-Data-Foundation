"""Live tests need a CLI opt-in in addition to marker selection."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--sdmx-historical-evidence",
        help="host-only offline replay: path to ignored historical evidence directory",
    )
    parser.addoption(
        "--live-provider",
        action="store_true",
        help="allow explicitly selected live tests",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--live-provider"):
        return
    for item in items:
        if item.get_closest_marker("live_provider"):
            item.add_marker(pytest.mark.skip(reason="requires --live-provider"))


@pytest.fixture(autouse=True)
def _fuyao_qualification_python_for_deterministic_tests(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Map frozen Fuyao host identity to the deterministic test interpreter only."""
    if request.node.get_closest_marker("live_provider"):
        return

    import sys
    from pathlib import Path

    try:
        from provider_lab import fuyao_r1_live_runner as runner
        from provider_lab import fuyao_single_attempt as single
    except ModuleNotFoundError:
        # This conftest is also copied into isolated marker/opt-in harness tests.
        return

    python = Path(sys.executable)
    monkeypatch.setattr(single, "CANONICAL_PYTHON", python)
    monkeypatch.setattr(runner, "CANONICAL_PYTHON", python)


@pytest.fixture(autouse=True)
def _host_request_ledgers_are_not_deterministic_fixtures(request, monkeypatch):
    """Fail before mkdir/flock on bound DEV paths; never reset a real budget."""
    if request.node.get_closest_marker("live_provider"):
        return
    from market_data_foundation.historical.cpi_request_control import CpiRequestControl

    original = CpiRequestControl.attempt

    def isolated_only(control):
        value = str(control.state_path.resolve())
        if value.startswith(
            ("/srv/market-data-foundation/", "/srv/market-data-foundation-control/")
        ):
            pytest.fail(
                "host request ledger requires isolated fixture or live_provider"
            )
        return original(control)

    monkeypatch.setattr(CpiRequestControl, "attempt", isolated_only)
