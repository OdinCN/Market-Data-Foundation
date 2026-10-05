"""Protection for the bounded in-process synthetic material path.

This is an ownership barrier, not a lease for an external storage system.
The immutable PostgreSQL bytea handoff is the only durable path in this kernel.
"""

from enum import StrEnum

from .model import digest, require


class ProtectionState(StrEnum):
    UNPROTECTED = "unprotected"
    ACTIVE = "active"
    FINAL_VERIFIED = "final_verified"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RESOLVED = "resolved"


class ProtectedMaterial:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        require(all(type(v) is bytes for v in payloads.values()), "mutable_material")
        self._payloads = dict(payloads)
        self.state = ProtectionState.UNPROTECTED
        self._verified: dict[str, str] = {}

    def preliminary_verify(self, expected: dict[str, str]) -> None:
        self._verify(expected)

    def replace(self, identity: str, payload: bytes) -> None:
        require(self.state == ProtectionState.UNPROTECTED, "material_protected")
        require(type(payload) is bytes, "mutable_material")
        self._payloads[identity] = payload

    def activate(self) -> None:
        require(self.state == ProtectionState.UNPROTECTED, "protection_not_available")
        self.state = ProtectionState.ACTIVE

    def final_verify(self, expected: dict[str, str]) -> None:
        require(self.state == ProtectionState.ACTIVE, "protection_not_active")
        self._verify(expected)
        self._verified = dict(expected)
        self.state = ProtectionState.FINAL_VERIFIED

    def _verify(self, expected: dict[str, str]) -> None:
        require(bool(expected), "empty_final_verification")
        for identity, expected_digest in expected.items():
            require(identity in self._payloads, "material_unavailable")
            require(
                digest(self._payloads[identity]) == expected_digest, "material_mismatch"
            )

    def payload(self, identity: str, expected_digest: str) -> bytes:
        require(self.state == ProtectionState.FINAL_VERIFIED, "final_verify_required")
        require(
            self._verified.get(identity) == expected_digest, "material_not_verified"
        )
        self._verify({identity: expected_digest})
        return self._payloads[identity]

    def lose_protection(self) -> None:
        """Invalidate a pre-handoff verification; never release an unresolved write."""
        require(self.state != ProtectionState.OUTCOME_UNKNOWN, "unresolved_protection")
        require(self.state != ProtectionState.RESOLVED, "already_resolved")
        self.state = ProtectionState.UNPROTECTED
        self._verified.clear()

    def pending(self) -> None:
        require(self.state == ProtectionState.FINAL_VERIFIED, "final_verify_required")
        self.state = ProtectionState.OUTCOME_UNKNOWN

    def resolve(self) -> None:
        require(
            self.state
            in {ProtectionState.FINAL_VERIFIED, ProtectionState.OUTCOME_UNKNOWN},
            "final_verify_required",
        )
        self.state = ProtectionState.RESOLVED

    def release(self) -> None:
        require(self.state == ProtectionState.RESOLVED, "unresolved_protection")
        self.state = ProtectionState.UNPROTECTED
        self._verified.clear()
