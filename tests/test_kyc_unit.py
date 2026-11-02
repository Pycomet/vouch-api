"""KYC provider selection and the fake's outcomes (no database)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Environment, Settings
from app.providers.kyc import FakeKycProvider, KycProvider, build_kyc_provider

REAL_SECRET = "a-real-production-secret-of-decent-length"
GUARDED = pytest.mark.parametrize(
    "environment", [Environment.STAGING, Environment.PRODUCTION], ids=["staging", "production"]
)


@GUARDED
def test_fake_kyc_provider_refused_outside_development_and_test(environment) -> None:
    with pytest.raises(ValidationError, match="kyc_provider 'fake' is not allowed"):
        Settings(
            _env_file=None, environment=environment, jwt_secret=REAL_SECRET, kyc_provider="fake"
        )


@GUARDED
def test_no_kyc_provider_outside_development_and_test(environment) -> None:
    settings = Settings(
        _env_file=None,
        environment=environment,
        jwt_secret=REAL_SECRET,
        fake_payment_webhook_secret=REAL_SECRET,
    )

    assert build_kyc_provider(settings) is None


@pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
def test_development_and_test_default_to_the_fake(environment) -> None:
    provider = build_kyc_provider(Settings(_env_file=None, environment=environment))

    assert isinstance(provider, FakeKycProvider)
    assert isinstance(provider, KycProvider)


def test_kyc_provider_setting_only_allows_fake() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, kyc_provider="smile-id")


async def test_fake_outcomes_are_configurable_and_references_unique() -> None:
    provider = FakeKycProvider(identity=False)

    first = await provider.verify_identity("nin", "12345678901", b"x")
    second = await provider.verify_identity("nin", "12345678901", b"x")
    address = await provider.verify_address(b"y")

    assert not first.passed and first.reasons == ["identity_mismatch"]
    assert address.passed and address.reasons == []
    assert first.reference != second.reference
    assert provider.identity_calls == 2
