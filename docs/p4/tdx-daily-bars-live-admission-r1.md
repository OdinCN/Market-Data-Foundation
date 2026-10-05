# P4 TDX Daily Bars Live Admission R1

This slice validates exactly one live TDX daily-bars acquisition against the
already-delivered raw-daily-bars contract and canonical kernel.

## Exact live boundary

- SDK: `tdx-python 0.1.6` in the existing isolated R1 environment;
- endpoint: `139.9.52.158:7709` only;
- dial: `api.dial(..., 2000, False)`;
- request: `get_kline(9, "sh600000", 0, 2)`;
- one guarded invocation only;
- no MDF/application retry or endpoint fallback;
- OS connect ceiling: 2 total / 2 per phase;
- wall ceiling: 15 seconds;
- any guard stop or invalid evidence stops the transaction.

The qualification harness label `600000.XSHG` is not treated as provider proof of
a MIC mapping. The explicit source request `sh600000` is mapped by this bounded
adapter to the provider-suffix MDF identity `600000.SH`. No provider-suffix→MIC
crosswalk is admitted by this slice.

## Temporal semantics

`observe.observed_at` is sampled before the provider call and is used only as the
request-start observation boundary. The outer guarded runner records a later
finished time used as MDF obtained/return boundary. No source event timestamp is
fabricated. Each bar keeps its local trading date as the row temporal semantic.

## Canonical lineage

The successful path is:

`DataRun.COLLECT`
→ one real `AcquisitionOccurrence`
→ source-projected `EvidenceMaterial`
→ finite source-bound raw-daily-bars `DatasetVersion`
→ `Publication`

The SourceBindingRevision is used for this exact acquisition/version lineage but
is not activated as a general runtime binding by this transaction.

`provider_data_attempts = 1` means the one authorized `get_kline` data request.
No authentication recovery or MDF retry is claimed.

## Material/evidence

The exact SDK projection is retained only under ignored transaction artifacts and
is hashed into the source-projected EvidenceMaterial. The controlled reference is
an artifact reference, not a public/runtime storage contract and not S3 adoption.

The safe observation keeps attempted endpoint separate from SDK-visible actual
endpoint. For `tdx-python`, the Python API cannot expose the connected peer, so
the SDK endpoint remains explicitly unobservable rather than being copied from
the attempted endpoint. Independently, the OS syscall guard must witness the
established peer as `139.9.52.158:7709`; that evidence is retained separately in
the AcquisitionOccurrence/content.

## PostgreSQL proof

After a fresh read-only gate, only schema
`mdf_p4_tdx_daily_bars_live_admission_r1` may be used.

The opt-in integration consumes the single live evidence directory and proves
Run/Acquisition/Material/Version/Publication durability plus reconnect readback.
It does not make another provider request.

Hosted CI excludes both live-provider and PostgreSQL integration paths by default.

## Non-scope

- ACTIVE binding activation;
- a second provider request or retry;
- endpoint discovery/failover;
- complete universe;
- multi-symbol/history backfill;
- corporate actions/factors/adjusted bars;
- Macro/KD/P5;
- storage adoption;
- scheduler/repair framework;
- MBO/API/WebUI;
- release/deployment/activation/adoption.
