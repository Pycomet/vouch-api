"""Root router for the versioned public API."""

from fastapi import APIRouter

from app.api.routes.assets import router as assets_router
from app.api.routes.auth import router as auth_router
from app.api.routes.bookings import router as bookings_router
from app.api.routes.catalogue import router as catalogue_router
from app.api.routes.health import router as health_router
from app.api.routes.home import router as home_router
from app.api.routes.kyc import router as kyc_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(assets_router)
api_router.include_router(bookings_router)
api_router.include_router(home_router)
api_router.include_router(kyc_router)
api_router.include_router(catalogue_router)
