from __future__ import annotations

import re
from typing import Any

SUPPORTED_LANGS = {"ru", "en", "uz"}


def normalize_lang(lang: str | None) -> str:
    code = (lang or "").strip().lower()
    return code if code in SUPPORTED_LANGS else "ru"


MENU_LABELS: dict[str, dict[str, str]] = {
    "ru": {
        "new_chat": "📞 Колл-центр",
        "end_session": "✅ Завершить сессию",
        "my_sessions": "🗂️ Мои сессии",
        "back": "⬅️ Назад",
        "change_language": "🌐 Сменить язык",
        "currency_rates": "💱 Курс валют",
        "branches": "🏢 Отделения",
        "nearest_branch": "📍 Найти ближайший ЦБУ",
        "contact_share": "Поделиться телефоном",
        "send_location": "Отправить геолокацию",
        "cancel": "Отмена",
        "branch_tashkent": "🏙 Ташкент",
        "branch_regions": "🌍 Регионы",
        "branches_filials": "🏦 Филиалы (ЦБУ)",
        "branches_sales_offices": "🏪 Офисы продаж",
        "branches_sales_points": "🚗 Точки продаж (автосалоны)",
        "human_mode_on": "👤 Живой оператор",
        "human_mode_off": "🤖 Вернуться к боту",
    },
    "en": {
        "new_chat": "📞 Call center",
        "end_session": "✅ End session",
        "my_sessions": "🗂️ My sessions",
        "back": "⬅️ Back",
        "change_language": "🌐 Change language",
        "currency_rates": "💱 Exchange rates",
        "branches": "🏢 Branches",
        "nearest_branch": "📍 Find nearest branch",
        "contact_share": "Share phone number",
        "send_location": "Send location",
        "cancel": "Cancel",
        "branch_tashkent": "🏙 Tashkent",
        "branch_regions": "🌍 Regions",
        "branches_filials": "🏦 Filials",
        "branches_sales_offices": "🏪 Sales offices",
        "branches_sales_points": "🚗 Sales points (car dealers)",
        "human_mode_on": "👤 Live operator",
        "human_mode_off": "🤖 Back to bot",
    },
    "uz": {
        "new_chat": "📞 Koll-markaz",
        "end_session": "✅ Sessiyani yakunlash",
        "my_sessions": "🗂️ Mening sessiyalarim",
        "back": "⬅️ Orqaga",
        "change_language": "🌐 Tilni almashtirish",
        "currency_rates": "💱 Valyuta kursi",
        "branches": "🏢 Filiallar",
        "nearest_branch": "📍 Eng yaqin filialni topish",
        "contact_share": "Telefon raqamini yuborish",
        "send_location": "Geolokatsiyani yuborish",
        "cancel": "Bekor qilish",
        "branch_tashkent": "🏙 Toshkent",
        "branch_regions": "🌍 Hududlar",
        "branches_filials": "🏦 Filiallar (BXM)",
        "branches_sales_offices": "🏪 Savdo ofislari",
        "branches_sales_points": "🚗 Savdo nuqtalari (avtosalon)",
        "human_mode_on": "👤 Jonli operator",
        "human_mode_off": "🤖 Botga qaytish",
    },
}

TEXTS: dict[str, dict[str, str]] = {
    "ask_language": {
        "ru": "Выберите язык / Choose language / Tilni tanlang:",
        "en": "Choose language / Выберите язык / Tilni tanlang:",
        "uz": "Tilni tanlang / Choose language / Выберите язык:",
    },
    "start_after_language": {
        "ru": "Вы в чате с агентом банка. Я сохраню историю сообщений.\nКоманды: /new — новая сессия, /end — завершить текущую.",
        "en": "You are in a bank support chat. I will keep the message history.\nCommands: /new — new session, /end — end current session.",
        "uz": "Siz bank agenti bilan chatdasiz. Xabarlar tarixini saqlayman.\nBuyruqlar: /new — yangi sessiya, /end — joriy sessiyani yakunlash.",
    },
    "share_phone_first": {
        "ru": "Поделитесь, пожалуйста, номером телефона для связи.\nКоманды: /new — новая сессия, /end — завершить текущую.",
        "en": "Please share your phone number for contact.\nCommands: /new — new session, /end — end current session.",
        "uz": "Aloqa uchun telefon raqamingizni yuboring.\nBuyruqlar: /new — yangi sessiya, /end — joriy sessiyani yakunlash.",
    },
    "phone_saved_choose_language": {
        "ru": "Спасибо, номер телефона сохранен. Теперь выберите язык.",
        "en": "Thank you, your phone number has been saved. Now choose a language.",
        "uz": "Rahmat, telefon raqamingiz saqlandi. Endi tilni tanlang.",
    },
    "language_saved": {
        "ru": "Язык сохранён.",
        "en": "Language saved.",
        "uz": "Til saqlandi.",
    },
    "feedback_saved": {
        "ru": "Спасибо за оценку!",
        "en": "Thank you for your rating!",
        "uz": "Baholaganingiz uchun rahmat!",
    },
    "feedback_failed": {
        "ru": "Не удалось сохранить оценку. Попробуйте позже.",
        "en": "Could not save your rating. Please try again later.",
        "uz": "Bahoni saqlab bo‘lmadi. Keyinroq qayta urinib ko‘ring.",
    },
    "session_closed_timeout": {
        "ru": "Сессия закрыта из-за отсутствия активности. Оцените работу агента:",
        "en": "The session was closed due to inactivity. Please rate the agent:",
        "uz": "Faollik bo‘lmagani sababli sessiya yopildi. Agent ishini baholang:",
    },
    "human_timeout_back_to_bot": {
        "ru": "Оператор не ответил в течение {minutes} минут. Я снова переключил сессию в режим бота, можем продолжать.",
        "en": "The operator did not respond within {minutes} minutes. I switched the session back to bot mode, we can continue.",
        "uz": "Operator {minutes} daqiqa ichida javob bermadi. Sessiya yana bot rejimiga qaytarildi, davom etishimiz mumkin.",
    },
    "agent_unavailable": {
        "ru": "Временно не могу ответить. Попробуйте позже.",
        "en": "I cannot reply right now. Please try again later.",
        "uz": "Hozircha javob bera olmayman. Keyinroq urinib ko‘ring.",
    },
    "sent_to_operator": {
        "ru": "Ваше сообщение передано оператору. Ожидайте ответа.",
        "en": "Your message has been forwarded to an operator. Please wait for a reply.",
        "uz": "Xabaringiz operatorga yuborildi. Javobni kuting.",
    },
    "pdf_caption": {
        "ru": "График выплат",
        "en": "Payment schedule",
        "uz": "To’lov jadvali",
    },
    "searching_operator": {
        "ru": "🔍 Ищем свободного оператора...",
        "en": "🔍 Looking for an available operator...",
        "uz": "🔍 Bo’sh operator qidirilmoqda...",
    },
    "all_operators_busy": {
        "ru": "😔 Все операторы заняты. Попробуйте позже.",
        "en": "😔 All operators are busy. Please try again later.",
        "uz": "😔 Barcha operatorlar band. Keyinroq urinib ko’ring.",
    },
    "operator_wait_timeout": {
        "ru": "⏰ Оператор не ответил. Возвращаю к боту.",
        "en": "⏰ Operator did not respond. Returning to bot.",
        "uz": "⏰ Operator javob bermadi. Botga qaytarilmoqda.",
    },
    "middleware_unavailable": {
        "ru": "😔 Операторы сейчас недоступны. Попробуйте позже.",
        "en": "😔 Operators are currently unavailable. Please try again later.",
        "uz": "😔 Operatorlar hozircha mavjud emas. Keyinroq urinib ko’ring.",
    },
    "operator_connected": {
        "ru": "✅ Оператор подключился, можете задать свой вопрос.",
        "en": "✅ Operator joined the chat, you can ask your question.",
        "uz": "✅ Operator ulandi, savolingizni berishingiz mumkin.",
    },
    "chat_ended": {
        "ru": "Чат завершён.",
        "en": "Chat ended.",
        "uz": "Chat yakunlandi.",
    },
    "chat_ended_try_again": {
        "ru": "Чат завершён, пожалуйста, попробуйте ещё раз.",
        "en": "Chat ended, please try again.",
        "uz": "Chat yakunlandi, iltimos qaytadan urinib ko‘ring.",
    },
    "chat_inactivity_warning": {
        "ru": "Чат закроется через минуту из-за бездействия. Чтобы продолжить — напишите сообщение.",
        "en": "Chat will close in a minute due to inactivity. Send a message to continue.",
        "uz": "Chat 1 daqiqada faoliyatsizlik sababli yopiladi. Davom etish uchun xabar yozing.",
    },
    "working_hours": {
        "ru": "Чат с оператором работает с 8:00 до 23:00 (Ташкент).",
        "en": "Operator chat is available from 8:00 to 23:00 (Tashkent time).",
        "uz": "Operator chati 8:00 dan 23:00 gacha ishlaydi (Toshkent vaqti).",
    },
    "phone_required_for_operator": {
        "ru": "Чтобы соединить вас с оператором, поделитесь номером телефона кнопкой ниже.",
        "en": "To connect you to an operator, please share your phone number using the button below.",
        "uz": "Operator bilan bog‘lash uchun pastdagi tugma orqali telefon raqamingizni yuboring.",
    },
    "message_send_failed": {
        "ru": "Не удалось отправить сообщение. Попробуйте ещё раз.",
        "en": "Failed to send the message. Please try again.",
        "uz": "Xabarni yuborib bo‘lmadi. Qayta urinib ko‘ring.",
    },
    "connection_lost": {
        "ru": "Соединение с оператором потеряно. Попробуйте подключиться снова.",
        "en": "Connection to the operator was lost. Please try connecting again.",
        "uz": "Operator bilan aloqa uzildi. Qaytadan ulanishga harakat qiling.",
    },
}

LANGUAGE_DISPLAY_NAMES: dict[str, str] = {
    "ru": "Русский",
    "en": "English",
    "uz": "O'zbek",
}


def t(key: str, lang: str | None = None, **kwargs: Any) -> str:
    code = normalize_lang(lang)
    variants = TEXTS.get(key, {})
    template = variants.get(code) or variants.get("ru") or key
    return template.format(**kwargs) if kwargs else template


def menu_label(action: str, lang: str | None = None) -> str:
    code = normalize_lang(lang)
    return MENU_LABELS.get(code, MENU_LABELS["ru"]).get(action, action)


def _normalize_for_match(text: str | None) -> str:
    if not text:
        return ""
    normalized = re.sub(r"^[^\wА-Яа-я]+", "", text, flags=re.UNICODE).strip().lower()
    normalized = re.sub(r"[^\w\s]+", " ", normalized, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def menu_action_from_text(text: str | None) -> str | None:
    norm = _normalize_for_match(text)
    if not norm:
        return None
    for action in next(iter(MENU_LABELS.values())).keys():
        for labels in MENU_LABELS.values():
            if _normalize_for_match(labels.get(action)) == norm:
                return action
    synonyms = {
        "back": {"назад", "back", "orqaga"},
        "cancel": {"отмена", "cancel", "bekor qilish", "bekor"},
        "change_language": {"язык", "сменить язык", "language", "change language", "til", "tilni almashtirish"},
        "new_chat": {"новая сессия", "новый чат", "new chat", "new session", "yangi sessiya"},
        "my_sessions": {"мои сессии", "sessions", "my sessions", "sessiyalarim"},
        "currency_rates": {"курс", "курс валют", "exchange rate", "exchange rates", "valyuta kursi"},
        "branches": {"отделения", "branches", "filiallar"},
        "nearest_branch": {"ближайший цбу", "nearest branch", "eng yaqin filial"},
        "end_session": {"завершить сессию", "end session", "sessiyani yakunlash"},
        "branch_tashkent": {"ташкент", "tashkent", "toshkent"},
        "branch_regions": {"регионы", "regions", "hududlar"},
        "branches_filials": {"филиалы цбу", "филиалы", "filials", "filiallar bxm", "filiallar"},
        "branches_sales_offices": {"офисы продаж", "sales offices", "savdo ofislari"},
        "branches_sales_points": {"точки продаж автосалоны", "точки продаж", "sales points car dealers", "sales points", "savdo nuqtalari avtosalon", "savdo nuqtalari"},
    }
    for action, values in synonyms.items():
        if norm in values:
            return action
    return None
