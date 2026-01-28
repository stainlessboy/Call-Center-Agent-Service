from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

NEW_CHAT = "📞 Колл-центр"
MY_SESSIONS = "🗂️ Мои сессии"
BACK = "⬅️ Назад"
CHANGE_LANGUAGE = "🌐 Сменить язык"
CURRENCY_RATES = "💱 Курс валют"
BRANCHES = "🏢 Отделения"
NEAREST_BRANCH = "📍 Найти ближайший ЦБУ"


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=NEW_CHAT), KeyboardButton(text=MY_SESSIONS)],
            [KeyboardButton(text=CURRENCY_RATES), KeyboardButton(text=BRANCHES)],
            [KeyboardButton(text=NEAREST_BRANCH), KeyboardButton(text=CHANGE_LANGUAGE)],
            [KeyboardButton(text=BACK)],
        ],
        resize_keyboard=True,
    )


def chat_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=NEW_CHAT)],
            [KeyboardButton(text=BACK)],
            [KeyboardButton(text=CURRENCY_RATES), KeyboardButton(text=BRANCHES)],
            [KeyboardButton(text=NEAREST_BRANCH), KeyboardButton(text=CHANGE_LANGUAGE)],
        ],
        resize_keyboard=True,
    )
