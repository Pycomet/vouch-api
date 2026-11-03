"""Live PostgreSQL tests for the home screens (spec 3.3, Task 4.3).

``GET /me/home`` (renter) and ``GET /vendor/home`` (vendor) run in-process on the
test's ``db_session`` exactly as in ``test_bookings_api.py``: ``get_session`` is
overridden, so nothing is committed for real and no rows leak. The clock is a
``FrozenClock`` at ``DEFAULT_NOW`` (2026-11-02 09:00 UTC, 10:00 in Africa/Lagos),
so "today" is 2026-11-02.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_clock, get_notifier
from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.models import Booking, LedgerAccount
from app.db.models.asset import ListingStatus
from app.db.models.audit import ActorRole
from app.db.models.booking import BookingStatus
from app.db.models.handover import HandoverDirection
from app.db.models.ledger import Direction
from app.db.models.payment import PaymentType
from app.db.models.user import RenterRole
from app.db.session import get_session
from app.main import create_app
from app.services.audit import Actor
from app.services.booking import BookingEvent, transition
from app.services.ledger import Posting, post_txn
from app.services.notify import RecordingNotifier
from tests.helpers.auth import bearer
from tests.postgres.factories import (
    DEFAULT_NOW,
    create_asset,
    create_booking,
    create_handover,
    create_payment,
    create_user,
    money_snapshot,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

D = Decimal
SETTINGS = get_settings().model_copy(
    update={"jwt_secret": SecretStr("test-secret-that-is-long-enough-for-hs256")}
)
TODAY = date(2026, 11, 2)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(DEFAULT_NOW)


@pytest.fixture
async def client(db_session: AsyncSession, clock: FrozenClock) -> AsyncIterator[AsyncClient]:
    app = create_app(SETTINGS)

    async def session_override() -> AsyncIterator[AsyncSession]:
        await db_session.commit()
        try:
            yield db_session
        finally:
            await db_session.rollback()

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_clock] = lambda: clock
    app.dependency_overrides[get_notifier] = RecordingNotifier
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def auth(user_id: uuid.UUID, role: ActorRole) -> dict[str, str]:
    return bearer(user_id, role, SETTINGS)


async def _vendor(session: AsyncSession):
    return await create_user(session, role=RenterRole.vendor, kyc_verified=True)


async def _renter(session: AsyncSession):
    return await create_user(session, kyc_verified=True)


async def _book(session, asset, renter, status, start, end=None, **kwargs):
    return await create_booking(
        session,
        asset=asset,
        renter=renter,
        status=status,
        starts_on=start,
        ends_on=end or (start + timedelta(days=2) if start else None),
        price_snapshot=money_snapshot(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# GET /vendor/home
# ---------------------------------------------------------------------------


async def test_vendor_home_requests_ordered_by_hold_expiry(client, db_session) -> None:
    vendor = await _vendor(db_session)
    renter = await _renter(db_session)
    holds = [DEFAULT_NOW + timedelta(hours=h) for h in (3, 1, 2)]
    ids = []
    for index, hold in enumerate(holds):
        asset = await create_asset(db_session, vendor=vendor)
        booking = await _book(
            db_session, asset, renter, BookingStatus.requested, TODAY + timedelta(days=10 + index)
        )
        booking.hold_expires_at = hold
        ids.append(str(booking.id))
    # Not a request any more, and another vendor's request: neither is listed.
    accepted_asset = await create_asset(db_session, vendor=vendor)
    await _book(db_session, accepted_asset, renter, BookingStatus.accepted, TODAY)
    await _book(db_session, await create_asset(db_session), renter, BookingStatus.requested, TODAY)
    await db_session.flush()

    response = await client.get("/api/v1/vendor/home", headers=auth(vendor.id, ActorRole.vendor))

    assert response.status_code == 200
    requests = response.json()["requests"]
    assert [card["id"] for card in requests] == [ids[1], ids[2], ids[0]]
    assert requests[0]["hold_expires_at"].startswith("2026-11-02T10:00:00")
    assert requests[0]["status"] == "requested"
    assert requests[0]["money_line"]["next"]["label"] == "Awaiting vendor response"


async def test_vendor_home_requests_skip_lapsed_holds(client, db_session) -> None:
    """A request whose hold has already lapsed (not yet swept by the timer) is gone."""
    vendor = await _vendor(db_session)
    renter = await _renter(db_session)
    lapsed = await _book(
        db_session, await create_asset(db_session, vendor=vendor), renter,
        BookingStatus.requested, TODAY + timedelta(10),
    )  # fmt: skip
    lapsed.hold_expires_at = DEFAULT_NOW  # lapses exactly now
    still_open = await _book(
        db_session, await create_asset(db_session, vendor=vendor), renter,
        BookingStatus.requested, TODAY + timedelta(11),
    )  # fmt: skip
    still_open.hold_expires_at = DEFAULT_NOW + timedelta(minutes=1)
    await db_session.flush()
    open_id = str(still_open.id)

    response = await client.get("/api/v1/vendor/home", headers=auth(vendor.id, ActorRole.vendor))

    assert [card["id"] for card in response.json()["requests"]] == [open_id]


async def test_vendor_home_today_lists(client, db_session) -> None:
    vendor = await _vendor(db_session)
    renter = await _renter(db_session)

    async def asset():
        return await create_asset(db_session, vendor=vendor, name="Abuja studio kit")

    handover = await _book(db_session, await asset(), renter, BookingStatus.confirmed, TODAY)
    give_back = await _book(
        db_session, await asset(), renter, BookingStatus.in_progress, TODAY - timedelta(2), TODAY
    )
    # Distractors: tomorrow's handover and return, an unpaid booking starting today,
    # and another vendor's handover today.
    await _book(db_session, await asset(), renter, BookingStatus.confirmed, TODAY + timedelta(1))
    await _book(
        db_session, await asset(), renter, BookingStatus.in_progress, TODAY, TODAY + timedelta(1)
    )
    await _book(db_session, await asset(), renter, BookingStatus.accepted, TODAY)
    await _book(db_session, await create_asset(db_session), renter, BookingStatus.confirmed, TODAY)
    handover_id, return_id = str(handover.id), str(give_back.id)

    response = await client.get("/api/v1/vendor/home", headers=auth(vendor.id, ActorRole.vendor))

    assert response.status_code == 200
    today = response.json()["today"]
    assert [card["id"] for card in today["handovers"]] == [handover_id]
    assert [card["id"] for card in today["returns"]] == [return_id]
    assert today["handovers"][0]["asset_name"] == "Abuja studio kit"
    assert today["returns"][0]["ends_on"] == TODAY.isoformat()


async def test_vendor_home_today_follows_the_business_calendar(client, db_session, clock) -> None:
    """At 23:30 UTC on 1 Nov it is already 2 Nov in Africa/Lagos."""
    clock.advance(timedelta(hours=-9, minutes=-30))
    vendor = await _vendor(db_session)
    renter = await _renter(db_session)
    asset = await create_asset(db_session, vendor=vendor)
    handover_id = str((await _book(db_session, asset, renter, BookingStatus.confirmed, TODAY)).id)

    response = await client.get("/api/v1/vendor/home", headers=auth(vendor.id, ActorRole.vendor))

    assert [card["id"] for card in response.json()["today"]["handovers"]] == [handover_id]


async def test_vendor_home_utilisation(client, db_session) -> None:
    vendor = await _vendor(db_session)
    renter = await _renter(db_session)
    first = await create_asset(db_session, vendor=vendor)
    second = await create_asset(db_session, vendor=vendor)
    draft = await create_asset(db_session, vendor=vendor, listing_status=ListingStatus.draft)
    # Window: 2 Nov .. 8 Nov. Confirmed 1-3 Nov counts 2 days (2, 3).
    await _book(db_session, first, renter, BookingStatus.confirmed, date(2026, 11, 1))
    # Completed 7-12 Nov counts 2 days (7, 8).
    await _book(
        db_session, second, renter, BookingStatus.completed, date(2026, 11, 7), date(2026, 11, 12)
    )
    # Not counted: a cancelled booking, and a booking of a listing that is not active.
    await _book(db_session, first, renter, BookingStatus.cancelled_by_renter, date(2026, 11, 4))
    await _book(db_session, draft, renter, BookingStatus.confirmed, TODAY, date(2026, 11, 8))

    response = await client.get("/api/v1/vendor/home", headers=auth(vendor.id, ActorRole.vendor))

    # 4 booked asset-days / (2 active listings x 7 days) = 0.2857...
    assert response.json()["utilisation_7d"] == "0.29"


async def test_vendor_home_utilisation_without_listings_is_zero(client, db_session) -> None:
    vendor = await _vendor(db_session)

    response = await client.get("/api/v1/vendor/home", headers=auth(vendor.id, ActorRole.vendor))

    assert response.status_code == 200
    assert response.json() == {
        "requests": [],
        "today": {"handovers": [], "returns": []},
        "next_payout": "0.00",
        "utilisation_7d": "0.00",
    }


async def test_vendor_home_next_payout(client, db_session) -> None:
    vendor = await _vendor(db_session)
    renter = await _renter(db_session)
    done = await _book(
        db_session, await create_asset(db_session, vendor=vendor), renter,
        BookingStatus.completed, date(2026, 10, 20),
    )  # fmt: skip
    live = await _book(
        db_session, await create_asset(db_session, vendor=vendor), renter,
        BookingStatus.confirmed, TODAY,
    )  # fmt: skip
    other = await _book(
        db_session, await create_asset(db_session), renter, BookingStatus.completed,
        date(2026, 10, 20),
    )  # fmt: skip

    async def payable(booking, amount: str, direction: Direction = Direction.cr) -> None:
        cash = Direction.dr if direction == Direction.cr else Direction.cr
        await post_txn(
            db_session,
            postings=[
                Posting(LedgerAccount.platform_cash, cash, D(amount)),
                Posting(LedgerAccount.vendor_payable, direction, D(amount)),
            ],
            booking_id=booking.id,
            memo="test",
        )

    await payable(done, "127500.00")
    await payable(done, "27500.00", Direction.dr)  # already paid out
    await payable(live, "50000.00")  # not completed yet
    await payable(other, "90000.00")  # another vendor's

    response = await client.get("/api/v1/vendor/home", headers=auth(vendor.id, ActorRole.vendor))

    assert response.json()["next_payout"] == "100000.00"


async def _move(session, booking_id, actor, event, at, payload=None) -> None:
    await transition(
        session, booking_id, actor, event, payload,
        clock=FrozenClock(at), notifier=RecordingNotifier(), settings=get_settings(),
    )  # fmt: skip


async def _paid(session, asset, renter) -> uuid.UUID:
    """A booking paid in full through the booking service (R 150000, K 22500, D 150000)."""
    booking = await _book(session, asset, renter, BookingStatus.accepted, None)
    total = D("300000.00")  # D 150000 + C 0 + R 150000 + F 0
    payment = await create_payment(session, booking, type=PaymentType.booking, amount=total)
    await _move(
        session, booking.id, Actor.system(), BookingEvent.payment_captured, DEFAULT_NOW,
        {"amount": total, "payment_id": payment.id},
    )  # fmt: skip
    return booking.id


async def _handed_over(session, asset, renter) -> Booking:
    booking_id = await _paid(session, asset, renter)
    booking = await session.get(Booking, booking_id)
    await create_handover(session, booking, direction=HandoverDirection.pickup)
    await _move(session, booking_id, Actor.system(), BookingEvent.handover_completed, DEFAULT_NOW)
    return booking


async def test_vendor_home_next_payout_counts_cancellation_fee_share(client, db_session) -> None:
    vendor = await _vendor(db_session)
    renter = await _renter(db_session)
    done = await _handed_over(db_session, await create_asset(db_session, vendor=vendor), renter)
    await create_handover(db_session, done, direction=HandoverDirection.return_)
    await _move(db_session, done.id, Actor.system(), BookingEvent.return_completed, DEFAULT_NOW)
    after_window = done.claim_window_ends_at + timedelta(minutes=1)
    await _move(db_session, done.id, Actor.system(), BookingEvent.complete, after_window)
    cancelled = await _paid(db_session, await create_asset(db_session, vendor=vendor), renter)
    await _move(
        db_session, cancelled, Actor(renter.id, ActorRole.renter), BookingEvent.cancel,
        DEFAULT_NOW,
    )  # fmt: skip
    # A paid booking that is still live is not owed yet.
    await _paid(db_session, await create_asset(db_session, vendor=vendor), renter)

    response = await client.get("/api/v1/vendor/home", headers=auth(vendor.id, ActorRole.vendor))

    # Completed: R + F - K = 150000 - 22500 = 127500. Cancelled: the vendor's half
    # of the 10% cancellation fee, 7500.
    assert response.json()["next_payout"] == "135000.00"


async def test_vendor_home_next_payout_counts_lost_forfeit(client, db_session) -> None:
    vendor = await _vendor(db_session)
    renter = await _renter(db_session)
    admin = await create_user(db_session, role=RenterRole.admin)
    booking = await _handed_over(db_session, await create_asset(db_session, vendor=vendor), renter)
    lost_at = datetime.combine(booking.ends_on + timedelta(days=8), time(12), tzinfo=UTC)
    await _move(
        db_session, booking.id, Actor(admin.id, ActorRole.admin), BookingEvent.mark_lost,
        lost_at, {"reason": "No contact since the return date"},
    )  # fmt: skip

    response = await client.get("/api/v1/vendor/home", headers=auth(vendor.id, ActorRole.vendor))

    # R + F - K (127500) plus the forfeited deposit (150000).
    assert response.json()["next_payout"] == "277500.00"


async def test_vendor_home_refuses_a_renter(client, db_session) -> None:
    renter = await _renter(db_session)

    response = await client.get("/api/v1/vendor/home", headers=auth(renter.id, ActorRole.renter))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_home_needs_a_token(client) -> None:
    for path in ("/api/v1/vendor/home", "/api/v1/me/home"):
        response = await client.get(path)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# GET /me/home
# ---------------------------------------------------------------------------


async def test_me_home_active_and_past(client, db_session) -> None:
    renter = await _renter(db_session)

    async def asset():
        return await create_asset(db_session, make="Sony", model=f"FX3 {uuid.uuid4().hex[:6]}")

    later = await _book(
        db_session, await asset(), renter, BookingStatus.confirmed, TODAY + timedelta(9)
    )
    soonest = await _book(
        db_session, await asset(), renter, BookingStatus.requested, TODAY + timedelta(3)
    )
    old = await _book(db_session, await asset(), renter, BookingStatus.completed, date(2026, 9, 1))
    newer = await _book(
        db_session, await asset(), renter, BookingStatus.declined, date(2026, 10, 1)
    )
    # Not the caller's: never listed. A disputed booking starting after the soonest
    # live one is not the active card, and it is never in ``past``.
    await _book(
        db_session, await asset(), await _renter(db_session), BookingStatus.confirmed, TODAY
    )
    disputed = await _book(
        db_session, await asset(), renter, BookingStatus.disputed, TODAY + timedelta(20)
    )
    ids = {
        name: str(b.id)
        for name, b in (
            ("later", later), ("soonest", soonest), ("old", old), ("newer", newer),
            ("disputed", disputed),
        )
    }  # fmt: skip

    response = await client.get("/api/v1/me/home", headers=auth(renter.id, ActorRole.renter))

    assert response.status_code == 200
    body = response.json()
    assert body["active"]["id"] == ids["soonest"]
    assert body["active"]["asset_name"].startswith("Sony FX3 ")
    assert body["active"]["money_line"]["deposit"] == {
        "amount": "150000.00",
        "status": "due",
        "held_by": "platform",
    }
    assert [card["id"] for card in body["past"]] == [ids["newer"], ids["old"]]
    assert ids["later"] not in {card["id"] for card in body["past"]}
    assert ids["disputed"] not in {card["id"] for card in body["past"]}


async def test_me_home_disputed_booking_is_the_active_card(client, db_session) -> None:
    """A disputed booking stays on the renter's home while it is under review."""
    renter = await _renter(db_session)
    disputed = await _book(
        db_session, await create_asset(db_session), renter, BookingStatus.disputed,
        date(2026, 10, 10),
    )  # fmt: skip
    await _book(
        db_session, await create_asset(db_session), renter, BookingStatus.confirmed,
        TODAY + timedelta(5),
    )  # fmt: skip
    disputed_id = str(disputed.id)

    response = await client.get("/api/v1/me/home", headers=auth(renter.id, ActorRole.renter))

    body = response.json()
    assert body["active"]["id"] == disputed_id
    assert body["active"]["status"] == "disputed"
    assert body["active"]["money_line"]["next"]["label"] == "Under review"
    assert body["past"] == []


async def test_me_home_past_is_capped_at_twenty(client, db_session) -> None:
    renter = await _renter(db_session)
    asset = await create_asset(db_session)
    for week in range(22):
        await _book(
            db_session,
            asset,
            renter,
            BookingStatus.completed,
            date(2026, 1, 1) + timedelta(7 * week),
        )

    response = await client.get("/api/v1/me/home", headers=auth(renter.id, ActorRole.renter))

    body = response.json()
    assert body["active"] is None
    assert len(body["past"]) == 20
    assert body["past"][0]["starts_on"] == (date(2026, 1, 1) + timedelta(7 * 21)).isoformat()


async def test_me_home_refuses_a_vendor(client, db_session) -> None:
    vendor = await _vendor(db_session)

    response = await client.get("/api/v1/me/home", headers=auth(vendor.id, ActorRole.vendor))

    assert response.status_code == 403
