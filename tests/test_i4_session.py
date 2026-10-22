from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from market_data_foundation.candidates.tdx_intraday_v1 import decode
from market_data_foundation.candidates.tdx_intraday_v1.model import (
    DecodeError,
    Operation,
)
from market_data_foundation.candidates.tdx_intraday_v1.session import (
    SessionWindow,
    adjudicate_refresh,
)
from tests.test_i4_exhq import fixture
from tests.test_i4_intraday import material


def window():
    zone = ZoneInfo("Asia/Hong_Kong")
    opens = datetime(2026, 10, 23, 9, 35, tzinfo=zone)
    closes = datetime(2026, 10, 23, 10, 0, tzinfo=zone)
    return SessionWindow(
        "HKEX",
        "synthetic-calendar-r1",
        "a" * 64,
        31,
        "00700",
        date(2026, 10, 23),
        "Asia/Hong_Kong",
        opens,
        closes,
        opens.astimezone(UTC),
        closes.astimezone(UTC),
        category=2,
    )


def pages():
    import struct

    req, raw = fixture(Operation.EX_QUOTE)
    start = window().opens_utc
    result = []
    for n in range(3):
        payload = bytearray(raw)
        struct.pack_into("<f", payload, 30, 101.5 + n)
        stamp = start + timedelta(seconds=n * 2)
        result.append(
            decode(
                material(
                    req,
                    bytes(payload),
                    mode="retained",
                    producer_sha256="b" * 64,
                    obtained_at=stamp,
                    received_at=stamp + timedelta(seconds=1),
                    known_at=None,
                )
            )
        )
    return tuple(result)


def test_multiple_changes_prove_only_exact_window_content_change():
    result = adjudicate_refresh(pages(), window())
    assert result["changed_transitions"] == 2
    assert result["active_refresh"] == "OBSERVED_CONTENT_CHANGE_IN_EXACT_WINDOW"
    assert result["source_freshness"] == "UNKNOWN"
    assert result["source_event_clock"] == "UNKNOWN"


def test_afterclose_duplicate_unknown_calendar_different_market_do_not_pass():
    sample = pages()
    assert adjudicate_refresh(sample, None)["active_refresh"] == "NOT_PROVEN"
    assert (
        adjudicate_refresh((sample[0],) * 3, window())["active_refresh"] == "NOT_PROVEN"
    )
    assert (
        adjudicate_refresh(sample, replace(window(), market=48))["active_refresh"]
        == "NOT_PROVEN"
    )
    late = tuple(
        replace(
            p,
            material=replace(
                p.material,
                obtained_at=p.material.obtained_at + timedelta(hours=8),
                received_at=p.material.received_at + timedelta(hours=8),
            ),
        )
        for p in sample
    )
    assert adjudicate_refresh(late, window())["active_refresh"] == "NOT_PROVEN"
    assert not window().contains(window().closes_utc)


def test_history_api_never_becomes_live_via_requested_today_date():
    req, raw = fixture(Operation.EX_HISTORY_TRADE)
    sample = pages()
    historical = tuple(
        decode(
            replace(
                p.material,
                request=replace(req, date="20261023"),
                raw=raw,
                raw_sha256=__import__("hashlib").sha256(raw).hexdigest(),
            )
        )
        for p in sample
    )
    assert adjudicate_refresh(historical, window())["active_refresh"] == "NOT_PROVEN"


def test_dst_fold_is_explicit_and_nonexistent_wall_rejected():
    zone = ZoneInfo("America/New_York")
    first = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=0)
    second = datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=1)
    w = replace(
        window(),
        venue="synthetic-US",
        timezone="America/New_York",
        trading_day=date(2026, 11, 1),
        opens_at=first,
        closes_at=second,
        opens_utc=first.astimezone(UTC),
        closes_utc=second.astimezone(UTC),
    )
    assert w.contains(datetime(2026, 11, 1, 6, 30, tzinfo=UTC))
    with pytest.raises(DecodeError, match="dst_or_zone"):
        replace(w, opens_utc=first.replace(fold=1).astimezone(UTC))
    nonexistent = datetime(2026, 3, 8, 2, 30, tzinfo=zone)
    with pytest.raises(DecodeError, match="dst_or_zone"):
        replace(w, opens_at=nonexistent, opens_utc=nonexistent.astimezone(UTC))
