"""Offline oracle + psycopg call contract; none of these execute PostgreSQL SQL."""

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from psycopg.pq import TransactionStatus

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.integration import tdx_daily_ingest as ingest
from market_data_foundation.storage import tdx_daily_pg as pg

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)
DAY = date(2026, 10, 22)
PROOF = [
    {
        "request_index": 0,
        "received_at": NOW.isoformat(),
        "line_sha256": "a" * 64,
        "wire_sha256": "b" * 64,
        "request_sha256": "c" * 64,
        "projection_sha256": "d" * 64,
    }
]
POLICY = pg.SourcePolicy()


def bar(day=DAY, values=(1000, 1100, 900, 1050, 23, 24000)):
    return pg.Bar(1, "600000", day, values)


def batch(bars=None, **changes):
    bars = bars or (bar(),)
    evidence = {
        f"{market}:{instrument}": [{**PROOF[0], "request_index": index}]
        for index, (market, instrument) in enumerate(sorted({b.key[:2] for b in bars}))
    }
    observed = changes.get("observed_at", NOW)
    for items in evidence.values():
        items[0]["received_at"] = observed.isoformat()
    return replace(
        pg.Batch(
            "test-1",
            pg.PRIMARY_SOURCE,
            observed,
            changes.get("admitted_at", observed),
            bars,
            evidence,
            True,
            True,
            min(b.trading_day for b in bars),
            max(b.trading_day for b in bars),
        ),
        **changes,
    )


def advance(original, batch_id, seconds=1, **changes):
    evidence = {
        scope: [
            {
                **p,
                "received_at": (
                    datetime.fromisoformat(p["received_at"])
                    + timedelta(seconds=seconds)
                ).isoformat(),
            }
            for p in proofs
        ]
        for scope, proofs in original.evidence.items()
    }
    return replace(
        original,
        batch_id=batch_id,
        observed_at=original.observed_at + timedelta(seconds=seconds),
        admitted_at=original.admitted_at + timedelta(seconds=seconds),
        evidence=evidence,
        **changes,
    )


def test_32_by_32_1024_overlap_and_fresh_repeat_oracle():
    bars = tuple(
        pg.Bar(
            market,
            f"{i:06}",
            DAY - timedelta(days=d),
            (1000 + i, 1100 + i, 900 + i, 1050 + i, 23, 24000),
        )
        for market in (0, 1)
        for i in range(16)
        for d in range(32)
    )
    first = batch(bars)
    store = pg.OfflineCurrent()
    assert store.apply(first) == pg.Counts(new=1024)
    old_values = json.dumps(store.current, sort_keys=True)
    overlap = batch(bars[:192], batch_id="overlap")
    assert store.apply(overlap) == pg.Counts(unchanged=192)
    repeated = advance(first, "fresh-full")
    assert store.apply(repeated) == pg.Counts(unchanged=1024)
    assert json.dumps(store.current, sort_keys=True) == old_values
    assert len(store.current) == 1024 and store.corrections == []
    assert pg.OfflineCurrent.rebuild(store.snapshot()).snapshot() == store.snapshot()


def test_ten_corrections_old_new_retry_and_late_replay():
    bars = tuple(bar(DAY - timedelta(days=d)) for d in range(32))
    first = batch(bars)
    store = pg.OfflineCurrent()
    store.apply(first)
    changed = tuple(
        replace(b, values=(*b.values[:3], b.values[3] + 1, *b.values[4:]))
        if i < 10
        else b
        for i, b in enumerate(bars)
    )
    corrected = advance(first, "corrected", bars=changed)
    assert store.apply(corrected) == pg.Counts(unchanged=22, changed=10)
    assert len(store.corrections) == 10
    for event in store.corrections:
        assert event["old"]["values"][3] == 1050
        assert event["new"]["values"][3] == 1051
        assert event["old"]["digest"] != event["new"]["digest"]
    snapshot = store.snapshot()
    assert store.apply(corrected).replay
    assert store.apply(first).replay  # late original retry cannot revert correction
    assert store.snapshot() == snapshot
    reimport = advance(first, "stale-new-id", seconds=0)
    assert store.apply(reimport) == pg.Counts(unchanged=22, blocked=10)


def test_ten_changes_in_1024_have_only_ten_old_new_events():
    bars = tuple(
        pg.Bar(
            1, f"{i:06}", DAY - timedelta(days=d), (1000, 1100, 900, 1050, 23, 24000)
        )
        for i in range(32)
        for d in range(32)
    )
    original = batch(bars)
    store = pg.OfflineCurrent()
    store.apply(original)
    changed = tuple(
        replace(b, values=(1000, 1100, 900, 1051, 23, 24000)) if i < 10 else b
        for i, b in enumerate(bars)
    )
    fix = advance(original, "correction-1024", bars=changed)
    assert store.apply(fix) == pg.Counts(unchanged=1014, changed=10)
    assert len(store.current) == 1024 and len(store.corrections) == 10
    before = store.snapshot()
    store.apply(fix)
    assert store.snapshot() == before


def test_lower_priority_and_unchanged_high_confirmation_protect_current():
    low = batch(source="controlled-secondary")
    store = pg.OfflineCurrent()
    store.apply(low)
    high = advance(low, "high-confirm", source=pg.PRIMARY_SOURCE)
    original_row = json.dumps(store.current, sort_keys=True)
    assert store.apply(high) == pg.Counts(unchanged=1)
    assert json.dumps(store.current, sort_keys=True) == original_row
    low_fix = advance(
        low,
        "late-low",
        seconds=20,
        bars=(replace(bar(), values=(1000, 1100, 900, 1051, 23, 24000)),),
    )
    assert store.apply(low_fix) == pg.Counts(blocked=1)
    assert store.corrections == []


def test_newer_equal_confirmation_blocks_stale_same_source_correction():
    first = batch()
    store = pg.OfflineCurrent()
    store.apply(first)
    store.apply(advance(first, "confirmation", 20))
    stale = advance(
        first,
        "stale",
        10,
        bars=(replace(bar(), values=(1000, 1100, 900, 1051, 23, 24000)),),
    )
    assert store.apply(stale) == pg.Counts(blocked=1)


def test_duplicate_equal_collapses_conflicting_rejects_atomically():
    store = pg.OfflineCurrent()
    assert store.apply(batch((bar(), bar()))) == pg.Counts(new=1)
    before = store.snapshot()
    conflict = replace(bar(), values=(1000, 1100, 900, 1051, 23, 24000))
    with pytest.raises(ContractError, match="conflicting_duplicate_key"):
        store.apply(batch((bar(), conflict), batch_id="conflict"))
    assert store.snapshot() == before


def test_changed_batch_identity_reuse_rejected():
    original = batch()
    store = pg.OfflineCurrent()
    store.apply(original)
    with pytest.raises(ContractError, match="batch_identity_reuse"):
        store.apply(
            replace(original, bars=(replace(bar(), values=(1, 2, 0, 1, 0, 0)),))
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"complete": False},
        {"quality_passed": False},
        {"evidence": {}},
        {"source": "uncontrolled"},
        {"observed_at": NOW.replace(tzinfo=None)},
        {"admitted_at": NOW - timedelta(seconds=1)},
        {"day_end": DAY - timedelta(days=1)},
        {"batch_id": "x; DROP TABLE y"},
    ],
)
def test_bad_batch_rejected_before_mutation(changes):
    store = pg.OfflineCurrent()
    before = store.snapshot()
    with pytest.raises(ContractError):
        store.apply(batch(**changes))
    assert store.snapshot() == before


@pytest.mark.parametrize(
    "values",
    [
        (True, 2, 0, 1, 0, 0),
        (1.0, 2, 0, 1, 0, 0),
        (1, 2, 0, None, 0, 0),
        (1, 2, 0, 1, -1, 0),
        (1, 2, 0, 3, 0, 0),
        (2**63, 2**63, 0, 1, 0, 0),
    ],
)
def test_native_validation(values):
    with pytest.raises(ContractError):
        bar(values=values)


def test_int_precision_and_factor_revisions_do_not_change_raw():
    large = 2**53 + 1
    native = bar(values=(large, large + 1, large - 1, large, 7, large))
    assert native.copy_row()[5] == large
    assert (
        native.value_digest
        != replace(
            native, values=(large + 1, large + 1, large - 1, large, 7, large)
        ).value_digest
    )
    store = pg.OfflineCurrent()
    store.apply(batch((native,)))
    before = store.snapshot()
    factor = pg.AdjustmentFactor(1, "600000", DAY, 1, 2, "test-factor", "r1")
    with pytest.raises(ContractError, match="trusted_factor_source_required"):
        factor.admit()
    # Synthetic named-source model exercise only; no actual trusted factor exists.
    factor.admit(trusted_sources=("test-factor",))
    replace(factor, numerator=2, revision="r2").admit(trusted_sources=("test-factor",))
    assert store.snapshot() == before


class RecordingConnection:
    """Call recorder, deliberately does not interpret SQL or emulate PG locking."""

    def __init__(self, *, duplicate=False, fail=None, previous=(), counts=None):
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self.calls = []
        self.rows = []
        self.duplicate, self.fail, self.previous = duplicate, fail, previous
        self.counts = counts or [("NEW", 1)]
        self.result = None
        self.commits = self.rollbacks = 0

    @contextmanager
    def transaction(self):
        try:
            yield
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1

    @contextmanager
    def cursor(self):
        yield self

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self.fail and self.fail in sql:
            raise RuntimeError("injected_offline_failure")
        if sql == pg.POLICY_CHECK:
            self.result = None
        elif "SELECT batch_digest" in sql:
            self.result = list(self.previous)
        elif sql == pg.RANGE_CHECK:
            self.result = None
        elif sql == pg.DUPLICATES:
            self.result = (1,) if self.duplicate else None
        elif "SELECT disposition,count" in sql:
            self.result = self.counts
        elif "WITH inserted AS" in sql:
            counts = dict(self.counts)
            self.result = (
                counts.get("NEW", 0),
                counts.get("CHANGED", 0),
                counts.get("CHANGED", 0),
            )

    @contextmanager
    def copy(self, sql):
        self.calls.append((sql, None))
        if self.fail == "COPY":
            raise RuntimeError("injected_offline_failure")
        yield self

    def write_row(self, row):
        self.rows.append(row)

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result


def test_psycopg_copy_set_operations_single_transaction_contract():
    conn = RecordingConnection()
    assert pg.write_batch(conn, batch()) == pg.Counts(new=1)
    assert conn.commits == 1 and conn.rollbacks == 0
    assert conn.rows == [(*bar().copy_row(), 0, NOW)]
    sql = [call[0] for call in conn.calls]
    assert sql.index("SELECT pg_advisory_xact_lock(721603, 1)") < next(
        i for i, s in enumerate(sql) if "SELECT batch_digest" in s
    )
    assert sql.index(pg.COPY) < sql.index(pg.DUPLICATES)
    assert sum("WITH inserted AS" in s for s in sql) == 1
    assert (
        sum("INSERT INTO mdf_tdx_daily.source_refresh_batches" in s for s in sql) == 1
    )


@pytest.mark.parametrize(
    "fail",
    [
        "COPY",
        "CREATE TEMP TABLE tdx_daily_delta",
        "WITH inserted AS",
        "INSERT INTO mdf_tdx_daily.source_refresh_batches",
    ],
)
def test_write_failures_exit_transaction_with_rollback(fail):
    conn = RecordingConnection(fail=fail)
    with pytest.raises(RuntimeError, match="injected_offline_failure"):
        pg.write_batch(conn, batch())
    assert conn.commits == 0 and conn.rollbacks == 1


def test_sql_duplicate_rejection_rolls_back_before_main_write():
    conn = RecordingConnection(duplicate=True)
    with pytest.raises(ContractError, match="conflicting_duplicate_key"):
        pg.write_batch(conn, batch((bar(), bar())))
    assert conn.rollbacks == 1 and not any(
        "WITH inserted AS" in s for s, _ in conn.calls
    )


def test_replay_short_circuits_copy_and_identity_drift_rejected():
    original = batch()
    conn = RecordingConnection(previous=[(original.fingerprint, 100)])
    assert pg.write_batch(conn, original).replay
    assert conn.commits == 1 and conn.rows == []
    bad = RecordingConnection(previous=[("0" * 64, 100)])
    with pytest.raises(ContractError, match="batch_identity_reuse"):
        pg.write_batch(bad, original)
    assert bad.rollbacks == 1


def test_open_transaction_rejected():
    conn = RecordingConnection()
    conn.info.transaction_status = TransactionStatus.INTRANS
    with pytest.raises(ContractError, match="idle_connection_required"):
        pg.write_batch(conn, batch())
    assert conn.calls == []


def test_sql_contract_and_identifiers_are_fixed():
    ddl = (pg.SQL_DIR / "001_current.sql").read_text()
    assert ddl.count("CREATE TABLE") == 3
    assert "PRIMARY KEY (market, instrument, period, trading_day, basis)" in ddl
    apply = pg.sql_file("004_apply.sql")
    assert "WHERE d.disposition='CHANGED'" in apply
    assert "FROM changed RETURNING 1" in apply
    assert "FROM pg_temp.tdx_daily_delta WHERE disposition='NEW'" in apply
    assert "last_seen" not in apply and "Last" not in apply
    classify = pg.sql_file("003_classify.sql")
    assert (
        "source_priority DESC" in classify
        and "scoped_refresh AS MATERIALIZED" in classify
    )
    assert "SELECT DISTINCT *" in classify
    assert "count(DISTINCT ROW" in pg.DUPLICATES
    with pytest.raises(ContractError, match="sql_allowlist"):
        pg.sql_file("../../other.sql")


def test_cold_read_is_bounded_and_parameterized():
    conn = RecordingConnection()
    pg.cold_read(conn, market=1, instrument="600000", day_start=DAY, day_end=DAY)
    sql, params = conn.calls[-1]
    assert params == (1, "600000", DAY, DAY)
    assert "ORDER BY trading_day LIMIT 3661" in sql
    with pytest.raises(ContractError):
        pg.cold_read(conn, market=1, instrument="x';--", day_start=DAY, day_end=DAY)


def test_no_default_live_mode_or_provider_import_in_cli():
    cli = Path("scripts/mdf_b1_tdx_pg_only_import.py").read_text()
    assert "required=True" in cli
    assert "connect(" not in cli and "write_batch" not in cli


def test_real_frozen_inputs_against_host_receipts_read_only():
    if not ingest.FROZEN_ROOT.exists() or not ingest.HOST_OUTPUTS.exists():
        pytest.skip(
            "Host-local retained B1 files unavailable; synthetic oracle still runs"
        )
    large, audit = ingest.frozen_batch("thirtytwo", known_at=NOW)
    small, small_audit = ingest.frozen_batch("six", known_at=NOW)
    assert (audit["rows"], small_audit["rows"]) == (1024, 192)
    large_values = {b.key: b.values for b in large.bars}
    assert len(large_values) == 1024
    assert all(large_values[b.key] == b.values for b in small.bars)
    store = pg.OfflineCurrent()
    assert store.apply(large) == pg.Counts(new=1024)
    before = json.dumps(store.current, sort_keys=True)
    assert store.apply(small) == pg.Counts(unchanged=192)
    assert store.apply(advance(large, "offline-fresh-reimport")) == pg.Counts(
        unchanged=1024
    )
    assert json.dumps(store.current, sort_keys=True) == before
    assert len(store.corrections) == 0
    assert pg.OfflineCurrent.rebuild(store.snapshot()).snapshot() == store.snapshot()


def test_host_sha_tamper_is_rejected(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text("{}")
    with pytest.raises(ContractError, match="host_digest_mismatch"):
        ingest.read_checked(path, "0" * 64)


def test_native_last_page_context_cannot_change_fact_or_duplicate_identity():
    native = {
        "Time": "2026-10-22T15:00:00+08:00",
        "Open": 1000,
        "High": 1100,
        "Low": 900,
        "Close": 1050,
        "Volume": 23,
        "Amount": 24000,
        "Last": 0,
    }
    first = ingest.native_bar(1, "600000", native)
    other = ingest.native_bar(1, "600000", {**native, "Last": 1055})
    assert first.value_digest == other.value_digest
    assert pg.OfflineCurrent().apply(batch((first, other))) == pg.Counts(new=1)


def test_same_key_values_different_market_remain_distinct():
    first = bar()
    other = replace(first, market=0)
    assert pg.OfflineCurrent().apply(batch((first, other))) == pg.Counts(new=2)
    with pytest.raises(ContractError, match="native_daily_only"):
        replace(first, basis="qfq")


def test_unverified_factor_and_duplicate_source_ranks_rejected():
    with pytest.raises(ContractError, match="fixed_source_registry_required"):
        pg.SourcePolicy(((pg.PRIMARY_SOURCE, 100), ("other", 100)))
    with pytest.raises(ContractError, match="positive_rational_factor_required"):
        pg.AdjustmentFactor(1, "600000", DAY, 1, 0, "test-factor", "r1").admit(
            trusted_sources=("test-factor",)
        )


def test_unchanged_writer_call_contract_reports_zero_actual_writes():
    conn = RecordingConnection(counts=[("UNCHANGED", 1024)])
    assert pg.write_batch(conn, batch()) == pg.Counts(unchanged=1024)
    assert conn.commits == 1


def test_missing_or_mismatched_evidence_and_nonempty_bounds_reject_before_copy():
    for changes in (
        {"evidence": {"1:600000": []}},
        {"bars": ()},
        {"bars": (None,)},
        {"evidence": {"1:600000": [{**PROOF[0], "wire_sha256": "bad"}]}},
    ):
        conn = RecordingConnection()
        with pytest.raises(ContractError):
            pg.write_batch(conn, replace(batch(), **changes))
        assert conn.calls == []


@pytest.mark.parametrize("stage", ["thirtytwo", "six"])
def test_r2_frozen_request_clocks_are_original_received_at(stage):
    if not ingest.FROZEN_ROOT.exists() or not ingest.HOST_OUTPUTS.exists():
        pytest.skip("Host-local frozen evidence unavailable")
    frozen, _ = ingest.frozen_batch(stage, known_at=NOW)
    lines = (
        (ingest.FROZEN_ROOT / f"b1-go-r7-{stage}-live/source-stdout.jsonl")
        .read_bytes()
        .splitlines()
    )
    store = pg.OfflineCurrent()
    store.apply(frozen)
    for b in frozen.bars:
        proof = frozen.evidence[f"{b.market}:{b.instrument}"][0]
        original = json.loads(lines[proof["request_index"] + 1])["received_at"]
        assert proof["received_at"] == original
        assert frozen.received_at(b) == datetime.fromisoformat(original)
    conn = RecordingConnection(counts=[("NEW", len(frozen.bars))])
    pg.write_batch(conn, frozen)
    for b, copied in zip(frozen.bars, conn.rows, strict=True):
        assert copied[-2:] == (frozen.request_index(b), frozen.received_at(b))
        original = frozen.evidence[f"{b.market}:{b.instrument}"][0]["received_at"]
        assert datetime.fromisoformat(
            store.current[json.dumps(b.key)]["observed"]
        ) == datetime.fromisoformat(original)


def test_r2_caller_cannot_reassign_fixed_source_rank():
    with pytest.raises(ContractError):
        pg.SourcePolicy(((pg.PRIMARY_SOURCE, 100), ("controlled-secondary", 99)))


def test_r2_high_source_authority_uses_its_own_clock():
    low = batch(source="controlled-secondary")
    high = advance(low, "high-same", seconds=-10, source=pg.PRIMARY_SOURCE)
    fix = advance(
        high,
        "high-fix",
        seconds=5,
        bars=(bar(values=(1000, 1100, 900, 1051, 23, 24000)),),
    )
    store = pg.OfflineCurrent()
    store.apply(low)
    assert store.apply(high) == pg.Counts(unchanged=1)
    assert store.apply(fix) == pg.Counts(changed=1)
    assert "GREATEST(c.source_observed_at" not in pg.sql_file("003_classify.sql")


def test_r2_history_is_read_as_sets_with_scope_access_paths():
    sql = pg.sql_file("003_classify.sql")
    assert "LEFT JOIN LATERAL" not in sql
    assert "s.trading_day=ANY" not in sql
    ddl = (pg.SQL_DIR / "001_current.sql").read_text()
    assert "USING gin (confirmed_days)" in ddl
    assert (
        "ON mdf_tdx_daily.source_refresh_batches\n"
        "    (market, instrument, period, basis)" in ddl
    )


def test_r2_interleaved_instruments_never_borrow_batch_completion_clock():
    x = bar()
    y = replace(x, instrument="600001")
    corrected_x = replace(x, values=(1000, 1100, 900, 1051, 23, 24000))
    # A received X at t10 and Y at t30; B received the X correction at t20.
    a = batch((x, y), batch_id="a", observed_at=NOW + timedelta(seconds=30))
    evidence = {scope: [dict(items[0])] for scope, items in a.evidence.items()}
    evidence["1:600000"][0]["received_at"] = (NOW + timedelta(seconds=10)).isoformat()
    a = replace(a, evidence=evidence)
    b = batch((corrected_x,), batch_id="b", observed_at=NOW + timedelta(seconds=20))
    store = pg.OfflineCurrent()
    assert store.apply(b) == pg.Counts(new=1)
    assert store.apply(a) == pg.Counts(new=1, blocked=1)
    assert store.current[json.dumps(x.key)]["values"] == list(corrected_x.values)
    assert store.current[json.dumps(y.key)]["observed"] == a.received_at(y).isoformat()
    assert store.corrections == []
    assert store.apply(a).replay
    conn = RecordingConnection(counts=[("NEW", 1), ("BLOCKED", 1)])
    pg.write_batch(conn, a)
    assert [row[-2:] for row in conn.rows] == [
        (a.request_index(x), a.received_at(x)),
        (a.request_index(y), a.received_at(y)),
    ]
    assert conn.rows[0][-1] != a.observed_at


def test_r2_sql_receives_request_clocks_and_never_updates_on_equal_values():
    stage = pg.sql_file("002_stage.sql")
    classify = pg.sql_file("003_classify.sql")
    apply = pg.sql_file("004_apply.sql")
    refresh = pg.sql_file("005_refresh.sql")
    assert "request_index smallint NOT NULL" in stage
    assert "received_at timestamptz NOT NULL" in stage
    assert "s.received_at > a.last_confirmed_at" in classify
    assert "UNION ALL" in classify and "FROM scoped_current" in classify
    assert "max(last_confirmed_at)" in classify
    assert "basis,source,source_priority" in classify
    assert 'source COLLATE "C"' in classify
    assert "source_observed_at=d.received_at" in apply
    assert "%(priority)s,received_at,%(admitted)s" in apply
    assert "received_at,%(admitted)s,'vendor_correction_or_unknown'" in apply
    assert "%(priority)s,received_at,%(admitted)s" in refresh
    assert "GROUP BY market,instrument,period,basis,received_at" in refresh
    assert "FILTER (WHERE disposition <> 'BLOCKED')" in refresh
    assert "%(observed)s" not in classify + apply + refresh
    assert apply.count("UPDATE ") == 1 and "WHERE d.disposition='CHANGED'" in apply
    # Materialized scoped receipt history is expanded once, then grouped by key.
    assert classify.count("mdf_tdx_daily.source_refresh_batches") == 1
    assert "r.confirmed_days && s.trading_days" in classify
    assert "CROSS JOIN unnest(r.confirmed_days)" in classify
    assert "SELECT DISTINCT ON (market,instrument,period,trading_day,basis)" in classify
    assert "LIMIT 1" not in classify and "=ANY" not in classify


@pytest.mark.parametrize("target", ["current", "confirmations", "receipts"])
@pytest.mark.parametrize("corrupt", ["rank", "source"])
def test_r2_offline_persisted_authority_pollution_rejects_atomically(target, corrupt):
    original = batch(source="controlled-secondary")
    store = pg.OfflineCurrent()
    store.apply(original)
    if target == "current":
        row = next(iter(store.current.values()))
        row["priority" if corrupt == "rank" else "source"] = (
            99 if corrupt == "rank" else "unknown"
        )
    elif target == "confirmations":
        row = next(iter(store.confirmations.values()))
        row[0 if corrupt == "rank" else 1] = 99 if corrupt == "rank" else "unknown"
    elif corrupt == "rank":
        next(iter(store.receipts.values()))[1] = 99
    else:
        value = store.receipts.pop(next(iter(store.receipts)))
        store.receipts["unknown/test-1"] = value
    before = store.snapshot()
    # Validation precedes replay as well as new corrections.
    with pytest.raises(ContractError):
        store.apply(original)
    assert store.snapshot() == before


def test_r2_fixed_rank_ignores_constructed_descriptor_and_rejects_policy_argument():
    forged = object.__new__(pg.SourcePolicy)
    object.__setattr__(forged, "priorities", (("controlled-secondary", 99),))
    assert forged.rank("controlled-secondary") == 10
    low = batch(source="controlled-secondary")
    for policy in (forged, POLICY, {"controlled-secondary": 99}):
        conn = RecordingConnection()
        with pytest.raises(TypeError):
            pg.write_batch(conn, low, policy=policy)
        assert conn.calls == []
        with pytest.raises(TypeError):
            pg.OfflineCurrent().apply(low, policy=policy)
    store = pg.OfflineCurrent()
    store.apply(batch())
    assert store.apply(
        advance(low, "low-fix", bars=(bar(values=(1000, 1100, 900, 1051, 23, 24000)),))
    ) == pg.Counts(blocked=1)
    with pytest.raises(ContractError, match="unbound_source"):
        pg.write_batch(RecordingConnection(), batch(source="unknown"))


def test_r2_writer_checks_persisted_rank_before_replay_and_rolls_back():
    class PollutedConnection(RecordingConnection):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if sql == pg.POLICY_CHECK:
                self.result = (1,)

    original = batch()
    conn = PollutedConnection(previous=[(original.fingerprint, 100)])
    with pytest.raises(ContractError, match="persisted_source_rank_mismatch"):
        pg.write_batch(conn, original)
    assert conn.rollbacks == 1 and conn.commits == 0 and conn.rows == []
    sql = [call[0] for call in conn.calls]
    assert sql.index("SELECT pg_advisory_xact_lock(721603, 1)") < sql.index(
        pg.POLICY_CHECK
    )
    assert not any("SELECT batch_digest" in s for s in sql)
    params = conn.calls[-1][1]
    assert params["registry"].obj == dict(pg.SOURCE_REGISTRY)
    assert params["scopes"].obj == [{"market": 1, "instrument": "600000"}]
    assert "IS DISTINCT FROM source_priority" in pg.POLICY_CHECK
    assert (
        "bars_daily_current" in pg.POLICY_CHECK
        and "source_refresh_batches" in pg.POLICY_CHECK
    )


def test_r2_equal_rank_different_source_requires_same_value_confirmation():
    low = batch(source="controlled-secondary")
    other = advance(
        low,
        "peer-change",
        source="controlled-peer",
        bars=(bar(values=(1000, 1100, 900, 1051, 23, 24000)),),
    )
    store = pg.OfflineCurrent()
    store.apply(low)
    assert store.apply(other) == pg.Counts(blocked=1)
    assert store.apply(
        advance(low, "peer-confirm", 2, source="controlled-peer")
    ) == pg.Counts(unchanged=1)
    assert store.apply(advance(other, "peer-fix", 2)) == pg.Counts(changed=1)
    assert store.apply(advance(low, "old-source-new-clock", 5)) == pg.Counts(blocked=1)
    assert pg.OfflineCurrent.rebuild(store.snapshot()).snapshot() == store.snapshot()
    # Equal timestamp confirmations use deterministic source name ordering.
    for first, second in (
        ("controlled-secondary", "controlled-peer"),
        ("controlled-peer", "controlled-secondary"),
    ):
        equal = pg.OfflineCurrent()
        equal.apply(batch(source=first))
        equal.apply(batch(source=second))
        assert next(iter(equal.confirmations.values()))[1] == "controlled-peer"


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "naive",
        "future",
        "multiple",
        "wrong_scope",
        "reused_index",
        "rank_payload",
        "future_day",
    ],
)
def test_r2_ambiguous_or_wrong_clock_evidence_fails_before_copy(kind):
    x = bar()
    y = replace(x, instrument="600001")
    original = batch((x, y))
    evidence = {scope: [dict(items[0])] for scope, items in original.evidence.items()}
    proof = evidence["1:600000"][0]
    if kind == "missing":
        del proof["received_at"]
    elif kind == "naive":
        proof["received_at"] = NOW.replace(tzinfo=None).isoformat()
    elif kind == "future":
        proof["received_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif kind == "future_day":
        proof["received_at"] = (NOW - timedelta(days=2)).isoformat()
    elif kind == "multiple":
        evidence["1:600000"].append({**proof, "request_index": 2})
    elif kind == "wrong_scope":
        evidence["0:600000"] = evidence.pop("1:600000")
    elif kind == "reused_index":
        evidence["1:600001"][0]["request_index"] = proof["request_index"]
    else:
        proof["rank"] = 99
    original = replace(original, evidence=evidence)
    conn = RecordingConnection()
    store = pg.OfflineCurrent()
    before = store.snapshot()
    with pytest.raises(ContractError):
        pg.write_batch(conn, original)
    with pytest.raises(ContractError):
        store.apply(original)
    assert conn.calls == [] and store.snapshot() == before


@pytest.mark.parametrize("kind", ["truncated", "wrong_clock", "wrong_projection"])
def test_r2_frozen_truncation_or_evidence_drift_is_rejected(monkeypatch, kind):
    if not ingest.FROZEN_ROOT.exists() or not ingest.HOST_OUTPUTS.exists():
        pytest.skip("Host-local frozen evidence unavailable")
    checked = ingest.read_checked

    def altered(path, expected, limit=4 * 1024 * 1024):
        raw = checked(path, expected, limit)
        if path.name != "source-stdout.jsonl":
            return raw
        # Fault injection in memory only, AFTER real SHA verification.
        lines = raw.splitlines()
        if kind == "truncated":
            return b"\n".join(lines[:-1])
        row = json.loads(lines[1])
        if kind == "wrong_clock":
            row["received_at"] = "2026-10-23T11:00:06Z"
        else:
            row["source_projection"]["List"][0]["Close"] += 1
        lines[1] = json.dumps(row).encode()
        return b"\n".join(lines)

    monkeypatch.setattr(ingest, "read_checked", altered)
    with pytest.raises(ContractError):
        ingest.frozen_batch("thirtytwo", known_at=NOW)


def test_r2_submicrosecond_clock_collision_fails_closed_for_changes():
    original = batch()
    evidence = {
        "1:600000": [
            {
                **original.evidence["1:600000"][0],
                "received_at": "2026-10-23T12:00:00.000000100Z",
            }
        ]
    }
    first = replace(original, evidence=evidence)
    later = replace(
        first,
        batch_id="nano-later",
        bars=(bar(values=(1000, 1100, 900, 1051, 23, 24000)),),
        evidence={
            "1:600000": [
                {
                    **evidence["1:600000"][0],
                    "received_at": "2026-10-23T12:00:00.000000900Z",
                }
            ]
        },
    )
    store = pg.OfflineCurrent()
    store.apply(first)
    assert store.apply(later) == pg.Counts(blocked=1)
    assert first.fingerprint != later.fingerprint


def test_r2_constructed_batch_cannot_supply_its_own_rank():
    class ForgedBatch(pg.Batch):
        def validate(self):
            return 99

    original = batch(source="controlled-secondary")
    forged = ForgedBatch(**original.__dict__)
    for candidate in (forged, SimpleNamespace(validate=lambda: 99)):
        conn = RecordingConnection()
        with pytest.raises(ContractError, match="batch_required"):
            pg.write_batch(conn, candidate)
        with pytest.raises(ContractError, match="batch_required"):
            pg.OfflineCurrent().apply(candidate)
        assert conn.calls == []


def test_native_bar_historical_shanghai_summer_time():
    native = dict(Open=1, High=2, Low=1, Close=1, Volume=1, Amount=1)
    for offset in ("+09:00", "+08:00"):
        row = {**native, "Time": "1991-07-01T15:00:00" + offset}
        assert ingest.native_bar(0, "000001", row).trading_day == date(1991, 7, 1)
    for clock in (
        "1992-07-01T15:00:00+09:00",
        "1991-07-01T15:00:00",
        "1991-07-01T14:00:00+09:00",
    ):
        with pytest.raises(ContractError, match="sdk_date_projection_invalid"):
            ingest.native_bar(0, "000001", {**native, "Time": clock})
