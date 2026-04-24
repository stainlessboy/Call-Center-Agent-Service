from __future__ import annotations

import re
from typing import Optional, Sequence


def _contains_any(text: str, tokens: Sequence[str]) -> bool:
    lower = text.lower()
    return any(t in lower for t in tokens)


def _is_greeting(text: str) -> bool:
    return _contains_any(text, (
        "привет", "здравств", "салом", "ассалом", "добрый",
        "hello", "hi ", "good morning", "good afternoon",
        "salom", "assalom", "hayrli kun", "hayrli tong",
    ))


def _is_thanks(text: str) -> bool:
    return _contains_any(text, (
        "спасибо", "благодар", "рахмат", "thank",
        "rahmat", "tashakkur", "minnatdor",
    ))


def _is_branch_question(text: str) -> bool:
    return _contains_any(text, (
        "филиал", "отделен", "офис", "цбу", "адрес", "ближайш", "режим работы", "часы работы",
        "branch", "office", "nearest", "working hours", "location",
        "filial", "manzil", "ofis", "ish vaqti", "yaqin", "bo'lim", "eng yaqin",
    ))


def _is_currency_question(text: str) -> bool:
    return _contains_any(text, (
        "курс", "доллар", "евро", "валют", "usd", "eur",
        "exchange", "rate", "dollar", "euro",
        "kurs", "valyuta", "dollar", "yevro", "valyuta kursi",
    ))


def _is_calc_trigger(text: str) -> bool:
    """Detect calculator/apply button press. Only short button-like messages trigger calc.
    Long free-text messages like 'рассчитай кредит на 250 млн' should NOT trigger."""
    lower = text.lower().strip()
    # Emoji buttons always trigger
    if "✅" in text or "📋" in text:
        return True
    # Only short messages (button presses) — max ~40 chars
    if len(lower) > 40:
        return False
    return (
        "рассчита" in lower
        or "подать заявку" in lower
        or "calculate" in lower or "hisoblash" in lower or "hisobla" in lower
        or "hisob-kitob" in lower
        or "apply" in lower or "ariza" in lower or "ariza topshir" in lower
    )


def _is_back_trigger(text: str) -> bool:
    lower = text.lower()
    return (
        "◀" in text
        or "все продукт" in lower or "назад" in lower
        or "all products" in lower or "back" in lower
        or "barcha mahsulot" in lower or "orqaga" in lower or "ortga" in lower
    )


_OPERATOR_PREFIX_RE = re.compile(
    r"\b(оператор|operator)[а-яa-z]*\b",
    re.IGNORECASE,
)


def _is_operator_request(text: str) -> bool:
    """True if the user asked to speak to a live operator.

    Uses a prefix-anchored regex for "оператор" / "operator" so any
    morphological suffix matches (оператора, оператором, операторов,
    operatorni, operatorga, operators, …) but collisions with longer
    words like "телеоператор" / "cooperator" are rejected because the
    leading ``\\b`` requires a word boundary before the prefix.

    Extra phrases ("live agent", "human agent", "jonli operator", …)
    fall back to substring matching.
    """
    lower = text.lower()
    if _OPERATOR_PREFIX_RE.search(lower):
        return True
    return _contains_any(lower, (
        "живой оператор", "подключи оператора",
        "соедини с оператором", "хочу оператора", "позовите оператора",
        "live agent", "human agent", "speak to human", "connect operator",
        "jonli operator",
    ))


def _is_identity_operation(text: str) -> bool:
    """Detect requests that require identity verification and cannot be handled by the bot."""
    return _contains_any(text, (
        # Card operations
        "разблокир", "заблокир", "блокировк", "блокировать карт",
        "unblock", "block card", "block my card",
        "kartani blokla", "blokdan chiqar", "kartani bloklash",
        # SMS services
        "подключи смс", "отключи смс", "смс-оповещ", "смс уведомл", "sms-информ",
        "enable sms", "disable sms", "sms notification", "sms alert",
        "sms ulash", "sms o'chir", "sms xabarnoma", "sms ulab ber", "sms o'chirib ber",
        # Account status / personal data
        "состояние кредита", "остаток по кредит", "остаток по вклад", "баланс карт",
        "мой кредит", "мой вклад", "мой баланс", "мой счёт", "мой счет",
        "loan status", "my loan", "my deposit", "my balance", "my account", "card balance",
        "kredit holati", "omonat holati", "karta balansi", "mening kreditim", "mening hisobim",
        "kredit qoldig'i", "omonat qoldig'i", "mening omonatim", "mening kartam",
        # Personal data changes / password / PIN
        "изменить данные", "изменить телефон", "изменить адрес", "обновить паспорт",
        "сменить пароль", "сменить пин", "поменять пин", "сбрось пароль", "сброс пароля",
        "забыл пароль", "забыл пин", "восстановить пароль", "восстановить пин",
        "новый пароль", "новый пин",
        "change my data", "change phone", "change address", "update passport",
        "change password", "change pin", "reset password", "reset pin",
        "forgot password", "forgot pin", "new password", "new pin",
        "ma'lumotlarni o'zgartir", "telefonni o'zgartir", "parolni o'zgartir", "pin kod",
        "parolni tikla", "yangi parol", "parolni unutdim", "pin kodni o'zgartir",
        "pasportni yangilash", "manzilni o'zgartir",
        # Money transfers
        "перевести деньги", "перевод на карт", "перевод на счёт", "перевод на счет",
        "отправить деньги", "перечислить",
        "transfer money", "send money", "wire transfer",
        "pul o'tkaz", "pul yubor", "pul jo'nat",
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
        "yordam ber", "qanday qilib", "nimaga", "qachon", "kim",
    ))


def _is_recalculate(text: str) -> bool:
    lower = text.lower()
    return any(t in lower for t in (
        "пересчитать", "пересчитай", "перерасч", "🔄", "заново", "recalculate", "recalc",
        "qayta hisob", "qayta hisobla",
    ))


def _is_yes(text: str) -> bool:
    lower = text.lower()
    return any(t in lower for t in (
        "да", "yes", "✅", "позвоните", "хочу", "конечно", "ок", "ok", "ага",
        "sure", "absolutely", "of course", "call me",
        "ha,", "ha!", "ha ", "albatta", "xohlayman", "xohlyman",
        "qo'ng'iroq qiling", "zarur", "kerak",
    ))


def _is_comparison_request(text: str) -> bool:
    lower = text.lower()
    return any(t in lower for t in (
        "разница между", "сравни", "сравнение", "чем отличается", "чем отличаются",
        "в чем разница", "отличие между", "что лучше",
        "compare", "difference", "which is better",
        "solishtir", "farqi", "qaysi yaxshi", "farqi nimada", "qaysi biri",
        "nimasi bilan farq",
    ))


def _detect_product_category(text: str) -> Optional[str]:
    """Rule-based product category detection. Returns category string or None."""
    lower = text.lower()
    if any(t in lower for t in (
        "ипотек", "квартир", "жиль", "недвижим", "новострой",
        "mortgage", "housing", "apartment", "real estate",
        "ipoteka", "kvartira", "uy-joy", "uy joy", "ko'chmas mulk",
    )):
        return "mortgage"
    if any(t in lower for t in (
        "автокредит", "авто кредит", "для машины", "для авто", "машин", "автомобил",
        "car loan", "auto loan", "car credit",
        "avtokredit", "mashina", "avtomobil", "avto kredit",
    )):
        return "autoloan"
    if any(t in lower for t in (
        "образовательн", "учеб", "обучени", "контракт", "университет",
        "education", "student", "tuition", "university",
        "talim", "ta'lim", "kontrakt", "universitet", "o'qish",
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
        "omonat", "jamg'arma", "depozit",
    )):
        return "deposit"
    if any(t in lower for t in (
        "валютн", "за границ", "visa", "mastercard", "поездк", "forex",
        "travel card", "valyuta karta", "chet el karta",
    )):
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
