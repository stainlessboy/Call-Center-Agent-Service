"""Hardcoded outbound links and phone numbers shown in the «Useful links» menu.

Centralised so the bot handlers stay clean. URL values are placeholders for the
real Asakabank assets — replace them in-place when the marketing team
provides updated targets. Localised LABEL fields are what's shown to the user;
URL fields stay language-agnostic.
"""
from __future__ import annotations


# ── Mobile app ──────────────────────────────────────────────────────────────
ANDROID_APP_URL = "https://lnnk.in/dMdU"
IOS_APP_URL = "https://lnnk.in/jhbr"

# ── Social media ────────────────────────────────────────────────────────────
INSTAGRAM_URL = "https://www.instagram.com/asakabankofficial/"
TELEGRAM_CHANNEL_URL = "https://t.me/Asakabank_official"
FACEBOOK_URL = "https://www.facebook.com/asakabankofficial"
WEBSITE_URL = "https://asakabank.uz/uz/physical-persons/home"

# ── Contacts ────────────────────────────────────────────────────────────────
TRUST_LINE_PHONE = "+998 71 200 55 22"
CALL_CENTER_PHONE = "1152"


# ── Localised labels ────────────────────────────────────────────────────────
APP_LABELS: dict[str, dict[str, str]] = {
    "android": {
        "ru": "🤖 Android",
        "en": "🤖 Android",
        "uz": "🤖 Android",
    },
    "ios": {
        "ru": "🍎 iOS",
        "en": "🍎 iOS",
        "uz": "🍎 iOS",
    },
}

SOCIAL_LABELS: dict[str, dict[str, str]] = {
    "instagram": {
        "ru": "📷 Instagram",
        "en": "📷 Instagram",
        "uz": "📷 Instagram",
    },
    "telegram": {
        "ru": "✈️ Telegram-канал",
        "en": "✈️ Telegram channel",
        "uz": "✈️ Telegram-kanal",
    },
    "facebook": {
        "ru": "👍 Facebook",
        "en": "👍 Facebook",
        "uz": "👍 Facebook",
    },
    "website": {
        "ru": "🌐 Веб-сайт",
        "en": "🌐 Website",
        "uz": "🌐 Veb-sayt",
    },
}

CONTACTS_HEADER: dict[str, str] = {
    "ru": "☎️ Контакты для связи и жалоб:",
    "en": "☎️ Contacts for inquiries and complaints:",
    "uz": "☎️ Aloqa va shikoyatlar uchun kontaktlar:",
}

CONTACTS_BODY: dict[str, str] = {
    "ru": (
        f"🛡 Линия доверия: <b>{TRUST_LINE_PHONE}</b>\n"
        f"📞 Колл-центр: <b>{CALL_CENTER_PHONE}</b>"
    ),
    "en": (
        f"🛡 Trust line: <b>{TRUST_LINE_PHONE}</b>\n"
        f"📞 Call center: <b>{CALL_CENTER_PHONE}</b>"
    ),
    "uz": (
        f"🛡 Ishonch telefoni: <b>{TRUST_LINE_PHONE}</b>\n"
        f"📞 Koll-markaz: <b>{CALL_CENTER_PHONE}</b>"
    ),
}

APP_HEADER: dict[str, str] = {
    "ru": "📱 Скачайте мобильное приложение Asakabank:",
    "en": "📱 Download the Asakabank mobile app:",
    "uz": "📱 Asakabank mobil ilovasini yuklab oling:",
}

SOCIAL_HEADER: dict[str, str] = {
    "ru": "🌍 Официальные соцсети Asakabank:",
    "en": "🌍 Asakabank official social media:",
    "uz": "🌍 Asakabankning rasmiy ijtimoiy tarmoqlari:",
}
