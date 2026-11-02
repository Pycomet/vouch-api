"""Vouch API application factory and ASGI entry point."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, request_context_middleware
from app.db.session import create_database_engine, create_database_sessionmaker
from app.schemas.errors import ErrorDetail, ErrorEnvelope, VouchAPIError


def _error_response(request: Request, exc: VouchAPIError) -> JSONResponse:
    """Build a standard error envelope from a domain exception."""
    request_id = getattr(request.state, "request_id", "unknown")
    body = ErrorEnvelope(
        error=ErrorDetail(
            code=exc.code,
            message=exc.detail_message,
            details=exc.error_details,
            request_id=request_id,
        )
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=body.model_dump(),
        headers={**exc.headers, "X-Request-ID": request_id},
    )


def _json_safe(value: object) -> object:
    """Replace NaN/Infinity (not valid JSON) with their text, recursively."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return value


def _validation_response(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's 422 body, but safe to encode when the rejected input was NaN/Infinity.

    The default handler echoes ``input`` back, and a non-finite float there makes the
    JSON encoder raise, turning a 422 into a 500.
    """
    return JSONResponse(
        status_code=422, content={"detail": _json_safe(jsonable_encoder(exc.errors()))}
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create an independently configurable Vouch API instance."""

    runtime_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(runtime_settings.debug)
        app.state.settings = runtime_settings
        engine = create_database_engine(runtime_settings)
        app.state.database_engine = engine
        app.state.sessionmaker = create_database_sessionmaker(engine)
        try:
            yield
        finally:
            await engine.dispose()

    application = FastAPI(
        title=runtime_settings.app_name,
        version=runtime_settings.app_version,
        debug=runtime_settings.debug,
        docs_url=None if runtime_settings.is_production else "/docs",
        redoc_url=None if runtime_settings.is_production else "/redoc",
        openapi_url=None if runtime_settings.is_production else "/openapi.json",
        lifespan=lifespan,
    )
    application.state.settings = runtime_settings
    application.middleware("http")(request_context_middleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=runtime_settings.cors_origin_values,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    application.add_exception_handler(VouchAPIError, _error_response)  # type: ignore[arg-type]
    application.add_exception_handler(RequestValidationError, _validation_response)  # type: ignore[arg-type]
    application.include_router(api_router, prefix=runtime_settings.api_v1_prefix)
    return application


app = create_app()
