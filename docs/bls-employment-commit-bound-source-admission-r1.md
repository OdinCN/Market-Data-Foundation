# T03 committed Source evidence boundary R1

This candidate adds `bls_employment_source_control.py`, an isolated, read-only
commit-bound evidence admission and a closed `execute_source_once` entry.
The existing `bls_employment_live.py` candidate guard and all its safety tests
remain in force. There is no execution bridge between the two paths.

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

These pins recognize historical evidence only. They do not confer independent
review of this new implementation. This uncommitted candidate is dirty and thus
cannot pass its own public historical inspection. A future commit, even on the
same branch, also fails until a new independently reviewed binding is established.
No future SHA, reviewer or authorization is predicted here. A result always has
`source_qualified=False` and `execution_enabled=False`.

`execute_source_once(owner_opt_in=...)` first checks the committed evidence.
For a clean matching historical checkout, absent opt-in yields
`named_source_only_owner_opt_in_required`; any supplied object yields
`trusted_external_grant_verifier_unavailable`. A bool, credential presence,
review verdict, signed-looking JSON, manual receipt or fabricated successful
inspection cannot authorize a POST. Observations contain fixed classifications,
zero provider requests, no attempt and no retry. A repeated or unknown-state
request remains rejected. This is a fail-closed entry, not a usable live runner.

The concrete missing capability is an externally authenticated, independently
issued grant channel whose trust root and issuer/reviewer identity cannot be
selected by this caller. It must bind the new admitted code/commit/tree/blobs,
new implementation reviewer and manifest, reviewed plan, request/capability/
fixture, expiry/revocation, named Owner source-only opt-in and a unique one-use
grant identity. Historical professional PASS and PLAN_PASS_READ_ONLY are evidence
inputs only; neither grants a real effect. The current plan has execution off
and all real effect budgets zero. No such external grant verification mechanism
was found in the existing child control surface. No READY is claimed.

A later independently reviewed implementation would also need a source-specific,
owner-local durable one-attempt consumer: fsynced nonsecret intent before send,
atomic duplicate protection, durable count/observation, and fail-closed restart,
unknown and retry handling. This candidate creates no attempt, intent, counter,
HTTP transport, key loader or artifact; it does not claim to have implemented or
qualified that future runner. Introducing a send callback without that consumer
would violate this boundary.

The unchanged contract remains one BLS v2 POST for CES0000000001/LNS14000000/
CES0500000003, 2025–2026, catalog/calculations, 4096 request bytes, 200000 response
bytes, a hard 20s network deadline within a hard 30s attempt, no redirect/retry/
fallback. A future successful response must pass original-byte echo screening
before any hash/report/retention, then retain clean original bytes and receipt
only under ignored `artifacts/`. A separate independent post-response gate alone
can establish Source qualification. PG/Garage, SourceBinding and production
effects retain zero budgets and separate authority.

Portable tests use explicit synthetic evidence and Git-output fixtures through
private seams, with key/transport/attempt/socket/PG traps. They are not real source
proof. The EO also records offline readback using actual existing Git objects and
historical reviewer hashes in a temporary local checkout, without new commits.
The existing 201 rejection and 112 legal encoding cases remain required regression
evidence. No real provider response or credential is used in any test.

Next gates are exact frozen candidate independent professional review, exclusive
Host full QA, coordinator Git/exact-head Hosted CI/Postflight, then separately
authenticated named Owner source-only opt-in and reviewed one-use execution.
No self-issued professional PASS, product completion or T05 dispatch follows.
