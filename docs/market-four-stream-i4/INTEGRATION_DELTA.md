# Central integration changes required by I-4

These are proposed changes to centrally owned files. None is applied in this
worktree. The I-4 owned implementation and tests are usable independently offline.

| File | Exact required change | Acceptance |
| --- | --- | --- |
| `drivers/tdx/main.go` | Apply adjacent `MAIN_INTEGRATION.patch`: call `I4Dispatch()` before existing version/IPC handling | Default builds return false; tagged candidate accepts only `--i4-probe`; missing `MDF_I4_ALLOW_LIVE=1` returns 2 without dialing |
| `scripts/build_tdx_driver.py` | Add an explicit I-4 candidate profile (for example `--i4-candidate`), reject that profile unless SDK version is v1.0.0; append `+i4-raw-r1` to the existing derived release; record profile and tags in build receipt | Existing default/rollback profiles retain existing release and build flags |
| Same builder test command | For the I-4 profile insert `-tags=mdf_i4` into **both** `go test` and `go build`; keep protocol tests offline and pin/hash/patch/toolchain checks | Tagged memory/mock race tests and default suite pass; explicit new `--version` equals new release; record exact resulting binary SHA |
| `runtime/tdx_watchlist.py` | After independent binary review, add the exact `host.I4_RELEASE` → independently frozen SHA entry to `DRIVER_BINARY_SHA256`; do not replace either existing entry | Old/new mismatches still fail before child launch; no config/sidecar/CLI pin override; Host starts the same sealed snapshot that was hashed |

The profile release is exactly
`injoyai/tdx v1.0.0@2a9000681f5a9896ea98e4de8bc4904f1dce78ba+mdf-wait-preregister-r1+i4-raw-r1`.
No new binary SHA is proposed here: there is no centrally wired, independently
qualified binary yet. The existing watchlist K-frame/ACK schema does not carry
I-4 requests and is not reused. The new entry consumes one bounded JSON plan on
stdin and emits bounded JSONL raw-transport records on stdout.

Central D2/D3/D4 and I-3 may read the new immutable receipt/projection as candidate
input. Supporting these new capabilities as formal published datasets requires
separate capability-specific domain/schema work. No core contract relaxation,
implicit Source fallback or reinterpretation of a daily bar Version is requested.
