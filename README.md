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

```
1. Пользователь отправляет сообщение в Telegram
2. Telegram → POST /telegram/webhook → FastAPI
3. aiogram Dispatcher → handlers/commands.py
4. ChatService.handle_user_message():
   a. Создаёт/находит активную сессию (ChatSession)
   b. Сохраняет Message(role="user") в БД
   c. Если human_mode → не отправляет в LangGraph, ждёт оператора
   d. Иначе → вызывает AgentClient → Agent.send_message()
5. Agent:
   a. Загружает состояние из checkpointer (история, dialog)
   b. Формирует BotState
   c. graph.ainvoke(BotState) → router → faq/calc_flow/human_mode
   d. Возвращает AgentTurnResult(text, keyboard, show_operator_button)
6. ChatService:
   a. Сохраняет Message(role="agent") в БД
   b. Извлекает [[PDF:...]] маркер если есть
   c. Возвращает ответ в Telegram с клавиатурой
```

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

Детерминистическая нода (без LLM), два подпотока:

### 1. calc_step — Сбор данных для расчёта

**Кредитные продукты:** сумма → срок → первоначальный взнос → PDF
**Депозиты:** сумма → срок → текстовый расчёт

Парсеры (`app/agent/parsers.py`):
- `_parse_amount()` — понимает "500 млн", "2 млрд", "100 тыс", "500 million"
- `_parse_term_months()` — конвертирует "10 лет" / "36 месяцев" / "2 years"
- `_parse_downpayment()` — проценты: "20%", "двадцать"

Если пользователь задаёт вопрос посреди расчёта → отвечает через LLM → переспрашивает текущий шаг.

**PDF-генерация** (`app/utils/pdf_generator.py`):
- Аннуитетная формула: `payment = principal × r × (1+r)^n / ((1+r)^n - 1)`
- Таблица: месяц, ежемесячный платёж, основной долг, проценты, остаток
- Маркер `[[PDF:/tmp/schedule_XXX.pdf]]` в ответе → ChatService извлекает и отправляет файл

### 2. lead_step — Захват контактов

После расчёта бот предлагает:
1. "Хотите, чтобы вам перезвонили?" → `lead_step="offer"`
2. "Да" → "Как вас зовут?" → `lead_step="name"`
3. Иван Петров → "Укажите номер телефона" → `lead_step="phone"`
4. +998901234567 → сохраняет **Lead** в БД → сброс dialog

---

## Node: human_mode (`app/agent/nodes/human_mode.py`)

1. `langgraph_interrupt()` приостанавливает выполнение графа
2. Оператор отвечает через:
   - **REST API:** `POST /operator/send` (с `X-API-Key`)
   - **SQLAdmin:** `https://agent-bot.uz/admin/` → ChatSessions → operator_reply
   - **Telegram:** оператор с ID из `OPERATOR_IDS`
3. `Command(resume=operator_reply)` возобновляет граф
4. Auto-timeout: если оператор не ответил за `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES` → возврат к боту

### Fallback → Оператор

- Если `faq_lookup` не нашёл ответ (similarity < 0.62) → `fallback_streak += 1`
- После **3 fallback подряд** → показываем кнопку "Связаться с оператором"

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
