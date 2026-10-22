# I-4 Host intake and one-shot execution

The executable code is implemented, but extension networking currently stops at
the missing independently reviewed candidate pin. The existing frozen watchlist
driver has no extension IPC route. Do not mutate its identity or run an unknown
rebuild through its existing allowlist.

## Central integration delta

1. Review/apply `MAIN_INTEGRATION.patch` in the central integration candidate.
   It adds only the call to `I4Dispatch()`. It has **not** been applied here.
   `intraday_route_disabled.go` is the inert default; `mdf_i4` enables the extension.
2. Reproduce offline QA with the command below. Build using the existing pinned,
   preregister-patched SDK overlay and `-tags=mdf_i4`. The central builder needs
   explicit tag plumbing; do not modify the frozen SDK. Set the linker release to
   `injoyai/tdx v1.0.0@2a9000681f5a9896ea98e4de8bc4904f1dce78ba+mdf-wait-preregister-r1+i4-raw-r1`.
3. Independently freeze the exact new binary SHA. Add that release/SHA to
   `runtime/tdx_watchlist.py::DRIVER_BINARY_SHA256` only as a reviewed central
   change, leaving both historical entries intact. The new Host module imports
   the existing sealed-fd verifier; CLI and sidecars cannot add trust.
4. Independently check source inputs, Host plan, I-2 endpoint scheduling and the
   new binary. Then run the one-shot Host launcher. No service/timer is created
   by these modules. Do not execute a Go test binary to bypass the launcher.

From the worktree root, with qualified uv Python 3.14.8 and the existing offline
Host environment (no dependency or system-Python change):

```bash
export UV_CACHE_DIR=/tmp/mdf-i4-uv-cache
export UV_PROJECT_ENVIRONMENT=/srv/market-data-foundation/.venv
uv run --offline --frozen --no-sync python docs/market-four-stream-i4/go_offline_qa.py \
  --sdk-checkout /var/tmp/mdf-tdx-sdk-v100-20261021 \
  --go /var/tmp/mdf-tdx-live-20261011/toolchain/go/bin/go \
  --module-cache /var/tmp/mdf-tdx-live-20261011/pkg/mod \
  --output artifacts/i4-host-qa-NEW
uv run --offline --frozen --no-sync pytest -q tests/test_i4_*
uv run --offline --frozen --no-sync ruff check .
uv run --offline --frozen --no-sync ruff format --check .
uv run --offline --frozen --no-sync python scripts/check_repository_hygiene.py --include-untracked
```

The Go QA tool validates SDK HEAD/tag, clean tracked bytes, Go 1.25.3 and the
existing patch input/output SHA. It copies tracked SDK files to a new ignored
overlay, runs only SDK protocol and default/tagged driver memory/mock race tests,
with GOPROXY/GOSUMDB off, GOMAXPROCS=2, `-p=1`. It records all input SHAs and
checks they stayed unchanged. It neither edits main nor builds/trusts a runtime
binary. A false `main_dispatch_present` is expected in the I-4 worktree.

## Plan and after-close run

A frozen plan has exact fields accepted by `I4Plan` and Python `validate_plan`:

```json
{
  "schema":"mdf.tdx.i4.raw-probe.v1",
  "sdk_commit":"2a9000681f5a9896ea98e4de8bc4904f1dce78ba",
  "run_id":"REPLACE_WITH_NEW_32_LOWERCASE_HEX",
  "source":"tdx.exhq.dev.research",
  "binding":"tdx.exhq.v100.i4.discovery-r1",
  "endpoint":"116.205.143.214:7727",
  "family":"exhq",
  "directory_identity":"",
  "requests":[
    {"operation":"ExMarkets","market":0,"code":"","category":null,"start":0,"count":0,"date":"","period":0,"kind":""},
    {"operation":"ExCount","market":0,"code":"","category":null,"start":0,"count":0,"date":"","period":0,"kind":""},
    {"operation":"ExInstruments","market":0,"code":"","category":null,"start":0,"count":64,"date":"","period":0,"kind":""}
  ],
  "max_requests":3,"max_request_bytes":4096,
  "max_response_bytes":262144,"max_decoded_bytes":262144,
  "max_frames":4,"wall_seconds":20,"operation_seconds":5,"round_interval_seconds":0
}
```

The named ExHq endpoint is from pinned `hosts_exhq.go`, not a scan or assurance
of current availability. Standard plans require a distinct standard SDK endpoint
and `family=standard`, port 7709. Go rechecks exact family-specific SDK endpoint
membership before one dial. One plan has 1–24 requests, one endpoint/connection,
no heartbeat/retry/redial/failover, wall ≤180s, operation ≤10s, request bytes ≤64KiB,
compressed and decoded cumulative bytes each ≤4MiB, and explicit frame limits.
Handshake traffic counts. Partial reads/writes, identity mismatch, sink failure
or exhausted budgets stop the run. No second endpoint is selected.

Discovery is a bounded sample, not full Universe qualification. After independent
raw inspection, freeze **a new plan** with exact sampled directory tuples and
association identity for ExQuote/ExBars/ExMinute/ExTrade and/or history methods.
Code, category and bar period remain separate. Directory pagination in this first
Host transport is intentionally limited to uint16 start, although upstream can
address uint32. Additional pages/families require new explicit finite plans;
there is no automatic exploration loop or initial three-symbol ceiling.

```bash
uv run --offline --frozen --no-sync python -m market_data_foundation.candidates.tdx_intraday_v1.plan \
  --input artifacts/NEW/input.json --output artifacts/NEW/frozen-plan.json
uv run --offline --frozen --no-sync python -m market_data_foundation.candidates.tdx_intraday_v1.host \
  --plan artifacts/NEW/frozen-plan.json --driver artifacts/REVIEWED/tdx-driver \
  --output artifacts/NEW/afterclose --live --stage afterclose
```

`afterclose` is the operator's capture context, not a universal market-status
inference. All market freshness remains UNKNOWN. The launcher persists intent,
verifies/seals the reviewed binary, runs exactly once with a minimal environment,
kills at wall+2s, and retains original JSONL before strict readback. Original
compressed frames and decompressed payloads have separate hashes/files. Corrupt
records stay in the retained JSONL and produce classified failures. No raw error
messages enter observations. Replay-only command:

```bash
uv run --offline --frozen --no-sync python -m market_data_foundation.candidates.tdx_intraday_v1.replay \
  --plan artifacts/NEW/afterclose/plan.json --records artifacts/NEW/afterclose/records.jsonl \
  --producer-sha256 REVIEWED_BINARY_SHA256 --evidence-mode retained --output artifacts/NEW/independent-readback
```

A caller-supplied replay producer SHA is a declaration, not execution attestation.
The independent reader must compare it with the Host sealed-fd/pin receipt.
PG/Garage persistence belongs to I-3; this candidate reports zero such writes and
does not turn an ignored file into a storage-durability PASS.

## 2026-10-23 session review

Only after adapter/plan/script/Host intake, the scheduler may install a separately
reviewed **single** timer. No timer has been installed by I-4. A-share 09:35 is a
candidate window; HK/futures/global require their own actual venue calendar and
instrument session, including holidays, lunch, night-session trading-day rules
and DST. Do not infer an open market from a weekday or another market.

Use `--stage session --calendar-guard artifacts/NEW/session-guard.json`. Guard
fields are exactly `calendar_material` (absolute retained path), `calendar_sha256`
and `windows`. Each window is a `SessionWindow` JSON object: venue,
calendar_revision, calendar_material_sha256, market, category, code, trading_day,
timezone (IANA), opens_at/closes_at (offset-aware local), opens_utc/closes_utc
(explicit UTC). The retained calendar bytes must actually support those exact
windows; the code verifies integrity/consistency, not external Calendar admission.

Immediately before launch the guard and calendar bytes are reread and hashed.
Every planned request must be a live API in exactly one matching open window,
and the full run plus a 2s allowance must fit before close. Historical APIs and
directory discovery cannot masquerade as this session check. DST fold endpoints
must match explicit UTC instants; nonexistent civil times fail round-trip.
GetTrade's requested day must match the explicit venue trading_day. Failure stops
without retries or timer rearming.

For one exact `(source,binding,producer,request,directory)` use at least three
nonempty rounds with strictly ordered local observation clocks, all inside the
same exact window. `adjudicate_refresh()` requires at least two changed
transitions to report `OBSERVED_CONTENT_CHANGE_IN_EXACT_WINDOW`. That result
still leaves source event clock/freshness UNKNOWN; an independent full source
clock is absent in this slice. Quote duplicates, after-close snapshots, historical
responses and changes at a different market do not qualify the target session.
