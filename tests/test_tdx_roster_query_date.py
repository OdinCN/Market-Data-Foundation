"""Roster reads use visible directory dates, never the daily close or census cap."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.historical import tdx_roster
from market_data_foundation.storage.tdx_p0 import PgStore
from market_data_foundation.web import tdx_roster_api
from market_data_foundation.web.tdx_roster_api import roster_router


def at(value):
    return datetime.fromisoformat(value + "+08:00")


class RosterStore:
    """Independent publication/visibility oracle, no provider or storage writes."""

    def __init__(self):
        self.calls = []
        members = [
            dict(
                market=0,
                instrument="000002",
                name="万科Ａ",
                evidence=dict(listing_date="1991-01-29", valid_through="2026-11-13"),
            ),
            dict(
                market=0,
                instrument="000003",
                name="已退市样本",
                evidence=dict(listing_date="1991-01-01", delist_date="2026-11-13"),
            ),
            dict(market=1, instrument="600001", name="无上市证据", evidence={}),
        ]
        self.snapshots = [
            dict(
                snapshot_id="directory-13",
                observed_at=at("2026-11-13T09:00:00"),
                available_at=at("2026-11-13T09:00:32"),
                members=members,
                details=dict(
                    roster_kind="exchange_lifecycle",
                    valid_through="2026-11-13",
                    directory_refreshed_at="2026-11-13T09:00:00+08:00",
                    markets=[dict(market=m, state="COMPLETE") for m in (0, 1, 2)],
                ),
            )
        ]
        fresh = deepcopy(self.snapshots[0])
        fresh.update(
            snapshot_id="directory-17",
            observed_at=at("2026-11-17T09:00:00"),
            available_at=at("2026-11-17T09:00:32"),
        )
        fresh["details"]["directory_refreshed_at"] = "2026-11-17T09:00:00+08:00"
        # A name difference is not evidence of either listing or directory presence.
        fresh["members"][0]["evidence"]["directory_name"] = "万科A"
        self.snapshots.append(fresh)
        failed = deepcopy(fresh)
        failed.update(
            snapshot_id="incomplete-17", available_at=at("2026-11-17T09:01:00")
        )
        failed["details"]["markets"][2]["state"] = "FAILED"
        self.snapshots.append(failed)
        # A later import of old exchange evidence must not displace the directory.
        imported = deepcopy(fresh)
        imported.update(
            snapshot_id="exchange-import-17", available_at=at("2026-11-17T10:00:00")
        )
        imported["details"].pop("directory_refreshed_at")
        self.snapshots.append(imported)

    def universe(
        self, snapshot_id=None, as_of=None, *, day=None, prefer_directory=False
    ):
        as_of = as_of or tdx_roster.datetime.now(UTC)
        self.calls.append((snapshot_id, as_of, day, prefer_directory))
        visible = [
            s
            for s in self.snapshots
            if s["available_at"] <= as_of
            and (
                s["snapshot_id"] == snapshot_id
                if snapshot_id
                else all(m["state"] == "COMPLETE" for m in s["details"]["markets"])
            )
        ]
        if not visible:
            return None

        def rank(s):
            details = s["details"]
            refreshed = (
                details.get("directory_refreshed_at", "") if prefer_directory else ""
            )
            return (
                refreshed[:10],
                bool(day and details.get("valid_through") == day.isoformat()),
                details.get("roster_kind") == "exchange_lifecycle",
                s["available_at"],
            )

        return deepcopy(max(visible, key=rank))

    def name_changes(self, day, cutoff, market):
        return {}

    def observed_names(self, day, cutoff, market):
        return {}


def late_exchange_store():
    """Nov 13 directory kept Nov 12 evidence; Nov 13 originals arrived Nov 14."""
    store = RosterStore()
    directory = store.snapshots[0]
    directory["details"]["valid_through"] = "2026-11-12"
    directory["members"][0]["evidence"]["valid_through"] = "2026-11-12"
    directory["details"]["sources"] = [
        dict(
            kind="sz_current",
            url="https://fixture.invalid/exchange-12",
            sha256="a" * 64,
        )
    ]
    official = deepcopy(directory)
    official.update(
        snapshot_id="exchange-13",
        observed_at=at("2026-11-14T07:00:00"),
        available_at=at("2026-11-14T07:15:00"),
    )
    official["details"].pop("directory_refreshed_at")
    official["details"]["valid_through"] = "2026-11-13"
    official["members"][0]["evidence"]["valid_through"] = "2026-11-13"
    official["details"]["sources"] = [
        dict(
            kind="sz_current",
            url="https://fixture.invalid/exchange-13",
            sha256="b" * 64,
        )
    ]
    store.snapshots.append(official)
    return store


@contextmanager
def roster_client(monkeypatch, clock="2026-11-17T09:05:00", store=None):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=UTC):
            return at(clock).astimezone(tz)

    monkeypatch.setattr(tdx_roster, "datetime", Clock)
    monkeypatch.setattr(tdx_roster_api, "datetime", Clock)
    store = store or RosterStore()
    runtime = SimpleNamespace(
        store=store,
        options=SimpleNamespace(allow_read=True, allow_collect=False),
    )
    app = FastAPI()
    app.include_router(roster_router(runtime))
    with TestClient(app) as client:
        yield client, store


@pytest.mark.parametrize(
    "clock,expected,snapshot,state",
    [
        ("2026-11-17T08:59:59", "2026-11-13", "directory-13", "ACTIVE"),
        ("2026-11-17T09:00:31.999999", "2026-11-13", "directory-13", "ACTIVE"),
        ("2026-11-17T09:00:32", "2026-11-17", "directory-17", "UNKNOWN"),
        ("2026-11-17T11:00:00", "2026-11-17", "exchange-import-17", "UNKNOWN"),
        ("2026-11-18T08:30:00", "2026-11-17", "exchange-import-17", "UNKNOWN"),
        ("2026-11-14T12:00:00", "2026-11-13", "directory-13", "ACTIVE"),
    ],
)
def test_morning_default_status_and_query_agree_without_census_or_close_cap(
    monkeypatch, clock, expected, snapshot, state
):
    with roster_client(monkeypatch, clock) as (client, _):
        status = client.get("/api/r5/tdx/roster/status")
        response = client.get("/api/r5/tdx/roster/query?search=000002")
        assert status.status_code == response.status_code == 200
        facts, value = status.json(), response.json()
        assert facts["default_query_day"] == value["requested_day"] == expected
        assert facts["snapshot_id"] == value["snapshot_id"] == snapshot
        assert value["directory_observed_day"] == expected
        assert value["directory_snapshot_id"] == "directory-" + expected[-2:]
        assert value["valid_through"] == "2026-11-13"
        assert value["rows"][0]["membership_state"] == state
        assert value["provider_requests"] == facts["provider_requests"] == 0


def test_explicit_day_historical_cutoff_and_missing_publication(monkeypatch):
    with roster_client(monkeypatch, "2026-11-17T11:00:00") as (client, _):
        old = client.get("/api/r5/tdx/roster/query?day=2026-11-13&search=000002").json()
        assert old["requested_day"] == "2026-11-13"
        assert old["rows"][0]["membership_state"] == "ACTIVE"
        for cutoff, expected, identity in [
            ("2026-11-17T09:00:31.999999+08:00", "2026-11-13", "directory-13"),
            ("2026-11-17T09:00:32+08:00", "2026-11-17", "directory-17"),
        ]:
            q = {"as_of": cutoff}
            status = client.get("/api/r5/tdx/roster/status", params=q).json()
            value = client.get("/api/r5/tdx/roster/query", params=q).json()
            assert value["requested_day"] == status["default_query_day"] == expected
            assert value["snapshot_id"] == status["snapshot_id"] == identity
            assert datetime.fromisoformat(
                value["search_as_of"]
            ) == datetime.fromisoformat(cutoff)
        # An explicit later day does not borrow facts unavailable at the cutoff.
        value = client.get(
            "/api/r5/tdx/roster/query",
            params={
                "day": "2026-11-17",
                "as_of": "2026-11-13T10:00:00+08:00",
            },
        ).json()
        assert value["requested_day"] == "2026-11-17"
        assert value["snapshot_id"] == "directory-13"
        assert value["rows"][0]["membership_state"] == "UNKNOWN"
        missing = client.get(
            "/api/r5/tdx/roster/query",
            params={
                "as_of": "2026-11-13T09:00:31+08:00",
            },
        ).json()
        assert missing["state"] == "MISSING"
        assert missing["snapshot_id"] is None and missing["rows"] == []
        for value in ("naive", "2026-11-17T09:00:00"):
            assert (
                client.get(
                    "/api/r5/tdx/roster/status", params={"as_of": value}
                ).status_code
                == 400
            )


def test_old_exchange_import_has_no_directory_attestation_or_member_upgrade(
    monkeypatch,
):
    store = RosterStore()
    store.snapshots = [store.snapshots[-1]]
    with roster_client(monkeypatch, "2026-11-17T11:00:00", store) as (client, _):
        value = client.get("/api/r5/tdx/roster/query").json()
        assert value["requested_day"] == "2026-11-13"
        assert value["directory_observed_day"] is None
        assert [r["membership_state"] for r in value["rows"]] == [
            "ACTIVE",
            "DELISTED_OR_TERMINATED",
            "UNKNOWN",
        ]
        later = client.get("/api/r5/tdx/roster/query?day=2026-11-17").json()
        assert [r["membership_state"] for r in later["rows"]] == [
            "UNKNOWN",
            "DELISTED_OR_TERMINATED",
            "UNKNOWN",
        ]


def test_default_page_keeps_exact_snapshot_and_cutoff_after_new_publication(
    monkeypatch,
):
    with roster_client(monkeypatch, "2026-11-17T09:05:00") as (client, store):
        first = client.get("/api/r5/tdx/roster/query?limit=1").json()
        assert first["requested_day"] == "2026-11-17"
        newer = deepcopy(store.snapshots[1])
        newer.update(
            snapshot_id="directory-new", available_at=at("2026-11-17T09:04:00")
        )
        newer["members"][1]["name"] = "新版本不能混页"
        store.snapshots.append(newer)
        second = client.get(
            "/api/r5/tdx/roster/query",
            params={
                "snapshot_id": first["snapshot_id"],
                "as_of": first["search_as_of"],
                "offset": first["next_offset"],
                "limit": 1,
            },
        ).json()
        assert second["snapshot_id"] == first["snapshot_id"] == "directory-17"
        assert (
            second["directory_snapshot_id"]
            == first["directory_snapshot_id"]
            == "directory-17"
        )
        assert second["rows"][0]["name"] == "已退市样本"
        # Each read resolves directory date, then evidence. The actual member
        # snapshot on the second page remains the original explicit identity.
        assert [c[0] for c in store.calls] == [None, None, None, first["snapshot_id"]]


def test_directory_only_receipt_uses_beijing_day_without_certifying_members(
    monkeypatch,
):
    store = RosterStore()
    snapshot = store.snapshots[1]
    snapshot["observed_at"] = datetime(2026, 11, 17, 1, tzinfo=UTC)
    snapshot["details"] = dict(
        markets=[dict(market=m, state="COMPLETE") for m in (0, 1, 2)],
        historical_membership="UNKNOWN_CURRENT_ROSTER_SURVIVOR_BIAS",
    )
    snapshot["members"] = [
        dict(market=0, instrument="000002", name="万科Ａ", evidence={})
    ]
    store.snapshots = [snapshot]
    with roster_client(monkeypatch, store=store) as (client, _):
        value = client.get("/api/r5/tdx/roster/query").json()
        assert value["directory_observed_day"] == value["requested_day"] == "2026-11-17"
        assert value["valid_through"] is None
        assert value["rows"][0]["membership_state"] == "UNKNOWN"


def test_pg_roster_preference_is_opt_in_and_preserves_existing_daily_selection():
    class Cursor:
        def __init__(self):
            self.queries = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def cursor(self, **_):
            return self

        def execute(self, sql, params):
            self.queries.append((sql, params))

        def fetchone(self):
            return None

    cursor = Cursor()
    store = PgStore(lambda: cursor)
    cutoff = at("2026-11-17T09:05:00")
    store.universe(as_of=cutoff, day=date(2026, 11, 17))
    legacy, legacy_params = cursor.queries[-1]
    store.universe(as_of=cutoff, day=date(2026, 11, 17), prefer_directory=True)
    roster, roster_params = cursor.queries[-1]
    assert "directory_refreshed_at" not in legacy
    order = roster.split("ORDER BY", 1)[1]
    assert order.index("directory_refreshed_at") < order.index("valid_through'=")
    assert "observed_at AT TIME ZONE 'Asia/Shanghai'" in order
    assert "available_at <= %s" in roster
    assert "bool_and(s->>'state'='COMPLETE')" in roster
    assert roster_params == legacy_params


@pytest.mark.parametrize(
    "cutoff,day,identity,through,state",
    [
        ("2026-11-13T16:20:00", "2026-11-13", "directory-13", "2026-11-12", "UNKNOWN"),
        ("2026-11-13T21:30:00", "2026-11-13", "directory-13", "2026-11-12", "UNKNOWN"),
        (
            "2026-11-14T07:14:59.999999",
            "2026-11-13",
            "directory-13",
            "2026-11-12",
            "UNKNOWN",
        ),
        ("2026-11-14T07:15:00", "2026-11-13", "exchange-13", "2026-11-13", "ACTIVE"),
        ("2026-11-14T08:00:00", "2026-11-13", "exchange-13", "2026-11-13", "ACTIVE"),
        ("2026-11-14T18:00:00", "2026-11-13", "exchange-13", "2026-11-13", "ACTIVE"),
        ("2026-11-17T09:00:31", "2026-11-13", "exchange-13", "2026-11-13", "ACTIVE"),
        ("2026-11-17T09:01:00", "2026-11-17", "directory-17", "2026-11-13", "UNKNOWN"),
    ],
)
def test_implicit_and_explicit_same_day_use_visible_exchange_evidence(
    monkeypatch, cutoff, day, identity, through, state
):
    with roster_client(monkeypatch, store=late_exchange_store()) as (client, _):
        params = dict(as_of=cutoff + "+08:00", search="000002")
        implicit = client.get("/api/r5/tdx/roster/query", params=params)
        explicit = client.get("/api/r5/tdx/roster/query", params=dict(params, day=day))
        status = client.get(
            "/api/r5/tdx/roster/status", params=dict(as_of=params["as_of"])
        )
        assert implicit.status_code == explicit.status_code == status.status_code == 200
        a, b, facts = implicit.json(), explicit.json(), status.json()
        assert (
            a["requested_day"]
            == b["requested_day"]
            == facts["default_query_day"]
            == day
        )
        assert a["snapshot_id"] == b["snapshot_id"] == facts["snapshot_id"] == identity
        assert a["valid_through"] == b["valid_through"] == through
        assert (
            a["rows"][0]["membership_state"]
            == b["rows"][0]["membership_state"]
            == state
        )
        assert a["rows"][0]["evidence"] == b["rows"][0]["evidence"]
        assert a["sources"] == b["sources"]
        assert a["directory_observed_day"] == b["directory_observed_day"] == day
        assert (
            a["directory_snapshot_id"]
            == b["directory_snapshot_id"]
            == "directory-" + day[-2:]
        )
        assert datetime.fromisoformat(a["available_at"]) <= at(cutoff)
        assert datetime.fromisoformat(a["directory_available_at"]) <= at(cutoff)
        if identity == "exchange-13":
            assert a["directory_observed_at"] == "2026-11-13T09:00:00+08:00"
            assert a["observed_at"] == "2026-11-14T07:00:00+08:00"
            assert a["sources"][0]["url"] == "https://fixture.invalid/exchange-13"


def test_late_exchange_page_pins_evidence_directory_and_cutoff(monkeypatch):
    store = late_exchange_store()
    with roster_client(monkeypatch, "2026-11-14T18:00:00", store) as (client, _):
        first = client.get(
            "/api/r5/tdx/roster/query",
            params={
                "as_of": "2026-11-14T08:00:00+08:00",
                "limit": 1,
            },
        ).json()
        assert first["snapshot_id"] == "exchange-13"
        assert first["rows"][0]["membership_state"] == "ACTIVE"
        correction = deepcopy(store.snapshots[-1])
        correction.update(
            snapshot_id="exchange-13-later", available_at=at("2026-11-14T12:00:00")
        )
        correction["members"][1]["name"] = "新证据版本不可混页"
        store.snapshots.append(correction)
        assert (
            client.get("/api/r5/tdx/roster/query").json()["snapshot_id"]
            == "exchange-13-later"
        )
        page = client.get(
            "/api/r5/tdx/roster/query",
            params={
                "day": first["requested_day"],
                "snapshot_id": first["snapshot_id"],
                "as_of": first["search_as_of"],
                "offset": first["next_offset"],
                "limit": 1,
            },
        ).json()
        assert page["snapshot_id"] == first["snapshot_id"]
        assert (
            page["directory_snapshot_id"]
            == first["directory_snapshot_id"]
            == "directory-13"
        )
        assert page["directory_observed_day"] == "2026-11-13"
        assert page["search_as_of"] == first["search_as_of"]
        assert page["rows"][0]["name"] == "已退市样本"
        # Pinning an older publication deliberately retains its original evidence.
        pinned = client.get(
            "/api/r5/tdx/roster/query",
            params={
                "day": "2026-11-13",
                "snapshot_id": "directory-13",
                "search": "000002",
            },
        ).json()
        assert pinned["snapshot_id"] == "directory-13"
        assert pinned["rows"][0]["membership_state"] == "UNKNOWN"
