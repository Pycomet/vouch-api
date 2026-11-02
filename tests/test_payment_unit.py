"""PaymentProvider seam: the fake, the registry and the settings that pick one (no database)."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.core.config import Environment, Settings
from app.main import create_app
from app.providers.payment import (
    FakePaymentProvider,
    PaymentEvent,
    PaymentEventError,
    PaymentProvider,
    PaymentProviderError,
    VirtualAccount,
)
from app.providers.registry import build_payment_provider, payment_provider_named
from app.schemas.errors import ResourceNotFoundError

BOOKING_ID = uuid.UUID("1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f")
SECRET = "webhook-secret-for-tests"
REAL_SECRET = "a-real-production-secret-of-decent-length"
GUARDED = pytest.mark.parametrize(
    "environment", [Environment.STAGING, Environment.PRODUCTION], ids=["staging", "production"]
)


def _guarded(environment: Environment, **kwargs) -> Settings:
    return Settings(_env_file=None, environment=environment, jwt_secret=REAL_SECRET, **kwargs)


# ---------------------------------------------------------------------------
# The fake
# ---------------------------------------------------------------------------


async def test_fake_virtual_account_is_derived_from_the_booking_id() -> None:
    provider = FakePaymentProvider(SECRET)

    first = await provider.create_virtual_account(BOOKING_ID, Decimal("307500.00"), "booking")
    again = await FakePaymentProvider(SECRET).create_virtual_account(
        BOOKING_ID, Decimal("307500.00"), "booking"
    )
    other_id = uuid.uuid4()
    other = await provider.create_virtual_account(other_id, Decimal("10.00"), "booking")

    assert isinstance(first, VirtualAccount)
    assert first == again
    assert first.reference == f"fake-booking-{BOOKING_ID.hex}"
    assert first.account_number == f"{BOOKING_ID.int % 10**10:010d}"
    assert len(first.account_number) == 10 and first.account_number.isdigit()
    assert first.bank == FakePaymentProvider.bank
    assert other.reference != first.reference
    assert provider.accounts == [
        (BOOKING_ID, Decimal("307500.00"), "booking"),
        (other_id, Decimal("10.00"), "booking"),
    ]


async def test_fake_can_fail_to_open_an_account() -> None:
    provider = FakePaymentProvider(SECRET, fail_create=True)

    with pytest.raises(PaymentProviderError):
        await provider.create_virtual_account(BOOKING_ID, Decimal("1.00"), "booking")
    assert provider.accounts == []


async def test_fake_payout_is_recorded_and_can_fail() -> None:
    provider = FakePaymentProvider(SECRET)

    reference = await provider.initiate_payout("RCP_vendor", Decimal("127500.00"), "po-1")

    assert reference == "fake-payout-po-1"
    assert provider.payouts == [("RCP_vendor", Decimal("127500.00"), "po-1")]
    with pytest.raises(PaymentProviderError):
        await FakePaymentProvider(SECRET, fail_payout=True).initiate_payout(
            "RCP_vendor", Decimal("1.00"), "po-2"
        )


def test_fake_satisfies_the_protocol() -> None:
    assert isinstance(FakePaymentProvider(SECRET), PaymentProvider)
    assert FakePaymentProvider(SECRET).name == "fake"


def test_signature_is_hmac_sha256_hex_of_the_body() -> None:
    provider = FakePaymentProvider(SECRET)
    body = b'{"id":"evt_1"}'
    expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    assert provider.sign(body) == expected
    assert provider.verify_webhook(expected, body)
    assert not provider.verify_webhook(expected, body + b" ")
    assert not provider.verify_webhook("0" * 64, body)
    assert not provider.verify_webhook("", body)
    assert not provider.verify_webhook(None, body)
    assert not FakePaymentProvider("another-secret").verify_webhook(expected, body)


def test_make_event_round_trips_through_parse_event() -> None:
    provider = FakePaymentProvider(SECRET)

    body = provider.make_event(
        "payment_captured",
        booking_id=BOOKING_ID,
        payment_reference="fake-booking-abc",
        amount=Decimal("307500.00"),
    )
    event = provider.parse_event(body)

    assert event == PaymentEvent(
        provider_event_id="evt_payment_captured_fake-booking-abc",
        type="payment_captured",
        booking_id=BOOKING_ID,
        payment_reference="fake-booking-abc",
        amount=Decimal("307500.00"),
    )
    assert json.loads(body)["data"]["amount"] == "307500.00"  # money as a string


def test_make_event_takes_an_explicit_event_id() -> None:
    provider = FakePaymentProvider(SECRET)
    body = provider.make_event(
        "payout_completed",
        booking_id=BOOKING_ID,
        payment_reference="po-1",
        amount=Decimal("1.00"),
        provider_event_id="evt_42",
    )

    assert provider.parse_event(body).provider_event_id == "evt_42"
    assert provider.parse_event(body).type == "payout_completed"
    assert provider.parse_event(body).recipient_code is None


def test_a_payout_event_carries_its_recipient_code() -> None:
    provider = FakePaymentProvider(SECRET)
    body = provider.make_event(
        "payout_completed",
        booking_id=BOOKING_ID,
        payment_reference="po-1",
        amount=Decimal("1.00"),
        recipient_code="RCP_1",
    )

    assert provider.parse_event(body).recipient_code == "RCP_1"
    with pytest.raises(PaymentEventError):
        provider.parse_event(_body(recipient_code=""))


def test_parse_event_keeps_an_unknown_type_for_the_caller_to_refuse() -> None:
    provider = FakePaymentProvider(SECRET)
    body = provider.make_event(
        "charge.disputed", booking_id=BOOKING_ID, payment_reference="r", amount=Decimal("1.00")
    )

    assert provider.parse_event(body).type == "charge.disputed"


def _body(**data) -> bytes:
    event = {
        "id": "evt_1",
        "type": "payment_captured",
        "data": {"booking_id": str(BOOKING_ID), "reference": "r", "amount": "1.00"},
    }
    event["data"].update(data)
    return json.dumps(event).encode()


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"id": "evt_1"}',
        json.dumps({"id": "", "type": "payment_captured", "data": {}}).encode(),
        _body(booking_id="not-a-uuid"),
        _body(amount="1.001"),
        _body(amount="NaN"),
        _body(amount="Infinity"),
        _body(amount=True),
        _body(amount="ten"),
        _body(amount="-1.00"),
        _body(amount="0.00"),
        _body(amount="10000000000.00"),
        _body(reference=""),
        _body(reference=7),
    ],
    ids=[
        "not-json",
        "not-object",
        "no-type",
        "empty-id",
        "bad-booking",
        "three-dp",
        "nan",
        "infinity",
        "bool",
        "text",
        "negative",
        "zero",
        "too-large",
        "empty-reference",
        "number-reference",
    ],
)
def test_parse_event_refuses_a_malformed_body(body: bytes) -> None:
    with pytest.raises(PaymentEventError):
        FakePaymentProvider(SECRET).parse_event(body)


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_parse_event_refuses_json_constants(constant: bytes) -> None:
    body = _body().replace(b'"1.00"', constant)

    with pytest.raises(PaymentEventError):
        FakePaymentProvider(SECRET).parse_event(body)


def test_parse_event_reads_a_json_number_amount_exactly() -> None:
    body = (
        b'{"id":"evt_1","type":"payment_captured","data":{"booking_id":"'
        + str(BOOKING_ID).encode()
        + b'","reference":"r","amount":307500.10}}'
    )

    assert FakePaymentProvider(SECRET).parse_event(body).amount == Decimal("307500.10")


# ---------------------------------------------------------------------------
# Registry and settings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
def test_development_and_test_default_to_the_fake(environment) -> None:
    settings = Settings(_env_file=None, environment=environment)
    provider = build_payment_provider(settings)

    assert settings.effective_payment_provider == "fake"
    assert isinstance(provider, FakePaymentProvider)


def test_the_fake_signs_with_the_configured_secret() -> None:
    settings = Settings(_env_file=None, fake_payment_webhook_secret=SecretStr(SECRET))
    provider = build_payment_provider(settings)

    assert provider.sign(b"x") == FakePaymentProvider(SECRET).sign(b"x")


@GUARDED
def test_fake_payment_provider_refused_outside_development_and_test(environment) -> None:
    with pytest.raises(ValidationError, match="payment_provider 'fake' is not allowed"):
        _guarded(environment, payment_provider="fake", fake_payment_webhook_secret=REAL_SECRET)


@GUARDED
def test_no_payment_provider_outside_development_and_test(environment) -> None:
    settings = _guarded(environment)

    assert settings.effective_payment_provider is None
    assert build_payment_provider(settings) is None


@GUARDED
def test_fake_provider_with_the_placeholder_webhook_secret_is_refused(environment) -> None:
    with pytest.raises(ValidationError, match="fake_payment_webhook_secret must be set"):
        _guarded(environment, payment_provider="fake")


@GUARDED
@pytest.mark.parametrize("secret", ["", "short", "x" * 31])
def test_fake_provider_with_a_weak_webhook_secret_is_refused(environment, secret) -> None:
    with pytest.raises(ValidationError, match="fake_payment_webhook_secret must be at least"):
        _guarded(
            environment, payment_provider="fake", fake_payment_webhook_secret=SecretStr(secret)
        )


@GUARDED
def test_no_payment_provider_does_not_check_the_webhook_secret(environment) -> None:
    """The secret belongs to the fake; without it the placeholder is harmless (N2)."""
    settings = _guarded(environment)

    assert settings.payment_provider is None
    assert settings.fake_payment_webhook_secret.get_secret_value() == "change-me"


def test_payment_provider_setting_only_allows_fake() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, payment_provider="paystack")


def test_webhook_secret_is_not_shown_in_repr() -> None:
    settings = Settings(_env_file=None, fake_payment_webhook_secret=SecretStr(SECRET))

    assert SECRET not in repr(settings)


def test_payment_provider_named_returns_the_configured_provider() -> None:
    provider = FakePaymentProvider(SECRET)

    assert payment_provider_named("fake", provider) is provider


@pytest.mark.parametrize("name", ["paystack", "FAKE", ""])
def test_payment_provider_named_refuses_another_name(name) -> None:
    with pytest.raises(ResourceNotFoundError):
        payment_provider_named(name, FakePaymentProvider(SECRET))


def test_payment_provider_named_refuses_when_none_is_configured() -> None:
    with pytest.raises(ResourceNotFoundError):
        payment_provider_named("fake", None)


def test_app_state_carries_the_payment_provider() -> None:
    app = create_app(Settings(_env_file=None, environment=Environment.TEST))

    assert isinstance(app.state.payment_provider, FakePaymentProvider)


def test_app_without_a_payment_provider_starts() -> None:
    settings = _guarded(
        Environment.PRODUCTION,
        database_url="postgresql+asyncpg://postgres:postgres@localhost:5432/vouch",
    )
    with TestClient(create_app(settings)) as client:
        assert client.app.state.payment_provider is None
