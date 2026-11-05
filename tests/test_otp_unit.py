"""Phone OTP pieces that need no database (spec 4, R26)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.deps import get_otp_sender
from app.core.config import Environment, Settings
from app.db.models import OtpChallenge
from app.main import create_app
from app.providers.otp import (
    FAKE_SENT_LIMIT,
    FakeOtpSender,
    LoggingOtpSender,
    OtpSender,
    build_otp_sender,
    mask_phone,
)
from app.schemas.auth import OtpRequest, OtpVerify
from app.schemas.errors import RateLimitedError
from app.services import auth as auth_service
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
        "+2340803123456",
        "2340803123456",
        "00803123456",
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


async def test_fake_sender_keeps_only_the_last_codes() -> None:
    sender = FakeOtpSender()
    for n in range(FAKE_SENT_LIMIT + 5):
        await sender.send("+2348031234567", f"{n:06d}")

    assert len(sender.sent) == FAKE_SENT_LIMIT
    assert sender.sent[0] == ("+2348031234567", "000005")
    assert sender.last_code("+2348031234567") == f"{FAKE_SENT_LIMIT + 4:06d}"


GUARDED = pytest.mark.parametrize(
    "environment", [Environment.STAGING, Environment.PRODUCTION], ids=["staging", "production"]
)
REAL_SECRET = "a-real-production-secret-of-decent-length"


@GUARDED
def test_fake_otp_sender_refused_outside_development_and_test(environment) -> None:
    with pytest.raises(ValidationError, match="otp_sender 'fake' is not allowed"):
        Settings(_env_file=None, environment=environment, jwt_secret=REAL_SECRET, otp_sender="fake")


@GUARDED
def test_no_otp_sender_outside_development_and_test(environment) -> None:
    settings = Settings(_env_file=None, environment=environment, jwt_secret=REAL_SECRET)

    assert settings.effective_otp_sender is None
    assert build_otp_sender(settings) is None


@pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
def test_development_and_test_default_to_the_fake(environment) -> None:
    settings = Settings(_env_file=None, environment=environment)

    assert settings.effective_otp_sender == "fake"
    assert isinstance(build_otp_sender(settings), FakeOtpSender)


def test_an_explicit_fake_sender_is_allowed_in_development() -> None:
    assert Settings(_env_file=None, otp_sender="fake").effective_otp_sender == "fake"


def test_otp_request_is_503_without_a_sender() -> None:
    settings = Settings(
        _env_file=None,
        environment=Environment.PRODUCTION,
        database_url="postgresql+asyncpg://postgres:postgres@localhost:5432/vouch",
        jwt_secret=REAL_SECRET,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/auth/otp/request", json={"phone": "08031234567"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "otp_unavailable"


# Fixed development code (VOUCH_FAKE_OTP_CODE) for local clients and e2e tests
# ---------------------------------------------------------------------------


@GUARDED
def test_fake_otp_code_refused_outside_development_and_test(environment) -> None:
    with pytest.raises(ValidationError, match="fake_otp_code is not allowed"):
        Settings(
            _env_file=None, environment=environment, jwt_secret=REAL_SECRET, fake_otp_code="000000"
        )


@pytest.mark.parametrize("code", ["", "12345", "1234567", "12a456", " 123456"])
def test_fake_otp_code_must_be_six_digits(code: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, fake_otp_code=code)


@pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
def test_fake_otp_code_replaces_the_random_code_with_the_fake_sender(environment) -> None:
    settings = Settings(_env_file=None, environment=environment, fake_otp_code="000000")

    assert auth_service._new_code(settings) == "000000"


def test_codes_stay_random_without_a_fixed_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_service.secrets, "randbelow", lambda _: 42)

    assert auth_service._new_code(Settings(_env_file=None)) == "000042"
