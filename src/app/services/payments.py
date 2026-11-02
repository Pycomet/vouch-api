"""The ``payments`` rows of a booking (R21, R22).

* :func:`open_booking_account`: when the vendor accepts, the provider opens a
  virtual account for the one upfront payment (D + C + R + F, A14) and a
  ``booking`` row is recorded ``pending`` with the provider's reference and
  account number (the bank goes in ``metadata``). The payment webhook (Task 5.4)
  marks it ``captured``.
* :func:`record_refund`: every refund of money received records a ``refund`` row,
  ``pending``, for the refunded amount, with the provider that took the money.
  Executing the refund through the provider is a later adapter's job (spec 4 has
  no refund call); ops sees the pending rows.
* :func:`payment_instructions`: what the renter needs to pay an accepted booking.

Services flush and never commit: the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Booking, Payment
from app.db.models.payment import PaymentStatus, PaymentType
from app.providers.payment import PaymentProvider, PaymentProviderError
from app.schemas.errors import PaymentAccountFailedError

BOOKING_PURPOSE = "booking"


class MissingBookingPaymentError(RuntimeError):
    """A paid booking has no captured ``booking`` payment to refund against.

    Cannot happen through the API (``payment_captured`` requires that row); if
    it does, the refund is refused rather than recorded against no provider.
    """


async def open_booking_account(
    session: AsyncSession, booking: Booking, provider: PaymentProvider, amount: Decimal
) -> Payment:
    """Open the renter's virtual account and record the pending ``booking`` payment.

    A provider failure is 502 ``payment_provider_error`` and nothing is written.
    """
    try:
        account = await provider.create_virtual_account(booking.id, amount, BOOKING_PURPOSE)
    except PaymentProviderError as exc:
        raise PaymentAccountFailedError() from exc
    payment = Payment(
        booking_id=booking.id,
        type=PaymentType.booking,
        status=PaymentStatus.pending,
        amount=amount,
        currency="NGN",
        provider=provider.name,
        provider_reference=account.reference,
        provider_virtual_account=account.account_number,
        metadata_={"bank": account.bank},
    )
    session.add(payment)
    await session.flush()
    return payment


async def captured_booking_payment(session: AsyncSession, booking_id: uuid.UUID) -> Payment:
    """The booking's captured upfront payment; :class:`MissingBookingPaymentError` if none."""
    payment = await session.scalar(
        select(Payment)
        .where(
            Payment.booking_id == booking_id,
            Payment.type == PaymentType.booking,
            Payment.status == PaymentStatus.captured,
        )
        .order_by(Payment.captured_at.desc(), Payment.id)
        .limit(1)
    )
    if payment is None:
        raise MissingBookingPaymentError(f"booking {booking_id} has no captured payment")
    return payment


async def record_refund(
    session: AsyncSession, refund_of: Payment, amount: Decimal, *, reason: str
) -> Payment:
    """A ``pending`` ``refund`` row for ``amount`` of ``refund_of``, same provider."""
    refund = Payment(
        booking_id=refund_of.booking_id,
        type=PaymentType.refund,
        status=PaymentStatus.pending,
        amount=amount,
        currency=refund_of.currency,
        provider=refund_of.provider,
        metadata_={"reason": reason, "refund_of": str(refund_of.id)},
    )
    session.add(refund)
    await session.flush()
    return refund


async def pending_booking_payment(session: AsyncSession, booking_id: uuid.UUID) -> Payment | None:
    """The booking's pending upfront payment (its virtual account), if it has one."""
    return await session.scalar(
        select(Payment)
        .where(
            Payment.booking_id == booking_id,
            Payment.type == PaymentType.booking,
            Payment.status == PaymentStatus.pending,
        )
        .order_by(Payment.created_at.desc(), Payment.id)
        .limit(1)
    )


def payment_instructions(booking: Booking, payment: Payment) -> dict[str, Any]:
    """Where and how much the renter pays, and by when (``deposit_due_at``)."""
    return {
        "bank": (payment.metadata_ or {}).get("bank", ""),
        "account_number": payment.provider_virtual_account,
        "reference": payment.provider_reference,
        "amount": payment.amount,
        "due_by": booking.deposit_due_at,
    }
