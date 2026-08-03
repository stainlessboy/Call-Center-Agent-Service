"""Exchange rates and the static "useful links" block."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.bot import links
from app.db.models import User
from app.miniapp.deps import get_db_user, user_lang
from app.utils.cbu_rates import fetch_cbu_rates

router = APIRouter()

# Sections that exist in the bot menu but are not implemented yet — the Mini App
# renders them with a "СКОРО" badge (screen 23).
COMING_SOON_RATES = [
    {
        "key": "corporate",
        "label": {"ru": "Курс юр.лиц", "en": "Corporate rates", "uz": "Yuridik shaxslar kursi"},
    },
    {
        "key": "online",
        "label": {"ru": "Курс онлайн-конверсии", "en": "Online conversion", "uz": "Onlayn konversiya"},
    },
    {
        "key": "atm",
        "label": {"ru": "Курс банкоматов", "en": "ATM rates", "uz": "Bankomatlar kursi"},
    },
]


def _to_float(raw) -> float | None:
    try:
        return float(str(raw).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


@router.get("/rates")
async def rates(top: int | None = None, user: User = Depends(get_db_user)) -> dict:
    lang = user_lang(user)
    items = await fetch_cbu_rates()
    serialized = [
        {
            "code": item["code"],
            "name": item.get(f"name_{lang}") or item.get("name_ru") or item["code"],
            "icon": item.get("icon") or "",
            "nominal": item.get("nominal") or "1",
            "rate": _to_float(item.get("rate")),
            "rate_text": str(item.get("rate") or "—"),
            "diff": _to_float(item.get("diff")) or 0.0,
            "date": item.get("date") or "",
        }
        for item in items
    ]
    if top:
        serialized = serialized[: max(1, top)]
    return {
        "items": serialized,
        "updated_at": serialized[0]["date"] if serialized else "",
        "coming_soon": [
            {"key": s["key"], "label": s["label"].get(lang) or s["label"]["ru"]}
            for s in COMING_SOON_RATES
        ],
    }


@router.get("/links")
async def useful_links(user: User = Depends(get_db_user)) -> dict:
    lang = user_lang(user)

    def label(table: dict, key: str) -> str:
        entry = table.get(key) or {}
        return entry.get(lang) or entry.get("ru") or key

    return {
        "apps": [
            {"key": "android", "label": label(links.APP_LABELS, "android"), "url": links.ANDROID_APP_URL},
            {"key": "ios", "label": label(links.APP_LABELS, "ios"), "url": links.IOS_APP_URL},
        ],
        "socials": [
            {"key": "telegram", "label": label(links.SOCIAL_LABELS, "telegram"), "url": links.TELEGRAM_CHANNEL_URL},
            {"key": "instagram", "label": label(links.SOCIAL_LABELS, "instagram"), "url": links.INSTAGRAM_URL},
            {"key": "facebook", "label": label(links.SOCIAL_LABELS, "facebook"), "url": links.FACEBOOK_URL},
            {"key": "website", "label": label(links.SOCIAL_LABELS, "website"), "url": links.WEBSITE_URL},
        ],
        "contacts": {
            "call_center": links.CALL_CENTER_PHONE,
            "trust_line": links.TRUST_LINE_PHONE,
        },
    }
