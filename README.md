# Telegram Bank Agent Bot (MVP)

Телеграм-бот для банка: регистрация пользователей, ведение одной активной сессии чата с агентом, хранение истории сообщений и интеграция с внешним агентом по HTTP.

## Стек
- Python 3.11+
- aiogram v3
- SQLAlchemy 2.x (async) + Alembic
- PostgreSQL или SQLite (через `DATABASE_URL`)
- Встроенный агент (langgraph + OpenAI)
- Redis — опционально (для rate-limit)
- Django admin для просмотра пользователей/сессий/сообщений
- Управление отделениями (branches) через Django admin

## Структура проекта
```
app/
  config.py               # загрузка настроек из env
  bot/
    handlers/             # команды и обработка сообщений
    keyboards/            # клавиатуры (запрос контакта)
  db/
    models.py             # User, ChatSession, Message
    session.py            # async engine/session
    alembic/              # миграции Alembic
      versions/0001_init.py
  services/
    agent_client.py       # HTTP-клиент к внешнему агенту
    chat_service.py       # бизнес-логика чатов/сессий/сообщений
    local_agent.py        # встроенный агент (langgraph + OpenAI)
  bot/
    ...
  db/
    ...
main.py                   # точка входа, настройка aiogram
manage.py                 # Django admin entrypoint
admin_site/               # Django проект (настройки/urls/wsgi)
bank_admin/               # Django app с моделями/админом (unmanaged, поверх существующих таблиц)
alembic.ini               # конфиг Alembic
requirements.txt
.env.example              # образец переменных окружения
```

## Переменные окружения
Скопируйте `.env.example` в `.env` и заполните:
- `BOT_TOKEN` — токен Telegram Bot API.
- `DATABASE_URL` — строка подключения (пример для SQLite: `sqlite+aiosqlite:///./bot.db`; для Postgres: `postgresql+asyncpg://user:pass@host:5432/db`).
- `REDIS_URL` — опционально, если нужен rate-limit.
- `LOG_LEVEL` — уровень логирования (`INFO` по умолчанию).
- `OPERATOR_IDS` — список Telegram ID операторов (через запятую) для уведомлений и ответов в режиме человека.
- Для локального агента через OpenAI: `OPENAI_API_KEY` и опционально `OPENAI_MODEL` (по умолчанию `gpt-4o-mini`).
- Для Django admin: `DJANGO_SECRET_KEY` (опционально), `DJANGO_DEBUG` (true/false). БД берётся из `DATABASE_URL`.

## Установка и запуск локально
1. Убедитесь, что установлен Python 3.11+.
2. Создайте и активируйте виртуальное окружение:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
3. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```
4. Настройте `.env` (см. выше).
5. Примените миграции Alembic:
   ```bash
   alembic upgrade head
   ```
   (используется `alembic.ini`, URL берётся из `DATABASE_URL`).
6. Запустите бота:
   ```bash
   python3 main.py
   ```
   Бот стартует в режиме long polling. Убедитесь, что Telegram доступен из окружения.
7. (Опционально) Запустите Django admin:
   ```bash
   python manage.py migrate        # создаст таблицы auth/session для админки
   python manage.py createsuperuser
   python manage.py collectstatic  # соберёт статику админки в STATIC_ROOT (нужно для DEBUG=false/whitenoise)
   python manage.py runserver
   ```
   Открыть: http://127.0.0.1:8000/admin

## Быстрый старт на SQLite
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# заполните BOT_TOKEN; для встроенного агента укажите OPENAI_API_KEY (AGENT_BASE_URL больше не нужен)
alembic upgrade head
python3 main.py
```

## Как работает бот
- `/start`: регистрация пользователя (telegram_user_id, username, имя/фамилия). Если телефона нет — просит поделиться контактом. Можно выбрать язык (ru/en/uz), сохраняется в `users.language`.
- Сессии: на пользователя максимум одна активная. `/new` создаёт новую, закрывая прежнюю; `/end` завершает текущую.
- Чат: любое текстовое сообщение сохраняется, проксируется во встроенный агент (langgraph+OpenAI), ответ сохраняется и отправляется пользователю.
- Хранение: таблицы `users`, `chat_sessions`, `messages` (latency_ms, agent_model, error_code для логирования ошибок агента).
- Авто-завершение сессии по неактивности (5 минут) с запросом оценки (1–5). Оценка сохраняется в `chat_sessions.feedback_rating`.
- Отделения: таблица `branches` (region/district/address/coords/реквизиты). Бот умеет показывать списки по Ташкенту/регионам, районы, отделения, искать ближайшее по геолокации.

## Локальный агент (in-process)
- При `AGENT_BASE_URL=local` используется встроенная логика из `app/services/local_agent.py` (перенесена из исходного `main.py`).
- Логика основана на langgraph + langchain + ChatOpenAI, требует `OPENAI_API_KEY` и (опционально) `OPENAI_MODEL`.
- Контекст по сессии поддерживается через `MemorySaver` по `session_id` (одна активная сессия на пользователя).

## Админ панель (Django)
- Запуск: `python manage.py migrate && python manage.py createsuperuser && python manage.py runserver`
- В админке доступны таблицы `users`, `chat_sessions`, `messages` (unmanaged, данные читаются из существующей базы), а также стандартные Django модели (auth/users и т.п. для входа).
- В админке можно добавлять/редактировать/удалять отделения (`branches`), поля: название, регион, район, адрес, ориентиры, метро, телефон, режим работы, реквизиты, координаты и транзитные счета.

## Миграции
- Создать новую миграцию (при изменении моделей):
  ```bash
  alembic revision -m "comment" --autogenerate
  ```
- Применить:
  ```bash
  alembic upgrade head
  ```
- Откатить:
  ```bash
  alembic downgrade -1
  ```

## Проверка
- Быстрая проверка синтаксиса:
  ```bash
  python3 -m compileall main.py app
  ```
Перед выкладкой убедитесь, что `AGENT_BASE_URL` отвечает, а база данных доступна по `DATABASE_URL`.
