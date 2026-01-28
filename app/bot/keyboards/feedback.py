from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def feedback_keyboard(session_id: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=str(i), callback_data=f"fb:{session_id}:{i}")
        for i in range(1, 6)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Русский", callback_data="lang:ru"),
                InlineKeyboardButton(text="English", callback_data="lang:en"),
                InlineKeyboardButton(text="O'zbek", callback_data="lang:uz"),
            ]
        ]
    )
