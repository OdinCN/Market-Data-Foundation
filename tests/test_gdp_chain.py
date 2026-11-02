"""GDP quarter/value/version/readback tests; all provider and raw data synthetic."""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs

import pytest

from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
from market_data_foundation.historical.gdp_source import GdpRangeSource
from market_data_foundation.historical.gdp_store import (
    GdpStorageError,
    InMemoryGdpStore,
    digest,
    raw_locator,
    validate_batch,
)
from market_data_foundation.historical.gdp_timeseries import (
    GdpTimeSeriesError,
    GdpTimeSeriesService,
)
from tests.test_gdp_source import AT, KEY, Control, document, no_network  # noqa: F401


class Raw:
    def __init__(self):
        self.saved = {}

    def put_verified(self, raw):
        self.saved[digest(raw)] = raw
        return raw_locator(digest(raw))


class DatasetWire:
    def __init__(self):
        self.overrides, self.calls = {}, []
        self.status = 200

    def post(self, endpoint, body, headers, **options):
        p = parse_qs(body.decode())
        self.calls.append(p)
        qs = tuple(
            y + "Q" + str(q) for y in p["Year"][0].split(",") for q in range(1, 5)
        )
        d = document(p["TableName"][0], qs)
        for row in d["BEAAPI"]["Results"]["Data"]:
            key = row["SeriesCode"], row["TimePeriod"]
            if key in self.overrides:
                row["DataValue"] = self.overrides[key]
        return HttpResult(
            self.status, json.dumps(d).encode() if self.status == 200 else b"", {}
        )


def service():
    wire, archive = DatasetWire(), Raw()
    clock = [AT]
    source = GdpRangeSource(
        credential_loader=lambda: KEY,
        transport=wire,
        request_control=Control(),
        clock=lambda: clock[0],
        sleep=lambda _: None,
        max_transport_attempts=1,
    )
    store = InMemoryGdpStore(archive, clock=lambda: clock[0] + timedelta(seconds=1))
    return GdpTimeSeriesService(store, source), wire, clock, archive


def test_initialization_readback_descending_quarters_and_repeat_is_inert():
    s, wire, _, archive = service()
    receipt = s.init_range("2025-Q4", "2026-Q3")
    assert receipt["coverage"] == {
        "expected": 32,
        "available": 32,
        "official_null": 0,
        "missing": 0,
    }
    assert receipt["saved_points"] == 64 and receipt["saved_revisions"] == 0
    assert receipt["provider_requests"] == 4 and receipt["provider_attempts"] == 4
    result = s.query_range("2025-Q4", "2026-Q3")
    assert [p["quarter"] for p in result["points"]][::8] == [
        "2026-Q3",
        "2026-Q2",
        "2026-Q1",
        "2025-Q4",
    ]
    assert all(p["origin"] == "local" for p in result["points"])
    growth = next(p for p in result["points"] if p["series_id"] == "A191RL")
    assert growth["value"] == "3.4" and growth["value_unit"] == "percent"
    assert growth["change_1q_ratio"] is None
    before = s.status(), len(archive.saved), len(wire.calls)
    again = s.init_range("2025-Q4", "2026-Q3")
    assert again["saved_points"] == again["provider_requests"] == 0
    assert (s.status(), len(archive.saved), len(wire.calls)) == before


def test_revisions_same_cutoff_comparisons_and_no_history_network():
    s, wire, clock, _ = service()
    s.init_range("2026-Q3", "2026-Q3", series_ids=("A191RX",))
    initial = s.query_range("2026-Q3", "2026-Q3", series_ids=("A191RX",))["points"][0]
    clock[0] += timedelta(days=1)
    wire.overrides["A191RX", "2026Q3"] = "26,400,000.1353"
    changed = s.update_range("2026-Q3", "2026-Q3", series_ids=("A191RX",))
    assert changed["saved_points"] == changed["saved_revisions"] == 1
    current = s.query_range("2026-Q3", "2026-Q3", series_ids=("A191RX",))["points"][0]
    assert current["revision"] == 2 and current["change_1q_ratio"] == "0.1000"
    calls = len(wire.calls)
    old = s.query_range(
        "2026-Q3",
        "2026-Q3",
        series_ids=("A191RX",),
        as_of=AT + timedelta(hours=1),
        allow_upstream=True,
    )
    assert old["points"][0]["value"] == initial["value"]
    assert old["points"][0]["change_1q_ratio"] == "0.0000"
    assert old["local_only"] is True and len(wire.calls) == calls
    assert s.status()["revision_count"] == 1
    clock[0] += timedelta(days=1)
    unchanged = s.update_range("2026-Q3", "2026-Q3", series_ids=("A191RX",))
    assert unchanged["saved_points"] == 0
    assert (
        s.query_range("2026-Q3", "2026-Q3", series_ids=("A191RX",))["points"][0][
            "observed_at"
        ]
        == current["observed_at"]
    )


def test_observed_and_available_cutoffs_are_distinct():
    s, _, _, _ = service()
    s.init_range("2026-Q3", "2026-Q3", series_ids=("A191RL",))
    observed = s.query_range(
        "2026-Q3", "2026-Q3", series_ids=("A191RL",), as_of=AT, as_of_basis="observed"
    )
    available = s.query_range(
        "2026-Q3", "2026-Q3", series_ids=("A191RL",), as_of=AT, as_of_basis="available"
    )
    assert observed["coverage"]["available"] == 1
    assert available["coverage"]["available"] == 0
    assert available["points"][0]["missing_reason"] == "local_record_missing"


def test_current_query_can_fill_missing_without_persisting_or_replacing_local_values():
    s, wire, clock, archive = service()
    s.init_range("2026-Q2", "2026-Q2", series_ids=("A191RL",))
    before = s.status(), len(archive.saved)
    clock[0] += timedelta(days=1)
    wire.overrides["A191RL", "2026Q2"] = "9.9"
    result = s.query_range(
        "2026-Q2", "2026-Q3", series_ids=("A191RL",), allow_upstream=True
    )
    assert result["coverage"]["available"] == 2
    assert result["points"][0]["origin"] == "upstream"
    assert (
        result["points"][1]["origin"] == "local"
        and result["points"][1]["value"] == "3.4"
    )
    assert (s.status(), len(archive.saved)) == before


def test_future_quarter_does_not_request_provider_and_failure_preserves_local_rows():
    s, wire, _, _ = service()
    result = s.query_range(
        "2026-Q4", "2026-Q4", series_ids=("A191RL",), allow_upstream=True
    )
    assert result["coverage"]["available"] == 0 and wire.calls == []
    s.init_range("2026-Q2", "2026-Q2", series_ids=("A191RL",))
    wire.status = 503
    result = s.query_range(
        "2026-Q2", "2026-Q3", series_ids=("A191RL",), allow_upstream=True
    )
    assert result["coverage"]["available"] == 1 and result["status"] == "partial"
    assert result["source_failure"] == "gdp_timeseries_http_failure"


def test_explicit_null_is_stored_and_not_treated_as_a_refetch_gap():
    s, wire, _, _ = service()
    wire.overrides["A191RL", "2026Q3"] = "(NA)"
    result = s.init_range("2026-Q3", "2026-Q3", series_ids=("A191RL",))
    assert result["coverage"]["official_null"] == 1
    assert s.store.read_range(("A191RL",), "2026-Q3", "2026-Q3")[0]["value"] is None
    before = len(wire.calls)
    s.init_range("2026-Q3", "2026-Q3", series_ids=("A191RL",))
    assert len(wire.calls) == before


def test_forged_point_or_request_rejected_before_any_raw_or_numeric_write():
    s, _, _, archive = service()
    batch = s.source.fetch(("A191RL",), "2026-Q3", "2026-Q3")[0]
    bad = replace(batch, points=(replace(batch.points[0], value=Decimal("999")),))
    with pytest.raises(GdpStorageError, match="batch_invalid"):
        s.store.save_batches((bad,))
    with pytest.raises(GdpStorageError, match="batch_invalid"):
        validate_batch(replace(batch, request={**batch.request, "UserID": KEY}))
    assert archive.saved == {} and s.status()["observation_count"] == 0


def test_stale_changed_observation_rolls_back_whole_collection():
    s, wire, _, _ = service()
    s.init_range("2026-Q3", "2026-Q3", series_ids=("A191RL",))
    before = s.status()
    wire.overrides["A191RL", "2026Q3"] = "7.1"
    with pytest.raises(GdpTimeSeriesError, match="observation_order_conflict"):
        s.update_range("2026-Q3", "2026-Q3", series_ids=("A191RL",))
    assert s.status() == before


def test_source_table_note_timestamp_drift_does_not_create_a_statistical_revision():
    s, _, clock, _ = service()
    batch = s.source.fetch(("A191RL",), "2026-Q3", "2026-Q3")[0]
    s.store.save_batches((batch,))
    clock[0] += timedelta(days=1)
    d = json.loads(batch.raw)
    d["Results"]["Notes"].append(
        {"NoteText": "New table production date; unchanged value"}
    )
    raw = json.dumps(d).encode()
    p = replace(
        batch.points[0],
        provider_extra={
            **batch.points[0].provider_extra,
            "retained_notes": d["Results"]["Notes"],
        },
    )
    again = replace(batch, raw=raw, points=(p,), observed_at=clock[0])
    result = s.store.save_batches((again,))
    assert result == {"saved_points": 0, "saved_revisions": 0}
    assert s.status()["batch_count"] == 2


@pytest.mark.parametrize(
    "options",
    [
        {"query_mode": "history"},
        {"as_of": "not-a-time"},
        {"as_of": "2026-11-02"},
        {"query_mode": "current", "as_of": AT},
        {"as_of_basis": "official"},
        {"allow_upstream": 1},
    ],
)
def test_invalid_or_unsupported_temporal_modes_fail_before_sources(options):
    s, wire, _, _ = service()
    with pytest.raises(GdpTimeSeriesError):
        s.query_range("2026-Q3", "2026-Q3", **options)
    assert wire.calls == []


def test_unknown_confirmation_blocks_strict_history_until_explicit_recovery():
    s, wire, clock, _ = service()
    s.init_range("2026-Q3", "2026-Q3", series_ids=("A191RL",))
    clock[0] += timedelta(days=1)
    wire.overrides["A191RL", "2026Q3"] = "4.2"
    confirmed_clock = s.store.clock

    def lost_confirmation():
        raise OSError("synthetic lost confirmation")

    s.store.clock = lost_confirmation
    with pytest.raises(GdpTimeSeriesError, match="confirmation_outcome_unknown"):
        s.update_range("2026-Q3", "2026-Q3", series_ids=("A191RL",))
    assert (
        s.store.read_range(("A191RL",), "2026-Q3", "2026-Q3", basis="available") == []
    )
    earlier = s.store.read_range(
        ("A191RL",), "2026-Q3", "2026-Q3", AT + timedelta(hours=1), "available"
    )
    assert earlier[0]["value"] == "3.4"
    clock[0] += timedelta(hours=1)
    s.store.clock = confirmed_clock
    result = s.store.confirm_current_availability(("A191RL",), "2026-Q3", "2026-Q3")
    assert result["confirmed_points"] == 1
    assert result["confirmed_at"] > clock[0].isoformat()
    assert (
        s.store.read_range(("A191RL",), "2026-Q3", "2026-Q3", basis="available")[0][
            "value"
        ]
        == "4.2"
    )
    assert s.status()["revision_count"] == 1
