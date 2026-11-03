"""Liveness and dependency-aware readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.api.examples import LIVE_EXAMPLE, NOT_READY_EXAMPLE, READY_EXAMPLE, documented
from app.db.health import database_is_ready
from app.schemas.health import ReadinessStatus, ServiceStatus

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=ServiceStatus, responses=documented(200, LIVE_EXAMPLE))
async def liveness(request: Request) -> ServiceStatus:
    """Report that the API process is running."""

    settings = request.app.state.settings
    return ServiceStatus(status="ok", service=settings.app_name, version=settings.app_version)


@router.get(
    "/ready",
    response_model=ReadinessStatus,
    responses={
        **documented(200, READY_EXAMPLE),
        503: {
            "model": ReadinessStatus,
            "description": "A dependency is unavailable.",
            "content": {"application/json": {"example": NOT_READY_EXAMPLE}},
        },
    },
)
async def readiness(request: Request, response: Response) -> ReadinessStatus:
    """Report whether dependencies required to serve traffic are available."""

    settings = request.app.state.settings
    database_ready = await database_is_ready(request.app.state.database_engine)
    if not database_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    dependency_status = "ok" if database_ready else "unavailable"
    return ReadinessStatus(
        status=dependency_status,
        service=settings.app_name,
        version=settings.app_version,
        checks={"database": dependency_status},
    )
