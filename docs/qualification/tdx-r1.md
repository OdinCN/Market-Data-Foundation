# TDX R1 — reconciled implementation qualification

Observed 2026-10-03 (Asia/Shanghai), starting HEAD
`5a52dc44b597553dbb551aa424c1450dfcb9fcac`. Python **3.14.8**, uv **0.12.22**;
SDKs **tdx-python 0.1.6**, **pytdxdata 0.6.1**, **mootdx-plus 0.12.0**.
Original mootdx is a legacy comparator only. SDKs remain isolated lab dependencies.

**Recommended implementation candidate: tdx-python 0.1.6, with limitations.**
This choice applies only if MDF later uses TDX, with an explicitly selected public
endpoint and `redial=False`. Use only capabilities qualified below. Exclude the
BJ aggregate method from a TDX-only universe binding; native adjusted bars/factors
are not available in this implementation. No production adapter, runtime provider
fallback, deployment or MDF Primary Provider binding is authorized or introduced.

## Evidence and reconciliation

This is the single canonical R1 report. The machine matrix is
`provider_lab/tdx_r1.matrix.json`; its evidence links are local ignored paths.
The old successor/recovery reports, matrices and scripts are preserved as
diagnostics, not competing final conclusions. Original raw files were not reset,
deleted or overwritten. Six conflicting candidate versions are saved under
`artifacts/tdx-r1/single-writer-20261003/retired/`.

The selected historical windows, concept membership comparison, native units,
xdxr semantics and second-endpoint observations were reanalysed offline rather
than downloaded again. Historical executing-source revisions were not captured;
their limitation remains explicit. Numeric-string serialization defects cannot
prove original code types. Use the string-preserving successor concept snapshots,
fresh BJ identity and repaired F10 descriptors for those claims. The old Go BJ
aggregate and metadata projected from it are excluded as TDX-source proof.

Final local evidence is under `artifacts/tdx-r1/single-writer-20261003/`:

- `admissible-observations.json`: offline corrected annotations retaining original
  live timestamps, source snapshot hashes, canonical capability IDs and exceptions.
  Old membership projections are excluded as independent live requests.
- `final-retests/*/`: raw results, safe observations, configuration, command
  traces, actual closed connections, process logs and OS boundary outcomes.
- Each final retest has `revision.json` and `execution-source/`, captured **before
  child spawn**, binding it to the exact executing source hashes.
- `retest-summary.json`, `evidence-references.json`, `evidence-manifest.json`:
  final run selection, file hashes and source/runtime provenance. The manifest
  excludes itself and the later delivery record to avoid recursive hashing.
- `completion.json`: delivery identity/checks recorded only after commit/push/CI;
  qualification conclusions do not depend on its existence.

The report, matrix and engineering code are bound by their Git revision. Local
live evidence is not reproducible from Git alone. No historical provenance is
manufactured from timestamps or an old claimed PASS.

## Four bounded retest groups

Linux x86_64 ptrace observes native `connect` syscall entries, including Go
threads, once per attempted connection. `/proc` socket inodes establish peers
owned by the child. SDK command peers supplement the pool trace. Out-of-bound
addresses or the next over-budget attempt are stopped **before executing that
network syscall**. No firewall/network configuration is changed.

The columns below are total/per-phase allowed connect entries and process/per-phase
wall-clock limits. Total counts include initialization. A guard stop is a lab
intervention, not an SDK timeout or successful native error bound. Denied entries
are recorded separately and are not counted as sent connect attempts.

| Final case | Attempt limits | Wall limits (s) | Sent attempts | Process result |
| --- | --- | --- | --- | --- |
| BJ Go | 12 / 4 | 25 / 8 | 1 | exit 0; count 386, standard list error |
| BJ pytdxdata | 16 / 5 | 30 / 8 | 5 | exit 0; count 386, standard list error |
| BJ mootdx | 12 / 4 | 25 / 8 | 2 | exit 0; count 386, list/quote empty |
| Go BJ aggregate | 8 / 4 | 20 / 8 | 1 | boundary stop, exit -9 |
| pytdxdata default pool | 20 / 8 | 30 / 8 | 4 | exit 0; cached repeat and recovery succeed |
| pytdxdata good+dead | 16 / 5 | 30 / 8 | 10 | attempt stop, exit -9 |
| pytdxdata dead-only | 8 / 3 | 20 / 8 | 6 | attempt stop, exit -9 |
| Go redial off | 2 / 2 | 15 / 8 | 1 | exit 0; classified RuntimeError |
| Go redial on | 2 / 2 | 15 / 8 | 2 | next attempt blocked, exit -9 |
| Go explicit close | 4 / 3 | 20 / 8 | 1 | exit 0; post-close RuntimeError |
| mootdx raising, configured | 10 / 4 | 25 / 8 | 2 | exit 0; constructor exception captured |
| mootdx nonraising, configured | 10 / 4 | 25 / 8 | 4 | exit 0; same-server recovery and cleanup |

Elapsed values and full process outcomes are in the summary; all final cases
finished within their budgets. Only these four areas were requested live.
Four preliminary BJ runs were retained and superseded after correcting empty
payload normalization and adding pre-spawn revision capture. Two preliminary
mootdx runs were stopped before their first network call because constructor
configuration loading replaced the in-memory candidate pool. Public
`config.setup()` now precedes the pool override; only that pair was repeated.

### BJ source and completeness

The controlled endpoint is **139.9.52.158:7709**. All three server counts are
**386**, not zero. Standard single-page lists failed for Go/pytdxdata and were
empty for mootdx; an empty result and an exception remain distinct.

Go's aggregate `get_code_all(2)` attempted DNS connection
**114.114.114.114:53**, which was blocked before execution. The exact installed
native binary also contains a BSE HTTPS universe URL. This is evidence of an
external-source risk, not proof that a fresh BSE response was fetched: no DNS or
BSE request was allowed. The old 348-row aggregate cannot establish TDX-only
origin or completeness and is excluded from the final universe conclusion.

The final representative **920000.XBSE** is confirmed by fresh native pytdxdata
snapshot fields `market=2`, code and name, and Go quote exchange/code. mootdx's
prefixed BJ quote wrapper returned empty. This confirms one representative's
identity, not a complete cash universe, delisting/suspension status or freshness
for every instrument. BJ920002 remains historical diagnostic evidence only.

### Native pool, retry and cleanup behavior

pytdxdata uses the same recorded family: timeout 0.5 s, pool_min 1, pool_max 2,
std_pool_cap 1 and isolated persistent cache. Public probe-disable is enabled to
avoid repeating the full background discovery sweep; **default pool** refers to
shipped server lists, not unrestricted discovery. Good/dead lists are recorded
separately. A repeated cached key executes no new standard command. Both recovery
cases recorded **one live standard connection actually closed**. The default pool
switched from 139.9.52.158 to 103.221.142.82 and recovered an uncached key. The
good+dead pool instead attempted the dead host five times in that request stage;
the sixth was blocked. Dead-only made three startup plus three request attempts;
the fourth request-stage attempt was blocked. These are configuration-specific
native outcomes; the SDK itself did not enforce the qualification attempt caps.

Go's exact failure invocation is `api.dial(124.70.176.52:7709, 500, redial)`.
`False` returns a classified RuntimeError after one connect attempt; `True`
continues retrying and was killed before its third attempt (~6.33 s including
startup). Do not reuse the old unsupported exact 90 s outcome. With a healthy
explicit target, closing a redial-enabled client makes the next request fail;
explicit API close is distinct from an accidental connection loss. The old BJ
request poisoning a non-redial connection is retained as a limitation, requiring
fixture isolation; it is not evidence of native alternate-server fallback.

mootdx uses the same two configured candidates, preferred dead host, timeout
0.5 s, auto_retry=True and heartbeat=False. Raising mode fails because cleanup of
the failed probe raises TdxConnectionError, despite a healthy candidate probe.
Nonraising mode selects 139.9.52.158; one actual request socket is closed, native
retry reconnects to that same server, and final cleanup returns normally. There
is no demonstrated post-selection server switch in this fixture.

The old **412** count is total dead-port socket attempts across all recovery
stages, not the two failure stages alone. Those stages contain 407 entries,
including 405 completed asynchronous refusals and two unfinished records. Do not
sum async and socket layers. Fresh syscall counts replace that ambiguous claim.

## Implementation × capability dispositions

`L` = QUALIFIED_WITH_LIMITATIONS; `U` = UNRESOLVED; `N` = NOT_SUPPORTED;
`R` = REJECTED. No capability is promoted to QUALIFIED_CANDIDATE. Successful
transport/request/content normalization alone is not capability qualification.

| Capability | tdx-python 0.1.6 | pytdxdata 0.6.1 | mootdx-plus 0.12.0 |
| --- | --- | --- | --- |
| security.universe | U | U | U |
| security.metadata | L | L | L — SH/SZ only |
| quote.snapshot | L — secondary | L — secondary | L — secondary |
| bars.daily | L | L | L |
| bars.intraday | L — secondary | L — secondary | L — secondary |
| trades.intraday | U | U | U |
| corporate_actions | L | L | L |
| sector.industry | L | L | U — raw fragment only |
| sector.concept | L | L | R |
| sector.membership | L | L | R |
| adjustment.factors | N | N | L — derived |
| bars.adjusted | N | L — native MAC QFQ | L — derived |
| finance.snapshot | L | L | L |
| finance.f10 | L | L | L |
| financial.statements | U | U | U |
| calendar.trading_days | U | U | U |

### Preserved semantic limits

- Seven representative stock/STAR/ChiNext/BJ/ETF/index 2024 windows agree on
  seven dates and OHLC. Offset zero is newest; each page is ascending, but raw
  multi-page arrays are **not globally ascending**. Sorting an inclusive window
  proves neither native pagination order nor intervening historical completeness.
  Gaps do not identify suspension; no sessions were filled or duplicates hidden.
- Go stock/ETF price and amount are milli-CNY; volume is truncated lots. mootdx
  volume is lots; pytdxdata volume is shares. Scaling cannot restore lost lot
  precision. Go index volume has a different native scaling; economic index-volume
  interpretation remains unqualified. Local dates do not prove absolute UTC times.
- Go/pytdxdata concept responses agree on 269 groups/41312 memberships.
  `block_zs.dat` is not industry merely because of its filename/API position.
  Actual industry evidence is `tdxhy.cfg`, with TDX/SW identities kept separate;
  pytdxdata drops market in its map. No THS taxonomy merge is performed.
  mootdx native block output has invalid codes and duplicate/mismatched membership;
  deduplication cannot repair it. Its raw industry fragment remains U.
- xdxr category-specific cash/rights/capital units and missing values are kept
  distinct. Go/pytdxdata expose no native exported factor-series API in the
  inspected versions. mootdx `adj` is derived from explicitly supplied same-client
  TDX actions, with a latest-bar anchor; omitted actions may create a hidden
  client. pytdxdata adjusted bars use native MAC QFQ, whose anchor/algorithm and
  timestamp differences are limitations. No external factor helper was invoked.
- Latest finance snapshots are not historical financial statements or publication
  chronology. F10 descriptors/text are a separate capability. Nonempty text is
  content success; the old string-to-rows normalization failure is corrected
  offline. True statements, listing/status and calendar semantics remain U.
- 110.41.147.114 second-endpoint results remain historical: Go two daily pages
  succeed, pytdxdata decode fails, mootdx constructor fails. No alternate server
  was substituted. These are endpoint/protocol findings, not baseline failures.

## Engineering and delivery gates

The full old bake-off driver and its broad live test are retired as executable
candidate behavior. The bounded CLI covers only the four reconciliation areas;
pure checks remain offline. All live tests need marker selection **and** explicit
`--live-provider`; GitHub CI excludes them and has no LAN/provider prerequisite.
SDK packages, raw data, source snapshots, logs and credentials remain outside Git.

Deterministic contracts/semantics/OS-guard tests, hygiene, ruff check/format,
ordinary pytest and build are required before the single commit. The actual
exact-HEAD push/CI/clean-worktree outcome belongs in the final response and local
delivery record, rather than a predicted PASS in this report.
