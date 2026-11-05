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
    # Phase 0 (M3a A20): hand-brokered rentals on the fake providers, with the
    # production safety rules on (strong secrets, no debug, no interactive docs).
    PHASE0 = "phase0"


# Environments where the secret rules are not enforced.
_UNGUARDED_ENVIRONMENTS = frozenset({Environment.DEVELOPMENT, Environment.TEST})
# Environments where the fake providers are allowed, and used when the setting is unset.
_FAKE_PROVIDER_ENVIRONMENTS = frozenset(
    {Environment.DEVELOPMENT, Environment.TEST, Environment.PHASE0}
)
# Environments that refuse debug mode and do not serve /docs, /redoc or /openapi.json.
_LOCKED_DOWN_ENVIRONMENTS = frozenset({Environment.PRODUCTION, Environment.PHASE0})


def _check_secret(field: str, value: SecretStr, env: str) -> None:
    """Refuse the placeholder or a key shorter than :data:`JWT_SECRET_MIN_BYTES`."""
    secret = value.get_secret_value()
    if secret == JWT_SECRET_PLACEHOLDER:
        raise ValueError(f"{field} must be set in {env}")
    if len(secret.strip().encode("utf-8")) < JWT_SECRET_MIN_BYTES:
        raise ValueError(f"{field} must be at least {JWT_SECRET_MIN_BYTES} bytes in {env}")


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
    # Largest accepted request body (413 ``payload_too_large`` above it).
    max_request_bytes: int = Field(default=8 * 1024 * 1024, ge=1024 * 1024)
    # HS256 signing key for access tokens; checked outside development and test.
    jwt_secret: SecretStr = SecretStr(JWT_SECRET_PLACEHOLDER)
    jwt_ttl_minutes: int = Field(default=60 * 24 * 7, ge=1)
    # Where phone OTP codes go. Only the in-memory fake exists in M2 (R26), and it
    # is refused in staging and production; unset there means "no sender" (the
    # OTP request route answers 503). Unset in development, test and phase0 (A20)
    # means the fake.
    otp_sender: Literal["fake"] | None = None
    # Which KYC provider verifies renters (R27); same rules as ``otp_sender``.
    kyc_provider: Literal["fake"] | None = None
    # Which payment provider opens virtual accounts and sends webhooks (R21, R28);
    # same rules as ``otp_sender``. Unset in staging and production means no
    # provider: accepting a booking then answers 503 ``payment_unavailable``.
    payment_provider: Literal["fake"] | None = None
    # HMAC-SHA256 key the fake provider signs webhook bodies with (R24). Outside
    # development and test it is checked like ``jwt_secret``, but only when the
    # fake payment provider is in use (the only provider that uses it).
    fake_payment_webhook_secret: SecretStr = SecretStr(JWT_SECRET_PLACEHOLDER)

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
        env = self.environment.value
        if self.environment in _LOCKED_DOWN_ENVIRONMENTS and self.debug:
            raise ValueError(f"debug must be disabled in {env}")
        # The signing key is checked everywhere except local development and tests,
        # so a staging or phase0 deployment cannot run on the placeholder or a weak key.
        if self.environment not in _UNGUARDED_ENVIRONMENTS:
            _check_secret("jwt_secret", self.jwt_secret, env)
        if self.environment == Environment.PHASE0:
            # The fakes are allowed (and the default) here, so the fake provider's
            # webhook key is checked whenever the fake payment provider is in use.
            if self.effective_payment_provider == "fake":
                _check_secret("fake_payment_webhook_secret", self.fake_payment_webhook_secret, env)
        elif self.environment not in _UNGUARDED_ENVIRONMENTS:
            if self.otp_sender == "fake":
                raise ValueError(f"otp_sender 'fake' is not allowed in {env}")
            if self.kyc_provider == "fake":
                raise ValueError(f"kyc_provider 'fake' is not allowed in {env}")
            if self.payment_provider == "fake":
                # The secret is the fake's own, so it is checked only when the fake
                # is selected (and the fake itself is then refused here anyway).
                _check_secret("fake_payment_webhook_secret", self.fake_payment_webhook_secret, env)
                raise ValueError(f"payment_provider 'fake' is not allowed in {env}")
        return self

    @property
    def effective_otp_sender(self) -> str | None:
        """The OTP sender to build: the setting, or the fake in development, test and phase0."""
        if self.otp_sender is not None:
            return self.otp_sender
        return "fake" if self.environment in _FAKE_PROVIDER_ENVIRONMENTS else None

    @property
    def effective_kyc_provider(self) -> str | None:
        """The KYC provider to build: the setting, or the fake in development, test and phase0."""
        if self.kyc_provider is not None:
            return self.kyc_provider
        return "fake" if self.environment in _FAKE_PROVIDER_ENVIRONMENTS else None

    @property
    def effective_payment_provider(self) -> str | None:
        """The payment provider to build: the setting, or the fake (development, test, phase0)."""
        if self.payment_provider is not None:
            return self.payment_provider
        return "fake" if self.environment in _FAKE_PROVIDER_ENVIRONMENTS else None

    @property
    def serves_api_docs(self) -> bool:
        """Whether /docs, /redoc and /openapi.json are served (not in production or phase0)."""
        return self.environment not in _LOCKED_DOWN_ENVIRONMENTS

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
