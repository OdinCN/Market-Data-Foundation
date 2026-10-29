# Offline sandbox TestClient runs

In the MDF-DEV agent sandbox, creating a Unix socket pair succeeds, but sending
the asyncio wakeup byte raises `PermissionError` (`EPERM`, errno 1). Python
3.14.8's `BaseSelectorEventLoop._write_to_self` catches that `OSError`. The
cross-thread callback stays queued while the event loop waits in its selector;
the calling thread waits on an AnyIO portal future. Consequently Starlette
`TestClient.__enter__` can hang before the first HTTP request or assertion.
This does not establish a deadlock in the application or a Python/AnyIO version
incompatibility.

The existing `sitecustomize.py` replaces only the selector loop's internal
wakeup socket pair with a nonblocking anonymous pipe. It is enabled **only**
when both of these are present when the Python interpreter starts:

- `MDF_TEST_PIPE_LOOP=1`;
- this directory in `PYTHONPATH`, so Python imports this `sitecustomize`.

The flag alone does not load the shim. Adding this directory to pytest's own
`pythonpath` setting is too late for interpreter startup. Include the current
worktree's `src` explicitly as well, because `.venv` can refer to a shared
environment whose editable package points to another checkout.

From the worktree root, reproduce the affected test with a 25-second limit:

```sh
timeout --kill-after=2s 25s env \
  MDF_TEST_PIPE_LOOP=1 PYTHONDONTWRITEBYTECODE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  UV_CACHE_DIR=/tmp/mdf-offline-test-uv-cache \
  PYTHONPATH="$PWD/tests/sandbox_support:$PWD/src:$PWD:$PWD/tests" \
  uv run --no-sync --offline pytest -q \
  tests/test_b3_05_dev_host.py::test_fixed_identity_same_intent_idempotence_and_independent_records \
  -o faulthandler_timeout=5
```

Run the full deterministic suite with an eight-minute total limit (including
the two-second kill grace period):

```sh
timeout --kill-after=2s 478s env \
  MDF_TEST_PIPE_LOOP=1 PYTHONDONTWRITEBYTECODE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  UV_CACHE_DIR=/tmp/mdf-offline-test-uv-cache \
  PATH="/root/.nvm/versions/node/v24.21.0/bin:$PATH" \
  PYTHONPATH="$PWD/tests/sandbox_support:$PWD/src:$PWD:$PWD/tests" \
  uv run --no-sync --offline pytest -q \
  -m 'not live_provider and not postgres_integration' \
  -o faulthandler_timeout=30
```

`faulthandler_timeout` prints thread stacks; the outer `timeout` enforces the
deadline. Preserve the pytest exit status when redirecting logs. Neither `uv
--offline` nor this shim blocks application network access: use synthetic test
fixtures and the deterministic marker selection above, without live opt-ins.

This is a Linux sandbox test aid, not a default runtime fix. It uses a private
asyncio hook and changes the wakeup mechanism in every selector loop in the
opted-in process and inheriting Python children. It does not validate socket
transport behavior, real HTTP listeners, providers, or PostgreSQL. Keep the flag
and this directory out of DEV Host/service environments; validate the normal
runtime separately under the host's authorized execution environment. No
production Python files or installed dependencies need modification.
