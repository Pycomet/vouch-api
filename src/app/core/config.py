"""Validated application configuration loaded from the environment."""

from __future__ import annotations

import re
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyHttpUrl, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_CITY_SLUG = re.compile(r"^[a-z][a-z-]*$")


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


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
    business_timezone: str = "Africa/Lagos"

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

    @field_validator("commission_rate", "damage_contribution_rate")
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
