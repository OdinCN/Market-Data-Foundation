# T04 offline single-attempt execution boundary R1

This revision implements the feasible offline portion of formal preflight B1 and
records B2 as `B2_UNPROVED/BLOCKED`. It grants no real execution authority.
Provider, PG and Garage budgets remain zero; `execution_enabled` and
`owner_opt_in` remain false. The old preflight is immutable. B3 and real
original/projection Source qualification remain unestablished.

`historical/bea_pce_boundary.py::dry_run` accepts only the exact `OfflineFixture`
type, containing two in-memory fabricated responses and an optional fixed-table
failure. It exposes no live transport, loader callback, grant flag or CLI.
The spawned worker uses a fixed synthetic authentication marker and disables
network connection/DNS functions and the real key loader inside the child.
The adapter's public `acquire` rejects all transport/loader callback pairs before
entering a supervisor, regardless of fake, wrapped or built-in transport identity
or a loader's identity, alias, wrapper or returned string. Offline execution uses
this exact fixture type and the internal fixed synthetic loader only.
Private worker/transport functions remain implementation primitives for offline
tests, not an authorized execution API or a sandbox against arbitrary Python.

One fixed owner-local slot is used per worktree:
`artifacts/t04-source-execution-boundary-r1/`. It is ignored, owner-owned, mode
0700. Every ancestor is opened from `/` through directory descriptors with
`O_NOFOLLOW`, checking directory type, trusted owner and no group/other write
bits. System ancestors may belong to root or the caller; the worktree, artifacts
and slot must belong to the caller. Ancestor modes 0755/0700 are allowed;
0775/0777 and symlinks are rejected, including shared sticky `/tmp`. The worktree
must therefore reside beneath trusted ancestors. Untrusted users cannot move a
consumed slot through a writable parent and obtain a fresh admission.
Records are mode 0600, create-only with `O_EXCL|O_NOFOLLOW`, and fsynced
before acknowledgement. A nonblocking exclusive directory `flock` serializes
writers. Slot allocation itself is exclusive: any existing slot directory
consumes admission, including an empty directory left by failure of the first
intent's `O_EXCL` open or the slot parent's fsync. Restoring filesystem IO, a new
attempt ID, manifest, partial write, crash, unknown result or restart cannot
readmit it. The creator must fsync the slot parent and persist intent before
entering the supervisor. There is
no delete, reset, resume, repair, fallback or retry API. Administrative deletion
or replacement by the filesystem owner is outside this boundary; these local
files are not tamper-proof storage or cryptographic reviewer authentication.
Future real use requires an independently verified fixed execution owner/location
and a real grant ledger; relocating/cloning an offline worktree is not authority.

The first durable record precedes worker spawn and any fake exchange. It binds
the exact current repository commit **and** the actual versioned bytes of all
three PCE source modules, code-manifest SHA, revision, plan SHA, an explicitly
synthetic reviewer binding, missing real Source review/grant, exact ordered
GetData parameters, 10/20/30 thresholds and zero real effect budgets. It does
not claim that the uncommitted code is already in the Git commit. Code/base/
manifest bindings are checked again before each exchange. The unique generated
attempt ID uses short colon-separated tokens compatible with the unchanged
conservative echo scanner. Each request intent is durable before pipe permission;
each completed-response observation is durable before continuation. Slot allocation
consumes the attempt even if intent persistence or spawn fails; send intent counts
conservatively even without
acknowledgement. Unknown remote receipt remains unknown.

Records form a SHA256 chain; only fixed classifications and nonsecret timings,
parameters and identity hashes enter it. A complete candidate is retained only
after both responses pass the unchanged sanitizer, mapping, metadata, cross-table
and replay checks. One create-only package contains the clean projection and
adapter receipt (base64 encoded), plus a synthetic producer execution receipt
bound to the exact intent/observations chain. This encoding is packaging, not a
secret scrubber: sanitation occurs first. Each response's wire/decoded cap remains
2,000,000 bytes, projection <=2,000,000, receipt <=32,768, normalization <=32 MB;
the enclosing base64 package has its own 2,800,000-byte cap. No raw response,
authenticated URL, original-response hash, key value or raw error is retained.
Failed/partial acquisition retains observations only. Persistence failure can
leave an incomplete immutable record or complete-only package without a terminal
outcome; replay rejects it, the slot stays consumed, and no retry follows.

`replay` opens existing files read-only, rejects symlinks, hardlinks, unsafe modes,
oversized/changed records, code/base/manifest/reviewer drift, missing terminal
outcome or broken chains. It replays the projection against its adapter receipt
and binds the synthetic producer receipt to the controlled observation bytes.
It never starts a worker, creates a slot, repairs a record or returns Source
PASS. A digest proves byte consistency under owner-local filesystem control; it
does not establish an actual BEA producer, original identity or independent
qualification. Offline reviewer hashes are explicitly assertions for tests.

B2's requested scope begins before spawn/start and includes pipes/clocks,
cancel/terminate/kill/join/cleanup, fsync/close and persisted observations/reaping.
The present host exposes a read-only cgroup v2 mount; the agent has no qualified
delegated deadline/reclamation service. `systemd-run`/`timeout` binaries alone
do not establish one. A timer in the same process cannot bound blocked start,
pipe, clock or fsync; a watchdog that kills the parent still does not guarantee
worker reaping and durable observations before the original 30-second cutoff.
Adding a longer timer or a timed join that leaves an orphan would not satisfy B2.

The offline tests deliberately inject finite blocking start, pipe, clock and
cleanup-join operations and show return after shortened cancellation thresholds;
they also exercise a child that ignores SIGTERM, blocked exchange/slow drip,
combined-network cancellation and slow persistence. These are negative structural
evidence, not real-network 10/20-second qualification or full 30-second proof.
The original supervisor's unbounded start/final join is retained as an explicit
laboratory limitation behind closed real admission. No complete host hard bound
is claimed. A future capability must independently prove termination, reaping and
observation durability within the same <=30 seconds, including adverse host IO
and scheduling, before any real path can be enabled.

The next review must bind current file bytes/SHA and this offline evidence,
evaluate B1's actual owner/grant/reviewer/receipt trust and B2's residual blocker,
then pass Host full QA and serial Git/CI before independent Source-specific B3.
Historical adapter review/CI and this offline receipt cannot substitute for B3.
Real HTTP still requires B1/B2/B3 closure, fresh canonical/direct-remote identity
and exact explicit Owner opt-in. Source original and projection remain unqualified;
the clean projection is transformed data and never exact original BEA bytes.
