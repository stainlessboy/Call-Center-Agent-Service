from __future__ import annotations

from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request

from app.config import get_settings


class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        username = form.get("username", "")
        password = form.get("password", "")

        settings = get_settings()
        if username == settings.admin_username and password == settings.admin_password:
            request.session.update({"authenticated": True})
            return True
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        return request.session.get("authenticated", False)
