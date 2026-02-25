from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.i18n import menu_label


def human_mode_keyboard(session_id: str, human_mode: bool = False, lang: str | None = None) -> InlineKeyboardMarkup:
    if human_mode:
        button = InlineKeyboardButton(text=menu_label("human_mode_off", lang), callback_data=f"bot:{session_id}")
    else:
        button = InlineKeyboardButton(text=menu_label("human_mode_on", lang), callback_data=f"human:{session_id}")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button],
        ]
    )
