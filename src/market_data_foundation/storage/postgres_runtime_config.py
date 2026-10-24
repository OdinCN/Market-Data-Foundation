"""Global non-secret transport configuration for MDF's pinned remote DEV PG.

This is a bounded DEV contract, not a general PostgreSQL configuration library.
Loading validates local files only; it never connects or loads credentials.
"""

import os
import ssl
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from market_data_foundation.canonical.model import ContractError

DEFAULT_RUNTIME_PATH = Path("/srv/market-data-foundation/config/local/runtime.toml")
DEV_HOST = "192.168.10.4"
DEV_DATABASE = DEV_USER = "market_data_foundation_dev"
SSLMode = Literal["require", "verify-ca", "verify-full"]

# Exact libpq controls, not a PG* prefix ban. Credentials, application names,
# timeouts and unrelated PG-prefixed process settings remain caller-owned.
_CONTROLLED_ENVIRONMENT = frozenset(
    {
        # Endpoint/identity selection and service indirection.
        "PGHOST",
        "PGHOSTADDR",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGSYSCONFDIR",
        "PGTARGETSESSIONATTRS",
        "PGLOADBALANCEHOSTS",
        # TLS trust, client identity, protocol and alternative GSS transport.
        "PGSSLMODE",
        "PGREQUIRESSL",  # Legacy libpq spelling.
        "PGSSLNEGOTIATION",
        "PGSSLCOMPRESSION",
        "PGSSLCERT",
        "PGSSLKEY",
        "PGSSLCERTMODE",
        "PGSSLROOTCERT",
        "PGSSLCRL",
        "PGSSLCRLDIR",
        "PGSSLSNI",
        "PGSSLMINPROTOCOLVERSION",
        "PGSSLMAXPROTOCOLVERSION",
        "PGGSSENCMODE",
        "PGKRBSRVNAME",
        "PGGSSLIB",
        "PGGSSDELEGATION",
        "PGCHANNELBINDING",
        "PGREQUIREAUTH",
        "PGREQUIREPEER",
        # Startup SQL options can change role/session security assumptions.
        "PGOPTIONS",
    }
)


class PgConfigError(ContractError):
    """Classified configuration failure; no input values or raw driver errors."""


def _file(path: Path, *, allow_symlinks: bool = False) -> None:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or ".." in path.parts
        or any(ord(c) < 32 or ord(c) == 127 for c in str(path))
        or str(path) != str(path).strip()
    ):
        raise PgConfigError("postgres_config_path_invalid")
    try:
        resolved = path.resolve(strict=True)
        valid = (
            (allow_symlinks or resolved == path)
            and resolved.is_file()
            and bool(resolved.stat().st_mode & 0o444)
        )
    except OSError, ValueError, RuntimeError:
        valid = False
    if not valid:
        raise PgConfigError("postgres_config_file_unreadable")


def _environment() -> None:
    # Reject controlled overrides even if empty or redundant with explicit
    # keywords. Never read/report values or mutate the caller's environment.
    if _CONTROLLED_ENVIRONMENT.intersection(os.environ):
        raise PgConfigError("postgres_environment_not_allowed")


@dataclass(frozen=True)
class PgConfig:
    host: str
    port: int
    database: str
    user: str
    sslmode: SSLMode
    sslrootcert: Path | None = None

    def __post_init__(self) -> None:
        if (
            self.host != DEV_HOST
            or type(self.port) is not int
            or self.port != 5432
            or self.database != DEV_DATABASE
            or self.user != DEV_USER
        ):
            raise PgConfigError("postgres_target_mismatch")
        if not isinstance(self.sslmode, str) or self.sslmode not in (
            "require",
            "verify-ca",
            "verify-full",
        ):
            raise PgConfigError("postgres_sslmode_invalid")
        if self.sslrootcert is None:
            if self.sslmode != "require":
                raise PgConfigError("postgres_sslrootcert_required")
        else:
            _file(self.sslrootcert, allow_symlinks=True)
            valid = False
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.load_verify_locations(cafile=str(self.sslrootcert))
                valid = bool(context.get_ca_certs())
            except OSError, ValueError:
                pass
            if not valid:
                raise PgConfigError("postgres_sslrootcert_invalid")

    def connection_kwargs(self) -> dict[str, str | int]:
        """Recheck trust/environment immediately before handing off to libpq."""
        _environment()
        self.__post_init__()
        result = {
            "host": self.host,
            "hostaddr": self.host,  # Physical pin; no DNS/environment redirection.
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "sslmode": self.sslmode,
            "gssencmode": "disable",  # Require TLS even on a Kerberos-enabled host.
        }
        if self.sslrootcert is not None:
            result["sslrootcert"] = str(self.sslrootcert)
        return result


def load_pg_config(path: Path) -> PgConfig:
    """Read an explicitly selected absolute runtime file; no path/env fallback."""
    _environment()
    _file(path)
    runtime = None
    try:
        with path.open("rb") as stream:
            runtime = tomllib.load(stream)
    except OSError, ValueError:
        pass
    if runtime is None:
        raise PgConfigError("postgres_runtime_invalid")
    pg = runtime.get("postgres")
    required = {"host", "port", "database", "user", "sslmode"}
    if (
        not isinstance(pg, dict)
        or not required <= pg.keys()
        or pg.keys() - required - {"sslrootcert"}
    ):
        raise PgConfigError("postgres_config_shape_invalid")
    root = pg.get("sslrootcert")
    if "sslrootcert" in pg and (
        not isinstance(root, str) or not root or root != root.strip()
    ):
        raise PgConfigError("postgres_sslrootcert_invalid")
    return PgConfig(
        host=pg["host"],
        port=pg["port"],
        database=pg["database"],
        user=pg["user"],
        sslmode=pg["sslmode"],
        sslrootcert=Path(root) if root is not None else None,
    )
