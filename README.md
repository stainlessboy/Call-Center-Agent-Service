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

---

## Архитектура

```
Telegram → POST /telegram/webhook → FastAPI → aiogram Dispatcher
  → handlers → ChatService → AgentClient → LangGraph Agent
  → PostgreSQL (asyncpg)
```

```
Internet → Nginx (SSL) :443 → API container :8001
                                    ↓
                              PostgreSQL :5432
```

### LangGraph Agent (3 ноды + 12 инструментов)

```
START → router
          ├─► faq         (LLM + 12 тулов: приветствие, FAQ, продукты, сравнение...)
          ├─► calc_flow   (расчёт кредита/вклада + захват лида)
          └─► human_mode  (режим оператора, interrupt())
         → END
```

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

Заполнить обязательные:
- `BOT_TOKEN` — токен Telegram бота от [@BotFather](https://t.me/BotFather)
- `OPENAI_API_KEY` — ключ OpenAI API
- `ADMIN_PASSWORD` — пароль для админ-панели

### 4. Миграции и seed-данные

```bash
alembic upgrade head

python scripts/seed_credit_product_offers.py --replace
python scripts/seed_deposit_product_offers.py --replace
python scripts/seed_card_product_offers.py --replace
python scripts/import_faq_xlsx.py "scripts/FAQ.xlsx" --replace
```

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

### Требования к серверу

- Ubuntu 24.04 LTS
- Docker Engine 24+ и docker compose plugin
- Домен `agent-bot.uz` с DNS A-записью → IP сервера
- Открытые порты: 80, 443

### Шаг 1. Установить Docker на сервер

```bash
ssh root@<server-ip>

# Обновить систему
apt update && apt upgrade -y

# Установить Docker
curl -fsSL https://get.docker.com | sh

# Проверить
docker --version
docker compose version
```

### Шаг 2. Скопировать проект на сервер

**Вариант A — через git:**

```bash
git clone <repo-url> ~/agent-bot
cd ~/agent-bot
```

**Вариант B — через rsync (с локальной машины):**

```bash
rsync -avz --exclude '.venv' --exclude '.env' --exclude '__pycache__' \
  ./ root@<server-ip>:~/agent-bot/
ssh root@<server-ip>
cd ~/agent-bot
```

### Шаг 3. Настроить .env

```bash
cp .env.example .env
nano .env
```

Обязательные переменные для продакшена:

```env
BOT_TOKEN=<токен от BotFather>
OPENAI_API_KEY=<ключ OpenAI>
ADMIN_PASSWORD=<надёжный пароль>
ADMIN_SECRET_KEY=<случайная строка 32+ символов>
WEBHOOK_BASE_URL=https://agent-bot.uz
WEBHOOK_SECRET=<случайная строка>
OPERATOR_API_KEY=<случайная строка>
POSTGRES_PASSWORD=<надёжный пароль для БД>
```

Генерация случайных строк:

```bash
openssl rand -hex 32    # для ADMIN_SECRET_KEY
openssl rand -hex 16    # для WEBHOOK_SECRET
openssl rand -hex 16    # для OPERATOR_API_KEY
openssl rand -hex 16    # для POSTGRES_PASSWORD
```

### Шаг 4. Настроить DNS

Добавить A-запись в DNS-панели домена:

```
agent-bot.uz  →  A  →  <IP сервера>
```

Проверить распространение DNS:

```bash
dig +short agent-bot.uz
# должен показать IP сервера
```

### Шаг 5. Запустить деплой

```bash
bash deploy.sh
```

Скрипт автоматически:
1. Проверит `.env` и обязательные переменные
2. Получит SSL-сертификат через Let's Encrypt
3. Соберёт и запустит все контейнеры (db, api, nginx)
4. Применит миграции Alembic
5. Настроит nginx с HTTPS
6. Предложит загрузить seed-данные (продукты, FAQ)

### Шаг 6. Проверить

```bash
# Статус контейнеров
make prod-status

# Health check
curl https://agent-bot.uz/health

# Логи
make prod-logs

# Проверить webhook
curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
```

Открыть админ-панель: https://agent-bot.uz/admin/
Отправить `/start` боту в Telegram.

---

## Обновление (после изменений кода)

```bash
cd ~/agent-bot
git pull                # или rsync новые файлы
make prod-update        # rebuild + migrate + reload nginx
```

---

## Makefile команды

| Команда | Описание |
|---------|----------|
| `make help` | Показать все команды |
| **Development** | |
| `make dev` | Запустить локальный PostgreSQL |
| `make dev-down` | Остановить локальный PostgreSQL |
| `make test` | Запустить тесты (152 теста) |
| `make migrate` | Применить миграции локально |
| **Production** | |
| `make prod-deploy` | Первый деплой (SSL + build + migrate) |
| `make prod-update` | Обновить: rebuild + migrate + reload |
| `make prod-logs` | Логи api + nginx |
| `make prod-restart` | Рестарт API + reload nginx |
| `make prod-status` | Статус контейнеров |
| `make prod-migrate` | Применить миграции |
| `make prod-seed` | Загрузить seed-данные (продукты, FAQ) |
| `make prod-shell` | Bash в контейнер API |
| `make prod-down` | Остановить все контейнеры |
| `make prod-renew-ssl` | Обновить SSL-сертификат |

---

## Обновление SSL-сертификата

Сертификат Let's Encrypt действует 90 дней. Ручное обновление:

```bash
make prod-renew-ssl
```

Автоматическое обновление через cron:

```bash
crontab -e
# Добавить строку (проверка раз в 2 месяца в 3:00):
0 3 1 */2 * cd ~/agent-bot && docker compose -f docker-compose.prod.yml run --rm --entrypoint certbot certbot renew && docker compose -f docker-compose.prod.yml exec nginx nginx -s reload >> /var/log/certbot-renew.log 2>&1
```

---

## Docker-архитектура (production)

```
docker-compose.prod.yml
├── db        PostgreSQL 16 (healthcheck, volume pgdata)
├── api       Python app (Dockerfile, depends_on: db healthy)
├── nginx     Reverse proxy (SSL termination, ports 80+443)
└── certbot   Let's Encrypt certificate management
```

| Файл | Назначение |
|------|-----------|
| `Dockerfile` | Образ API (python:3.13-slim + requirements) |
| `docker-compose.prod.yml` | Production compose (db + api + nginx + certbot) |
| `docker-compose.yml` | Dev compose (только PostgreSQL) |
| `nginx/app-http.conf` | Nginx HTTP-конфиг (для certbot challenge) |
| `nginx/app-ssl.conf` | Nginx HTTPS-конфиг (proxy → api:8001) |
| `deploy.sh` | Скрипт первого деплоя с SSL |
| `.dockerignore` | Исключения для Docker build |

---

## Переменные окружения

| Переменная | Обязательна | По умолчанию | Описание |
|------------|:-----------:|:------------:|----------|
| `BOT_TOKEN` | ✅ | — | Telegram bot token |
| `OPENAI_API_KEY` | ✅ | — | OpenAI API key |
| `ADMIN_PASSWORD` | ✅ | — | Пароль админ-панели |
| `DATABASE_URL` | — | `...localhost:5432/bankbot` | Async SQLAlchemy URL |
| `POSTGRES_USER` | — | `bankbot` | PostgreSQL user |
| `POSTGRES_PASSWORD` | — | `bankbot` | PostgreSQL password |
| `POSTGRES_DB` | — | `bankbot` | PostgreSQL database |
| `WEBHOOK_BASE_URL` | prod | — | `https://agent-bot.uz` |
| `WEBHOOK_SECRET` | prod | — | Секрет для webhook |
| `WEBHOOK_PATH` | — | `/telegram/webhook` | Путь webhook |
| `OPERATOR_IDS` | — | — | Telegram ID операторов (через запятую) |
| `OPERATOR_API_KEY` | — | — | API-ключ для `/operator/send` |
| `ADMIN_USERNAME` | — | `admin` | Логин админ-панели |
| `ADMIN_SECRET_KEY` | prod | `change-me...` | Секрет сессий |
| `OPENAI_MODEL` | — | `gpt-4o-mini` | Модель OpenAI |
| `OPENAI_BASE_URL` | — | — | Кастомный OpenAI endpoint |
| `LOG_LEVEL` | — | `INFO` | Уровень логирования |
| `LANGGRAPH_CHECKPOINT_BACKEND` | — | `auto` | `memory\|postgres\|auto` |
| `SESSION_INACTIVITY_TIMEOUT_MINUTES` | — | `1440` | Таймаут неактивной сессии |
| `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES` | — | `10` | Таймаут оператора |
| `MAX_DIALOG_MESSAGES` | — | `50` | Лимит сообщений в истории |

---

## Гибридный режим (оператор)

- Кнопка «Подключить оператора» → `ChatSession.human_mode = True`
- Сообщения пользователя сохраняются в БД, но **не** отправляются в LangGraph
- Оператор отвечает через:
  - REST API: `POST /operator/send` (с `X-API-Key`)
  - Админ-панель: https://agent-bot.uz/admin/ → ChatSessions → operator_reply
- Фоновый watcher (60 сек) автоматически возвращает сессию боту, если оператор не ответил за `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES`

### Operator API

```bash
curl -X POST https://agent-bot.uz/operator/send \
  -H "X-API-Key: $OPERATOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "...", "text": "Привет!", "operator_name": "Ali", "operator_id": 123}'
```

---

## Структура проекта

```
complex-agent-api/
├── app/
│   ├── admin/           # SQLAdmin панель (auth, views, setup)
│   ├── agent/           # LangGraph AI-агент (17 модулей)
│   │   ├── i18n.py      # Переводы агента (ru/en/uz, ~80 ключей)
│   │   ├── tools.py     # 12 LangGraph-инструментов
│   │   ├── products.py  # Форматирование карточек продуктов
│   │   ├── intent.py    # Определение намерения (ru/en/uz)
│   │   ├── parsers.py   # Парсеры сумм и сроков (ru/en/uz)
│   │   └── nodes/       # Ноды графа (faq, calc_flow, router, human_mode)
│   ├── api/             # FastAPI: webhook, operator API, health
│   ├── bot/             # aiogram: хэндлеры, i18n, клавиатуры
│   ├── db/              # SQLAlchemy модели, Alembic миграции
│   ├── services/        # ChatService, AgentClient
│   └── utils/           # FAQ-поиск, PDF-генератор, text utils
├── scripts/             # Seed-скрипты для данных
├── tests/               # 152 теста
├── nginx/               # Конфиги nginx (HTTP/HTTPS)
├── docker-compose.yml         # Dev (PostgreSQL)
├── docker-compose.prod.yml    # Production (db + api + nginx + certbot)
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
| `🏠 Ипотека` | Список ипотечных программ |
| Выбрать продукт | Карточка продукта с кнопкой «Рассчитать» |
| `✅ Рассчитать платёж` | Вопрос о сумме → сроке → взносе → PDF |
| `забыл пароль приложения` | Ответ из FAQ |
| `сравни ипотечные программы` | Сравнение через LLM |
| `хочу оператора` | Кнопка подключения оператора |
| `I want a mortgage` (en) | Список ипотечных программ на английском |
| `Ipoteka olmoqchiman` (uz) | Список на узбекском |

---

## Troubleshooting

### Бот не отвечает

```bash
make prod-logs                    # проверить логи
curl https://agent-bot.uz/health  # проверить API

# Проверить webhook
curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
```

### Ошибка SSL-сертификата

```bash
dig +short agent-bot.uz                            # проверить DNS
rm -rf nginx/certbot/conf/live/agent-bot.uz        # удалить старый сертификат
bash deploy.sh                                      # переполучить
```

### Ошибка базы данных

```bash
docker compose -f docker-compose.prod.yml logs db   # логи PostgreSQL
docker compose -f docker-compose.prod.yml exec db psql -U bankbot bankbot  # подключиться
```

### Полный перезапуск

```bash
make prod-down
docker volume rm complex-agent-api_pgdata   # ⚠️ удалит все данные!
bash deploy.sh                               # всё с нуля
```
