"""Root router for the versioned public API."""

from fastapi import APIRouter

from app.api.routes.assets import router as assets_router
from app.api.routes.bookings import router as bookings_router
from app.api.routes.health import router as health_router
from app.api.routes.home import router as home_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(assets_router)
api_router.include_router(bookings_router)
api_router.include_router(home_router)
