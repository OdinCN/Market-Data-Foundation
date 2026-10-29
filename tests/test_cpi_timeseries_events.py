"""CPI ratio, availability and evidence versions; all boundaries are offline."""

import runpy
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from market_data_foundation.historical.cpi_timeseries import (
    CpiTimeSeriesError,
    CpiTimeSeriesService,
    _ratio,
)
from market_data_foundation.historical.cpi_timeseries_source import (
    SourceBatch,
    SourcePoint,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    SCHEMA,
    SERIES_IDS,
    CpiStorageError,
    InMemoryTimeSeriesStore,
    PostgresTimeSeriesStore,
    _rates_view_sql,
    digest,
    raw_locator,
)

SID = SERIES_IDS[0]
AT = datetime(2026, 10, 28, tzinfo=UTC)
NOTICE = "https://www.bls.gov/errata/2026/cpi-withdrawal.htm"
RESTORE = "https://www.bls.gov/errata/2026/cpi-restoration.htm"


def batch(values, observed=AT):
    return SourceBatch(
        tuple(
            SourcePoint(
                SID,
                m,
                None if v is None else Decimal(v),
                {"pct_changes": {"1": "9.9", "12": "8.8"}},
                (),
            )
            for m, v in values.items()
        ),
        repr(values).encode(),
        {},
        observed,
        1,
    )


class Source:
    def __init__(self, batches=()):
        self.batches, self.calls = batches, []

    def fetch(self, series, start, end):
        self.calls.append((series, start, end))
        return self.batches


def query(service, start="2026-09", end="2026-09", **kwargs):
    return service.query_range(
        start, end, series_ids=(SID,), allow_upstream=False, **kwargs
    )


def test_ratios_exact_month_endpoints_legacy_units_and_no_intermediate_requirement():
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    store.save_batches(
        (
            batch(
                {
                    "2025-09": "80",
                    "2026-03": "125",
                    "2026-06": "100",
                    "2026-08": "128",
                    "2026-09": "130",
                }
            ),
        )
    )
    result = query(CpiTimeSeriesService(store, Source()))
    p = result["points"][0]
    assert [p[f"change_{k}m_ratio"] for k in (1, 3, 6, 12)] == [
        "0.0156",
        "0.3000",
        "0.0400",
        "0.6250",
    ]
    assert (p["mom_pct"], p["yoy_pct"]) == ("1.56", "62.50")
    assert p["provider_calculations"]["pct_changes"] == {"1": "9.9", "12": "8.8"}
    assert result["units"]["change_3m_ratio"] == "ratio"
    assert result["units"]["mom_pct"] == result["units"]["bls_pct_1m"] == "percent"


@pytest.mark.parametrize(
    "numerator,denominator,expected",
    [
        ("100.005", "100", "0.0001"),
        ("99.995", "100", "-0.0001"),
        ("100.0049999999999999999999999999999999999", "100", "0.0000"),
        ("2", "3", "-0.3333"),
        ("3", "2", "0.5000"),
    ],
)
def test_ratio_single_half_up_rounding(numerator, denominator, expected):
    assert _ratio(numerator, denominator) == expected


def test_available_cutoff_uses_same_confirmed_vintage_for_both_endpoints():
    now = [AT]
    store = InMemoryTimeSeriesStore(clock=lambda: now[0])
    store.save_batches((batch({"2026-08": "100", "2026-09": "110"}),))
    now[0] += timedelta(hours=2)
    store.save_batches(
        (batch({"2026-08": "105", "2026-09": "126"}, AT + timedelta(minutes=1)),)
    )
    service = CpiTimeSeriesService(store, Source())
    earlier = query(service, vintage_mode="available", as_of=AT + timedelta(hours=1))
    assert earlier["points"][0]["change_1m_ratio"] == "0.1000"
    latest = query(service, vintage_mode="available", as_of=now[0])
    assert latest["points"][0]["change_1m_ratio"] == "0.2000"
    assert (
        latest["points"][0]["calculation_inputs"]["change_1m_ratio"][
            "denominator_available_at"
        ]
        == now[0].isoformat()
    )
    assert "confirmation" in latest["as_of_semantics"]


def test_late_import_cannot_enter_old_available_history_legacy_observed_is_unchanged():
    store = InMemoryTimeSeriesStore(clock=lambda: AT + timedelta(days=90))
    store.save_batches((batch({"2026-09": "110"}),))
    source = Source()
    service = CpiTimeSeriesService(store, source)
    strict = query(service, as_of=AT, vintage_mode="available")
    assert not strict["points"] and strict["missing"][0]["reason"] == (
        "availability_evidence_insufficient"
    )
    assert not query(service, as_of=AT)["points"]
    assert (
        query(service, as_of=AT, vintage_mode="observed")["points"][0]["index_value"]
        == "110"
    )
    assert query(service, as_of=AT + timedelta(days=90), vintage_mode="available")[
        "complete"
    ]
    assert not source.calls


def test_unknown_legacy_availability_is_not_backfilled_by_unchanged_update():
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    old = batch({"2026-09": "110"})
    store.save_batches((old,))
    store._history[(SID, "2026-09")][0]["available_at"] = None
    source = Source((replace(old, observed_at=AT + timedelta(days=1)),))
    service = CpiTimeSeriesService(store, source)
    service.update_range("2026-09", "2026-09", series_ids=(SID,), include_buffer=False)
    strict = query(service, vintage_mode="available", as_of=AT + timedelta(days=2))
    assert not strict["points"]
    assert strict["missing"][0]["reason"] == "availability_evidence_insufficient"
    assert store._history[(SID, "2026-09")][0]["available_at"] is None
    assert not query(service)["complete"]
    assert query(service, query_mode="history", vintage_mode="observed")["complete"]


def test_strict_unknown_denominator_never_falls_back_to_current_api():
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    store.save_batches((batch({"2026-08": "100", "2026-09": "110"}),))
    store._history[(SID, "2026-08")][0]["available_at"] = None
    source = Source()
    result = CpiTimeSeriesService(store, source).query_range(
        "2026-09", "2026-09", series_ids=(SID,), vintage_mode="available", as_of=AT
    )
    assert result["points"][0]["change_1m_ratio"] is None
    assert (
        result["calculation_missing"][0]["reason"]
        == "availability_evidence_insufficient"
    )
    assert not source.calls


def test_official_first_release_is_explicitly_unsupported():
    source = Source()
    with pytest.raises(CpiTimeSeriesError, match="official_vintage_unsupported"):
        query(
            CpiTimeSeriesService(InMemoryTimeSeriesStore(), source),
            vintage_mode="official",
        )
    assert not source.calls


def test_withdrawal_history_stale_api_and_explicit_restoration():
    now = [AT]
    store = InMemoryTimeSeriesStore(clock=lambda: now[0])
    original = batch({"2026-08": "100", "2026-09": "110"})
    store.save_batches((original,))
    source = Source((replace(original, observed_at=AT + timedelta(days=2)),))
    service = CpiTimeSeriesService(store, source)
    now[0] += timedelta(days=1)
    assert (
        service.withdraw_range(
            "2026-08", "2026-08", series_ids=(SID,), evidence_url=NOTICE
        )["saved_revisions"]
        == 1
    )
    assert (
        query(service, vintage_mode="available", as_of=AT)["points"][0][
            "change_1m_ratio"
        ]
        == "0.1000"
    )
    assert query(service)["points"][0]["change_1m_ratio"] is None
    withdrawn = query(service, "2026-08", "2026-08")
    assert not withdrawn["source_unavailable"]
    assert withdrawn["withdrawn"][0]["evidence_url"] == NOTICE
    assert withdrawn["missing"][0]["reason"] == "withdrawn"
    assert (
        service.withdraw_range(
            "2026-08", "2026-08", series_ids=(SID,), evidence_url=NOTICE
        )["saved_points"]
        == 0
    )
    now[0] += timedelta(days=2)
    updated = service.update_range(
        "2026-08", "2026-09", series_ids=(SID,), include_buffer=False
    )
    assert not updated["refresh_complete"]
    assert updated["saved_points"] == 0 and len(updated["withdrawn"]) == 1
    assert updated["withdrawn"][0]["origin"] == "local"
    restored = service.update_range(
        "2026-08",
        "2026-09",
        series_ids=(SID,),
        include_buffer=False,
        restoration_evidence_url=RESTORE,
    )
    assert restored["complete"] and restored["saved_revisions"] == 1
    p = restored["points"][0]
    assert p["revision"] == 3 and p["evidence_url"] == RESTORE
    assert not query(
        service,
        "2026-08",
        "2026-08",
        vintage_mode="available",
        as_of=AT + timedelta(days=2),
    )["complete"]


def test_withdraw_unknown_key_prevents_future_api_resurrection_and_omission_is_inert():
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    source = Source((batch({"2026-09": "100"}, AT + timedelta(days=1)),))
    service = CpiTimeSeriesService(store, source)
    service.withdraw_range("2026-09", "2026-09", series_ids=(SID,), evidence_url=NOTICE)
    assert (
        service.update_range(
            "2026-09", "2026-09", series_ids=(SID,), include_buffer=False
        )["saved_points"]
        == 0
    )
    store.save_batches((batch({"2026-08": "99"}),))
    source.batches = (batch({}, AT + timedelta(days=2)),)
    result = service.update_range(
        "2026-08", "2026-08", series_ids=(SID,), include_buffer=False
    )
    assert result["points"][0]["index_value"] == "99"
    assert not result["refresh_coverage_complete"] and not result["withdrawn"]


def test_source_dash_is_not_withdrawal_and_can_recover_without_evidence():
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    store.save_batches((batch({"2026-09": None}),))
    service = CpiTimeSeriesService(
        store, Source((batch({"2026-09": "100"}, AT + timedelta(days=1)),))
    )
    before = query(service)
    assert before["source_unavailable"] and not before["withdrawn"]
    assert service.update_range(
        "2026-09", "2026-09", series_ids=(SID,), include_buffer=False
    )["complete"]


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        "",
        "https://example.com/notice",
        "http://www.bls.gov/errata/",
        "https://www.bls.gov/errata/?token=x",
    ],
)
def test_invalid_evidence_rejected_before_storage_or_provider(evidence):
    source = Source()
    store = InMemoryTimeSeriesStore()
    service = CpiTimeSeriesService(store, source)
    with pytest.raises(CpiTimeSeriesError, match="evidence_url_invalid"):
        service.withdraw_range("2026-09", "2026-09", evidence_url=evidence)
    assert not source.calls and not store._history


def test_include_buffer_false_uses_fixed_history_start_and_filters_response():
    source = Source((batch({"1998-01": "90", "1999-01": "100", "2026-09": "110"}),))
    store = InMemoryTimeSeriesStore()
    service = CpiTimeSeriesService(store, source)
    for _ in range(2):
        service.update_range(
            "1999-01", "2026-09", series_ids=(SID,), include_buffer=False
        )
    assert all(call[1:] == ("1999-01", "2026-09") for call in source.calls)
    assert (SID, "1998-01") not in store._history


class Raw:
    def put_verified(self, raw):
        return raw_locator(digest(raw))


class Connection:
    autocommit = True

    def __init__(self, *, fail_commit=0, old=None):
        self.statements = []
        self.commits = 0
        self.fail_commit, self.old = fail_commit, old
        self.info = SimpleNamespace(
            transaction_status=psycopg.pq.TransactionStatus.IDLE
        )

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if "SET available_at" in sql:
            assert self.commits == 1, "timestamp must follow successful data commit"
        return SimpleNamespace(
            fetchone=lambda: self.old if "SELECT revision" in sql else None,
            fetchall=lambda: [],
        )

    @contextmanager
    def transaction(self):
        yield
        self.commits += 1
        if self.commits == self.fail_commit:
            raise psycopg.OperationalError("unexposed detail")


@pytest.mark.parametrize("fail_commit", [0, 1, 2])
def test_pg_confirmation_happens_only_after_successful_data_commit(fail_commit):
    conn = Connection(fail_commit=fail_commit)

    def clock():
        assert conn.commits == 1
        return AT + timedelta(days=90)

    store = PostgresTimeSeriesStore(conn, Raw(), clock=clock)
    if fail_commit:
        with pytest.raises(
            CpiStorageError,
            match=(
                "storage_outcome_unknown"
                if fail_commit == 1
                else "confirmation_outcome_unknown"
            ),
        ):
            store.save_batches((batch({"2026-09": "100"}),))
    else:
        assert store.save_batches((batch({"2026-09": "100"}),))["saved_points"] == 1
    updates = [p for sql, p in conn.statements if "SET available_at" in sql]
    assert len(updates) == (0 if fail_commit == 1 else 1)
    if updates:
        assert updates[0][0] == AT + timedelta(days=90)
    insert = next(
        sql for sql, _ in conn.statements if f"INSERT INTO {SCHEMA}.revisions" in sql
    )
    assert "available_at" not in insert


def test_pg_withdrawn_value_is_not_restored_by_normal_source():
    old = (2, AT, None, None, None, None, None, None, [], "withdrawn")
    conn = Connection(old=old)
    result = PostgresTimeSeriesStore(conn, Raw()).save_batches(
        (batch({"2026-09": "100"}, AT + timedelta(days=1)),)
    )
    assert result["saved_points"] == 0
    assert not any(
        "INSERT INTO" in sql and ".revisions" in sql for sql, _ in conn.statements
    )


def test_pg_available_cutoff_is_applied_before_latest_revision_selection():
    conn = Connection()
    PostgresTimeSeriesStore(conn, None).read_range(
        (SID,), "2026-08", "2026-09", as_of=AT, vintage_mode="available"
    )
    sql, params = conn.statements[-1]
    assert "DISTINCT ON(r.series_id,r.month)" in sql
    assert "r.available_at IS NOT NULL" in sql and "r.available_at <= %s" in sql
    assert "r.observed_at <=" not in sql
    assert "unconfirmed.observed_at <= %s" in sql
    assert params == ([SID], date(2026, 8, 1), date(2026, 9, 1), AT, AT)


def test_rates_view_joins_exact_dates_uses_numeric_ratios_and_excludes_withdrawn():
    sql = _rates_view_sql()
    assert "to_char(c.month,'YYYY-MM') AS month_label" in sql
    for lag in (1, 3, 6, 12):
        assert f"interval '{lag} months'" in sql
        assert f"p{lag}.series_id=c.series_id" in sql
        assert f"p{lag}.version_status='active'" in sql
        assert f"round(c.index_value / nullif(p{lag}.index_value,0) - 1,4)" in sql
    assert "WHERE c.version_status='active'" in sql


def test_event_migration_idempotent_preserves_views_acl_and_unknown_availability():
    migrate = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts/cpi_timeseries_event_migrate.py")
    )["migrate_event_schema"]

    class MigrationConnection:
        info = SimpleNamespace(transaction_status=psycopg.pq.TransactionStatus.INTRANS)

        def __init__(self):
            self.statements, self.constraints = [], set()

        def execute(self, sql, params=()):
            self.statements.append(sql)
            if sql.startswith("SELECT 1"):
                return SimpleNamespace(
                    fetchone=lambda: (1,) if params[1] in self.constraints else None
                )
            if "ADD CONSTRAINT" in sql:
                self.constraints.add(sql.split("ADD CONSTRAINT ")[1].split()[0])
            return SimpleNamespace(fetchone=lambda: None)

    conn = MigrationConnection()
    assert (
        migrate(conn)
        == migrate(conn)
        == {"available_at_backfilled": False, "fact_tables": 2}
    )
    sql = "\n".join(conn.statements)
    assert sql.count("ADD CONSTRAINT cpi_") == 2
    assert sql.count("ADD COLUMN IF NOT EXISTS") == 6
    assert "CREATE OR REPLACE VIEW" in sql
    assert not any(
        word in sql
        for word in (
            "DROP TABLE",
            "DROP VIEW",
            "DROP COLUMN",
            "UPDATE ",
            "GRANT ",
            "REVOKE ",
            "CREATE TABLE",
        )
    )
    conn.info.transaction_status = psycopg.pq.TransactionStatus.IDLE
    with pytest.raises(CpiStorageError, match="transaction_required"):
        migrate(conn)


def test_unconfirmed_newer_version_never_resurrects_prior_active_value():
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    store.save_batches((batch({"2026-09": "100"}),))
    store._clock = lambda: AT + timedelta(days=1)
    service = CpiTimeSeriesService(store, Source())
    service.withdraw_range("2026-09", "2026-09", series_ids=(SID,), evidence_url=NOTICE)
    store._history[(SID, "2026-09")][-1]["available_at"] = None
    strict = query(service, vintage_mode="available", as_of=AT + timedelta(days=2))
    assert not strict["points"]
    assert strict["missing"][0]["reason"] == "availability_evidence_insufficient"
    assert query(service, as_of=AT)["complete"]


def test_nested_savepoint_cannot_be_used_as_commit_confirmation():
    class NoRaw:
        def put_verified(self, _):
            pytest.fail("must reject caller transaction before raw writes")

    conn = Connection()
    conn.info.transaction_status = psycopg.pq.TransactionStatus.INTRANS
    with pytest.raises(CpiStorageError, match="top_level_transaction_required"):
        PostgresTimeSeriesStore(conn, NoRaw()).save_batches(
            (batch({"2026-09": "100"}),)
        )
    assert not conn.statements


def test_duplicate_withdrawal_neither_adds_batch_nor_claims_provider_attempt():
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    service = CpiTimeSeriesService(store, Source())
    service.withdraw_range("2026-09", "2026-09", series_ids=(SID,), evidence_url=NOTICE)
    assert next(iter(store._batches.values())).attempts == 0
    before = store.status()
    store._clock = lambda: AT + timedelta(days=1)
    service.withdraw_range("2026-09", "2026-09", series_ids=(SID,), evidence_url=NOTICE)
    assert store.status() == before


def test_future_unknown_does_not_contaminate_past_available_cutoff_or_ratios():
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    store.save_batches((batch({"2026-08": "100", "2026-09": "110"}),))
    future = AT + timedelta(days=30)
    store._clock = lambda: future
    store.save_batches((batch({"2026-08": "120", "2026-09": "150"}, future),))
    for history in store._history.values():
        history[-1]["available_at"] = None
    service = CpiTimeSeriesService(store, Source())
    earlier = query(service, vintage_mode="available", as_of=AT + timedelta(days=15))
    assert earlier["complete"]
    assert earlier["points"][0]["change_1m_ratio"] == "0.1000"
    assert earlier["points"][0]["revision"] == 1
    for cutoff in (future, future + timedelta(days=1), None):
        uncertain = query(service, vintage_mode="available", as_of=cutoff)
        assert not uncertain["complete"]
        assert uncertain["missing"][0]["reason"] == "availability_evidence_insufficient"


def test_explicit_confirmation_only_latest_unknown_at_now_without_new_versions():
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    store.save_batches((batch({"2026-08": "100", "2026-09": "110", "2026-07": None}),))
    store.save_batches((batch({"2026-08": "105"}, AT + timedelta(days=1)),))
    for history in store._history.values():
        for row in history:
            row["available_at"] = None
    now = AT + timedelta(days=90)
    store._clock = lambda: now
    source = Source()
    service = CpiTimeSeriesService(store, source)
    before = store.status()
    confirmed = service.confirm_current_availability(
        "2026-08", "2026-09", series_ids=(SID,)
    )
    assert confirmed == {"confirmed_points": 2, "confirmed_at": now.isoformat()}
    assert store.status() == before and not source.calls
    assert store._history[SID, "2026-08"][0]["available_at"] is None
    assert store._history[SID, "2026-07"][0]["available_at"] is None
    assert not query(
        service, vintage_mode="available", as_of=now - timedelta(seconds=1)
    )["complete"]
    assert query(service, vintage_mode="available", as_of=now)["complete"]
    assert service.confirm_current_availability(
        "2026-08", "2026-09", series_ids=(SID,)
    ) == {"confirmed_points": 0, "confirmed_at": None}
    assert store._history[SID, "2026-09"][0]["observed_at"] == AT.isoformat()


def test_explicit_confirmation_includes_withdrawal_and_source_missing():
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    store.save_batches((batch({"2026-08": None}),))
    service = CpiTimeSeriesService(store, Source())
    service.withdraw_range("2026-09", "2026-09", series_ids=(SID,), evidence_url=NOTICE)
    for history in store._history.values():
        history[-1]["available_at"] = None
    now = AT + timedelta(days=1)
    store._clock = lambda: now
    assert (
        service.confirm_current_availability("2026-08", "2026-09", series_ids=(SID,))[
            "confirmed_points"
        ]
        == 2
    )
    result = query(service, "2026-08", "2026-09", vintage_mode="available", as_of=now)
    assert len(result["withdrawn"]) == len(result["source_unavailable"]) == 1
    assert result["coverage_complete"] and not result["complete"]


def test_pg_explicit_confirmation_locks_committed_latest_and_changes_no_history():
    class ConfirmConnection:
        autocommit = True
        info = SimpleNamespace(transaction_status=psycopg.pq.TransactionStatus.IDLE)

        def __init__(self):
            self.statements, self.pending, self.commits = [], [], 0
            self.known = False
            self.fail_commit = False

        @contextmanager
        def transaction(self):
            self.info.transaction_status = psycopg.pq.TransactionStatus.INTRANS
            try:
                yield
                if self.fail_commit:
                    raise psycopg.OperationalError("unexposed")
                self.known = bool(self.pending) or self.known
                self.commits += 1
            finally:
                self.pending = []
                self.info.transaction_status = psycopg.pq.TransactionStatus.IDLE

        def execute(self, sql, params=()):
            self.statements.append((sql, params))
            if "SET available_at" in sql:
                self.pending.append(params)
            rows = [] if self.known else [(SID, date(2026, 9, 1), 2)]
            return SimpleNamespace(fetchone=lambda: (True,), fetchall=lambda: rows)

    conn = ConfirmConnection()

    def clock():
        assert "FROM " + SCHEMA + ".latest" in conn.statements[-1][0]
        assert any("SHARE ROW EXCLUSIVE" in sql for sql, _ in conn.statements)
        return AT

    store = PostgresTimeSeriesStore(conn, None, clock=clock)
    assert store.confirm_current_availability((SID,), "2026-09", "2026-09") == {
        "confirmed_points": 1,
        "confirmed_at": AT.isoformat(),
    }
    updates = [(sql, params) for sql, params in conn.statements if "UPDATE " in sql]
    assert len(updates) == 1
    assert updates[0][1] == (AT, SID, date(2026, 9, 1), 2)
    assert "revision=%s" in updates[0][0] and "available_at IS NULL" in updates[0][0]
    assert not any("INSERT " in sql for sql, _ in conn.statements)
    assert (
        store.confirm_current_availability((SID,), "2026-09", "2026-09")[
            "confirmed_points"
        ]
        == 0
    )
    before = len(conn.statements)
    conn.info.transaction_status = psycopg.pq.TransactionStatus.INTRANS
    with pytest.raises(CpiStorageError, match="top_level_transaction_required"):
        store.confirm_current_availability((SID,), "2026-09", "2026-09")
    assert len(conn.statements) == before
    conn.info.transaction_status = psycopg.pq.TransactionStatus.IDLE
    conn.known = False
    conn.fail_commit = True
    with pytest.raises(CpiStorageError, match="confirmation_outcome_unknown"):
        store.confirm_current_availability((SID,), "2026-09", "2026-09")
    assert not conn.known


@pytest.mark.parametrize(
    "expected", [[], [{"series_id": SID, "month": "2026-09", "index_value": "200"}]]
)
def test_scheduler_expectation_mismatch_never_restores_withdrawn(expected):
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    source = Source((batch({"2026-09": "100"}, AT + timedelta(days=1)),))
    service = CpiTimeSeriesService(store, source)
    service.withdraw_range("2026-09", "2026-09", series_ids=(SID,), evidence_url=NOTICE)
    result = service.update_range(
        "2026-09",
        "2026-09",
        series_ids=(SID,),
        include_buffer=False,
        restoration_evidence_url=RESTORE,
        restoration_expected_points=expected,
    )
    assert result["saved_points"] == 0
    assert not result["refresh_complete"] and result["withdrawn"]
    assert len(store._history[SID, "2026-09"]) == 1


def test_restoration_expectations_match_each_key_with_decimal_equality():
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    source = Source(
        (batch({"2026-08": "100.00", "2026-09": "100"}, AT + timedelta(days=1)),)
    )
    service = CpiTimeSeriesService(store, source)
    service.withdraw_range("2026-08", "2026-09", series_ids=(SID,), evidence_url=NOTICE)
    result = service.update_range(
        "2026-08",
        "2026-09",
        series_ids=(SID,),
        include_buffer=False,
        restoration_evidence_url=RESTORE,
        restoration_expected_points=[
            {"series_id": SID, "month": "2026-08", "index_value": "100.0"}
        ],
    )
    assert result["saved_revisions"] == 1
    assert result["points"][0]["month"] == "2026-08"
    assert result["withdrawn"][0]["month"] == "2026-09"
    assert not result["refresh_complete"]


@pytest.mark.parametrize("value", [None, True, "NaN", "Infinity", "0", "-1", {}, []])
def test_invalid_restoration_expectations_rejected_before_source(value):
    source = Source()
    service = CpiTimeSeriesService(InMemoryTimeSeriesStore(), source)
    with pytest.raises(CpiTimeSeriesError, match="restoration_expected_points_invalid"):
        service.update_range(
            "2026-09",
            "2026-09",
            series_ids=(SID,),
            restoration_evidence_url=RESTORE,
            restoration_expected_points=[
                {"series_id": SID, "month": "2026-09", "index_value": value}
            ],
        )
    assert not source.calls


def test_restoration_expectations_require_url_unique_keys_and_scope():
    source = Source()
    service = CpiTimeSeriesService(InMemoryTimeSeriesStore(), source)
    point = {"series_id": SID, "month": "2026-09", "index_value": "100"}
    for expected, url in [
        ([point], None),
        ([point, point], RESTORE),
        ([dict(point, month="2026-08")], RESTORE),
        ([dict(point, previous_value="99")], RESTORE),
        ({"bad": "shape"}, RESTORE),
    ]:
        with pytest.raises(
            CpiTimeSeriesError, match="restoration_expected_points_invalid"
        ):
            service.update_range(
                "2026-09",
                "2026-09",
                series_ids=(SID,),
                restoration_evidence_url=url,
                restoration_expected_points=expected,
            )
    assert not source.calls


@pytest.mark.parametrize("expected_value,saved", [("99", 0), ("100.00", 1)])
def test_pg_restoration_requires_matching_expected_value(expected_value, saved):
    old = (2, AT, None, None, None, None, None, None, [], "withdrawn")
    conn = Connection(old=old)
    result = PostgresTimeSeriesStore(conn, Raw()).save_batches(
        (batch({"2026-09": "100"}, AT + timedelta(days=1)),),
        restoration_evidence_url=RESTORE,
        restoration_expected_points=[
            {"series_id": SID, "month": "2026-09", "index_value": expected_value}
        ],
    )
    assert result["saved_points"] == saved
    assert (
        sum("INSERT INTO" in sql and ".revisions" in sql for sql, _ in conn.statements)
        == saved
    )
