# Complex Agent API

Telegram-бот банка с локальным LLM-агентом, хранением сессий в БД, сценариями подбора кредитов и операторским API на FastAPI.

## 1) Что внутри

- `Telegram bot` на `aiogram` (`main.py`, `app/bot/*`)
- `Локальный агент` (интенты, FAQ, сценарии, подбор, PDF) в `app/services/local_agent.py`
- `Сервис сессий/сообщений` в `app/services/chat_service.py`
- `Operator API` на FastAPI в `app/api/fastapi_app.py`
- `БД` через SQLAlchemy + Alembic (`app/db/*`)
- `Django admin` для ручного управления данными (`manage.py`, `bank_admin/*`)

## 2) Быстрый запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
python3 main.py
```

## 3) Переменные окружения

Минимум:

- `BOT_TOKEN` - токен Telegram-бота
- `DATABASE_URL` - например `sqlite+aiosqlite:///./bot.db`
- `OPENAI_API_KEY` - ключ для локального LLM-агента

Опционально:

- `OPENAI_MODEL` - по умолчанию `gpt-4o-mini`
- `LOG_LEVEL` - по умолчанию `INFO`
- `OPERATOR_IDS` - список Telegram ID операторов через запятую
- `OPERATOR_API_KEY` - ключ для FastAPI endpoint `/operator/send`
- `REDIS_URL` - если нужен внешний rate-limit/кэш

## 4) Документация агента

Код: `app/services/local_agent.py`

### 4.1 Поведение агента

- Классифицирует интент (`greeting`, `qa`, `mortgage`, `auto_loan`, `microloan`, `education`, `service`, `unknown`)
- Проверяет FAQ из БД (`faq`/`FaqItem`) и выбирает релевантный ответ
- Ведет пошаговые сценарии для:
  - `Ипотека`
  - `Автокредит`
  - `Микрозайм`
  - `Образовательный кредит`
- Парсит ответы клиента через LLM + fallback-парсеры (сумма, срок, взнос, тип дохода и т.д.)
- Формирует подбор (точные и близкие варианты), может собрать lead и PDF-график

### 4.2 Источники данных агента

- FAQ из БД (`FaqItem`)
- Продуктовые данные из `app/data/ai_chat_info.json`
- Подсказки/правила сценариев в константах `*_FLOW_QUESTIONS` внутри `local_agent.py`

### 4.3 Импорт FAQ из XLSX

Скрипт: `scripts/import_faq_xlsx.py`

```bash
python3 scripts/import_faq_xlsx.py "scripts/FAQ.xlsx" --replace
```

Полезные флаги:

- `--sheet <имя_листа>`
- `--limit <N>`
- `--dry-run`

## 5) Документация Telegram-бота

Код: `main.py`, `app/bot/handlers/commands.py`, `app/bot/keyboards/*`

### 5.1 Главное

- `/start` - регистрация пользователя, запрос контакта/языка при необходимости
- `/new` - новая сессия
- `/end` - завершение активной сессии
- Текстовые команды с кнопок:
  - `📞 Колл-центр`
  - `🏢 Отделения`
  - `💱 Курс валют`
  - `📍 Найти ближайший ЦБУ`
  - `🗂️ Мои сессии`
  - `🌐 Сменить язык`

### 5.2 Отделения

- По `🏢 Отделения` отправляются inline-кнопки региона (`🏙 Ташкент`, `🌍 Регионы`)
- Далее выбор района/области и вывод карточек отделений
- По геолокации ищется ближайший ЦБУ (haversine в `handle_location`)

### 5.3 Режим оператора

- После ответа бота показывается кнопка подключения оператора
- Оператор может отвечать командой:

```bash
/op <session_id> <текст>
```

- Список активных human-сессий:

```bash
/op_sessions
```

## 6) Документация FastAPI

Код: `app/api/fastapi_app.py`

### 6.1 Запуск

```bash
uvicorn app.api.fastapi_app:app --host 0.0.0.0 --port 8001
```

### 6.2 Endpoint

- `POST /operator/send`
- Назначение: отправить сообщение пользователю в Telegram от оператора и записать его в историю сессии

Тело запроса:

```json
{
  "session_id": "SESSION_ID",
  "text": "Здравствуйте, я оператор",
  "operator_name": "Ali",
  "operator_id": 123456
}
```

Пример:

```bash
curl -X POST http://127.0.0.1:8001/operator/send \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{
    "session_id":"SESSION_ID",
    "text":"Здравствуйте, я оператор",
    "operator_name":"Ali",
    "operator_id":123456
  }'
```

Коды:

- `200` - отправлено
- `401` - неверный `X-API-Key`
- `404` - сессия не найдена
- `409` - сессия уже закрыта
- `502` - ошибка отправки в Telegram API

## 7) Тестовые данные отделений

Скрипт: `scripts/seed_branches.py`

```bash
python3 scripts/seed_branches.py --replace
```

Что добавляет:

- 10 тестовых отделений в `Ташкент`
- Тестовые отделения в `Самарканд`, `Бухара`, `Андижан`

## 8) Миграции

```bash
alembic upgrade head
```

Новая миграция:

```bash
alembic revision -m "comment" --autogenerate
```

## 9) Проверка после изменений

```bash
python3 -m py_compile app/bot/handlers/commands.py app/services/local_agent.py app/services/chat_service.py app/api/fastapi_app.py
```

