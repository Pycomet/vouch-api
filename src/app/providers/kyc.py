"""Identity and address verification behind :class:`KycProvider` (spec 4, R27).

A provider receives the renter's id type and number, the selfie and the address
document, and answers with a :class:`VerificationResult`. It must never log or
keep those inputs. ``Settings.kyc_provider`` picks one through
:func:`build_kyc_provider`; only ``"fake"`` exists until a real vendor is chosen,
and it is refused outside development and test, where the builder returns ``None``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.core.config import Settings


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    reference: str
    reasons: list[str] = field(default_factory=list)


@runtime_checkable
class KycProvider(Protocol):
    name: str

    async def verify_identity(
        self, id_type: str, id_number: str, selfie: bytes
    ) -> VerificationResult:
        """Check the id against the selfie."""
        ...

    async def verify_address(self, doc: bytes) -> VerificationResult:
        """Check the proof-of-address document."""
        ...


class FakeKycProvider:
    """Answers with the configured outcomes; keeps only call counts, never inputs."""

    name = "fake"

    def __init__(self, identity: bool = True, address: bool = True) -> None:
        self.identity = identity
        self.address = address
        self.identity_calls = 0
        self.address_calls = 0

    @staticmethod
    def _reference() -> str:
        return f"fake-{uuid.uuid4().hex}"

    async def verify_identity(
        self, id_type: str, id_number: str, selfie: bytes
    ) -> VerificationResult:
        self.identity_calls += 1
        reasons = [] if self.identity else ["identity_mismatch"]
        return VerificationResult(self.identity, self._reference(), reasons)

    async def verify_address(self, doc: bytes) -> VerificationResult:
        self.address_calls += 1
        reasons = [] if self.address else ["address_unverified"]
        return VerificationResult(self.address, self._reference(), reasons)


_PROVIDERS: dict[str, type[FakeKycProvider]] = {"fake": FakeKycProvider}


def build_kyc_provider(settings: Settings) -> KycProvider | None:
    """The provider the settings name (one per app), or ``None`` when there is none."""
    name = settings.effective_kyc_provider
    return None if name is None else _PROVIDERS[name]()
