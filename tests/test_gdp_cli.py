"""CLI defaults and explicit DEV write intent, without constructing live ports."""

from types import SimpleNamespace

import pytest

from scripts import gdp_timeseries as cli


@pytest.mark.parametrize("command", ["prepare", "init", "update"])
def test_write_intent_is_checked_before_opening_ports(monkeypatch, command):
    monkeypatch.setattr(cli, "build_service", lambda: pytest.fail("opened live port"))
    with pytest.raises(SystemExit) as raised:
        cli.main([command])
    assert raised.value.code == 2


def test_query_defaults_local_and_preserves_quarter_arguments(monkeypatch, capsys):
    calls = []
    service = SimpleNamespace(
        query_range=lambda *args, **kwargs: (
            calls.append((args, kwargs)) or {"status": "partial"}
        ),
        close=lambda: calls.append("closed"),
    )
    monkeypatch.setattr(cli, "build_service", lambda: service)
    assert (
        cli.main(
            ["query", "--start", "2026-Q2", "--end", "2026-Q3", "--series", "A191RL"]
        )
        == 0
    )
    assert calls[0][0] == ("2026-Q2", "2026-Q3")
    assert calls[0][1]["allow_upstream"] is False
    assert calls[0][1]["series_ids"] == ("A191RL",)
    assert calls[-1] == "closed"
    assert '"partial"' in capsys.readouterr().out
