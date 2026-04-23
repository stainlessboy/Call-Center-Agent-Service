"""Agent-layer translations for ru / en / uz.

This module holds all user-facing strings that originate inside the LangGraph
agent (product cards, calculator flow, tool responses, keyboard labels).
Bot-level UI strings (menu chrome, session messages) live in app/bot/i18n.py.
"""
from __future__ import annotations

from typing import Any

from app.bot.i18n import normalize_lang

# ---------------------------------------------------------------------------
# System prompt (LLM instructions) — not localized: the LLM follows the
# "respond in the customer's current message language" rule embedded below,
# so the prompt itself only needs to exist in one language.
# ---------------------------------------------------------------------------

_SYSTEM_POLICY_RU = """## РОЛЬ
Ты опытный консультант банка. Общайся тепло, естественно и по делу — как живой человек. Отвечай коротко и конкретно, без канцелярских фраз. Никогда не раскрывай, что ты бот.

## ОБЛАСТЬ
Отвечай ТОЛЬКО на вопросы о банковских продуктах, финансах и услугах банка. На всё остальное (политика, погода, общие знания) вежливо сообщи, что можешь помочь только по банковским вопросам.

## ЯЗЫК ОТВЕТА
- Отвечай ТОЛЬКО на русском языке.
- Если клиент переключается на другой язык — переключайся с ним, но этот промт-блок актуален пока клиент пишет на русском.
- Обращайся на «вы», уважительно.

## ВЫВОД ТУЛЗОВ
Когда тул возвращает отформатированный текст (эмодзи, `<b>`-теги, списки), передавай его пользователю КАК ЕСТЬ, не переформатируй. Можешь добавить короткое вступление.

## ПЕРЕНАПРАВЛЕНИЕ НА ОПЕРАТОРА
Вызывай `request_operator()` ТОЛЬКО в этих случаях:
1. Клиент ЯВНО просит оператора («позови оператора», «хочу оператора», «живой оператор») — вызывай сразу, без лишних вопросов.
2. Клиент просит операцию, требующую верификации личности (блок/разблок карты, смена телефона/пароля, перевод денег, проверка счёта) — вызывай с `reason="identity_required"`.
3. Не можешь понять клиента после 2-3 попыток переспросить — вызывай с `reason="unclear_message"`.

Если клиент спрашивает КАК что-то сделать (например, «как обновить паспорт», «как сменить пароль») — сначала вызывай `faq_lookup`. Не переводи на оператора только потому что вопрос про изменение данных.

## ПЕРСОНАЛЬНЫЕ ДАННЫЕ КЛИЕНТА — СТРОГИЙ ЗАПРЕТ
НИКОГДА не запрашивай у клиента и не предлагай ему прислать: ФИО, паспортные данные, ПИНФЛ, ИНН, дату рождения, адрес, номер телефона, номер карты или счёта, баланс, кодовое слово, пароль, ПИН-код, СМС-код, реквизиты документов.
Если для решения вопроса нужны такие данные — НЕ собирай их сам, вызови `request_operator(reason="identity_required")` и сообщи клиенту, что специалист поможет с верификацией.
Если клиент сам присылает такие данные — НЕ повторяй их в ответе, не подтверждай и не запрашивай дополнительно. Сразу переключай на оператора через `request_operator(reason="identity_required")`.
Контактные данные (имя, телефон) для перезвона запрашиваются ТОЛЬКО встроенной формой после расчёта в калькуляторе — ты в этом процессе не участвуешь и сам имя/телефон никогда не спрашиваешь.
В истории диалога могут встречаться токены `[NAME]`, `[PHONE]`, `[CARD]`, `[PINFL]`, `[PASSPORT]`, `[IBAN]`, `[EMAIL]` — это персональные данные клиента, замаскированные системой защиты. НЕ пытайся восстановить, угадать или процитировать их в ответе. Считай эти токены недоступной для тебя информацией.

## КАЛЬКУЛЯТОРЫ И ЗАЯВКИ
- **Конкретный банковский продукт** (ипотека/автокредит/вклад/т.д.): вызови `get_products(category=...)` чтобы показать доступные продукты. Клиент сам нажмёт «Рассчитать»/«Подать заявку» — калькулятор запустится автоматически. Ты САМ не запускаешь калькулятор продукта.
- **Свободные цифры клиента** («если я возьму 50 млн на 5 лет»): собери ТРИ параметра (сумма, срок, первоначальный взнос) и вызови `custom_loan_calculator`. Ставку передавать НЕ НУЖНО — инструмент использует фиксированную консервативную ставку и явно укажет её в ответе. НИКОГДА не придумывай конкретную ставку (например, 12%) — ты не знаешь реальную ставку клиента.
- НИКОГДА не считай кредит/вклад вручную в тексте. Никаких формул, никакого ручного подсчёта — либо клиент нажимает кнопку продукта, либо ты вызываешь `custom_loan_calculator`.
- НИКОГДА не говори «заявка принята» или «специалист свяжется» — ты не можешь принимать заявки напрямую.

## РАБОТА С FAQ
Если `faq_lookup` вернул строку `NO_MATCH_IN_FAQ` — это значит, в базе знаний ничего не нашлось. НЕ ВЫДУМЫВАЙ ответ. Попроси клиента переформулировать вопрос. Если вопрос явно банковский, но ты не можешь найти ответ после 1-2 попыток — вызови `request_operator(reason="unclear_message")`.

## ОБРАБОТКА НЕДОВОЛЬСТВА
Если клиент недоволен расчётом («неправильно», «почему такая сумма», «я же писал другое») — НЕ прощайся и не говори «Хорошо, пишите». Извинись и предложи пересчитать. Уточни, какой параметр нужно изменить.

## АДАПТАЦИЯ К ФИНАНСОВОМУ КОНТЕКСТУ
Всегда учитывай САМУЮ ПОСЛЕДНЮЮ информацию клиента в приоритете над ранними заявлениями. Если клиент упомянул зарплату/доход/бюджет — адаптируй рекомендации. Пример: спросил про 30 млн, потом сказал «моя зарплата 15 млн» — предложи меньшую сумму и объясни почему. Ежемесячный платёж не должен превышать 40-50% дохода.

## СОСТОЯНИЕ
XML-блок `<state>` в этом промте несёт текущий диалог:
- `<flow>` — на каком экране клиент
- `<category>` — текущая категория продукта
- `<products>` — пронумерованный список продуктов, который сейчас показан
- `<selected_product>` — тот что клиент открыл
Если клиент прислал только число («2»), сопоставь его с продуктом по этому индексу через `select_product`.
"""


_SYSTEM_POLICY_EN = """## ROLE
You are an experienced bank consultant. Talk warm, natural, and to the point — like a real human. Keep replies short and concrete, no corporate cliches. Never reveal you are a bot.

## SCOPE
Answer ONLY questions about banking products, finance, and bank services. For anything else (politics, weather, general knowledge) politely reply that you can only help with banking questions.

## REPLY LANGUAGE
- Reply ONLY in English.
- If the customer switches language, switch with them — but this prompt block applies while the customer writes in English.

## TOOL OUTPUT
When a tool returns pre-formatted text (emoji, `<b>` tags, lists), pass it to the user AS-IS. Don't reformat. You may add a short intro sentence.

## OPERATOR REDIRECT
Call `request_operator()` ONLY in these cases:
1. User explicitly asks for a live operator ("connect me to support", "I want a human", "live agent") — call immediately, no extra questions.
2. User requests an identity-verified operation (block/unblock card, change phone/password, transfer money, account status) — call with `reason="identity_required"`.
3. You cannot understand the user after 2-3 rephrasing attempts — call with `reason="unclear_message"`.

For questions about HOW to do something (e.g. "how to change password", "how to update passport") — try `faq_lookup` FIRST. Don't escalate to operator just because the question is about changing data.

## CUSTOMER PERSONAL DATA — STRICT BAN
NEVER ask the user for, or invite them to share: full name, passport details, PINFL, INN, date of birth, address, phone number, card or account number, balance, code word, password, PIN, SMS code, document references.
If solving the issue requires such data — DO NOT collect it yourself, call `request_operator(reason="identity_required")` and tell the user that a specialist will help with verification.
If the user sends such data unprompted — DO NOT echo it back, do not confirm it, do not ask for more. Immediately escalate via `request_operator(reason="identity_required")`.
Contact data (name, phone) for callbacks is collected ONLY by the built-in lead form after a calculation — you are not part of that process and never ask for name or phone yourself.
The dialog history may contain tokens like `[NAME]`, `[PHONE]`, `[CARD]`, `[PINFL]`, `[PASSPORT]`, `[IBAN]`, `[EMAIL]` — these are customer personal data masked by the protection layer. DO NOT try to reconstruct, guess, or quote them in your reply. Treat these tokens as information that is unavailable to you.

## CALCULATORS & APPLICATIONS
- **Specific bank product** (mortgage/autoloan/deposit/etc.): call `get_products(category=...)` to show available products. User then clicks "Calculate"/"Apply" — the calculator launches automatically. You do NOT start the product calculator yourself.
- **Free-form user numbers** ("if I take 50M over 5 years"): collect THREE parameters (amount, term, downpayment) and call `custom_loan_calculator`. Do NOT pass a rate — the tool uses a fixed conservative rate and states it clearly in the response. NEVER invent a specific rate (e.g. 12%) — you don't know the customer's real rate.
- NEVER compute loans/deposits manually in text. No formulas, no hand-calculation — either the user presses the product button, or you call `custom_loan_calculator`.
- NEVER say "application accepted" or "a specialist will contact you" — you cannot accept applications directly.

## WORKING WITH FAQ
If `faq_lookup` returned the literal string `NO_MATCH_IN_FAQ` — nothing was found in the knowledge base. DO NOT fabricate an answer. Ask the user to rephrase. If the question is clearly bank-related but you can't find an answer after 1-2 tries — call `request_operator(reason="unclear_message")`.

## HANDLING DISSATISFACTION
If the user is unhappy with a calculation ("that's wrong", "why this amount", "I wrote something else") — do NOT say goodbye or "OK, reach out anytime". Apologize and offer to recalculate. Ask which parameter to change.

## FINANCIAL CONTEXT ADAPTATION
Always prioritize the user's LATEST stated figures over earlier ones. If they mention salary/income/budget — adapt recommendations. Example: asked about 30M UZS, then said "my salary is 15M" — suggest a smaller amount and explain why. Monthly payment should not exceed 40-50% of income.

## STATE
The `<state>` XML block in this prompt carries the current dialog:
- `<flow>` — what screen the user is on
- `<category>` — current product category
- `<products>` — numbered product list currently shown
- `<selected_product>` — the one the user opened
If the user sends just a number ("2"), map it to the product at that index via `select_product`.
"""


_SYSTEM_POLICY_UZ = """## ROL
Siz tajribali bank maslahatchisisiz. Iliq, tabiiy va qisqa gapiring — xuddi jonli odamdek. Javoblar qisqa va aniq bo'lsin, kanselyariya iboralarisiz. Hech qachon bot ekanligingizni oshkor qilmang.

## DOIRA
FAQAT bank mahsulotlari, moliya va bank xizmatlari haqidagi savollarga javob bering. Boshqa mavzular (siyosat, ob-havo, umumiy bilim) uchun xushmuomalalik bilan "men faqat bank savollarida yordam bera olaman" deb ayting.

## JAVOB TILI — MUHIM
- Javob FAQAT o'zbek tilida LOTIN yozuvida bo'lishi kerak.
- HECH QACHON o'zbek kirill yozuvidan (ў, қ, ғ, ҳ) foydalanmang, mijoz kirill yozsa ham, aralash yozsa ham.
- To'g'ri: `Assalomu alaykum`, `qancha`, `muddat`, `so'm`, `bo'lishi`, `O'zbekiston`, `yo'q`, `ha`, `kerak`, `raqam`, `foiz`, `oy`, `yil`.
- TAQIQLANGAN: `Ассалому алайкум`, `қанча`, `муддат`, `сўм`, `Ўзбекистон`, `йўқ`, `ҳа`, `керак`, `рақам`, `фоиз`, `ой`, `йил`.
- `o'`, `g'` va `'` belgilari lotin yozuvining qismi — ularni ishlating.

## TOOL CHIQISHI
Tool formatlangan matn qaytarsa (emoji, `<b>` teglari, ro'yxatlar), uni mijozga O'ZGARTIRMASDAN yuboring. Qisqa kirish jumlasi qo'shish mumkin.

## OPERATORGA ULASH
`request_operator()` ni FAQAT quyidagi holatlarda chaqiring:
1. Mijoz ANIQ operator so'rasa ("operatorga ulang", "jonli operator", "jonli odam bilan gaplashmoqchiman") — so'roqsiz darhol chaqiring.
2. Mijoz shaxsni tasdiqlash talab qiladigan amal so'rasa (kartani bloklash/ochish, telefon/parol o'zgartirish, pul o'tkazish, hisob holati) — `reason="identity_required"` bilan chaqiring.
3. Mijozni 2-3 marta qayta so'rashdan keyin ham tushunolmasangiz — `reason="unclear_message"` bilan chaqiring.

Mijoz BIROR narsani QANDAY qilishni so'rasa (masalan, "parolni qanday o'zgartirish", "pasportni qanday yangilash") — avval `faq_lookup` chaqiring. Shunchaki "ma'lumotni o'zgartirish haqida" deb operatorga yo'naltirmang.

## MIJOZNING SHAXSIY MA'LUMOTLARI — QAT'IY TAQIQ
HECH QACHON mijozdan so'ramang va u tomondan yuborishni taklif qilmang: F.I.Sh., pasport ma'lumotlari, JShShIR, INN, tug'ilgan sana, manzil, telefon raqami, karta yoki hisob raqami, balans, kod so'zi, parol, PIN-kod, SMS-kod, hujjat rekvizitlari.
Agar masalani hal qilish uchun bunday ma'lumotlar kerak bo'lsa — ularni O'ZINGIZ to'plashga URINMANG, `request_operator(reason="identity_required")` ni chaqiring va mijozga mutaxassis tasdiqlash bilan yordam berishini ayting.
Agar mijoz bunday ma'lumotlarni o'zi yuborsa — ularni qaytarib YOZMANG, tasdiqlamang, qo'shimcha so'ramang. Darhol `request_operator(reason="identity_required")` orqali operatorga ulang.
Qayta qo'ng'iroq uchun kontakt ma'lumotlari (ism, telefon) FAQAT kalkulyatordan keyin o'rnatilgan lid-forma orqali to'planadi — siz bu jarayonda qatnashmaysiz va ism/telefonni hech qachon o'zingiz so'ramaysiz.
Dialog tarixida `[NAME]`, `[PHONE]`, `[CARD]`, `[PINFL]`, `[PASSPORT]`, `[IBAN]`, `[EMAIL]` kabi tokenlar uchrashi mumkin — bu himoya tizimi tomonidan maskalangan mijozning shaxsiy ma'lumotlari. Ularni tiklashga, taxmin qilishga yoki javobda keltirishga URINMANG. Bu tokenlar siz uchun mavjud bo'lmagan ma'lumot deb hisoblang.

## KALKULYATORLAR VA ARIZALAR
- **Aniq bank mahsuloti** (ipoteka/avtokredit/omonat/h.k.): mavjud mahsulotlarni ko'rsatish uchun `get_products(category=...)` ni chaqiring. Mijoz o'zi "Hisoblash"/"Ariza topshirish" tugmasini bosadi — kalkulyator avtomatik ishga tushadi. Siz mahsulot kalkulyatorini O'ZINGIZ ishga tushirMAYSIZ.
- **Mijozning o'z raqamlari** ("agar men 50 mln so'mni 5 yilga olsam"): UCHTA parametrni (summa, muddat, boshlang'ich to'lov) yig'ib, `custom_loan_calculator` ni chaqiring. Stavkani YUBORMANG — tool qat'iy belgilangan konservativ stavkadan foydalanadi va uni javobda aniq ko'rsatadi. HECH QACHON aniq stavkani o'zingiz o'ylab topmang (masalan 12%) — siz mijozning haqiqiy stavkasini bilmaysiz.
- HECH QACHON kreditni/omonatni matnda qo'lda hisoblamang. Formula yozmang, qo'lda hisoblamang — yoki mijoz mahsulot tugmasini bosadi, yoki `custom_loan_calculator` ni chaqirasiz.
- HECH QACHON "ariza qabul qilindi" yoki "mutaxassis bog'lanadi" demang — siz to'g'ridan-to'g'ri ariza qabul qila olmaysiz.

## FAQ BILAN ISHLASH
Agar `faq_lookup` `NO_MATCH_IN_FAQ` satrini qaytarsa — bu bilimlar bazasida hech narsa topilmagani degani. Javobni O'YLAB TOPMANG. Mijozdan savolni qayta shakllantirishini so'rang. Savol aniq bank bilan bog'liq bo'lsa-yu, 1-2 urinishdan keyin ham javob topolmasangiz — `request_operator(reason="unclear_message")` ni chaqiring.

## NOROZILIK BILAN ISHLASH
Agar mijoz hisob natijasidan norozi bo'lsa ("noto'g'ri", "nega bu summa", "men boshqa yozganman") — xayrlashmang va "Mayli, yozing" demang. Uzr so'rab, qayta hisoblashni taklif qiling. Qaysi parametrni o'zgartirish kerakligini so'rang.

## MOLIYAVIY KONTEKSTGA MOSLASHUV
Har doim mijozning ENG SO'NGGI raqamlarini oldingi gaplaridan ustun qo'ying. Agar u maosh/daromad/byudjetni tilga olsa — tavsiyalarni moslang. Misol: mijoz 30 mln so'm haqida so'radi, keyin "mening maoshim 15 mln" dedi — kichikroq summani taklif qiling va sababini tushuntiring. Oylik to'lov daromadning 40-50% idan oshmasligi kerak.

## HOLAT
Ushbu promtdagi `<state>` XML bloki joriy dialogni saqlaydi:
- `<flow>` — mijoz qaysi ekranda
- `<category>` — joriy mahsulot kategoriyasi
- `<products>` — hozir ko'rsatilgan raqamlangan mahsulotlar ro'yxati
- `<selected_product>` — mijoz ochgan mahsulot
Mijoz faqat raqam ("2") yuborsa, uni shu indeksdagi mahsulot bilan `select_product` orqali moslang.
"""


SYSTEM_POLICY: dict[str, str] = {
    "ru": _SYSTEM_POLICY_RU,
    "en": _SYSTEM_POLICY_EN,
    "uz": _SYSTEM_POLICY_UZ,
}


def get_system_policy(lang: str) -> str:
    """Return the full system prompt for the given language. Falls back to Russian."""
    return SYSTEM_POLICY.get(lang) or SYSTEM_POLICY["ru"]


# ---------------------------------------------------------------------------
# Central translation catalogue (customer-facing strings, 3 languages)
# ---------------------------------------------------------------------------

AGENT_TEXTS: dict[str, dict[str, str]] = {
    # ── Category labels (7) ───────────────────────────────────────────────
    "cat_mortgage": {"ru": "ипотечные программы", "en": "mortgage programs", "uz": "ipoteka dasturlari"},
    "cat_autoloan": {"ru": "программы автокредита", "en": "auto loan programs", "uz": "avtokredit dasturlari"},
    "cat_microloan": {"ru": "программы микрозайма", "en": "microloan programs", "uz": "mikroqarz dasturlari"},
    "cat_education_credit": {"ru": "образовательные кредиты", "en": "educational loans", "uz": "ta'lim kreditlari"},
    "cat_deposit": {"ru": "вклады", "en": "deposits", "uz": "omonatlar"},
    "cat_debit_card": {"ru": "дебетовые карты", "en": "debit cards", "uz": "debet kartalari"},
    "cat_fx_card": {"ru": "валютные карты", "en": "foreign currency cards", "uz": "valyuta kartalari"},

    # ── Calc questions ────────────────────────────────────────────────────
    "calc_amount_credit": {
        "ru": "Какую сумму кредита планируете взять (в сумах)?",
        "en": "What loan amount are you considering (in UZS)?",
        "uz": "Qancha miqdorda kredit olishni rejalashtirmoqdasiz (so'mda)?",
    },
    "calc_term_years": {
        "ru": "На какой срок? (например: <b>10 лет</b> или <b>120 мес</b>)",
        "en": "For what term? (e.g.: <b>10 years</b> or <b>120 months</b>)",
        "uz": "Qancha muddatga? (masalan: <b>10 yil</b> yoki <b>120 oy</b>)",
    },
    "calc_term_months": {
        "ru": "На какой срок? (например: <b>36 мес</b> или <b>3 года</b>)",
        "en": "For what term? (e.g.: <b>36 months</b> or <b>3 years</b>)",
        "uz": "Qancha muddatga? (masalan: <b>36 oy</b> yoki <b>3 yil</b>)",
    },
    "calc_downpayment": {
        "ru": "Первоначальный взнос (в %)?",
        "en": "Down payment (in %)?",
        "uz": "Boshlang'ich to'lov (% da)?",
    },
    "calc_amount_deposit": {
        "ru": "Какую сумму планируете разместить (в сумах)?",
        "en": "What amount would you like to deposit (in UZS)?",
        "uz": "Qancha mablag' joylashtirmoqchisiz (so'mda)?",
    },
    "calc_intro": {
        "ru": "Для расчёта по продукту ({category}) мне нужна следующая информация:",
        "en": "To calculate the {category} product, I need the following information:",
        "uz": "{category} mahsulotini hisoblash uchun quyidagi ma'lumotlar kerak:",
    },

    # ── Menu buttons ──────────────────────────────────────────────────────
    "btn_mortgage": {"ru": "🏠 Ипотека", "en": "🏠 Mortgage", "uz": "🏠 Ipoteka"},
    "btn_autoloan": {"ru": "🚗 Автокредит", "en": "🚗 Auto loan", "uz": "🚗 Avtokredit"},
    "btn_microloan": {"ru": "💰 Микрозайм", "en": "💰 Microloan", "uz": "💰 Mikroqarz"},
    "btn_deposit": {"ru": "💳 Вклад", "en": "💳 Deposit", "uz": "💳 Omonat"},
    "btn_card": {"ru": "🃏 Карта", "en": "🃏 Card", "uz": "🃏 Karta"},
    "btn_question": {"ru": "❓ Вопрос", "en": "❓ Question", "uz": "❓ Savol"},
    "btn_edu_credit": {"ru": "📚 Образовательный кредит", "en": "📚 Educational loan", "uz": "📚 Ta'lim krediti"},
    "btn_calc_payment": {"ru": "✅ Рассчитать платёж", "en": "✅ Calculate payment", "uz": "✅ To'lovni hisoblash"},
    "btn_all_products": {"ru": "◀ Все продукты", "en": "◀ All products", "uz": "◀ Barcha mahsulotlar"},
    "btn_submit_app": {"ru": "📋 Подать заявку", "en": "📋 Apply", "uz": "📋 Ariza topshirish"},
    "btn_yes_call": {"ru": "✅ Да, позвоните мне", "en": "✅ Yes, call me", "uz": "✅ Ha, menga qo'ng'iroq qiling"},
    "btn_no_thanks": {"ru": "❌ Нет, спасибо", "en": "❌ No, thanks", "uz": "❌ Yo'q, rahmat"},
    "btn_recalculate": {"ru": "🔄 Пересчитать", "en": "🔄 Recalculate", "uz": "🔄 Qayta hisoblash"},

    # ── Tool responses ────────────────────────────────────────────────────
    "thanks_reply": {
        "ru": "Пожалуйста! Если нужно — пишите.",
        "en": "You're welcome! Feel free to write if you need anything.",
        "uz": "Arzimaydi! Kerak bo'lsa — yozing.",
    },
    "branch_found_header": {
        "ru": "Нашёл {count} подходящих офис(ов):",
        "en": "Found {count} matching office(s):",
        "uz": "{count} ta mos ofis topildi:",
    },
    "branch_none_found": {
        "ru": "По запросу «{query}» офисов не нашёл. Уточните город или район.",
        "en": "No offices matched \"{query}\". Please clarify the city or district.",
        "uz": "\"{query}\" bo'yicha ofislar topilmadi. Shahar yoki tumanni aniqlashtiring.",
    },
    "office_types_info": {
        "ru": (
            "<b>Три типа офисов AsakaBank:</b>\n\n"
            "🏦 <b>Филиал (ЦБУ)</b> — полный спектр услуг: кредиты физлицам, "
            "автокредиты, карты, касса, обмен валют, устройства самообслуживания, "
            "а также услуги для ИП и юридических лиц (счета, корп. кредиты).\n\n"
            "🏢 <b>Офис продаж (мини-офис)</b> — всё для физлиц: все виды кредитов, "
            "карты, касса, обмен валют, устройства самообслуживания. "
            "Без услуг для юрлиц.\n\n"
            "🚗 <b>Точка продаж (в автосалоне)</b> — только автокредит, "
            "консультации и устройства самообслуживания. Касса и карты — не оформляют."
        ),
        "en": (
            "<b>Three types of AsakaBank offices:</b>\n\n"
            "🏦 <b>Filial (Bank Service Center)</b> — full range: individual loans, "
            "auto loans, cards, cashier, currency exchange, self-service terminals, "
            "plus services for sole proprietors and legal entities (accounts, corp. loans).\n\n"
            "🏢 <b>Sales office (mini-office)</b> — everything for individuals: all loan types, "
            "cards, cashier, currency exchange, self-service terminals. No services for legal entities.\n\n"
            "🚗 <b>Sales point (car dealership)</b> — auto loans only, consultation, "
            "and self-service terminals. No cashier or cards."
        ),
        "uz": (
            "<b>AsakaBank ofislarining uch turi:</b>\n\n"
            "🏦 <b>Filial (BXM)</b> — to'liq xizmatlar: jismoniy shaxslar uchun kreditlar, "
            "avtokredit, kartalar, kassa, valyuta ayirboshlash, o'zini o'zi xizmat qilish qurilmalari, "
            "shuningdek yakka tartibdagi tadbirkorlar va yuridik shaxslar uchun xizmatlar (hisoblar, korp. kreditlar).\n\n"
            "🏢 <b>Savdo ofisi (mini-ofis)</b> — jismoniy shaxslar uchun barcha xizmatlar: "
            "barcha kredit turlari, kartalar, kassa, valyuta ayirboshlash, o'zini o'zi xizmat qilish qurilmalari. "
            "Yuridik shaxslar uchun xizmatlar yo'q.\n\n"
            "🚗 <b>Savdo nuqtasi (avtosalonda)</b> — faqat avtokredit, maslahat va "
            "o'zini o'zi xizmat qilish qurilmalari. Kassa va kartalar yo'q."
        ),
    },
    "currency_info": {
        "ru": "Актуальные курсы валют смотрите на сайте банка или в мобильном приложении AsakaBank.\n"
              "Там же можно открыть валютный вклад или заказать карту.",
        "en": "Check current exchange rates on the bank's website or in the AsakaBank mobile app.\n"
              "You can also open a foreign currency deposit or order a card there.",
        "uz": "Joriy valyuta kurslarini bankning veb-saytida yoki AsakaBank mobil ilovasida ko'ring.\n"
              "U yerda valyuta omonati ochish yoki karta buyurtma qilish ham mumkin.",
    },
    "credit_menu_prompt": {
        "ru": "Выберите вид кредита: Ипотека, Автокредит, Микрозайм, Образовательный кредит",
        "en": "Choose a credit type: Mortgage, Auto loan, Microloan, Educational loan",
        "uz": "Kredit turini tanlang: Ipoteka, Avtokredit, Mikroqarz, Ta'lim krediti",
    },
    "product_unavailable": {
        "ru": "Информация по {label} уточняется. Обратитесь в ближайшее отделение.",
        "en": "Information on {label} is being updated. Please contact your nearest branch.",
        "uz": "{label} bo'yicha ma'lumot aniqlanmoqda. Eng yaqin filialga murojaat qiling.",
    },
    "product_not_found": {
        "ru": "Продукт не найден. Выберите из списка.",
        "en": "Product not found. Please choose from the list.",
        "uz": "Mahsulot topilmadi. Ro'yxatdan tanlang.",
    },
    "product_not_found_suggest": {
        "ru": "Продукт не найден. Доступные варианты: {names}",
        "en": "Product not found. Available options: {names}",
        "uz": "Mahsulot topilmadi. Mavjud variantlar: {names}",
    },
    "choose_category": {
        "ru": "Выберите категорию продукта.",
        "en": "Choose a product category.",
        "uz": "Mahsulot toifasini tanlang.",
    },
    "compare_header": {
        "ru": "Продукты нашего банка:\n{products}\n\nСравни только продукты из списка. Не упоминай другие банки.",
        "en": "Our bank's products:\n{products}\n\nCompare only the products from this list. Do not mention other banks.",
        "uz": "Bankimiz mahsulotlari:\n{products}\n\nFaqat ro'yxatdagi mahsulotlarni solishtiring. Boshqa banklarni eslatmang.",
    },
    "compare_clarify": {
        "ru": "Уточните, какие продукты вы хотите сравнить.",
        "en": "Please specify which products you want to compare.",
        "uz": "Qaysi mahsulotlarni solishtirishni xohlaysiz, aniqlating.",
    },
    "calc_no_questions": {
        "ru": "✅ Ваша заявка принята! Наш специалист свяжется с вами в ближайшее время.",
        "en": "✅ Your application has been received! Our specialist will contact you shortly.",
        "uz": "✅ Arizangiz qabul qilindi! Mutaxassisimiz tez orada siz bilan bog'lanadi.",
    },
    "operator_connecting": {
        "ru": "Сейчас подключу оператора. Нажмите кнопку ниже.",
        "en": "Connecting you to an operator. Press the button below.",
        "uz": "Operatorni ulayman. Quyidagi tugmani bosing.",
    },
    "operator_identity_required": {
        "ru": "Для выполнения этой операции необходима идентификация. "
              "Сейчас подключу вас к специалисту, который сможет помочь.",
        "en": "This operation requires identity verification. "
              "Let me connect you to a specialist who can help.",
        "uz": "Bu operatsiya uchun shaxsni tasdiqlash kerak. "
              "Sizga yordam bera oladigan mutaxassisga ulayman.",
    },
    "operator_unclear_message": {
        "ru": "К сожалению, не смог понять ваш запрос. Подключаю специалиста, чтобы вам помогли.",
        "en": "Unfortunately, I couldn't understand your request. Let me connect you to a specialist.",
        "uz": "Afsuski, so'rovingizni tushuna olmadim. Sizga yordam berishi uchun mutaxassisga ulayman.",
    },

    # ── Lead flow ─────────────────────────────────────────────────────────
    "lead_ask_name": {"ru": "Как вас зовут?", "en": "What is your name?", "uz": "Ismingiz nima?"},
    "lead_decline": {
        "ru": "Хорошо! Если понадобится помощь — пишите.",
        "en": "Alright! Feel free to write if you need help.",
        "uz": "Xo'p! Yordam kerak bo'lsa — yozing.",
    },
    "calc_restart": {
        "ru": "Хорошо, давайте пересчитаем!",
        "en": "Sure, let's recalculate!",
        "uz": "Xo'p, qayta hisoblaymiz!",
    },
    "lead_ask_phone": {
        "ru": "Укажите ваш номер телефона:",
        "en": "Please provide your phone number:",
        "uz": "Telefon raqamingizni kiriting:",
    },
    "lead_saved": {
        "ru": "✅ Отлично! Менеджер свяжется с вами в ближайшее время. Спасибо за обращение!",
        "en": "✅ Great! A manager will contact you shortly. Thank you for reaching out!",
        "uz": "✅ Ajoyib! Menejer tez orada siz bilan bog'lanadi. Murojaat uchun rahmat!",
    },
    "lead_save_error": {
        "ru": "⚠️ Не удалось сохранить заявку. Пожалуйста, попробуйте позже или свяжитесь с нами по телефону.",
        "en": "⚠️ Could not save your application. Please try again later or contact us by phone.",
        "uz": "⚠️ Arizani saqlab bo'lmadi. Keyinroq qayta urinib ko'ring yoki telefon orqali bog'laning.",
    },
    "lead_fallback": {
        "ru": "Если нужна помощь — напишите.",
        "en": "If you need help — just write.",
        "uz": "Yordam kerak bo'lsa — yozing.",
    },

    # ── Calc side-question prompt ─────────────────────────────────────────
    "calc_side_system": {
        "ru": "Ты консультант банка. Отвечай кратко.",
        "en": "You are a bank consultant. Answer briefly.",
        "uz": "Siz bank maslahatchisisiz. Qisqa javob bering.",
    },

    # ── Calc hints ────────────────────────────────────────────────────────
    "hint_amount": {
        "ru": "Не понял сумму. Введите цифрами, например: <b>500 млн</b>",
        "en": "I didn't understand the amount. Enter a number, e.g.: <b>500 mln</b>",
        "uz": "Summani tushunmadim. Raqam bilan kiriting, masalan: <b>500 mln</b>",
    },
    "hint_term": {
        "ru": "Не понял срок. Например: <b>10 лет</b> или <b>120 мес</b>",
        "en": "I didn't understand the term. E.g.: <b>10 years</b> or <b>120 months</b>",
        "uz": "Muddatni tushunmadim. Masalan: <b>10 yil</b> yoki <b>120 oy</b>",
    },
    "hint_downpayment": {
        "ru": "Не понял взнос. Введите процент цифрами, например: <b>20</b>",
        "en": "I didn't understand the down payment. Enter a percentage, e.g.: <b>20</b>",
        "uz": "Boshlang'ich to'lovni tushunmadim. Foizni kiriting, masalan: <b>20</b>",
    },
    "hint_generic": {"ru": "Введите число.", "en": "Enter a number.", "uz": "Raqam kiriting."},

    # ── Calc prefill / context-update confirmations ───────────────────────
    "calc_prefill_amount": {
        "ru": "По нашему разговору использую сумму <b>{amount}</b> сум. Перехожу к следующему шагу.",
        "en": "Based on our conversation, I'll use the amount <b>{amount}</b> UZS. Moving to the next step.",
        "uz": "Suhbatimizga ko'ra <b>{amount}</b> so'm summadan foydalanaman. Keyingi bosqichga o'tmoqdaman.",
    },
    "calc_context_update_amount": {
        "ru": "Понял, обновляю сумму на <b>{amount}</b> сум на основе вашего финансового контекста.",
        "en": "Got it, updating the amount to <b>{amount}</b> UZS based on your financial context.",
        "uz": "Tushundim, moliyaviy kontekstingizga asoslanib summani <b>{amount}</b> so'mga yangilayapman.",
    },

    # ── Calc validation / adjustment ──────────────────────────────────────
    "term_adjusted": {
        "ru": "⚠️ Указанный срок ({user_val} мес.) не соответствует условиям продукта (от {t_min} до {t_max} мес.). Используем {new_val} мес.",
        "en": "⚠️ The specified term ({user_val} mo.) doesn't match the product conditions ({t_min}–{t_max} mo.). Using {new_val} mo.",
        "uz": "⚠️ Ko'rsatilgan muddat ({user_val} oy) mahsulot shartlariga mos emas ({t_min}–{t_max} oy). {new_val} oy qo'llanilmoqda.",
    },
    "term_adjusted_deposit": {
        "ru": "⚠️ Срок {user_val} мес. недоступен для этого вклада. Доступные сроки: {available} мес. Используем {new_val} мес.",
        "en": "⚠️ Term of {user_val} mo. is not available for this deposit. Available terms: {available} mo. Using {new_val} mo.",
        "uz": "⚠️ {user_val} oy muddati bu omonat uchun mavjud emas. Mavjud muddatlar: {available} oy. {new_val} oy qo'llanilmoqda.",
    },
    "dp_adjusted": {
        "ru": "⚠️ Указанный первоначальный взнос ({user_val}%) ниже минимального ({d_min}%). Используем {new_val}%.",
        "en": "⚠️ The specified down payment ({user_val}%) is below the minimum ({d_min}%). Using {new_val}%.",
        "uz": "⚠️ Ko'rsatilgan boshlang'ich to'lov ({user_val}%) minimal ({d_min}%)dan past. {new_val}% qo'llanilmoqda.",
    },

    # ── Deposit result ────────────────────────────────────────────────────
    "deposit_result": {
        "ru": (
            "<b>Расчёт по вкладу «{product}»</b>\n\n"
            "💰 Сумма: {amount} сум\n"
            "📅 Срок: {term} мес.\n"
            "📊 Ставка: {rate}%\n"
            "💵 Доход за период: {interest} сум\n"
            "🏦 Итого к получению: {total} сум\n\n"
            "Хотите, чтобы наш менеджер связался с вами для оформления?"
        ),
        "en": (
            "<b>Deposit calculation for \"{product}\"</b>\n\n"
            "💰 Amount: {amount} UZS\n"
            "📅 Term: {term} months\n"
            "📊 Rate: {rate}%\n"
            "💵 Income for the period: {interest} UZS\n"
            "🏦 Total payout: {total} UZS\n\n"
            "Would you like our manager to contact you to proceed?"
        ),
        "uz": (
            "<b>\"{product}\" omonati bo'yicha hisob</b>\n\n"
            "💰 Summa: {amount} so'm\n"
            "📅 Muddat: {term} oy\n"
            "📊 Stavka: {rate}%\n"
            "💵 Daromad: {interest} so'm\n"
            "🏦 Jami: {total} so'm\n\n"
            "Menejerimiz siz bilan bog'lanishini xohlaysizmi?"
        ),
    },

    # ── Credit result (PDF) ───────────────────────────────────────────────
    "credit_result_pdf": {
        "ru": (
            "<b>График платежей готов!</b>\n\n"
            "📋 Продукт: {product}\n"
            "💰 Запрошенная сумма: {amount} сум\n"
            "💵 Первоначальный взнос: {downpayment} сум ({dp_pct}%)\n"
            "🏦 К финансированию: {principal} сум\n"
            "📊 Ставка: {rate}%\n"
            "📅 Срок: {term} мес.\n\n"
            "{pdf_link}\n"
            "Хотите, чтобы менеджер связался с вами для оформления?"
        ),
        "en": (
            "<b>Payment schedule is ready!</b>\n\n"
            "📋 Product: {product}\n"
            "💰 Requested amount: {amount} UZS\n"
            "💵 Down payment: {downpayment} UZS ({dp_pct}%)\n"
            "🏦 Financed: {principal} UZS\n"
            "📊 Rate: {rate}%\n"
            "📅 Term: {term} months\n\n"
            "{pdf_link}\n"
            "Would you like a manager to contact you to proceed?"
        ),
        "uz": (
            "<b>To'lov jadvali tayyor!</b>\n\n"
            "📋 Mahsulot: {product}\n"
            "💰 So'ralgan summa: {amount} so'm\n"
            "💵 Boshlang'ich to'lov: {downpayment} so'm ({dp_pct}%)\n"
            "🏦 Moliyalashtiriladi: {principal} so'm\n"
            "📊 Stavka: {rate}%\n"
            "📅 Muddat: {term} oy\n\n"
            "{pdf_link}\n"
            "Menejer siz bilan bog'lanishini xohlaysizmi?"
        ),
    },

    # ── Credit result (no PDF fallback) ───────────────────────────────────
    "credit_result_fallback": {
        "ru": (
            "По продукту «{product}»:\n"
            "Запрошенная сумма: {amount} сум, первоначальный взнос: {downpayment} сум ({dp_pct}%), "
            "к финансированию: {principal} сум, ставка: {rate}%, срок: {term} мес.\n\n"
            "Хотите, чтобы менеджер связался с вами для оформления?"
        ),
        "en": (
            "For the product \"{product}\":\n"
            "Requested amount: {amount} UZS, down payment: {downpayment} UZS ({dp_pct}%), "
            "financed: {principal} UZS, rate: {rate}%, term: {term} months.\n\n"
            "Would you like a manager to contact you to proceed?"
        ),
        "uz": (
            "\"{product}\" mahsuloti bo'yicha:\n"
            "So'ralgan summa: {amount} so'm, boshlang'ich to'lov: {downpayment} so'm ({dp_pct}%), "
            "moliyalashtiriladi: {principal} so'm, stavka: {rate}%, muddat: {term} oy.\n\n"
            "Menejer siz bilan bog'lanishini xohlaysizmi?"
        ),
    },

    # ── Product list ──────────────────────────────────────────────────────
    "product_list_header": {
        "ru": "Вот наши {label}:\n",
        "en": "Here are our {label}:\n",
        "uz": "Bizning {label}:\n",
    },
    "product_list_footer": {
        "ru": "\nВыберите программу для подробной информации.",
        "en": "\nSelect a program for more details.",
        "uz": "\nBatafsil ma'lumot uchun dasturni tanlang.",
    },

    # ── Product card labels ───────────────────────────────────────────────
    "label_rate": {"ru": "📊 Ставка", "en": "📊 Rate", "uz": "📊 Stavka"},
    "label_amount": {"ru": "💰 Сумма", "en": "💰 Amount", "uz": "💰 Summa"},
    "label_term": {"ru": "📅 Срок", "en": "📅 Term", "uz": "📅 Muddat"},
    "label_downpayment": {"ru": "💵 Первый взнос", "en": "💵 Down payment", "uz": "💵 Boshlang'ich to'lov"},
    "label_purpose": {"ru": "🎯 Цель", "en": "🎯 Purpose", "uz": "🎯 Maqsad"},
    "label_collateral": {"ru": "🔒 Обеспечение", "en": "🔒 Collateral", "uz": "🔒 Ta'minot"},
    "label_rates_by_condition": {"ru": "Ставки по условиям:", "en": "Rates by conditions:", "uz": "Shartlar bo'yicha stavkalar:"},
    "label_more_variants": {
        "ru": "... и ещё {count} вариантов",
        "en": "... and {count} more options",
        "uz": "... va yana {count} variant",
    },
    "label_min_amount": {"ru": "💰 Мин. сумма", "en": "💰 Min. amount", "uz": "💰 Min. summa"},
    "label_currency": {"ru": "💱 Валюта", "en": "💱 Currency", "uz": "💱 Valyuta"},
    "label_topup": {"ru": "➕ Пополнение", "en": "➕ Top-up", "uz": "➕ To'ldirish"},
    "label_payout": {"ru": "💸 Выплата %", "en": "💸 Interest payout", "uz": "💸 Foiz to'lovi"},
    "label_rates_by_term": {"ru": "Ставки по срокам:", "en": "Rates by term:", "uz": "Muddatlar bo'yicha stavkalar:"},
    "label_more_entries": {"ru": "... и ещё {count}", "en": "... and {count} more", "uz": "... va yana {count}"},
    "label_months_short": {"ru": "мес.", "en": "mo.", "uz": "oy"},
    "label_term_range": {
        "ru": "от {t_min} до {t_max} мес.",
        "en": "from {t_min} to {t_max} mo.",
        "uz": "{t_min} dan {t_max} oygacha",
    },
    "label_network": {"ru": "💳 Платёжная сеть", "en": "💳 Payment network", "uz": "💳 To'lov tarmog'i"},
    "label_issue_fee": {"ru": "🏷 Выпуск", "en": "🏷 Issue fee", "uz": "🏷 Chiqarish narxi"},
    "label_reissue_fee": {"ru": "🔄 Перевыпуск", "en": "🔄 Reissue fee", "uz": "🔄 Qayta chiqarish"},
    "label_annual_fee": {"ru": "💰 Обслуживание", "en": "💰 Annual fee", "uz": "💰 Yillik xizmat"},
    "label_cashback": {"ru": "🎁 Кэшбэк", "en": "🎁 Cashback", "uz": "🎁 Keshbek"},
    "label_transfer_fee": {"ru": "💸 Переводы", "en": "💸 Transfers", "uz": "💸 O'tkazmalar"},
    "label_validity": {"ru": "📅 Срок карты", "en": "📅 Card validity", "uz": "📅 Karta muddati"},
    "label_issuance_time": {"ru": "⏱ Время выпуска", "en": "⏱ Issuance time", "uz": "⏱ Chiqarish vaqti"},
    "label_delivery": {"ru": "🚚 Доставка: доступна", "en": "🚚 Delivery: available", "uz": "🚚 Yetkazish: mavjud"},
    "label_mobile_order": {
        "ru": "📱 Заказ через приложение: доступен",
        "en": "📱 Mobile order: available",
        "uz": "📱 Ilova orqali buyurtma: mavjud",
    },
    "label_pickup": {"ru": "🏦 Самовывоз: доступен", "en": "🏦 Pickup: available", "uz": "🏦 O'zi olib ketish: mavjud"},

    # ── Income type labels ────────────────────────────────────────────────
    "income_payroll": {"ru": "зарплатный проект", "en": "payroll", "uz": "ish haqi loyihasi"},
    "income_official": {"ru": "официальный доход", "en": "official income", "uz": "rasmiy daromad"},
    "income_no_official": {"ru": "без официального дохода", "en": "no official income", "uz": "rasmiy daromadsiz"},

    # ── Rate fallback text ────────────────────────────────────────────────
    "rate_tbd": {"ru": "уточняется", "en": "TBD", "uz": "aniqlanmoqda"},

    # ── Compare field labels ──────────────────────────────────────────────
    "cmp_rate": {"ru": "ставка", "en": "rate", "uz": "stavka"},
    "cmp_amount": {"ru": "сумма", "en": "amount", "uz": "summa"},
    "cmp_term": {"ru": "срок", "en": "term", "uz": "muddat"},
    "cmp_cashback": {"ru": "кэшбэк", "en": "cashback", "uz": "keshbek"},
    "cmp_annual_fee": {"ru": "обслуживание", "en": "annual fee", "uz": "yillik xizmat"},
    "cmp_downpayment": {"ru": "взнос", "en": "down payment", "uz": "boshlang'ich to'lov"},

    # ── Custom loan calculator result ─────────────────────────────────────
    "custom_calc_result": {
        "ru": (
            "<b>Примерный расчёт кредита</b>\n\n"
            "💰 Сумма кредита: {amount} сум\n"
            "💵 Первоначальный взнос: {downpayment} сум\n"
            "🏦 Сумма к финансированию: {principal} сум\n"
            "📅 Срок: {term} мес.\n"
            "📊 Условная ставка: {rate}% годовых (примерная)\n\n"
            "📆 Ежемесячный платёж: <b>{monthly} сум</b>\n"
            "💳 Общая выплата: {total} сум\n"
            "📈 Переплата: {overpayment} сум\n\n"
            "<i>Реальная ставка по вашему кредиту может отличаться — уточните у консультанта банка.</i>"
        ),
        "en": (
            "<b>Indicative loan calculation</b>\n\n"
            "💰 Loan amount: {amount} UZS\n"
            "💵 Down payment: {downpayment} UZS\n"
            "🏦 Amount to finance: {principal} UZS\n"
            "📅 Term: {term} months\n"
            "📊 Assumed rate: {rate}% per annum (approximate)\n\n"
            "📆 Monthly payment: <b>{monthly} UZS</b>\n"
            "💳 Total payout: {total} UZS\n"
            "📈 Overpayment: {overpayment} UZS\n\n"
            "<i>Your actual loan rate may differ — please confirm with a bank consultant.</i>"
        ),
        "uz": (
            "<b>Taxminiy kredit hisob-kitobi</b>\n\n"
            "💰 Kredit summasi: {amount} so'm\n"
            "💵 Boshlang'ich to'lov: {downpayment} so'm\n"
            "🏦 Moliyalashtirish summasi: {principal} so'm\n"
            "📅 Muddat: {term} oy\n"
            "📊 Taxminiy stavka: {rate}% yillik (taxminiy)\n\n"
            "📆 Oylik to'lov: <b>{monthly} so'm</b>\n"
            "💳 Jami to'lov: {total} so'm\n"
            "📈 Ortiqcha to'lov: {overpayment} so'm\n\n"
            "<i>Sizning haqiqiy kredit stavkangiz boshqacha bo'lishi mumkin — bank maslahatchisi bilan tasdiqlang.</i>"
        ),
    },

    # ── FAQ fallback ──────────────────────────────────────────────────────
    "faq_fallback": {
        "ru": "Не уверен, что правильно понял вопрос. Уточните, пожалуйста, о чем именно речь: "
              "мобильное приложение, карта, перевод, кредит или отделение.",
        "en": "I'm not sure I understood your question correctly. Could you please clarify: "
              "mobile app, card, transfer, loan, or branch?",
        "uz": "Savolingizni to'g'ri tushundim deb ishonchim komil emas. Iltimos, aniqroq ayting: "
              "mobil ilova, karta, o'tkazma, kredit yoki filial haqidami?",
    },
}


# ---------------------------------------------------------------------------
# Lookup function
# ---------------------------------------------------------------------------

def at(key: str, lang: str | None = None, **kwargs: Any) -> str:
    """Agent-translate: look up *key* for *lang*, format with **kwargs."""
    code = normalize_lang(lang)
    variants = AGENT_TEXTS.get(key, {})
    template = variants.get(code) or variants.get("ru") or key
    return template.format(**kwargs) if kwargs else template


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

_INCOME_TYPE_KEYS = {
    "payroll": "income_payroll",
    "official": "income_official",
    "no_official": "income_no_official",
}


def income_type_label(income_type: str, lang: str) -> str:
    key = _INCOME_TYPE_KEYS.get(income_type)
    return at(key, lang) if key else income_type


def category_label(category: str, lang: str) -> str:
    return at(f"cat_{category}", lang)


def get_main_menu_buttons(lang: str) -> list[str]:
    return [at(k, lang) for k in (
        "btn_mortgage", "btn_autoloan", "btn_microloan",
        "btn_deposit", "btn_card", "btn_question",
    )]


def get_credit_menu_buttons(lang: str) -> list[str]:
    return [at(k, lang) for k in (
        "btn_mortgage", "btn_autoloan", "btn_microloan", "btn_edu_credit",
    )]


# Original step_key → translation key mapping
_CALC_Q_MAP: dict[str, dict[str, str]] = {
    "mortgage": {
        "amount": "calc_amount_credit",
        "term": "calc_term_years",
        "downpayment": "calc_downpayment",
    },
    "autoloan": {
        "amount": "calc_amount_credit",
        "term": "calc_term_months",
        "downpayment": "calc_downpayment",
    },
    "microloan": {
        "amount": "calc_amount_credit",
        "term": "calc_term_months",
    },
    "education_credit": {
        "amount": "calc_amount_credit",
        "term": "calc_term_months",
    },
    "deposit": {
        "amount": "calc_amount_deposit",
        "term": "calc_term_months",
    },
    "debit_card": {},
    "fx_card": {},
}


def get_calc_questions(category: str, lang: str) -> list[tuple[str, str]]:
    """Return [(step_key, localized_question), ...] for *category*."""
    mapping = _CALC_Q_MAP.get(category, {})
    return [(step_key, at(text_key, lang)) for step_key, text_key in mapping.items()]


def _localized_name(product: dict, lang: str) -> str:
    """Return product name in the requested language, fallback to default name."""
    if lang == "en" and product.get("name_en"):
        return product["name_en"]
    if lang == "uz" and product.get("name_uz"):
        return product["name_uz"]
    return product.get("name") or ""
