"""Spec 5: the full happy path through the HTTP API, with no network (R28, A14).

OTP login (renter and vendor) -> KYC (renter) -> request -> accept -> payment
webhook -> pickup handover (both sign) -> return (both sign) -> past the claim
window -> ``run_timers`` -> completed -> payout webhook.

Everything runs through the production session path (no ``get_session``
override): the app's lifespan opens its own engine on the migrated database and
every route really commits; the checks read the ledger on separate sessions. The
providers are the app's own fakes (``FakeOtpSender``, ``FakeKycProvider``,
``FakePaymentProvider``, chosen by ``Settings`` in the test environment), and the
clock is a ``FrozenClock``. ``socket.socket.connect`` is patched to refuse any
address that is not this machine, so a provider reaching out would fail the test.

The booking is delivered, so F > 0, and the renter takes the damage contribution,
so C > 0: D, C, R and F are all non-zero.
"""

from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select

from app.api.deps import get_clock, get_notifier
from app.core.clock import FrozenClock
from app.core.config import Environment, Settings
from app.db.models import Booking, LedgerAccount, LedgerEntry, Payment
from app.db.models.payment import PaymentStatus, PaymentType
from app.db.models.user import RenterRole
from app.main import create_app
from app.providers.kyc import FakeKycProvider
from app.providers.otp import FakeOtpSender
from app.providers.payment import FakePaymentProvider
from app.services.ledger import account_balance
from app.services.money import commission_amount
from app.services.notify import RecordingNotifier
from app.services.timers import run_timers
from tests.postgres.conftest import DATABASE_URL
from tests.postgres.factories import DEFAULT_NOW, FIRST_START, create_asset, create_user

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

D = Decimal
API = "/api/v1"
RENTER_PHONE = "+2348031110001"
VENDOR_PHONE = "+2348031110002"
DEPOSIT = D("150000.00")
DELIVERY_FEE = D("5000.00")
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
CHECKLIST = {"body": "no scratches", "battery": "2 of 2"}
EVIDENCE = ["https://cdn.example/handover/1.jpg"]


class NetworkBlocked(RuntimeError):
    """A connection to somewhere other than this machine was attempted."""


@pytest.fixture
def no_network(monkeypatch) -> list[object]:
    """Refuse every non-local ``connect``; returns the addresses that were attempted."""
    attempts: list[object] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(address: object) -> None:
        attempts.append(address)
        if isinstance(address, tuple) and address[0] not in LOCAL_HOSTS:
            raise NetworkBlocked(f"network access attempted: {address!r}")

    def connect(self, address):
        _check(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    return attempts


def test_the_guard_refuses_a_remote_address(no_network) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock, pytest.raises(NetworkBlocked):
        sock.connect(("203.0.113.10", 443))  # TEST-NET-3: refused before any packet


async def _balances(session_factory, booking_id: uuid.UUID) -> dict[LedgerAccount, Decimal]:
    async with session_factory() as session:
        return {
            account: await account_balance(session, account, booking_id=booking_id)
            for account in LedgerAccount
        }


async def _whole_ledger_balanced(session_factory) -> bool:
    async with session_factory() as session:
        rows = await session.execute(
            select(LedgerEntry.direction, func.coalesce(func.sum(LedgerEntry.amount), 0)).group_by(
                LedgerEntry.direction
            )
        )
        sums = {direction: D(total) for direction, total in rows.all()}
    return sums["dr"] == sums["cr"] and sums["dr"] > 0


async def _login(http: AsyncClient, sender: FakeOtpSender, phone: str, **extra) -> dict[str, str]:
    sent = await http.post(f"{API}/auth/otp/request", json={"phone": phone})
    assert sent.status_code == 202, sent.text
    code = sender.last_code(phone)
    verified = await http.post(
        f"{API}/auth/otp/verify", json={"phone": phone, "code": code, **extra}
    )
    assert verified.status_code == 200, verified.text
    return {"Authorization": f"Bearer {verified.json()['access_token']}"}


async def _sign(http, booking_id, step: str, headers) -> dict:
    response = await http.post(
        f"{API}/bookings/{booking_id}/{step}",
        json={"checklist": CHECKLIST, "evidence_urls": EVIDENCE, "sign": True},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _webhook(http, provider: FakePaymentProvider, body: bytes) -> dict:
    response = await http.post(
        f"{API}/webhooks/{provider.name}",
        content=body,
        headers={"Content-Type": "application/json", "X-Vouch-Signature": provider.sign(body)},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_full_happy_path_no_network(session_factory, no_network) -> None:
    assert DATABASE_URL is not None
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        database_url=DATABASE_URL,
        jwt_secret=SecretStr("test-secret-that-is-long-enough-for-hs256"),
        fake_payment_webhook_secret=SecretStr("fake-webhook-secret-long-enough-for-hmac"),
    )
    clock = FrozenClock(DEFAULT_NOW)
    notifier = RecordingNotifier()
    async with session_factory() as session:
        vendor = await create_user(
            session, role=RenterRole.vendor, kyc_verified=True, phone=VENDOR_PHONE
        )
        asset = await create_asset(
            session,
            vendor=vendor,
            minimum_trust_tier=1,
            deposit_amount=DEPOSIT,
            fulfilment={"pickup": True, "delivery": True, "delivery_fee": str(DELIVERY_FEE)},
        )
        await session.commit()
        asset_id = asset.id

    app = create_app(settings)
    app.dependency_overrides[get_clock] = lambda: clock
    app.dependency_overrides[get_notifier] = lambda: notifier
    sender: FakeOtpSender = app.state.otp_sender
    provider: FakePaymentProvider = app.state.payment_provider
    assert isinstance(sender, FakeOtpSender)
    assert isinstance(app.state.kyc_provider, FakeKycProvider)
    assert isinstance(provider, FakePaymentProvider)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        # Log in by phone: the renter is new, the vendor already exists.
        renter = await _login(http, sender, RENTER_PHONE, full_name="Happy Renter")
        vendor_headers = await _login(http, sender, VENDOR_PHONE)

        # KYC lifts the renter to tier 1, which this asset requires.
        kyc = await http.post(
            f"{API}/me/kyc",
            json={
                "id_type": "nin",
                "id_number": "12345678901",
                "selfie_b64": "U0VMRklF",
                "address_doc_b64": "QUREUkVTUw==",
            },
            headers=renter,
        )
        assert kyc.status_code == 200, kyc.text
        assert kyc.json() == {"tier": 1, "status": "verified"}

        # Request and accept.
        created = await http.post(
            f"{API}/bookings",
            json={
                "asset_id": str(asset_id),
                "starts_on": FIRST_START.isoformat(),
                "ends_on": (FIRST_START + timedelta(days=2)).isoformat(),
                "fulfilment": "delivery",
                "damage_contribution": True,
            },
            headers=renter,
        )
        assert created.status_code == 201, created.text
        booking_id = uuid.UUID(created.json()["id"])
        accepted = await http.post(f"{API}/bookings/{booking_id}/accept", headers=vendor_headers)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["status"] == "accepted"
        shown = (await http.get(f"{API}/bookings/{booking_id}", headers=renter)).json()
        instructions = shown["payment_instructions"]

        async with session_factory() as session:
            snapshot = (await session.get(Booking, booking_id)).price_snapshot
        d = D(snapshot["deposit"])
        c = D(snapshot["damage_contribution"])
        r = D(snapshot["rental_total"])
        f = D(snapshot["delivery_fee"])
        k = commission_amount(r, D(snapshot["commission_rate"]))
        assert (d, f) == (DEPOSIT, DELIVERY_FEE)
        assert c > 0 and r > 0 and k > 0
        assert D(instructions["amount"]) == d + c + r + f
        assert await _balances(session_factory, booking_id) == dict.fromkeys(
            LedgerAccount, D("0.00")
        )

        # The renter pays into the virtual account; the provider's webhook confirms.
        paid = await _webhook(
            http,
            provider,
            provider.make_event(
                "payment_captured",
                booking_id=booking_id,
                payment_reference=instructions["reference"],
                amount=D(instructions["amount"]),
            ),
        )
        assert paid == {"status": "processed", "processing_error": None}
        status = (await http.get(f"{API}/bookings/{booking_id}", headers=renter)).json()["status"]
        assert status == "confirmed"
        balances = await _balances(session_factory, booking_id)
        assert balances[LedgerAccount.renter_deposit_liability] == d
        assert balances[LedgerAccount.protection_pool] == c
        assert balances[LedgerAccount.vendor_payable] == r + f - k
        assert balances[LedgerAccount.platform_revenue] == k
        assert await _whole_ledger_balanced(session_factory)

        # Pickup on the first day: both sign, the rental starts.
        clock.advance(datetime.combine(FIRST_START, time(9), DEFAULT_NOW.tzinfo) - clock.now())
        await _sign(http, booking_id, "handover", vendor_headers)
        assert (await _sign(http, booking_id, "handover", renter))["status"] == "in_progress"
        assert (await _balances(session_factory, booking_id))[
            LedgerAccount.renter_deposit_liability
        ] == d
        assert await _whole_ledger_balanced(session_factory)

        # Return on the last day: both sign, the claim window opens.
        clock.advance(timedelta(days=2))
        await _sign(http, booking_id, "return", renter)
        assert (await _sign(http, booking_id, "return", vendor_headers))["status"] == "returned"
        assert await _whole_ledger_balanced(session_factory)

        # No claim: past the window, the timer completes the booking.
        clock.advance(timedelta(hours=24, minutes=1))
        report = await run_timers(
            session_factory, clock=clock, notifier=notifier, settings=settings
        )
        assert report.completed == 1 and report.failed == 0
        status = (await http.get(f"{API}/bookings/{booking_id}", headers=renter)).json()["status"]
        assert status == "completed"
        balances = await _balances(session_factory, booking_id)
        assert balances[LedgerAccount.renter_deposit_liability] == D("0.00")
        assert balances[LedgerAccount.protection_pool] == c
        assert balances[LedgerAccount.vendor_payable] == r + f - k
        assert balances[LedgerAccount.platform_revenue] == k
        assert await _whole_ledger_balanced(session_factory)

        # The vendor's payout lands.
        payout = await _webhook(
            http,
            provider,
            provider.make_event(
                "payout_completed",
                booking_id=booking_id,
                payment_reference=f"trf_{booking_id.hex}",
                amount=r + f - k,
                recipient_code="RCP_happy_vendor",
            ),
        )
        assert payout == {"status": "processed", "processing_error": None}

    balances = await _balances(session_factory, booking_id)
    assert balances[LedgerAccount.vendor_payable] == D("0.00")
    assert balances[LedgerAccount.renter_deposit_liability] == D("0.00")
    # What Vouch keeps: the commission and the protection pool.
    assert balances[LedgerAccount.platform_cash] == -(k + c)
    assert await _whole_ledger_balanced(session_factory)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Payment.type, Payment.status, Payment.amount)
                .where(Payment.booking_id == booking_id)
                .order_by(Payment.created_at, Payment.id)
            )
        ).all()
    assert rows == [
        (PaymentType.booking, PaymentStatus.captured, d + c + r + f),
        (PaymentType.refund, PaymentStatus.pending, d),
        (PaymentType.payout, PaymentStatus.captured, r + f - k),
    ]
    # The database was reached (locally), and nothing else was.
    assert no_network
    assert all(
        not isinstance(address, tuple) or address[0] in LOCAL_HOSTS for address in no_network
    )
