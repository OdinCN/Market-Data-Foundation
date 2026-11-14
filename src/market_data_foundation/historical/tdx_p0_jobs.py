"Resumable DEV all-A daily jobs. Commit receipts precede page checkpoints."

import fcntl
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from queue import Empty, Queue
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from market_data_foundation.canonical.model import require
from market_data_foundation.historical.tdx_p0_roster import lifecycle
from market_data_foundation.historical.tdx_p0_source import (
    TdxError,
    classify,
    encoded,
    verify_wire,
)
from market_data_foundation.historical.tdx_roster import published, require_usable
from market_data_foundation.integration.tdx_daily_ingest import native_bar
from market_data_foundation.storage.tdx_daily_pg import PRIMARY_SOURCE, Batch

MARKETS = {0: "SZ", 1: "SH", 2: "BJ"}
PATTERNS = {
    0: r"(000|001|002|003|300|301|302)[0-9]{3}",
    1: r"(600|601|603|605|688)[0-9]{3}",
    2: r"(4[0-9]{5}|8[0-9]{5}|92[0-9]{4})",
}


def strategy_slot_id(day, action):
    return str(uuid5(NAMESPACE_URL, f"mdf-tdx-p0:{day}:{action}"))


def now():
    return datetime.now(UTC).isoformat()


def latest_day(at=None):
    local = (at or datetime.now(UTC)).astimezone(ZoneInfo("Asia/Shanghai"))
    return (
        local.date()
        if (local.hour, local.minute) >= (15, 5)
        else local.date() - timedelta(days=1)
    )


def atomic(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate(command):
    require(
        type(command) is dict
        and set(command)
        <= {"request_id", "action", "start", "end", "markets", "instruments"},
        "tdx_job_shape",
    )
    request_id = str(UUID(command["request_id"]))
    require(request_id == command["request_id"], "tdx_request_id")
    action = command["action"]
    require(action in {"universe", "init", "update"}, "tdx_action")
    markets = command.get("markets", [0, 1, 2])
    require(
        type(markets) is list
        and bool(markets)
        and all(type(m) is int and m in MARKETS for m in markets)
        and len(markets) == len(set(markets)),
        "tdx_markets",
    )
    latest = (
        datetime.now(ZoneInfo("Asia/Shanghai")).date()
        if action == "universe"
        else latest_day()
    )
    end = date.fromisoformat(command.get("end", latest.isoformat()))
    start = date.fromisoformat(
        command.get(
            "start",
            (
                date(1990, 1, 1) if action == "init" else end - timedelta(days=14)
            ).isoformat(),
        )
    )
    require(date(1990, 1, 1) <= start <= end <= latest, "tdx_after_close_range")
    instruments = command.get("instruments")
    require(
        instruments is None
        or type(instruments) is list
        and 1 <= len(instruments) <= 10000
        and len(set(instruments)) == len(instruments)
        and all(
            type(s) is str and re.fullmatch(r"[012]:[0-9]{6}", s) for s in instruments
        ),
        "tdx_instruments",
    )
    return dict(
        request_id=request_id,
        action=action,
        start=start.isoformat(),
        end=end.isoformat(),
        markets=markets,
        instruments=instruments,
    )


def batch_from_pending(pending):
    raw = Path(pending["path"]).read_bytes()
    require(sha256(raw).hexdigest() == pending["sha256"], "tdx_retained_digest")
    row = json.loads(raw)
    sent, wire = verify_wire(row)
    r = row["request"]
    bars = tuple(
        native_bar(r["market"], r["code"], b)
        for b in row["source_projection"]["List"] or []
        if pending["start"] <= b["Time"][:10] <= pending["end"]
    )
    if not bars:
        return None
    received = datetime.fromisoformat(row["received_at"])
    proof = dict(
        request_index=0,
        received_at=row["received_at"],
        line_sha256=pending["sha256"],
        wire_sha256=sha256(wire).hexdigest(),
        request_sha256=sha256(sent).hexdigest(),
        projection_sha256=sha256(encoded(row["source_projection"])).hexdigest(),
    )
    return Batch(
        pending["batch_id"],
        PRIMARY_SOURCE,
        received,
        datetime.fromisoformat(pending["admitted_at"]),
        bars,
        {f"{r['market']}:{r['code']}": [proof]},
        True,
        True,
        min(b.trading_day for b in bars),
        max(b.trading_day for b in bars),
    )


class DailyJobs:
    def __init__(self, directory, store, source_factory, *, workers=4):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.source_factory = source_factory
        require(type(workers) is int and 1 <= workers <= 8, "tdx_worker_budget")
        self.workers = workers
        self.save_lock = threading.RLock()
        self._summary_lock = threading.Lock()
        self._terminal_summaries = {}

    def path(self, identity):
        require(str(UUID(identity)) == identity, "tdx_request_id")
        return self.directory / (identity + ".json")

    @contextmanager
    def lock(self, identity):
        with self.path(identity).with_suffix(".lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TdxError("job_running") from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def read(self, identity):
        path = self.path(identity)
        if not path.exists():
            raise TdxError("job_not_found")
        record = json.loads(path.read_bytes())
        record.pop("completed_step_count", None)
        if "step_refs" in record:
            directory = self.directory / identity
            base_path = directory / "base.json"
            refs = record.pop("step_refs")
            base = (
                json.loads(base_path.read_bytes())
                if base_path.exists()
                else [None] * len(refs)
            )
            record["steps"] = [
                json.loads((directory / (key + ".json")).read_bytes())
                if (directory / (key + ".json")).exists()
                else item
                for key, item in zip(refs, base)
            ]
        return self._with_recovery_state(identity, record)

    def _with_recovery_state(self, identity, record):
        if record["state"] in {"RUNNING", "QUEUED"}:
            try:
                with self.lock(identity):
                    record["stored_state"] = record["state"]
                    record["state"] = "INTERRUPTED"
                    record["recoverable"] = True
            except TdxError:
                pass
        return record

    def list(self):
        return sorted(
            [
                self.read(p.stem)
                for p in self.directory.glob("*.json")
                if p.stem != "strategy"
            ],
            key=lambda j: j["created_at"],
            reverse=True,
        )

    def summaries(self):
        # Concurrent workbench tabs often request the same legacy journals. Only
        # one reader rebuilds their terminal summaries per atomic header version.
        with self._summary_lock:
            rows = [
                self._summary(path)
                for path in self.directory.glob("*.json")
                if path.stem != "strategy"
            ]
        return sorted(rows, key=lambda row: row["created_at"], reverse=True)

    def summary(self, identity):
        with self._summary_lock:
            return self._summary(self.path(identity))

    def _summary(self, path):
        stat = path.stat()
        version = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        cached = self._terminal_summaries.get(path.stem)
        if cached and cached[0] == version:
            return cached[1]
        record = json.loads(path.read_bytes())
        if "step_refs" in record and "completed_step_count" not in record:
            record = self.read(path.stem)
        if "step_refs" in record:
            count = len(record.pop("step_refs"))
            complete = record.pop("completed_step_count")
        else:
            steps = record.pop("steps")
            count = len(steps)
            complete = sum(step["state"] == "COMPLETE" for step in steps)
        row = self._with_recovery_state(path.stem, record)
        row.update(step_count=count, complete_steps=complete)
        if row["state"] in {"COMPLETE", "PARTIAL"}:
            self._terminal_summaries[path.stem] = (version, row)
        return row

    def save(self, record, step=None):
        with self.save_lock:
            self._save(record, step)

    def _save(self, record, step=None):
        record["updated_at"] = now()
        value = record
        if record["command"]["action"] != "universe" and record["steps"]:
            directory = self.directory / record["request_id"]
            directory.mkdir(exist_ok=True)
            if not (directory / "base.json").exists():
                atomic(directory / "base.json", record["steps"])
            selected = [step] if step else []
            for item in selected:
                atomic(directory / f"{item['market']}-{item['instrument']}.json", item)
            value = {k: v for k, v in record.items() if k != "steps"}
            value["completed_step_count"] = sum(
                item["state"] == "COMPLETE" for item in record["steps"]
            )
            value["step_refs"] = [
                f"{item['market']}-{item['instrument']}" for item in record["steps"]
            ]
        atomic(self.path(record["request_id"]), value)

    def submit(self, command, *, launch=True, origin=None, admission=None):
        command = validate(command)
        identity = command["request_id"]
        with self.lock(identity):
            if self.path(identity).exists():
                record = self.read(identity)
                require(record["command"] == command, "tdx_request_identity_conflict")
                if admission is not None:
                    require(
                        record.get("admission") == admission,
                        "tdx_receipt_manifest_conflict",
                    )
                return record
            record = dict(
                request_id=identity,
                command=command,
                state="QUEUED",
                origin=origin,
                created_at=now(),
                updated_at=now(),
                steps=[],
                provider_requests=0,
                resume_count=0,
            )
            if admission is not None:
                record["admission"] = admission
            self.save(record)
        if launch:
            self.launch(identity)
        return record

    def launch(self, identity):
        threading.Thread(
            target=self.run,
            args=(identity,),
            daemon=True,
            name="mdf-tdx-daily-" + identity[:8],
        ).start()

    def resume(self, identity, *, launch=True):
        with self.lock(identity):
            record = self.read(identity)
            if record["state"] == "COMPLETE":
                return record
            record["resume_count"] += 1
            if record.get("finished_at"):
                record["last_attempt_finished_at"] = record["finished_at"]
            record["finished_at"] = None
            record["resumed_at"] = now()
            for step in record["steps"]:
                pending = step.get("pending")
                terminal_overlap = False
                if pending and step.get("classification") == "tdx_page_no_progress":
                    retained = json.loads(Path(pending["path"]).read_bytes())
                    rows = retained["source_projection"]["List"] or []
                    terminal_overlap = bool(
                        len(rows) == 1
                        and rows[0]["Time"][:10] == step["previous_oldest"]
                    )
                if (
                    step.get("classification")
                    in {
                        "tdx_repeated_page",
                        "tdx_page_no_progress",
                        "tdx_moving_page_boundary",
                    }
                    and not terminal_overlap
                ):
                    step.update(
                        offset=0, previous_oldest=None, previous_page=None, pending=None
                    )
                    step["recovery_restarts"] = step.get("recovery_restarts", 0) + 1
                    self.save(record, step)
            record["state"] = "QUEUED"
            record.pop("classification", None)
            self.save(record)
        if launch:
            self.launch(identity)
        return record

    def run(self, identity):
        try:
            with self.lock(identity):
                record = self.read(identity)
                if record["state"] == "COMPLETE":
                    return record
                record["state"] = "RUNNING"
                record["started_at"] = record.get("started_at") or now()
                self.save(record)
                source = self.source_factory(identity)
                original_request = source.request

                def counted_request(*args, **kwargs):
                    before = source.requests
                    try:
                        return original_request(*args, **kwargs)
                    finally:
                        with self.save_lock:
                            record["provider_requests"] += source.requests - before

                source.request = counted_request
                try:
                    if record["command"]["action"] == "universe":
                        self._universe(record, source)
                    else:
                        self._daily(record, source)
                except Exception as error:
                    record["state"] = "PARTIAL"
                    record["classification"] = classify(error)
                finally:
                    source.close()
                    record["finished_at"] = now()
                    self.save(record)
                return record
        except TdxError as error:
            if error.classification != "job_running":
                raise

    def _universe(self, record, source):
        command = record["command"]
        if not record["steps"]:
            record["steps"] = [
                dict(
                    market=m,
                    state="PENDING",
                    offset=0,
                    members=[],
                    evidence=[],
                    duplicates=[],
                    count=None,
                )
                for m in command["markets"]
            ]
            self.save(record)
        for step in record["steps"]:
            if step["state"] == "COMPLETE":
                continue
            try:
                m = step["market"]
                if m < 2 and step["count"] is None:
                    outcome = source.request("GetCount", m)
                    step["count"] = outcome["row"]["source_projection"]["Count"]
                    step["evidence"].append({k: outcome[k] for k in ("path", "sha256")})
                    self.save(record)
                while True:
                    outcome = source.request(
                        "GetBJDirectory" if m == 2 else "GetCode",
                        m,
                        start=step["offset"],
                        count=0 if m == 2 else 1000,
                    )
                    data = outcome["row"]["source_projection"]
                    rows = data["List"] or []
                    require(data["Count"] == len(rows), "tdx_directory_count")
                    evidence = {k: outcome[k] for k in ("path", "sha256")}
                    step["evidence"].append(evidence)
                    seen = {v["instrument"] for v in step["members"]}
                    new_members = []
                    for v in rows:
                        if not re.fullmatch(PATTERNS[m], v["Code"]):
                            continue
                        require(v["Code"] not in seen, "tdx_directory_duplicate")
                        seen.add(v["Code"])
                        new_members.append(
                            dict(
                                market=m,
                                instrument=v["Code"],
                                name=v["Name"],
                                evidence=evidence,
                            )
                        )
                    step["members"].extend(new_members)
                    step["offset"] += len(rows)
                    self.save(record)
                    if m == 2 or len(rows) < 1000:
                        break
                    require(step["offset"] <= 65535, "tdx_directory_offset_exhausted")
                require(
                    m == 2 or step["offset"] == step["count"],
                    "tdx_directory_total_drift",
                )
                step["state"] = "COMPLETE"
                step.pop("classification", None)
            except Exception as error:
                step["state"] = "FAILED"
                step["classification"] = classify(error)
            self.save(record)
        members = [member for step in record["steps"] for member in step["members"]]
        observed = now()
        snapshot = dict(
            snapshot_id=uuid4().hex,
            observed_at=observed,
            available_at=now(),
            members=members,
            details=dict(
                markets=[
                    {k: v for k, v in step.items() if k != "members"}
                    for step in record["steps"]
                ],
                historical_membership="UNKNOWN_CURRENT_ROSTER_SURVIVOR_BIAS",
                official_reconciliation="NOT_VERIFIED",
                listing_delisting_intervals="UNKNOWN",
                code_reuse="UNKNOWN",
                suspension_status="NOT_BOUND",
            ),
        )
        previous = self.store.universe()
        if (
            previous
            and previous.get("details", {}).get("roster_kind") == "exchange_lifecycle"
            and all(s["state"] == "COMPLETE" for s in record["steps"])
            and set(command["markets"]) == {0, 1, 2}
        ):
            # A provider directory cannot promote unlisted candidates into the
            # formal exchange census. Retain them as reconciliation evidence.
            merged = {(m["market"], m["instrument"]): m for m in previous["members"]}
            candidates = [
                m for m in members if (m["market"], m["instrument"]) not in merged
            ]
            for item in members:
                known = merged.get((item["market"], item["instrument"]))
                if known and known["name"] != item["name"]:
                    known["evidence"] = dict(
                        known["evidence"],
                        directory_name=item["name"],
                        directory_name_observed_at=observed,
                    )
                elif known and "directory_name" in known["evidence"]:
                    known["evidence"] = {
                        key: value
                        for key, value in known["evidence"].items()
                        if key not in {"directory_name", "directory_name_observed_at"}
                    }
            snapshot["members"] = list(merged.values())
            snapshot["details"] = dict(
                previous["details"],
                directory_snapshot=snapshot["details"],
                directory_refreshed_at=observed,
                directory_only_candidates=candidates,
            )
        self.store.save_universe(snapshot)
        record["snapshot_id"] = snapshot["snapshot_id"]
        record["state"] = (
            "COMPLETE"
            if all(s["state"] == "COMPLETE" for s in record["steps"])
            and set(command["markets"]) == {0, 1, 2}
            else "PARTIAL"
        )

    def _daily(self, record, source):
        command = record["command"]
        universe, roster_status = published(
            self.store,
            date.fromisoformat(command["end"]),
            snapshot_id=record.get("snapshot_id") if record["steps"] else None,
        )
        record["roster"] = roster_status
        if not record["steps"]:
            record["snapshot_id"] = (universe or {}).get("snapshot_id")
            require_usable(universe, roster_status)
        require(universe is not None, "tdx_universe_required")
        if not record["steps"]:
            candidates = (
                universe["details"].get("directory_only_candidates", [])
                if roster_status.get("same_day_directory")
                else []
            )
            members = [
                m
                for m in [*universe["members"], *candidates]
                if m["market"] in command["markets"]
                and lifecycle(m, date.fromisoformat(command["end"])) != "NOT_YET_LISTED"
                and (
                    not m.get("evidence", {}).get("delist_date")
                    or m["evidence"]["delist_date"] > command["start"]
                )
                and (
                    command["instruments"] is None
                    or f"{m['market']}:{m['instrument']}" in command["instruments"]
                )
            ]
            require(bool(members), "tdx_universe_selection_empty")
            if command["instruments"]:
                require(
                    {f"{m['market']}:{m['instrument']}" for m in members}
                    == set(command["instruments"]),
                    "tdx_instrument_not_in_roster",
                )
            record["snapshot_id"] = universe["snapshot_id"]
            record["steps"] = [
                dict(
                    market=m["market"],
                    instrument=m["instrument"],
                    name=m["name"],
                    state="PENDING",
                    offset=0,
                    pages=0,
                    rows=0,
                    first=None,
                    last=None,
                    previous_oldest=None,
                    previous_page=None,
                    pending=None,
                    counts=dict(new=0, unchanged=0, changed=0, blocked=0, replays=0),
                )
                for m in members
            ]
            self.save(record)
        else:
            # Older executors selected the entire catalogue for increments.
            # Reconcile the existing request against dated evidence before
            # resuming, retaining completed receipts and the original identity.
            dated = {(m["market"], m["instrument"]): m for m in universe["members"]}
            for step in record["steps"]:
                member = dated.get((step["market"], step["instrument"]))
                if (
                    step["state"] == "COMPLETE"
                    or member is None
                    or step.get("pending") is not None
                ):
                    continue
                evidence = member.get("evidence", {})
                if (
                    (evidence.get("listing_date") or "") > command["end"]
                    or evidence.get("delist_date")
                    and evidence["delist_date"] <= command["start"]
                ):
                    step.update(
                        state="COMPLETE",
                        boundary="EXCLUDED_BY_EXCHANGE_LIFECYCLE",
                        scope_snapshot_id=universe["snapshot_id"],
                    )
                    step.pop("classification", None)
                    self.save(record, step)
        pending = Queue()
        from itertools import zip_longest

        by_market = [
            [
                step
                for step in record["steps"]
                if step["market"] == market and step["state"] != "COMPLETE"
            ]
            for market in MARKETS
        ]
        for group in zip_longest(*by_market):
            for step in group:
                if step is not None:
                    pending.put(step)
        stopped = threading.Event()

        def worker(index):
            current = (
                source if index == 0 else self.source_factory(record["request_id"])
            )
            if index:
                original = current.request

                def counted(*args, **kwargs):
                    before = current.requests
                    try:
                        return original(*args, **kwargs)
                    finally:
                        with self.save_lock:
                            record["provider_requests"] += current.requests - before

                current.request = counted
            failures = 0
            try:
                while not stopped.is_set():
                    try:
                        step = pending.get_nowait()
                    except Empty:
                        break
                    success = self._collect_step(record, current, step)
                    failures = 0 if success else failures + 1
                    if failures >= 3:
                        record["classification"] = (
                            "source_circuit_open_resume_incomplete"
                        )
                        stopped.set()
            finally:
                close_writer = getattr(self.store, "close_writer", None)
                if close_writer:
                    close_writer()
                if index:
                    current.close()

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(worker, range(min(self.workers, pending.qsize()))))
        record["state"] = (
            "COMPLETE"
            if all(s["state"] == "COMPLETE" for s in record["steps"])
            else "PARTIAL"
        )

    def _collect_step(self, record, source, step):
        command = record["command"]
        step["state"] = "RUNNING"
        self.save(record, step)
        try:
            while True:
                if step["pending"] is None:
                    require(step["offset"] <= 65535, "tdx_offset_exhausted")
                    outcome = source.request(
                        "GetKline",
                        step["market"],
                        step["instrument"],
                        step["offset"],
                        getattr(
                            source,
                            "page_size",
                            800 if command["action"] == "init" else 32,
                        ),
                    )
                    step["pending"] = dict(
                        path=outcome["path"],
                        sha256=outcome["sha256"],
                        admitted_at=now(),
                        batch_id="p0-" + outcome["row"]["request"]["id"],
                        start=command["start"],
                        end=command["end"],
                    )
                    if outcome.get("retained"):
                        record["retained_responses"] = (
                            record.get("retained_responses", 0) + 1
                        )
                    # Persist immutable write identity BEFORE commit.
                    self.save(record, step)
                pending = step["pending"]
                batch = batch_from_pending(pending)
                raw = json.loads(Path(pending["path"]).read_bytes())
                data = raw["source_projection"]
                rows = data["List"] or []
                days = [r["Time"][:10] for r in rows]
                page_digest = sha256(
                    encoded([(d, r["Open"], r["Close"]) for d, r in zip(days, rows)])
                ).hexdigest()
                require(
                    not days or page_digest != step["previous_page"],
                    "tdx_repeated_page",
                )
                require(
                    not days
                    or step["previous_oldest"] is None
                    or days[0] < step["previous_oldest"]
                    or len(rows) == 1
                    and days[0] == step["previous_oldest"],
                    "tdx_page_no_progress",
                )
                require(
                    not days
                    or step["previous_oldest"] is None
                    or step["previous_oldest"] in days,
                    "tdx_moving_page_boundary",
                )
                if batch:
                    result = self.store.write(
                        batch
                    )  # Unknown commit leaves pending for exact replay.
                    for k in ("new", "unchanged", "changed", "blocked"):
                        step["counts"][k] += getattr(result, k)
                    step["counts"]["replays"] += int(result.replay)
                    step["rows"] += len(batch.bars)
                    first, last = (
                        batch.day_start.isoformat(),
                        batch.day_end.isoformat(),
                    )
                    step["first"] = min(step["first"] or first, first)
                    step["last"] = max(step["last"] or last, last)
                step["pages"] += 1
                step["previous_page"] = page_digest
                step["previous_oldest"] = min(days) if days else step["previous_oldest"]
                step["pending"] = None
                step.pop("classification", None)
                if len(rows) < raw["request"]["count"]:
                    step["state"] = "COMPLETE"
                    step["boundary"] = (
                        "SOURCE_EMPTY_NO_HISTORY_AVAILABLE"
                        if not step["rows"]
                        else "SOURCE_EXHAUSTED_LISTING_UNKNOWN"
                    )
                    self.save(record, step)
                    break
                if days[0] <= command["start"]:
                    step["state"] = "COMPLETE"
                    step["boundary"] = "REQUESTED_WINDOW_REACHED"
                    self.save(record, step)
                    break
                step["offset"] += raw["request"]["count"] - 1
                self.save(record, step)
            return True
        except Exception as error:
            step["state"] = "FAILED"
            step["classification"] = classify(error)
            source.close()
            self.save(record, step)
            return False


def coverage(store, day, *, as_of=None, job=None):
    cutoff = as_of or datetime.now(UTC)
    snapshot = store.universe(as_of=cutoff, day=day)
    data = store.query(start=day, end=day, as_of=cutoff, limit=10000)
    rows = {(r["market"], r["instrument"]): r for r in data["rows"]}
    cutoff = data["as_of"]
    failures = {
        (s["market"], s.get("instrument")): s
        for s in (job or {}).get("steps", [])
        if s["state"] == "FAILED"
        and job["command"]["start"] <= day.isoformat() <= job["command"]["end"]
        and datetime.fromisoformat(job["updated_at"]) <= cutoff
    }
    result = []
    missing = []
    for market, name in MARKETS.items():
        members = [
            m for m in (snapshot or {}).get("members", []) if m["market"] == market
        ]
        market_steps = [
            s
            for s in (snapshot or {}).get("details", {}).get("markets", [])
            if s["market"] == market
        ]
        roster_known = bool(market_steps and market_steps[0]["state"] == "COMPLETE")
        enumeration = market_steps[0] if market_steps else {}
        enumeration_known = bool(
            enumeration.get("enumeration_from")
            and enumeration["enumeration_from"]
            <= day.isoformat()
            <= enumeration["enumeration_through"]
        )
        counts = dict(
            valid_bars=0,
            zero_volume_bars=0,
            failed=0,
            unknown=0,
            pre_listing=0,
            post_delisting=0,
            market_closed=0,
            confirmed_suspended=0,
            suspended_with_bars=0,
            active_count=0,
            lifecycle_unknown=0,
        )
        details = (snapshot or {}).get("details", {})
        calendar = next(
            (
                c
                for c in details.get("calendar", [])
                if c["market"] == market
                and c["day"] == day.isoformat()
                and datetime.fromisoformat(c["available_at"]) <= cutoff
            ),
            None,
        )
        for member in members:
            key = (market, member["instrument"])
            state = lifecycle(member, day)
            if state in {"NOT_YET_LISTED", "DELISTED_OR_TERMINATED"}:
                counts[
                    "pre_listing" if state == "NOT_YET_LISTED" else "post_delisting"
                ] += 1
                continue
            counts["active_count" if state == "ACTIVE" else "lifecycle_unknown"] += 1
            if calendar and not calendar["is_open"]:
                counts["market_closed"] += 1
                continue
            suspended = next(
                (
                    s
                    for s in details.get("suspensions", [])
                    if (s["market"], s["instrument"]) == key
                    and s["start"] <= day.isoformat() <= s["end"]
                    and datetime.fromisoformat(s["available_at"]) <= cutoff
                ),
                None,
            )
            if state == "ACTIVE" and suspended:
                counts["confirmed_suspended"] += 1
                counts["suspended_with_bars"] += int(key in rows)
            elif key in rows:
                counts["valid_bars"] += 1
                counts["zero_volume_bars"] += int(rows[key]["volume"] == 0)
            else:
                classification = (
                    failures[key]["classification"]
                    if key in failures
                    else "NO_BAR_STATUS_UNKNOWN"
                )
                counts["failed" if key in failures else "unknown"] += 1
                missing.append(
                    dict(
                        market=market,
                        instrument=member["instrument"],
                        name=member["name"],
                        classification=classification,
                        recover_start=day,
                        recover_end=day,
                    )
                )
        result.append(
            dict(
                market=market,
                exchange=name,
                roster_count=len(members) if roster_known else None,
                observed_roster_count=len(members),
                effective_count=counts["active_count"] + counts["lifecycle_unknown"],
                enumeration_known=enumeration_known,
                expected_trading_count=(
                    0
                    if calendar and not calendar["is_open"]
                    else counts["active_count"] - counts["confirmed_suspended"]
                    if calendar and not counts["lifecycle_unknown"]
                    else None
                ),
                calendar_state="OPEN"
                if calendar and calendar["is_open"]
                else "CLOSED"
                if calendar
                else "UNKNOWN",
                complete=bool(
                    roster_known
                    and enumeration_known
                    and calendar
                    and not counts["unknown"]
                    and not counts["failed"]
                    and not counts["lifecycle_unknown"]
                ),
                **counts,
            )
        )
    applicable = [r for r in result if r["calendar_state"] != "CLOSED"]
    return dict(
        day=day,
        as_of=data["as_of"],
        snapshot_id=(snapshot or {}).get("snapshot_id"),
        markets=result,
        missing=missing,
        applicable=bool(applicable),
        complete=bool(applicable) and all(r["complete"] for r in applicable),
        denominator_semantics=(
            (snapshot or {})
            .get("details", {})
            .get(
                "historical_membership",
                "current source roster; historical listing/delisting and "
                "official reconciliation unknown",
            )
        ),
        price_basis="native_projection",
        suspension_semantics="zero volume or empty response never proves suspension",
        provider_requests=0,
    )
