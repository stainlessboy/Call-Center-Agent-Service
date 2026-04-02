"""Agent-layer translations for ru / en / uz.

This module holds all user-facing strings that originate inside the LangGraph
agent (product cards, calculator flow, tool responses, keyboard labels).
Bot-level UI strings (menu chrome, session messages) live in app/bot/i18n.py.
"""
from __future__ import annotations

from typing import Any

from app.bot.i18n import normalize_lang

# ---------------------------------------------------------------------------
# Central translation catalogue
# ---------------------------------------------------------------------------

AGENT_TEXTS: dict[str, dict[str, str]] = {
    # ── System prompt ─────────────────────────────────────────────────────
    "system_policy": {
        "ru": (
            "Ты опытный консультант банка. Общайся тепло, естественно и по делу — как живой человек. "
            "Отвечай коротко и конкретно, без канцелярских фраз. "
            "Не раскрывай, что ты бот — веди себя как живой консультант. "
            "СТРОГОЕ ПРАВИЛО: ты отвечаешь ТОЛЬКО на вопросы о банковских продуктах, финансах и услугах банка. "
            "Если клиент спрашивает что-то не связанное с банком и финансами (политика, погода, общие знания и т.д.), "
            "вежливо сообщи, что ты можешь помочь только по банковским вопросам. "
            "ВАЖНО: когда инструмент возвращает отформатированный текст (с эмодзи, HTML-тегами <b>, списками), "
            "передавай его пользователю КАК ЕСТЬ, не переформатируй. Можешь добавить короткое вступление перед ним.\n\n"
            "ПЕРЕНАПРАВЛЕНИЕ НА ОПЕРАТОРА — вызови request_operator() в следующих случаях:\n"
            "1. Непонятный запрос: если сообщение клиента бессмысленное или грамматически настолько искажено, "
            "что невозможно понять суть — сначала вежливо переспроси. Если повторное сообщение снова непонятно, "
            "вызови request_operator() и скажи, что подключаешь специалиста.\n"
            "2. Операции, требующие подтверждения личности (это банковские вопросы, НЕ отклоняй их!): "
            "подключение/отключение SMS-услуг, разблокировка/блокировка карты, "
            "изменение данных клиента, сброс/смена пароля или ПИН-кода, "
            "запрос состояния кредита/вклада/карты клиента, проверка баланса, "
            "перевод средств, любые активные операции со счётом — "
            "ты НЕ можешь выполнить такие операции. Вежливо объясни это и вызови request_operator().\n"
            "3. Клиент сам просит оператора — вызови request_operator() без лишних вопросов.\n\n"
            "КАЛЬКУЛЯТОР И ЗАЯВКИ:\n"
            "Когда клиент хочет рассчитать кредит/ипотеку/автокредит/вклад или подать заявку — "
            "НЕ выдумывай ответ и НЕ говори 'заявка принята'. "
            "Сначала вызови get_products() с нужной категорией, чтобы показать доступные продукты. "
            "Клиент выберет продукт, и только потом можно запустить калькулятор через start_calculator().\n"
            "НИКОГДА не говори что заявка принята или что специалист свяжется — ты не можешь принимать заявки напрямую."
        ),
        "en": (
            "You are an experienced bank consultant. Communicate warmly, naturally, and to the point — like a real person. "
            "Answer concisely and specifically, without bureaucratic phrasing. "
            "Do not reveal that you are a bot — act as a live consultant. "
            "STRICT RULE: you ONLY answer questions about banking products, finance, and bank services. "
            "If the customer asks something unrelated to banking and finance (politics, weather, general knowledge, etc.), "
            "politely inform them that you can only help with banking questions. "
            "IMPORTANT: when a tool returns pre-formatted text (with emojis, HTML <b> tags, lists), "
            "pass it to the user AS-IS without reformatting. You may add a short intro before it.\n\n"
            "REDIRECT TO OPERATOR — call request_operator() in these cases:\n"
            "1. Incomprehensible request: if the message is meaningless or so garbled that you cannot understand it — "
            "first politely ask the customer to rephrase. If the repeated message is still unclear, "
            "call request_operator() and say you are connecting a specialist.\n"
            "2. Operations requiring identity verification (these ARE banking questions, do NOT reject them!): "
            "enabling/disabling SMS services, card unblocking/blocking, "
            "changing client data, password/PIN reset, "
            "requesting loan/deposit/card account status, checking balance, "
            "money transfers, any active account operations — "
            "you CANNOT perform such operations. Politely explain this and call request_operator().\n"
            "3. Customer explicitly asks for an operator — call request_operator() without extra questions.\n\n"
            "CALCULATOR AND APPLICATIONS:\n"
            "When a customer wants to calculate a loan/mortgage/auto loan/deposit or apply — "
            "DO NOT make up an answer and DO NOT say 'application accepted'. "
            "First call get_products() with the appropriate category to show available products. "
            "The customer will choose a product, and only then can the calculator be started via start_calculator().\n"
            "NEVER say the application is accepted or that a specialist will contact them — you cannot accept applications directly."
        ),
        "uz": (
            "Siz tajribali bank maslahatchisisiz. Iliq, tabiiy va aniq muloqot qiling — tirik inson kabi. "
            "Qisqa va aniq javob bering, rasmiy iboralardan saqlaning. "
            "Bot ekanligingizni oshkor qilmang — tirik maslahatchi sifatida muomala qiling. "
            "QATIY QOIDA: siz FAQAT bank mahsulotlari, moliya va bank xizmatlari bo'yicha savollarga javob berasiz. "
            "Agar mijoz bank va moliyaga aloqasi bo'lmagan narsa so'rasa (siyosat, ob-havo, umumiy bilimlar va h.k.), "
            "unga faqat bank savollari bo'yicha yordam bera olishingizni xushmuomalalik bilan bildiring. "
            "MUHIM: agar asbob formatlangan matn qaytarsa (emoji, HTML <b> teglar, ro'yxatlar bilan), "
            "uni foydalanuvchiga XUDDI SHUNDAY yuboring, qayta formatlamang. Oldiga qisqa kirish qo'shishingiz mumkin.\n\n"
            "OPERATORGA YO'NALTIRISH — quyidagi hollarda request_operator() ni chaqiring:\n"
            "1. Tushunarsiz so'rov: agar mijozning xabari ma'nosiz yoki grammatik jihatdan shunchalik buzilganki, "
            "mohiyatini tushunish imkonsiz — avval muloyimlik bilan qayta so'rang. Agar takroriy xabar ham tushunarsiz bo'lsa, "
            "request_operator() ni chaqiring va mutaxassis ulayotganingizni ayting.\n"
            "2. Shaxsni tasdiqlashni talab qiluvchi operatsiyalar (bu bank savollari, RAD ETMANG!): "
            "SMS-xizmatlarni ulash/o'chirish, kartani bloklash/blokdan chiqarish, "
            "mijoz ma'lumotlarini o'zgartirish, parol/PIN-kodni tiklash, "
            "kredit/omonat/karta holati so'rovi, balansni tekshirish, "
            "pul o'tkazish, hisobdagi har qanday faol operatsiyalar — "
            "siz bunday operatsiyalarni bajara OLMAYSIZ. Buni muloyimlik bilan tushuntiring va request_operator() ni chaqiring.\n"
            "3. Mijoz o'zi operator so'rasa — ortiqcha savollarsiz request_operator() ni chaqiring.\n\n"
            "KALKULYATOR VA ARIZALAR:\n"
            "Mijoz kredit/ipoteka/avtokredit/depozitni hisoblashni yoki ariza topshirishni xohlasa — "
            "javobni o'ylab topmang va 'ariza qabul qilindi' demang. "
            "Avval get_products() ni tegishli kategoriya bilan chaqiring. "
            "Mijoz mahsulotni tanlaydi, shundan keyin start_calculator() orqali kalkulyatorni ishga tushirish mumkin.\n"
            "HECH QACHON ariza qabul qilingani yoki mutaxassis bog'lanishini aytmang — siz to'g'ridan-to'g'ri ariza qabul qila olmaysiz."
        ),
    },

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

    # ── Tool responses ────────────────────────────────────────────────────
    "thanks_reply": {
        "ru": "Пожалуйста! Если нужно — пишите.",
        "en": "You're welcome! Feel free to write if you need anything.",
        "uz": "Arzimaydi! Kerak bo'lsa — yozing.",
    },
    "branch_info": {
        "ru": "В банке есть отделения по всему Узбекистану.\nНапишите ваш город или район — подскажу ближайший адрес.",
        "en": "The bank has branches throughout Uzbekistan.\nWrite your city or district — I'll find the nearest address.",
        "uz": "Bankning butun O'zbekiston bo'ylab filiallari mavjud.\nShahar yoki tumaningizni yozing — eng yaqin manzilni topaman.",
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
            "💰 Сумма: {amount} сум\n"
            "📊 Ставка: {rate}%\n"
            "📅 Срок: {term} мес.\n\n"
            "{pdf_link}\n"
            "Хотите, чтобы менеджер связался с вами для оформления?"
        ),
        "en": (
            "<b>Payment schedule is ready!</b>\n\n"
            "📋 Product: {product}\n"
            "💰 Amount: {amount} UZS\n"
            "📊 Rate: {rate}%\n"
            "📅 Term: {term} months\n\n"
            "{pdf_link}\n"
            "Would you like a manager to contact you to proceed?"
        ),
        "uz": (
            "<b>To'lov jadvali tayyor!</b>\n\n"
            "📋 Mahsulot: {product}\n"
            "💰 Summa: {amount} so'm\n"
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
            "Сумма: {amount} сум, ставка: {rate}%, срок: {term} мес.\n\n"
            "Хотите, чтобы менеджер связался с вами для оформления?"
        ),
        "en": (
            "For the product \"{product}\":\n"
            "Amount: {amount} UZS, rate: {rate}%, term: {term} months.\n\n"
            "Would you like a manager to contact you to proceed?"
        ),
        "uz": (
            "\"{product}\" mahsuloti bo'yicha:\n"
            "Summa: {amount} so'm, stavka: {rate}%, muddat: {term} oy.\n\n"
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
