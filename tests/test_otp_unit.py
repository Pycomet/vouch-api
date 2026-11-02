"""Phone OTP pieces that need no database (spec 4, R26)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.deps import get_otp_sender
from app.core.config import Settings
from app.db.models import OtpChallenge
from app.main import create_app
from app.providers.otp import (
    FakeOtpSender,
    LoggingOtpSender,
    OtpSender,
    build_otp_sender,
    mask_phone,
)
from app.schemas.auth import OtpRequest, OtpVerify
from app.schemas.errors import RateLimitedError
from app.services.auth import normalize_phone


@pytest.mark.parametrize(
    "raw", ["08031234567", "2348031234567", "+2348031234567"], ids=["local", "234", "plus234"]
)
def test_normalize_phone_accepts_the_three_nigerian_spellings(raw: str) -> None:
    assert normalize_phone(raw) == "+2348031234567"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "8031234567",
        "0803123456",
        "080312345678",
        "+234803123456",
        "23480312345678",
        "+448031234567",
        "0803 123 4567",
        "+2348031234567\n",
        "０８０３１２３４５６７",  # full-width
    ],
)
def test_normalize_phone_refuses_anything_else(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_phone(raw)


def test_normalize_phone_refuses_a_non_string() -> None:
    with pytest.raises(ValueError):
        normalize_phone(8031234567)  # type: ignore[arg-type]


def test_mask_phone_keeps_only_the_last_digits() -> None:
    assert mask_phone("+2348031234567") == "+234******4567"
    assert mask_phone("12345") == "*****"


def test_challenge_repr_hides_the_phone_and_hash() -> None:
    challenge = OtpChallenge(phone="+2348031234567", code_hash="secret-hash", attempts=2)

    assert "+234" not in repr(challenge) and "secret-hash" not in repr(challenge)
    assert "attempts=2" in repr(challenge)


def test_app_has_one_otp_sender_from_its_settings(settings: Settings) -> None:
    app = create_app(settings)
    request = SimpleNamespace(app=app)

    sender = get_otp_sender(request)  # type: ignore[arg-type]

    assert isinstance(sender, FakeOtpSender)
    assert get_otp_sender(request) is sender  # type: ignore[arg-type]


def test_request_schema_normalizes() -> None:
    assert OtpRequest(phone="08031234567").phone == "+2348031234567"


def test_verify_schema_refuses_extra_fields() -> None:
    with pytest.raises(ValidationError):
        OtpVerify.model_validate({"phone": "08031234567", "code": "123456", "role": "admin"})


def test_verify_schema_strips_and_bounds_full_name() -> None:
    body = OtpVerify(phone="08031234567", code="123456", full_name="  Ada Obi  ")
    assert body.full_name == "Ada Obi"
    with pytest.raises(ValidationError):
        OtpVerify(phone="08031234567", code="123456", full_name="   ")
    with pytest.raises(ValidationError):
        OtpVerify(phone="08031234567", code="123456", full_name="x" * 201)


async def test_fake_sender_records_codes() -> None:
    sender = FakeOtpSender()
    await sender.send("+2348031234567", "123456")
    await sender.send("+2348031234567", "654321")

    assert sender.sent == [("+2348031234567", "123456"), ("+2348031234567", "654321")]
    assert sender.last_code("+2348031234567") == "654321"
    with pytest.raises(LookupError):
        sender.last_code("+2348000000000")


@pytest.mark.parametrize("sender", [FakeOtpSender(), LoggingOtpSender()], ids=["fake", "logging"])
async def test_senders_never_log_the_code_or_the_full_phone(sender, vouch_caplog) -> None:
    await sender.send("+2348031234567", "987654")

    assert "otp sent" in vouch_caplog.text
    assert "4567" in vouch_caplog.text
    assert "987654" not in vouch_caplog.text
    assert "+2348031234567" not in vouch_caplog.text


@pytest.mark.parametrize("sender", [FakeOtpSender(), LoggingOtpSender()], ids=["fake", "logging"])
def test_senders_satisfy_the_protocol(sender) -> None:
    assert isinstance(sender, OtpSender)


def test_build_otp_sender_from_settings() -> None:
    assert isinstance(build_otp_sender(Settings(_env_file=None)), FakeOtpSender)


def test_otp_sender_setting_only_allows_fake() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, otp_sender="sms-gateway")


def test_rate_limited_error_carries_retry_after() -> None:
    error = RateLimitedError(retry_after=90)

    assert (error.status_code, error.code) == (429, "rate_limited")
    assert dict(error.headers) == {"Retry-After": "90"}
    assert dict(RateLimitedError.headers) == {}
    with pytest.raises(TypeError):
        error.headers["X-Leak"] = "1"  # type: ignore[index]


def test_rate_limited_error_retry_after_is_at_least_one_second() -> None:
    assert RateLimitedError(retry_after=0).headers["Retry-After"] == "1"
