"""Opt-in private DEV PG checks in an isolated namespace; never a CI dependency."""

import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from market_data_foundation.storage.tdx_daily_pg import (
    PRIMARY_SOURCE,
    SQL_DIR,
    Bar,
    Batch,
    Counts,
)
from market_data_foundation.storage.tdx_p0 import PgStore, connect

pytestmark = pytest.mark.postgres_integration


def isolated_sql(query, schema):
    # Isolate advisory locks as well as physical tables from active DEV writers.
    base = 10000000 + 3 * int(schema.rsplit("_", 1)[-1][:6], 16)
    for i in range(3):
        query = query.replace(f"({721603 + i},", f"({base + i},")
    return query.replace("mdf_tdx_daily", schema)


class Cursor:
    def __init__(self, cursor, schema):
        self.cursor = cursor
        self.schema = schema

    def __enter__(self):
        self.cursor.__enter__()
        return self

    def __exit__(self, *args):
        return self.cursor.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def execute(self, query, *args, **kwargs):
        self.cursor.execute(isolated_sql(query, self.schema), *args, **kwargs)
        return self

    def copy(self, query, *args, **kwargs):
        return self.cursor.copy(isolated_sql(query, self.schema), *args, **kwargs)


class Connection:
    def __init__(self, connection, schema):
        self.connection = connection
        self.schema = schema

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def cursor(self, *args, **kwargs):
        return Cursor(self.connection.cursor(*args, **kwargs), self.schema)

    def execute(self, query, *args, **kwargs):
        return self.connection.execute(
            isolated_sql(query, self.schema), *args, **kwargs
        )


def test_dev_b1_bj_cold_asof_corrections_confirmations_and_late_block():
    if os.environ.get("MDF_TDX_P0_DEV_PG") != "1":
        pytest.skip("explicit private DEV opt-in required")
    schema = "mdf_tdx_p0_test_" + uuid4().hex[:12]
    with connect() as conn:
        conn.execute(
            (SQL_DIR / "001_current.sql").read_text().replace("mdf_tdx_daily", schema)
        )
        conn.execute(
            (SQL_DIR / "006_p0.sql").read_text().replace("mdf_tdx_daily", schema)
        )
        conn.execute(
            (SQL_DIR / "007_p0_publication.sql")
            .read_text()
            .replace("mdf_tdx_daily", schema)
        )
    try:
        clock = datetime(2020, 5, 20, 8, tzinfo=UTC)

        def batch(identity, second, close):
            observed = clock + timedelta(seconds=second)
            proof = dict(
                request_index=0,
                received_at=observed.isoformat(),
                line_sha256="a" * 64,
                wire_sha256="b" * 64,
                request_sha256="c" * 64,
                projection_sha256="d" * 64,
            )
            return Batch(
                identity,
                PRIMARY_SOURCE,
                observed,
                observed,
                (Bar(2, "920002", date(2020, 5, 19), (1000, 2000, 900, close, 0, 0)),),
                {"2:920002": [proof]},
                True,
                True,
                date(2020, 5, 19),
                date(2020, 5, 19),
            )

        store = PgStore(lambda: Connection(connect(), schema))
        first = batch("controlled-first", 0, 1100)
        # The all-market writer can progress while an unrelated instrument is locked.
        with Connection(connect(), schema) as holding, holding.transaction():
            holding.execute("SELECT pg_advisory_xact_lock_shared(721603, 1)")
            holding.execute("SELECT pg_advisory_xact_lock(721605, 999999)")
            assert store.write(first) == Counts(new=1)
        first_visible = store.query(start=date(2020, 5, 19), end=date(2020, 5, 19))[
            "rows"
        ][0]["available_at"]
        assert store.write(first) == Counts(new=1, replay=True)
        changed = batch("controlled-correction", 2, 1200)
        assert store.write(changed) == Counts(changed=1)
        changed_visible = store.query(start=date(2020, 5, 19), end=date(2020, 5, 19))[
            "rows"
        ][0]["available_at"]
        confirmed = batch("controlled-confirm", 4, 1200)
        assert store.write(confirmed) == Counts(unchanged=1)
        assert store.write(replace(first, batch_id="controlled-late")) == Counts(
            blocked=1
        )

        def query(cutoff, **kwargs):
            return store.query(
                start=date(2020, 5, 19), end=date(2020, 5, 19), as_of=cutoff, **kwargs
            )

        assert query(first_visible - timedelta(microseconds=1))["total"] == 0
        history = query(first_visible, market=2, instrument="920002")
        assert history["rows"][0]["close"] == 1100
        current = query(changed_visible)
        assert current["rows"][0]["close"] == 1200
        assert current["rows"][0]["available_at"] == changed_visible
        assert query(datetime.now(UTC))["rows"][0]["available_at"] == changed_visible
        assert (
            current["provider_requests"] == 0
            and current["return_qualification"] == "NOT_QUALIFIED"
        )
        # A held pending identity and an unknown fact commit must not expose
        # its old admission clock. Replay publishes only after fact confirmation.
        from market_data_foundation.storage.tdx_daily_pg import write_batch

        delayed = replace(
            batch("controlled-delayed", 8, 1300),
            bars=(Bar(2, "920003", date(2020, 5, 19), (1000, 2000, 900, 1300, 0, 0)),),
            evidence={"2:920003": first.evidence["2:920002"]},
        )
        with Connection(connect(), schema) as conn:
            assert write_batch(conn, delayed, independent_instruments=True) == Counts(
                new=1
            )
        held_cutoff = datetime.now(UTC)
        assert query(held_cutoff, market=2, instrument="920003")["total"] == 0
        assert store.summary(as_of=held_cutoff)["markets"][0]["rows"] == 1
        assert store.write(delayed) == Counts(new=1, replay=True)
        published = query(datetime.now(UTC), market=2, instrument="920003")["rows"][0][
            "available_at"
        ]
        assert published > held_cutoff
        assert (
            query(published - timedelta(microseconds=1), market=2, instrument="920003")[
                "total"
            ]
            == 0
        )
        assert query(published, market=2, instrument="920003")["total"] == 1
        assert store.write(delayed).replay
        assert (
            query(datetime.now(UTC), market=2, instrument="920003")["rows"][0][
                "available_at"
            ]
            == published
        )

        # An incomplete refresh cannot erase a usable roster. The lifecycle
        # filter and summary use the same dated membership and visibility.
        stamp = datetime.now(UTC)
        snapshot = dict(
            snapshot_id="complete",
            observed_at=stamp,
            available_at=stamp,
            details=dict(
                markets=[dict(market=m, state="COMPLETE") for m in (0, 1, 2)],
                roster_kind="exchange_lifecycle",
            ),
            members=[
                dict(
                    market=2,
                    instrument="920002",
                    name="fixture",
                    evidence=dict(listing_date="2020-05-01", delist_date="2020-05-20"),
                )
            ],
        )
        store.save_universe(snapshot)
        store.save_universe(
            dict(
                snapshot,
                snapshot_id="failed",
                available_at=datetime.now(UTC),
                details=dict(
                    markets=[dict(market=m, state="FAILED") for m in (0, 1, 2)]
                ),
                members=[],
            )
        )
        assert store.universe()["snapshot_id"] == "complete"
        assert store.universe(snapshot_id="failed")["members"] == []
        assert store.query(start=date(2020, 5, 19), end=date(2020, 5, 19))["total"] == 1
        assert store.summary()["markets"][0]["rows"] == 1
    finally:
        with connect() as conn:
            conn.execute("DROP SCHEMA " + schema + " CASCADE")
