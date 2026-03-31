from __future__ import annotations

from typing import Optional, Sequence


def _contains_any(text: str, tokens: Sequence[str]) -> bool:
    lower = text.lower()
    return any(t in lower for t in tokens)


def _is_greeting(text: str) -> bool:
    return _contains_any(text, (
        "привет", "здравств", "салом", "ассалом", "добрый",
        "hello", "hi ", "good morning", "good afternoon",
    ))


def _is_thanks(text: str) -> bool:
    return _contains_any(text, ("спасибо", "благодар", "рахмат", "thank"))


def _is_branch_question(text: str) -> bool:
    return _contains_any(text, (
        "филиал", "отделен", "офис", "цбу", "адрес", "ближайш", "режим работы", "часы работы",
        "branch", "office", "nearest", "working hours", "location",
        "filial", "manzil", "ofis", "ish vaqti", "yaqin",
    ))


def _is_currency_question(text: str) -> bool:
    return _contains_any(text, (
        "курс", "доллар", "евро", "валют", "usd", "eur",
        "exchange", "rate", "dollar", "euro",
        "kurs", "valyuta",
    ))


def _is_calc_trigger(text: str) -> bool:
    lower = text.lower()
    return (
        "рассчита" in lower or "✅" in text or "📋" in text
        or "подать заявку" in lower
        or "calculate" in lower or "hisoblash" in lower or "hisobla" in lower
        or "apply" in lower or "ariza" in lower
    )


def _is_back_trigger(text: str) -> bool:
    lower = text.lower()
    return (
        "◀" in text
        or "все продукт" in lower or "назад" in lower
        or "all products" in lower or "back" in lower
        or "barcha mahsulot" in lower or "orqaga" in lower
    )


def _is_operator_request(text: str) -> bool:
    return _contains_any(text, (
        "оператор", "живой оператор", "подключи оператора",
        "соедини с оператором", "хочу оператора", "позовите оператора",
        "operator", "live agent", "human agent", "speak to human", "connect operator",
        "оператор билан", "операторга", "jonli operator", "operatorga",
    ))


def _is_identity_operation(text: str) -> bool:
    """Detect requests that require identity verification and cannot be handled by the bot."""
    return _contains_any(text, (
        # Card operations
        "разблокир", "заблокир", "блокировк", "блокировать карт",
        "unblock", "block card", "block my card",
        "kartani blokla", "blokdan chiqar",
        # SMS services
        "подключи смс", "отключи смс", "смс-оповещ", "смс уведомл", "sms-информ",
        "enable sms", "disable sms", "sms notification", "sms alert",
        "sms ulash", "sms o'chir", "sms xabarnoma",
        # Account status / personal data
        "состояние кредита", "остаток по кредит", "остаток по вклад", "баланс карт",
        "мой кредит", "мой вклад", "мой баланс", "мой счёт", "мой счет",
        "loan status", "my loan", "my deposit", "my balance", "my account", "card balance",
        "kredit holati", "omonat holati", "karta balansi", "mening kreditim", "mening hisobim",
        # Personal data changes / password / PIN
        "изменить данные", "изменить телефон", "изменить адрес", "обновить паспорт",
        "сменить пароль", "сменить пин", "поменять пин", "сбрось пароль", "сброс пароля",
        "забыл пароль", "забыл пин", "восстановить пароль", "восстановить пин",
        "новый пароль", "новый пин",
        "change my data", "change phone", "change address", "update passport",
        "change password", "change pin", "reset password", "reset pin",
        "forgot password", "forgot pin", "new password", "new pin",
        "ma'lumotlarni o'zgartir", "telefonni o'zgartir", "parolni o'zgartir", "pin kod",
        "parolni tikla", "yangi parol", "parolni unutdim",
        # Money transfers
        "перевести деньги", "перевод на карт", "перевод на счёт", "перевод на счет",
        "отправить деньги", "перечислить",
        "transfer money", "send money", "wire transfer",
        "pul o'tkaz", "pul yubor",
    ))


def _looks_like_question(text: str) -> bool:
    if "?" in text:
        return True
    lower = text.lower()
    return any(t in lower for t in (
        "забыл", "помоги", "не могу", "объясни", "расскажи",
        "где найти", "почему", "скажи", "как зайти", "как восстановить",
        "forgot", "help me", "cannot", "explain", "how to", "why", "where",
        "unutdim", "yordam", "qanday", "qayerda", "nima uchun", "tushuntir",
    ))


def _is_yes(text: str) -> bool:
    lower = text.lower()
    return any(t in lower for t in (
        "да", "yes", "✅", "позвоните", "хочу", "конечно", "ок", "ok", "ага",
        "sure", "absolutely", "of course", "call me",
        "ha", "albatta", "xohlayman",
    ))


def _is_comparison_request(text: str) -> bool:
    lower = text.lower()
    return any(t in lower for t in (
        "разница между", "сравни", "сравнение", "чем отличается", "чем отличаются",
        "в чем разница", "отличие между", "что лучше",
        "compare", "difference", "which is better",
        "solishtir", "farqi", "qaysi yaxshi",
    ))


def _detect_product_category(text: str) -> Optional[str]:
    """Rule-based product category detection. Returns category string or None."""
    lower = text.lower()
    if any(t in lower for t in (
        "ипотек", "квартир", "жиль", "недвижим", "новострой",
        "mortgage", "housing", "apartment", "real estate",
        "ipoteka", "kvartira", "uy-joy",
    )):
        return "mortgage"
    if any(t in lower for t in (
        "автокредит", "авто кредит", "для машины", "для авто", "машин", "автомобил",
        "car loan", "auto loan", "car credit",
        "avtokredit", "mashina",
    )):
        return "autoloan"
    if any(t in lower for t in (
        "образовательн", "учеб", "обучени", "контракт", "университет",
        "education", "student", "tuition", "university",
        "talim", "kontrakt", "universitet", "o'qish",
    )):
        return "education_credit"
    if any(t in lower for t in (
        "микрозайм", "микро займ", "микрокредит",
        "microloan", "micro loan",
        "mikroqarz", "mikro qarz",
    )):
        return "microloan"
    if any(t in lower for t in (
        "вклад", "депозит", "накоп", "сбережени",
        "deposit", "savings",
        "omonat", "jamg'arma",
    )):
        return "deposit"
    if any(t in lower for t in ("валютн", "за границ", "visa", "mastercard", "поездк", "forex", "travel card", "valyuta karta")):
        if any(t in lower for t in ("карт", "карточ", "card", "karta")):
            return "fx_card"
    if any(t in lower for t in (
        "карт", "карточ", "uzcard", "humo",
        "card", "karta",
    )):
        return "debit_card"
    # generic credit intent → show category selection menu
    if any(t in lower for t in ("кредит", "займ", "заём", "credit", "loan", "kredit", "qarz")):
        return "credit_menu"
    return None
