from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.bot.i18n import menu_label

NEW_CHAT = "new_chat"
END_SESSION = "end_session"
MY_SESSIONS = "my_sessions"
BACK = "back"
CHANGE_LANGUAGE = "change_language"
CURRENCY_RATES = "currency_rates"
BRANCHES = "branches"
NEAREST_BRANCH = "nearest_branch"


def main_menu_keyboard(lang: str | None = None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=menu_label(NEW_CHAT, lang)), KeyboardButton(text=menu_label(MY_SESSIONS, lang))],
            [KeyboardButton(text=menu_label(CURRENCY_RATES, lang)), KeyboardButton(text=menu_label(BRANCHES, lang))],
            [KeyboardButton(text=menu_label(NEAREST_BRANCH, lang)), KeyboardButton(text=menu_label(CHANGE_LANGUAGE, lang))],
            [KeyboardButton(text=menu_label(BACK, lang))],
        ],
        resize_keyboard=True,
    )


def chat_keyboard(lang: str | None = None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=menu_label(NEW_CHAT, lang))],
            [KeyboardButton(text=menu_label(END_SESSION, lang))],
            [KeyboardButton(text=menu_label(BACK, lang))],
            [KeyboardButton(text=menu_label(CURRENCY_RATES, lang)), KeyboardButton(text=menu_label(BRANCHES, lang))],
            [KeyboardButton(text=menu_label(NEAREST_BRANCH, lang)), KeyboardButton(text=menu_label(CHANGE_LANGUAGE, lang))],
        ],
        resize_keyboard=True,
    )


