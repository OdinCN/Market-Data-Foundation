"""Offline transaction/SQL models only; no PostgreSQL concurrency proof."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from psycopg import Connection
from psycopg.pq import TransactionStatus

from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
)
from market_data_foundation.canonical.operator_asof import (
    OperatorQueryBudgetError,
    inspect_as_of,
)
from market_data_foundation.canonical.operator_bindings import (
    inspect_binding_candidates,
)
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from tests.test_p6_consumer_read import _seed_bound_cpi, _seed_daily
from tests.test_p6_operator_asof import AT, _store
from tests.test_p6_operator_producer_admission import SQLDouble

IDLE = TransactionStatus.IDLE
INTRANS = TransactionStatus.INTRANS
INERROR = TransactionStatus.INERROR


class SnapshotDouble(SQLDouble):
    """Model explicit BEGIN and a frozen view, not actual server isolation."""

    def __init__(self, ledger, autocommit=False):
        super().__init__(ledger)
        self.live = ledger
        self.info = SimpleNamespace(transaction_status=IDLE)
        self.closed = False
        self._num_transactions = 0
        self._tpc = None
        self._pipeline = None
        self._autocommit = autocommit
        self.events = []
        self.settings = ("repeatable read", "on")
        self.fault = None
        self.after_read = None
        self.view = 0
        self.read_views = []

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value):
        self.events.append(("autocommit", value))
        if self.fault == "enable" and value:
            raise RuntimeError("private enable failure")
        if self.fault == "restore" and not value:
            raise RuntimeError("private restore failure")
        self._autocommit = value

    def execute(self, query, parameters=None):
        if isinstance(query, str):
            self.events.append(query)
            if query == "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY":
                assert self.autocommit is True
                assert self.info.transaction_status is IDLE
                if self.fault == "begin_before":
                    raise RuntimeError("private begin failure")
                self.info.transaction_status = INTRANS
                self.view += 1
                frozen = SQLDouble(SimpleNamespace(_records=dict(self.live._records)))
                self.records, self.publications = frozen.records, frozen.publications
                if self.fault == "begin_after":
                    raise RuntimeError("private begin acknowledgement failure")
                if self.fault == "begin_unknown":
                    self.info.transaction_status = TransactionStatus.UNKNOWN
                return SimpleNamespace()
            assert query == (
                "SELECT current_setting('transaction_isolation'), "
                "current_setting('transaction_read_only')"
            )
            if self.fault == "verify":
                self.info.transaction_status = INERROR
                raise RuntimeError("private verification failure")
            return SimpleNamespace(fetchone=lambda: self.settings)
        assert self.info.transaction_status is INTRANS
        assert self.autocommit is True
        if self.fault == "read":
            self.info.transaction_status = INERROR
            raise RuntimeError("private read failure")
        self.read_views.append(self.view)
        result = super().execute(query, parameters)
        if self.after_read:
            action, self.after_read = self.after_read, None
            action()
        return result

    def rollback(self):
        self.events.append("rollback")
        if self.fault == "rollback":
            raise RuntimeError("private rollback failure")
        if self.fault != "rollback_stuck":
            self.info.transaction_status = IDLE

    def close(self):
        self.events.append("close")
        self.closed = True
        self.info.transaction_status = TransactionStatus.UNKNOWN
        if self.fault == "close":
            raise RuntimeError("private close failure")


class PoolDouble:
    def __init__(self, close_returns=False):
        self.close_returns = close_returns
        self.returned = []

    def putconn(self, connection):
        self.returned.append(connection)


class DriverCloseDouble(SnapshotDouble):
    """Run the installed synchronous driver close method without a transport."""

    def __init__(self, ledger, autocommit=False):
        super().__init__(ledger, autocommit)
        self._closed = False
        self.pgconn = SimpleNamespace(finish=self._finish)

    def _finish(self):
        self.closed = True
        self.info.transaction_status = TransactionStatus.UNKNOWN

    def close(self):
        self.events.append("close")
        Connection.close(self)


def _source(connection):
    return PostgresLedger(connection, "mdf_p3_canonical_kernel_r1_operator_snapshot")


def _classified(call):
    with pytest.raises(ReadContractError) as caught:
        call()
    assert caught.value.classification is ReadFailure.STORAGE_UNAVAILABLE
    assert caught.value.args == ("storage_unavailable",)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    return caught.value


@pytest.mark.parametrize("autocommit", [False, True])
@pytest.mark.parametrize("definition", [BLS_CPI_HEADLINES, RAW_DAILY_BARS])
def test_unchanged_both_queries_fresh_budgets_and_owned_rollback(
    autocommit, definition
):
    ledger = (
        _seed_bound_cpi()[0] if definition == BLS_CPI_HEADLINES else _seed_daily()[0]
    )
    connection = SnapshotDouble(ledger, autocommit)
    before = ledger.snapshot()
    expected_asof = inspect_as_of(ledger, definition.key, as_of=AT)
    expected_bindings = inspect_binding_candidates(ledger, definition.key)
    with _source(connection).operator_snapshot() as snapshot:
        first = snapshot.operator_reads()
        second = snapshot.operator_reads()
        assert first is not second and first.admission is not second.admission
        for _ in range(2):
            assert inspect_as_of(snapshot, definition.key, as_of=AT) == expected_asof
            assert (
                inspect_binding_candidates(snapshot, definition.key)
                == expected_bindings
            )
    assert set(connection.read_views) == {1}
    assert connection.info.transaction_status is IDLE
    assert connection.autocommit is autocommit
    assert connection.closed is False
    assert connection.events == [
        ("autocommit", True),
        "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY",
        "SELECT current_setting('transaction_isolation'), "
        "current_setting('transaction_read_only')",
        "rollback",
        ("autocommit", autocommit),
    ]
    assert ledger.snapshot() == before
    assert not hasattr(snapshot, "put") and not hasattr(snapshot, "get_content")
    _classified(snapshot.operator_reads)
    _classified(lambda: first.get(definition.key))
    assert len(connection.events) == 5


def test_modeled_writer_between_statements_and_queries_does_not_change_view():
    ledger, plan, _ = _seed_bound_cpi()
    connection = SnapshotDouble(ledger)
    expected_asof = inspect_as_of(ledger, BLS_CPI_HEADLINES.key, as_of=AT)
    expected_bindings = inspect_binding_candidates(ledger, BLS_CPI_HEADLINES.key)

    def writer():
        _store(
            ledger, replace(plan.binding_candidate, limitations=("new synthetic view",))
        )

    connection.after_read = writer
    with _source(connection).operator_snapshot() as snapshot:
        assert inspect_as_of(snapshot, BLS_CPI_HEADLINES.key, as_of=AT) == expected_asof
        assert (
            inspect_binding_candidates(snapshot, BLS_CPI_HEADLINES.key)
            == expected_bindings
        )
        assert inspect_as_of(snapshot, BLS_CPI_HEADLINES.key, as_of=AT) == expected_asof
    assert set(connection.read_views) == {1}
    with _source(connection).operator_snapshot() as snapshot:
        assert (
            inspect_binding_candidates(snapshot, BLS_CPI_HEADLINES.key)
            != expected_bindings
        )
    assert set(connection.read_views) == {1, 2}


@pytest.mark.parametrize(
    "status", [s for s in TransactionStatus if s is not IDLE] + [None, 0]
)
def test_ambient_or_unknown_status_is_untouched(status):
    ledger, _, _ = _seed_bound_cpi()
    connection = SnapshotDouble(ledger)
    connection.info.transaction_status = status
    before = vars(connection).copy()
    _classified(lambda: _enter(connection))
    assert vars(connection) == before
    assert connection.events == []


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("_num_transactions", 1),
        ("_num_transactions", None),
        ("_tpc", object()),
        ("_pipeline", object()),
        ("closed", True),
        ("_autocommit", None),
    ],
)
def test_idle_driver_managed_or_unknown_connection_is_untouched(attribute, value):
    ledger, _, _ = _seed_bound_cpi()
    connection = SnapshotDouble(ledger)
    setattr(connection, attribute, value)
    _classified(lambda: _enter(connection))
    assert getattr(connection, attribute) is value
    assert connection.events == []


def test_missing_status_or_bookkeeping_is_untouched():
    for connection in (SimpleNamespace(), SimpleNamespace(closed=False, info=None)):
        before = vars(connection).copy()
        _classified(lambda: _enter(connection))
        assert vars(connection) == before
    ledger, _, _ = _seed_bound_cpi()
    connection = SnapshotDouble(ledger)
    del connection._num_transactions
    _classified(lambda: _enter(connection))
    assert connection.events == []


def _enter(connection):
    with _source(connection).operator_snapshot():
        pytest.fail("unconfirmed snapshot must never be exposed")


@pytest.mark.parametrize("autocommit", [False, True])
@pytest.mark.parametrize("error_type", [AttributeError, RuntimeError])
def test_autocommit_first_read_failure_rejects_without_side_effects(
    monkeypatch, autocommit, error_type
):
    ledger, _, _ = _seed_bound_cpi()
    connection = DriverCloseDouble(ledger, autocommit)
    pool = connection._pool = PoolDouble()
    reads = []
    original = SnapshotDouble.autocommit

    def read_once_fails(self):
        reads.append(self)
        if len(reads) == 1:
            raise error_type("synthetic private autocommit read failure")
        return original.fget(self)

    monkeypatch.setattr(
        SnapshotDouble, "autocommit", property(read_once_fails, original.fset)
    )
    before = ledger.snapshot()
    _classified(lambda: _enter(connection))
    assert reads == [connection]
    assert connection.events == []
    assert connection.calls == []
    assert pool.returned == []
    assert connection._pool is pool
    assert connection.closed is False and connection._closed is False
    assert connection.info.transaction_status is IDLE
    assert connection._autocommit is autocommit
    assert ledger.snapshot() == before


def test_installed_driver_close_returns_uncertain_connection_to_pool():
    # Counterexample runs real psycopg 3.3.6 close(), not a mirrored close double.
    ledger, _, _ = _seed_bound_cpi()
    connection = DriverCloseDouble(ledger, True)
    pool = connection._pool = PoolDouble(True)
    connection.info.transaction_status = INTRANS
    connection.close()
    assert pool.returned == [connection]
    assert connection.closed is False and connection._closed is False
    assert connection.info.transaction_status is INTRANS


@pytest.mark.parametrize("autocommit", [False, True])
@pytest.mark.parametrize("close_returns", [True, None, 1, 0, "false"])
def test_pool_close_returns_or_unknown_policy_rejects_without_side_effects(
    autocommit, close_returns
):
    ledger, _, _ = _seed_bound_cpi()
    connection = DriverCloseDouble(ledger, autocommit)
    pool = connection._pool = PoolDouble(close_returns)
    connection.fault = "rollback_stuck"
    before = ledger.snapshot()
    _classified(lambda: _enter(connection))
    assert connection.events == []
    assert connection.calls == []
    assert pool.returned == []
    assert connection._pool is pool and pool.close_returns is close_returns
    assert connection.closed is False and connection._closed is False
    assert connection.info.transaction_status is IDLE
    assert connection._autocommit is autocommit
    assert ledger.snapshot() == before


@pytest.mark.parametrize("fault", ["missing", "policy_read", "pool_read"])
def test_unknown_pool_policy_rejects_without_side_effects(monkeypatch, fault):
    ledger, _, _ = _seed_bound_cpi()
    connection = DriverCloseDouble(ledger)
    pool = connection._pool = PoolDouble()

    def cannot_read(self):
        raise RuntimeError("synthetic private pool policy read failure")

    if fault == "missing":
        del pool.close_returns
    elif fault == "policy_read":
        monkeypatch.setattr(
            PoolDouble, "close_returns", property(cannot_read), raising=False
        )
    else:
        monkeypatch.setattr(
            DriverCloseDouble, "_pool", property(cannot_read), raising=False
        )
    _classified(lambda: _enter(connection))
    assert connection.events == []
    assert connection.calls == []
    assert pool.returned == []
    assert vars(connection)["_pool"] is pool
    assert connection.closed is False and connection._closed is False
    assert connection.info.transaction_status is IDLE
    assert connection._autocommit is False


@pytest.mark.parametrize("autocommit", [False, True])
@pytest.mark.parametrize("fault", [None, "rollback", "rollback_stuck"])
def test_pool_with_close_returns_false_preserves_transaction_and_discard(
    autocommit, fault
):
    ledger, _, _ = _seed_bound_cpi()
    connection = DriverCloseDouble(ledger, autocommit)
    pool = connection._pool = PoolDouble(False)
    connection.fault = fault

    def call():
        with _source(connection).operator_snapshot() as snapshot:
            assert inspect_as_of(snapshot, BLS_CPI_HEADLINES.key, as_of=AT) == (
                inspect_as_of(ledger, BLS_CPI_HEADLINES.key, as_of=AT)
            )
            assert inspect_binding_candidates(snapshot, BLS_CPI_HEADLINES.key) == (
                inspect_binding_candidates(ledger, BLS_CPI_HEADLINES.key)
            )

    if fault is None:
        call()
        assert connection.info.transaction_status is IDLE
        assert connection.autocommit is autocommit
        assert connection.closed is False
        assert connection.events[-2:] == ["rollback", ("autocommit", autocommit)]
    else:
        _classified(call)
        assert connection.closed is True and connection._closed is True
        assert connection.events[-2:] == ["rollback", "close"]
    assert pool.returned == []


@pytest.mark.parametrize("close_returns", [True, None])
def test_changed_pool_policy_withholds_close_on_uncertain_cleanup(close_returns):
    ledger, _, _ = _seed_bound_cpi()
    connection = DriverCloseDouble(ledger)
    pool = connection._pool = PoolDouble(False)
    connection.fault = "rollback_stuck"
    snapshots = []

    def call():
        with _source(connection).operator_snapshot() as snapshot:
            snapshots.append(snapshot)
            inspect_binding_candidates(snapshot, BLS_CPI_HEADLINES.key)
            pool.close_returns = close_returns

    _classified(call)
    assert connection.events[-1] == "rollback"
    assert "close" not in connection.events
    assert connection.closed is False and connection._closed is False
    assert connection.info.transaction_status is INTRANS
    assert pool.returned == []
    _classified(snapshots[0].operator_reads)


@pytest.mark.parametrize(
    "settings",
    [
        ("read committed", "on"),
        ("repeatable read", "off"),
        None,
        ("serializable", "on"),
    ],
)
def test_no_isolation_or_readonly_fallback(settings):
    ledger, _, _ = _seed_bound_cpi()
    connection = SnapshotDouble(ledger)
    connection.settings = settings
    _classified(lambda: _enter(connection))
    assert connection.info.transaction_status is IDLE
    assert connection.autocommit is False
    assert connection.events[-2:] == ["rollback", ("autocommit", False)]
    assert connection.calls == []


@pytest.mark.parametrize("fault", ["enable", "begin_after", "verify"])
def test_setup_failure_restores_idle_without_leaking_raw_error(fault):
    ledger, _, _ = _seed_bound_cpi()
    connection = SnapshotDouble(ledger)
    connection.fault = fault
    _classified(lambda: _enter(connection))
    assert connection.info.transaction_status is IDLE
    assert connection.autocommit is False
    assert connection.closed is False
    assert connection.calls == []


@pytest.mark.parametrize("fault", ["begin_before", "begin_unknown"])
def test_uncertain_begin_discards_connection(fault):
    ledger, _, _ = _seed_bound_cpi()
    connection = SnapshotDouble(ledger)
    connection.fault = fault
    _classified(lambda: _enter(connection))
    assert connection.closed is True
    assert "rollback" not in connection.events


@pytest.mark.parametrize("fault", ["rollback", "rollback_stuck", "restore"])
@pytest.mark.parametrize("body_fails", [False, True])
def test_cleanup_failure_discards_and_prevents_success(fault, body_fails):
    ledger, _, _ = _seed_bound_cpi()
    connection = SnapshotDouble(ledger)
    connection.fault = fault

    def call():
        with _source(connection).operator_snapshot() as snapshot:
            inspect_as_of(snapshot, BLS_CPI_HEADLINES.key, as_of=AT)
            if body_fails:
                raise RuntimeError("private body failure")

    _classified(call)
    assert connection.closed is True
    assert connection.events[-1] == "close"


@pytest.mark.parametrize(
    "error", [ValueError("synthetic caller error"), KeyboardInterrupt()]
)
def test_body_exception_identity_survives_confirmed_cleanup(error):
    ledger, _, _ = _seed_bound_cpi()
    connection = SnapshotDouble(ledger)
    with pytest.raises(type(error)) as caught:
        with _source(connection).operator_snapshot():
            raise error
    assert caught.value is error
    assert connection.info.transaction_status is IDLE
    assert connection.autocommit is False
    assert connection.closed is False


def test_read_failure_and_nested_scope_leave_outer_scope_ownership_intact():
    ledger, _, _ = _seed_bound_cpi()
    connection = SnapshotDouble(ledger)

    def call():
        with _source(connection).operator_snapshot() as snapshot:
            before = list(connection.events)
            _classified(lambda: _enter(connection))
            assert connection.events == before
            assert inspect_binding_candidates(
                snapshot, BLS_CPI_HEADLINES.key
            ).candidates
            connection.fault = "read"
            _classified(
                lambda: inspect_as_of(snapshot, BLS_CPI_HEADLINES.key, as_of=AT)
            )
            assert connection.info.transaction_status is INERROR
            _classified(snapshot.operator_reads)

    _classified(call)
    assert connection.info.transaction_status is IDLE
    assert connection.autocommit is False
    assert connection.closed is False


def test_propagated_storage_failure_keeps_classification_and_cleans_transaction():
    ledger, _, _ = _seed_bound_cpi()
    connection = SnapshotDouble(ledger)
    connection.fault = "read"

    def call():
        with _source(connection).operator_snapshot() as snapshot:
            inspect_as_of(snapshot, BLS_CPI_HEADLINES.key, as_of=AT)

    _classified(call)
    assert connection.info.transaction_status is IDLE
    assert connection.autocommit is False


@pytest.mark.parametrize(
    "status", [IDLE, TransactionStatus.UNKNOWN, TransactionStatus.ACTIVE, 2]
)
def test_lost_transaction_state_discards_without_rollback(status):
    ledger, _, _ = _seed_bound_cpi()
    connection = SnapshotDouble(ledger)

    def call():
        with _source(connection).operator_snapshot() as snapshot:
            connection.info.transaction_status = status
            _classified(snapshot.operator_reads)

    _classified(call)
    assert connection.closed is True
    assert "rollback" not in connection.events


def test_close_failure_still_fails_closed_and_returns_no_candidate(monkeypatch):
    ledger, _, _ = _seed_bound_cpi()
    connection = SnapshotDouble(ledger)
    connection.fault = "rollback"

    def cannot_close():
        connection.events.append("failed_close")
        raise RuntimeError("private close failure")

    monkeypatch.setattr(connection, "close", cannot_close)

    def call():
        with _source(connection).operator_snapshot() as snapshot:
            inspect_binding_candidates(snapshot, BLS_CPI_HEADLINES.key)

    _classified(call)
    assert connection.events[-1] == "failed_close"
    # The candidate cannot claim a broken transport was closed. Caller must
    # discard this unusable handle; it must never be returned to a pool.
    assert connection.closed is False


def test_old_read_session_remains_invalid_inside_a_new_snapshot():
    ledger, _, _ = _seed_bound_cpi()
    connection = SnapshotDouble(ledger)
    source = _source(connection)
    with source.operator_snapshot() as snapshot:
        old = snapshot.operator_reads()
        assert old.get(BLS_CPI_HEADLINES.key) == BLS_CPI_HEADLINES
    with source.operator_snapshot() as snapshot:
        before = len(connection.calls)
        _classified(lambda: old.get(BLS_CPI_HEADLINES.key))
        assert len(connection.calls) == before
        assert snapshot.operator_reads().get(BLS_CPI_HEADLINES.key) == BLS_CPI_HEADLINES


def test_existing_budget_failure_and_lineage_classification_are_preserved():
    ledger, plan, _ = _seed_bound_cpi()
    for number in range(128):
        ledger.put(replace(plan.binding_candidate, identity=f"synthetic:{number}"))
    connection = SnapshotDouble(ledger)
    with pytest.raises(OperatorQueryBudgetError) as caught:
        with _source(connection).operator_snapshot() as snapshot:
            inspect_binding_candidates(snapshot, BLS_CPI_HEADLINES.key)
    assert caught.value.args == ("operator_query_budget_exceeded",)
    assert connection.info.transaction_status is IDLE
    _store(ledger, replace(BLS_CPI_HEADLINES, temporal_semantics="synthetic invalid"))
    with pytest.raises(ReadContractError) as caught:
        with _source(connection).operator_snapshot() as snapshot:
            inspect_as_of(snapshot, BLS_CPI_HEADLINES.key, as_of=AT)
    assert caught.value.classification is ReadFailure.LINEAGE_MISSING_OR_MISMATCHED
    assert connection.info.transaction_status is IDLE


def test_read_committed_statement_model_can_observe_changed_readback():
    # Negative control: the legacy SQL double reads the live dictionary on each
    # statement. It is not a substitute for the explicit transaction boundary.
    ledger, plan, _ = _seed_bound_cpi()
    connection = SQLDouble(ledger)
    reads = _source(connection).operator_reads()
    first = reads.binding_candidates(128)[0]
    _store(ledger, replace(plan.binding_candidate, limitations=("new synthetic view",)))
    assert reads.get(first.key) != first
