"""Default holdings reuse an exact published head; historical cutoffs bypass it."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Event

from market_data_foundation.storage.tdx_p0 import PgStore


class Store(PgStore):
    def __init__(self):
        self.head = (1, datetime(2026, 1, 1, tzinfo=UTC))
        self.snapshot = "roster-1"
        self.computations = 0
        self.started = Event()
        self.release = Event()
        self.release.set()
        super().__init__(self.connection)

    @contextmanager
    def connection(self):
        yield self

    def execute(self, *_):
        return self

    def fetchone(self):
        return self.head

    def universe(self, **_):
        return dict(snapshot_id=self.snapshot)

    def _summary(self, as_of, *, snapshot=None):
        self.computations += 1
        self.started.set()
        assert self.release.wait(5)
        return dict(as_of=as_of, markets=[dict(rows=self.head[0])])


def test_summary_retains_cutoff_and_refreshes_for_publication_or_roster():
    store = Store()
    first = store.summary()
    assert store.summary() == first
    assert store.computations == 1
    store.head = (2, store.head[1])  # another writer can share a publication clock
    second = store.summary()
    assert second["markets"][0]["rows"] == 2
    assert store.computations == 2
    store.snapshot = "roster-2"
    store.summary()
    assert store.computations == 3
    historic = datetime(2020, 1, 1, tzinfo=UTC)
    assert store.summary(as_of=historic)["as_of"] == historic
    assert store.computations == 4
    assert store.summary()["as_of"] != historic
    assert store.computations == 4


def test_parallel_summary_requests_share_one_computation():
    store = Store()
    store.release.clear()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(store.summary)
        assert store.started.wait(5)
        second = executor.submit(store.summary)
        store.release.set()
        assert first.result() == second.result()
    assert store.computations == 1
