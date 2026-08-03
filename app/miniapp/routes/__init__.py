"""Mini App API router — mounted at /api/miniapp by app/api/fastapi_app.py."""
from __future__ import annotations

from fastapi import APIRouter

from app.miniapp.routes import bootstrap, branches, calc, catalog, chat, leads, misc, sessions

router = APIRouter(prefix="/api/miniapp", tags=["miniapp"])
router.include_router(bootstrap.router)
router.include_router(catalog.router)
router.include_router(calc.router)
router.include_router(leads.router)
router.include_router(branches.router)
router.include_router(misc.router)
router.include_router(sessions.router)
router.include_router(chat.router)

__all__ = ["router"]
