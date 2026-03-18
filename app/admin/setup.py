from __future__ import annotations

from fastapi import FastAPI
from sqladmin import Admin

from app.admin.auth import AdminAuth
from app.admin.views import (
    BranchAdmin,
    CardProductOfferAdmin,
    ChatSessionAdmin,
    CreditProductOfferAdmin,
    DepositProductOfferAdmin,
    FaqItemAdmin,
    LeadAdmin,
    MessageAdmin,
    UserAdmin,
)
from app.config import get_settings
from app.db.session import engine


def setup_admin(app: FastAPI) -> Admin:
    settings = get_settings()
    authentication_backend = AdminAuth(secret_key=settings.admin_secret_key)

    admin = Admin(
        app,
        engine,
        authentication_backend=authentication_backend,
        title="Bank Bot CRM",
        base_url="/admin",
    )

    admin.add_view(UserAdmin)
    admin.add_view(ChatSessionAdmin)
    admin.add_view(MessageAdmin)
    admin.add_view(LeadAdmin)
    admin.add_view(CreditProductOfferAdmin)
    admin.add_view(DepositProductOfferAdmin)
    admin.add_view(CardProductOfferAdmin)
    admin.add_view(FaqItemAdmin)
    admin.add_view(BranchAdmin)

    return admin
