"""Asynchronous PostgreSQL engine and session lifecycle."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_database_engine(settings: Settings) -> AsyncEngine:
    """Build a new async engine without opening a connection eagerly.

    ``hide_parameters`` keeps bound values (phones, names, amounts) out of the
    text of database errors, which reaches logs and tracebacks.
    """

    return create_async_engine(
        settings.database_url,
        hide_parameters=True,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )


def create_database_sessionmaker(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Build the session factory bound to an application engine."""

    return async_sessionmaker(bind=engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session per request."""

    async with request.app.state.sessionmaker() as session:
        yield session
