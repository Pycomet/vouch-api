"""Configuration validation tests."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import Environment, Settings


def test_settings_use_vouch_defaults_without_env_file() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "Vouch API"
    assert settings.environment == Environment.DEVELOPMENT
    assert settings.api_v1_prefix == "/api/v1"
    assert settings.database_url.startswith("postgresql+asyncpg://")


def test_cors_origins_are_normalized() -> None:
    settings = Settings(_env_file=None, cors_origins=["https://app.vouch.test/"])

    assert settings.cors_origin_values == ["https://app.vouch.test"]


def test_production_rejects_debug_mode() -> None:
    with pytest.raises(ValidationError, match="debug must be disabled in production"):
        Settings(_env_file=None, environment=Environment.PRODUCTION, debug=True)


def test_pool_size_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_pool_size=0)


def test_launched_cities_parses_csv() -> None:
    settings = Settings(_env_file=None, launched_cities="abuja, Lagos")

    assert settings.launched_cities == ["abuja", "lagos"]


def test_launched_cities_parses_env_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOUCH_LAUNCHED_CITIES", "abuja,lagos")

    assert Settings(_env_file=None).launched_cities == ["abuja", "lagos"]


def test_launched_cities_accepts_list() -> None:
    settings = Settings(_env_file=None, launched_cities=["Abuja"])

    assert settings.launched_cities == ["abuja"]


def test_launched_cities_default_is_abuja() -> None:
    assert Settings(_env_file=None).launched_cities == ["abuja"]


@pytest.mark.parametrize("value", ["ab uja", "abuja,,lagos", "ab1", "-abuja"])
def test_launched_cities_rejects_bad_slug(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, launched_cities=value)


def test_rates_are_decimal() -> None:
    settings = Settings(_env_file=None)

    assert settings.commission_rate == Decimal("0.15")
    assert settings.damage_contribution_rate == Decimal("0.05")
    assert isinstance(settings.commission_rate, Decimal)
    assert isinstance(settings.damage_contribution_rate, Decimal)
    assert settings.business_timezone == "Africa/Lagos"


@pytest.mark.parametrize("value", ["-0.01", "1", "1.5"])
def test_rates_must_be_between_zero_and_one(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, commission_rate=Decimal(value))
    with pytest.raises(ValidationError):
        Settings(_env_file=None, damage_contribution_rate=Decimal(value))
