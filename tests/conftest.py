"""Live tests need a CLI opt-in in addition to marker selection."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
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
