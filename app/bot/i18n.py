from __future__ import annotations

import re
from typing import Any

SUPPORTED_LANGS = {"ru", "en", "uz"}


def normalize_lang(lang: str | None) -> str:
    code = (lang or "").strip().lower()
    return code if code in SUPPORTED_LANGS else "ru"


MENU_LABELS: dict[str, dict[str, str]] = {
    "ru": {
        # Top-level (main menu)
        "start_dialog": "💬 Начать диалог",
        "end_dialog": "✅ Завершить диалог",
        "our_branches": "🏢 Наши отделения",
        "currency_rates": "💱 Курс валют",
        "useful_links": "🔗 Полезные ссылки",
        "settings": "⚙️ Настройки",
        # Branches submenu
        "office_list": "📋 Список офисов",
        "nearest_branch": "📍 Найти ближайший ЦБУ",
        "electronic_queue": "🎫 Электронная очередь",
        "weekend_days": "📅 Выходные дни",
        # Rates submenu
        "rates_individuals": "👤 Курс физ.лиц",
        "rates_corporate": "🏢 Курс юр.лиц",
        "rates_online": "🌐 Курс онлайн конверсии",
        "rates_atm": "🏧 Курс банкоматов",
        # Useful links submenu
        "mobile_app_link": "📱 Ссылка на мобильное приложение",
        "social_links": "🌍 Ссылки официальных соц.сетей",
        "contacts_complaints": "☎️ Контакты для связи/жалоб",
        # Settings submenu
        "change_language": "🌐 Сменить язык",
        "change_phone": "📞 Сменить номер телефона",
        "my_sessions": "🗂️ Мои сессии",
        # Other
        "back": "⬅️ Назад",
        "contact_share": "Поделиться телефоном",
        "send_location": "Отправить геолокацию",
        "cancel": "Отмена",
        "branches_filials": "🏦 Филиалы (ЦБУ)",
        "branches_sales_offices": "🏪 Офисы продаж",
        "branches_sales_points": "🚗 Точки продаж (автосалоны)",
        "human_mode_on": "👤 Живой оператор",
        "human_mode_off": "🤖 Вернуться к боту",
    },
    "en": {
        "start_dialog": "💬 Start dialog",
        "end_dialog": "✅ End dialog",
        "our_branches": "🏢 Our branches",
        "currency_rates": "💱 Exchange rates",
        "useful_links": "🔗 Useful links",
        "settings": "⚙️ Settings",
        "office_list": "📋 Office list",
        "nearest_branch": "📍 Find nearest branch",
        "electronic_queue": "🎫 Electronic queue",
        "weekend_days": "📅 Weekend hours",
        "rates_individuals": "👤 Individual rates",
        "rates_corporate": "🏢 Corporate rates",
        "rates_online": "🌐 Online conversion rates",
        "rates_atm": "🏧 ATM rates",
        "mobile_app_link": "📱 Mobile app link",
        "social_links": "🌍 Official social media",
        "contacts_complaints": "☎️ Contacts / complaints",
        "change_language": "🌐 Change language",
        "change_phone": "📞 Change phone number",
        "my_sessions": "🗂️ My sessions",
        "back": "⬅️ Back",
        "contact_share": "Share phone number",
        "send_location": "Send location",
        "cancel": "Cancel",
        "branches_filials": "🏦 Filials",
        "branches_sales_offices": "🏪 Sales offices",
        "branches_sales_points": "🚗 Sales points (car dealers)",
        "human_mode_on": "👤 Live operator",
        "human_mode_off": "🤖 Back to bot",
    },
    "uz": {
        "start_dialog": "💬 Suhbatni boshlash",
        "end_dialog": "✅ Suhbatni yakunlash",
        "our_branches": "🏢 Bizning filiallar",
        "currency_rates": "💱 Valyuta kursi",
        "useful_links": "🔗 Foydali havolalar",
        "settings": "⚙️ Sozlamalar",
        "office_list": "📋 Ofislar ro'yxati",
        "nearest_branch": "📍 Eng yaqin filialni topish",
        "electronic_queue": "🎫 Elektron navbat",
        "weekend_days": "📅 Dam olish kunlari",
        "rates_individuals": "👤 Jismoniy shaxslar kursi",
        "rates_corporate": "🏢 Yuridik shaxslar kursi",
        "rates_online": "🌐 Onlayn konversiya kursi",
        "rates_atm": "🏧 Bankomatlar kursi",
        "mobile_app_link": "📱 Mobil ilova havolasi",
        "social_links": "🌍 Rasmiy ijtimoiy tarmoqlar",
        "contacts_complaints": "☎️ Aloqa va shikoyatlar",
        "change_language": "🌐 Tilni almashtirish",
        "change_phone": "📞 Telefon raqamini o'zgartirish",
        "my_sessions": "🗂️ Mening sessiyalarim",
        "back": "⬅️ Orqaga",
        "contact_share": "Telefon raqamini yuborish",
        "send_location": "Geolokatsiyani yuborish",
        "cancel": "Bekor qilish",
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
    # ── New menu (2026-04 redesign) ─────────────────────────────────────────
    "main_menu_prompt": {
        "ru": "Главное меню. Выберите раздел:",
        "en": "Main menu. Pick a section:",
        "uz": "Asosiy menyu. Bo'limni tanlang:",
    },
    "branches_menu_prompt": {
        "ru": "🏢 Наши отделения — выберите действие:",
        "en": "🏢 Our branches — pick an action:",
        "uz": "🏢 Bizning filiallar — tanlang:",
    },
    "rates_menu_prompt": {
        "ru": "💱 Курс валют — выберите тип:",
        "en": "💱 Exchange rates — pick a type:",
        "uz": "💱 Valyuta kursi — turini tanlang:",
    },
    "links_menu_prompt": {
        "ru": "🔗 Полезные ссылки — выберите раздел:",
        "en": "🔗 Useful links — pick a section:",
        "uz": "🔗 Foydali havolalar — bo'limni tanlang:",
    },
    "settings_menu_prompt": {
        "ru": "⚙️ Настройки — выберите действие:",
        "en": "⚙️ Settings — pick an action:",
        "uz": "⚙️ Sozlamalar — tanlang:",
    },
    "feature_coming_soon": {
        "ru": "🛠 Раздел в разработке. Скоро будет доступен.",
        "en": "🛠 This section is under development. Coming soon.",
        "uz": "🛠 Bo'lim ishlanmoqda. Tez orada ishga tushadi.",
    },
    "start_dialog_greeting": {
        "ru": "Здравствуйте! Я бот Asaka Bank. Чем могу помочь?",
        "en": "Hello! I'm the Asaka Bank bot. How can I help?",
        "uz": "Assalomu alaykum! Men Asaka Bank botiman. Qanday yordam bera olaman?",
    },
    "dialog_already_active": {
        "ru": "Диалог уже идёт. Завершите текущий, чтобы начать новый — кнопка «✅ Завершить диалог».",
        "en": "A dialog is already active. End it first to start a new one — tap «✅ End dialog».",
        "uz": "Suhbat allaqachon faol. Yangisini boshlash uchun «✅ Suhbatni yakunlash»ni bosing.",
    },
    "dialog_ended": {
        "ru": "Диалог завершён. Нажмите «💬 Начать диалог», чтобы начать новый.",
        "en": "Dialog ended. Tap «💬 Start dialog» to start a new one.",
        "uz": "Suhbat yakunlandi. Yangisini boshlash uchun «💬 Suhbatni boshlash»ni bosing.",
    },
    "dialog_no_active": {
        "ru": "Активного диалога нет. Нажмите «💬 Начать диалог», чтобы начать.",
        "en": "No active dialog. Tap «💬 Start dialog» to begin.",
        "uz": "Faol suhbat yo'q. Boshlash uchun «💬 Suhbatni boshlash»ni bosing.",
    },
    "history_header": {
        "ru": "🗂 История текущей сессии:",
        "en": "🗂 Current session history:",
        "uz": "🗂 Joriy sessiya tarixi:",
    },
    "history_empty": {
        "ru": "В текущей сессии пока нет сообщений.",
        "en": "No messages in the current session yet.",
        "uz": "Joriy sessiyada hali xabarlar yo'q.",
    },
    "change_phone_prompt": {
        "ru": "Поделитесь новым номером телефона кнопкой ниже.",
        "en": "Please share your new phone number using the button below.",
        "uz": "Pastdagi tugma orqali yangi telefon raqamingizni yuboring.",
    },
    "phone_updated": {
        "ru": "✅ Номер телефона обновлён.",
        "en": "✅ Phone number updated.",
        "uz": "✅ Telefon raqami yangilandi.",
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
        "start_dialog": {"начать диалог", "start dialog", "suhbatni boshlash"},
        "end_dialog": {"завершить диалог", "end dialog", "suhbatni yakunlash"},
        "our_branches": {"наши отделения", "отделения", "our branches", "branches", "bizning filiallar", "filiallar"},
        "currency_rates": {"курс", "курс валют", "exchange rate", "exchange rates", "valyuta kursi"},
        "useful_links": {"полезные ссылки", "useful links", "foydali havolalar"},
        "settings": {"настройки", "settings", "sozlamalar"},
        "office_list": {"список офисов", "office list", "ofislar royxati", "ofislar"},
        "nearest_branch": {"ближайший цбу", "nearest branch", "eng yaqin filial"},
        "electronic_queue": {"электронная очередь", "electronic queue", "elektron navbat"},
        "weekend_days": {"выходные дни", "weekend hours", "weekend days", "dam olish kunlari"},
        "rates_individuals": {"курс физ лиц", "курс физлиц", "individual rates", "jismoniy shaxslar kursi"},
        "rates_corporate": {"курс юр лиц", "курс юрлиц", "corporate rates", "yuridik shaxslar kursi"},
        "rates_online": {"курс онлайн конверсии", "online conversion rates", "onlayn konversiya kursi"},
        "rates_atm": {"курс банкоматов", "atm rates", "bankomatlar kursi"},
        "mobile_app_link": {"ссылка на мобильное приложение", "mobile app link", "mobil ilova havolasi"},
        "social_links": {"ссылки официальных соц сетей", "соц сети", "official social media", "social media", "rasmiy ijtimoiy tarmoqlar"},
        "contacts_complaints": {"контакты для связи жалоб", "контакты", "contacts complaints", "contacts", "aloqa va shikoyatlar"},
        "change_language": {"язык", "сменить язык", "language", "change language", "til", "tilni almashtirish"},
        "change_phone": {"сменить номер телефона", "change phone number", "telefon raqamini ozgartirish"},
        "my_sessions": {"мои сессии", "sessions", "my sessions", "sessiyalarim"},
        "branches_filials": {"филиалы цбу", "филиалы", "filials", "filiallar bxm", "filiallar"},
        "branches_sales_offices": {"офисы продаж", "sales offices", "savdo ofislari"},
        "branches_sales_points": {"точки продаж автосалоны", "точки продаж", "sales points car dealers", "sales points", "savdo nuqtalari avtosalon", "savdo nuqtalari"},
    }
    for action, values in synonyms.items():
        if norm in values:
            return action
    return None
