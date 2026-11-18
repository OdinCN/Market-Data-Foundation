"""Build two large offline journals; no provider, database or business POST."""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from shutil import copytree
from uuid import NAMESPACE_URL, uuid5


def prepare_corrupt(root):
    from tempfile import TemporaryDirectory
    from uuid import uuid4

    from tests.test_tdx_morning_binding import DAILY, DAY, LATEST, Replay

    root.mkdir(parents=True, exist_ok=False)
    with TemporaryDirectory() as temporary:
        replay = Replay(Path(temporary))
        morning = replay.morning()
        roster = replay.manual_roster(LATEST)
        identity = str(uuid4())
        replay.post(
            DAILY + "/jobs", dict(request_id=identity, action="update", end=DAY)
        )
        assert replay.runtime.jobs.run(identity)["state"] == "COMPLETE"
        path = replay.runtime.jobs.path(morning["request_id"])
        (root / "repair-header.bin").write_bytes(path.read_bytes())
        path.write_bytes(b"{")
        replay.dispatch()
        copytree(replay.runtime.jobs.directory, root / "journal")
        copytree(replay.runtime.strategy.path.parent, root / "strategy")
        value = dict(
            daily=identity,
            roster=roster["request_id"],
            corrupt=morning["request_id"],
            day=DAY,
        )
        (root / "fixture-only.json").write_text(json.dumps(value))


def repair_corrupt(root):
    from market_data_foundation.historical.tdx_p0_jobs import DailyJobs
    from market_data_foundation.historical.tdx_p0_strategy import DailyStrategy
    from tests.test_tdx_morning_binding import Publications

    value = json.loads((root / "fixture-only.json").read_text())
    (root / "journal" / (value["corrupt"] + ".json")).write_bytes(
        (root / "repair-header.bin").read_bytes()
    )

    def forbidden(*_):
        raise AssertionError("no dispatch while repairing isolated evidence")

    jobs = DailyJobs(root / "journal", Publications(), forbidden)
    jobs.launch = forbidden
    strategy = DailyStrategy(jobs, directory=root / "strategy")
    # Simulated 09:01 recheck, before daily admission; no source or job dispatch.
    strategy.tick(datetime(2026, 11, 16, 1, 1, tzinfo=UTC))
    assert strategy.read()["blocked"] is None


def prepare(root):
    root.mkdir(parents=True, exist_ok=False)
    directory = root / "journal"
    directory.mkdir()
    samples = []
    for number in range(2):
        identity = str(uuid5(NAMESPACE_URL, f"mdf-offline-large-detail:{number}"))
        folder = directory / identity
        folder.mkdir()
        steps = [
            dict(market=0, instrument=f"{i:06}", state="COMPLETE") for i in range(5582)
        ]
        # One old journal has base+overrides; the other has only real shards.
        # Both must support full legacy reads and the new bounded protocol.
        if number == 0:
            (folder / "base.json").write_text(json.dumps(steps))
        else:
            for step in steps:
                (folder / f"0-{step['instrument']}.json").write_text(json.dumps(step))
        record = dict(
            request_id=identity,
            origin="manual" if number == 0 else "periodic",
            state="COMPLETE",
            created_at=f"2026-11-16T0{number}:00:00+00:00",
            finished_at=f"2026-11-16T0{number}:10:00+00:00",
            command=dict(action="update", start="2026-11-02", end="2026-11-16"),
            provider_requests=5582,
            completed_step_count=5582,
            step_refs=[f"0-{step['instrument']}" for step in steps],
        )
        (directory / f"{identity}.json").write_text(json.dumps(record))
        samples.append(dict(id=identity, step_count=len(steps), sample="005581"))
    (root / "fixture-only.json").write_text(json.dumps(samples))


if __name__ == "__main__":
    operation = sys.argv[2] if len(sys.argv) > 2 else "large"
    {"large": prepare, "surface": prepare_corrupt, "surface-repair": repair_corrupt}[
        operation
    ](Path(sys.argv[1]))
