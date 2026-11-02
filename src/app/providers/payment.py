"""Collecting and paying out money behind :class:`PaymentProvider` (spec 4, R21, R28).

The four calls of spec 4:

* ``create_virtual_account(booking_id, amount, purpose)``: a bank account the
  renter pays into, opened when the vendor accepts (R21).
* ``initiate_payout(recipient_code, amount, reference)``: send the vendor's money.
* ``verify_webhook(signature, body)``: is this webhook body really from the provider?
* ``parse_event(body)``: the body as a :class:`PaymentEvent`.

A provider raises :class:`PaymentProviderError` when a call fails, and
``parse_event`` raises :class:`PaymentEventError` for a body it cannot read. The
event ``type`` is passed through as the provider sent it; the webhook handler
decides which types it supports (:data:`PAYMENT_EVENT_TYPES`).

:class:`FakePaymentProvider` is the only provider in M2: deterministic, in
memory, no network. ``app.providers.registry`` builds the one the settings name.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable

# The most a Numeric(12, 2) column holds.
_MAX_AMOUNT = Decimal("9999999999.99")
_CENT = Decimal("0.01")

# The event types the webhook acts on (Step 5 addendum, A14): the one upfront
# payment arriving, and a vendor payout landing.
PAYMENT_EVENT_TYPES = ("payment_captured", "payout_completed")


class PaymentProviderError(Exception):
    """The provider could not do what was asked (network, refusal, outage)."""


class PaymentEventError(ValueError):
    """A webhook body that is not a well-formed event."""


@dataclass(frozen=True)
class VirtualAccount:
    """Where the renter pays: an account number at a bank, and the provider's reference."""

    account_number: str
    bank: str
    reference: str


@dataclass(frozen=True)
class PaymentEvent:
    """One provider event, already verified and parsed.

    ``payment_reference`` is the provider reference of the payment it concerns
    (the virtual account's reference for ``payment_captured``).
    """

    provider_event_id: str
    type: str
    booking_id: uuid.UUID
    payment_reference: str
    amount: Decimal


@runtime_checkable
class PaymentProvider(Protocol):
    name: str

    async def create_virtual_account(
        self, booking_id: uuid.UUID, amount: Decimal, purpose: str
    ) -> VirtualAccount:
        """Open an account the renter pays ``amount`` into for ``booking_id``."""
        ...

    async def initiate_payout(self, recipient_code: str, amount: Decimal, reference: str) -> str:
        """Send ``amount`` to ``recipient_code``; returns the provider's transfer reference."""
        ...

    def verify_webhook(self, signature: str | None, body: bytes) -> bool:
        """Whether ``signature`` is the provider's signature of the raw ``body``."""
        ...

    def parse_event(self, body: bytes) -> PaymentEvent:
        """The event in ``body``; :class:`PaymentEventError` if it is malformed."""
        ...


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaymentEventError(f"event {field} must be a non-empty string")
    return value


def _amount(value: Any) -> Decimal:
    """Money from the body: a string or an exact JSON number, > 0, 2 places at most."""
    if isinstance(value, bool) or not isinstance(value, str | Decimal | int):
        raise PaymentEventError("event amount must be a decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise PaymentEventError("event amount must be a decimal string") from exc
    if (
        not amount.is_finite()
        or amount <= 0
        or amount > _MAX_AMOUNT
        or amount != amount.quantize(_CENT)
    ):
        raise PaymentEventError("event amount must be a positive sum of money")
    return amount.quantize(_CENT)


class FakePaymentProvider:
    """A deterministic in-memory provider for development and tests.

    The account number (10 digits) and reference come from the booking id, so the
    same booking always gets the same account. Webhook bodies are signed with
    HMAC-SHA256 (hex) under ``secret``; :meth:`sign` and :meth:`make_event` let tests
    build the bodies the provider would send. ``fail_create`` / ``fail_payout``
    make those calls raise :class:`PaymentProviderError`. Calls are recorded in
    ``accounts`` and ``payouts``.
    """

    name = "fake"
    bank = "Fake Bank"

    def __init__(self, secret: str, *, fail_create: bool = False, fail_payout: bool = False):
        self._secret = secret.encode("utf-8")
        self.fail_create = fail_create
        self.fail_payout = fail_payout
        self.accounts: list[tuple[uuid.UUID, Decimal, str]] = []
        self.payouts: list[tuple[str, Decimal, str]] = []

    async def create_virtual_account(
        self, booking_id: uuid.UUID, amount: Decimal, purpose: str
    ) -> VirtualAccount:
        if self.fail_create:
            raise PaymentProviderError("fake provider: virtual account refused")
        self.accounts.append((booking_id, amount, purpose))
        return VirtualAccount(
            account_number=f"{booking_id.int % 10**10:010d}",
            bank=self.bank,
            reference=f"fake-{purpose}-{booking_id.hex}",
        )

    async def initiate_payout(self, recipient_code: str, amount: Decimal, reference: str) -> str:
        if self.fail_payout:
            raise PaymentProviderError("fake provider: payout refused")
        self.payouts.append((recipient_code, amount, reference))
        return f"fake-payout-{reference}"

    def sign(self, body: bytes) -> str:
        """The signature the provider would send with ``body``."""
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    def verify_webhook(self, signature: str | None, body: bytes) -> bool:
        if not signature:
            return False
        return hmac.compare_digest(self.sign(body), signature)

    def make_event(
        self,
        type: str,
        *,
        booking_id: uuid.UUID,
        payment_reference: str,
        amount: Decimal,
        provider_event_id: str | None = None,
    ) -> bytes:
        """A webhook body as the provider would send it (the event id defaults to
        one derived from the type and the reference, so a resend is a replay)."""
        event = {
            "id": provider_event_id or f"evt_{type}_{payment_reference}",
            "type": type,
            "data": {
                "booking_id": str(booking_id),
                "reference": payment_reference,
                "amount": str(amount),
            },
        }
        return json.dumps(event, separators=(",", ":")).encode("utf-8")

    def parse_event(self, body: bytes) -> PaymentEvent:
        try:
            # Exact numbers: a JSON amount never passes through a binary float.
            event = json.loads(body, parse_float=Decimal, parse_constant=_refuse_constant)
        except (ValueError, UnicodeDecodeError) as exc:
            raise PaymentEventError("event body is not JSON") from exc
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
            raise PaymentEventError("event body must be an object with data")
        data = event["data"]
        try:
            booking_id = uuid.UUID(_text(data.get("booking_id"), "booking_id"))
        except ValueError as exc:
            raise PaymentEventError("event booking_id must be a UUID") from exc
        return PaymentEvent(
            provider_event_id=_text(event.get("id"), "id"),
            type=_text(event.get("type"), "type"),
            booking_id=booking_id,
            payment_reference=_text(data.get("reference"), "reference"),
            amount=_amount(data.get("amount")),
        )


def _refuse_constant(name: str) -> Any:
    raise PaymentEventError(f"event body contains {name}")
