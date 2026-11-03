"""Shared fixtures for isolated Vouch API tests."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_name="Vouch Test API",
        app_version="9.9.9",
        environment=Environment.TEST,
        database_url="postgresql+asyncpg://postgres:postgres@localhost:5432/vouch_test",
        cors_origins=["https://app.vouch.test"],
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def vouch_caplog(caplog, monkeypatch):
    """``caplog`` at INFO that also sees the project's ``vouch.*`` loggers.

    Alembic's ``fileConfig`` (run by the migration fixtures) disables every logger
    that exists at that moment, so re-enable the project loggers for this test.
    """
    for name, logger in list(logging.root.manager.loggerDict.items()):
        if name.startswith("vouch") and isinstance(logger, logging.Logger):
            monkeypatch.setattr(logger, "disabled", False)
    caplog.set_level(logging.INFO)
    return caplog
