"""Explicit PostgreSQL operator read scope; no ambient transaction adoption."""

from collections.abc import Iterator
from contextlib import contextmanager

from psycopg.pq import TransactionStatus

from .consumer_read import ReadContractError, ReadFailure
from .operator_reads import PostgresOperatorReads


def _storage_error() -> ReadContractError:
    return ReadContractError(ReadFailure.STORAGE_UNAVAILABLE)


def _fail() -> None:
    # A generator resumed by contextlib.throw() still has the caller exception
    # active. Clear its implicit chain as well as suppressing traceback display.
    try:
        raise _storage_error() from None
    except ReadContractError as error:
        error.__context__ = None
        raise


def _unmanaged(connection, status: TransactionStatus) -> bool:
    # Pinned psycopg 3.3.6 bookkeeping also rejects an IDLE connection inside
    # a driver transaction/pipeline/TPC scope. Missing bookkeeping fails closed.
    return (
        connection.closed is False
        and connection.info.transaction_status is status
        and type(connection._num_transactions) is int
        and connection._num_transactions == 0
        and connection._tpc is None
        and connection._pipeline is None
    )


class _Snapshot:
    """Operator source/SQL guard; every inspection gets fresh budgets."""

    def __init__(self, connection, schema: str) -> None:
        self._connection = connection
        self._schema = schema
        self._active = False

    def _check(self) -> None:
        valid = False
        try:
            valid = (
                self._active
                and _unmanaged(self._connection, TransactionStatus.INTRANS)
                and self._connection.autocommit is True
            )
        except Exception:
            pass
        if not valid:
            _fail()

    def operator_reads(self) -> PostgresOperatorReads:
        self._check()
        return PostgresOperatorReads(self, self._schema)

    def execute(self, query, parameters):
        self._check()
        # Exclusive use is required: callers must not issue commands through
        # the original connection or change/end the transaction during scope.
        return self._connection.execute(query, parameters)


def _close_discards(connection) -> bool:
    # psycopg 3.3.6 close() can call pool.putconn() instead of closing. Accept
    # only no pool or an explicit non-returning policy; unknown fails closed.
    pool = getattr(connection, "_pool", None)
    return pool is None or getattr(pool, "close_returns", None) is False


def _discard(connection) -> None:
    try:
        if _close_discards(connection):
            connection.close()
    except BaseException:
        # Never mask the classified failure with a raw close/transport error.
        pass


@contextmanager
def operator_snapshot(connection, schema: str) -> Iterator[_Snapshot]:
    """Reject caller transactions untouched; rollback owned work on every exit.

    The caller must exclusively hold this synchronous psycopg connection for
    the entire scope. Results are usable only after successful context exit.
    """
    admitted = False
    try:
        unmanaged = _unmanaged(connection, TransactionStatus.IDLE)
        original_autocommit = connection.autocommit
        admitted = (
            unmanaged
            and type(original_autocommit) is bool
            and _close_discards(connection)
        )
    except Exception:
        pass
    if not admitted:
        # No SQL, rollback, close or setting mutation on an ambient/unknown conn.
        _fail()

    snapshot = _Snapshot(connection, schema)
    begin_attempted = False
    setup_failed = False
    cleanup_failed = False
    transaction_failed = False
    body_error = None
    try:
        try:
            # Avoid psycopg's implicit BEGIN before our explicit transaction.
            # These are connection-local settings, restored after confirmed IDLE.
            connection.autocommit = True
            if connection.autocommit is not True:
                raise _storage_error()
            begin_attempted = True
            connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if not _unmanaged(connection, TransactionStatus.INTRANS):
                raise _storage_error()
            settings = connection.execute(
                "SELECT current_setting('transaction_isolation'), "
                "current_setting('transaction_read_only')"
            ).fetchone()
            if settings != ("repeatable read", "on") or not _unmanaged(
                connection, TransactionStatus.INTRANS
            ):
                raise _storage_error()
        except Exception:
            setup_failed = True
        if not setup_failed:
            snapshot._active = True
            yield snapshot
    except BaseException as exc:
        body_error = exc
    finally:
        snapshot._active = False
        try:
            if begin_attempted:
                # Unexpected scopes or an already ended transaction are not
                # adopted. Discard instead of rolling back possibly foreign work.
                status = connection.info.transaction_status
                transaction_failed = status is TransactionStatus.INERROR
                if (
                    status is not TransactionStatus.INTRANS
                    and status is not TransactionStatus.INERROR
                ):
                    raise _storage_error()
                if not _unmanaged(connection, status):
                    raise _storage_error()
                connection.rollback()
            if not _unmanaged(connection, TransactionStatus.IDLE):
                raise _storage_error()
            connection.autocommit = original_autocommit
            if connection.autocommit is not original_autocommit:
                raise _storage_error()
        except BaseException:
            cleanup_failed = True
            _discard(connection)
    if cleanup_failed or setup_failed or (transaction_failed and body_error is None):
        _fail()
    if body_error is not None:
        raise body_error
