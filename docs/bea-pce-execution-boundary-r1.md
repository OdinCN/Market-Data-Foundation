# T04 historical offline boundary helper

`historical/bea_pce_boundary.py` is retained only as a historical offline test
helper. Public `bea_pce_live.acquire()` does not import it, allocate its single
slot, read its records or require its reviewer/grant/B1/B2/B3 assertions.
The standing Source capability and current admission rules are documented in
[bea-pce-live-edge-r1.md](bea-pce-live-edge-r1.md).

The helper accepts only its exact `OfflineFixture` of fabricated responses.
Its worker disables real network and key loading. It exercises create-only local
records, intent-before-send ordering, hash chains, sanitized candidate packaging
and read-only replay under an ignored `artifacts/` slot. The slot remains consumed
after failure/unknown; replay neither repairs it nor starts an acquisition.
Owner-controlled hashes and synthetic reviewer bindings do not authenticate a
reviewer, provider or original response. These receipts never qualify a Source.

Legacy fields such as `owner_explicit_grant_sha256`, `execution_enabled=false`,
zero real effect budgets and `B2_UNPROVED/BLOCKED` describe that offline experiment
only. Its timing counterexamples document limits of in-process supervision;
they do not block the standing BEA capability. The current adapter retains
10/20/30-second cancellation thresholds without claiming a <=30s whole-process
host guarantee. No replacement grant, verifier or control entity is introduced.

The helper's tests remain deterministic historical regression coverage. They
cannot load the real secret file, issue BEA/PG/Garage requests, authorize live
execution or substitute for fresh review and actual Source/storage/Read evidence.
