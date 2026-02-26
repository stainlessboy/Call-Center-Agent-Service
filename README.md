# Banking Chatbot API

Telegram-бот банка с AI-консультантом для подбора кредитов, вкладов и карт. Построен на **aiogram 3** (Telegram), **FastAPI** (HTTP), **LangGraph** (AI граф), **SQLAlchemy** (БД).

## Возможности

- Подбор кредитных продуктов (ипотека, автокредит, микрозайм, образовательный) с расчётом графика платежей
- Подбор вкладов и карт через диалог
- Сравнение продуктов по запросу пользователя
- Генерация PDF-графика аннуитетных платежей
- Захват лидов: после расчёта бот спрашивает имя и телефон → запись в таблицу `leads`
- FAQ-база по услугам банка
- Гибридный режим: бот + оператор (human handoff)
- Мультиязычность: ru / en / uz
- Поиск отделений по геолокации или региону
- Множественные сессии: пользователь может вести несколько диалогов одновременно

---

## Быстрый старт

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # заполнить BOT_TOKEN, OPENAI_API_KEY, DATABASE_URL

alembic upgrade head          # создать таблицы

# Загрузить справочные данные
python3 scripts/seed_credit_product_offers.py --replace
python3 scripts/seed_deposit_product_offers.py --replace
python3 scripts/seed_card_product_offers.py --replace
python3 scripts/import_faq_xlsx.py "scripts/FAQ.xlsx" --replace

python3 main.py               # запустить (http://0.0.0.0:8001)
```

Проверка: `curl http://127.0.0.1:8001/health`

---

## Переменные окружения

| Переменная | Обязательная | По умолчанию | Описание |
|---|---|---|---|
| `BOT_TOKEN` | да | — | Telegram bot token |
| `OPENAI_API_KEY` | да | — | OpenAI API key |
| `DATABASE_URL` | нет | `sqlite+aiosqlite:///./bot.db` | SQLAlchemy async URL |
| `WEBHOOK_BASE_URL` | нет | — | Если задан — регистрирует webhook; иначе polling |
| `WEBHOOK_PATH` | нет | `/telegram/webhook` | Путь webhook |
| `WEBHOOK_SECRET` | нет | — | Секрет для X-Telegram-Bot-Api-Secret-Token |
| `OPERATOR_IDS` | нет | — | Telegram ID операторов (через запятую) |
| `OPERATOR_API_KEY` | нет | — | Bearer-ключ для `POST /operator/send` |
| `LANGGRAPH_CHECKPOINT_BACKEND` | нет | `auto` | `auto\|sqlite\|postgres\|memory` |
| `LANGGRAPH_CHECKPOINT_URL` | нет | `.langgraph_checkpoints.sqlite3` | Путь/URL к базе чекпоинтов |
| `SESSION_INACTIVITY_TIMEOUT_MINUTES` | нет | `1440` | Автозакрытие неактивной сессии |
| `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES` | нет | `10` | Автовозврат из human-mode если оператор не ответил |
| `LOCAL_AGENT_INTENT_LLM_ENABLED` | нет | `1` | Включить LLM-ответы (0 = только правила) |
| `LOCAL_AGENT_INTENT_LLM_MODEL` | нет | `gpt-4o-mini` | Модель OpenAI |
| `APP_HOST` | нет | `0.0.0.0` | Bind host |
| `APP_PORT` | нет | `8001` | Bind port |

---

## Архитектура

### Поток запроса

```
Telegram → POST /telegram/webhook → FastAPI → aiogram Dispatcher
  → commands.py handlers
      → ChatService.handle_user_message()
          → AgentClient → Agent.send_message()
              → LangGraph: node_classify_intent → node_faq | node_calc_flow | node_human_mode_turn
  → Database (SQLAlchemy async)
```

### Структура проекта

```
app/
├── api/
│   └── fastapi_app.py        # FastAPI: /telegram/webhook, /operator/send, /health
├── bot/
│   ├── handlers/commands.py  # Все Telegram-обработчики команд, текста, callback
│   ├── i18n.py               # Переводы ru/en/uz
│   └── keyboards/            # Reply и Inline клавиатуры
├── db/
│   ├── models.py             # ORM-модели
│   ├── session.py            # Async engine + get_session()
│   └── alembic/versions/     # Миграции
├── services/
│   ├── agent.py              # LangGraph-агент: граф, ноды, BotState
│   ├── agent_client.py       # Тонкая обёртка вокруг Agent
│   ├── chat_service.py       # Сессии, история, human mode
│   └── telegram_sender.py    # HTTP-отправка сообщений операторов
├── tools/
│   ├── data_loaders.py       # Синхронная загрузка продуктов из БД (lru_cache)
│   ├── faq_tools.py          # Поиск в FAQ (token similarity)
│   ├── pdf_generator.py      # Генератор PDF аннуитетного графика (fpdf2)
│   └── text_utils.py         # normalize_text, token_stem, token_set
└── data/
    └── ai_chat_info/         # JSON-источники продуктов (seed для БД)
```

---

## LangGraph агент (`app/services/agent.py`)

### Граф

```
START
  └─► node_classify_intent
          ├─► node_faq               (FAQ, приветствие, каталог продуктов)
          ├─► node_calc_flow         (расчёт + захват лида)
          └─► node_human_mode_turn   (режим оператора)
         → END
```

### Маршрутизация (`node_classify_intent`)

| Условие | Маршрут |
|---|---|
| `state.human_mode == True` | `human` → `node_human_mode_turn` |
| `dialog.flow == "calc_flow"` | `calc_flow` → `node_calc_flow` |
| Всё остальное | `faq` → `node_faq` |

### Узел `node_faq`

Обрабатывает в порядке приоритета:

1. **Приветствие** — отвечает с меню-кнопками (ипотека / авто / микрозайм / вклад / карта / вопрос)
2. **Спасибо** — короткий ответ
3. **Назад** — возврат к списку продуктов
4. **Рассчитать / Подать заявку** — переход в `calc_flow`
5. **Сравнение** (`_is_comparison_request`) — подгружает продукты категории, передаёт в LLM с инструкцией "только наш банк"
6. **Выбор продукта из списка** — показывает карточку, кнопки "Рассчитать / Назад"
7. **Категория продукта** (`_detect_product_category`) — показывает список продуктов
8. **Вопрос об отделениях** — краткий ответ
9. **Курс валют** — краткий ответ
10. **FAQ из БД** → **LLM (контекстный)** → **LLM (finance)** → **Статичный fallback**

### Узел `node_calc_flow`

Многошаговый сбор данных для расчёта:

```
lead_step == "offer"  → "Хотите оформить?" (кнопки Да/Нет)
lead_step == "name"   → "Как вас зовут?"
lead_step == "phone"  → "Укажите телефон:" → _save_lead_sync() → _default_dialog()

calc_step == "amount"      → _parse_amount()
calc_step == "term"        → _parse_term_months()
calc_step == "downpayment" → _parse_downpayment()

Если вопрос по теме → FAQ + повтор вопроса
Если неверный формат → подсказка ("Введите цифрами, например: 500 млн")

Все слоты собраны:
  ├─► deposit → текстовый расчёт дохода → lead_step = "offer"
  └─► credit  → PDF аннуитетного графика → lead_step = "offer"
```

### BotState

```python
class BotState(TypedDict):
    messages: List[Any]            # история LangChain-сообщений
    last_user_text: str            # текущий текст пользователя
    answer: str                    # итоговый ответ
    human_mode: bool               # True → node_human_mode_turn
    keyboard_options: Optional[List[str]]  # кнопки для ответа
    dialog: dict                   # см. _default_dialog()
    _route: str                    # внутренняя маршрутизация
    session_id: Optional[str]      # ID текущей ChatSession
    user_id: Optional[int]         # ID пользователя
```

**`dialog`** (сбрасывается через `_default_dialog()`):

```python
{
    "flow": None | "show_products" | "product_detail" | "calc_flow",
    "category": None | "mortgage" | "autoloan" | "microloan" | "education_credit"
               | "deposit" | "debit_card" | "fx_card",
    "products": [],              # список продуктов для show_products
    "selected_product": None,    # выбранный продукт для calc/lead
    "calc_step": None | "amount" | "term" | "downpayment",
    "calc_slots": {},            # {"amount": int, "term_months": int, "downpayment": float}
    "lead_step": None | "offer" | "name" | "phone",
    "lead_slots": {},            # {"name": str, "phone": str}
}
```

### Checkpointing

LangGraph сохраняет `BotState` между сообщениями в рамках сессии.

| `LANGGRAPH_CHECKPOINT_BACKEND` | Хранилище |
|---|---|
| `auto` (default) | SQLite (`.langgraph_checkpoints.sqlite3`) |
| `sqlite` | SQLite по пути из `LANGGRAPH_CHECKPOINT_URL` |
| `postgres` | PostgreSQL, нужен `LANGGRAPH_CHECKPOINT_URL` |
| `memory` | In-memory, теряется при рестарте (только dev) |

---

## Схема базы данных

| Таблица | Назначение |
|---|---|
| `users` | Пользователи Telegram (id, phone, язык) |
| `chat_sessions` | Сессии чата (статус, human_mode, feedback, last_activity_at) |
| `messages` | История сообщений (role: user/agent/operator/system) |
| `leads` | Захваченные лиды: контакт + продукт + параметры кредита/вклада |
| `branches` | Отделения банка с координатами |
| `faq` | FAQ-база (вопрос/ответ на 3 языках) |
| `credit_product_offers` | Кредитные продукты (ипотека, авто, микрозайм, образование) |
| `deposit_product_offers` | Вкладные продукты |
| `card_product_offers` | Карточные продукты |

---

## Гибридный режим (оператор)

- Кнопка «Подключить оператора» → `ChatSession.human_mode = True`
- Сообщения пользователя сохраняются в БД, но **не** отправляются в LangGraph
- Оператор отвечает через:
  - Telegram: `/op <session_id> <текст>`
  - REST API: `POST /operator/send`
- Фоновый watcher (60 сек) автоматически возвращает сессию в bot-mode, если оператор не ответил за `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES`

### Operator API

```bash
curl -X POST http://127.0.0.1:8001/operator/send \
  -H "X-API-Key: $OPERATOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "...", "text": "Привет!", "operator_name": "Ali"}'
```

---

## Добавить новый тип продукта

1. Добавить ORM-модель в `app/db/models.py`
2. Создать миграцию: `alembic revision -m "add_new_product" --autogenerate`
3. Создать seed-скрипт в `scripts/`
4. Добавить loader в `app/tools/data_loaders.py`
5. Добавить категорию в `_detect_product_category()` и `CALC_QUESTIONS` в `app/services/agent.py`
6. Добавить метку в `CATEGORY_LABELS` и `CREDIT_SECTION_MAP` (если кредит)

---

## Webhook vs Polling

| Режим | Настройка | Применение |
|---|---|---|
| Webhook | Задать `WEBHOOK_BASE_URL` (HTTPS URL) | Production |
| Polling | Оставить `WEBHOOK_BASE_URL` пустым | Local dev |

Для локального тестирования webhook: `ngrok http 8001`, затем `WEBHOOK_BASE_URL=https://xxxx.ngrok.io`

---

## Скрипты

```bash
# Загрузка продуктов
python3 scripts/seed_credit_product_offers.py --replace
python3 scripts/seed_deposit_product_offers.py --replace
python3 scripts/seed_card_product_offers.py --replace

# FAQ
python3 scripts/import_faq_xlsx.py "scripts/FAQ.xlsx" --replace

# Миграции
alembic upgrade head
alembic revision -m "описание" --autogenerate
```

---

## Проверка в Telegram

```bash
# Проверить импорт
python3 -c "from app.services.agent import Agent; print('OK')"

# Запустить
python3 main.py

# Health check
curl http://127.0.0.1:8001/health
```

Сценарии для ручного тестирования:
- `"хочу ипотеку"` → список ипотечных программ → выбор → расчёт → PDF → лид
- `"а какие кредиты есть?"` → меню: ипотека / авто / микрозайм / образовательный
- `"в чем разница между KIA и Chevrolet Onix?"` → сравнение по данным банка
- `"забыл пароль в приложении"` → ответ из FAQ
- `"хочу вклад"` → список вкладов → выбор → расчёт дохода → лид
- `"🏠 Ипотека"` (кнопка) → то же, что `"хочу ипотеку"`
