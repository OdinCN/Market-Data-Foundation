# T03 Source entry and historical evidence inspection

The latest Owner Occam ruling removes additional grant/verifier/per-attempt
approval requirements for the existing MDF BLS Source capability. The current
entry is `execute_source_once(enabled=True)`, delegating to the bounded Employment
runner. It does not call historical commit admission and requires no manifest,
review receipt, signed grant or verifier. Explicit live opt-in remains required;
importing the module or ordinary Read does not execute a request.

The previous unconditional `trusted_external_grant_verifier_unavailable` failure
and the live runner's obsolete uncommitted-candidate manifest gate are removed.
Historical T03 CONTRACT/STATUS gate descriptions remain historical control
records, not new execution prerequisites. Fresh independent review of this
correction can proceed directly to the bounded real Source → Garage → PG → Read
qualification run using existing capabilities and job/effect accounting. This
turn makes no real Provider, Garage or PG requests and claims no qualification.

`inspect_committed_source()` verifies the historical independent review,
manifest, source plan and PLAN_PASS_READ_ONLY review, Host 3711 evidence,
Hosted CI and independent Postflight against fixed external evidence digests.
It verifies actual Git HEAD `b0e233305891de8d1aa9e8f20121209a8a25597d`, tree
`ad6cb4525ac12688e427748257bdc5e6c2ff117c`, parent and T03 branch; the exact three
historical product paths, 100644 modes, Git blob SHAs, byte sizes and SHA256s
must agree with their reviewed manifest. It requires the complete tracked and
nonignored untracked dirty set to be empty, rejects index visibility overrides,
symlinks and special evidence files, and repeats identity/evidence checks before
returning. The public API accepts no caller-supplied SHA, path, verifier or receipt.
All Git introspection goes through a helper that explicitly passes the global
`--no-optional-locks` option, including status and both identity checks, so status
cannot write back a refreshed index through optional locking. This does not depend on
the launcher's environment or change any provenance check. Offline argv capture
tests cover every Git command with `GIT_OPTIONAL_LOCKS` absent or set to `1`.

The inspection result is audit-only (`source_qualified=False`,
`execution_enabled=False`). A different HEAD or changed bytes still fail this
historical inspection; they do not prevent the separate current Source entry.
The historical constants and evidence digests are preserved, not rebound to
pretend that an old review covers new code.

The Source contract remains one BLS v2 POST for CES0000000001/LNS14000000/
CES0500000003, 2025–2026, catalog/calculations, 4096 request bytes, 200000 response
bytes, a hard 20s network deadline within a hard 30s attempt, no redirect/retry/
fallback. Original-byte echo screening precedes hashes, reporting and retention.
Safe attempt/pending/release receipts and retained bytes stay under ignored
`artifacts/`; failures expose classifications, never secrets or raw exceptions.
Unknown is not automatically retried, reconciled or repaired. Existing journal
identity, duplicate handling and explicit reconcile/retained repair semantics
are preserved. No new grant-consumer subsystem is introduced.

A Source result remains unqualified until independently checked against the
source-specific capability/fixture/observation contract and original bytes.
The retained candidate then enters `EmploymentLocalJob` with its exact digest
and existing bounded storage preflight: Garage raw identity and independent
verification, PG Version/Publication/lineage, then independent exact-version,
as-of and reference local Read. Storage job accounting remains separate from the
one Source request. No production or SourceBinding activation is added.

Offline tests trap sockets/PG/real credentials, exercise Source dispatch with no
historical gate or grant inputs, preserve historical unknown evidence without rewriting it, and run the
supervised fake Source response through the existing retained job and local Read
using in-memory storage/ledger. Existing byte screening, receipt, deadline,
parser, provenance and failure/reconcile/repair regression tests remain in scope.
These simulations do not establish real Provider/Garage/PG qualification.
