"""Opt-in test-only asyncio wakeup pipe for socket-restricted offline sandboxes.

MDF_TEST_PIPE_LOOP=1 PYTHONPATH="$PWD/tests/sandbox_support:$PWD/src:$PWD" \
    uv run --no-sync --offline pytest ...
Both the environment flag and startup PYTHONPATH entry are required. See README.md
for a bounded deterministic run and the sandbox wakeup diagnosis.
Only the selector's internal thread wakeup is replaced by os.pipe. Application
transports are unchanged. Never use this module in the DEV Host process.
"""

import os

if os.environ.get("MDF_TEST_PIPE_LOOP") == "1":
    from asyncio.selector_events import BaseSelectorEventLoop

    class _PipeEnd:
        def __init__(self, fd):
            self.fd = fd

        def fileno(self):
            return self.fd

        def recv(self, size):
            return os.read(self.fd, size)

        def send(self, data):
            return os.write(self.fd, data)

        def close(self):
            os.close(self.fd)
            self.fd = -1

    def _make_self_pipe(self):
        reader, writer = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self._ssock, self._csock = _PipeEnd(reader), _PipeEnd(writer)
        self._internal_fds += 1
        self._add_reader(reader, self._read_from_self)

    BaseSelectorEventLoop._make_self_pipe = _make_self_pipe
