"""Real automatic/manual directory execution, publication and read-only queries."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.historical import tdx_p0_jobs, tdx_roster
from market_data_foundation.web import tdx_roster_api
from market_data_foundation.web.tdx_daily_api import (
    DailyRuntime,
    TdxOptions,
    tdx_router,
)
from market_data_foundation.web.tdx_roster_api import roster_router
from tests.test_tdx_p0 import Source
from tests.test_tdx_roster_query_date import RosterStore, at

ORIGIN = "http://192.168.10.20:18766"
PREFIX = "/api/r5/tdx/roster"
NAMES = {
    "000001": "平安银行",
    "000002": "万科Ａ",
    "000006": "深振业Ａ",
    "000007": "全新好",
    "000008": "神州高铁",
}


class Store(RosterStore):
    def __init__(self):
        super().__init__()
        official = self.snapshots[0]
        official.update(
            snapshot_id="exchange-13",
            observed_at=at("2026-11-13T18:00:00"),
            available_at=at("2026-11-13T18:01:00"),
        )
        official["details"].pop("directory_refreshed_at")
        official["members"] = [
            dict(
                market=0,
                instrument=c,
                name=n,
                evidence=dict(listing_date="1991-01-01", valid_through="2026-11-13"),
            )
            for c, n in NAMES.items()
        ]
        official["members"].extend(
            dict(
                market=0,
                instrument=c,
                name="退市样本" + c,
                evidence=dict(
                    listing_date="1991-01-01",
                    delist_date="2026-11-13",
                    valid_through="2026-11-13",
                ),
            )
            for c in ("000003", "000004", "000005")
        )
        official["members"].append(
            dict(
                market=0,
                instrument="000009",
                name="本次缺席",
                evidence=dict(listing_date="1991-01-01", valid_through="2026-11-13"),
            )
        )
        self.snapshots = [official]

    def save_universe(self, snapshot):
        value = deepcopy(snapshot)
        for key in ("observed_at", "available_at"):
            value[key] = datetime.fromisoformat(value[key])
        self.snapshots.append(value)

    def strategy_calendar(self, **_):
        return [
            dict(
                market=m,
                day="2026-11-18",
                is_open=True,
                available_at="2026-11-01T00:00:00Z",
            )
            for m in range(3)
        ]


class Rig:
    def __init__(self, root, monkeypatch):
        self.root = root
        self.current = at("2026-11-18T09:01:00")
        self.codes = {
            0: dict(NAMES) | {"000010": "首次出现"},
            1: {"600000": "沪市样本"},
            2: {"920002": "北交所样本"},
        }
        self.fail = set()
        self.calls = []
        self.store = Store()
        monkeypatch.setattr(tdx_p0_jobs, "now", lambda: self.current.isoformat())
        owner = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=UTC):
                return owner.current.astimezone(tz)

        monkeypatch.setattr(tdx_roster, "datetime", Clock)
        monkeypatch.setattr(tdx_roster_api, "datetime", Clock)
        self.runtime = DailyRuntime(
            TdxOptions(
                root / "runtime",
                root / "unused",
                "unused:1",
                allow_collect=True,
                origin=ORIGIN,
            ),
            self.store,
            self.source,
        )
        self.runtime.jobs.launch = lambda _: None
        app = FastAPI()
        app.include_router(tdx_router(self.runtime))
        app.include_router(roster_router(self.runtime))
        app.state.mdf_dev_read_origin = ORIGIN
        self.client = TestClient(app, base_url=ORIGIN, client=("192.168.10.25", 1234))

    def source(self, identity):
        owner = self

        class Directory(Source):
            def request(self, operation, market, code="", start=0, count=0):
                owner.calls.append((identity, operation, market, start))
                if market in owner.fail:
                    self.requests += 1
                    raise tdx_p0_jobs.TdxError("fixture_directory_timeout")
                result = super().request(operation, market, code, start, count)
                row = result["row"]
                row["received_at"] = (
                    owner.current - timedelta(seconds=10 - market)
                ).isoformat()
                entries = [
                    dict(Code=c, Name=n) for c, n in sorted(owner.codes[market].items())
                ]
                row["source_projection"] = (
                    dict(Count=len(entries))
                    if operation == "GetCount"
                    else dict(
                        Count=len(entries[start : start + 1000]),
                        List=entries[start : start + 1000],
                    )
                )
                raw = tdx_p0_jobs.encoded(row)
                Path(result["path"]).write_bytes(raw)
                return dict(
                    row=row, path=result["path"], sha256=sha256(raw).hexdigest()
                )

        return Directory(self.root)

    def manual(self):
        identity = str(uuid4())
        response = self.client.post(
            PREFIX + "/jobs",
            json=dict(request_id=identity),
            headers=dict(origin=ORIGIN),
        )
        assert response.status_code == 202, response.text
        return self.runtime.jobs.run(identity)

    def query(self, **params):
        before = len(self.calls)
        response = self.client.get(PREFIX + "/query", params=params)
        assert response.status_code == 200, response.text
        assert len(self.calls) == before, "GET must not construct or query a provider"
        return response.json()


def keyed(value):
    return {r["instrument"]: r for r in value["rows"]}


def test_automatic_and_manual_refresh_verify_every_returned_name_and_keep_lifecycle(
    tmp_path, monkeypatch
):
    rig = Rig(tmp_path, monkeypatch)
    rig.runtime.strategy.set(True)
    rig.runtime.strategy.tick(at("2026-11-18T09:00:00"))
    identity = tdx_p0_jobs.strategy_slot_id("2026-11-18", "universe")
    automatic = rig.runtime.jobs.run(identity)
    assert automatic["state"] == "COMPLETE" and automatic["origin"] == "periodic"
    first = rig.query()
    rows = keyed(first)
    for code in NAMES:
        row = rows[code]
        assert row["roster_membership_state"] == "IN_DIRECTORY"
        assert row["membership_state"] == "UNKNOWN"
        assert row["roster_verified_at"] == "2026-11-18T09:00:50+08:00"
        assert row["evidence"]["valid_through"] == "2026-11-13"
        assert row["roster_verification"]["request_id"] == identity
    for code in ("000003", "000004", "000005"):
        assert (
            rows[code]["roster_membership_state"]
            == rows[code]["membership_state"]
            == "DELISTED_OR_TERMINATED"
        )
    assert rows["000009"]["roster_membership_state"] == "NOT_IN_DIRECTORY"
    assert rows["000010"]["roster_membership_state"] == "IN_DIRECTORY"
    assert rows["000010"]["membership_state"] == "UNKNOWN"
    stored = deepcopy(rig.store.snapshots[-1])
    assert "000010" not in {m["instrument"] for m in stored["members"]}
    rig.current = at("2026-11-18T14:01:00")
    manual = rig.manual()
    assert manual["state"] == "COMPLETE" and manual["origin"] == "manual"
    second = keyed(rig.query())
    for code in NAMES:
        assert second[code]["roster_verified_at"] == "2026-11-18T14:00:50+08:00"
        assert second[code]["roster_name"] == rows[code]["roster_name"]
    assert rig.store.snapshots[-2] == stored, (
        "refresh cannot rewrite the previous publication"
    )


def test_missing_new_member_failure_retry_and_pinned_pages(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    old = rig.manual()
    first = rig.query(limit=2)
    rig.current = at("2026-11-18T14:01:00")
    rig.codes[0].pop("000010")
    rig.codes[0]["000011"] = "新出现二"
    rig.fail.add(2)
    failed = rig.manual()
    assert failed["state"] == "PARTIAL"
    retained = rig.query()
    assert retained["directory_snapshot_id"] == old["snapshot_id"]
    assert (
        keyed(retained)["000001"]["roster_verified_at"] == "2026-11-18T09:00:50+08:00"
    )
    assert retained["latest_refresh"]["request_id"] == failed["request_id"]
    assert retained["latest_refresh"]["state"] == "PARTIAL"
    rig.current = at("2026-11-18T16:01:00")
    rig.fail.clear()
    rig.runtime.jobs.resume(failed["request_id"], launch=False)
    resumed = rig.runtime.jobs.run(failed["request_id"])
    assert resumed["state"] == "COMPLETE"
    current = rig.query()
    rows = keyed(current)
    assert rows["000001"]["roster_verified_at"] == "2026-11-18T14:00:50+08:00", (
        "completed steps retain their real observation time"
    )
    assert rows["920002"]["roster_verified_at"] == "2026-11-18T16:00:52+08:00"
    assert rows["000010"]["roster_membership_state"] == "NOT_IN_DIRECTORY"
    assert rows["000011"]["roster_membership_state"] == "IN_DIRECTORY"
    page = rig.query(
        day=first["requested_day"],
        snapshot_id=first["snapshot_id"],
        directory_snapshot_id=first["directory_snapshot_id"],
        as_of=first["search_as_of"],
        offset=first["next_offset"],
        limit=20,
    )
    assert page["directory_snapshot_id"] == first["directory_snapshot_id"]
    assert "000010" in keyed(page) and "000011" not in keyed(page)


def test_legacy_complete_steps_recover_original_times_without_writing(
    tmp_path, monkeypatch
):
    rig = Rig(tmp_path, monkeypatch)
    rig.current = at("2026-11-17T09:01:00")
    job = rig.manual()
    legacy = rig.store.snapshots[-1]
    legacy["details"].pop("directory_verification")
    legacy["details"].pop("producer")
    for m in [*legacy["members"], *legacy["details"]["directory_only_candidates"]]:
        m["evidence"].pop("directory_verification", None)
    original = json.dumps(legacy, default=str, sort_keys=True)
    journal = rig.runtime.jobs.path(job["request_id"])
    before = journal.read_bytes()
    rig.current = at("2026-11-18T14:01:00")
    value = rig.query()
    assert value["directory_verification"]["method"] == "retained_job_steps"
    assert value["directory_verification"]["request_id"] == job["request_id"]
    assert keyed(value)["000001"]["roster_verified_at"] == "2026-11-17T09:00:50+08:00"
    assert keyed(value)["000001"]["roster_membership_state"] == "IN_DIRECTORY"
    assert json.dumps(legacy, default=str, sort_keys=True) == original
    assert journal.read_bytes() == before
    proof = job["steps"][0]["members"][0]["evidence"]
    raw = Path(proof["path"]).read_bytes()
    Path(proof["path"]).write_bytes(b"corrupt")
    bad = rig.query()
    assert keyed(bad)["000001"]["roster_membership_state"] == "UNVERIFIED"
    assert keyed(bad)["000001"]["roster_verified_at"] is None
    assert bad["directory_verification"]["state"] == "UNVERIFIED"
    Path(proof["path"]).write_bytes(raw)
    assert (
        keyed(rig.query())["000001"]["roster_verified_at"]
        == "2026-11-17T09:00:50+08:00"
    )
    old = rig.query(day="2026-11-13", as_of="2026-11-13T19:00:00+08:00")
    assert keyed(old)["000001"]["membership_state"] == "ACTIVE"
    assert keyed(old)["000001"]["roster_membership_state"] == "UNVERIFIED"


def test_explicit_previous_day_keeps_its_directory_after_new_publication(
    tmp_path, monkeypatch
):
    rig = Rig(tmp_path, monkeypatch)
    rig.current = at("2026-11-17T09:01:00")
    seventeenth = rig.manual()
    rig.current = at("2026-11-18T09:01:00")
    rig.codes[0]["000001"] = "十八日名称"
    eighteenth = rig.manual()
    current = rig.query()
    assert current["directory_snapshot_id"] == eighteenth["snapshot_id"]
    first = rig.query(day="2026-11-17", limit=2)
    assert first["directory_snapshot_id"] == seventeenth["snapshot_id"]
    assert first["directory_verification"]["applies_to_query_day"] is True
    assert keyed(first)["000001"]["roster_name"] == "平安银行"
    assert keyed(first)["000001"]["roster_verified_at"] == "2026-11-17T09:00:50+08:00"
    assert first["snapshot_id"] == eighteenth["snapshot_id"], (
        "official lifecycle publication selection keeps its existing semantics"
    )
    rig.current = at("2026-11-18T14:01:00")
    rig.manual()
    page = rig.query(
        day=first["requested_day"],
        snapshot_id=first["snapshot_id"],
        directory_snapshot_id=first["directory_snapshot_id"],
        as_of=first["search_as_of"],
        offset=first["next_offset"],
        limit=20,
    )
    assert page["directory_snapshot_id"] == first["directory_snapshot_id"]
    assert page["snapshot_id"] == first["snapshot_id"]
    assert page["search_as_of"] == first["search_as_of"]
    assert keyed(page)["000006"]["roster_verified_at"] == "2026-11-17T09:00:50+08:00"
    for cutoff, expected in [
        ("2026-11-17T09:00:31+08:00", None),
        ("2026-11-17T09:01:00+08:00", seventeenth["snapshot_id"]),
    ]:
        value = rig.query(day="2026-11-17", as_of=cutoff)
        assert value["directory_snapshot_id"] == expected
        assert keyed(value)["000001"]["roster_verified_at"] == (
            "2026-11-17T09:00:50+08:00" if expected else None
        )


@pytest.mark.parametrize(
    "fault",
    [
        "job_snapshot",
        "job_origin",
        "job_step",
        "member_drift",
        "raw_time",
        "no_matching_job",
        "missing_receipt",
        "scope_count",
        "producer_type",
    ],
)
def test_unqualified_evidence_cannot_claim_a_current_member(
    tmp_path, monkeypatch, fault
):
    rig = Rig(tmp_path, monkeypatch)
    job = rig.manual()
    publication = rig.store.snapshots[-1]
    if fault == "missing_receipt":
        publication["members"][0]["evidence"].pop("directory_verification")
    elif fault == "scope_count":
        publication["details"]["directory_verification"]["markets"][0]["members"] += 1
    elif fault == "producer_type":
        publication["details"]["producer"] = "unreadable identity"
    else:
        publication["details"].pop("directory_verification")
        record = rig.runtime.jobs.read(job["request_id"])
        if fault == "job_snapshot":
            record["snapshot_id"] = "unrelated"
        elif fault == "job_origin":
            record["origin"] = "periodic"
        elif fault == "job_step":
            record["steps"][0]["state"] = "FAILED"
        elif fault == "member_drift":
            record["steps"][0]["members"][0]["name"] = "not in retained response"
        elif fault == "raw_time":
            proof = record["steps"][0]["evidence"][0]
            value = json.loads(Path(proof["path"]).read_bytes())
            value["received_at"] = "2026-11-19T09:00:00+08:00"
            raw = tdx_p0_jobs.encoded(value)
            Path(proof["path"]).write_bytes(raw)
            proof["sha256"] = sha256(raw).hexdigest()
        else:
            publication["details"].pop("producer")
            record["snapshot_id"] = "not-this-publication"
        rig.runtime.jobs.save(record)
    value = rig.query()
    assert value["directory_verification"]["state"] == "UNVERIFIED"
    row = keyed(value)["000001"]
    assert row["roster_membership_state"] == "UNVERIFIED"
    assert row["roster_verified_at"] is None
    assert row["membership_state"] == "UNKNOWN"
    assert keyed(value)["000003"]["membership_state"] == "DELISTED_OR_TERMINATED"


@pytest.mark.parametrize("fault", ["ambiguous", "incomplete", "corrupt", "missing"])
def test_unbound_legacy_snapshot_requires_one_readable_complete_original_job(
    tmp_path, monkeypatch, fault
):
    rig = Rig(tmp_path, monkeypatch)
    job = rig.manual()
    publication = rig.store.snapshots[-1]
    publication["details"].pop("producer")
    publication["details"].pop("directory_verification")
    original = json.dumps(publication, default=str, sort_keys=True)
    if fault == "ambiguous":
        other = deepcopy(job)
        other["request_id"] = str(uuid4())
        rig.runtime.jobs.save(other)
    elif fault == "incomplete":
        job["steps"][0]["state"] = "FAILED"
        rig.runtime.jobs.save(job)
    elif fault == "corrupt":
        rig.runtime.jobs.path(job["request_id"]).write_bytes(b"invalid json")
    else:
        rig.runtime.jobs.path(job["request_id"]).unlink()
    value = rig.query()
    assert value["directory_verification"]["state"] == "UNVERIFIED"
    assert (
        value["directory_verification"]["cause_classification"]
        == {
            "ambiguous": "tdx_directory_job_ambiguous",
            "incomplete": "tdx_directory_incomplete",
            "corrupt": "tdx_directory_job_unreadable",
            "missing": "tdx_directory_job_unbound",
        }[fault]
    )
    assert keyed(value)["000001"]["roster_verified_at"] is None
    assert keyed(value)["000003"]["roster_membership_state"] == "DELISTED_OR_TERMINATED"
    assert json.dumps(publication, default=str, sort_keys=True) == original


def test_directory_return_does_not_erase_a_known_delisting(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.codes[0]["000003"] = "旧目录仍在列的退市证券"
    rig.manual()
    row = keyed(rig.query())["000003"]
    assert row["directory_membership_state"] == "PRESENT"
    assert (
        row["roster_membership_state"]
        == row["membership_state"]
        == "DELISTED_OR_TERMINATED"
    )
    assert row["roster_verified_at"] == "2026-11-18T09:00:50+08:00"
