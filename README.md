# AsakaBank Agent Bot

Telegram-бот банковского консультанта с AI на базе LangGraph + FastAPI + aiogram.

**Домен:** https://agent-bot.uz
**Стек:** Python 3.13, FastAPI, aiogram 3, LangGraph, SQLAlchemy (asyncpg), PostgreSQL 16, SQLAdmin, Nginx, Docker

---

## Возможности

- AI-консультант по банковским продуктам (ипотека, автокредит, вклады, карты)
- Калькулятор платежей с генерацией PDF-графика
- FAQ-поиск по базе знаний
- Мультиязычность: русский, английский, узбекский
- Гибридный режим бот/оператор с передачей диалога живому оператору
- Сравнение продуктов по запросу пользователя
- Захват лидов: имя + телефон → БД
- Админ-панель SQLAdmin на `/admin`
- REST API для операторов (`/operator/send`)
- CI/CD: автодеплой через GitHub Actions при push в `main`

---

## Архитектура

### Общая схема

```
Telegram → POST /telegram/webhook → FastAPI → aiogram Dispatcher
  → handlers/commands.py → ChatService → AgentClient → LangGraph Agent
  → PostgreSQL (asyncpg)
```

```
Internet → Nginx (SSL) :443 → API container :8001
                                    ↓
                              PostgreSQL :5432
```

### Путь сообщения (полный цикл)

Подробная трассировка одного хода — от Telegram до финального ответа.

#### 1. Telegram → FastAPI

`Update` приходит на `POST /telegram/webhook` ([app/api/fastapi_app.py:270-293](app/api/fastapi_app.py#L270-L293)):
- Проверка `X-Telegram-Bot-Api-Secret-Token` (если настроен `WEBHOOK_SECRET`).
- `Update.model_validate(...)` → `dp.feed_update(bot, update)` → aiogram-диспетчер.

В режиме polling вход — тот же Dispatcher, без webhook-эндпоинта.

#### 2. aiogram Dispatcher → handler

`ChatServiceMiddleware` ([app/api/fastapi_app.py:80-82](app/api/fastapi_app.py#L80-L82)) инжектит `chat_service` в каждый handler. Текстовые сообщения попадают в `handle_text` ([app/bot/handlers/commands.py:636](app/bot/handlers/commands.py#L636)).

Перед агентом — мета-логика, которая отвечает сама и **не доходит до LangGraph**:
- Жёсткий лимит длины: `text[:max_message_length]`.
- Меню-кнопки: `BACK / END_SESSION / NEW_CHAT / BRANCHES / NEAREST_BRANCH / MY_SESSIONS / CHANGE_LANGUAGE / CURRENCY_RATES`.
- Парсинг «переключи на сессию N / UUID» (`_parse_session_switch_request`).

Если ничего не сработало — путь к агенту, обёрнутый в `_with_typing` (отправляет `chat_action=typing` каждые 4 c пока агент думает).

#### 3. ChatService.handle_user_message

[app/services/chat_service.py:195-295](app/services/chat_service.py#L195-L295):
1. `ensure_active_session(user.id)` — активная `ChatSession` или создаётся новая. **Её UUID становится `thread_id`** для LangGraph-чекпоинтера.
2. `set_session_title_if_empty` — первое сообщение пользователя становится заголовком сессии.
3. `touch_session` — обновляет `last_activity_at`.
4. `_save_message(role="user", text=...)` — пишем входящее в БД.
5. **Ветвление по `human_mode`**:
   - `human_mode=True` + есть активный middleware-чат → `ChatMiddlewareClient.send_message` шлёт реплику оператору, граф не вызывается; пользователю отвечаем `"sent_to_operator"`.
   - `human_mode=True` без middleware → всё-таки вызываем `agent_client.send_message(human_mode=True)`, чтобы `interrupt()` мог принять operator-reply из другого канала.
   - Иначе — нормальный путь.
6. `await asyncio.wait_for(agent_client.send_message(...), timeout=AGENT_TIMEOUT_SECONDS)`. На `TimeoutError` пишет `"agent_timeout"` в БД и возвращает `agent_unavailable`.
7. Парсит `[[PDF:/path]]`-маркер из ответа агента → отдельное поле `pdf_path`, маркер удаляется из текста.
8. Сохраняет `Message(role="agent", latency_ms, llm_usage)`.
9. Возвращает `AgentReply(text, pdf_path, session_id, human_mode, keyboard_options, show_operator_button)`.

#### 4. AgentClient → Agent._ainvoke

[app/agent/agent.py:45-92](app/agent/agent.py#L45-L92):
1. `config = {"configurable": {"thread_id": session_id}}`.
2. `graph.aget_state(config)` — поднимает прежний `BotState` из чекпоинтера (Postgres / Memory).
3. **`detect_language(user_text, fallback=...)`** — отдельный маленький LLM-вызов (`gpt-4o-mini`, `LANG_DETECTOR_TIMEOUT`) определяет ru/en/uz; пишет в `dialog["last_lang"]`.
4. **Свежий `SystemMessage(get_system_policy(detected_lang))` каждый ход** — затирает прежний в `messages[0]`. Это даёт два эффекта: правки `agent/i18n.py` применяются без сброса сессии и язык переключается на лету посреди диалога.
5. Сборка `state_in: BotState` (last_user_text, messages, dialog, lang, session_id, user_id, …) → `graph.ainvoke(state_in, config)`.
6. Из `out` берёт `answer`, `keyboard_options`, `show_operator_button`, `token_usage` → `AgentTurnResult`.

#### 5. LangGraph: router → одна из трёх нод → END

См. секции «Роутер», «Node: FAQ», «Node: calc_flow», «Node: human_mode» ниже.

#### 6. Возврат: Agent → ChatService → handle_text

`handle_text` ([app/bot/handlers/commands.py:828-861](app/bot/handlers/commands.py#L828-L861)) собирает финальную клавиатуру:
- `human_mode=True` → `human_mode_keyboard(human_mode=True)` (кнопка «вернуться к боту»).
- `show_operator_button=True` → `human_mode_keyboard(human_mode=False)` (кнопка «оператор»).
- Иначе если есть `keyboard_options` → `_flow_keyboard` (inline-кнопки `flow:<idx>`, лейблы извлекаются из `reply_markup` в callback'е).
- Иначе — `chat_keyboard(lang)`.

`_answer_safe` режет ответ по `TELEGRAM_SAFE_CHUNK=3800` и шлёт `parse_mode="HTML"` (Markdown преобразуется через `_md_to_html`). Если есть `pdf_path` — `answer_document(FSInputFile(...))`, потом `os.remove`.

#### 7. Параллельные процессы

- **Inactivity watcher** ([app/api/fastapi_app.py:33-62](app/api/fastapi_app.py#L33-L62)) каждые 60 c: закрывает сессии без активности и возвращает залипшие human-mode обратно к боту через `return_stale_human_sessions_to_bot` + `sync_human_mode_history_to_agent` (см. ниже).
- **Health probe** (`/health`) проверяет, что чекпоинтер не `MemorySaver`, если включён `REQUIRE_PERSISTENT_CHECKPOINTER`.

---

## LangGraph Agent

### Граф (3 ноды + роутер)

```
START → router
          ├─► faq          LLM + 12 инструментов
          ├─► calc_flow    Калькулятор кредита/вклада + захват лида
          └─► human_mode   Режим оператора (interrupt)
         → END
```

**Файл графа:** `app/agent/graph.py`

### Роутер (`app/agent/nodes/router.py`)

| Условие | Куда направляет |
|---------|----------------|
| `human_mode == True` | → `human_mode` |
| `dialog.lead_step` установлен | → `calc_flow` |
| `dialog.flow == "calc_flow"` | → `calc_flow` |
| Всё остальное | → `faq` |

Роутер не содержит LLM-логики — это чистый `if/elif`. Навигация через `Command(goto=...)`.

### Состояние (BotState)

```python
class BotState(TypedDict):
    messages: List[Any]           # История сообщений LangChain
    last_user_text: str           # Текущий ввод пользователя
    answer: str                   # Ответ бота
    human_mode: bool              # Режим оператора
    keyboard_options: List[str]   # Кнопки Telegram-клавиатуры
    dialog: dict                  # Состояние диалога (см. ниже)
    _route: str                   # Результат роутера
    session_id: str               # UUID сессии
    user_id: int                  # Telegram user ID
```

**dialog dict:**

```python
{
    "flow": None,              # None | "show_products" | "product_detail" | "calc_flow"
    "category": None,          # None | "mortgage" | "autoloan" | "microloan" |
                               #   "education_credit" | "deposit" | "debit_card" | "fx_card"
    "products": [],            # Загруженные продукты (list of dict)
    "selected_product": None,  # Выбранный продукт (dict)
    "calc_step": None,         # None | "amount" | "term" | "downpayment"
    "calc_slots": {},          # {amount: int, term_months: int, downpayment: float}
    "lead_step": None,         # None | "offer" | "name" | "phone"
    "lead_slots": {},          # {name: str, phone: str}
    "fallback_streak": 0,      # Счётчик fallback подряд (3 → кнопка оператора)
}
```

---

## Node: FAQ (`app/agent/nodes/faq.py`)

LLM получает историю сообщений + системный промпт + контекст состояния, и сам выбирает какой инструмент вызвать. Максимум **3 раунда tool-call** за один ход.

После LLM-цикла `_update_dialog_from_tools()` анализирует какие инструменты были вызваны и обновляет `dialog` + `keyboard_options`.

### Что происходит внутри (`node_faq`)

1. **Нормализация ввода** (`_normalize_user_text`) — схлопывает пробелы, режет повторы пунктуации (`!!!`, `???`), обрезает до 2000 символов. Регистр и диакритика сохраняются.
2. **PII-маскинг** (`mask_pii`) применяется только к `HumanMessage`, который уходит в LLM. В БД и в `messages` хранится оригинал.
3. **Системный промпт каждый ход = `policy[lang] + <state>...</state>`**. XML-сериализация `dialog` (`_format_state_xml`) включает `flow`, `category`, `products`, `selected_product`, `offices` + хинт «если пользователь прислал только число — вызови `select_product`/`select_office` с этим индексом». Используется XML, потому что `gpt-4o-mini` парсит теги надёжнее, чем «Current state: …» свободным текстом.
4. **Окно истории**: `[system] + последние MAX_DIALOG_MESSAGES` (по умолчанию 12). System-сообщение никогда не обрезается.
5. **Цикл tool-calling, до 3 раундов** (`max_rounds = 3`):
   - `llm_with_tools.ainvoke(loop_msgs)` → `AIMessage` с `tool_calls` или с финальным текстом.
   - Если `tool_calls` пуст → берём текст как `answer`, выходим.
   - Если есть → `ToolNode(_FAQ_TOOLS).ainvoke({"messages": ..., "dialog": dialog})` исполняет инструменты, кладёт `ToolMessage`-ответы в `loop_msgs`, идём на следующий раунд.
   - Если упёрлись в лимит при ещё ожидающих tool-call'ах → лог `WARNING` с именами последних tool'ов.
   - На `APIError / TimeoutError / JSONDecodeError` — fallback `_faq_lookup` по БД.
6. **Учёт токенов**: `accumulate_usage` суммирует usage с каждого `AIMessage`, `finalize_usage` считает стоимость по модели.

### Как `dialog` попадает в инструменты (InjectedState)

Часть инструментов в `app/agent/tools.py` (например `select_product`, `find_office`) объявляют параметр

```python
state: Annotated[dict, InjectedState] = None
```

`ToolNode` сам подкладывает туда текущий граф-state — этот параметр **не появляется в схеме инструмента, которую видит LLM**, поэтому модель его не «угадывает» и не пытается передать. Это позволяет инструменту читать `dialog["products"] / dialog["offices"]` без глобальных contextvars.

### Apply rule: ничего не меняем без tool-call

После цикла `_update_dialog_from_tools(dialog, tool_calls_made, user_text, lang)` смотрит **только** на последний tool-call и решает:
- какой `flow / category / selected_product / offices` стало;
- какую клавиатуру переподнять (`_reattach_keyboard`).

Если LLM не вызвал ни одного инструмента — `dialog` остаётся прежним, `keyboard_options` восстанавливается из текущего `flow`. То есть «свободный ответ» не разрушает состояние диалога.

### 12 инструментов (`app/agent/tools.py`)

| # | Tool | Когда LLM вызывает | Параметры | Влияние на dialog |
|---|------|--------------------|-----------|--------------------|
| 1 | `greeting_response()` | привет / hello / салом | — | Сброс dialog, главное меню (6 кнопок) |
| 2 | `thanks_response()` | спасибо / рахмат / thank you | — | Без изменений |
| 3 | `get_branch_info()` | адрес / филиал / branch | — | Без изменений |
| 4 | `get_currency_info()` | курс / доллар / exchange rate | — | Без изменений |
| 5 | `show_credit_menu()` | "хочу кредит" (без типа) | — | Подменю кредитов (4 кнопки) |
| 6 | `get_products(category)` | Запрос конкретного типа продукта | `category: str` | `flow="show_products"`, загрузка из БД |
| 7 | `select_product(product_name)` | Выбор продукта из списка | `product_name: str` | `flow="product_detail"`, карточка |
| 8 | `compare_products(query)` | "сравни" / "compare" | `query: str` | Текстовое сравнение |
| 9 | `back_to_product_list()` | ◀ / назад / back | — | Возврат к списку продуктов |
| 10 | `start_calculator()` | ✅ рассчитать / calculate | — | `flow="calc_flow"`, первый вопрос |
| 11 | `faq_lookup(query)` | Любой вопрос о банке | `query: str` | FAQ-поиск (similarity ≥ 0.62) |
| 12 | `request_operator()` | "оператор" / "operator" | — | Сигнал подключения оператора |

### Категории продуктов

| Категория | Описание | Модель в БД |
|-----------|----------|-------------|
| `mortgage` | Ипотека | CreditProductOffer |
| `autoloan` | Автокредит | CreditProductOffer |
| `microloan` | Микрозайм | CreditProductOffer |
| `education_credit` | Образовательный кредит | CreditProductOffer |
| `deposit` | Вклады | DepositProductOffer |
| `debit_card` | Дебетовые карты | CardProductOffer |
| `fx_card` | Валютные карты | CardProductOffer |

---

## Node: calc_flow (`app/agent/nodes/calc_flow.py`)

Детерминированная по структуре нода: переходы между шагами и финальные расчёты — обычный `if/elif`. Внутри отдельных шагов всё-таки используются маленькие LLM-вызовы (extractor'ы) для разбора пользовательского ввода — это надёжнее регулярок на «двадцать миллионов сум на 12 месяцев под 20%».

Два подпотока:

### 1. calc_step — сбор данных для расчёта

**Кредитные продукты:** сумма → срок → первоначальный взнос → PDF.
**Депозиты:** сумма → срок → текстовый расчёт.

#### Извлечение значений (LLM-экстракторы)

- `extract_prefill_from_history(messages, category, lang)` — на первом заходе в калькулятор вытягивает уже названные пользователем `amount` / `term_months` из недавних сообщений. Если в истории есть «мне 100 млн на 12 месяцев», шаги пропускаются.
- `extract_calc_value(user_text, calc_step, product_name, lang, recent_messages)` — на каждом шаге классифицирует ответ: `{type: "value", value: ...}` или `{type: "question"}`.
- `extract_updated_value(user_text, calc_step, calc_slots, ...)` — если ответ выглядит как вопрос, проверяет, не является ли он скрытым контекст-апдейтом («нет, давай 200 млн»). Если да — `type: "context_update"`, обновляет соответствующий слот.

Если шаг получил настоящий вопрос (`is_question and not parsed_value`) — отвечаем через `_faq_lookup` или короткий side-LLM (system: `at("calc_side_system", lang)`) и переспрашиваем текущий шаг.

#### Ограничение значений по продукту

Все значения клампятся к ограничениям продукта **до** расчёта:
- `_clamp_term(term, product, category)`: для кредитов — к `[term_min_months, term_max_months]` из `rate_matrix`; для депозитов — к ближайшему доступному значению из `rate_schedule`.
- `_clamp_downpayment(dp, product)`: к `[downpayment_min_pct, downpayment_max_pct]` из `rate_matrix`.

Если значение скорректировано — формируется `adjustment_note` («Минимальный срок 12 мес, использую 12») и приклеивается к ответу.

Дополнительно, если у продукта `term_min == term_max` или `downpayment_min == downpayment_max` — соответствующий вопрос не задаётся, слот заполняется автоматически.

#### Подбор ставки

- `_lookup_credit_rate(product, calc_slots)` — перебирает `rate_matrix`, ищет запись, в чьи диапазоны попадают введённые `term_months` и `downpayment`, выбирает `rate_min_pct`. Fallback: минимум по матрице.
- `_lookup_deposit_rate(product, calc_slots)` — ищет в `rate_schedule` запись с тем же `term_months` (предпочтение `currency=UZS`). Fallback: ближайший срок.

Если матрицы нет вовсе — берётся `rate_min_pct / rate_pct` продукта или константа `DEFAULT_CUSTOM_LOAN_RATE_PCT` (по умолчанию 20.0).

#### PDF-генерация (только для кредитов)

`generate_amortization_pdf(...)` запускается через `asyncio.to_thread` ([app/utils/pdf_generator.py](app/utils/pdf_generator.py)):
- Аннуитетная формула: `payment = principal × r × (1+r)^n / ((1+r)^n - 1)`.
- Таблица: месяц, ежемесячный платёж, основной долг, проценты, остаток.
- Маркер `[[PDF:/tmp/schedule_XXX.pdf]]` в ответе → `ChatService` извлекает путь и отдельно отправляет файл документом, потом удаляет с диска.

Депозит не генерирует PDF — выводится текстом через `at("deposit_result", lang, ...)`.

### 2. lead_step — захват контактов

После расчёта бот предлагает:
1. `lead_step="offer"` → «Хотите, чтобы вам перезвонили?» (кнопки: Да / Нет / Пересчитать). На «Пересчитать» (`_is_recalculate`) — рестарт калькулятора с тем же продуктом.
2. `lead_step="name"` → «Как вас зовут?» В историю пишется маска `[NAME]` вместо реального имени.
3. `lead_step="phone"` → «Укажите номер телефона». В истории — `[PHONE]`. `_save_lead_async` пишет `Lead` в БД (продукт, сумма, срок, ставка, имя, телефон).

---

## Node: human_mode (`app/agent/nodes/human_mode.py`)

Тело ноды — одна строка:
```python
operator_reply = langgraph_interrupt({"user_message": user_text, "reason": "human_mode_active"})
```

1. `langgraph_interrupt(...)` приостанавливает граф **в чекпоинтере** — состояние persistent, можно перезапускать процесс, ход не теряется.
2. Оператор отвечает через один из каналов:
   - **REST API:** `POST /operator/send` (с `X-API-Key`).
   - **SQLAdmin:** `https://agent-bot.uz/admin/` → ChatSessions → `operator_reply`.
   - **Telegram:** оператор с ID из `OPERATOR_IDS`.
   - **Asaka chat-middleware:** `_on_agent_message` callback ([app/api/fastapi_app.py:100-112](app/api/fastapi_app.py#L100-L112)).
3. Любой из каналов в итоге зовёт `agent_client.resume_human_mode(session_id, text)` → `graph.ainvoke(Command(resume=text), config)`. Граф возобновляется ровно с точки `interrupt`, `operator_reply` становится `answer`.
4. **Auto-timeout** через `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES`:
   - `inactivity_watcher` (60 c) находит сессии с `human_mode=True` и `human_mode_since <= threshold`, у которых **не было ни одного `Message(role="operator")`** с момента включения.
   - Сбрасывает `human_mode=False` и зовёт `sync_human_mode_history_to_agent(session_id, since=human_mode_since)`.
   - Та подгружает все `user`/`operator` сообщения с этого момента и инжектит их в `messages` графа: `user → HumanMessage`, `operator → AIMessage` (так бот «увидит», что оператор уже что-то ответил, и не будет переспрашивать с нуля).

### Fallback → Оператор

- Если `faq_lookup` не нашёл ответ (similarity < 0.62) → `fallback_streak += 1`.
- После **3 fallback подряд** → `show_operator_button=True` → `handle_text` показывает кнопку «Связаться с оператором».
- Сам `human_mode` включается только когда пользователь жмёт эту кнопку (callback `human:<session_id>` → `enable_human_mode`).

---

## FAQ-поиск (`app/utils/faq_tools.py`)

1. Загружает FAQ из БД (`FaqItem`) на нужном языке
2. Нормализация текста (lowercase, удаление спецсимволов)
3. Для каждого FAQ вычисляет similarity:
   - Полное вхождение → 1.0
   - Иначе: `SequenceMatcher ratio + token set overlap`
4. Если `best_score ≥ 0.62` → возвращает ответ
5. Иначе → `None` (fallback)

---

## Клавиатура (кнопки)

Кнопки устанавливаются в `keyboard_options` через `_reattach_keyboard()`:

| dialog.flow | Кнопки |
|-------------|--------|
| `None` (главное меню) | Ипотека, Автокредит, Микрозайм, Вклады, Карты, Вопрос |
| `"show_products"` | Названия продуктов |
| `"product_detail"` (кредит) | ✅ Рассчитать, ◀ Все продукты |
| `"product_detail"` (карта) | 📋 Подать заявку, ◀ Все продукты |
| `"calc_flow"` | — (свободный ввод) |
| после расчёта (lead) | Да, перезвоните / Нет, спасибо |

---

## i18n — Мультиязычность (`app/agent/i18n.py`)

Единый словарь `AGENT_TEXTS` с ~80 ключами на 3 языках (ru/en/uz).

```python
at("system_policy", "ru")                    # → системный промпт на русском
category_label("mortgage", "en")             # → "Mortgage"
get_calc_questions("mortgage", "uz")         # → [("amount", "Qancha..."), ...]
get_main_menu_buttons("ru")                  # → ["🏠 Ипотека", "🚗 Автокредит", ...]
```

Содержит: системный промпт, категории, кнопки, вопросы калькулятора, лейблы продуктов, типы доходов, fallback-сообщения, lead flow, сравнение.

---

## База данных

### Модели (`app/db/models.py`)

| Модель | Таблица | Назначение |
|--------|---------|-----------|
| `User` | `users` | Telegram user: id, username, language, phone |
| `ChatSession` | `chat_sessions` | Сессия: UUID, human_mode, operator, status |
| `Message` | `messages` | Сообщение: role (user/agent/operator/system), text, latency |
| `Lead` | `leads` | Захват контактов: продукт, сумма, срок, имя, телефон |
| `CreditProductOffer` | `credit_product_offers` | Кредитные продукты + rate_matrix |
| `DepositProductOffer` | `deposit_product_offers` | Вклады + ставки по срокам |
| `CardProductOffer` | `card_product_offers` | Дебетовые/валютные карты |
| `FaqItem` | `faq_items` | Вопрос-ответ на 3 языках |
| `Branch` | `branches` | Филиалы: адрес, телефон, координаты |

### Checkpointing

LangGraph сохраняет состояние графа через checkpointer:
- **Dev:** `MemorySaver` (in-memory)
- **Prod:** `PostgresSaver` (та же БД)
- Настройка: `LANGGRAPH_CHECKPOINT_BACKEND=memory|postgres|auto`

---

## Структура проекта

```
complex-agent-api/
├── app/
│   ├── agent/                  # LangGraph AI-агент
│   │   ├── graph.py            # Определение графа (4 ноды)
│   │   ├── agent.py            # Класс Agent (lifecycle, invocation)
│   │   ├── state.py            # BotState + _default_dialog()
│   │   ├── tools.py            # 12 LangChain-инструментов
│   │   ├── products.py         # Загрузка и форматирование продуктов
│   │   ├── i18n.py             # Переводы (ru/en/uz, ~80 ключей)
│   │   ├── intent.py           # Определение намерения (категория, приветствие)
│   │   ├── parsers.py          # Парсеры сумм, сроков, процентов
│   │   ├── constants.py        # Константы, context vars
│   │   ├── llm.py              # Инициализация LLM (ChatOpenAI)
│   │   ├── checkpointer.py     # Postgres/Memory checkpointer
│   │   └── nodes/
│   │       ├── router.py       # Роутер (human_mode → calc_flow → faq)
│   │       ├── faq.py          # FAQ нода (LLM + tools, 3 раунда)
│   │       ├── calc_flow.py    # Калькулятор + lead capture
│   │       ├── human_mode.py   # Оператор (interrupt/resume)
│   │       └── helpers.py      # _reattach_keyboard, _update_dialog_from_tools
│   ├── api/
│   │   └── fastapi_app.py      # FastAPI: webhook, /operator/send, health, inactivity watcher
│   ├── bot/
│   │   ├── handlers/
│   │   │   └── commands.py     # Все Telegram-хэндлеры
│   │   └── i18n.py             # Переводы интерфейса бота
│   ├── db/
│   │   ├── models.py           # SQLAlchemy ORM (9 моделей)
│   │   ├── session.py          # AsyncSession factory
│   │   └── alembic/            # Миграции
│   ├── services/
│   │   ├── chat_service.py     # Сессии, сообщения, timeout, hybrid mode
│   │   └── agent_client.py     # Обёртка над Agent
│   ├── admin/
│   │   ├── views.py            # SQLAdmin ModelView (9 классов)
│   │   ├── auth.py             # Авторизация (env-based)
│   │   ├── setup.py            # Инициализация SQLAdmin
│   │   ├── seed_view.py        # /admin/seed — форма загрузки xlsx
│   │   └── services/           # Парсинг xlsx + сид в БД
│   └── utils/
│       ├── faq_tools.py        # FAQ similarity search
│       ├── pdf_generator.py    # Аннуитетный PDF-график
│       ├── text_utils.py       # Нормализация, стемминг
│       └── data_loaders.py     # Async загрузчики из БД
├── scripts/                    # chat_cli.py — локальный REPL для агента
├── tests/                      # Тесты
├── nginx/                      # Конфиги nginx
├── .github/workflows/
│   └── deploy.yml              # CI/CD: автодеплой при push в main
├── docker-compose.yml          # Dev (PostgreSQL)
├── docker-compose.prod.yml     # Production (db + api + nginx + certbot)
├── Dockerfile
├── deploy.sh
├── Makefile
├── requirements.txt
└── .env.example
```

---

## Сценарии для тестирования

| Сообщение | Ожидаемый результат |
|-----------|-------------------|
| `/start` | Приветствие + главное меню (6 кнопок) |
| `🏠 Ипотека` | Список ипотечных программ (кнопки с названиями) |
| Выбрать продукт | Карточка с rate_matrix + кнопка «Рассчитать» |
| `✅ Рассчитать платёж` | Вопрос: сумма → срок → взнос → PDF-график |
| `Да, перезвоните` | Запрос имени → телефона → Lead в БД |
| `забыл пароль приложения` | Ответ из FAQ |
| `сравни ипотечные программы` | Таблица сравнения через LLM |
| `хочу оператора` | Кнопка подключения оператора |
| 3 непонятных вопроса подряд | Автоматическая кнопка «Связаться с оператором» |
| `I want a mortgage` (en) | Список на английском |
| `Ipoteka olmoqchiman` (uz) | Список на узбекском |
| Вопрос посреди калькулятора | Ответ + повтор текущего шага |

---

## Быстрый старт (локальная разработка)

### 1. Клонировать и настроить

```bash
git clone <repo-url> && cd complex-agent-api
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Запустить PostgreSQL

```bash
make dev
# или: docker compose up -d
```

### 3. Настроить окружение

```bash
cp .env.example .env
nano .env
```

Обязательные:
- `BOT_TOKEN` — токен Telegram бота от [@BotFather](https://t.me/BotFather)
- `OPENAI_API_KEY` — ключ OpenAI API
- `ADMIN_PASSWORD` — пароль для админ-панели

### 4. Миграции и seed-данные

```bash
alembic upgrade head
```

Продукты / FAQ / филиалы загружаются через админку:

1. Запусти приложение (см. ниже).
2. Зайди в `/admin/seed` (логин — `ADMIN_USERNAME` / `ADMIN_PASSWORD`).
3. Загрузи в форме актуальные xlsx-файлы (продукты, FAQ, три файла по филиалам).

CLI seed-скрипты удалены — админ-форма теперь единственная точка входа для сида.

### 5. Запустить

```bash
python main.py
```

Бот работает в режиме long-polling (без `WEBHOOK_BASE_URL`).

Health check: `curl http://127.0.0.1:8001/health`

### 6. Тесты

```bash
make test
# или: python3 -m pytest tests/test_agent.py -v
```

---

## Деплой на сервер (Ubuntu 24 + Docker)

### Требования

- Ubuntu 24.04 LTS
- Docker Engine 24+ и docker compose plugin
- Домен с DNS A-записью → IP сервера
- Открытые порты: 80, 443

### Шаг 1. Docker на сервер

```bash
ssh root@<server-ip>
apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh
docker --version && docker compose version
```

### Шаг 2. Проект на сервер

```bash
git clone <repo-url> /web/Call-Center-Agent-Service
cd /web/Call-Center-Agent-Service
```

### Шаг 3. Настроить .env

```bash
cp .env.example .env
nano .env
```

```env
BOT_TOKEN=<токен от BotFather>
OPENAI_API_KEY=<ключ OpenAI>
ADMIN_PASSWORD=<надёжный пароль>
ADMIN_SECRET_KEY=<openssl rand -hex 32>
WEBHOOK_BASE_URL=https://agent-bot.uz
WEBHOOK_SECRET=<openssl rand -hex 16>
OPERATOR_API_KEY=<openssl rand -hex 16>
POSTGRES_PASSWORD=<openssl rand -hex 16>
```

### Шаг 4. Деплой

```bash
bash deploy.sh
```

Скрипт: проверит .env → получит SSL → соберёт контейнеры → миграции → nginx → предложит seed-данные.

### Шаг 5. Проверить

```bash
make prod-status
curl https://agent-bot.uz/health
make prod-logs
```

---

## CI/CD — Автодеплой

При push в `main` → GitHub Actions автоматически деплоит на сервер.

**Файл:** `.github/workflows/deploy.yml`

### Настройка GitHub Secrets

| Secret | Значение |
|--------|----------|
| `SERVER_HOST` | IP сервера |
| `SERVER_USER` | `root` |
| `SERVER_SSH_KEY` | Приватный SSH-ключ (`cat ~/.ssh/id_ed25519`) |
| `SERVER_PORT` | `22` |
| `PROJECT_PATH` | `/web/Call-Center-Agent-Service` |

### Что делает workflow

1. SSH на сервер
2. `git fetch origin main && git reset --hard origin/main`
3. `docker compose up -d --build`
4. `alembic upgrade head`
5. `nginx -s reload`
6. Health check

---

## Makefile команды

| Команда | Описание |
|---------|----------|
| `make help` | Показать все команды |
| **Development** | |
| `make dev` | Запустить локальный PostgreSQL |
| `make dev-down` | Остановить локальный PostgreSQL |
| `make test` | Запустить тесты |
| `make migrate` | Применить миграции локально |
| **Production** | |
| `make prod-deploy` | Первый деплой (SSL + build + migrate) |
| `make prod-update` | Обновить: rebuild + migrate + reload |
| `make prod-logs` | Логи api + nginx |
| `make prod-restart` | Рестарт API + reload nginx |
| `make prod-status` | Статус контейнеров |
| `make prod-migrate` | Применить миграции |
| `make prod-seed` | Загрузить seed-данные |
| `make prod-shell` | Bash в контейнер API |
| `make prod-down` | Остановить все контейнеры |
| `make prod-renew-ssl` | Обновить SSL-сертификат |

---

## SSL-сертификат

Сертификат Let's Encrypt действует 90 дней.

Ручное обновление:
```bash
make prod-renew-ssl
```

Автоматическое (cron):
```bash
crontab -e
0 3 1 */2 * cd /web/Call-Center-Agent-Service && docker compose -f docker-compose.prod.yml run --rm --entrypoint certbot certbot renew && docker compose -f docker-compose.prod.yml exec nginx nginx -s reload >> /var/log/certbot-renew.log 2>&1
```

---

## Переменные окружения

| Переменная | Обязательна | По умолчанию | Описание |
|------------|:-----------:|:------------:|----------|
| `BOT_TOKEN` | ✅ | — | Telegram bot token |
| `OPENAI_API_KEY` | ✅ | — | OpenAI API key |
| `ADMIN_PASSWORD` | ✅ | — | Пароль админ-панели |
| `DATABASE_URL` | — | `...localhost:5432/bankbot` | Async SQLAlchemy URL |
| `POSTGRES_USER` | — | `bankbot` | PostgreSQL user (Docker) |
| `POSTGRES_PASSWORD` | — | `bankbot` | PostgreSQL password (Docker) |
| `POSTGRES_DB` | — | `bankbot` | PostgreSQL database (Docker) |
| `WEBHOOK_BASE_URL` | prod | — | `https://agent-bot.uz` |
| `WEBHOOK_SECRET` | prod | — | Секрет для webhook |
| `OPERATOR_IDS` | — | — | Telegram ID операторов (через запятую) |
| `OPERATOR_API_KEY` | — | — | API-ключ для `/operator/send` |
| `ADMIN_USERNAME` | — | `admin` | Логин админ-панели |
| `ADMIN_SECRET_KEY` | prod | `change-me...` | Секрет сессий |
| `OPENAI_MODEL` | — | `gpt-4o-mini` | Модель OpenAI |
| `OPENAI_BASE_URL` | — | — | Кастомный OpenAI endpoint |
| `LANGGRAPH_CHECKPOINT_BACKEND` | — | `auto` | `memory\|postgres\|auto` |
| `SESSION_INACTIVITY_TIMEOUT_MINUTES` | — | `1440` | Таймаут сессии (мин) |
| `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES` | — | `10` | Таймаут оператора (мин) |
| `MAX_DIALOG_MESSAGES` | — | `12` | Лимит истории сообщений |

---

## Гибридный режим (оператор)

### Как работает

1. Пользователь запрашивает оператора (или 3 fallback подряд)
2. `ChatSession.human_mode = True`
3. Сообщения сохраняются в БД, но **не** отправляются в LangGraph
4. Оператор отвечает через REST API, SQLAdmin или Telegram
5. Фоновый watcher (60 сек) возвращает сессию боту при таймауте оператора

### Operator API

```bash
curl -X POST https://agent-bot.uz/operator/send \
  -H "X-API-Key: $OPERATOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "...", "text": "Привет!", "operator_name": "Ali", "operator_id": 123}'
```

---

## Troubleshooting

### Бот не отвечает

```bash
make prod-logs
curl https://agent-bot.uz/health
curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
```

### Ошибка SSL

```bash
dig +short agent-bot.uz
rm -rf nginx/certbot/conf/live/agent-bot.uz
bash deploy.sh
```

### Ошибка БД

```bash
docker compose -f docker-compose.prod.yml logs db
docker compose -f docker-compose.prod.yml exec db psql -U bankbot bankbot
```

### Полный перезапуск

```bash
make prod-down
docker volume rm call-center-agent-service_pgdata   # ⚠️ удалит данные!
bash deploy.sh
```

---

## Руководство для разработчиков

### Добавление нового инструмента (Tool)

Инструменты — основной способ расширения агента. LLM сам решает, какой инструмент вызвать, на основе docstring.

**Шаг 1.** Создать функцию в `app/agent/tools.py`:

```python
@lc_tool
async def my_new_tool(param: str) -> str:
    """Описание на английском — КОГДА вызывать этот инструмент.
    LLM читает именно docstring чтобы решить вызывать или нет.
    Будьте конкретны: 'Use when user asks about X' """
    lang = _REQUEST_LANGUAGE.get()      # текущий язык пользователя
    dialog = _CURRENT_DIALOG.get()      # текущее состояние диалога
    # ... логика ...
    return "ответ для пользователя"
```

**Шаг 2.** Добавить в список `_FAQ_TOOLS` в конце `tools.py`:

```python
_FAQ_TOOLS = [
    greeting_response, thanks_response, ...,
    my_new_tool,   # ← добавить сюда
]
```

**Шаг 3.** Если инструмент меняет dialog state → добавить обработчик в `app/agent/nodes/helpers.py` функция `_update_dialog_from_tools()`:

```python
elif tool_name == "my_new_tool":
    dialog["flow"] = "my_flow"
    dialog["some_field"] = tool_args.get("param")
    keyboard = ["Кнопка 1", "Кнопка 2"]
```

**Шаг 4.** Добавить переводы в `app/agent/i18n.py` если нужны:

```python
AGENT_TEXTS = {
    "ru": { ..., "my_tool_reply": "Ответ на русском" },
    "en": { ..., "my_tool_reply": "English reply" },
    "uz": { ..., "my_tool_reply": "O'zbek javob" },
}
```

**Готово.** LLM автоматически обнаружит инструмент по docstring. Никакой регистрации intent не нужно.

### Добавление нового типа продукта

**Шаг 1.** Добавить модель в `app/db/models.py`:

```python
class InsuranceOffer(Base):
    __tablename__ = "insurance_offers"
    id = Column(Integer, primary_key=True)
    service_name = Column(String, nullable=False)
    # ...
```

**Шаг 2.** Создать миграцию:

```bash
alembic revision -m "add insurance_offers" --autogenerate
alembic upgrade head
```

**Шаг 3.** Добавить категорию в `app/agent/constants.py`:

```python
CREDIT_SECTION_MAP["insurance"] = "Страхование"
```

**Шаг 4.** Добавить загрузку в `app/agent/products.py` функция `_get_products_by_category()`:

```python
elif category == "insurance":
    rows = await _load_insurance_offers()
    return [{"name": r.service_name, "rate": r.rate, ...} for r in rows]
```

**Шаг 5.** Добавить лейблы в `app/agent/i18n.py`:

```python
"cat_insurance": "Страхование" / "Insurance" / "Sug'urta"
```

**Шаг 6.** Добавить вопросы калькулятора если нужны (в `AGENT_TEXTS` → `calc_questions`).

**Шаг 7.** Добавить сервис сида в `app/admin/services/` (парсинг Excel → запись в БД) и подключить его из `app/admin/seed_view.py`.

LLM сам будет направлять запросы на `get_products(category="insurance")`.

### Добавление новой ноды

Если логика не укладывается в существующие ноды (faq/calc_flow/human_mode):

**Шаг 1.** Создать файл `app/agent/nodes/my_node.py`:

```python
from langgraph.types import Command
from app.agent.state import BotState

async def node_my_feature(state: BotState) -> dict:
    user_text = state["last_user_text"]
    dialog = dict(state.get("dialog") or {})
    # ... логика ...
    return {
        "answer": "ответ",
        "dialog": dialog,
        "keyboard_options": ["кнопка1"],
    }
```

**Шаг 2.** Зарегистрировать в `app/agent/graph.py`:

```python
from app.agent.nodes.my_node import node_my_feature

graph.add_node("my_feature", node_my_feature)
graph.add_edge("my_feature", END)
```

**Шаг 3.** Добавить условие в роутер `app/agent/nodes/router.py`:

```python
if dialog.get("flow") == "my_feature":
    return Command(goto="my_feature")
```

### Добавление новых FAQ

- Из Excel: `/admin/seed` → форма «FAQ» → загрузить xlsx (листы `RU`/`EN`/`UZ`).
- Или вручную: `https://agent-bot.uz/admin/` → FaqItems → Create.

Внутри: логика парсинга лежит в `app/admin/services/faq_import.py`.

### Добавление переводов

Все тексты агента в `app/agent/i18n.py`, словарь `AGENT_TEXTS`. Структура:

```python
AGENT_TEXTS = {
    "ru": {"key": "Текст на русском {param}"},
    "en": {"key": "English text {param}"},
    "uz": {"key": "O'zbek matni {param}"},
}
```

Использование: `at("key", lang, param="значение")`

### Запуск тестов

```bash
python3 -m pytest tests/test_agent.py -v          # все тесты
python3 -m pytest tests/test_agent.py -v -k "faq"  # только FAQ тесты
```

Тесты не требуют БД — используют моки.
