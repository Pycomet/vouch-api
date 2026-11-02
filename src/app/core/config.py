"""Validated application configuration loaded from the environment."""

from __future__ import annotations

import re
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_CITY_SLUG = re.compile(r"^[a-z][a-z-]*$")
# The ``.env.example`` value; refused outside development and test.
JWT_SECRET_PLACEHOLDER = "change-me"
# HS256 keys shorter than the hash output (32 bytes) are refused outside development and test.
JWT_SECRET_MIN_BYTES = 32


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


# Environments where the JWT secret rules are not enforced.
_UNGUARDED_ENVIRONMENTS = frozenset({Environment.DEVELOPMENT, Environment.TEST})


class Settings(BaseSettings):
    """Runtime settings shared by the API and background processes."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="VOUCH_",
        env_ignore_empty=True,
        extra="ignore",
    )

    app_name: str = "Vouch API"
    app_version: str = "0.1.0"
    environment: Environment = Environment.DEVELOPMENT
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/vouch"
    cors_origins: list[AnyHttpUrl] = Field(default_factory=list)
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    # Comma-separated in the environment (VOUCH_LAUNCHED_CITIES=abuja,lagos).
    launched_cities: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["abuja"])
    # Placeholders pending legal advice.
    commission_rate: Decimal = Decimal("0.15")
    damage_contribution_rate: Decimal = Decimal("0.05")
    # Renter cancellation of a paid booking (A13), also placeholders pending legal
    # advice: the fee is this fraction of the rental, and the vendor's share of the
    # fee is the second fraction (Vouch keeps the rest). Frozen into price_snapshot.
    renter_cancellation_fee_rate: Decimal = Decimal("0.10")
    cancellation_fee_vendor_share: Decimal = Decimal("0.50")
    business_timezone: str = "Africa/Lagos"
    # Booking request limits: the longest rental (days, inclusive) and how far
    # ahead (days after today in the business zone) a rental may start.
    max_rental_days: int = Field(default=90, ge=1)
    booking_horizon_days: int = Field(default=365, ge=1)
    # HS256 signing key for access tokens; checked outside development and test.
    jwt_secret: SecretStr = SecretStr(JWT_SECRET_PLACEHOLDER)
    jwt_ttl_minutes: int = Field(default=60 * 24 * 7, ge=1)
    # Where phone OTP codes go. Only the in-memory fake exists in M2 (R26).
    otp_sender: Literal["fake"] = "fake"

    @field_validator("launched_cities", mode="before")
    @classmethod
    def parse_launched_cities(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.split(",")
        if isinstance(value, list | tuple):
            return [str(item).strip().lower() for item in value]
        return value

    @field_validator("launched_cities")
    @classmethod
    def validate_launched_cities(cls, value: list[str]) -> list[str]:
        for city in value:
            if not _CITY_SLUG.fullmatch(city):
                raise ValueError(f"invalid city slug: {city!r}")
        return value

    @field_validator(
        "commission_rate",
        "damage_contribution_rate",
        "renter_cancellation_fee_rate",
        "cancellation_fee_vendor_share",
    )
    @classmethod
    def validate_rate(cls, value: Decimal) -> Decimal:
        if not Decimal(0) <= value < Decimal(1):
            raise ValueError("rate must satisfy 0 <= rate < 1")
        return value

    @field_validator("business_timezone")
    @classmethod
    def validate_business_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown business_timezone: {value!r}") from exc
        return value

    @model_validator(mode="after")
    def enforce_production_safety(self) -> Settings:
        if self.environment == Environment.PRODUCTION and self.debug:
            raise ValueError("debug must be disabled in production")
        # The signing key is checked everywhere except local development and tests,
        # so a staging deployment cannot run on the placeholder or a weak key either.
        if self.environment not in _UNGUARDED_ENVIRONMENTS:
            env = self.environment.value
            secret = self.jwt_secret.get_secret_value()
            if secret == JWT_SECRET_PLACEHOLDER:
                raise ValueError(f"jwt_secret must be set in {env}")
            if len(secret.strip().encode("utf-8")) < JWT_SECRET_MIN_BYTES:
                raise ValueError(
                    f"jwt_secret must be at least {JWT_SECRET_MIN_BYTES} bytes in {env}"
                )
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION

    @property
    def business_zone(self) -> ZoneInfo:
        return ZoneInfo(self.business_timezone)

    @property
    def cors_origin_values(self) -> list[str]:
        return [str(origin).rstrip("/") for origin in self.cors_origins]


@lru_cache
def get_settings() -> Settings:
    """Return one immutable-by-convention settings instance per process."""

    return Settings()
