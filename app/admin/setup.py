from __future__ import annotations

import os

from fastapi import FastAPI
from sqladmin import Admin

from app.admin.auth import AdminAuth
from app.admin.dashboard_view import DashboardAdmin
from app.admin.seed_view import SeedAdmin
from app.admin.views import (
    CardProductOfferAdmin,
    ChatSessionAdmin,
    CreditProductOfferAdmin,
    DepositProductOfferAdmin,
    FaqItemAdmin,
    FilialAdmin,
    LeadAdmin,
    MessageAdmin,
    SalesOfficeAdmin,
    SalesPointAdmin,
    UserAdmin,
)
from app.config import get_settings
from app.db.session import engine


def setup_admin(app: FastAPI) -> Admin:
    settings = get_settings()
    authentication_backend = AdminAuth(secret_key=settings.admin_secret_key)

    _base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _templates_dir = os.path.join(_base_dir, "templates")

    admin = Admin(
        app,
        engine,
        authentication_backend=authentication_backend,
        title="Bank Bot CRM",
        base_url="/admin",
        templates_dir=_templates_dir,
    )

    admin.add_base_view(DashboardAdmin)
    admin.add_base_view(SeedAdmin)
    admin.add_view(UserAdmin)
    admin.add_view(ChatSessionAdmin)
    admin.add_view(MessageAdmin)
    admin.add_view(LeadAdmin)
    admin.add_view(CreditProductOfferAdmin)
    admin.add_view(DepositProductOfferAdmin)
    admin.add_view(CardProductOfferAdmin)
    admin.add_view(FaqItemAdmin)
    admin.add_view(FilialAdmin)
    admin.add_view(SalesOfficeAdmin)
    admin.add_view(SalesPointAdmin)

    return admin
