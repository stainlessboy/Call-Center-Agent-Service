"""Dashboard custom view for SQLAdmin."""
from __future__ import annotations

import json

from sqladmin import BaseView, expose
from starlette.requests import Request

from app.admin.dashboard_data import get_full_dashboard
from app.db.session import AsyncSessionLocal


class DashboardAdmin(BaseView):
    name = "Дашборд"
    icon = "fa-solid fa-chart-line"

    @expose("/dashboard", methods=["GET"])
    async def dashboard_page(self, request: Request):
        async with AsyncSessionLocal() as session:
            data = await get_full_dashboard(session)

        return await self.templates.TemplateResponse(
            request,
            "dashboard.html",
            context={"data": data, "data_json": json.dumps(data, default=str)},
        )
