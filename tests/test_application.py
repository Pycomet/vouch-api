"""Application factory and HTTP boundary tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import Request
from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.db import session as database_session
from app.db.session import get_session
from app.main import create_app


class DisposableEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class SessionContext:
    def __init__(self, session: Any) -> None:
        self.session = session
        self.exited = False

    async def __aenter__(self) -> Any:
        return self.session

    async def __aexit__(self, *_args) -> None:
        self.exited = True


class SessionFactory:
    def __init__(self, context: SessionContext) -> None:
        self.context = context
        self.calls = 0

    def __call__(self) -> SessionContext:
        self.calls += 1
        return self.context


def test_application_exposes_vouch_metadata(settings: Settings) -> None:
    application = create_app(settings)

    assert application.title == "Vouch Test API"
    assert application.version == "9.9.9"


def test_liveness_returns_branded_service_status(client: TestClient) -> None:
    response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "Vouch Test API",
        "version": "9.9.9",
    }
    assert response.headers["X-Request-ID"]


def test_cors_allows_only_configured_web_origin(client: TestClient) -> None:
    response = client.options(
        "/api/v1/health/live",
        headers={
            "Origin": "https://app.vouch.test",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://app.vouch.test"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_production_hides_interactive_api_documentation() -> None:
    settings = Settings(
        _env_file=None,
        environment=Environment.PRODUCTION,
        database_url="postgresql+asyncpg://postgres:postgres@localhost:5432/vouch",
    )

    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_lifespan_builds_and_cleans_up_injected_database(
    settings: Settings,
    monkeypatch,
) -> None:
    engine = DisposableEngine()
    engine_arguments: list[tuple[str, dict[str, Any]]] = []

    def create_engine(url: str, **options: Any) -> DisposableEngine:
        engine_arguments.append((url, options))
        return engine

    monkeypatch.setattr(database_session, "create_async_engine", create_engine)
    application = create_app(settings)

    assert engine_arguments == []

    with TestClient(application):
        assert engine_arguments == [
            (
                settings.database_url,
                {
                    "hide_parameters": True,
                    "pool_pre_ping": True,
                    "pool_size": settings.database_pool_size,
                    "max_overflow": settings.database_max_overflow,
                },
            )
        ]
        assert application.state.database_engine is engine
        assert application.state.sessionmaker.kw["bind"] is engine
        assert engine.disposed is False

    assert engine.disposed is True


async def test_get_session_uses_request_application_sessionmaker() -> None:
    expected_session = object()
    context = SessionContext(expected_session)
    sessionmaker = SessionFactory(context)
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(state=SimpleNamespace(sessionmaker=sessionmaker)),
        }
    )

    dependency = get_session(request)
    session = await anext(dependency)

    assert session is expected_session
    assert sessionmaker.calls == 1

    await dependency.aclose()
    assert context.exited is True
