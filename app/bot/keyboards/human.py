from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def human_mode_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Режим человека", callback_data=f"human:{session_id}")],
        ]
    )
