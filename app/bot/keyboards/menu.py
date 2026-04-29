"""Reply keyboards for the bot.

Layout (2026-04 redesign):

    main_menu_keyboard (idle, no active dialog)
        💬 Начать диалог
        🏢 Наши отделения  │  💱 Курс валют
        🔗 Полезные ссылки │  ⚙️ Настройки

    chat_keyboard (during an active dialog — same nav, just swap top button)
        ✅ Завершить диалог
        🏢 Наши отделения  │  💱 Курс валют
        🔗 Полезные ссылки │  ⚙️ Настройки

Submenus are reply keyboards (1 level deep). Drill-downs deeper than that
(e.g. specific office → details, app store links) live as inline keyboards
in app/bot/handlers/commands.py.
"""
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.bot.i18n import menu_label

# ── Top-level actions ───────────────────────────────────────────────────────
START_DIALOG = "start_dialog"
END_DIALOG = "end_dialog"
OUR_BRANCHES = "our_branches"
CURRENCY_RATES = "currency_rates"
USEFUL_LINKS = "useful_links"
SETTINGS = "settings"

# ── Branches submenu ────────────────────────────────────────────────────────
OFFICE_LIST = "office_list"
NEAREST_BRANCH = "nearest_branch"
ELECTRONIC_QUEUE = "electronic_queue"
WEEKEND_DAYS = "weekend_days"

# ── Rates submenu ───────────────────────────────────────────────────────────
RATES_INDIVIDUALS = "rates_individuals"
RATES_CORPORATE = "rates_corporate"
RATES_ONLINE = "rates_online"
RATES_ATM = "rates_atm"

# ── Useful links submenu ────────────────────────────────────────────────────
MOBILE_APP_LINK = "mobile_app_link"
SOCIAL_LINKS = "social_links"
CONTACTS_COMPLAINTS = "contacts_complaints"

# ── Settings submenu ────────────────────────────────────────────────────────
CHANGE_LANGUAGE = "change_language"
CHANGE_PHONE = "change_phone"
MY_SESSIONS = "my_sessions"  # repurposed: shows current session history (read-only)

# ── Other ───────────────────────────────────────────────────────────────────
BACK = "back"


def _section_rows(lang: str | None) -> list[list[KeyboardButton]]:
    """Common 2x2 grid shown at the bottom of both idle/chat keyboards."""
    return [
        [
            KeyboardButton(text=menu_label(OUR_BRANCHES, lang)),
            KeyboardButton(text=menu_label(CURRENCY_RATES, lang)),
        ],
        [
            KeyboardButton(text=menu_label(USEFUL_LINKS, lang)),
            KeyboardButton(text=menu_label(SETTINGS, lang)),
        ],
    ]


def main_menu_keyboard(lang: str | None = None) -> ReplyKeyboardMarkup:
    """Idle keyboard (no active dialog) — top button starts a dialog."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=menu_label(START_DIALOG, lang))],
            *_section_rows(lang),
        ],
        resize_keyboard=True,
    )


def chat_keyboard(lang: str | None = None) -> ReplyKeyboardMarkup:
    """In-dialog keyboard — top button ends the dialog."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=menu_label(END_DIALOG, lang))],
            *_section_rows(lang),
        ],
        resize_keyboard=True,
    )


def branches_submenu_keyboard(lang: str | None = None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=menu_label(OFFICE_LIST, lang))],
            [KeyboardButton(text=menu_label(NEAREST_BRANCH, lang))],
            [KeyboardButton(text=menu_label(ELECTRONIC_QUEUE, lang))],
            [KeyboardButton(text=menu_label(WEEKEND_DAYS, lang))],
            [KeyboardButton(text=menu_label(BACK, lang))],
        ],
        resize_keyboard=True,
    )


def rates_submenu_keyboard(lang: str | None = None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=menu_label(RATES_INDIVIDUALS, lang))],
            [KeyboardButton(text=menu_label(RATES_CORPORATE, lang))],
            [KeyboardButton(text=menu_label(RATES_ONLINE, lang))],
            [KeyboardButton(text=menu_label(RATES_ATM, lang))],
            [KeyboardButton(text=menu_label(BACK, lang))],
        ],
        resize_keyboard=True,
    )


def links_submenu_keyboard(lang: str | None = None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=menu_label(MOBILE_APP_LINK, lang))],
            [KeyboardButton(text=menu_label(SOCIAL_LINKS, lang))],
            [KeyboardButton(text=menu_label(CONTACTS_COMPLAINTS, lang))],
            [KeyboardButton(text=menu_label(BACK, lang))],
        ],
        resize_keyboard=True,
    )


def settings_submenu_keyboard(lang: str | None = None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=menu_label(CHANGE_LANGUAGE, lang))],
            [KeyboardButton(text=menu_label(CHANGE_PHONE, lang))],
            [KeyboardButton(text=menu_label(MY_SESSIONS, lang))],
            [KeyboardButton(text=menu_label(BACK, lang))],
        ],
        resize_keyboard=True,
    )
