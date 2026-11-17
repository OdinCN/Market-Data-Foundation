"""Build two large offline journals; no provider, database or business POST."""

import json
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5


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
    prepare(Path(sys.argv[1]))
