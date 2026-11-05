"""The ``phase0`` environment (M3a amendment A20).

Phase 0 runs on the fake OTP sender, KYC provider and payment provider, with the
production safety rules on: strong secrets, no debug, no interactive API docs.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.core.config import Environment, Settings
from app.main import create_app

STRONG = SecretStr("a-real-phase0-secret-of-decent-length-123")
WEBHOOK = SecretStr("a-real-webhook-secret-of-decent-length-45")


def _phase0(**overrides) -> Settings:
    values = {
        "environment": Environment.PHASE0,
        "jwt_secret": STRONG,
        "fake_payment_webhook_secret": WEBHOOK,
        "otp_sender": "fake",
        "kyc_provider": "fake",
        "payment_provider": "fake",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_phase0_with_fakes_and_strong_secrets_builds() -> None:
    settings = _phase0()

    assert settings.environment == Environment.PHASE0
    assert settings.effective_otp_sender == "fake"
    assert settings.effective_kyc_provider == "fake"
    assert settings.effective_payment_provider == "fake"
    assert settings.serves_api_docs is False


def test_phase0_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOUCH_ENVIRONMENT", "phase0")
    monkeypatch.setenv("VOUCH_JWT_SECRET", STRONG.get_secret_value())
    monkeypatch.setenv("VOUCH_FAKE_PAYMENT_WEBHOOK_SECRET", WEBHOOK.get_secret_value())

    assert Settings(_env_file=None).environment == Environment.PHASE0


def test_phase0_defaults_unset_providers_to_the_fakes() -> None:
    settings = _phase0(otp_sender=None, kyc_provider=None, payment_provider=None)

    assert settings.effective_otp_sender == "fake"
    assert settings.effective_kyc_provider == "fake"
    assert settings.effective_payment_provider == "fake"


@pytest.mark.parametrize("secret", ["change-me", "short", "x" * 31])
def test_phase0_refuses_a_weak_jwt_secret(secret: str) -> None:
    with pytest.raises(ValidationError, match="jwt_secret"):
        _phase0(jwt_secret=SecretStr(secret))


@pytest.mark.parametrize("secret", ["change-me", "x" * 31])
@pytest.mark.parametrize("provider", ["fake", None])
def test_phase0_refuses_a_weak_fake_webhook_secret(secret: str, provider) -> None:
    with pytest.raises(ValidationError, match="fake_payment_webhook_secret"):
        _phase0(fake_payment_webhook_secret=SecretStr(secret), payment_provider=provider)


def test_phase0_refuses_debug() -> None:
    with pytest.raises(ValidationError, match="debug must be disabled in phase0"):
        _phase0(debug=True)


def test_phase0_serves_no_interactive_api_docs() -> None:
    with TestClient(create_app(_phase0())) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


@pytest.mark.parametrize("field", ["otp_sender", "kyc_provider", "payment_provider"])
def test_production_still_refuses_each_fake(field: str) -> None:
    with pytest.raises(ValidationError, match=f"{field} 'fake' is not allowed in production"):
        Settings(
            _env_file=None,
            environment=Environment.PRODUCTION,
            jwt_secret=STRONG,
            fake_payment_webhook_secret=WEBHOOK,
            **{field: "fake"},
        )


@pytest.mark.parametrize(
    ("environment", "serves"),
    [
        (Environment.DEVELOPMENT, True),
        (Environment.TEST, True),
        (Environment.STAGING, True),
        (Environment.PRODUCTION, False),
    ],
)
def test_serves_api_docs_by_environment(environment: Environment, serves: bool) -> None:
    settings = Settings(_env_file=None, environment=environment, jwt_secret=STRONG)
    assert settings.serves_api_docs is serves
