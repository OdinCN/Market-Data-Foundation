"""Offline PG-protocol double and in-memory canonical engine; not real PG proof."""

from contextlib import contextmanager
from unittest.mock import patch

from test_b3_05_cpi_pg_jobs import IDENTITY, Server
from test_cpi_local_job import Rig

from market_data_foundation.historical.cpi_local_job import CpiLocalJob
from market_data_foundation.historical.cpi_pg_journal import PostgresJobJournal
from market_data_foundation.storage.postgres_runtime_config import PgConfig
from market_data_foundation.web.cpi_dev_host import BINDING, create_preview
from tests.r5_fixture import ReadOnlySnapshot


def fixture(tmp_path):
    server = Server()
    profile = PgConfig(
        "192.168.10.4",
        5432,
        "market_data_foundation_dev",
        "market_data_foundation_dev",
        "require",
    )
    journal = PostgresJobJournal(
        server.connect, profile, IDENTITY, BINDING, enabled=True
    )
    rig = Rig(tmp_path / "rig")
    rig.journal = journal
    old = rig.engine
    rig.engine = CpiLocalJob(
        journal,
        old.clock,
        old.kernel,
        old.store,
        old.protection,
        old.readback,
        old.commit,
        target_binding=BINDING,
    )

    @contextmanager
    def read_scope():
        yield ReadOnlySnapshot(rig.ledger)

    with patch("market_data_foundation.web.cpi_dev_host.clock", old.clock):
        app = create_preview(journal, read_scope, enabled=True)
    return server, rig, app
