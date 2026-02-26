# Banking Chatbot API

Telegram-бот банка с AI-консультантом для подбора кредитов, вкладов и карт. Построен на **aiogram 3** (Telegram), **FastAPI** (HTTP), **LangGraph** (AI граф), **SQLAlchemy** (БД).

## Возможности

- Подбор кредитных продуктов (ипотека, автокредит, микрозайм, образовательный) с пошаговым опросом
- Подбор вкладов и карт через диалог
- Генерация PDF-графика аннуитетных платежей
- FAQ-база по услугам банка
- Гибридный режим: бот + оператор (human handoff)
- Мультиязычность: ru / en / uz
- Поиск отделений по геолокации или региону

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
| `LANGGRAPH_DIALOG_TTL_MINUTES` | нет | `720` | TTL диалогового контекста в минутах |
| `SESSION_INACTIVITY_TIMEOUT_MINUTES` | нет | `1440` | Автозакрытие неактивной сессии |
| `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES` | нет | `10` | Автовозврат из human-mode если оператор не ответил |
| `LOCAL_AGENT_INTENT_LLM_ENABLED` | нет | `1` | Включить LLM-классификацию интентов |
| `LOCAL_AGENT_INTENT_LLM_MODEL` | нет | `gpt-4o-mini` | Модель OpenAI |
| `APP_HOST` | нет | `0.0.0.0` | Bind host |
| `APP_PORT` | нет | `8001` | Bind port |

---

## Архитектура

### Поток запроса

```
Telegram → POST /telegram/webhook → FastAPI → aiogram Dispatcher
  → commands.py handlers → ChatService → AgentClient → Agent (LangGraph)
  → Database (SQLAlchemy async)
```

### Структура проекта

```
app/
├── api/
│   └── fastapi_app.py        # FastAPI: webhook, /operator/send, /health, inactivity watcher
├── bot/
│   ├── handlers/commands.py  # Все Telegram обработчики (1200 строк)
│   ├── i18n.py               # Переводы ru/en/uz
│   └── keyboards/            # Reply и Inline клавиатуры
├── db/
│   ├── models.py             # ORM модели
│   ├── session.py            # Async engine
│   └── alembic/              # Миграции
├── services/
│   ├── agent.py              # LangGraph агент (1400 строк)
│   ├── agent_client.py       # Тонкая обёртка вокруг Agent
│   ├── chat_service.py       # Сессии, история сообщений, human mode (~570 строк)
│   └── telegram_sender.py    # HTTP-отправка сообщений в Telegram
├── tools/
│   ├── credit_tools.py       # Подбор кредитов из БД
│   ├── deposit_tools.py      # Подбор вкладов из БД
│   ├── card_tools.py         # Подбор карт из БД
│   ├── faq_tools.py          # FAQ-поиск (similarity matching)
│   ├── data_loaders.py       # Загрузка продуктов, FAQ, файлов данных
│   ├── question_engine.py    # Схемы вопросов + логика следующего вопроса
│   ├── pdf_generator.py      # Генератор PDF аннуитетного графика (fpdf2)
│   └── text_utils.py         # normalize_text, token_stem, token_set
└── data/
    ├── ai_chat_info.json     # Манифест продуктового справочника
    └── ai_chat_info/         # JSON-секции (ипотека, авто, депозиты и т.д.)
```

### LangGraph граф агента

```
START → node_classify_intent → _route
  "faq"            → node_faq            → END
  "product_credit" → node_product_credit → END
  "cross_sell"     → node_cross_sell     → END
  "human"          → node_human_mode     → END
```

**Узлы:**

| Узел | Что делает |
|---|---|
| `node_classify_intent` | Правила + LLM (gpt-4o-mini) → определяет `_route` и инициализирует `dialog` |
| `node_faq` | Приветствие / FAQ по БД / LLM-ответ / follow-up уточнения |
| `node_product_credit` | Пошаговый опрос слотов → подбор кредита → PDF аннуитетного графика |
| `node_cross_sell` | Пошаговый опрос слотов → подбор вклада или карты |
| `node_human_mode` | Сохраняет сообщение, уведомляет операторов |

**BotState:**
```python
class BotState(TypedDict, total=False):
    messages: List[Any]       # история LangChain сообщений
    last_user_text: str       # текущий текст пользователя
    answer: str               # итоговый ответ
    dialog: Dict[str, Any]   # {"flow": str|None, "step": str|None, "slots": dict}
    human_mode: bool          # True → node_human_mode
    _route: str               # внутренняя маршрутизация
```

**dialog.flow** может быть: `"product_credit"` | `"cross_sell"` | `None`

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
| `chat_sessions` | Сессии чата (статус, human_mode, feedback) |
| `messages` | История сообщений (role: user/agent/operator/system) |
| `branches` | Отделения банка с координатами |
| `faq` | FAQ-база (вопрос/ответ на 3 языках) |
| `credit_product_offers` | Кредитные продукты (ипотека, авто, микрозайм, образование) |
| `deposit_product_offers` | Вкладные продукты |
| `card_product_offers` | Карточные продукты |

---

## Подбор продуктов

### Кредиты (`node_product_credit`)

1. Классификация интента → определение `credit_category` (ипотека/авто/микро/образование)
2. Пошаговый опрос через `question_engine.py`:
   - **GENERAL_QUESTIONS**: гражданство, возраст, пол, доход, сумма, срок
   - **SERVICE_QUESTION_BLOCKS**: специфичные вопросы (регион ипотеки, цель микрозайма и т.д.)
3. `credit_tools.py` — точный или приближённый подбор из `credit_product_offers`
4. Если подходящие варианты найдены — `pdf_generator.py` генерирует аннуитетный PDF

### Вклады и карты (`node_cross_sell`)

Аналогично, через `NON_CREDIT_QUESTION_BLOCKS` (deposit / card) → `deposit_tools.py` / `card_tools.py`

---

## Гибридный режим (оператор)

- Кнопка «Подключить оператора» → `ChatSession.human_mode = True`
- Сообщения клиента сохраняются в БД, но НЕ отправляются в LangGraph
- Оператор отвечает через:
  - Telegram: `/op <session_id> <текст>`
  - REST API: `POST /operator/send`
- Фоновый watcher (60 сек) автоматически возвращает сессию в bot-mode если оператор не ответил за `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES`

### Operator API

```bash
curl -X POST http://127.0.0.1:8001/operator/send \
  -H "X-API-Key: $OPERATOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "...", "text": "Привет!", "operator_name": "Ali"}'
```

---

## Добавить новый тип продукта

1. Добавить ORM-модель в [app/db/models.py](app/db/models.py)
2. Создать миграцию: `alembic revision -m "add_new_product" --autogenerate`
3. Создать seed-скрипт в `scripts/`
4. Добавить инструмент подбора в `app/tools/`
5. Добавить вопросы в [app/tools/question_engine.py](app/tools/question_engine.py)
6. Зарегистрировать новый интент в [app/services/agent.py](app/services/agent.py):
   - Добавить в `Intent = Literal[...]`
   - Добавить эвристику в `_classify_new_intent_rules()`
   - Добавить маршрутизацию в `node_classify_intent()`

---

## Webhook vs Polling

| Режим | Настройка | Применение |
|---|---|---|
| Webhook | Задать `WEBHOOK_BASE_URL` (HTTPS) | Production |
| Polling | Оставить `WEBHOOK_BASE_URL` пустым | Local dev |

Для локального тестирования webhook: `ngrok http 8001`, затем `WEBHOOK_BASE_URL=https://xxxx.ngrok.io`

---

## Скрипты

```bash
# Загрузка продуктов
python3 scripts/seed_credit_product_offers.py --replace
python3 scripts/seed_deposit_product_offers.py --replace
python3 scripts/seed_card_product_offers.py --replace
python3 scripts/seed_noncredit_product_offers.py --replace

# FAQ
python3 scripts/import_faq_xlsx.py "scripts/FAQ.xlsx" --replace

# Миграции
alembic upgrade head
alembic revision -m "description" --autogenerate
```

---

## Разработка

Тестов нет — валидация через запуск и ручное тестирование в Telegram:

```bash
# Проверить импорт
python3 -c "from app.services.agent import Agent; print('OK')"

# Запустить
python3 main.py

# Health check
curl http://127.0.0.1:8001/health
```

Сценарии для проверки в Telegram:
- `"хочу ипотеку"` → должен запустить подбор ипотеки
- `"забыл пароль в приложении"` → должен ответить из FAQ
- `"хочу вклад в долларах"` → должен запустить подбор вклада
- `"и все?"` после ответа → должен дать короткое продолжение, а не повторить FAQ
