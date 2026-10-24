# MDF global PostgreSQL runtime transport R1

This is the configuration contract for MDF's existing remote DEV PostgreSQL
capability, not a universal PostgreSQL library or a PKI system. Global transport
policy lives in `config/local/runtime.toml`; credentials remain in the existing
local secret file. No dependencies, canonical kernels, Garage contracts, schema
permissions, write opt-ins or provider bindings change.

`storage.postgres_runtime_config.load_pg_config(path: Path) -> PgConfig` reads
only local non-secret configuration and returns a frozen typed profile. Imports
perform no I/O. `PgConfigError` (a `ContractError`) contains a fixed classification,
not input values or raw parsing/SSL exceptions. No connection opens on rejection.

| Explicit mode | Root requirement | Assurance |
| --- | --- | --- |
| `require` | Optional; any declared root must be valid/readable | Encryption; no hostname verification or promised CA server authentication |
| `verify-ca` | Explicit valid/readable CA file | Encryption and certificate chain verification; no hostname match |
| `verify-full` | Explicit valid/readable CA file | Encryption, certificate chain and requested host/IP verification |

These semantics follow [PostgreSQL 18 SSL documentation](https://www.postgresql.org/docs/18/libpq-ssl.html).
Libpq has a compatibility behavior: `require` verifies the chain when a root CA
file exists, including its default home-directory root file. MDF does not rely on
that behavior as an authentication guarantee. `require` without a configured
root passes **no `sslrootcert` argument** and does not search for or inject a CA.
Select a verification mode explicitly when server authentication is required.
The Host should audit the service account's libpq default files; this module is
not a sandbox for OpenSSL configuration, home-directory certificates or CRLs.
No connection failure triggers retry, transport downgrade or another provider.

`disable`, `allow` and `prefer` are deliberately rejected for this remote DEV
capability to avoid accidental plaintext credential transmission. Every connection
sets `gssencmode=disable`, because [libpq can otherwise prefer GSS encryption over
TLS](https://www.postgresql.org/docs/18/libpq-connect.html#LIBPQ-CONNECT-GSSENCMODE).

The required PG keys are exactly `host`, `port`, `database`, `user`, `sslmode`,
with optional `sslrootcert`. Unknown PG keys, DSNs, alternate targets, noninteger
ports and undeclared modes are rejected. The fixed identity is host and hostaddr
`192.168.10.4`, port `5432`, database and user `market_data_foundation_dev`.
`hostaddr` is derived and explicitly sent, not accepted as a configuration key.
Environment checks use exact names at load and immediately before a connection;
there is no blanket `PG*` rejection. The following controlled overrides are
rejected even when empty or redundant with explicit connection keywords:

| Controls | Rejected environment names | Reason |
| --- | --- | --- |
| Target and identity | `PGHOST`, `PGHOSTADDR`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGTARGETSESSIONATTRS`, `PGLOADBALANCEHOSTS` | Keep DEV identity and target selection explicit |
| Service indirection | `PGSERVICE`, `PGSERVICEFILE`, `PGSYSCONFDIR` | Prevent indirect connection/TLS defaults |
| TLS | `PGSSLMODE`, `PGREQUIRESSL`, `PGSSLNEGOTIATION`, `PGSSLCOMPRESSION`, `PGSSLCERT`, `PGSSLKEY`, `PGSSLCERTMODE`, `PGSSLROOTCERT`, `PGSSLCRL`, `PGSSLCRLDIR`, `PGSSLSNI`, `PGSSLMINPROTOCOLVERSION`, `PGSSLMAXPROTOCOLVERSION` | Keep trust, client identity and TLS protocol policy explicit |
| Alternative transport and authentication guarantees | `PGGSSENCMODE`, `PGKRBSRVNAME`, `PGGSSLIB`, `PGGSSDELEGATION`, `PGCHANNELBINDING`, `PGREQUIREAUTH`, `PGREQUIREPEER` | Prevent ambient transport/authentication policy changes |
| Session security | `PGOPTIONS` | Prevent startup role/session overrides |

Ordinary libpq inputs such as `PGAPPNAME`, `PGCONNECT_TIMEOUT`, `PGPASSFILE`,
`PGPASSWORD`, `PGCLIENTENCODING`, `PGTZ` and `PGDATESTYLE`, and unrelated names
with a `PG` prefix, are permitted. These remain caller/libpq inputs, not global
PG configuration keys. Explicit connector arguments retain precedence: for
example, `connect_dev` still supplies its password and five-second timeout.
Permitting `PGPASSFILE` does not read it during configuration validation or
guarantee authentication succeeds. No environment value is printed, copied into
the profile or used to choose a runtime path; the environment is not mutated.
Requalify this exact-name policy when upgrading libpq's connection controls.

Runtime and CA paths must be absolute and have no `..`, control characters
(including DEL) or leading/trailing whitespace. Runtime paths still reject
symlink components. CA paths may contain file or directory symlinks, including
relative link targets and chains, provided strict resolution reaches a readable
regular file. This accepts normal system aliases such as `/usr/lib/ssl/cert.pem`
when their targets satisfy the same checks; it does not select a system bundle
automatically. Dangling/looping links, directories, special files and unreadable
targets fail closed. The configured CA alias is preserved in the profile and
connection keywords and rechecked before every connection. CA files are parsed
locally by Python's standard SSL library and must contain a CA
certificate. This is a syntax/trust-input check, not server chain, validity-period
or SAN proof. CA path compatibility PASS does not establish that a bundle trusts
the real DEV server. The real TLS handshake remains necessary. Blank paths, `system`,
relative paths, absent/unreadable/empty/malformed files all fail before connecting.
An explicitly selected absolute temporary or staging runtime file is allowed
outside `/srv`; there is no directory jail that would prevent offline tests or
Host staging. There is no runtime file discovery or fallback to active config.
CA file availability/contents are revalidated for each connection; the immutable
profile freezes configuration values, not filesystem bytes. Owner-controlled
files and symlinks must remain stable during a preflight/rollout; no dynamic rotation system,
CA fingerprint mandate or certificate provisioning is introduced.

The integration points are:

- `connect_dev(password, *, runtime_path=DEFAULT_RUNTIME_PATH)` retains its
  historical positional call, timeout, autocommit and session settings. It loads
  the profile on each call. Existing SQL identity/ACL preflight is unchanged.
- Historical `mdf_i3_storage_simple_dev.credentials()` validates through the
  loader before reading secrets, with the same return shape and exact S3/Garage
  checks. Its `mdf_i1_i3_job_storage_dev` import remains valid. Do not invoke either
  script's `main()` for a read-only credential check.
- A2 Host wiring explicitly calls `load_pg_config(global_runtime_path)` and passes
  `pg_profile=profile` to `dev_read_scope_factory` or `create_dev_readonly_app`.
  An enabled reader without a profile fails closed; no active config is loaded
  implicitly. `identity.role` must equal the profile user. The caller-owned
  connector adds credentials and honors all supplied connection keywords,
  including `sslrootcert`; it must not replace them with a hardcoded CA/mode.
  A2 verifies the returned client's configured mode/root, physical IP, actual
  TLS, roles, PG18 version, schema identity/ACLs and transaction settings. Fresh
  repeatable-read read-only scopes, bounded snapshots and rollback/close behavior
  are unchanged. A `require` connection is not described as CA-authenticated.

Desired DEV configuration **after independent acceptance and code deployment**:

```toml
[postgres]
host = "192.168.10.4"
port = 5432
database = "market_data_foundation_dev"
user = "market_data_foundation_dev"
sslmode = "verify-full"
sslrootcert = "/srv/market-data-foundation/tls/marketbayesos-postgres-ca.crt"
```

The public CA's prior accepted SHA256 is
`8e9c8fac219c8cfe5d549cac5bfaaf4d4439ab0bbe20a45723cd366ceec82dfa`.
The original SAN IP `192.168.10.4` and actual CPI `2026-09` exact/as-of reads
are prior Host evidence, not new live evidence from this code stage. Offline
fixtures use synthetic certificates/data, not the active CA or live database.

Serial rollout remains: (a) Host and independent code review; (b) parse the staged
configuration and perform separately authorized REAL read-only verify-full PG
preflight plus the historical `credentials()` read-only check using candidate
code and the existing secret file in-process; (c) only once accepted code is in
the actual runtime import path, recheck the old runtime hash and CA, retain a
secure rollback copy, preserve all bytes outside the PG fields, atomically replace
the non-secret runtime file and perform read-only postflight, rolling back on
failure; (d) separately deploy explicit profile wiring for 18757/other readers;
(e) resume the Owner-requested 05 DEV write/Navicat E2E under its separate scope.
An old enabled A2 launcher must be updated to supply the profile at that deployment.
Never run the historical write scripts as configuration migration validation.

This stage changes no active runtime, MBO, services, previews, PG/Garage/provider
state, Git history or production. B0–B5, R5 seven-entry workflow, DATA B1→B2 and
PRODUCT B3 responsibilities remain intact. It does not restore the withdrawn
TDX usage-rights gate or claim B1/B3 functional acceptance.
