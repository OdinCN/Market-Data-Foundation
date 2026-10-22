import json
import subprocess
from dataclasses import asdict
from datetime import timedelta

import pytest

from market_data_foundation.candidates.tdx_intraday_v1.host import (
    I4_RELEASE,
    check_session_guard,
    run_host,
)
from market_data_foundation.candidates.tdx_intraday_v1.model import (
    DecodeError,
    Operation,
    canonical,
    sha,
)
from market_data_foundation.runtime.tdx_watchlist import DRIVER_BINARY_SHA256
from tests.test_i4_exhq import fixture
from tests.test_i4_replay import plan
from tests.test_i4_session import window


def test_host_current_candidate_gate_before_any_child_or_provider(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delitem(DRIVER_BINARY_SHA256, I4_RELEASE, raising=False)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **kw: pytest.fail("child launch forbidden")
    )
    req, _ = fixture(Operation.EX_QUOTE)
    path = tmp_path / "plan.json"
    path.write_bytes(canonical(plan(req)))
    output = tmp_path / "artifacts" / "run"
    result = run_host(
        path,
        tmp_path / "untrusted-missing-binary",
        output,
        allow_live=True,
        stage="afterclose",
    )
    assert result["status"] == "STOP"
    assert result["gate"] == "CANDIDATE_RELEASE_HAS_NO_REVIEWED_BINARY_PIN"
    assert not result["subprocess_started"]
    assert result["provider_effect"] == "ZERO_NOT_LAUNCHED"
    assert json.loads((output / "host-ledger.json").read_bytes()) == result
    with pytest.raises(DecodeError, match="opt_in"):
        run_host(
            path,
            tmp_path / "missing",
            tmp_path / "artifacts" / "noopt",
            stage="afterclose",
        )
    assert not (tmp_path / "artifacts" / "noopt").exists()
    with pytest.raises(DecodeError):
        run_host(
            path,
            tmp_path / "missing",
            tmp_path / "artifacts" / ".." / "outside",
            allow_live=True,
            stage="afterclose",
        )
    assert not (tmp_path / "outside").exists()


def test_session_guard_reloads_calendar_bytes_and_rechecks_exact_window(tmp_path):
    req, _ = fixture(Operation.EX_QUOTE)
    p = plan(req)
    calendar = tmp_path / "synthetic-calendar.bin"
    calendar.write_bytes(b"synthetic-calendar-r1")
    from dataclasses import replace

    w = replace(window(), calendar_material_sha256=sha(calendar.read_bytes()))
    values = asdict(w)
    values["trading_day"] = w.trading_day.isoformat()
    for field in ("opens_at", "closes_at", "opens_utc", "closes_utc"):
        values[field] = getattr(w, field).isoformat()
    guard = {
        "calendar_material": str(calendar),
        "calendar_sha256": sha(calendar.read_bytes()),
        "windows": [values],
    }
    path = tmp_path / "guard.json"
    path.write_bytes(canonical(guard))
    now = w.opens_utc + timedelta(seconds=1)
    assert (
        check_session_guard(p, path, now)["calendar_sha256"] == guard["calendar_sha256"]
    )
    for instant in (
        w.opens_utc - timedelta(seconds=1),
        w.closes_utc - timedelta(seconds=1),
    ):
        with pytest.raises(DecodeError):
            check_session_guard(p, path, instant)
    changed = dict(p, requests=[dict(p["requests"][0], category=3)])
    with pytest.raises(DecodeError):
        check_session_guard(changed, path, now)
    calendar.write_bytes(b"changed-synthetic-calendar")
    with pytest.raises(DecodeError, match="digest_mismatch"):
        check_session_guard(p, path, now)
