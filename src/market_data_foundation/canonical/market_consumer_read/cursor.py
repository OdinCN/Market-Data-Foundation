"""Stateless, bounded HMAC cursor. Keys are injected, never generated or persisted."""

import base64
import binascii
import hmac
from dataclasses import dataclass, field
from datetime import date, datetime

from .contracts import WIRE, Limits, ReadError, check, encode, strict_json

FIELDS = frozenset(
    {
        "v",
        "aud",
        "kid",
        "contract",
        "binding",
        "q",
        "a",
        "t0",
        "generation",
        "c",
        "s",
        "g",
        "r",
        "pos",
        "last",
        "total",
        "iat",
        "exp",
    }
)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64(value: str) -> bytes:
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
        check(b64(raw) == value, "INVALID_CURSOR", 400)
        return raw
    except ValueError, binascii.Error, UnicodeError:
        raise ReadError("INVALID_CURSOR", 400) from None


def utc(value: str) -> datetime:
    try:
        check(type(value) is str, "INVALID_CURSOR", 400)
        result = datetime.fromisoformat(value)
        check(encode(result) == encode(value), "INVALID_CURSOR", 400)
        return result
    except ValueError, TypeError, ReadError:
        raise ReadError("INVALID_CURSOR", 400) from None


@dataclass(frozen=True)
class CursorCodec:
    audience: str
    issuer: str
    keys: dict[str, bytes] = field(repr=False)
    ttl: int = 900
    skew: int = 30

    def configured(self):
        check(
            bool(self.audience)
            and self.issuer in self.keys
            and len(self.keys) <= 8
            and 0 < self.ttl <= 900
            and 0 <= self.skew <= 30
            and all(
                type(k) is str
                and 0 < len(k) <= 64
                and type(v) is bytes
                and len(v) >= 32
                for k, v in self.keys.items()
            ),
            "SIGNING_UNAVAILABLE",
            503,
        )

    def sign(self, payload: dict, limits: Limits) -> str:
        self.configured()
        payload = {
            **payload,
            "v": 1,
            "aud": self.audience,
            "kid": self.issuer,
            "contract": WIRE,
        }
        raw = encode(payload)
        check(len(raw) <= limits.decoded_cursor, "CURSOR_TOO_LARGE", 413)
        signature = hmac.digest(self.keys[self.issuer], raw, "sha256")
        token = b64(raw) + "." + b64(signature)
        check(len(token) <= limits.cursor, "CURSOR_TOO_LARGE", 413)
        return token

    def verify(
        self, token: str, query_digest: str, now: datetime, limits: Limits
    ) -> dict:
        check(type(token) is str, "INVALID_CURSOR", 400)
        check(len(token) <= limits.cursor, "CURSOR_TOO_LARGE", 413)
        self.configured()
        check(token.count(".") == 1, "INVALID_CURSOR", 400)
        left, right = token.split(".")
        raw, signature = unb64(left), unb64(right)
        check(len(raw) <= limits.decoded_cursor, "CURSOR_TOO_LARGE", 413)
        try:
            p = strict_json(raw)
            check(
                type(p) is dict and set(p) == FIELDS and encode(p) == raw,
                "INVALID_CURSOR",
                400,
            )
            check(
                type(p["last"]) is list and len(p["last"]) == 2, "INVALID_CURSOR", 400
            )
            check(
                type(p["last"][0]) is int
                and p["last"][0] >= 0
                and type(p["last"][1]) is str
                and len(p["last"][1]) == 10,
                "INVALID_CURSOR",
                400,
            )
            try:
                check(
                    date.fromisoformat(p["last"][1]).isoformat() == p["last"][1],
                    "INVALID_CURSOR",
                    400,
                )
            except ValueError:
                raise ReadError("INVALID_CURSOR", 400) from None
            check(
                type(p["kid"]) is str and p["kid"] in self.keys, "INVALID_CURSOR", 400
            )
            check(
                hmac.compare_digest(
                    signature, hmac.digest(self.keys[p["kid"]], raw, "sha256")
                ),
                "INVALID_CURSOR",
                400,
            )
            check(
                type(p["v"]) is int
                and p["v"] == 1
                and p["aud"] == self.audience
                and p["contract"] == WIRE
                and p["q"] == query_digest,
                "INVALID_CURSOR",
                400,
            )
            for name in ("pos", "total", "iat", "exp"):
                check(type(p[name]) is int and p[name] >= 0, "INVALID_CURSOR", 400)
            check(
                0 < p["pos"] < p["total"]
                and type(p["last"]) is list
                and len(p["last"]) == 2,
                "INVALID_CURSOR",
                400,
            )
            for name in ("binding", "q", "c", "s", "g", "r"):
                check(
                    type(p[name]) is str
                    and len(p[name]) == 64
                    and all(c in "0123456789abcdef" for c in p[name]),
                    "INVALID_CURSOR",
                    400,
                )
            check(
                type(p["generation"]) is str and bool(p["generation"]),
                "INVALID_CURSOR",
                400,
            )
            a, t0 = utc(p["a"]), utc(p["t0"])
            timestamp = now.timestamp()
            check(
                a <= t0
                and abs(t0.timestamp() - p["iat"]) < 1
                and p["iat"] <= timestamp + self.skew
                and 0 < p["exp"] - p["iat"] <= self.ttl,
                "INVALID_CURSOR",
                400,
            )
            check(timestamp < p["exp"] + self.skew, "CURSOR_EXPIRED", 410)
            return p
        except ReadError as exc:
            if exc.code in {"CURSOR_EXPIRED", "INVALID_CURSOR"}:
                raise
            raise ReadError("INVALID_CURSOR", 400) from None
        except TypeError, KeyError, IndexError, ValueError, OverflowError:
            raise ReadError("INVALID_CURSOR", 400) from None
