"""systemd ExecCondition for the single authorized absolute DEV window."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def permitted(path, *, now=None):
    try:
        value = json.loads(Path(path).read_text())
        start = datetime.fromisoformat(value["started_at"])
        end = datetime.fromisoformat(value["expires_at"])
        at = now or datetime.now(UTC)
        return (
            start.utcoffset() is not None
            and end.utcoffset() is not None
            and end - start == timedelta(days=45)
            and start <= at < end
        )
    except OSError, ValueError, KeyError, TypeError:
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    raise SystemExit(0 if permitted(parser.parse_args().state) else 1)
