from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.bot.i18n import menu_label


def contact_keyboard(lang: str | None = None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=menu_label("contact_share", lang), request_contact=True)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def location_keyboard(lang: str | None = None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=menu_label("send_location", lang), request_location=True)],
            [KeyboardButton(text=menu_label("cancel", lang))],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
