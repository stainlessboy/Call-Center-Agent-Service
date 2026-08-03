"""Serving the built Mini App SPA from FastAPI.

In development the frontend runs on the Vite dev server and this mount does
nothing (no build directory yet). After ``npm run build`` the bundle lands in
``frontend/dist`` and is served at ``/app`` with an index.html fallback so
client-side navigation survives a reload.
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

MINIAPP_MOUNT_PATH = "/app"


def _dist_dir() -> str:
    override = (os.getenv("MINIAPP_DIST_DIR") or "").strip()
    if override:
        return override
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(project_root, "frontend", "dist")


class SPAStaticFiles(StaticFiles):
    """StaticFiles that falls back to index.html for unknown paths."""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            index = os.path.join(self.directory, "index.html")  # type: ignore[arg-type]
            if os.path.exists(index):
                return FileResponse(index)
            raise


def mount_miniapp_static(app: FastAPI) -> bool:
    """Mount the built SPA if present. Returns whether it was mounted."""
    dist = _dist_dir()
    if not os.path.isdir(dist) or not os.path.exists(os.path.join(dist, "index.html")):
        logger.info(
            "Mini App bundle not found at %s — serve it with `npm run dev` during development",
            dist,
        )
        return False
    app.mount(MINIAPP_MOUNT_PATH, SPAStaticFiles(directory=dist, html=True), name="miniapp")
    logger.info("Mini App mounted at %s from %s", MINIAPP_MOUNT_PATH, dist)
    return True
