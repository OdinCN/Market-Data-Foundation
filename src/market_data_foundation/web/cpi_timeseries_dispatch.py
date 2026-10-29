"""Detach a durable CPI job from the lifetime of its submitting HTTP request."""

from copy import deepcopy
from queue import Queue
from threading import Lock, Thread


def dispatch_job(submit, command):
    """Return once submit has persisted running, or return its replay/error.

    ``submit(command, on_started=callback)`` owns admission, the execution lock,
    journal updates and service cleanup. It invokes the callback only after the
    running record is durable, while still holding its execution lock. The
    worker keeps that ownership after this function returns. An interrupted
    worker leaves journal recovery to the existing execution-lock protocol.
    """
    pending = deepcopy(command)
    response = Queue(maxsize=1)
    publication = Lock()
    published = False

    def reply(value, *, failed=False):
        nonlocal published
        with publication:
            if published:
                return
            snapshot = value if failed else deepcopy(value)
            response.put_nowait((failed, snapshot))
            published = True

    def run():
        try:
            result = submit(pending, on_started=reply)
            reply(result)
        except BaseException as error:
            # Before acknowledgment, propagate even an interrupted worker so
            # the submitting request cannot wait forever. After acknowledgment,
            # the durable journal remains the source of execution status.
            reply(error, failed=True)

    Thread(target=run, name="mdf-cpi-collection", daemon=True).start()
    failed, result = response.get()
    if failed:
        raise result
    return result
