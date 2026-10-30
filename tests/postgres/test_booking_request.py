"""Live PostgreSQL tests for request_booking and the vendor responses (Task 3.2).

Covers spec 3.1 rows request, accept, decline, approve_vendor and expire_hold, and
the spec 2.7 tier guards (band B at tier 1 refused; band C without vendor approval
refused).
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.models import AuditEvent, AvailabilityWindow, Booking, KYCProfile
from app.db.models.audit import ActorRole
from app.db.models.booking import BookingStatus
from app.db.models.user import KYCStatus, RenterRole, TrustTier, User
from app.schemas.errors import (
    BookingGuardFailed,
    InvalidRequestError,
    InvalidTransition,
    ResourceNotFoundError,
)
from app.services.audit import Actor
from app.services.booking import BookingEvent, request_booking, transition
from app.services.notify import RecordingNotifier
from tests.postgres.factories import (
    DEFAULT_NOW,
    create_asset,
    create_booking,
    create_user,
    create_vendor_reference,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

START = date(2027, 1, 4)
END = date(2027, 1, 6)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(DEFAULT_NOW)


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


def renter_actor(user: User) -> Actor:
    return Actor(user_id=user.id, role=ActorRole.renter)


def vendor_actor(user_id: uuid.UUID) -> Actor:
    return Actor(user_id=user_id, role=ActorRole.vendor)


async def _request(session, user, asset, clock, notifier, **overrides) -> Booking:
    kwargs = {
        "renter": renter_actor(user),
        "asset_id": asset.id,
        "starts_on": START,
        "ends_on": END,
        "fulfilment": "pickup",
        "damage_contribution": False,
        "clock": clock,
        "notifier": notifier,
        "settings": get_settings(),
    }
    kwargs.update(overrides)
    return await request_booking(session, **kwargs)


async def _transition(session, booking, actor, event, clock, notifier, payload=None) -> Booking:
    return await transition(
        session,
        booking.id,
        actor,
        event,
        payload,
        clock=clock,
        notifier=notifier,
        settings=get_settings(),
    )


async def _tier2_renter(session: AsyncSession) -> User:
    """A verified renter with a vendor reference (effective tier 2)."""
    renter = await create_user(session, kyc_verified=True)
    other = await create_asset(session)
    past = await create_booking(
        session,
        asset=other,
        renter=renter,
        status=BookingStatus.completed,
        starts_on=date(2026, 6, 1),
    )
    await create_vendor_reference(session, past)
    return renter


async def _audits(session: AsyncSession, booking_id: uuid.UUID) -> list[AuditEvent]:
    result = await session.execute(
        select(AuditEvent).where(AuditEvent.entity_id == booking_id).order_by(AuditEvent.id)
    )
    return list(result.scalars())


# ---------------------------------------------------------------------------
# request
# ---------------------------------------------------------------------------


async def test_request_sets_hold_and_snapshot(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(
        db_session,
        minimum_trust_tier=1,
        daily_rate=Decimal("10000.00"),
        fulfilment={"pickup": True, "delivery": True, "delivery_fee": "2500.00"},
    )

    booking = await _request(
        db_session, renter, asset, clock, notifier, fulfilment="delivery", damage_contribution=True
    )

    assert booking.status == BookingStatus.requested
    assert booking.hold_expires_at == DEFAULT_NOW + timedelta(hours=4)
    assert booking.vendor_id == asset.vendor_id
    assert booking.fulfilment_choice == "delivery"
    assert booking.total_rental_fee == Decimal("30000.00")
    assert booking.deposit_amount == Decimal("150000.00")
    snapshot = booking.price_snapshot
    assert Decimal(snapshot["rental_total"]) == Decimal("30000.00")
    assert Decimal(snapshot["delivery_fee"]) == Decimal("2500.00")
    assert Decimal(snapshot["deposit"]) == Decimal("150000.00")
    assert Decimal(snapshot["damage_contribution"]) == Decimal("1500.00")
    assert Decimal(snapshot["commission_rate"]) == get_settings().commission_rate
    assert snapshot["rate_basis"] == "daily"

    [audit] = await _audits(db_session, booking.id)
    assert audit.action == "booking.requested"
    assert audit.actor_id == renter.id
    assert audit.actor_role == "renter"
    assert audit.after["status"] == "requested"
    assert notifier.calls[-1].user_id == asset.vendor_id
    assert notifier.calls[-1].template == "booking.requested"


async def test_request_band_a_at_tier1_ok(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session, minimum_trust_tier=1, band="A")

    booking = await _request(db_session, renter, asset, clock, notifier)

    assert booking.status == BookingStatus.requested


async def test_request_band_b_at_tier1_refused(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session, minimum_trust_tier=2, band="B")

    with pytest.raises(BookingGuardFailed) as caught:
        await _request(db_session, renter, asset, clock, notifier)

    assert caught.value.code == "tier_insufficient"
    assert caught.value.status_code == 409
    assert notifier.calls == []


async def test_request_band_c_at_tier1_refused(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session, minimum_trust_tier=3, band="C")

    with pytest.raises(BookingGuardFailed) as caught:
        await _request(db_session, renter, asset, clock, notifier)

    assert caught.value.code == "tier_insufficient"


async def test_request_on_blocked_window_refused(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    db_session.add(
        AvailabilityWindow(asset_id=asset.id, starts_at=END, ends_at=END, is_blocked=True)
    )
    await db_session.flush()

    with pytest.raises(BookingGuardFailed) as caught:
        await _request(db_session, renter, asset, clock, notifier)

    assert caught.value.code == "dates_blocked"


async def test_request_beside_blocked_window_ok(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    after = END + timedelta(days=1)
    db_session.add(
        AvailabilityWindow(asset_id=asset.id, starts_at=after, ends_at=after, is_blocked=True)
    )
    db_session.add(AvailabilityWindow(asset_id=asset.id, starts_at=START, ends_at=END))
    await db_session.flush()

    booking = await _request(db_session, renter, asset, clock, notifier)

    assert booking.status == BookingStatus.requested


async def test_request_overlapping_live_booking_409(db_session, clock, notifier) -> None:
    asset = await create_asset(db_session)
    first = await create_user(db_session, kyc_verified=True)
    second = await create_user(db_session, kyc_verified=True)
    await _request(db_session, first, asset, clock, notifier)

    with pytest.raises(BookingGuardFailed) as caught:
        await _request(
            db_session, second, asset, clock, notifier, starts_on=END, ends_on=END + timedelta(1)
        )

    assert caught.value.code == "dates_unavailable"
    # The SAVEPOINT kept the caller's transaction usable.
    count = len((await db_session.execute(select(Booking.id))).all())
    assert count == 1


async def test_self_booking_is_409(db_session, clock, notifier) -> None:
    vendor = await create_user(db_session, role=RenterRole.vendor, kyc_verified=True)
    asset = await create_asset(db_session, vendor=vendor)

    with pytest.raises(BookingGuardFailed) as caught:
        await _request(db_session, vendor, asset, clock, notifier)

    assert caught.value.code == "self_booking"
    assert caught.value.status_code == 409


async def test_deadlock_maps_to_dates_unavailable(db_session, clock, notifier, monkeypatch):
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)

    class DeadlockDetected(Exception):
        sqlstate = "40P01"

    async def deadlock(*args, **kwargs):
        raise DBAPIError("INSERT INTO bookings", {}, DeadlockDetected("deadlock detected"))

    monkeypatch.setattr(db_session, "flush", deadlock)

    with pytest.raises(BookingGuardFailed) as caught:
        await _request(db_session, renter, asset, clock, notifier)

    assert caught.value.code == "dates_unavailable"


async def test_other_insert_errors_propagate(db_session, clock, notifier, monkeypatch):
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)

    class Unexpected(Exception):
        sqlstate = "XX000"

    async def broken(*args, **kwargs):
        raise DBAPIError("INSERT INTO bookings", {}, Unexpected("boom"))

    monkeypatch.setattr(db_session, "flush", broken)

    with pytest.raises(DBAPIError):
        await _request(db_session, renter, asset, clock, notifier)


async def test_restricted_renter_cannot_request(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    renter.restricted_at = DEFAULT_NOW
    renter.restricted_reason = "No contact after rental start"
    await db_session.flush()
    asset = await create_asset(db_session)

    with pytest.raises(BookingGuardFailed) as caught:
        await _request(db_session, renter, asset, clock, notifier)

    assert caught.value.code == "renter_restricted"


async def test_request_in_unlaunched_city_404(db_session, clock, notifier, monkeypatch) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    settings = get_settings().model_copy(update={"launched_cities": ["lagos"]})

    with pytest.raises(ResourceNotFoundError):
        await _request(db_session, renter, asset, clock, notifier, settings=settings)


async def test_request_unknown_asset_404(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)

    with pytest.raises(ResourceNotFoundError):
        await _request(db_session, renter, asset, clock, notifier, asset_id=uuid.uuid4())


async def test_request_past_start_date_400(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    yesterday = DEFAULT_NOW.date() - timedelta(days=1)

    with pytest.raises(InvalidRequestError):
        await _request(db_session, renter, asset, clock, notifier, starts_on=yesterday, ends_on=END)


async def test_request_today_in_business_zone_ok(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    today = DEFAULT_NOW.date()

    booking = await _request(
        db_session, renter, asset, clock, notifier, starts_on=today, ends_on=today
    )

    assert booking.starts_on == today


async def test_request_reversed_dates_400(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)

    with pytest.raises(InvalidRequestError):
        await _request(db_session, renter, asset, clock, notifier, starts_on=END, ends_on=START)


async def test_request_delivery_not_offered_400(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session, fulfilment={"pickup": True, "delivery": False})

    with pytest.raises(InvalidRequestError):
        await _request(db_session, renter, asset, clock, notifier, fulfilment="delivery")


async def test_request_by_non_renter_actor_refused(db_session, clock, notifier) -> None:
    vendor = await create_user(db_session, role=RenterRole.vendor, kyc_verified=True)
    asset = await create_asset(db_session)

    with pytest.raises(InvalidTransition):
        await _request(db_session, vendor, asset, clock, notifier, renter=vendor_actor(vendor.id))


# ---------------------------------------------------------------------------
# accept / decline / approve_vendor / expire_hold
# ---------------------------------------------------------------------------


async def test_accept_sets_deposit_due(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    booking = await _request(db_session, renter, asset, clock, notifier)
    clock.advance(timedelta(hours=1))

    accepted = await _transition(
        db_session, booking, vendor_actor(asset.vendor_id), BookingEvent.accept, clock, notifier
    )

    assert accepted.status == BookingStatus.accepted
    assert accepted.deposit_due_at == clock.now() + timedelta(hours=12)
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.action == "booking.accepted"
    assert audit.before["status"] == "requested"
    assert audit.after["status"] == "accepted"
    assert audit.after["deposit_due_at"] == (clock.now() + timedelta(hours=12)).isoformat()
    assert notifier.calls[-1].user_id == renter.id
    assert notifier.calls[-1].template == "booking.accepted"


async def test_accept_at_exact_hold_expiry_is_409(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    booking = await _request(db_session, renter, asset, clock, notifier)
    clock.advance(timedelta(hours=4))

    with pytest.raises(BookingGuardFailed) as caught:
        await _transition(
            db_session, booking, vendor_actor(asset.vendor_id), BookingEvent.accept, clock, notifier
        )

    assert caught.value.code == "hold_expired"
    assert caught.value.status_code == 409
    await db_session.refresh(booking)
    assert booking.status == BookingStatus.requested
    assert len(await _audits(db_session, booking.id)) == 1


async def _accept_refused(session, booking, asset, clock, notifier) -> BookingGuardFailed:
    """Accept must fail a guard and write nothing."""
    audits = await session.scalar(select(func.count()).select_from(AuditEvent))
    notifier.calls.clear()

    with pytest.raises(BookingGuardFailed) as caught:
        await _transition(
            session, booking, vendor_actor(asset.vendor_id), BookingEvent.accept, clock, notifier
        )

    assert await session.scalar(select(func.count()).select_from(AuditEvent)) == audits
    await session.refresh(booking)
    assert booking.status == BookingStatus.requested
    assert booking.deposit_due_at is None
    assert notifier.calls == []
    return caught.value


@pytest.mark.parametrize("change", ["restricted", "inactive", "deleted"])
async def test_accept_refused_when_renter_restricted_after_request(
    db_session, clock, notifier, change
) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    booking = await _request(db_session, renter, asset, clock, notifier)
    if change == "restricted":
        renter.restricted_at = DEFAULT_NOW
        renter.restricted_reason = "at_risk"
    elif change == "inactive":
        renter.is_active = False
    else:
        renter.deleted_at = DEFAULT_NOW
    await db_session.flush()

    error = await _accept_refused(db_session, booking, asset, clock, notifier)

    assert error.code == "renter_restricted"


async def test_accept_refused_when_renter_tier_dropped(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session, minimum_trust_tier=1)
    booking = await _request(db_session, renter, asset, clock, notifier)
    profile = await db_session.scalar(select(KYCProfile).where(KYCProfile.user_id == renter.id))
    profile.status = KYCStatus.failed
    profile.tier = TrustTier.phone_verified
    profile.verified_at = None
    await db_session.flush()

    error = await _accept_refused(db_session, booking, asset, clock, notifier)

    assert error.code == "tier_insufficient"
    assert error.error_details == {"required": 1, "effective": 0}


async def test_band_c_accept_refused_when_tier_dropped_even_if_approved(
    db_session, clock, notifier
) -> None:
    renter = await _tier2_renter(db_session)
    asset = await create_asset(db_session, minimum_trust_tier=3, band="C")
    booking = await _request(db_session, renter, asset, clock, notifier)
    await _transition(
        db_session,
        booking,
        vendor_actor(asset.vendor_id),
        BookingEvent.approve_vendor,
        clock,
        notifier,
    )
    profile = await db_session.scalar(select(KYCProfile).where(KYCProfile.user_id == renter.id))
    profile.status = KYCStatus.failed
    profile.tier = TrustTier.phone_verified
    profile.verified_at = None
    await db_session.flush()

    error = await _accept_refused(db_session, booking, asset, clock, notifier)

    assert error.code == "tier_insufficient"


async def test_band_c_accept_without_vendor_approval_refused(db_session, clock, notifier) -> None:
    renter = await _tier2_renter(db_session)
    asset = await create_asset(db_session, minimum_trust_tier=3, band="C")
    booking = await _request(db_session, renter, asset, clock, notifier)

    with pytest.raises(BookingGuardFailed) as caught:
        await _transition(
            db_session, booking, vendor_actor(asset.vendor_id), BookingEvent.accept, clock, notifier
        )

    assert caught.value.code == "vendor_approval_required"


async def test_band_c_accept_after_approval_ok(db_session, clock, notifier) -> None:
    renter = await _tier2_renter(db_session)
    asset = await create_asset(db_session, minimum_trust_tier=3, band="C")
    booking = await _request(db_session, renter, asset, clock, notifier)
    vendor = vendor_actor(asset.vendor_id)

    approved = await _transition(
        db_session, booking, vendor, BookingEvent.approve_vendor, clock, notifier
    )
    assert approved.status == BookingStatus.requested
    assert approved.vendor_approved_at == clock.now()

    accepted = await _transition(db_session, booking, vendor, BookingEvent.accept, clock, notifier)
    assert accepted.status == BookingStatus.accepted

    actions = [audit.action for audit in await _audits(db_session, booking.id)]
    assert actions == ["booking.requested", "booking.vendor_approved", "booking.accepted"]


async def test_vendor_approve_twice_is_invalid(db_session, clock, notifier) -> None:
    renter = await _tier2_renter(db_session)
    asset = await create_asset(db_session, minimum_trust_tier=3, band="C")
    booking = await _request(db_session, renter, asset, clock, notifier)
    vendor = vendor_actor(asset.vendor_id)
    await _transition(db_session, booking, vendor, BookingEvent.approve_vendor, clock, notifier)

    with pytest.raises(InvalidTransition):
        await _transition(db_session, booking, vendor, BookingEvent.approve_vendor, clock, notifier)


async def test_decline_releases_slot(db_session, clock, notifier) -> None:
    asset = await create_asset(db_session)
    first = await create_user(db_session, kyc_verified=True)
    second = await create_user(db_session, kyc_verified=True)
    booking = await _request(db_session, first, asset, clock, notifier)

    declined = await _transition(
        db_session,
        booking,
        vendor_actor(asset.vendor_id),
        BookingEvent.decline,
        clock,
        notifier,
        payload={"reason": "Booked offline"},
    )

    assert declined.status == BookingStatus.declined
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.action == "booking.declined"
    assert audit.reason == "Booked offline"
    assert notifier.calls[-1].user_id == first.id
    assert notifier.calls[-1].template == "booking.declined"

    again = await _request(db_session, second, asset, clock, notifier)
    assert again.status == BookingStatus.requested


async def test_other_vendor_cannot_accept(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    booking = await _request(db_session, renter, asset, clock, notifier)
    stranger = await create_user(db_session, role=RenterRole.vendor, kyc_verified=True)
    sent = len(notifier.calls)

    with pytest.raises(InvalidTransition) as caught:
        await _transition(
            db_session, booking, vendor_actor(stranger.id), BookingEvent.accept, clock, notifier
        )

    assert caught.value.status_code == 409
    assert caught.value.code == "invalid_transition"
    assert caught.value.error_details == {"from": "requested", "event": "accept"}
    await db_session.refresh(booking)
    assert booking.status == BookingStatus.requested
    assert len(await _audits(db_session, booking.id)) == 1
    assert len(notifier.calls) == sent


async def test_renter_cannot_accept_own_booking(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    booking = await _request(db_session, renter, asset, clock, notifier)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session, booking, renter_actor(renter), BookingEvent.accept, clock, notifier
        )


async def test_renter_cannot_withdraw_request(db_session, clock, notifier) -> None:
    """Not in the spec 3.1 table (spec gap, R7): a requested booking has no cancel."""
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    booking = await _request(db_session, renter, asset, clock, notifier)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session, booking, renter_actor(renter), BookingEvent.cancel, clock, notifier
        )


async def test_accept_on_declined_booking_is_invalid(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    booking = await create_booking(
        db_session, asset=asset, renter=renter, status=BookingStatus.declined
    )

    with pytest.raises(InvalidTransition) as caught:
        await _transition(
            db_session, booking, vendor_actor(asset.vendor_id), BookingEvent.accept, clock, notifier
        )

    assert caught.value.error_details == {"from": "declined", "event": "accept"}


async def test_transition_unknown_booking_404(db_session, clock, notifier) -> None:
    missing = Booking(id=uuid.uuid4())

    with pytest.raises(ResourceNotFoundError):
        await _transition(
            db_session, missing, Actor.system(), BookingEvent.expire_hold, clock, notifier
        )


async def test_expire_hold_after_expiry(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    booking = await _request(db_session, renter, asset, clock, notifier)
    clock.advance(timedelta(hours=4, seconds=1))

    expired = await _transition(
        db_session, booking, Actor.system(), BookingEvent.expire_hold, clock, notifier
    )

    assert expired.status == BookingStatus.expired
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.action == "booking.expired"
    assert audit.actor_role == "system"
    assert audit.actor_id is None
    assert notifier.calls[-1].user_id == renter.id
    assert notifier.calls[-1].template == "booking.expired"


async def test_expire_hold_at_exact_expiry_is_invalid(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    booking = await _request(db_session, renter, asset, clock, notifier)
    clock.advance(timedelta(hours=4))

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session, booking, Actor.system(), BookingEvent.expire_hold, clock, notifier
        )


async def test_vendor_cannot_expire_hold(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    booking = await _request(db_session, renter, asset, clock, notifier)
    clock.advance(timedelta(hours=5))

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session,
            booking,
            vendor_actor(asset.vendor_id),
            BookingEvent.expire_hold,
            clock,
            notifier,
        )


@pytest.mark.parametrize(
    ("event", "actor_kind", "advance"),
    [
        (BookingEvent.accept, "vendor", timedelta(0)),
        (BookingEvent.decline, "vendor", timedelta(0)),
        (BookingEvent.approve_vendor, "vendor", timedelta(0)),
        (BookingEvent.expire_hold, "system", timedelta(hours=5)),
    ],
)
async def test_each_transition_writes_one_audit_row(
    db_session, clock, notifier, event, actor_kind, advance
) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    booking = await _request(db_session, renter, asset, clock, notifier)
    before = len(await _audits(db_session, booking.id))
    clock.advance(advance)
    actor = Actor.system() if actor_kind == "system" else vendor_actor(asset.vendor_id)

    result = await _transition(db_session, booking, actor, event, clock, notifier)

    audits = await _audits(db_session, booking.id)
    assert len(audits) == before + 1
    audit = audits[-1]
    assert audit.entity == "booking"
    assert audit.before["status"] == "requested"
    assert audit.after["status"] == result.status.value
